"""Running yt-dlp: its command line, the download with its watchdog, and stopping its process tree."""

import errno
import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, assert_never

from ytmp3_host.config import DENO_PATH, FFMPEG_PATH, yt_dlp_path
from ytmp3_host.media_format import MediaFormat, Mp3Format, Mp4Format
from ytmp3_host.metadata import INFO_FIELDS, VideoInfo, parse_info

logger = logging.getLogger(__name__)

INFO_MARKER = "YTMP3_INFO:"
PROGRESS_MARKER = "YTMP3_PROGRESS:"
THUMBNAIL_EXTS = ("webp", "jpg", "jpeg", "png")
TASKKILL_TIMEOUT_S = 10

DOWNLOAD_TIMEOUT_S = 900
MIN_SPEED_BPS = 500_000  # bytes per sec
DOWNLOAD_TIMEOUT_BASE_S = 120   # time needed for other steps
DOWNLOAD_TIMEOUT_CAP_S = 4 * 3600  # 4h


def _worst_bytes_per_s(media_format: MediaFormat) -> float:
    """The most data one second of media can need, for a video without a size."""
    match media_format:
        case Mp3Format():
            return 40_000  # 320 kbit/s source audio
        case Mp4Format():
            return 3_000_000  # 4K60 HDR, ~180 MB/min
        case _:
            assert_never(media_format)


def _positive_number(info: VideoInfo, key: str) -> float | None:
    """A video info field as a positive number, or None if it is missing or not a positive number."""
    value = info.get(key)
    return float(value) if isinstance(value, int | float) and value > 0 else None


@dataclass(frozen=True, slots=True)
class Progress:
    """Where a running download is, for the user.

    Attributes:
        stage: "downloading" while yt-dlp downloads the streams; "processing" from its
            first postprocessor on (the MP3 encode or the MP4 merge, then the cover pass),
            which reports no progress of its own.
        fraction: The downloaded share of the whole download, 0.0-1.0, or None while
            unknown (and while processing).
    """

    stage: Literal["downloading", "processing"]
    fraction: float | None = None


class ProgressTracker:
    """Turns yt-dlp's progress events into one Progress for the whole download.

    yt-dlp reports every stream on its own (an MP4 is two downloads, video then audio,
    each counting from 0 to its own size). The fraction counts the bytes of all streams
    together. Once every size is known (the last stream reports its own), they are the
    whole, so the download ends at exactly 100 % even when `filesize_approx` was off.
    Before that the whole is `filesize_approx`, the size yt-dlp expects for all streams,
    but never less than the sizes known so far plus UNKNOWN_SHARE for what is still
    unknown: without a usable estimate an MP4's video ends at 90 % instead of sitting at
    100 % while the audio downloads. The fraction never goes back and is rounded to
    0.1 %, and an event that would repeat the last Progress gives none, so a large
    download reports at most ~1000 steps.

    Attributes:
        expected_bytes: `filesize_approx` from the video info, or None if unknown.
        streams: How many streams yt-dlp downloads (`format_id` "399+251" is 2); 1 if unknown.
    """

    UNKNOWN_SHARE = 0.1  # an MP4's audio is ~10 % of it (3.4 of 34 MB for a 1080p AV1 song)

    def __init__(self) -> None:
        self.expected_bytes: float | None = None
        self.streams = 1
        self._finished_bytes = 0.0  # the streams that finished downloading
        self._finished_streams = 0
        self._fraction = 0.0
        self._last: Progress | None = None

    def use_info(self, info: VideoInfo) -> None:
        """Take the expected size and the stream count from the video info yt-dlp printed."""
        self.expected_bytes = _positive_number(info, "filesize_approx")
        format_id = info.get("format_id")
        self.streams = format_id.count("+") + 1 if isinstance(format_id, str) else 1

    def update(self, event: dict[str, object]) -> Progress | None:
        """Take one event of the progress templates (see build_download_command).

        Args:
            event: A download event (status, downloaded_bytes, total_bytes or
                total_bytes_estimate) or a postprocessor event (status, postprocessor).
        Returns:
            The new Progress, or None if the event changes nothing for the user.
        """
        if "postprocessor" in event:
            progress = Progress("processing")
        else:
            progress = self._download_progress(event)
        if progress is None or progress == self._last:
            return None
        self._last = progress
        return progress

    def _download_progress(self, event: dict[str, object]) -> Progress | None:
        """The Progress while yt-dlp downloads the streams, or None if the event is not a download one.
         - compute the downloaded bytes and the sizes known so far from the event and the finished streams
         - the whole is those sizes once nothing is unknown, else filesize_approx with room for the rest
         - update the fraction, never going back and rounding to 0.1 %

         Notes: - total bytes is the stream's size
                - total bytes estimate is the stream's size if known, else the best guess (HLS)
                - filesize_approx is the whole download's size, if known, else None
        """
        status = event.get("status")
        if status not in ("downloading", "finished"):  # yt-dlp reports error itself
            return None
        downloaded = _positive_number(event, "downloaded_bytes") or 0.0
        size = _positive_number(event, "total_bytes") or _positive_number(event, "total_bytes_estimate")
        if status == "finished":
            self._finished_bytes += downloaded or size or 0.0
            self._finished_streams += 1
            done = known = self._finished_bytes
            unknown_left = self._finished_streams < self.streams
        else:
            done = self._finished_bytes + downloaded
            known = self._finished_bytes + (size or 0.0)
            unknown_left = size is None or self._finished_streams + 1 < self.streams
        if not unknown_left:
            whole = known
        elif known or self.expected_bytes:
            # done: a stream of unknown size has at least the bytes it downloaded
            whole = max(self.expected_bytes or 0.0, max(known, done) / (1 - self.UNKNOWN_SHARE))
        else:
            whole = 0.0  # nothing about the size is known yet
        if not whole:
            return Progress("downloading")
        self._fraction = max(self._fraction, round(min(done / whole, 1.0), 3))
        return Progress("downloading", self._fraction)


