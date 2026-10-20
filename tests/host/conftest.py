"""Shared fixtures for the native host tests.

No test touches the real Documents folder, the network or the real yt-dlp.exe
(except the opt-in `network` tests). yt-dlp is replaced by fake_yt_dlp.py; ffmpeg
is either faked (fake_embed) or the real binary (ffmpeg, skipped when missing).
"""

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from ytmp3_host import config, core

FAKE_YT_DLP = Path(__file__).with_name("fake_yt_dlp.py")


@pytest.fixture(autouse=True)
def default_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the default download folder into the test's temp dir (not created)."""
    folder = tmp_path / "default"
    monkeypatch.setattr(core, "DEFAULT_FOLDER", folder)
    return folder


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    """Create a temp folder for the test to download into, and return it."""
    folder = tmp_path / "out"
    folder.mkdir()
    return folder


@dataclass
class FakeYtDlp:
    monkeypatch: pytest.MonkeyPatch

    def set(self, mode: str = "success", title: str | None = None) -> None: # todo: check how the monkeypatch works here
        self.monkeypatch.setenv("FAKE_YTDLP_MODE", mode)
        if title is not None:
            self.monkeypatch.setenv("FAKE_YTDLP_TITLE", title)


@pytest.fixture
def fake_yt_dlp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeYtDlp:
    """Run fake_yt_dlp.py with the real yt-dlp arguments instead of yt-dlp.exe.
    Also makes the binary existence checks in download_audio pass.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("yt-dlp.exe", "ffmpeg.exe"):
        (bin_dir / name).write_bytes(b"")
    monkeypatch.setattr(core, "YT_DLP_PATH", bin_dir / "yt-dlp.exe")
    monkeypatch.setattr(core, "FFMPEG_PATH", bin_dir / "ffmpeg.exe")

    real_build = core.build_download_command
    monkeypatch.setattr(
        core,
        "build_download_command",
        lambda url, temp_stem: [sys.executable, str(FAKE_YT_DLP), *real_build(url, temp_stem)[1:]],
    )
    for var in ("FAKE_YTDLP_MODE", "FAKE_YTDLP_TITLE", "FAKE_YTDLP_MP3", "FAKE_YTDLP_THUMB"):
        monkeypatch.delenv(var, raising=False)
    return FakeYtDlp(monkeypatch)


@dataclass
class EmbedCall:
    temp_mp3: Path
    thumb_file: Path | None
    artist: str
    output: Path


@pytest.fixture
def fake_embed(monkeypatch: pytest.MonkeyPatch) -> list[EmbedCall]:
    """Replace the ffmpeg pass with one that records its arguments and writes the output."""
    calls: list[EmbedCall] = []

    def _embed(temp_mp3: Path, thumb_file: Path | None, artist: str, output: Path) -> None:
        calls.append(EmbedCall(temp_mp3, thumb_file, artist, output))
        output.write_bytes(temp_mp3.read_bytes())

    monkeypatch.setattr(core, "embed_thumbnail", _embed)
    return calls


def find_ffmpeg() -> Path | None:
    if config.FFMPEG_PATH.exists():
        return config.FFMPEG_PATH
    found = shutil.which("ffmpeg")
    return Path(found) if found else None


@pytest.fixture(scope="session")
def ffmpeg() -> Path:
    """The real ffmpeg.exe (host/bin/ or PATH); tests using it are skipped without one."""
    path = find_ffmpeg()
    if path is None:
        pytest.skip("ffmpeg.exe not found in host/bin/ or on PATH")
    return path


@dataclass(frozen=True)
class Media:
    mp3: Path  # 1 s sine, LAME V0
    cover: Path  # 320x180 PNG, not square on purpose


@pytest.fixture(scope="session")
def media(ffmpeg: Path, tmp_path_factory: pytest.TempPathFactory) -> Media:
    folder = tmp_path_factory.mktemp("media")
    mp3, cover = folder / "sample.mp3", folder / "cover.png"
    base = [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i"]
    subprocess.run([*base, "sine=frequency=440:duration=1", "-c:a", "libmp3lame", "-q:a", "0", str(mp3)], check=True)
    subprocess.run([*base, "color=c=red:s=320x180", "-frames:v", "1", str(cover)], check=True)
    return Media(mp3, cover)
