"""Stand-in for yt-dlp.exe in the host tests.

Run as `python fake_yt_dlp.py <the arguments core.build_download_command builds>`.
It honors the real `-o` output template and `--print` title template, so the
tests also check the command contract. Behavior comes from environment variables:

    FAKE_YTDLP_MODE   success (default) | fail | hang | no_title | no_thumb | no_mp3
    FAKE_YTDLP_TITLE  the video title to print
    FAKE_YTDLP_MP3    an MP3 to copy as the download (default: junk bytes)
    FAKE_YTDLP_THUMB  an image to copy as the .png thumbnail (default: junk .webp)
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def emit(line: str) -> None:
    print(line, flush=True)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # yt-dlp runs with --encoding utf-8
    args = sys.argv[1:]
    stem = args[args.index("-o") + 1].removesuffix(".%(ext)s")
    title_template = args[args.index("--print") + 1].removeprefix("after_move:")
    mode = os.environ.get("FAKE_YTDLP_MODE", "success")
    title = os.environ.get("FAKE_YTDLP_TITLE", "Artist - Song (Official Video)")

    emit(f"[youtube] Extracting URL: {args[-1]}")
    partial = Path(f"{stem}.webm.part")
    partial.write_bytes(b"partial download")
    for percent in ("0.0", "42.5", "100.0"):
        emit(f"[download] {percent:>5}% of 3.00MiB at 1.00MiB/s ETA 00:01")

    if mode == "fail":
        emit("WARNING: [youtube] some harmless warning")
        emit("ERROR: [youtube] abc123: Video unavailable")
        sys.exit(1)
    if mode == "hang":
        # like the real yt-dlp child behind the PyInstaller bootloader: a second
        # process that keeps the stdout pipe open
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdout=sys.stdout)
        time.sleep(30)
        sys.exit(0)

    partial.unlink()
    if mode != "no_mp3":
        if source := os.environ.get("FAKE_YTDLP_MP3"):
            shutil.copyfile(source, f"{stem}.mp3")
        else:
            Path(f"{stem}.mp3").write_bytes(b"fake mp3")
    if mode != "no_thumb":
        if source := os.environ.get("FAKE_YTDLP_THUMB"):
            shutil.copyfile(source, f"{stem}.png")
        else:
            Path(f"{stem}.webp").write_bytes(b"fake webp")
    if mode != "no_title":
        emit(title_template.replace("%(title)s", title))


if __name__ == "__main__":
    main()