def download_timeout(info: VideoInfo, media_format: MediaFormat, fallback: float) -> float:
    """The timeout for a download, from its size in the video info.

    The size is yt-dlp's `filesize_approx`; without it, the duration times the most data one
    second of the format can need. The timeout is the time that size takes at MIN_SPEED_BPS
    plus DOWNLOAD_TIMEOUT_BASE_S, never shorter than fallback, so a small download (any MP3)
    keeps fallback. It is never longer than DOWNLOAD_TIMEOUT_CAP_S, whatever fallback is: the
    cleanup of stale temp files relies on no download running longer.

    Args:
        info: The video info (from metadata.parse_info).
        media_format: The requested media format (MP3 or MP4).
        fallback: The timeout without a size or duration, and the minimum below the cap.

    Returns:
        The timeout in seconds.
    """
    size = _positive_number(info, "filesize_approx")
    if size is None and (duration := _positive_number(info, "duration")) is not None:
        size = duration * _worst_bytes_per_s(media_format)
    timeout = fallback if size is None else max(fallback, DOWNLOAD_TIMEOUT_BASE_S + size / MIN_SPEED_BPS)
    return min(timeout, DOWNLOAD_TIMEOUT_CAP_S)


def kill_process_tree(process: subprocess.Popen[str]) -> None:
    """Kill a process together with all its children.

    process.kill() stops only the process itself, while its children keep running and
    keep the stdout pipe open: the ffmpeg yt-dlp runs to encode, and with the single-file
    yt-dlp.exe (a PyInstaller onefile build, the fallback) the real yt-dlp, a child of
    its bootloader. taskkill /T stops the whole tree.

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


def _format_args(media_format: MediaFormat) -> list[str]:
    """The yt-dlp arguments that choose and convert the streams for one format.

    MP3: `-x` extracts the audio, `--audio-quality 0` is LAME V0, the best VBR preset,
    which encodes as fast as the default V5.
    MP4: yt-dlp's default choice (best video + best audio) stays; `-S` only sets what
    it prefers, earlier keys first. `compatible` prefers H.264 video and AAC audio
    (plays everywhere) over resolution; a quality is the best resolution at or below it;
    SDR is preferred, since HDR looks washed out on most screens and players.
    `--merge-output-format mp4` puts the two streams into one MP4 without re-encoding;
    `--remux-video mp4` does the same for a site's single video file in another container
    (WebM, FLV). A merged MP4 is left as it is.

    Args:
        media_format: The requested media format.
    Returns:
        The arguments for build_download_command.
    """
    match media_format:
        case Mp3Format():
            return ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        case Mp4Format(quality=quality, codec=codec):
            sort = ["vcodec:h264", "acodec:aac"] if codec == "compatible" else []
            if quality != "best":
                sort.append(f"res:{quality}")
            sort.append("hdr:sdr")
            return ["-S", ",".join(sort), "--merge-output-format", "mp4", "--remux-video", "mp4"]
        case _ as unreachable:
            assert_never(unreachable)


def build_download_command(url: str, temp_stem: Path, media_format: MediaFormat) -> list[str]:
    """Build the yt-dlp command for one download.

    A single call fetches the video info, the media (see _format_args) and the raw
    thumbnail. The info (INFO_FIELDS) is printed as JSON after INFO_MARKER before the
    download starts (`before_dl`), so an existing title-named file can stop it early.
    `--print` implies `--quiet`, so `--progress` keeps the progress lines; their templates
    print each download step and each postprocessor start as JSON after PROGRESS_MARKER
    (for ProgressTracker). `--no-simulate` makes sure printing never turns the call into
    a dry run. `--playlist-items 1` and `--no-playlist` ensure that only the first item in a
    playlist is downloaded. A Deno the host installed is passed explicitly
    (yt-dlp only looks on PATH); without one, yt-dlp uses a Deno on PATH or YouTube
    clients that need no JavaScript, which is faster.

    Args:
        url: The video or track URL.
        temp_stem: Output path without extension; yt-dlp appends the extensions.
        media_format: The requested media format (MP3 or MP4).
    Returns:
        The command as an argument list for subprocess.
    """
    js_runtime = ["--js-runtimes", f"deno:{DENO_PATH}"] if DENO_PATH.exists() else []
    return [
        str(yt_dlp_path()),
        "--no-playlist",
        "--playlist-items", "1",
        "--concurrent-fragments", "3",
        *_format_args(media_format),
        "--write-thumbnail",
        "--ffmpeg-location", str(FFMPEG_PATH),
        "--print", f"before_dl:{INFO_MARKER}%(.{{{','.join(INFO_FIELDS)}}})j",
        "--no-simulate",
        "--progress", "--newline",
        "--progress-template",
        f"download:{PROGRESS_MARKER}%(progress.{{status,downloaded_bytes,total_bytes,total_bytes_estimate}})j",
        "--progress-template", f"postprocess:{PROGRESS_MARKER}%(progress.{{status,postprocessor}})j",
        "--encoding", "utf-8",
        *js_runtime,
        "-o", f"{str(temp_stem).replace('%', '%%')}.%(ext)s",  # a % in the folder is not part of the template
        url,
    ]


def find_thumbnail(temp_stem: Path) -> Path | None:
    """Find the thumbnail yt-dlp wrote next to the temp media file.

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


