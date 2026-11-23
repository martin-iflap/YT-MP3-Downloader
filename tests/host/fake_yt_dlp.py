"""Stand-in for yt-dlp.exe in the host tests.

Run as `python fake_yt_dlp.py <the arguments ytdlp.build_download_command builds>`.
It honors the real `-o` output template, the `--print` info template (`%(.{fields})j`:
a JSON object of the requested fields the video has) and the `--progress-template`s
(`download:`/`postprocess:` with `%(progress.{fields})j`), so the tests also check the
command contract. With `--version` (health.yt_dlp_version_command) it only prints its
version; fail, hang and bad_version apply there too.
Behavior comes from environment variables:

    FAKE_YTDLP_MODE     success (default) | fail | hang | no_title | no_thumb | no_mp3 | bad_version |
                        needs_js (fails like the real one when YouTube wants JavaScript, unless given --js-runtimes
                        with a Deno that is not outdated, i.e. does not hold OUTDATED_DENO) |
                        missing_dll (a onedir build with a DLL a virus scanner removed: --version works, a
                        download fails at an import)
    FAKE_YTDLP_TITLE    the video title to print
    FAKE_YTDLP_INFO     more video info as a JSON object, e.g. {"album": "..."} (default: {"channel": "Some Channel"});
                        format_id and vcodec default to YouTube's (one audio stream for an MP3, AV1 video
                        plus audio for an MP4), {"vcodec": "none"} is a link without video
    FAKE_YTDLP_VERSION  the version to print (default 2026.08.19)
    FAKE_YTDLP_MP3      an MP3 to copy as the download (default: junk bytes)
    FAKE_YTDLP_MP4      the same for an MP4 download
The download gets the extension the real yt-dlp would give it (output_ext), so a
command without the right format arguments fails like the real one; no_mp3 writes no
media file in either format.
    FAKE_YTDLP_THUMB    an image to copy as the .png thumbnail (default: junk .webp)
    FAKE_YTDLP_CHILD_PID_FILE  where hang mode writes the PID of the process it leaves behind
    FAKE_YTDLP_DELAY_S  seconds the download takes after the info is printed (default 0), for the watchdog
    FAKE_YTDLP_STREAMS  the sizes of the downloaded streams as a JSON list (default: [3000000], or
                        [3000000, 1000000] for a merged video and audio); each reports 0 %, 42.5 % and finished
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

OUTDATED_DENO = b"a Deno too old for YouTube's challenges"


def emit(line: str) -> None:
    print(line, flush=True)


def hang() -> None:
    """Like the real yt-dlp child behind the PyInstaller bootloader: a second process keeps stdout open."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdout=sys.stdout)
    if pid_file := os.environ.get("FAKE_YTDLP_CHILD_PID_FILE"):
        Path(pid_file).write_text(str(child.pid))
    time.sleep(30)
    sys.exit(0)


def print_version(mode: str) -> None:
    if mode == "fail":
        emit("ERROR: something broke")
        sys.exit(1)
    if mode == "hang":
        hang()
    emit("[debug] not a version" if mode == "bad_version" else os.environ.get("FAKE_YTDLP_VERSION", "2026.08.19"))


def solves_js(args: list[str]) -> bool:
    """Whether yt-dlp was given a JavaScript runtime (--js-runtimes deno:<path>) that is not outdated."""
    if "--js-runtimes" not in args:
        return False
    deno = Path(args[args.index("--js-runtimes") + 1].removeprefix("deno:"))
    return deno.read_bytes() != OUTDATED_DENO


def output_ext(args: list[str]) -> str:
    """The extension the real yt-dlp gives the finished file, decided by the same arguments."""
    if "-x" in args:
        return args[args.index("--audio-format") + 1]
    if "--merge-output-format" in args:
        return args[args.index("--merge-output-format") + 1]
    return "webm"  # what YouTube's best streams merge to without it


def progress_templates(args: list[str]) -> dict[str, tuple[str, list[str]]]:
    """The marker and fields of each --progress-template, by type (download, postprocess)."""
    templates = {}
    for index, arg in enumerate(args):
        if arg == "--progress-template":
            kind, _, template = args[index + 1].partition(":")
            parts = re.fullmatch(r"(.*)%\(progress\.\{(.*)}\)j", template)
            assert parts, "unexpected --progress-template"
            templates[kind] = (parts[1], parts[2].split(","))
    return templates


