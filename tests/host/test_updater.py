"""updater: yt-dlp updates and repairs, and the host's self-update, against a local release server.

The server answers like GitHub's release downloads. The "yt-dlp.exe" files are
small Python scripts run as `python <file> --version`, so whether a download
"runs" depends on its content, like the real exe. Nothing here touches the network
or the real install; the lock file and download folder are under tmp_path
(update_paths in conftest).
"""

import hashlib
import http.server
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ytmp3_host import __version__, github, updater
from ytmp3_host.github import HOST_REPO, YT_DLP_REPO
from ytmp3_host.updater import UpdateError

OLD, NEW = "2026.08.19", "2026.09.01"


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
    exe: Path

    @property
    def files(self) -> list[str]:
        return sorted(p.name for p in self.exe.parent.iterdir() if p.name != "update.lock")


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Install:
    """An installed yt-dlp OLD; yt-dlp is run as `python <exe> --version`; latest releases are unknown."""
    app = tmp_path / "app"
    app.mkdir()
    exe = app / "yt-dlp.exe"
    exe.write_bytes(script(OLD))
    monkeypatch.setattr(updater, "YT_DLP_PATH", exe)
    monkeypatch.setattr(
        updater, "yt_dlp_version_command", lambda path=None: [sys.executable, str(path or exe), "--version"]
    )
    return Install(exe)


def latest(monkeypatch: pytest.MonkeyPatch, **tags: str | None) -> None:
    """Set the latest release per repo: latest(monkeypatch, yt_dlp="2026.09.01", host="v2.1.0")."""
    repos = {"yt_dlp": YT_DLP_REPO, "host": HOST_REPO}
    by_repo = {repos[key]: tag for key, tag in tags.items()}
    monkeypatch.setattr(updater, "latest_release_tag", lambda repo: by_repo.get(repo))


def publish_yt_dlp(server: ReleaseServer, tag: str = NEW, exe: bytes | None = None, checksum_of: bytes | None = None) -> bytes:
    """Publish yt-dlp release tag with exe (default: a working NEW) and a checksum for checksum_of (default: exe)."""
    exe = script(NEW) if exe is None else exe
    server.release(YT_DLP_REPO, tag, **{"yt-dlp.exe": exe, "SHA2-256SUMS": sums(**{"yt-dlp.exe": checksum_of or exe})})
    return exe


# --- checksums ----------------------------------------------------------------


def test_parse_checksums_reads_sha256sum_lines_and_skips_junk() -> None:
    a, b = "a" * 64, "B" * 64
    text = f"{a}  yt-dlp.exe\n{b} *Setup.exe\n\nnot a line\n{'c' * 63}  short\n"
    assert updater.parse_checksums(text) == {"yt-dlp.exe": a, "Setup.exe": "b" * 64}


def test_asset_url_quotes_the_tag_and_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "GITHUB_URL", "https://github.com")
    assert updater.asset_url("o/r", "v2.0.0", "a b.exe") == "https://github.com/o/r/releases/download/v2.0.0/a%20b.exe"


# --- yt-dlp updates -----------------------------------------------------------


def test_an_outdated_yt_dlp_is_replaced_by_the_checked_download(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    new_exe = publish_yt_dlp(server)
    assert updater.update_yt_dlp() == NEW
    assert install.exe.read_bytes() == new_exe
    assert install.files == ["yt-dlp.exe"]  # the replaced exe is deleted right away
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
    assert install.files == ["yt-dlp.exe", "yt-dlp.old.exe"]
    assert updater.read_version(updater.yt_dlp_version_command(install.exe.with_name("yt-dlp.old.exe"))) == OLD
    latest(monkeypatch, yt_dlp="2026.10.01")
    publish_yt_dlp(server, "2026.10.01", script("2026.10.01"))
    assert updater.update_yt_dlp() == "2026.10.01"
    assert install.files == ["yt-dlp.exe"]


def test_a_current_yt_dlp_is_not_downloaded_again(install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch) -> None:
    latest(monkeypatch, yt_dlp=OLD)
    assert updater.update_yt_dlp() == OLD
    assert server.requests == []


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
    assert install.files == ["yt-dlp.exe"]


def test_a_download_that_does_not_run_is_never_installed(
    install: Install, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest(monkeypatch, yt_dlp=NEW)
    publish_yt_dlp(server, exe=script(None))  # checksum matches, but it fails to run
    with pytest.raises(UpdateError, match="does not run"):
        updater.update_yt_dlp()
    assert install.exe.read_bytes() == script(OLD)
    assert install.files == ["yt-dlp.exe"]


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
    assert install.files == ["yt-dlp.exe"]


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
    assert install.files == ["yt-dlp.exe"]



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
    assert install.files == ["yt-dlp.exe", "yt-dlp.old.exe"]
    assert old.read_bytes() == script("2026.01.01")


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
        with pytest.raises(UpdateError):
            with updater.update_lock(lock, timeout=0.2):
                pass
    finally:
        holder.kill()
        holder.wait()
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


def test_after_the_grace_period_the_checked_installer_is_reused(
    host_install: Launches, server: ReleaseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest(monkeypatch, host="v99.0.0")
    publish_host(server)
    monkeypatch.setattr(updater, "INSTALL_GRACE_S", 0)
    updater.update_host()
    updater.update_host()
    assert len(host_install.commands) == 2
    assert server.gets(updater.INSTALLER_ASSET) == 1, "the verified copy is started again, not downloaded"


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


@pytest.mark.parametrize("tag", [None, "v-1.1"], ids=["offline", "1.x-style-tag"])
def test_an_unknown_host_release_is_an_error(host_install: Launches, monkeypatch: pytest.MonkeyPatch, tag: str | None) -> None:
    latest(monkeypatch, host=tag)
    with pytest.raises(UpdateError):
        updater.update_host()
    assert host_install.commands == []


# --- launching outside the job ------------------------------------------------


def test_launch_detached_starts_a_process_that_outlives_the_call(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    updater.launch_detached([sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"])
    deadline = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert marker.exists()


def test_launch_detached_falls_back_when_the_job_forbids_breaking_away(monkeypatch: pytest.MonkeyPatch) -> None:
    flags: list[int] = []

    def _popen(command: list[str], creationflags: int = 0, **kwargs: object) -> None:
        flags.append(creationflags)
        if creationflags & subprocess.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(updater.subprocess, "Popen", _popen)
    updater.launch_detached(["setup.exe"])
    assert len(flags) == 2
    assert flags[0] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert not flags[1] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert all(f & subprocess.DETACHED_PROCESS for f in flags)
