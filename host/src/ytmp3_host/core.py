"""Download pipeline shared by the headless and GUI download paths.

One download is one yt-dlp call (audio as MP3 V0 plus the raw thumbnail) followed by
one ffmpeg pass that adds the cover art and tags and writes the final MP3.
"""

import errno
import logging
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from ytmp3_host.config import FFMPEG_PATH, YT_DLP_PATH
from ytmp3_host.files import remove_stale_temp_files, remove_temp_files
from ytmp3_host.metadata import artist_for, extra_tags, info_text, title_filename
from ytmp3_host.naming import DEFAULT_FILENAME, is_youtube_url, title_from_filename
from ytmp3_host.ytdlp import find_thumbnail, run_yt_dlp
# only download_with_repair's failure path uses these
lazy from ytmp3_host import health, updater

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT_S = 300
EMBED_TIMEOUT_S = 50
# what yt-dlp prints when YouTube asks for a JavaScript challenge it could not solve without a runtime
JS_CHALLENGE_FAILURES = ("Signature solving failed", "n challenge solving failed", "Requested format is not available")


def embed_thumbnail(
    temp_mp3: Path,
    thumb_file: Path | None,
    artist: str,
    title: str,
    output: Path,
    tags: dict[str, str] | None = None,
) -> None:
    """Write the final MP3 with cover art and tags in a single ffmpeg pass.

    The audio stream is copied, never re-encoded. The thumbnail is center-cropped
    to 1:1 and encoded as high-quality JPEG ID3v2.3 cover art. Without a thumbnail
    only the tags are added.

    Args:
        temp_mp3: The MP3 yt-dlp produced.
        thumb_file: The thumbnail to embed, or None.
        artist: The artist metadata value.
        title: The title metadata value.
        output: The final MP3 path. Must not exist yet (ffmpeg runs with -n).
        tags: More metadata as ffmpeg keys (see extra_tags), e.g. {"album": "..."}.
    Raises:
        FileExistsError: If output already exists; it is left untouched.
        subprocess.CalledProcessError: If ffmpeg fails. A partial output is removed.
        subprocess.TimeoutExpired: If ffmpeg takes longer than EMBED_TIMEOUT_S.
    """
    cmd = [str(FFMPEG_PATH), "-hide_banner", "-nostdin", "-loglevel", "error", "-n", "-i", str(temp_mp3)]
    if thumb_file:
        cmd += [
            "-i", str(thumb_file),
            "-map", "0:a",
            "-map", "1:0",
            "-c:v", "mjpeg", "-q:v", "2",
            "-vf", "crop='min(iw,ih)':'min(iw,ih)'",
            "-metadata:s:v", "title=Album cover",
            "-metadata:s:v", "comment=Cover (front)",
        ]
    cmd += [
        "-c:a", "copy",
        "-id3v2_version", "3",
        "-metadata", f"artist={artist}",
        "-metadata", f"title={title}",
    ]
    for key, value in (tags or {}).items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd.append(str(output))
    # -n alone is not enough: ffmpeg 7 exits 0 when it refuses to overwrite
    _ensure_not_exists(output)
    try:
        subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=EMBED_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.error("Error with thumbnail embedding: %s\n%s", e, e.stderr or "")
        output.unlink(missing_ok=True)
        raise


def _ensure_not_exists(output: Path) -> None:
    """Raise FileExistsError if output already exists."""
    if output.exists():
        raise FileExistsError(errno.EEXIST, "File with this name already exists", str(output))


