"""Download pipeline shared by the headless and GUI download paths.

One download is one yt-dlp call (the audio as MP3 V0, or video and audio merged into an
MP4, plus the raw thumbnail) followed by one ffmpeg pass that adds the cover art and tags
and writes the final file.
"""

import errno
import logging
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from ytmp3_host.config import DENO_PATH, FFMPEG_PATH, yt_dlp_path
from ytmp3_host.files import remove_stale_temp_files, remove_temp_files
from ytmp3_host.media_format import MediaFormat, get_suffix
from ytmp3_host.metadata import VideoInfo, artist_for, extra_tags, info_text, named_title, title_filename
from ytmp3_host.naming import DEFAULT_FILENAME, is_youtube_url, title_from_filename
from ytmp3_host.ytdlp import find_thumbnail, run_yt_dlp

# only download_with_repair's failure path uses these
lazy from ytmp3_host import health, updater

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT_S = 900
EMBED_TIMEOUT_S = 50
# what yt-dlp prints when YouTube asks for a JavaScript challenge it could not solve without a runtime
JS_CHALLENGE_FAILURES = ("Signature solving failed", "n challenge solving failed", "Requested format is not available")
# what Python prints when a file of yt-dlp's onedir build is missing or blocked (e.g. a virus scanner
# removed a DLL): `--version` does not load every module, so the build can still look
BROKEN_INSTALL_FAILURES = ("ImportError", "ModuleNotFoundError", "DLL load failed")


