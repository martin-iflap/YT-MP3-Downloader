"""Opt-in end-to-end download with the real yt-dlp.exe and ffmpeg.exe: `uv run pytest -m network`.
Catches what the fakes cannot: YouTube changes that break the current yt-dlp.exe.
"""

from pathlib import Path

import pytest

from ytmp3_host import config, core

pytestmark = pytest.mark.network

SHORT_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo", 19 s


def test_real_download(out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for binary in (config.YT_DLP_PATH, config.FFMPEG_PATH):
        if not binary.exists():
            pytest.skip(f"{binary} missing")
    warnings: list[str] = []

    result = core.download_audio(SHORT_VIDEO, out_dir, on_warning=warnings.append)

    assert result.exists() and result.stat().st_size > 50_000
    assert result.read_bytes()[:4] == b"ID3\x03"
    assert warnings == []
    assert [p.name for p in out_dir.iterdir()] == [result.name]