def emit_progress(templates: dict[str, tuple[str, list[str]]], kind: str, event: dict[str, object]) -> None:
    """Print one progress event like the real yt-dlp: only the requested fields the event has."""
    if kind in templates:
        marker, fields = templates[kind]
        emit(marker + json.dumps({field: event[field] for field in fields if field in event}))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # yt-dlp runs with --encoding utf-8
    args = sys.argv[1:]
    mode = os.environ.get("FAKE_YTDLP_MODE", "success")
    if args == ["--version"]:
        print_version(mode)
        return
    stem = args[args.index("-o") + 1].removesuffix(".%(ext)s").replace("%%", "%")
    template = re.fullmatch(r"before_dl:(.*)%\(\.\{(.*)}\)j", args[args.index("--print") + 1])
    assert template, "unexpected --print template"
    marker, fields = template.groups()
    info = json.loads(os.environ.get("FAKE_YTDLP_INFO", '{"channel": "Some Channel"}'))
    info["title"] = os.environ.get("FAKE_YTDLP_TITLE", "Artist - Song (Official Video)")
    merged = "--merge-output-format" in args
    # what the real one picks on YouTube: the audio for an MP3, AV1 video plus audio for an MP4
    info.setdefault("format_id", "399+251" if merged else "251")
    info.setdefault("vcodec", "av01.0.08M.08" if merged else "none")

    if mode == "missing_dll":  # what the PyInstaller build of yt-dlp prints
        emit("Traceback (most recent call last):")
        emit('  File "ssl.py", line 100, in <module>')
        emit("ImportError: DLL load failed while importing _ssl: The specified module could not be found.")
        emit("[PYI-1234:ERROR] Failed to execute script '__main__' due to unhandled exception!")
        sys.exit(1)
    emit(f"[youtube] Extracting URL: {args[-1]}")
    if mode == "needs_js" and not solves_js(args):
        if "--js-runtimes" not in args:
            emit("WARNING: [youtube] No supported JavaScript runtime could be found.")
        emit("WARNING: [youtube] abc123: n challenge solving failed: Some formats may be missing.")
        emit("ERROR: [youtube] abc123: Requested format is not available. Use --list-formats for a list of available formats")
        sys.exit(1)
    if mode != "no_title":
        emit(marker + json.dumps({field: info[field] for field in fields.split(",") if field in info}))
    partial = Path(f"{stem}.webm.part")
    partial.write_bytes(b"partial download")
    time.sleep(float(os.environ.get("FAKE_YTDLP_DELAY_S", "0")))
    templates = progress_templates(args)
    for size in json.loads(os.environ.get("FAKE_YTDLP_STREAMS", "[3000000, 1000000]" if merged else "[3000000]")):
        for downloaded in (0, int(size * 0.425)):
            emit_progress(templates, "download", {"status": "downloading", "downloaded_bytes": downloaded, "total_bytes": size})
        emit_progress(templates, "download", {"status": "finished", "downloaded_bytes": size, "total_bytes": size})

    if mode == "fail":
        emit("WARNING: [youtube] some harmless warning")
        emit("ERROR: [youtube] abc123: Video unavailable")
        sys.exit(1)
    if mode == "hang":
        hang()

    partial.unlink()
    for postprocessor in ("ExtractAudio" if "-x" in args else "Merger", "MoveFiles"):
        for status in ("started", "finished"):
            emit_progress(templates, "postprocess", {"status": status, "postprocessor": postprocessor})
    if mode != "no_mp3":
        ext = output_ext(args)
        if source := os.environ.get(f"FAKE_YTDLP_{ext.upper()}"):
            shutil.copyfile(source, f"{stem}.{ext}")
        else:
            Path(f"{stem}.{ext}").write_bytes(f"fake {ext}".encode())
    if mode != "no_thumb":
        if source := os.environ.get("FAKE_YTDLP_THUMB"):
            shutil.copyfile(source, f"{stem}.png")
        else:
            Path(f"{stem}.webp").write_bytes(b"fake webp")


if __name__ == "__main__":
    main()