def embed_thumbnail(
    temp_file: Path,
    thumb_file: Path | None,
    artist: str,
    title: str,
    output: Path,
    tags: dict[str, str] | None = None,
) -> None:
    """Write the final MP3 or MP4 with cover art and tags in a single ffmpeg pass.

    The output's suffix decides the format, as it decides ffmpeg's container. The
    media streams are copied, never re-encoded; only the thumbnail is encoded, as a
    high-quality JPEG. MP3: the audio, with the thumbnail center-cropped to 1:1 as
    ID3v2.3 cover art. MP4: the video and audio, with the thumbnail uncropped (it
    has the video's shape) as an `attached_pic` stream, which players show as the
    cover instead of as a second video. Without a thumbnail only the tags are added.

    Args:
        temp_file: The MP3 or MP4 yt-dlp produced.
        thumb_file: The thumbnail to embed, or None.
        artist: The artist metadata value.
        title: The title metadata value.
        output: The final .mp3 or .mp4 path. Must not exist yet (ffmpeg runs with -n).
        tags: More metadata as ffmpeg keys (see extra_tags), e.g. {"album": "..."}.
    Raises:
        ValueError: If output is neither .mp3 nor .mp4.
        FileExistsError: If output already exists; it is left untouched.
        subprocess.CalledProcessError: If ffmpeg fails. A partial output is removed.
        subprocess.TimeoutExpired: If ffmpeg takes longer than EMBED_TIMEOUT_S.
    """
    match output.suffix:
        case ".mp3":
            is_mp3 = True
        case ".mp4":
            is_mp3 = False
        case _:
            raise ValueError(f"embed_thumbnail: output must be .mp3 or .mp4, not {output.suffix!r}")

    cmd = [str(FFMPEG_PATH), "-hide_banner", "-nostdin", "-loglevel", "error", "-n", "-i", str(temp_file)]
    if thumb_file:
        cmd += ["-i", str(thumb_file)]  # every input comes before the output options
    if is_mp3:
        if thumb_file:
            cmd += [
                "-map", "0:a",
                "-map", "1:0",
                "-c:v", "mjpeg", "-q:v", "2",
                "-vf", "crop='min(iw,ih)':'min(iw,ih)'",
                "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)",
            ]
        cmd += ["-c:a", "copy", "-id3v2_version", "3"]
    else:
        cmd += ["-map", "0", "-c", "copy"]
        if thumb_file:  # the cover is the second video stream (v:1); these override -c copy for it
            cmd += ["-map", "1:0", "-c:v:1", "mjpeg", "-q:v:1", "2", "-disposition:v:1", "attached_pic"]
    cmd += [
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
    *,
    media_format: MediaFormat,
    on_progress: Callable[[float], object] | None = None,
    on_start: Callable[[subprocess.Popen[str], Path], None] | None = None,
    on_warning: Callable[[str], object] | None = None,
    timeout: float = DOWNLOAD_TIMEOUT_S,
) -> Path:
    """Download a video or track as media_format (an MP3 or MP4) with cover art and tags.
    yt-dlp writes to a unique temp name in folder and prints the title before it
    downloads, so a title-named file that already exists stops the download early.
    Temp files are always removed, also on failure; stale ones that killed hosts
    left in folder are removed first.

    Args:
        url: The video or track URL.
        folder: An existing folder to save the file in.
        filename: The filename without extension, already sanitized. An empty
            filename means: name the file after the video title (title_filename).
        media_format: The requested media format (MP3 or MP4) and its options.
        on_progress: Optional callback receiving the download progress as 0.0-1.0.
            Called from the calling thread.
        on_start: Optional callback receiving the yt-dlp process and the temp stem
            once the download started, e.g. to cancel it with kill_process_tree.
        on_warning: Optional callback receiving a user-facing message about a
            problem that did not stop the download (no title, no thumbnail).
        timeout: Seconds the yt-dlp call may take in total.

    Returns:
        The path of the final file (.mp3 or .mp4, see get_suffix).

    Raises:
        FileExistsError: If the target file already exists (`filename` holds its path).
        FileNotFoundError: If ffmpeg.exe or yt-dlp.exe is missing, or yt-dlp or
            ffmpeg did not produce its file.
        subprocess.TimeoutExpired: If yt-dlp or ffmpeg timed out.
        subprocess.CalledProcessError: If yt-dlp or ffmpeg failed.
    """
    suffix = get_suffix(media_format)
    file_type = suffix.removeprefix(".").upper()  # MP3 or MP4, for the messages

    def _warn(message: str) -> None:
        logger.warning(message)
        if on_warning:
            on_warning(message)

    def _check_title(video_info: VideoInfo) -> None:
        if not filename:
            _ensure_not_exists(folder / f"{title_filename(video_info, folder)}{suffix}")

    if filename:
        _ensure_not_exists(folder / f"{filename}{suffix}")
    if not FFMPEG_PATH.exists():
        raise FileNotFoundError(errno.ENOENT, "ffmpeg.exe not found, please reinstall the app", str(FFMPEG_PATH))
    if not (yt_dlp := yt_dlp_path()).exists():
        raise FileNotFoundError(errno.ENOENT, "yt-dlp.exe not found, please reinstall the app", str(yt_dlp))

    remove_stale_temp_files(folder)
    temp_stem = folder / f"_ytmp3_{uuid.uuid4().hex[:12]}"
    start = time.perf_counter()
    try:
        info = run_yt_dlp(
            url, temp_stem, media_format, on_progress=on_progress, on_start=on_start, on_video_info=_check_title, timeout=timeout
        )
        downloaded = time.perf_counter()
        logger.debug("yt-dlp took %.2f s, info: %r", downloaded - start, info)

        tag_name = filename  # the tags can hold what a filename can't ("AC/DC")
        if not filename:
            if not info_text(info, "title"):
                _warn(f"Could not fetch the video title, using '{DEFAULT_FILENAME}'")
            filename = title_filename(info, folder)
            tag_name = named_title(info) or filename
        output = folder / f"{filename}{suffix}"
        _ensure_not_exists(output)  # again: it may have appeared during the download

        temp_file = temp_stem.with_name(f"{temp_stem.name}{suffix}")
        if not temp_file.exists():
            raise FileNotFoundError(errno.ENOENT, f"yt-dlp did not create the {file_type}", str(temp_file))

        thumb_file = find_thumbnail(temp_stem)
        if thumb_file is None:
            _warn(f"No thumbnail found, the {file_type} is saved without cover art")
        embed_thumbnail(
            temp_file, thumb_file, artist_for(tag_name, info), title_from_filename(tag_name), output, extra_tags(info)
        )
        if not output.exists():  # cheap guard: never report success for a file that is not there
            raise FileNotFoundError(errno.ENOENT, f"ffmpeg did not create the {file_type}", str(output))
        end = time.perf_counter()
        logger.debug("ffmpeg took %.2f s, total %.2f s: %s", end - downloaded, end - start, output)
        return output
    except subprocess.CalledProcessError as e:
        logger.error("Error while downloading: %s\n%s", e, e.output or "")
        raise
    except FileExistsError as e:  # not an actual error
        logger.info("Not downloaded, the file exists: %s", e.filename)
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
            return error.filename == str(yt_dlp_path())
        case _:
            return False


