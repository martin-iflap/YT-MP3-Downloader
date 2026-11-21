"""core.embed_thumbnail and the whole pipeline with the real ffmpeg.exe (skipped without one)."""

import re
import subprocess
from pathlib import Path

import pytest

from conftest import FakeYtDlp, Media
from ytmp3_host import core
from ytmp3_host.core import download_audio, embed_thumbnail
from ytmp3_host.media_format import Mp3Format, Mp4Format
from ytmp3_host.naming import filename_limit


@pytest.fixture(autouse=True)
def real_ffmpeg(ffmpeg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make core use the real ffmpeg.exe in every test of this file (skips them all without one)."""
    monkeypatch.setattr(core, "FFMPEG_PATH", ffmpeg)


def probe(ffmpeg: Path, file: Path) -> str:
    """ffmpeg's description of file (streams and metadata); without an output file ffmpeg exits with 1."""
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(file)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.stderr


def stream_md5(ffmpeg: Path, file: Path, stream: str = "a") -> str:
    """Hash of the audio packets (or of another stream, e.g. "v:0"), equal only if it was copied, not re-encoded."""
    cmd = [str(ffmpeg), "-nostdin", "-v", "error", "-i", str(file), "-map", f"0:{stream}", "-c", "copy", "-f", "md5", "-"]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def test_embeds_square_jpeg_cover_artist_and_title(ffmpeg: Path, media: Media, tmp_path: Path) -> None:
    output = tmp_path / "Artist - Song.mp3"

    embed_thumbnail(media.mp3, media.cover, "Artíst", "Søng", output)

    info = probe(ffmpeg, output)
    assert re.search(r"^\s*artist\s*: Artíst$", info, re.MULTILINE)
    assert re.search(r"^\s*title\s*: Søng$", info, re.MULTILINE)
    video = re.search(r"Video: mjpeg.*?(\d+)x(\d+)", info)
    assert video, info
    assert video.group(1) == video.group(2) == "180"  # 320x180 center-cropped to 1:1
    assert "Cover (front)" in info
    assert output.read_bytes()[:4] == b"ID3\x03"  # ID3v2.3
    assert stream_md5(ffmpeg, output) == stream_md5(ffmpeg, media.mp3)


def test_extra_tags_become_id3v23_frames(ffmpeg: Path, media: Media, tmp_path: Path) -> None:
    output = tmp_path / "out.mp3"
    tags = {"album": "Albüm", "date": "1987", "track": "3", "comment": "https://www.youtube.com/watch?v=abc"}
    embed_thumbnail(media.mp3, media.cover, "x", "y", output, tags)

    info = probe(ffmpeg, output)
    assert re.search(r"^\s*album\s*: Albüm$", info, re.MULTILINE)
    assert re.search(r"^\s*track\s*: 3$", info, re.MULTILINE)
    assert re.search(r"^\s*comment\s*: https://www.youtube.com/watch\?v=abc$", info, re.MULTILINE)
    header = output.read_bytes()[:2000]
    for frame in (b"TALB", b"TYER", b"TRCK", b"COMM"):  # the v2.3 frames players read (TYER, not v2.4's TDRC)
        assert frame in header
    assert stream_md5(ffmpeg, output) == stream_md5(ffmpeg, media.mp3)


def test_the_cover_is_a_high_quality_jpeg(media: Media, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    real_run = subprocess.run

    def _recording_run(command: list[str], **kwargs: object) -> object:
        commands.append(command)
        return real_run(command, **kwargs)

    monkeypatch.setattr(core.subprocess, "run", _recording_run)
    embed_thumbnail(media.mp3, media.cover, "x", "y", tmp_path / "out.mp3")
    [cmd] = commands
    assert cmd[cmd.index("-q:v") + 1] == "2"  # see the project priorities in CLAUDE.md


def test_the_longest_allowed_filename_can_be_written(media: Media, tmp_path: Path) -> None:
    output = tmp_path / f"{'x' * filename_limit(tmp_path)}.mp3"
    embed_thumbnail(media.mp3, None, "x", "y", output)
    assert output.is_file()  # past the limit ffmpeg still writes it, but Python can't see it


def test_without_thumbnail_only_sets_the_tags(ffmpeg: Path, media: Media, tmp_path: Path) -> None:
    output = tmp_path / "out.mp3"
    embed_thumbnail(media.mp3, None, "Someone", "Something", output)
    info = probe(ffmpeg, output)
    assert "Video:" not in info
    assert re.search(r"^\s*artist\s*: Someone$", info, re.MULTILINE)
    assert re.search(r"^\s*title\s*: Something$", info, re.MULTILINE)
    assert stream_md5(ffmpeg, output) == stream_md5(ffmpeg, media.mp3)


def test_broken_input_raises_and_leaves_no_partial_file(tmp_path: Path, media: Media) -> None:
    broken = tmp_path / "broken.mp3"
    broken.write_bytes(b"not an mp3 at all")
    output = tmp_path / "out.mp3"

    with pytest.raises(subprocess.CalledProcessError) as info:
        embed_thumbnail(broken, media.cover, "x", "y", output)

    assert not output.exists()
    assert core.describe_error(info.value).startswith(f"{core.FFMPEG_PATH.name} failed: ")


def test_never_overwrites_or_deletes_an_existing_file(tmp_path: Path, media: Media) -> None:
    output = tmp_path / "out.mp3"
    output.write_bytes(b"user's file")

    # ffmpeg 7 exits 0 when -n refuses to overwrite, so this must not depend on ffmpeg
    with pytest.raises(FileExistsError):
        embed_thumbnail(media.mp3, media.cover, "x", "y", output)

    assert output.read_bytes() == b"user's file"


def test_full_pipeline(fake_yt_dlp: FakeYtDlp, ffmpeg: Path, media: Media, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "FFMPEG_PATH", ffmpeg)  # fake_yt_dlp pointed it at a dummy
    monkeypatch.setenv("FAKE_YTDLP_MP3", str(media.mp3))
    monkeypatch.setenv("FAKE_YTDLP_THUMB", str(media.cover))
    fake_yt_dlp.set(title="Jay-Z - Empire State of Mind (Official Video)")

    result = download_audio("https://youtu.be/abc", out_dir, media_format=Mp3Format())

    assert [p.name for p in out_dir.iterdir()] == ["Jay-Z - Empire State of Mind.mp3"]
    info = probe(ffmpeg, result)
    assert re.search(r"^\s*artist\s*: Jay-Z$", info, re.MULTILINE)
    assert re.search(r"^\s*title\s*: Empire State of Mind$", info, re.MULTILINE)
    assert "Video: mjpeg" in info


def test_mp4_gets_the_uncropped_cover_as_attached_pic(ffmpeg: Path, media: Media, tmp_path: Path) -> None:
    output = tmp_path / "Artist - Video.mp4"

    embed_thumbnail(media.mp4, media.cover, "Artíst", "Vídéo", output)

    info = probe(ffmpeg, output)
    assert re.search(r"^\s*artist\s*: Artíst$", info, re.MULTILINE)
    assert re.search(r"^\s*title\s*: Vídéo$", info, re.MULTILINE)
    assert len(re.findall(r"Stream #0:\d+.*: Video:", info)) == 2, info  # the video and the cover
    cover = re.search(r"Video: mjpeg.*?(\d+)x(\d+).*\(attached pic\)$", info, re.MULTILINE)
    assert cover, info  # marked as the cover, so players don't show it as a second video
    assert cover.groups() == ("320", "180")  # not cropped: it has the video's shape
    assert "Cover (front)" not in info  # the ID3 stream tags are MP3 only
    # an MP4 starts with its ftyp box: 4 bytes of box size (depends on the brands), then "ftyp"
    assert output.read_bytes()[4:8] == b"ftyp"
    assert stream_md5(ffmpeg, output, "v:0") == stream_md5(ffmpeg, media.mp4, "v:0")  # the video is copied
    assert stream_md5(ffmpeg, output) == stream_md5(ffmpeg, media.mp4)


def test_extra_tags_become_mp4_atoms(ffmpeg: Path, media: Media, tmp_path: Path) -> None:
    output = tmp_path / "out.mp4"
    tags = {"album": "Albüm", "date": "1987", "track": "3", "comment": "https://www.youtube.com/watch?v=abc"}
    embed_thumbnail(media.mp4, media.cover, "x", "y", output, tags)

    info = probe(ffmpeg, output)
    assert re.search(r"^\s*album\s*: Albüm$", info, re.MULTILINE)
    assert re.search(r"^\s*date\s*: 1987$", info, re.MULTILINE)
    assert re.search(r"^\s*track\s*: 3$", info, re.MULTILINE)
    assert re.search(r"^\s*comment\s*: https://www.youtube.com/watch\?v=abc$", info, re.MULTILINE)
    data = output.read_bytes()
    for atom in (b"\xa9alb", b"\xa9day", b"trkn", b"\xa9cmt"):  # the iTunes atoms players read
        assert atom in data


def test_the_mp4_cover_is_a_high_quality_jpeg(media: Media, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    real_run = subprocess.run

    def _recording_run(command: list[str], **kwargs: object) -> object:
        commands.append(command)
        return real_run(command, **kwargs)

    monkeypatch.setattr(core.subprocess, "run", _recording_run)
    embed_thumbnail(media.mp4, media.cover, "x", "y", tmp_path / "out.mp4")
    [cmd] = commands
    assert cmd[cmd.index("-q:v:1") + 1] == "2"  # see the project priorities in CLAUDE.md
    assert "-id3v2_version" not in cmd and "-vf" not in cmd  # MP3 only: ID3 and the square crop


def test_mp4_without_thumbnail_only_sets_the_tags(ffmpeg: Path, media: Media, tmp_path: Path) -> None:
    output = tmp_path / "out.mp4"
    embed_thumbnail(media.mp4, None, "Someone", "Something", output)
    info = probe(ffmpeg, output)
    assert len(re.findall(r"Stream #0:\d+", info)) == 2, info  # only the video and the audio
    assert "attached pic" not in info
    assert re.search(r"^\s*artist\s*: Someone$", info, re.MULTILINE)
    assert re.search(r"^\s*title\s*: Something$", info, re.MULTILINE)
    assert stream_md5(ffmpeg, output, "v:0") == stream_md5(ffmpeg, media.mp4, "v:0")
    assert stream_md5(ffmpeg, output) == stream_md5(ffmpeg, media.mp4)


def test_never_overwrites_an_existing_mp4(tmp_path: Path, media: Media) -> None:
    output = tmp_path / "out.mp4"
    output.write_bytes(b"user's file")

    with pytest.raises(FileExistsError):
        embed_thumbnail(media.mp4, media.cover, "x", "y", output)

    assert output.read_bytes() == b"user's file"


@pytest.mark.parametrize("name", ["out.mov", "out.MP3", "out"])
def test_other_suffixes_are_refused_before_ffmpeg_runs(tmp_path: Path, media: Media, name: str) -> None:
    output = tmp_path / name
    with pytest.raises(ValueError, match="must be .mp3 or .mp4"):
        embed_thumbnail(media.mp3, media.cover, "x", "y", output)
    assert not output.exists()


def test_full_mp4_pipeline(fake_yt_dlp: FakeYtDlp, ffmpeg: Path, media: Media, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "FFMPEG_PATH", ffmpeg)  # fake_yt_dlp pointed it at a dummy
    monkeypatch.setenv("FAKE_YTDLP_MP4", str(media.mp4))
    monkeypatch.setenv("FAKE_YTDLP_THUMB", str(media.cover))
    fake_yt_dlp.set(title="Jay-Z - Empire State of Mind (Official Video)")

    result = download_audio("https://youtu.be/abc", out_dir, media_format=Mp4Format(quality="best", codec="best"))

    assert result == out_dir / "Jay-Z - Empire State of Mind.mp4"
    assert [p.name for p in out_dir.iterdir()] == [result.name]  # no temp files left
    info = probe(ffmpeg, result)
    assert re.search(r"^\s*artist\s*: Jay-Z$", info, re.MULTILINE)
    assert re.search(r"^\s*title\s*: Empire State of Mind$", info, re.MULTILINE)
    assert re.search(r"Video: mjpeg.*\(attached pic\)$", info, re.MULTILINE)
    assert stream_md5(ffmpeg, result, "v:0") == stream_md5(ffmpeg, media.mp4, "v:0")