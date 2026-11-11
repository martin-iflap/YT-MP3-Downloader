"""Running yt-dlp: its command line, the download with its watchdog, and stopping its process tree."""

import errno
import logging
import re
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from ytmp3_host.config import DENO_PATH, FFMPEG_PATH, YT_DLP_PATH
from ytmp3_host.metadata import INFO_FIELDS, VideoInfo, parse_info

logger = logging.getLogger(__name__)

INFO_MARKER = "YTMP3_INFO:"
THUMBNAIL_EXTS = ("webp", "jpg", "jpeg", "png")
PROGRESS_RE = re.compile(r"(\d+\.?\d*)%")
TASKKILL_TIMEOUT_S = 10


def kill_process_tree(process: subprocess.Popen[str]) -> None:
    """Kill a process together with all its children.

    yt-dlp.exe is a PyInstaller onefile build: process.kill() only stops its
    bootloader, while the real yt-dlp child (and its ffmpeg) keep running and keep
    the stdout pipe open. taskkill /T stops the whole tree.

    Args:
        process: The process to kill. Does nothing if it already exited.
    """
    if process.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=True,
            timeout=TASKKILL_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.error("taskkill failed, killing only the main process: %s", e)
        process.kill()


def build_download_command(url: str, temp_stem: Path) -> list[str]:
    """Build the yt-dlp command for one download.

    A single call fetches the video info, the audio and the raw thumbnail. The info
    (INFO_FIELDS) is printed as JSON after INFO_MARKER before the download starts
    (`before_dl`), so an existing title-named file can stop it early. `--audio-quality 0`
    is LAME V0, the best VBR preset, which encodes as fast as the default V5.
    `--print` implies `--quiet`, so `--progress` keeps the progress lines the GUI parses;
    `--no-simulate` makes sure printing never turns the call into a dry run.
    `--playlist-items 1` and `--no-playlist` ensure that only the first item in a
    playlist is downloaded. A Deno the host installed is passed explicitly
    (yt-dlp only looks on PATH); without one, yt-dlp uses a Deno on PATH or YouTube
    clients that need no JavaScript, which is faster.

    Args:
        url: The video or track URL.
        temp_stem: Output path without extension; yt-dlp appends the extensions.
    Returns:
        The command as an argument list for subprocess.
    """
    js_runtime = ["--js-runtimes", f"deno:{DENO_PATH}"] if DENO_PATH.exists() else []
    return [
        str(YT_DLP_PATH),
        "--no-playlist",
        "--playlist-items", "1",
        "--concurrent-fragments", "3",
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "--write-thumbnail",
        "--ffmpeg-location", str(FFMPEG_PATH),
        "--print", f"before_dl:{INFO_MARKER}%(.{{{','.join(INFO_FIELDS)}}})j",
        "--no-simulate",
        "--progress", "--newline",
        "--encoding", "utf-8",
        *js_runtime,
        "-o", f"{str(temp_stem).replace('%', '%%')}.%(ext)s",  # a % in the folder is not part of the template
        url,
    ]


def find_thumbnail(temp_stem: Path) -> Path | None:
    """Find the thumbnail yt-dlp wrote next to the temp MP3.

    Args:
        temp_stem: The temp output path without extension.
    Returns:
        The thumbnail path (usually .webp), or None if yt-dlp wrote none.
    """
    for ext in THUMBNAIL_EXTS:
        thumb = temp_stem.with_name(f"{temp_stem.name}.{ext}")
        if thumb.exists():
            return thumb
    return None


def run_yt_dlp(
    url: str,
    temp_stem: Path,
    *,
    on_progress: Callable[[float], object] | None,
    on_start: Callable[[subprocess.Popen[str], Path], None] | None,
    on_video_info: Callable[[VideoInfo], None],
    timeout: float,
) -> VideoInfo:
    """Run the yt-dlp download and return the video info.

    Args:
        url: The video or track URL.
        temp_stem: Output path without extension.
        on_progress: Optional callback receiving the progress as 0.0-1.0.
        on_start: Optional callback receiving the process and temp stem once started.
        on_video_info: Callback receiving the video info (see INFO_FIELDS) before the
            download starts; raising from it stops yt-dlp and propagates.
        timeout: Seconds after which the whole process tree is killed.

    Returns:
        The video info yt-dlp printed (see INFO_FIELDS), or {} if it printed none.

    Raises:
        subprocess.TimeoutExpired: If the download exceeded timeout.
        subprocess.CalledProcessError: If yt-dlp exited with an error. `output`
            holds the last lines of its output, without progress lines.
        FileNotFoundError: If yt-dlp.exe can't be started (e.g. a corrupt file), for the
            repair in download_with_repair, like a missing one.
    """
    try:
        process = subprocess.Popen(
            build_download_command(url, temp_stem),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as e:
        raise FileNotFoundError(errno.ENOENT, "yt-dlp.exe could not be started", str(YT_DLP_PATH)) from e
    with process:
        timed_out = threading.Event()

        def _kill_on_timeout() -> None:
            timed_out.set()
            kill_process_tree(process)

        watchdog = threading.Timer(timeout, _kill_on_timeout)
        watchdog.start()

        info: VideoInfo = {}
        output_lines: list[str] = []
        try:
            if on_start:
                on_start(process, temp_stem)
            assert process.stdout is not None  # stdout=PIPE
            for line in process.stdout:
                if line.startswith(INFO_MARKER):
                    info = parse_info(line[len(INFO_MARKER):])
                    on_video_info(info)
                    continue
                match = PROGRESS_RE.search(line)
                if match and on_progress:
                    on_progress(float(match.group(1)) / 100.0)
                # progress lines would push the warnings download_with_repair reads out of the error's output
                if not (match and line.startswith("[download]")):
                    output_lines.append(line)
            process.wait()
        finally:
            watchdog.cancel()
            # a raising callback must not leave yt-dlp running (Popen.__exit__ would wait for it)
            kill_process_tree(process)

    if timed_out.is_set() and process.returncode != 0:
        raise subprocess.TimeoutExpired(process.args, timeout)
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, process.args, "".join(output_lines[-20:]))
    return info