def _yt_dlp_stamp() -> tuple[Path, int, int] | None:
    """The yt-dlp the host runs, with its size and modification time, to tell whether an
    update replaced it (in place, or by one at another path); None if it is missing."""
    exe = yt_dlp_path()
    try:
        stat = exe.stat()
    except OSError:
        return None
    return exe, stat.st_size, stat.st_mtime_ns


def _deno_stamp() -> tuple[int, int] | None:
    """The size and modification time of the host's deno.exe, to tell whether an install
    or update replaced it; None if it is missing."""
    try:
        stat = DENO_PATH.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _repair_yt_dlp(url: str, error: Exception, on_info: Callable[[str], object]) -> bool:
    """Update yt-dlp, and install or update Deno if YouTube asked for JavaScript, after a failed download.
    Deno is only for YouTube URLs: other sites also say "Requested format is not available".
    Only the host's own deno.exe is installed or updated; a Deno on PATH is the user's.
    When the output shows a damaged install (BROKEN_INSTALL_FAILURES), the update switches
    a current onedir build to the single-file yt-dlp.exe for this release.

    Returns:
        True if anything changed, so trying again can help. Repairs that fail are
        logged; the caller reports the original error.
    """
    before = _yt_dlp_stamp()
    output = str(getattr(error, "output", None) or "")
    damaged = any(sign in output for sign in BROKEN_INSTALL_FAILURES)
    on_info("yt-dlp is damaged, repairing it..." if damaged
            else "The download failed, checking for a yt-dlp update...")
    # OSError: e.g. the update lock or a file a virus scanner holds; never hides the download's own error
    try:
        updater.update_yt_dlp(damaged=damaged)
    except (updater.UpdateError, OSError) as e:
        logger.error("Repair after a failed download: %s", e)
    repaired = _yt_dlp_stamp() not in (None, before)

    js_challenge = is_youtube_url(url) and any(sign in output for sign in JS_CHALLENGE_FAILURES)
    if js_challenge and (runtime := health.find_js_runtime()) in (None, DENO_PATH):
        on_info("YouTube still needs an extra component for this video (Deno), checking for an update..." if runtime
                else "YouTube needs an extra component for this video (Deno, about 45 MB). "
                     "Downloading it, this can take a minute...")
        before_deno = _deno_stamp()
        try:
            updater.install_and_update_deno()
        except (updater.UpdateError, OSError) as e:
            logger.error("Repair after a failed download: %s", e)
        repaired = repaired or _deno_stamp() not in (None, before_deno)
    return repaired


def download_with_repair(
    url: str,
    folder: Path,
    filename: str = "",
    *,
    media_format: MediaFormat,
    on_progress: Callable[[float], object] | None = None,
    on_start: Callable[[subprocess.Popen[str], Path], None] | None = None,
    on_warning: Callable[[str], object] | None = None,
    on_info: Callable[[str], object] | None = None,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> Path:
    """download_audio, tried once more after a yt-dlp failure that a repair may fix.

    YouTube changes break older yt-dlp versions, so when yt-dlp fails (or is
    missing) it is updated, and the download is retried only if a new yt-dlp was
    installed; a damaged onedir build is replaced by the single-file exe. If
    yt-dlp's output says YouTube wants a JavaScript challenge solved, the host's
    Deno is installed, or updated when it is there but outdated (a Deno on PATH
    is left alone). Other failures (an existing
    file, ffmpeg, timeouts) are raised right away, as is a failure while offline.

    Args:
        url: The URL of the video to download.
        folder: The folder to save the downloaded file in.
        filename: The name of the file to save the download as.
        media_format: The requested media format (MP3 or MP4) and its options.
        on_progress: Optional callback receiving the download progress, for the user.
        on_start: Optional callback receiving the process and the file path when the download starts.
        on_warning: Optional callback receiving a warning message, for the user.
        on_info: Optional callback receiving what the repair is doing, for the user.
        is_cancelled: Tells whether the user canceled; a canceled download is not repaired.
    Returns:
        The path of the final file.
    Raises:
        As download_audio: the error of the first attempt, or of the retry.
    """
    def _info(message: str) -> None:
        logger.info(message)
        if on_info:
            on_info(message)

    def _download() -> Path:
        return download_audio(url, folder, filename, media_format=media_format, on_progress=on_progress, on_start=on_start, on_warning=on_warning)

    try:
        return _download()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        if is_cancelled() or not _yt_dlp_failed(e) or not _repair_yt_dlp(url, e, _info) or is_cancelled():
            raise
    _info("Trying the download again...")
    return _download()


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