def download_audio(
    url: str,
    folder: Path,
    filename: str = "",
    on_progress: Callable[[float], object] | None = None,
    on_start: Callable[[subprocess.Popen[str], Path], None] | None = None,
    on_warning: Callable[[str], object] | None = None,
    timeout: float = DOWNLOAD_TIMEOUT_S,
) -> Path:
    """Download a YouTube video's audio as an MP3 with cover art.
    yt-dlp writes to a unique temp name in folder and prints the title before it
    downloads, so a title-named file that already exists stops the download early.
    Temp files are always removed, also on failure; stale ones that killed hosts
    left in folder are removed first.

    Args:
        url: The YouTube video URL.
        folder: An existing folder to save the MP3 in.
        filename: The filename without extension, already sanitized. An empty
            filename means: name the file after the video title (title_filename).
        on_progress: Optional callback receiving the download progress as 0.0-1.0.
            Called from the calling thread.
        on_start: Optional callback receiving the yt-dlp process and the temp stem
            once the download started, e.g. to cancel it with kill_process_tree.
        on_warning: Optional callback receiving a user-facing message about a
            problem that did not stop the download (no title, no thumbnail).
        timeout: Seconds the yt-dlp call may take in total.

    Returns:
        The path of the final MP3.

    Raises:
        FileExistsError: If the target MP3 already exists (`filename` holds its path).
        FileNotFoundError: If ffmpeg.exe or yt-dlp.exe is missing, or yt-dlp or
            ffmpeg did not produce its MP3.
        subprocess.TimeoutExpired: If yt-dlp or ffmpeg timed out.
        subprocess.CalledProcessError: If yt-dlp or ffmpeg failed.
    """
    def _warn(message: str) -> None:
        logger.warning(message)
        if on_warning:
            on_warning(message)

    def _check_title(inf: dict[str, object]) -> None:
        if not filename:
            _ensure_not_exists(folder / f"{title_filename(inf)}.mp3")

    if filename:
        _ensure_not_exists(folder / f"{filename}.mp3")
    if not FFMPEG_PATH.exists():
        raise FileNotFoundError(errno.ENOENT, "ffmpeg.exe not found, please reinstall the app", str(FFMPEG_PATH))
    if not YT_DLP_PATH.exists():
        raise FileNotFoundError(errno.ENOENT, "yt-dlp.exe not found, please reinstall the app", str(YT_DLP_PATH))

    remove_stale_temp_files(folder)
    temp_stem = folder / f"_ytmp3_{uuid.uuid4().hex[:12]}"
    start = time.perf_counter()
    try:
        info = run_yt_dlp(url, temp_stem, on_progress, on_start, _check_title, timeout)
        downloaded = time.perf_counter()
        logger.debug("yt-dlp took %.2f s, info: %r", downloaded - start, info)

        if not filename:
            if not info_text(info, "title"):
                _warn(f"Could not fetch the video title, using '{DEFAULT_FILENAME}'")
            filename = title_filename(info)
        output = folder / f"{filename}.mp3"
        _ensure_not_exists(output)  # again: it may have appeared during the download

        temp_mp3 = temp_stem.with_name(f"{temp_stem.name}.mp3")
        if not temp_mp3.exists():
            raise FileNotFoundError(errno.ENOENT, "yt-dlp did not create the MP3", str(temp_mp3))

        thumb_file = find_thumbnail(temp_stem)
        if thumb_file is None:
            _warn("No thumbnail found, the MP3 is saved without cover art")
        embed_thumbnail(
            temp_mp3, thumb_file, artist_for(filename, info), title_from_filename(filename), output, extra_tags(info)
        )
        if not output.exists():  # cheap guard: never report success for a file that is not there
            raise FileNotFoundError(errno.ENOENT, "ffmpeg did not create the MP3", str(output))
        end = time.perf_counter()
        logger.debug("ffmpeg took %.2f s, total %.2f s: %s", end - downloaded, end - start, output)
        return output
    except subprocess.CalledProcessError as e:
        logger.error("Error while downloading: %s\n%s", e, e.output or "")
        raise
    except Exception as e:
        logger.error("Download error: %s", e)
        raise
    finally:
        remove_temp_files(temp_stem)


def _yt_dlp_failed(error: Exception) -> bool:
    """Whether download_audio failed because of yt-dlp itself (an error exit or a missing exe)."""
    match error:
        case subprocess.CalledProcessError():  # yt-dlp's or ffmpeg's
            return not (isinstance(error.cmd, list) and error.cmd and error.cmd[0] == str(FFMPEG_PATH))
        case FileNotFoundError():
            return error.filename == str(YT_DLP_PATH)
        case _:
            return False


