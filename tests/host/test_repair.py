"""core.download_with_repair: a download that fails in yt-dlp is retried once after a repair.

The repairs themselves (updater.update_yt_dlp, updater.install_deno) are replaced by
recorders that change the fake install the way the real ones would; test_updater.py
tests them against a local release server.
"""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conftest import EmbedCall, FakeYtDlp
from ytmp3_host import core, health, updater
from ytmp3_host.updater import UpdateError

URL = "https://www.youtube.com/watch?v=abc123"
CHECKING = "The download failed, checking for a yt-dlp update..."
RETRYING = "Trying the download again..."


@dataclass
class Repairs:
    """What the fake repairs did, and how they behave (set the fields before the download)."""
    bin_dir: Path
    fake_yt_dlp: FakeYtDlp
    new_yt_dlp: bool = False  # update_yt_dlp installs a new exe that downloads fine
    update_error: bool = False  # update_yt_dlp raises UpdateError
    calls: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)

    def update_yt_dlp(self) -> str:
        self.calls.append("update_yt_dlp")
        if self.update_error:
            raise UpdateError("GitHub could not be reached")
        if self.new_yt_dlp:
            (self.bin_dir / "yt-dlp.exe").write_bytes(b"a newer yt-dlp")
            self.fake_yt_dlp.set("success")
        return "2026.10.21"

    def install_deno(self) -> str:
        self.calls.append("install_deno")
        (self.bin_dir / "deno.exe").write_bytes(b"deno")
        return "2.9.8"

    def download(self, out_dir: Path, is_cancelled: Callable[[], bool] = lambda: False) -> Path:
        return core.download_with_repair(URL, out_dir, on_info=self.infos.append, is_cancelled=is_cancelled)


@pytest.fixture
def repairs(fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Repairs:
    """Fake repairs on the fake_yt_dlp install, with no Deno on PATH."""
    state = Repairs(tmp_path / "bin", fake_yt_dlp)
    monkeypatch.setattr(updater, "update_yt_dlp", state.update_yt_dlp)
    monkeypatch.setattr(updater, "install_deno", state.install_deno)
    monkeypatch.setattr(health.shutil, "which", lambda name: None)
    return state


def test_a_download_that_works_needs_no_repair(repairs: Repairs, out_dir: Path) -> None:
    assert repairs.download(out_dir).exists()
    assert repairs.calls == [] and repairs.infos == []


def test_a_failure_a_new_yt_dlp_fixes_is_retried(repairs: Repairs, out_dir: Path) -> None:
    repairs.fake_yt_dlp.set("fail")
    repairs.new_yt_dlp = True
    assert repairs.download(out_dir) == out_dir / "Artist - Song.mp3"
    assert repairs.calls == ["update_yt_dlp"]
    assert repairs.infos == [CHECKING, RETRYING]


def test_without_a_new_yt_dlp_the_first_error_is_raised(repairs: Repairs, out_dir: Path) -> None:
    repairs.fake_yt_dlp.set("fail")
    with pytest.raises(subprocess.CalledProcessError) as error:
        repairs.download(out_dir)
    assert "Video unavailable" in error.value.output
    assert repairs.calls == ["update_yt_dlp"]  # no Deno: YouTube did not ask for JavaScript
    assert repairs.infos == [CHECKING]


def test_a_failed_update_raises_the_download_error(repairs: Repairs, out_dir: Path) -> None:
    repairs.fake_yt_dlp.set("fail")
    repairs.update_error = True
    with pytest.raises(subprocess.CalledProcessError):
        repairs.download(out_dir)


def test_a_missing_yt_dlp_is_downloaded_and_the_download_retried(repairs: Repairs, out_dir: Path) -> None:
    (repairs.bin_dir / "yt-dlp.exe").unlink()
    repairs.new_yt_dlp = True
    assert repairs.download(out_dir).exists()
    assert repairs.calls == ["update_yt_dlp"]


def test_deno_is_installed_when_youtube_wants_javascript(repairs: Repairs, out_dir: Path) -> None:
    repairs.fake_yt_dlp.set("needs_js")  # works once it gets --js-runtimes
    assert repairs.download(out_dir).exists()
    assert repairs.calls == ["update_yt_dlp", "install_deno"]
    assert repairs.infos[0] == CHECKING and "Deno" in repairs.infos[1] and repairs.infos[2] == RETRYING


def test_no_deno_download_when_a_runtime_is_there(repairs: Repairs, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health.shutil, "which", lambda name: r"C:\deno\deno.exe")
    repairs.fake_yt_dlp.set("needs_js")
    with pytest.raises(subprocess.CalledProcessError):
        repairs.download(out_dir)
    assert repairs.calls == ["update_yt_dlp"]


def test_an_installed_deno_is_passed_to_yt_dlp(repairs: Repairs, tmp_path: Path) -> None:
    command = core.build_download_command(URL, tmp_path / "stem")
    assert "--js-runtimes" not in command
    (repairs.bin_dir / "deno.exe").write_bytes(b"deno")
    command = core.build_download_command(URL, tmp_path / "stem")
    assert command[command.index("--js-runtimes") + 1] == f"deno:{repairs.bin_dir / 'deno.exe'}"


def test_a_canceled_download_is_not_repaired(repairs: Repairs, out_dir: Path) -> None:
    repairs.fake_yt_dlp.set("fail")
    repairs.new_yt_dlp = True
    with pytest.raises(subprocess.CalledProcessError):
        repairs.download(out_dir, is_cancelled=lambda: True)
    assert repairs.calls == []


def test_an_ffmpeg_failure_is_not_a_yt_dlp_repair(repairs: Repairs, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken_embed(*args: object) -> None:
        raise subprocess.CalledProcessError(1, [str(core.FFMPEG_PATH), "-i"], stderr="broken")

    monkeypatch.setattr(core, "embed_thumbnail", _broken_embed)
    with pytest.raises(subprocess.CalledProcessError):
        repairs.download(out_dir)
    assert repairs.calls == []


def test_progress_lines_do_not_crowd_warnings_out_of_the_error(repairs: Repairs, out_dir: Path) -> None:
    repairs.fake_yt_dlp.set("fail")
    with pytest.raises(subprocess.CalledProcessError) as error:
        core.download_audio(URL, out_dir)
    assert "some harmless warning" in error.value.output
    assert "[download]" not in error.value.output
