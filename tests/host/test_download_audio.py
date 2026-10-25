"""core.download_audio with the fake yt-dlp: naming, warnings, errors, timeout and cleanup."""

import os
import subprocess
import time
from pathlib import Path

import pytest

from conftest import EmbedCall, FakeYtDlp
from ytmp3_host import core
from ytmp3_host.core import download_audio

URL = "https://www.youtube.com/watch?v=abc123"


def leftovers(folder: Path) -> list[str]:
    """Temp files of any download in folder (there must be none after download_audio)."""
    return sorted(p.name for p in folder.glob("_ytmp3_*"))


def test_names_the_file_after_the_cleaned_title(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    fake_yt_dlp.set(title="Jay-Z - Empire State of Mind (Official Music Video)")
    warnings: list[str] = []

    result = download_audio(URL, out_dir, on_warning=warnings.append)

    assert result == out_dir / "Jay-Z - Empire State of Mind.mp3"
    assert result.read_bytes() == b"fake mp3"
    assert warnings == []
    [call] = fake_embed
    assert call.artist == "Jay-Z"
    assert call.title == "Empire State of Mind"
    assert call.thumb_file is not None and call.thumb_file.suffix == ".webp"
    assert call.output == result
    assert leftovers(out_dir) == []


def test_youtube_music_info_becomes_the_tags(fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path) -> None:
    fake_yt_dlp.set(title="Never Gonna Give You Up", info={
        "artists": ["Rick Astley"], "album": "Whenever You Need Somebody", "release_year": 1987,
        "channel": "Rick Astley - Topic", "webpage_url": "https://www.youtube.com/watch?v=abc123",
    })

    assert download_audio(URL, out_dir) == out_dir / "Rick Astley - Never Gonna Give You Up.mp3"
    [call] = fake_embed
    assert (call.artist, call.title) == ("Rick Astley", "Never Gonna Give You Up")
    assert call.tags == {
        "album": "Whenever You Need Somebody", "date": "1987", "comment": "https://www.youtube.com/watch?v=abc123"
    }


def test_title_without_artist_takes_the_channel(fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path) -> None:
    fake_yt_dlp.set(title="Me at the zoo")  # the fake's default info has only a channel
    download_audio(URL, out_dir)
    assert fake_embed[0].artist == "Some Channel"
    assert fake_embed[0].tags == {}


def test_existing_artist_title_file_stops_before_downloading(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    # the early check uses the same name as the final file, artist included
    fake_yt_dlp.set(title="Flickermood", info={"artists": ["Forss"], "uploader": "Forss"})
    (out_dir / "Forss - Flickermood.mp3").write_bytes(b"user's file")
    progress: list[float] = []
    with pytest.raises(FileExistsError):
        download_audio("https://soundcloud.com/forss/flickermood", out_dir, on_progress=progress.append)
    assert progress == [] and fake_embed == []


def test_title_with_non_ascii_characters(fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path) -> None:
    fake_yt_dlp.set(title="Artíst – Søng 日本語 (Lyrics)")
    assert download_audio(URL, out_dir).name == "Artíst – Søng 日本語.mp3"


def test_requested_filename_wins_over_the_title(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    fake_yt_dlp.set(title="Some Title")
    assert download_audio(URL, out_dir, "My Name - Mine") == out_dir / "My Name - Mine.mp3"
    assert fake_embed[0].artist == "My Name"
    assert fake_embed[0].title == "Mine"


def test_missing_title_warns_and_uses_the_default_name(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    fake_yt_dlp.set("no_title")
    warnings: list[str] = []
    assert download_audio(URL, out_dir, on_warning=warnings.append) == out_dir / "Youtube MP3.mp3"
    assert len(warnings) == 1 and "title" in warnings[0]


def test_missing_thumbnail_warns_and_still_saves(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    fake_yt_dlp.set("no_thumb")
    warnings: list[str] = []
    assert download_audio(URL, out_dir, on_warning=warnings.append).exists()
    assert fake_embed[0].thumb_file is None
    assert len(warnings) == 1 and "thumbnail" in warnings[0]


def test_reports_progress_as_fractions(fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path) -> None:
    progress: list[float] = []
    download_audio(URL, out_dir, on_progress=progress.append)
    assert progress == [0.0, 0.425, 1.0]


def test_existing_requested_file_fails_before_downloading(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    existing = out_dir / "Taken.mp3"
    existing.write_bytes(b"user's file")
    started: list[object] = []

    with pytest.raises(FileExistsError) as info:
        download_audio(URL, out_dir, "Taken", on_start=lambda p, s: started.append(p))

    assert info.value.filename == str(existing)
    assert started == []  # yt-dlp never ran
    assert existing.read_bytes() == b"user's file"


def test_existing_title_file_stops_before_downloading(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    fake_yt_dlp.set(title="Artist - Song (Official Video)")
    existing = out_dir / "Artist - Song.mp3"
    existing.write_bytes(b"user's file")
    progress: list[float] = []

    with pytest.raises(FileExistsError) as info:
        download_audio(URL, out_dir, on_progress=progress.append)

    assert info.value.filename == str(existing)
    assert progress == []  # the title comes before any progress line, so the download never ran
    assert existing.read_bytes() == b"user's file"
    assert fake_embed == []
    assert leftovers(out_dir) == []


def test_removes_stale_temp_files_but_keeps_recent_ones(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    stale, recent = out_dir / "_ytmp3_old.webm.part", out_dir / "_ytmp3_new.webm.part"
    stale.write_bytes(b"killed host")
    recent.write_bytes(b"running download")
    old = time.time() - core.STALE_TEMP_AGE_S - 60
    os.utime(stale, (old, old))

    download_audio(URL, out_dir, "Song")

    assert leftovers(out_dir) == [recent.name]


def test_yt_dlp_failure_raises_with_its_output_and_cleans_up(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    fake_yt_dlp.set("fail")

    with pytest.raises(subprocess.CalledProcessError) as info:
        download_audio(URL, out_dir)

    assert info.value.returncode == 1
    assert core.describe_error(info.value).endswith("failed: [youtube] abc123: Video unavailable")
    assert leftovers(out_dir) == []  # the .webm.part is gone too
    assert fake_embed == []


def test_yt_dlp_without_mp3_raises(fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path) -> None:
    fake_yt_dlp.set("no_mp3")
    with pytest.raises(FileNotFoundError, match="did not create the MP3"):
        download_audio(URL, out_dir)
    assert leftovers(out_dir) == []
    assert list(out_dir.glob("*.mp3")) == []


def test_ffmpeg_failure_cleans_up(fake_yt_dlp: FakeYtDlp, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _failing_embed(*_args: object) -> None:
        raise subprocess.CalledProcessError(1, ["ffmpeg.exe"], None, "boom")

    monkeypatch.setattr(core, "embed_thumbnail", _failing_embed)
    with pytest.raises(subprocess.CalledProcessError):
        download_audio(URL, out_dir)
    assert leftovers(out_dir) == []


def test_ffmpeg_without_output_is_not_reported_as_success(
    fake_yt_dlp: FakeYtDlp, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "embed_thumbnail", lambda *_args: None)
    with pytest.raises(FileNotFoundError, match="ffmpeg did not create the MP3"):
        download_audio(URL, out_dir)
    assert leftovers(out_dir) == []


@pytest.mark.parametrize("missing", ["FFMPEG_PATH", "YT_DLP_PATH"])
def test_missing_binary_raises_before_downloading(
    fake_yt_dlp: FakeYtDlp, out_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setattr(core, missing, tmp_path / "gone.exe")
    started: list[object] = []
    with pytest.raises(FileNotFoundError, match="please reinstall"):
        download_audio(URL, out_dir, on_start=lambda p, s: started.append(p))
    assert started == []


def test_timeout_kills_the_whole_process_tree(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    # the fake leaves a grandchild holding stdout open for 30 s: if only the direct
    # child were killed, reading stdout would block until the grandchild exits
    fake_yt_dlp.set("hang")
    start = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired) as info:
        download_audio(URL, out_dir, timeout=1)

    assert time.monotonic() - start < 15
    assert info.value.timeout == 1
    assert leftovers(out_dir) == []


def test_cancel_through_on_start_stops_the_download(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    # what the GUI's cancel button does, here as soon as the download is running
    # (killing right at on_start can beat the launcher to spawning its child)
    fake_yt_dlp.set("hang")
    processes: list[subprocess.Popen[str]] = []

    def _cancel(_fraction: float) -> None:
        core.kill_process_tree(processes[0])

    start = time.monotonic()
    with pytest.raises(subprocess.CalledProcessError):
        download_audio(URL, out_dir, on_progress=_cancel, on_start=lambda p, _s: processes.append(p))

    assert time.monotonic() - start < 15
    assert leftovers(out_dir) == []


def test_raising_progress_callback_does_not_leave_yt_dlp_running(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    fake_yt_dlp.set("hang")
    processes: list[subprocess.Popen[str]] = []

    def _broken_progress(_fraction: float) -> None:
        raise RuntimeError("UI is gone")

    start = time.monotonic()
    with pytest.raises(RuntimeError, match="UI is gone"):
        download_audio(URL, out_dir, on_progress=_broken_progress, on_start=lambda p, _s: processes.append(p))

    assert time.monotonic() - start < 15
    assert processes[0].poll() is not None
    assert leftovers(out_dir) == []


def test_two_downloads_use_different_temp_files(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    stems: list[Path] = []
    download_audio(URL, out_dir, "One", on_start=lambda _p, stem: stems.append(stem))
    download_audio(URL, out_dir, "Two", on_start=lambda _p, stem: stems.append(stem))
    assert stems[0] != stems[1]
    assert sorted(p.name for p in out_dir.iterdir()) == ["One.mp3", "Two.mp3"]
