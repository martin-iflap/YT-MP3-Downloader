"""Shared fixtures for the native host tests.

No test touches the real Documents folder, the network or the real yt-dlp.exe
(except the opt-in `network` tests). yt-dlp is replaced by fake_yt_dlp.py; ffmpeg
is either faked (fake_embed) or the real binary (ffmpeg, skipped when missing).

Every file a test writes goes under pytest's `tmp_path`: a new, empty folder per
test (%TEMP%/pytest-of-<user>/pytest-<N>/<test name>0). pytest deletes it when the
test passes (`tmp_path_retention_policy = "failed"` in pyproject.toml) and keeps it
when it fails, so the files can be inspected. Everything changed through
`monkeypatch` (module attributes, environment variables) is restored after each test.
"""

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from ytmp3_host import config, core, files, health, updater, ytdlp

FAKE_YT_DLP = Path(__file__).with_name("fake_yt_dlp.py")


@pytest.fixture(autouse=True)
def default_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point files.DEFAULT_FOLDER at <tmp_path>/default in every test (autouse).

    Keeps the code under test away from the real ~/Documents/YT-MP3-Downloads.
    The folder is not created, so tests can check that resolve_folder creates it.
    Returns its path.
    """
    folder = tmp_path / "default"
    monkeypatch.setattr(files, "DEFAULT_FOLDER", folder)
    return folder


@pytest.fixture(autouse=True)
def no_log_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep hosts started as child processes from writing mp3.log into the repo (autouse)."""
    monkeypatch.setenv(config.LOG_LEVEL_ENV, "CRITICAL")


@pytest.fixture(autouse=True)
def no_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep get_version and the updater from reaching GitHub in every test (autouse).

    Every latest release is unknown (None, as when offline) unless a test patches
    latest_release_tag itself, and the updater's downloads fail. test_github.py and
    test_updater.py test the real network code against a local server.
    """
    monkeypatch.setattr(health, "latest_release_tag", lambda repo: None)
    monkeypatch.setattr(updater, "latest_release_tag", lambda repo: None)
    monkeypatch.setattr(updater, "_opener", None)  # any download raises instead of going online


@pytest.fixture(autouse=True)
def update_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the updater's lock file and installer download folder into tmp_path (autouse)."""
    monkeypatch.setattr(updater, "UPDATE_LOCK_PATH", tmp_path / "update.lock")
    monkeypatch.setattr(updater, "UPDATE_DIR", tmp_path / "update")


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    """Create <tmp_path>/out, an existing folder for the test to download into, and return it."""
    folder = tmp_path / "out"
    folder.mkdir()
    return folder


def point_yt_dlp_at(monkeypatch: pytest.MonkeyPatch, exe: Path) -> None:
    """Make exe the single-file yt-dlp.exe (config.YT_DLP_PATH) and put the onedir builds (yt-dlp\\<tag>\\) and
    the onedir-failed marker next to it. config.yt_dlp_path() and the updater read all three from config when
    used; without the folder a dev's host/bin/yt-dlp\\<tag>\\ would be found."""
    monkeypatch.setattr(config, "YT_DLP_PATH", exe)
    monkeypatch.setattr(config, "YT_DLP_DIR", exe.with_name("yt-dlp"))
    monkeypatch.setattr(config, "ONEDIR_FAILED_PATH", exe.with_name("yt-dlp") / "onedir-failed")


@dataclass
class FakeYtDlp:
    """Handle returned by the fake_yt_dlp fixture to choose how the fake behaves.

    The fake runs as a separate process, so it can't be configured with Python
    arguments or monkeypatch. It is configured through FAKE_YTDLP_* environment
    variables instead, which the child process inherits from the test.
    """
    monkeypatch: pytest.MonkeyPatch

    def set(
        self,
        mode: str = "success",
        title: str | None = None,
        version: str | None = None,
        info: dict | None = None,
        streams: list[int] | None = None,
    ) -> None:
        """Set the mode (see fake_yt_dlp.py) and the title, version, other video info and stream
        sizes the fake prints; None keeps the defaults."""
        self.monkeypatch.setenv("FAKE_YTDLP_MODE", mode)
        if streams is not None:
            self.monkeypatch.setenv("FAKE_YTDLP_STREAMS", json.dumps(streams))
        if title is not None:
            self.monkeypatch.setenv("FAKE_YTDLP_TITLE", title)
        if version is not None:
            self.monkeypatch.setenv("FAKE_YTDLP_VERSION", version)
        if info is not None:
            self.monkeypatch.setenv("FAKE_YTDLP_INFO", json.dumps(info))


