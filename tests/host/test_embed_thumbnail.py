"""core.embed_thumbnail and the whole pipeline with the real ffmpeg.exe (skipped without one)."""

import re
import subprocess
from pathlib import Path

import pytest

from conftest import FakeYtDlp, Media
from ytmp3_host import core
from ytmp3_host.core import download_audio, embed_thumbnail


@pytest.fixture(autouse=True)
def real_ffmpeg(ffmpeg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make core use the real ffmpeg.exe in every test of this file (skips them all without one)."""
    monkeypatch.setattr(core, "FFMPEG_PATH", ffmpeg)


def probe(ffmpeg: Path, file: Path) -> str:
    """ffmpeg's description of file (streams and metadata)."""
    result = subprocess.run([str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(file)], capture_output=True, text=True)
    return result.stderr


def audio_md5(ffmpeg: Path, file: Path) -> str:
    """Hash of the audio packets, equal only if the audio was copied, not re-encoded."""
    cmd = [str(ffmpeg), "-nostdin", "-v", "error", "-i", str(file), "-map", "0:a", "-c", "copy", "-f", "md5", "-"]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def test_embeds_square_jpeg_cover_and_artist(ffmpeg: Path, media: Media, tmp_path: Path) -> None:
    output = tmp_path / "Artist - Song.mp3"

    embed_thumbnail(media.mp3, media.cover, "Artíst", output)

    info = probe(ffmpeg, output)
    assert re.search(r"^\s*artist\s*: Artíst$", info, re.MULTILINE)
    video = re.search(r"Video: mjpeg.*?(\d+)x(\d+)", info)
    assert video, info
    assert video.group(1) == video.group(2) == "180"  # 320x180 center-cropped to 1:1
    assert "Cover (front)" in info
    assert output.read_bytes()[:4] == b"ID3\x03"  # ID3v2.3
    assert audio_md5(ffmpeg, output) == audio_md5(ffmpeg, media.mp3)


def test_without_thumbnail_only_sets_the_artist(ffmpeg: Path, media: Media, tmp_path: Path) -> None:
    output = tmp_path / "out.mp3"
    embed_thumbnail(media.mp3, None, "Someone", output)
    info = probe(ffmpeg, output)
    assert "Video:" not in info
    assert re.search(r"^\s*artist\s*: Someone$", info, re.MULTILINE)
    assert audio_md5(ffmpeg, output) == audio_md5(ffmpeg, media.mp3)


def test_broken_input_raises_and_leaves_no_partial_file(tmp_path: Path, media: Media) -> None:
    broken = tmp_path / "broken.mp3"
    broken.write_bytes(b"not an mp3 at all")
    output = tmp_path / "out.mp3"

    with pytest.raises(subprocess.CalledProcessError) as info:
        embed_thumbnail(broken, media.cover, "x", output)

    assert not output.exists()
    assert core.describe_error(info.value).startswith(f"{core.FFMPEG_PATH.name} failed: ")


def test_never_overwrites_or_deletes_an_existing_file(tmp_path: Path, media: Media) -> None:
    output = tmp_path / "out.mp3"
    output.write_bytes(b"user's file")

    # ffmpeg 7 exits 0 when -n refuses to overwrite, so this must not depend on ffmpeg
    with pytest.raises(FileExistsError):
        embed_thumbnail(media.mp3, media.cover, "x", output)

    assert output.read_bytes() == b"user's file"


def test_full_pipeline(fake_yt_dlp: FakeYtDlp, ffmpeg: Path, media: Media, out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "FFMPEG_PATH", ffmpeg)  # fake_yt_dlp pointed it at a dummy
    monkeypatch.setenv("FAKE_YTDLP_MP3", str(media.mp3))
    monkeypatch.setenv("FAKE_YTDLP_THUMB", str(media.cover))
    fake_yt_dlp.set(title="Jay-Z - Empire State of Mind (Official Video)")

    result = download_audio("https://youtu.be/abc", out_dir)

    assert [p.name for p in out_dir.iterdir()] == ["Jay-Z - Empire State of Mind.mp3"]
    info = probe(ffmpeg, result)
    assert re.search(r"^\s*artist\s*: Jay-Z$", info, re.MULTILINE)
    assert "Video: mjpeg" in info
