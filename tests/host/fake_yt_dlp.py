"""Stand-in for yt-dlp.exe in the host tests.

Run as `python fake_yt_dlp.py <the arguments core.build_download_command builds>`.
It honors the real `-o` output template and `--print` title template, so the
tests also check the command contract. With `--version` (health.yt_dlp_version_command)
it only prints its version; fail, hang and bad_version apply there too.
Behavior comes from environment variables:

    FAKE_YTDLP_MODE     success (default) | fail | hang | no_title | no_thumb | no_mp3 | bad_version |
                        needs_js (fails like the real one when YouTube wants JavaScript, unless given --js-runtimes)
    FAKE_YTDLP_TITLE    the video title to print
    FAKE_YTDLP_VERSION  the version to print (default 2026.08.19)
    FAKE_YTDLP_MP3      an MP3 to copy as the download (default: junk bytes)
    FAKE_YTDLP_THUMB    an image to copy as the .png thumbnail (default: junk .webp)
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def emit(line: str) -> None:
    print(line, flush=True)


def hang() -> None:
    """Like the real yt-dlp child behind the PyInstaller bootloader: a second process keeps stdout open."""
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdout=sys.stdout)
    time.sleep(30)
    sys.exit(0)


def print_version(mode: str) -> None:
    if mode == "fail":
        emit("ERROR: something broke")
        sys.exit(1)
    if mode == "hang":
        hang()
    emit("[debug] not a version" if mode == "bad_version" else os.environ.get("FAKE_YTDLP_VERSION", "2026.08.19"))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # yt-dlp runs with --encoding utf-8
    args = sys.argv[1:]
    mode = os.environ.get("FAKE_YTDLP_MODE", "success")
    if args == ["--version"]:
        print_version(mode)
        return
    stem = args[args.index("-o") + 1].removesuffix(".%(ext)s")
    title_template = args[args.index("--print") + 1].removeprefix("before_dl:")
    title = os.environ.get("FAKE_YTDLP_TITLE", "Artist - Song (Official Video)")

    emit(f"[youtube] Extracting URL: {args[-1]}")
    if mode == "needs_js" and "--js-runtimes" not in args:
        emit("WARNING: [youtube] No supported JavaScript runtime could be found.")
        emit("WARNING: [youtube] abc123: n challenge solving failed: Some formats may be missing.")
        emit("ERROR: [youtube] abc123: Requested format is not available. Use --list-formats for a list of available formats")
        sys.exit(1)
    if mode != "no_title":
        emit(title_template.replace("%(title)s", title))
    partial = Path(f"{stem}.webm.part")
    partial.write_bytes(b"partial download")
    for percent in ("0.0", "42.5", "100.0"):
        emit(f"[download] {percent:>5}% of 3.00MiB at 1.00MiB/s ETA 00:01")

    if mode == "fail":
        emit("WARNING: [youtube] some harmless warning")
        emit("ERROR: [youtube] abc123: Video unavailable")
        sys.exit(1)
    if mode == "hang":
        hang()

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


if __name__ == "__main__":
    main()