def _file_stamp(path: Path) -> tuple[int, int] | None:
    """Size and modification time of path, to tell whether an update replaced it; None if missing."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _repair_yt_dlp(url: str, error: Exception, on_info: Callable[[str], object]) -> bool:
    """Update yt-dlp, and install Deno if YouTube asked for JavaScript, after a failed download.
    Deno is only for YouTube URLs: other sites also say "Requested format is not available".

    Returns:
        True if anything changed, so trying again can help. Repairs that fail are
        logged; the caller reports the original error.
    """
    before = _file_stamp(YT_DLP_PATH)
    on_info("The download failed, checking for a yt-dlp update...")
    # OSError: e.g. the update lock or a file a virus scanner holds; never hides the download's own error
    try:
        updater.update_yt_dlp()
    except (updater.UpdateError, OSError) as e:
        logger.error("Repair after a failed download: %s", e)
    repaired = _file_stamp(YT_DLP_PATH) not in (None, before)

    output = str(getattr(error, "output", None) or "")
    js_challenge = is_youtube_url(url) and any(sign in output for sign in JS_CHALLENGE_FAILURES)
    if js_challenge and health.find_js_runtime() is None:
        on_info("YouTube needs an extra component for this video (Deno, about 45 MB). "
                "Downloading it once, this can take a minute...")
        try:
            updater.install_deno()
            repaired = True
        except (updater.UpdateError, OSError) as e:
            logger.error("Repair after a failed download: %s", e)
    return repaired


def download_with_repair(
    url: str,
    folder: Path,
    filename: str = "",
    on_progress: Callable[[float], object] | None = None,
    on_start: Callable[[subprocess.Popen[str], Path], None] | None = None,
    on_warning: Callable[[str], object] | None = None,
    on_info: Callable[[str], object] | None = None,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> Path:
    """download_audio, tried once more after a yt-dlp failure that a repair may fix.

    YouTube changes break older yt-dlp versions, so when yt-dlp fails (or is
    missing) it is updated, and the download is retried only if a new yt-dlp was
    installed. If yt-dlp's output says YouTube wants a JavaScript challenge solved
    and there is no runtime, Deno is installed too. Other failures (an existing
    file, ffmpeg, timeouts) are raised right away, as is a failure while offline.

    Args:
        url: The URL of the video to download.
        folder: The folder to save the downloaded file in.
        filename: The name of the file to save the download as.
        on_progress: Optional callback receiving the download progress, for the user.
        on_start: Optional callback receiving the process and the file path when the download starts.
        on_warning: Optional callback receiving a warning message, for the user.
        on_info: Optional callback receiving what the repair is doing, for the user.
        is_cancelled: Tells whether the user canceled; a canceled download is not repaired.
    Returns:
        The path of the final MP3.
    Raises:
        As download_audio: the error of the first attempt, or of the retry.
    """
    def _info(message: str) -> None:
        logger.info(message)
        if on_info:
            on_info(message)

    try:
        return download_audio(url, folder, filename, on_progress, on_start, on_warning)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        if is_cancelled() or not _yt_dlp_failed(e) or not _repair_yt_dlp(url, e, _info) or is_cancelled():
            raise
    _info("Trying the download again...")
    return download_audio(url, folder, filename, on_progress, on_start, on_warning)


def describe_error(error: Exception) -> str:
    """Turn a download_audio exception into a short message for the user.
    Used by both the headless and the GUI path, so they report errors the same way.

    Args:
        error: The exception raised by download_audio.
    Returns:
        A one-line message without the raw command line.
    """
    def _tool(cmd: object) -> str:
        return Path(str(cmd[0])).name if isinstance(cmd, list) and cmd else "unknown tool"

    match error:
        case FileExistsError():
            return f"File with this name already exists: {Path(error.filename or '').name}"
        case subprocess.TimeoutExpired():
            return f"Download timed out after {error.timeout:.0f} s ({_tool(error.cmd)})"
        case subprocess.CalledProcessError():
            tool = _tool(error.cmd)
            # ffmpeg reports on stderr; yt-dlp's stderr is merged into output
            details = error.stderr or error.output
            lines = [line.strip() for line in str(details or "").splitlines() if line.strip()]
            errors = [line.removeprefix("ERROR:").strip() for line in lines if line.startswith("ERROR:")]
            reason = (errors or lines or [f"exit code {error.returncode}"])[-1]
            return f"{tool} failed: {reason[:300]}"
        case OSError() if error.strerror:
            return f"{error.strerror}: {error.filename}" if error.filename else error.strerror
        case _:
            return f"Unexpected error: {error}"