def _progress_event(line: str) -> dict[str, object]:
    """The JSON object of a progress line, or {} if it is not one (ProgressTracker ignores that)."""
    try:
        event = json.loads(line[len(PROGRESS_MARKER):])
    except json.JSONDecodeError:
        return {}
    return event if isinstance(event, dict) else {}


def run_yt_dlp(
    url: str,
    temp_stem: Path,
    media_format: MediaFormat,
    *,
    on_progress: Callable[[Progress], object] | None,
    on_start: Callable[[subprocess.Popen[str], Path], None] | None,
    on_video_info: Callable[[VideoInfo], None],
    on_timeout: Callable[[float], None] | None,
) -> VideoInfo:
    """Run the yt-dlp download and return the video info.

    Args:
        url: The video or track URL.
        temp_stem: Output path without extension.
        media_format: The requested media format (MP3 or MP4).
        on_progress: Optional callback receiving each new Progress (see ProgressTracker).
        on_start: Optional callback receiving the process and temp stem once started.
        on_video_info: Callback receiving the video info (see INFO_FIELDS) before the
            download starts; raising from it stops yt-dlp and propagates.
        on_timeout: Optional callback receiving the timeout in seconds once it is known.

    Returns:
        The video info yt-dlp printed (see INFO_FIELDS), or {} if it printed none.

    Raises:
        subprocess.TimeoutExpired: If the download exceeded its timeout (the extended one, if any).
        subprocess.CalledProcessError: If yt-dlp exited with an error. `output`
            holds the last lines of its output, without progress lines.
        FileNotFoundError: If yt-dlp.exe can't be started (e.g. a corrupt file), for the
            repair in download_with_repair, like a missing one.
    """
    try:
        process = subprocess.Popen(
            build_download_command(url, temp_stem, media_format),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as e:
        raise FileNotFoundError(errno.ENOENT, "yt-dlp.exe could not be started", str(yt_dlp_path())) from e
    with process:
        timed_out = threading.Event()

        def _kill_on_timeout() -> None:
            timed_out.set()
            kill_process_tree(process)

        timeout = DOWNLOAD_TIMEOUT_S
        watchdog = threading.Timer(timeout, _kill_on_timeout)
        started = time.monotonic()
        watchdog.start()

        info: VideoInfo = {}
        output_lines: list[str] = []
        tracker = ProgressTracker()
        try:
            if on_start:
                on_start(process, temp_stem)
            assert process.stdout is not None  # stdout=PIPE
            for line in process.stdout:
                if line.startswith(PROGRESS_MARKER):
                    if on_progress and (progress := tracker.update(_progress_event(line))):
                        on_progress(progress)
                    continue
                if line.startswith(INFO_MARKER):
                    info = parse_info(line[len(INFO_MARKER):])
                    tracker.use_info(info)
                    on_video_info(info)
                    new = download_timeout(info, media_format, timeout)
                    if timeout < new:
                        watchdog.cancel()
                        watchdog = threading.Timer(new - (time.monotonic() - started), _kill_on_timeout)
                        watchdog.start()
                        if on_timeout:
                            on_timeout(new)
                        timeout = new
                    continue
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
