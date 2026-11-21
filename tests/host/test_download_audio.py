"""core.download_audio with the fake yt-dlp: naming, warnings, errors, timeout and cleanup."""

import functools
import os
import subprocess
import time
from pathlib import Path

import pytest

from conftest import EmbedCall, FakeYtDlp
from ytmp3_host import config, core, files, ytdlp
from ytmp3_host.media_format import Mp3Format, Mp4Format

URL = "https://www.youtube.com/watch?v=abc123"

# these tests are about the MP3 download; an MP4 test passes its own media_format
download_audio = functools.partial(core.download_audio, media_format=Mp3Format())


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


def test_tags_keep_what_a_filename_can_not_hold(fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path) -> None:
    fake_yt_dlp.set(title="AC/DC - Thunder: Live? (Official Video)")
    assert download_audio(URL, out_dir).name == "ACDC - Thunder Live.mp3"
    assert (fake_embed[0].artist, fake_embed[0].title) == ("AC/DC", "Thunder: Live?")


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
    old = time.time() - files.STALE_TEMP_AGE_S - 60
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


def test_mp4_download_is_named_and_embedded_as_mp4(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    fake_yt_dlp.set(title="Jay-Z - Empire State of Mind (Official Music Video)")

    result = core.download_audio(URL, out_dir, media_format=Mp4Format(quality="720"))

    assert result == out_dir / "Jay-Z - Empire State of Mind.mp4"
    assert result.read_bytes() == b"fake mp4"  # the fake only writes .mp4 when the command merges to MP4
    [call] = fake_embed
    assert call.temp_mp3.suffix == ".mp4" and call.temp_mp3.name.startswith("_ytmp3_")
    assert call.output == result
    assert leftovers(out_dir) == []


def test_an_existing_mp4_stops_an_mp4_download(fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path) -> None:
    existing = out_dir / "Taken.mp4"
    existing.write_bytes(b"user's file")

    with pytest.raises(FileExistsError) as info:
        core.download_audio(URL, out_dir, "Taken", media_format=Mp4Format())

    assert info.value.filename == str(existing)
    assert existing.read_bytes() == b"user's file"
    assert fake_embed == []


def test_an_mp3_with_the_same_name_does_not_stop_an_mp4_download(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    fake_yt_dlp.set(title="Artist - Song")
    (out_dir / "Artist - Song.mp3").write_bytes(b"user's mp3")

    result = core.download_audio(URL, out_dir, media_format=Mp4Format())

    assert result == out_dir / "Artist - Song.mp4"
    assert (out_dir / "Artist - Song.mp3").read_bytes() == b"user's mp3"


def test_yt_dlp_without_mp4_raises(fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path) -> None:
    fake_yt_dlp.set("no_mp3")  # writes no media file in either format
    with pytest.raises(FileNotFoundError, match="did not create the MP4"):
        core.download_audio(URL, out_dir, media_format=Mp4Format())
    assert leftovers(out_dir) == []
    assert list(out_dir.iterdir()) == []


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


@pytest.mark.parametrize(("module", "missing"), [(core, "FFMPEG_PATH"), (config, "YT_DLP_PATH")])
def test_missing_binary_raises_before_downloading(
    fake_yt_dlp: FakeYtDlp,
    out_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    missing: str,
) -> None:
    monkeypatch.setattr(module, missing, tmp_path / "gone.exe")
    started: list[object] = []
    with pytest.raises(FileNotFoundError, match="please reinstall"):
        download_audio(URL, out_dir, on_start=lambda p, s: started.append(p))
    assert started == []


def process_running(pid: int) -> bool:
    tasks = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=True)
    return str(pid) in tasks.stdout


def test_timeout_kills_the_whole_process_tree(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the fake leaves a grandchild holding stdout open for 30 s: if only the direct
    # child were killed, reading stdout would block until the grandchild exits
    fake_yt_dlp.set("hang")
    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_YTDLP_CHILD_PID_FILE", str(pid_file))
    start = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired) as info:
        download_audio(URL, out_dir, timeout=3)

    assert time.monotonic() - start < 15
    assert info.value.timeout == 3
    assert leftovers(out_dir) == []
    deadline = time.monotonic() + 5
    while process_running(child := int(pid_file.read_text())) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not process_running(child), "the process behind yt-dlp was left running"


def test_a_known_size_extends_the_timeout(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the download takes longer than the timeout passed in, but its size asks for more
    fake_yt_dlp.set(info={"filesize_approx": 50_000_000})
    monkeypatch.setenv("FAKE_YTDLP_DELAY_S", "3")
    assert download_audio(URL, out_dir, timeout=2) == out_dir / "Artist - Song.mp3"


def test_the_extended_timeout_still_stops_a_hanging_download(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 4 MB at 1 MB/s without a base: 4 s, counted from the start, not from the info
    monkeypatch.setattr(ytdlp, "DOWNLOAD_TIMEOUT_BASE_S", 0)
    monkeypatch.setattr(ytdlp, "MIN_SPEED_BPS", 1_000_000)
    fake_yt_dlp.set("hang", info={"filesize_approx": 4_000_000})
    start = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired) as info:
        download_audio(URL, out_dir, timeout=2)

    assert info.value.timeout == 4  # the timeout it ran with, for describe_error
    assert 4 <= time.monotonic() - start < 15
    assert leftovers(out_dir) == []


def test_cancel_through_on_start_stops_the_download(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    # what the GUI's cancel button does, here as soon as the download is running
    # (killing right at on_start can beat the launcher to spawning its child)
    fake_yt_dlp.set("hang")
    started: list[subprocess.Popen[str]] = []

    def _cancel(_fraction: float) -> None:
        ytdlp.kill_process_tree(started[0])

    start = time.monotonic()
    with pytest.raises(subprocess.CalledProcessError):
        download_audio(URL, out_dir, on_progress=_cancel, on_start=lambda p, _s: started.append(p))

    assert time.monotonic() - start < 15
    assert leftovers(out_dir) == []


def test_raising_progress_callback_does_not_leave_yt_dlp_running(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    fake_yt_dlp.set("hang")
    prcs: list[subprocess.Popen[str]] = []

    def _broken_progress(_fraction: float) -> None:
        raise RuntimeError("UI is gone")

    start = time.monotonic()
    with pytest.raises(RuntimeError, match="UI is gone"):
        download_audio(URL, out_dir, on_progress=_broken_progress, on_start=lambda p, _s: prcs.append(p))

    assert time.monotonic() - start < 15
    assert prcs[0].poll() is not None
    assert leftovers(out_dir) == []


def test_two_downloads_use_different_temp_files(
    fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    stems: list[Path] = []
    download_audio(URL, out_dir, "One", on_start=lambda _p, stem: stems.append(stem))
    download_audio(URL, out_dir, "Two", on_start=lambda _p, stem: stems.append(stem))
    assert stems[0] != stems[1]
    assert sorted(p.name for p in out_dir.iterdir()) == ["One.mp3", "Two.mp3"]
