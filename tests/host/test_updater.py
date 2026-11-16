"""updater: yt-dlp updates and repairs, and the host's self-update, against a local release server.

The server answers like GitHub's release downloads. The "yt-dlp.exe" files (single-file,
or inside a fake onedir zip) are small Python scripts run as `python <file> --version`,
so whether a download "runs" depends on its content, like the real exe. Nothing here touches the network
or the real install; the lock file and download folder are under tmp_path
(update_paths in conftest).
"""

import hashlib
import http.server
import io
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conftest import point_yt_dlp_at
from ytmp3_host import __version__, config, github, processes, updater, ytdlp
from ytmp3_host.github import DENO_REPO, HOST_REPO, YT_DLP_REPO
from ytmp3_host.updater import UpdateError

OLD, NEW = "2026.08.19", "2026.09.01"
CDN = "/cdn"


def script(version: str | None) -> bytes:
    """A stand-in yt-dlp.exe that prints version, or fails like a broken exe when version is None."""
    return (f"print({version!r})" if version else "raise SystemExit(1)").encode()


def sums(**files: bytes) -> bytes:
    """A sha256sum-style checksum list for name=content pairs (pass real asset names with **{...})."""
    return "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()).encode()


@dataclass
class ReleaseServer:
    """Files the local server serves (path -> bytes, or an int HTTP status), and the GETs it received."""
    base_url: str
    files: dict[str, bytes | int] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    drop: set[str] = field(default_factory=set)  # paths whose connection drops halfway

    def release(self, repo: str, tag: str, **assets: bytes | int) -> None:
        """Publish assets (name -> content, or an HTTP status to answer) for a release."""
        for name, data in assets.items():
            self.files[f"/{repo}/releases/download/{tag}/{name}"] = data

    def gets(self, name: str) -> int:
        """How many times an asset with this file name was downloaded."""
        return sum(1 for path in self.requests if path.endswith(f"/{name}"))


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[ReleaseServer]:
    """Serve ReleaseServer files on 127.0.0.1 and point github.GITHUB_URL and the updater's opener at it."""
    states: list[ReleaseServer] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state = states[0]
            if not self.path.startswith(CDN):  # GitHub redirects every asset to its download host
                self.send_response(302)
                self.send_header("Location", f"{state.base_url}{CDN}{self.path}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.path = self.path.removeprefix(CDN)
            state.requests.append(self.path)
            data = state.files.get(self.path, 404)
            if isinstance(data, int):
                self.send_response(data)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.path in state.drop:
                self.wfile.write(data[: len(data) // 2])  # then the connection closes
                return
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state = ReleaseServer(f"http://127.0.0.1:{httpd.server_address[1]}")
    states.append(state)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(github, "GITHUB_URL", state.base_url)
    monkeypatch.setattr(updater, "_opener", urllib.request.build_opener(urllib.request.ProxyHandler({})))
    yield state
    httpd.shutdown()
    httpd.server_close()


@dataclass
class Install:
    """The yt-dlp install the updater works on, in <tmp_path>/app."""
    exe: Path  # the single-file yt-dlp.exe (config.YT_DLP_PATH)

    @property
    def files(self) -> list[str]:
        """Every file in the install, relative to it, e.g. "yt-dlp/<tag>/yt-dlp.exe"."""
        app = self.exe.parent
        return sorted(p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file())

    def build(self, tag: str) -> Path:
        """The folder of the onedir build of release tag."""
        return self.exe.with_name("yt-dlp") / tag

    @property
    def onedir_failed(self) -> str | None:
        """The release the onedir-failed marker names, None without one."""
        marker = self.exe.with_name("yt-dlp") / "onedir-failed"
        return marker.read_text() if marker.exists() else None


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Install:
    """An installed single-file yt-dlp OLD, as hosts before onedir have it; yt-dlp is run as
    `python <exe> --version`; latest releases are unknown."""
    app = tmp_path / "app"
    app.mkdir()
    exe = app / "yt-dlp.exe"
    exe.write_bytes(script(OLD))
    point_yt_dlp_at(monkeypatch, exe)
    monkeypatch.setattr(updater, "DENO_PATH", app / "deno.exe")
    monkeypatch.setattr(
        updater,
        "yt_dlp_version_command",
        lambda path=None: [sys.executable, str(path or config.yt_dlp_path()), "--version"],
    )
    return Install(exe)


def latest(monkeypatch: pytest.MonkeyPatch, **tags: str | None) -> None:
    """Set the latest release per repo: latest(monkeypatch, yt_dlp="2026.09.01", host="v2.1.0")."""
    repos = {"yt_dlp": YT_DLP_REPO, "host": HOST_REPO, "deno": DENO_REPO}
    by_repo = {repos[key]: tag for key, tag in tags.items()}
    monkeypatch.setattr(updater, "latest_release_tag", lambda repo: by_repo.get(repo))


def publish_yt_dlp(server: ReleaseServer, tag: str = NEW, exe: bytes | None = None, checksum_of: bytes | None = None) -> bytes:
    """Publish yt-dlp release tag without an onedir build, so the update falls back to the single-file exe:
    exe (default: a working NEW) and a checksum for checksum_of (default: exe)."""
    exe = script(NEW) if exe is None else exe
    server.release(YT_DLP_REPO, tag, **{"yt-dlp.exe": exe, "SHA2-256SUMS": sums(**{"yt-dlp.exe": checksum_of or exe})})
    return exe


def onedir_zip(version: str | None = NEW, **extra: bytes) -> bytes:
    """A yt-dlp_win.zip like the real one, yt-dlp.exe and _internal/ at its root, plus the extra members
    (name -> content). Its yt-dlp.exe prints version only next to its _internal/ files, like the real one;
    with version None it fails like a broken build."""
    exe = (
        "import pathlib, sys\n"
        "if not (pathlib.Path(sys.argv[0]).parent / '_internal' / 'base_library.zip').exists(): raise SystemExit(1)\n"
        f"print({version!r})\n"
    ).encode() if version else script(None)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("yt-dlp.exe", exe)
        zf.writestr("_internal/base_library.zip", b"the Python standard library")
        for name, data in extra.items():
            zf.writestr(name, data)
    return archive.getvalue()


def publish_release(
    server: ReleaseServer, tag: str = NEW, archive: bytes | None = None, zip_checksum_of: bytes | None = None
) -> None:
    """Publish yt-dlp release tag like yt-dlp does: the onedir archive (default: a working build of tag) and a
    working single-file yt-dlp.exe of tag, with checksums of both (the zip's of zip_checksum_of if given)."""
    archive = onedir_zip(tag) if archive is None else archive
    exe = script(tag)
    checksums = sums(**{updater.YT_DLP_ZIP: zip_checksum_of or archive, "yt-dlp.exe": exe})
    server.release(YT_DLP_REPO, tag, **{updater.YT_DLP_ZIP: archive, "yt-dlp.exe": exe, "SHA2-256SUMS": checksums})


# --- checksums ----------------------------------------------------------------


def test_parse_checksums_reads_sha256sum_lines_and_skips_junk() -> None:
    a, b = "a" * 64, "B" * 64
    text = f"{a}  yt-dlp.exe\n{b} *Setup.exe\n\nnot a line\n{'c' * 63}  short\n"
    assert updater.parse_checksums(text) == {"yt-dlp.exe": a, "Setup.exe": "b" * 64}


def test_asset_url_quotes_the_tag_and_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "GITHUB_URL", "https://github.com")
    assert updater.asset_url("o/r", "v2.0.0", "a b.exe") == "https://github.com/o/r/releases/download/v2.0.0/a%20b.exe"


# --- the yt-dlp lookup --------------------------------------------------------


def test_the_lookup_takes_the_newest_onedir_build(install: Install) -> None:
    for name in ("2026.08.19", "2026.10.01", "2026.09.30", "2026.12.01.download"):  # .download: still unpacking
        install.build(name).mkdir(parents=True)
    install.build("2026.11.01").write_bytes(b"a file, not a build")
    assert config.yt_dlp_path() == install.build("2026.10.01") / "yt-dlp.exe"


def test_without_an_onedir_build_the_lookup_takes_the_single_exe(install: Install) -> None:
    assert config.yt_dlp_path() == install.exe
    install.exe.unlink()
    assert config.yt_dlp_path() == install.exe, "missing: the repair installs yt-dlp"


@pytest.mark.parametrize(
    ("failed", "single_exe", "expected"),
    [
        (NEW, True, "single"),  # the onedir build of the newest release failed
        ("2026.10.01", True, "single"),
        (OLD, True, "build"),  # an older failure: a newer onedir build works
        (NEW, False, "build"),  # the fallback could not be installed: the older build still works
        ("not a tag", True, "build"),
    ],
)
def test_the_lookup_takes_the_single_exe_for_a_release_whose_onedir_build_failed(
    install: Install, failed: str, single_exe: bool, expected: str
) -> None:
    install.build(NEW).mkdir(parents=True)
    (install.build(NEW).parent / "onedir-failed").write_text(failed)
    if not single_exe:
        install.exe.unlink()
    assert config.yt_dlp_path() == (install.exe if expected == "single" else install.build(NEW) / "yt-dlp.exe")


# --- yt-dlp onedir updates ----------------------------------------------------


def test_the_onedir_build_is_installed_from_the_checked_zip(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    publish_release(server)
    assert updater.update_yt_dlp() == NEW
    assert config.yt_dlp_path() == install.build(NEW) / "yt-dlp.exe"
    # the old single-file exe stays: a host Firefox still runs may use it
    assert install.files == ["yt-dlp.exe", f"yt-dlp/{NEW}/_internal/base_library.zip", f"yt-dlp/{NEW}/yt-dlp.exe"]
    assert install.onedir_failed is None
    assert server.requests == [
        f"/{YT_DLP_REPO}/releases/download/{NEW}/SHA2-256SUMS",
        f"/{YT_DLP_REPO}/releases/download/{NEW}/{updater.YT_DLP_ZIP}",
    ]


def test_a_current_single_exe_gets_the_onedir_build(install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch) -> None:
    # a host before onedir, e.g. run by the installer: its yt-dlp.exe is the latest release
    latest(monkeypatch, yt_dlp=OLD)
    publish_release(server, OLD)
    assert updater.update_yt_dlp() == OLD
    assert config.yt_dlp_path() == install.build(OLD) / "yt-dlp.exe"
    assert server.gets("yt-dlp.exe") == 0


def test_a_current_onedir_build_is_not_downloaded_again(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    publish_release(server)
    updater.update_yt_dlp()
    server.requests.clear()
    assert updater.update_yt_dlp() == NEW
    assert server.requests == []


def test_a_broken_onedir_build_of_the_release_is_replaced(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = install.build(NEW)
    build.mkdir(parents=True)
    (build / "yt-dlp.exe").write_bytes(script(None))  # e.g. a power cut before its files were on disk
    latest(monkeypatch, yt_dlp=NEW)
    publish_release(server)
    assert updater.update_yt_dlp() == NEW
    assert updater.read_version(updater.yt_dlp_version_command(build / "yt-dlp.exe")) == NEW


# --- a damaged yt-dlp (core.download_with_repair saw an ImportError) ---------------------------


def test_a_damaged_onedir_build_falls_back_to_the_single_exe_once(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    publish_release(server)
    updater.update_yt_dlp()
    assert updater.update_yt_dlp() == NEW, "--version works, so an update sees nothing to do"
    assert updater.update_yt_dlp(damaged=True) == NEW
    assert install.onedir_failed == NEW
    assert config.yt_dlp_path() == install.exe and install.exe.read_bytes() == script(NEW)
    # never reinstalled: a virus scanner would remove the file again, and every download would download it
    server.requests.clear()
    assert updater.update_yt_dlp(damaged=True) == NEW
    assert server.requests == []
    assert server.gets(updater.YT_DLP_ZIP) == 0
    updater.remove_unused_yt_dlp()  # the next get_version
    assert install.files == ["yt-dlp.exe", "yt-dlp/onedir-failed"], "the damaged build is deleted"
    latest(monkeypatch, yt_dlp="2026.10.01")
    publish_release(server, "2026.10.01")
    assert updater.update_yt_dlp() == "2026.10.01"
    assert config.yt_dlp_path() == install.build("2026.10.01") / "yt-dlp.exe", "the next release tries onedir again"


def test_a_damaged_outdated_onedir_build_gets_the_new_release(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    unpack_build(install, OLD)
    latest(monkeypatch, yt_dlp=NEW)
    publish_release(server)
    assert updater.update_yt_dlp(damaged=True) == NEW
    assert config.yt_dlp_path() == install.build(NEW) / "yt-dlp.exe"
    assert install.onedir_failed is None


def test_a_damaged_yt_dlp_offline_is_kept(install: Install) -> None:
    unpack_build(install, NEW)
    assert updater.update_yt_dlp(damaged=True) == NEW  # no_github
    assert install.onedir_failed is None


@pytest.mark.parametrize(
    ("archive", "limit"),
    [
        (b"not a zip", None),
        (onedir_zip(**{"../evil.exe": b"x"}), None),
        (onedir_zip(**{"C:/evil.exe": b"x"}), None),
        (onedir_zip(), "MAX_UNPACKED_BYTES"),
        (onedir_zip(None), None),
    ],
    ids=["bad-zip", "parent-path", "drive-path", "too-large", "does-not-run"],
)
def test_an_onedir_build_that_can_not_be_installed_falls_back_to_the_single_exe(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch, archive: bytes, limit: str | None
) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    publish_release(server, archive=archive)  # its checksum matches: the release itself is like this
    if limit:
        monkeypatch.setattr(updater, limit, 100)
    assert updater.update_yt_dlp() == NEW
    assert install.onedir_failed == NEW
    assert config.yt_dlp_path() == install.exe
    assert install.exe.read_bytes() == script(NEW)
    assert install.files == ["yt-dlp.exe", "yt-dlp/onedir-failed"], "nothing unpacked is left, nothing outside"


def test_an_onedir_build_that_can_not_be_renamed_into_place_falls_back(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    rename = Path.rename

    def held(self: Path, target: Path) -> Path:
        if self.name == f"{NEW}.download":  # e.g. a virus scanner holds a file in it
            raise PermissionError(13, "Access is denied", str(self))
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", held)
    latest(monkeypatch, yt_dlp=NEW)
    publish_release(server)
    assert updater.update_yt_dlp() == NEW
    assert install.files == ["yt-dlp.exe", "yt-dlp/onedir-failed"]


@pytest.mark.parametrize("problem", ["damaged", "dropped"])
def test_a_damaged_or_dropped_onedir_download_fails_without_the_fallback(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    if problem == "damaged":
        publish_release(server, zip_checksum_of=b"what the release really holds")
    else:
        publish_release(server)
        server.drop.add(f"/{YT_DLP_REPO}/releases/download/{NEW}/{updater.YT_DLP_ZIP}")
    with pytest.raises(UpdateError, match="damaged" if problem == "damaged" else "connection dropped"):
        updater.update_yt_dlp()
    assert install.onedir_failed is None, "the next update tries onedir again"
    assert install.files == ["yt-dlp.exe"]
    assert install.exe.read_bytes() == script(OLD)
    assert server.gets("yt-dlp.exe") == 0


def test_a_release_whose_onedir_build_failed_keeps_the_single_exe(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    publish_release(server, archive=onedir_zip(None))
    updater.update_yt_dlp()
    server.requests.clear()
    assert updater.update_yt_dlp() == NEW
    assert server.requests == [], "the current fallback is kept"
    install.exe.write_bytes(script(None))  # the fallback broke
    assert updater.update_yt_dlp() == NEW
    assert server.gets(updater.YT_DLP_ZIP) == 0, "onedir is not tried again for this release"
    assert server.gets("yt-dlp.exe") == 1


def test_the_next_release_tries_onedir_again(install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    publish_release(server, archive=onedir_zip(None))
    updater.update_yt_dlp()
    latest(monkeypatch, yt_dlp="2026.10.01")
    publish_release(server, "2026.10.01")
    assert updater.update_yt_dlp() == "2026.10.01"
    assert config.yt_dlp_path() == install.build("2026.10.01") / "yt-dlp.exe"
    assert server.gets("yt-dlp.exe") == 1, "only the fallback of the failed release"


def test_a_fallback_is_used_over_an_older_onedir_build(install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch) -> None:
    latest(monkeypatch, yt_dlp=OLD)
    publish_release(server, OLD)
    updater.update_yt_dlp()  # the onedir build of OLD
    latest(monkeypatch, yt_dlp=NEW)
    publish_release(server, archive=onedir_zip(None))
    assert updater.update_yt_dlp() == NEW
    assert config.yt_dlp_path() == install.exe, "not the older, outdated build"
    server.requests.clear()
    assert updater.update_yt_dlp() == NEW
    assert server.requests == []


def test_a_killed_onedir_install_is_cleaned_up(install: Install) -> None:
    unpacking = install.build(NEW).with_name(f"{NEW}.download")
    (unpacking / "_internal").mkdir(parents=True)
    (unpacking / "_internal" / "base_library.zip").write_bytes(b"partial")
    unpacking.with_name(f"{NEW}.download.zip").write_bytes(b"partial")
    assert updater.update_yt_dlp() == OLD  # offline: no_github
    assert install.files == ["yt-dlp.exe"]


# --- removing the yt-dlp files no longer used ---------------------------------


def unpack_build(install: Install, tag: str, version: str | None = None) -> Path:
    """Put the onedir build of release tag in place, as the updater leaves it; it prints version (default: tag)."""
    with zipfile.ZipFile(io.BytesIO(onedir_zip(version or tag))) as zf:
        zf.extractall(install.build(tag))
    return install.build(tag)


def build_files(*tags: str) -> list[str]:
    """Install.files of the onedir builds of tags."""
    return sorted(f"yt-dlp/{tag}/{name}" for tag in tags for name in ("_internal/base_library.zip", "yt-dlp.exe"))


def test_unused_yt_dlp_files_are_removed_once_an_onedir_build_works(install: Install) -> None:
    unpack_build(install, OLD)
    unpack_build(install, NEW)
    install.exe.with_name("yt-dlp.old.exe").write_bytes(script("2026.01.01"))
    install.exe.with_name("yt-dlp.download.exe").write_bytes(b"half an exe")
    (install.build(NEW).parent / "onedir-failed").write_text("2026.01.01")
    killed = install.build("2026.10.01").with_name("2026.10.01.download")
    (killed / "_internal").mkdir(parents=True)
    updater.remove_unused_yt_dlp()
    assert install.files == build_files(NEW)
    assert [p.name for p in install.build(NEW).parent.iterdir()] == [NEW], "no empty folders left"
    assert config.yt_dlp_path() == install.build(NEW) / "yt-dlp.exe"


def test_an_older_build_alone_is_removed(install: Install) -> None:
    # the usual state after a second onedir release: no single-file exe or marker left
    install.exe.unlink()
    unpack_build(install, OLD)
    unpack_build(install, NEW)
    updater.remove_unused_yt_dlp()
    assert install.files == build_files(NEW)


@pytest.mark.parametrize("state", ["no-build", "broken-build"])
def test_nothing_is_removed_without_a_working_onedir_build(install: Install, state: str) -> None:
    install.exe.with_name("yt-dlp.old.exe").write_bytes(script("2026.01.01"))
    if state == "broken-build":  # the newest build, e.g. after a power cut: the update replaces it
        unpack_build(install, OLD)
        install.build(NEW).mkdir()
        (install.build(NEW) / "yt-dlp.exe").write_bytes(script(None))
    before = install.files
    updater.remove_unused_yt_dlp()
    assert install.files == before


def test_a_build_that_hangs_is_checked_only_briefly(install: Install, monkeypatch: pytest.MonkeyPatch) -> None:
    # get_version checks the build again afterward, and must answer within the extension's timeout
    monkeypatch.setattr(updater, "CLEANUP_VERSION_TIMEOUT_S", 1)
    unpack_build(install, NEW)
    (install.build(NEW) / "yt-dlp.exe").write_bytes(b"import time; time.sleep(30)")
    before = install.files
    start = time.monotonic()
    updater.remove_unused_yt_dlp()
    assert time.monotonic() - start < 10
    assert install.files == before


def test_with_the_single_exe_fallback_only_the_onedir_builds_are_removed(install: Install) -> None:
    # the onedir build of NEW was damaged (or failed): the single-file NEW is used, no build ever again
    unpack_build(install, OLD)
    unpack_build(install, NEW)
    install.exe.write_bytes(script(NEW))
    (install.build(NEW).parent / "onedir-failed").write_text(NEW)
    with (install.build(OLD) / "yt-dlp.exe").open("rb"):  # another host still runs it
        updater.remove_unused_yt_dlp()
        assert install.files == ["yt-dlp.exe", *build_files(OLD), "yt-dlp/onedir-failed"]
    updater.remove_unused_yt_dlp()
    assert install.files == ["yt-dlp.exe", "yt-dlp/onedir-failed"]
    assert config.yt_dlp_path() == install.exe


def test_nothing_is_removed_while_an_update_runs(install: Install) -> None:
    unpack_build(install, NEW)
    with updater.update_lock():
        start = time.monotonic()
        updater.remove_unused_yt_dlp()
        # any wait for the lock sleeps at least one poll
        assert time.monotonic() - start < updater.LOCK_POLL_S, "it does not wait for the update"
        assert install.files == ["yt-dlp.exe", *build_files(NEW)]
    updater.remove_unused_yt_dlp()
    assert install.files == build_files(NEW)


def test_yt_dlp_files_another_host_still_runs_are_removed_later(install: Install) -> None:
    unpack_build(install, OLD)
    unpack_build(install, NEW)
    # an open file can't be deleted, and its folder can't be renamed, like a running exe and its build
    with install.exe.open("rb"), (install.build(OLD) / "yt-dlp.exe").open("rb"):
        updater.remove_unused_yt_dlp()
        assert install.files == ["yt-dlp.exe", *build_files(OLD, NEW)], "the running build is left whole"
    updater.remove_unused_yt_dlp()
    assert install.files == build_files(NEW)


def test_an_update_leaves_the_files_it_replaced_to_the_cleanup(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest(monkeypatch, yt_dlp=OLD)
    publish_release(server, OLD)
    updater.update_yt_dlp()
    latest(monkeypatch, yt_dlp=NEW)
    publish_release(server)
    updater.update_yt_dlp()
    # the installer runs update_yt_dlp while Firefox still uses the previous host and its yt-dlp
    assert install.files == ["yt-dlp.exe", *build_files(OLD, NEW)]
    updater.remove_unused_yt_dlp()
    assert install.files == build_files(NEW)


# --- the single-file yt-dlp.exe (fallback) ------------------------------------


def test_a_release_without_an_onedir_build_installs_the_single_exe(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    new_exe = publish_yt_dlp(server)
    assert updater.update_yt_dlp() == NEW
    assert install.exe.read_bytes() == new_exe
    assert install.onedir_failed == NEW
    assert install.files == ["yt-dlp.exe", "yt-dlp/onedir-failed"]  # the replaced exe is deleted right away
    assert server.requests == [
        f"/{YT_DLP_REPO}/releases/download/{NEW}/SHA2-256SUMS",
        f"/{YT_DLP_REPO}/releases/download/{NEW}/yt-dlp.exe",
    ]


def test_a_replaced_exe_still_running_is_removed_by_the_next_update(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_unlink = Path.unlink

    def running(path: Path, missing_ok: bool = False) -> None:
        if path.name.startswith("yt-dlp.old"):
            raise PermissionError(13, "Access is denied")  # like a running exe
        real_unlink(path, missing_ok=missing_ok)

    latest(monkeypatch, yt_dlp=NEW)
    publish_yt_dlp(server)
    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", running)
        assert updater.update_yt_dlp() == NEW
    assert install.files == ["yt-dlp.exe", "yt-dlp.old.exe", "yt-dlp/onedir-failed"]
    assert updater.read_version(updater.yt_dlp_version_command(install.exe.with_name("yt-dlp.old.exe"))) == OLD
    latest(monkeypatch, yt_dlp="2026.10.01")
    publish_yt_dlp(server, "2026.10.01", script("2026.10.01"))
    assert updater.update_yt_dlp() == "2026.10.01"
    assert install.files == ["yt-dlp.exe", "yt-dlp/onedir-failed"]


def test_a_current_single_exe_is_kept_when_the_onedir_build_can_not_be_downloaded(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest(monkeypatch, yt_dlp=OLD)  # nothing published: every download fails
    assert updater.update_yt_dlp() == OLD
    assert install.files == ["yt-dlp.exe"], "no onedir-failed: the next update tries onedir again"


@pytest.mark.parametrize("state", ["missing", "broken"])
def test_a_missing_or_broken_yt_dlp_is_repaired(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    if state == "missing":
        install.exe.unlink()
    else:
        install.exe.write_bytes(script(None))
    latest(monkeypatch, yt_dlp=NEW)
    new_exe = publish_yt_dlp(server)
    assert updater.update_yt_dlp() == NEW
    assert install.exe.read_bytes() == new_exe


def test_a_damaged_download_is_never_installed(install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    publish_yt_dlp(server, exe=script(NEW), checksum_of=b"what the release really holds")
    with pytest.raises(UpdateError, match="damaged"):
        updater.update_yt_dlp()
    assert install.exe.read_bytes() == script(OLD)
    assert install.files == ["yt-dlp.exe", "yt-dlp/onedir-failed"]


def test_a_download_that_does_not_run_is_never_installed(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    publish_yt_dlp(server, exe=script(None))  # checksum matches, but it fails to run
    with pytest.raises(UpdateError, match="does not run"):
        updater.update_yt_dlp()
    assert install.exe.read_bytes() == script(OLD)
    assert install.files == ["yt-dlp.exe", "yt-dlp/onedir-failed"]


def test_a_dropped_connection_leaves_the_install_as_it_was(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    publish_yt_dlp(server, exe=script(NEW) * 1000)
    server.drop.add(f"/{YT_DLP_REPO}/releases/download/{NEW}/yt-dlp.exe")
    # the checksum would catch it too; the size check says what happened
    with pytest.raises(UpdateError, match=r"Could not download yt-dlp\.exe: the connection dropped"):
        updater.update_yt_dlp()
    assert install.exe.read_bytes() == script(OLD)
    assert install.files == ["yt-dlp.exe", "yt-dlp/onedir-failed"]


@pytest.mark.parametrize(
    ("assets", "message"),
    [
        ({"yt-dlp.exe": script(NEW)}, "Could not download SHA2-256SUMS"),
        ({"yt-dlp.exe": script(NEW), "SHA2-256SUMS": sums(other=b"x")}, "no checksum for yt-dlp.exe"),
        ({"yt-dlp.exe": 500, "SHA2-256SUMS": sums(**{"yt-dlp.exe": script(NEW)})}, "Could not download yt-dlp.exe"),
    ],
    ids=["no-checksum-file", "not-listed", "server-error"],
)
def test_release_problems_are_errors(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch, assets: dict[str, bytes | int], message: str
) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    server.release(YT_DLP_REPO, NEW, **assets)
    with pytest.raises(UpdateError, match=message):
        updater.update_yt_dlp()
    assert install.exe.read_bytes() == script(OLD)


@pytest.mark.parametrize(("limit", "message"), [("MAX_DOWNLOAD_BYTES", "larger than expected"), ("YT_DLP_DOWNLOAD_S", "took too long")])
def test_too_large_or_too_slow_downloads_are_stopped(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch, limit: str, message: str
) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    publish_yt_dlp(server)
    monkeypatch.setattr(updater, limit, -1)
    with pytest.raises(UpdateError, match=message):
        updater.update_yt_dlp()
    assert install.files == ["yt-dlp.exe", "yt-dlp/onedir-failed"]


def test_offline_with_a_working_yt_dlp_keeps_it(install: Install) -> None:
    # no_github (conftest): the latest release is unknown
    assert updater.update_yt_dlp() == OLD


def test_offline_without_a_working_yt_dlp_is_an_error(install: Install) -> None:
    install.exe.unlink()
    with pytest.raises(UpdateError, match="yt-dlp could not be downloaded: GitHub could not be reached"):
        updater.update_yt_dlp()


def test_a_release_tag_that_is_no_version_is_refused(install: Install, monkeypatch: pytest.MonkeyPatch) -> None:
    install.exe.unlink()
    latest(monkeypatch, yt_dlp="../../evil")
    with pytest.raises(UpdateError, match="no version number"):
        updater.update_yt_dlp()


# --- recovery after a killed update -------------------------------------------


def test_killed_between_the_renames_puts_the_old_exe_back_offline(install: Install) -> None:
    install.exe.rename(install.exe.with_name("yt-dlp.old.exe"))
    assert updater.update_yt_dlp() == OLD  # offline: no_github
    assert install.files == ["yt-dlp.exe"]


def test_the_newest_old_copy_is_restored(install: Install) -> None:
    older = install.exe.with_name("yt-dlp.old-1234abcd.exe")
    older.write_bytes(script("2026.01.01"))
    install.exe.rename(install.exe.with_name("yt-dlp.old.exe"))
    time.sleep(0.05)
    install.exe.with_name("yt-dlp.old.exe").touch()  # newer than older
    assert updater.update_yt_dlp() == OLD


def test_killed_mid_download_leaves_only_a_partial_that_is_removed(install: Install) -> None:
    install.exe.with_name("yt-dlp.download.exe").write_bytes(b"half an exe")
    assert updater.update_yt_dlp() == OLD
    assert install.files == ["yt-dlp.exe"]


def test_an_old_copy_still_in_use_is_left_alone(install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch) -> None:
    old = install.exe.with_name("yt-dlp.old.exe")
    old.write_bytes(script("2026.01.01"))
    latest(monkeypatch, yt_dlp=NEW)
    publish_yt_dlp(server)
    with old.open("rb"):  # an open file can't be deleted on Windows, like a running exe
        assert updater.update_yt_dlp() == NEW
    # the swap moved the replaced exe to a unique name instead, and deleted it
    assert install.files == ["yt-dlp.exe", "yt-dlp.old.exe", "yt-dlp/onedir-failed"]
    assert old.read_bytes() == script("2026.01.01")


def test_a_swap_that_fails_keeps_the_working_exe(install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    publish_yt_dlp(server)
    rename = Path.rename

    def locked_download(self: Path, target: Path) -> Path:
        if self.name == "yt-dlp.download.exe":  # e.g. a virus scanner still holds it
            raise PermissionError(13, "Access is denied", str(self))
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", locked_download)
    with pytest.raises(UpdateError, match="could not be installed"):
        updater.update_yt_dlp()
    assert install.files == ["yt-dlp.exe", "yt-dlp/onedir-failed"]
    assert install.exe.read_bytes() == script(OLD)


# --- the update lock ----------------------------------------------------------


def test_a_held_lock_makes_a_second_update_wait_and_give_up(install: Install, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater, "LOCK_TIMEOUT_S", 0.5)
    with updater.update_lock():
        start = time.monotonic()
        with pytest.raises(UpdateError, match="Another update is still running"):
            updater.update_yt_dlp()
        assert time.monotonic() - start >= 0.5


def test_a_lock_held_by_a_killed_process_is_free(tmp_path: Path) -> None:
    lock = tmp_path / "update.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", (
            "import msvcrt, sys, time\n"
            f"f = open({str(lock)!r}, 'a+b'); f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)\n"
            "print('locked', flush=True); time.sleep(60)\n"
        )],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        with pytest.raises(UpdateError, match="Another update"), updater.update_lock(lock, timeout=0.2):
            pass
    finally:
        ytdlp.kill_process_tree(holder)  # the venv's python.exe is a launcher with the real Python as its child
        holder.communicate()
    with updater.update_lock(lock, timeout=2):  # Windows dropped the dead process's lock
        pass


def test_two_updates_at_once_download_once(install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    publish_yt_dlp(server)
    results: list[str] = []
    threads = [threading.Thread(target=lambda: results.append(updater.update_yt_dlp())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert results == [NEW, NEW]
    assert server.gets("yt-dlp.exe") == 1, "the second saw the first's update and did not download"


# --- Deno ---------------------------------------------------------------------

DENO_TAG = "v2.9.8"


@pytest.fixture
def deno(install: Install, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where Deno goes (next to yt-dlp.exe, not there yet); "deno.exe" is run like the fake yt-dlp scripts."""
    exe = install.exe.with_name("deno.exe")  # the install fixture's DENO_PATH
    monkeypatch.setattr(updater, "deno_version_command", lambda path=None: [sys.executable, str(path or exe)])
    latest(monkeypatch, deno=DENO_TAG)
    return exe


def publish_deno(server: ReleaseServer, exe: bytes = script("2.9.8"), checksum_of: bytes | None = None) -> None:
    """Publish the Deno zip and its checksum in the Get-FileHash list format Deno's releases use."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("deno.exe", exe)
    data = archive.getvalue()
    digest = hashlib.sha256(checksum_of or data).hexdigest().upper()
    checksum = f"\r\nAlgorithm : SHA256\r\nHash      : {digest}\r\nPath      : C:\\a\\deno\\{updater.DENO_ASSET}\r\n\r\n"
    server.release(DENO_REPO, DENO_TAG, **{updater.DENO_ASSET: data, updater.DENO_SUMS: checksum.encode()})


def test_deno_is_installed_from_the_checked_zip(deno: Path, install: Install, server: ReleaseServer) -> None:
    publish_deno(server)
    assert updater.install_and_update_deno() == "2.9.8"
    assert deno.read_bytes() == script("2.9.8")
    assert install.files == ["deno.exe", "yt-dlp.exe"]  # no zip or download left


def test_a_current_deno_is_not_downloaded_again(deno: Path, server: ReleaseServer) -> None:
    deno.write_bytes(script("2.9.8"))
    assert updater.install_and_update_deno() == "2.9.8"
    assert server.requests == []


def test_a_broken_deno_is_replaced(deno: Path, server: ReleaseServer) -> None:
    deno.write_bytes(script(None))
    publish_deno(server)
    assert updater.install_and_update_deno() == "2.9.8"


def test_a_damaged_deno_is_never_installed(deno: Path, install: Install, server: ReleaseServer) -> None:
    publish_deno(server, checksum_of=b"the real zip")
    with pytest.raises(UpdateError, match="Deno could not be installed: .* damaged"):
        updater.install_and_update_deno()
    assert install.files == ["yt-dlp.exe"]


def test_a_deno_that_does_not_run_is_never_installed(deno: Path, install: Install, server: ReleaseServer) -> None:
    publish_deno(server, exe=script(None))
    with pytest.raises(UpdateError, match="does not run"):
        updater.install_and_update_deno()
    assert install.files == ["yt-dlp.exe"]


def kill_leftovers(deno: Path) -> list[Path]:
    """The files a Deno install killed mid-way leaves next to deno.exe."""
    leftovers = [deno.with_name("deno.download.zip"), deno.with_name("deno.download.exe")]
    for path in leftovers:
        path.write_bytes(b"partial")
    return leftovers


def test_a_killed_deno_install_is_cleaned_up_by_the_next_one(deno: Path, install: Install, server: ReleaseServer) -> None:
    deno.write_bytes(script("2.9.8"))  # e.g. another host finished it
    kill_leftovers(deno)
    updater.install_and_update_deno()
    assert install.files == ["deno.exe", "yt-dlp.exe"]


def test_a_killed_deno_install_is_cleaned_up_by_a_yt_dlp_update(deno: Path, install: Install) -> None:
    kill_leftovers(deno)
    assert updater.update_yt_dlp() == OLD  # offline: no_github
    assert install.files == ["yt-dlp.exe"]


def test_deno_offline_is_an_error(deno: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    latest(monkeypatch)
    with pytest.raises(UpdateError, match="Deno could not be installed: GitHub could not be reached"):
        updater.install_and_update_deno()


def test_an_outdated_deno_is_replaced(deno: Path, install: Install, server: ReleaseServer) -> None:
    deno.write_bytes(script("2.9.7"))
    publish_deno(server)
    assert updater.install_and_update_deno() == "2.9.8"
    assert deno.read_bytes() == script("2.9.8")
    assert install.files == ["deno.exe", "yt-dlp.exe"]


def test_offline_an_outdated_working_deno_is_kept(deno: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deno.write_bytes(script("2.9.7"))
    latest(monkeypatch)
    assert updater.install_and_update_deno() == "2.9.7"
    assert deno.read_bytes() == script("2.9.7")


def test_new_exe_doesnt_run_so_the_old_one_stays(deno: Path, install: Install, server: ReleaseServer) -> None:
    deno.write_bytes(script("2.9.7"))
    publish_deno(server, exe=script(None))  # the new one fails to run
    with pytest.raises(UpdateError, match="does not run"):
        updater.install_and_update_deno()
    assert deno.read_bytes() == script("2.9.7")
    assert install.files == ["deno.exe", "yt-dlp.exe"]


def test_a_deno_killed_between_the_renames_is_restored(deno: Path, install: Install, server: ReleaseServer) -> None:
    deno.with_name("deno.old.exe").write_bytes(script("2.9.8"))
    assert updater.install_and_update_deno() == "2.9.8"
    assert server.requests == [], "the old exe is restored, not downloaded"
    assert install.files == ["deno.exe", "yt-dlp.exe"]


def test_a_replaced_deno_still_running_is_removed_by_the_next_call(
    deno: Path, install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    deno.write_bytes(script("2.9.7"))
    publish_deno(server)
    real_unlink = Path.unlink

    def running(path: Path, missing_ok: bool = False) -> None:
        if path.name.startswith("deno.old"):
            raise PermissionError(13, "Access is denied")  # like a running exe
        real_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", running)
        assert updater.install_and_update_deno() == "2.9.8"
    assert deno.read_bytes() == script("2.9.8")
    assert deno.with_name("deno.old.exe").read_bytes() == script("2.9.7"), "renamed aside, not deleted"
    assert updater.install_and_update_deno() == "2.9.8"  # that host has finished
    assert install.files == ["deno.exe", "yt-dlp.exe"]
    assert server.gets(updater.DENO_ASSET) == 1


def test_two_deno_updates_at_once_download_once(deno: Path, server: ReleaseServer) -> None:
    deno.write_bytes(script("2.9.7"))
    publish_deno(server)
    results: list[str] = []
    threads = [threading.Thread(target=lambda: results.append(updater.install_and_update_deno())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert results == ["2.9.8", "2.9.8"]
    assert server.gets(updater.DENO_ASSET) == 1, "the second saw the first's update and did not download"


# --- host self-update ---------------------------------------------------------


@dataclass
class Launches:
    commands: list[list[str]] = field(default_factory=list)


@pytest.fixture
def host_install(monkeypatch: pytest.MonkeyPatch) -> Launches:
    """An installed host whose installer launches are recorded instead of run."""
    monkeypatch.setattr(updater, "INSTALLED", True)
    launches = Launches()
    monkeypatch.setattr(updater, "launch_detached", launches.commands.append)
    return launches


SETUP = b"MZ fake installer"


def publish_host(server: ReleaseServer, tag: str = "v99.0.0", installer: bytes = SETUP, checksum_of: bytes | None = None) -> None:
    server.release(HOST_REPO, tag, **{
        updater.INSTALLER_ASSET: installer,
        "SHA256SUMS": sums(**{updater.INSTALLER_ASSET: checksum_of or installer}),
    })


def test_a_host_that_is_not_installed_does_not_update_itself(server: ReleaseServer, monkeypatch: pytest.MonkeyPatch) -> None:
    latest(monkeypatch, host="v99.0.0")
    with pytest.raises(UpdateError, match="Only an installed app"):
        updater.update_host()
    assert server.requests == []


def test_the_latest_host_has_nothing_to_install(host_install: Launches, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch) -> None:
    latest(monkeypatch, host=f"v{__version__}")
    assert updater.update_host() is None
    assert server.requests == [] and host_install.commands == []


def test_a_newer_host_is_downloaded_checked_and_started_silently(
    host_install: Launches, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    latest(monkeypatch, host="v99.0.0")
    publish_host(server)
    assert updater.update_host() == "v99.0.0"
    folder = tmp_path / "update" / "v99.0.0"
    installer = folder / updater.INSTALLER_ASSET
    assert installer.read_bytes() == SETUP
    assert host_install.commands == [[str(installer), *updater.INSTALLER_ARGS, f"/LOG={folder / 'install.log'}"]]
    assert "/VERYSILENT" in host_install.commands[0]


def test_a_started_installer_is_not_started_again_right_away(
    host_install: Launches, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest(monkeypatch, host="v99.0.0")
    publish_host(server)
    updater.update_host()
    assert updater.update_host() == "v99.0.0"
    assert len(host_install.commands) == 1
    assert server.gets(updater.INSTALLER_ASSET) == 1


def test_a_failed_install_is_not_retried_until_the_next_day(
    host_install: Launches, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest(monkeypatch, host="v99.0.0")
    publish_host(server)
    monkeypatch.setattr(updater, "INSTALL_GRACE_S", 0)
    updater.update_host()
    with pytest.raises(UpdateError, match=r"The v99\.0\.0 update did not install, next try in about 24 h"):
        updater.update_host()
    assert len(host_install.commands) == 1


def test_after_the_retry_period_the_checked_installer_is_reused(
    host_install: Launches, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest(monkeypatch, host="v99.0.0")
    publish_host(server)
    monkeypatch.setattr(updater, "INSTALL_GRACE_S", 0)
    monkeypatch.setattr(updater, "INSTALL_RETRY_S", 0)
    updater.update_host()
    updater.update_host()
    assert len(host_install.commands) == 2
    assert server.gets(updater.INSTALLER_ASSET) == 1, "the verified copy is started again, not downloaded"


def test_a_release_republished_under_the_same_tag_is_installed_at_once(
    host_install: Launches, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    latest(monkeypatch, host="v99.0.0")
    publish_host(server)
    monkeypatch.setattr(updater, "INSTALL_GRACE_S", 0)
    updater.update_host()
    publish_host(server, installer=b"MZ fixed installer")
    assert updater.update_host() == "v99.0.0"
    assert len(host_install.commands) == 2
    assert (tmp_path / "update" / "v99.0.0" / updater.INSTALLER_ASSET).read_bytes() == b"MZ fixed installer"


def test_a_damaged_installer_is_never_started(
    host_install: Launches, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    latest(monkeypatch, host="v99.0.0")
    publish_host(server, checksum_of=b"the real installer")
    with pytest.raises(UpdateError, match="damaged"):
        updater.update_host()
    assert host_install.commands == []
    assert list((tmp_path / "update" / "v99.0.0").iterdir()) == []


def test_a_cached_installer_that_changed_is_downloaded_again(
    host_install: Launches, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    latest(monkeypatch, host="v99.0.0")
    publish_host(server)
    folder = tmp_path / "update" / "v99.0.0"
    folder.mkdir(parents=True)
    (folder / updater.INSTALLER_ASSET).write_bytes(b"tampered")
    updater.update_host()
    assert (folder / updater.INSTALLER_ASSET).read_bytes() == SETUP


def test_an_installer_that_can_not_start_is_an_error(
    host_install: Launches, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    latest(monkeypatch, host="v99.0.0")
    publish_host(server)

    def _fail(command: list[str]) -> None:
        raise OSError("blocked by antivirus")

    monkeypatch.setattr(updater, "launch_detached", _fail)
    with pytest.raises(UpdateError, match="Could not start the app installer: blocked by antivirus"):
        updater.update_host()
    assert not (tmp_path / "update" / "v99.0.0" / updater.STARTED_MARKER).exists(), "the next check may try again"


def test_downloads_of_older_releases_are_removed(
    host_install: Launches, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stale = tmp_path / "update" / "v98.0.0"
    stale.mkdir(parents=True)
    (stale / updater.INSTALLER_ASSET).write_bytes(b"old")
    latest(monkeypatch, host="v99.0.0")
    publish_host(server)
    updater.update_host()
    assert [p.name for p in (tmp_path / "update").iterdir()] == ["v99.0.0"]


def test_installed_downloads_are_removed_and_newer_ones_kept(tmp_path: Path) -> None:
    update_dir = tmp_path / "update"
    for tag in ("v1.9.0", f"v{__version__}", "v99.0.0", "junk"):
        (update_dir / tag).mkdir(parents=True)
        (update_dir / tag / updater.INSTALLER_ASSET).write_bytes(SETUP)
    updater.remove_installed_downloads()
    assert [p.name for p in update_dir.iterdir()] == ["v99.0.0"], "a newer release may be about to install"


def test_the_download_folder_goes_once_it_is_empty(tmp_path: Path) -> None:
    (tmp_path / "update" / f"v{__version__}").mkdir(parents=True)
    updater.remove_installed_downloads()
    assert not (tmp_path / "update").exists()
    updater.remove_installed_downloads()  # nothing there: no error


def test_an_installer_still_running_is_removed_later(tmp_path: Path) -> None:
    folder = tmp_path / "update" / f"v{__version__}"
    folder.mkdir(parents=True)
    with (folder / "install.log").open("w"):  # the installer still writes its log (Windows: can't delete)
        updater.remove_installed_downloads()
    assert (folder / "install.log").exists()
    updater.remove_installed_downloads()
    assert not (tmp_path / "update").exists()


@pytest.mark.parametrize(
    ("tag", "message"),
    [(None, "GitHub could not be reached"), ("v-1.1", "no version number")],
    ids=["offline", "1.x-style-tag"],
)
def test_an_unknown_host_release_is_an_error(
    host_install: Launches, monkeypatch: pytest.MonkeyPatch, tag: str | None, message: str
) -> None:
    latest(monkeypatch, host=tag)
    with pytest.raises(UpdateError, match=message):
        updater.update_host()
    assert host_install.commands == []


# --- launching outside the job ------------------------------------------------


def test_launch_detached_survives_the_job_firefox_ends_with_the_host(tmp_path: Path) -> None:
    # a host in a job like Firefox's starts the process, then the job is ended: what happens when the host exits.
    # The started process sleeps first, so it would be killed with the job; it then writes the marker and exits.
    marker = tmp_path / "ran"
    installer = f"import os, time; time.sleep(2); open({str(marker)!r}, 'w').write(os.getcwd())"
    host = (
        "import ctypes, struct, sys\n"
        "from ytmp3_host import processes\n"
        "kernel32 = ctypes.windll.kernel32\n"
        "kernel32.CreateJobObjectW.restype = kernel32.GetCurrentProcess.restype = ctypes.c_void_p\n"
        "job = ctypes.c_void_p(kernel32.CreateJobObjectW(None, None))\n"
        "limits = ctypes.create_string_buffer(144)  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION; LimitFlags at 16\n"
        "struct.pack_into('<I', limits, 16, 0x2000 | 0x800)  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK\n"
        "assert kernel32.SetInformationJobObject(job, 9, limits, 144)\n"
        "assert kernel32.AssignProcessToJobObject(job, ctypes.c_void_p(kernel32.GetCurrentProcess()))\n"
        "processes.launch_detached([sys.executable, '-c', sys.argv[1]])\n"
        "kernel32.TerminateJobObject(job, 1)\n"
    )
    subprocess.run([sys.executable, "-c", host, installer], check=False, timeout=30)
    deadline = time.monotonic() + 20
    while not (marker.exists() and marker.read_text()) and time.monotonic() < deadline:
        time.sleep(0.1)
    # not the host's working directory (its version folder), which the installer removes
    assert Path(marker.read_text()) == Path(tempfile.gettempdir())


def test_launch_detached_falls_back_when_the_job_forbids_breaking_away(monkeypatch: pytest.MonkeyPatch) -> None:
    flags: list[int] = []

    def _popen(command: list[str], creationflags: int = 0, **kwargs: object) -> None:
        flags.append(creationflags)
        if creationflags & subprocess.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(processes.subprocess, "Popen", _popen)
    processes.launch_detached(["setup.exe"])
    assert len(flags) == 2
    assert flags[0] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert not flags[1] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert all(f & subprocess.DETACHED_PROCESS for f in flags)