@pytest.fixture
def fake_yt_dlp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeYtDlp:
    """Make download_media run fake_yt_dlp.py instead of yt-dlp.exe.

    - Creates empty yt-dlp.exe and ffmpeg.exe in <tmp_path>/bin and points config.yt_dlp_path()
      (point_yt_dlp_at) and core and ytdlp at them, so the "binary exists" checks in download_media
      pass. They never run.
    - Wraps ytdlp.build_download_command and health.yt_dlp_version_command: the real
      command is built, then only its executable is swapped for `python
      fake_yt_dlp.py`. The fake reads the real -o and --print arguments, so a broken
      output template or title marker fails the tests.
    - Does the same for health (the get_version checks) and points the updater at
      the same yt-dlp.exe. deno.exe goes to the same folder too, but is not created.
    - Clears FAKE_YTDLP_* variables left in the shell, so the fake starts in its
      default mode (success, title "Artist - Song (Official Video)").
    Returns a FakeYtDlp to change the mode or title.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("yt-dlp.exe", "ffmpeg.exe"):
        (bin_dir / name).write_bytes(b"")
    point_yt_dlp_at(monkeypatch, bin_dir / "yt-dlp.exe")
    for module in (core, ytdlp, health):
        monkeypatch.setattr(module, "FFMPEG_PATH", bin_dir / "ffmpeg.exe")
    for module in (core, ytdlp, health, updater):
        monkeypatch.setattr(module, "DENO_PATH", bin_dir / "deno.exe")

    real_build = ytdlp.build_download_command
    monkeypatch.setattr(
        ytdlp,
        "build_download_command",
        lambda url, temp_stem, media_format: [
            sys.executable, str(FAKE_YT_DLP), *real_build(url, temp_stem, media_format)[1:]
        ],
    )
    real_version = health.yt_dlp_version_command
    monkeypatch.setattr(
        health,
        "yt_dlp_version_command",
        lambda exe=None: [sys.executable, str(FAKE_YT_DLP), *real_version(exe)[1:]],
    )
    for var in ("FAKE_YTDLP_MODE", "FAKE_YTDLP_TITLE", "FAKE_YTDLP_VERSION", "FAKE_YTDLP_INFO", "FAKE_YTDLP_MP3",
                "FAKE_YTDLP_MP4", "FAKE_YTDLP_THUMB", "FAKE_YTDLP_CHILD_PID_FILE"):
        monkeypatch.delenv(var, raising=False)
    return FakeYtDlp(monkeypatch)


@dataclass
class EmbedCall:
    """The arguments of one call to the fake embed_thumbnail (see the fake_embed fixture)."""
    temp_mp3: Path
    thumb_file: Path | None
    artist: str
    title: str
    output: Path
    tags: dict[str, str] | None


@pytest.fixture
def fake_embed(monkeypatch: pytest.MonkeyPatch) -> list[EmbedCall]:
    """Replace core.embed_thumbnail (the ffmpeg pass) with a fake.

    The fake records its arguments as an EmbedCall and copies the temp mp3's bytes
    to the output path, so download_media's "output was written" check passes and
    the test can compare the final file with what the fake yt-dlp wrote.
    Returns the list of calls; `[call] = fake_embed` also asserts there was exactly one.
    """
    calls: list[EmbedCall] = []

    def _embed(
        temp_mp3: Path,
        thumb_file: Path | None,
        artist: str,
        title: str,
        output: Path,
        tags: dict[str, str] | None = None,
    ) -> None:
        calls.append(EmbedCall(temp_mp3, thumb_file, artist, title, output, tags))
        output.write_bytes(temp_mp3.read_bytes())

    monkeypatch.setattr(core, "embed_thumbnail", _embed)
    return calls


def find_ffmpeg() -> Path | None:
    """The real ffmpeg.exe: host/bin/ (config.FFMPEG_PATH) first, then PATH; None if neither has one."""
    if config.FFMPEG_PATH.exists():
        return config.FFMPEG_PATH
    found = shutil.which("ffmpeg")
    return Path(found) if found else None


@pytest.fixture(scope="session")
def ffmpeg() -> Path:
    """Path of the real ffmpeg.exe (see find_ffmpeg); every test using it is skipped without one.

    Session scope: looked up once per run. It only returns the path; tests that
    need core to use it patch core.FFMPEG_PATH themselves.
    """
    path = find_ffmpeg()
    if path is None:
        pytest.skip("ffmpeg.exe not found in host/bin/ or on PATH")
    return path


@dataclass(frozen=True)
class Media:
    """Paths to the test media and cover image, created in a temp folder."""
    mp3: Path  # 1 s sine, LAME V0
    mp4: Path  # 1 s 320x240 test picture (MPEG-4) with a 1 s sine (AAC)
    cover: Path  # 320x180 PNG, not square on purpose


@pytest.fixture(scope="session")
def media(ffmpeg: Path, tmp_path_factory: pytest.TempPathFactory) -> Media:
    """Create a 1 s MP3, a 1 s MP4 and a 320x180 PNG cover with the real ffmpeg, and return their paths.

    The cover is not square on purpose: embed_thumbnail center-crops it for an MP3 and
    keeps its shape for an MP4. The MP4 uses ffmpeg's built-in encoders (mpeg4, aac),
    which every ffmpeg build has, unlike libx264.
    Session scope: generated once per run in a shared temp folder (tmp_path_factory)
    and reused by every test, so tests must only read these files, never change them.
    pytest deletes the folder at the end of a fully passing run.
    """
    folder = tmp_path_factory.mktemp("media")
    mp3, mp4, cover = folder / "sample.mp3", folder / "sample.mp4", folder / "cover.png"
    lavfi = [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i"]
    sine = "sine=frequency=440:duration=1"
    subprocess.run([*lavfi, sine, "-c:a", "libmp3lame", "-q:a", "0", str(mp3)], check=True)
    subprocess.run(
        [*lavfi, "testsrc=size=320x240:rate=15:duration=1", "-f", "lavfi", "-i", sine,
         "-c:v", "mpeg4", "-c:a", "aac", str(mp4)],
        check=True,
    )
    subprocess.run([*lavfi, "color=c=red:s=320x180", "-frames:v", "1", str(cover)], check=True)
    return Media(mp3, mp4, cover)
