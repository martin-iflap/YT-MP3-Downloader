"""Download pipeline and helpers shared by the headless and GUI download paths.

One download is one yt-dlp call (audio as MP3 V0 plus the raw thumbnail) followed by
one ffmpeg pass that adds the cover art and artist and writes the final MP3.
"""

import errno
import logging
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from ytmp3_host.config import DEFAULT_FOLDER, FFMPEG_PATH, YT_DLP_PATH

DOWNLOAD_TIMEOUT_S = 300
EMBED_TIMEOUT_S = 50
REMOVE_ATTEMPTS = 6
REMOVE_RETRY_DELAY_S = 0.5
TITLE_MARKER = "YTMP3_TITLE:"
THUMBNAIL_EXTS = ("webp", "jpg", "jpeg", "png")
DEFAULT_FILENAME = "Youtube MP3"
UNKNOWN_ARTIST = "<unknown>"
YOUTUBE_HOSTS = ("youtube.com", "youtu.be/")
NOISE_WORDS = ("Official", "Music", "Video", "HD", "Lyrics", "Lyric", "Audio", "Explicit", "Clean", "Live")

PROGRESS_RE = re.compile(r"(\d+\.?\d*)%")
INVALID_CHARS_RE = re.compile(r'[\\/*?:"<>|]')
NOISE_WORDS_RE = re.compile(r"\b(?:" + "|".join(map(re.escape, NOISE_WORDS)) + r")\b", flags=re.IGNORECASE)
EMPTY_BRACKETS_RE = re.compile(r"[(\[]\s*[)\]]")
WHITESPACE_RE = re.compile(r"\s+")


def is_youtube_url(url: str) -> bool:
    """Loosely check that a URL points to YouTube.
    The extension validates URLs strictly before sending them; this only rejects
    obvious garbage on the host side.

    Args:
        url: The URL to check.
    Returns:
        True if the URL contains a YouTube host name.
    """
    return any(host in url for host in YOUTUBE_HOSTS)


def safe_filename(name: object) -> str:
    """Turn a video title or user input into a valid Windows filename.
    Removes characters Windows forbids in filenames, common YouTube title noise
    ("Official Video", "Lyrics", ...), the empty brackets that removal leaves
    behind, and repeated whitespace.

    Args:
        name: The raw name. Anything that is not a non-empty string is treated as empty.
    Returns:
        The cleaned name, or DEFAULT_FILENAME if nothing meaningful is left.

    Example:
        >>> safe_filename('Artist - Song (Official Video) [HD]')
        'Artist - Song'
    """
    if not isinstance(name, str):
        return DEFAULT_FILENAME
    name = INVALID_CHARS_RE.sub("", name)
    name = NOISE_WORDS_RE.sub("", name)
    name = EMPTY_BRACKETS_RE.sub("", name)
    name = WHITESPACE_RE.sub(" ", name).strip()
    return name or DEFAULT_FILENAME


def requested_filename(name: str) -> str:
    """Sanitize a filename requested by the user.

    Args:
        name: The filename from the request or the GUI field, possibly empty.
    Returns:
        The sanitized filename, or "" if nothing meaningful was given, which tells
        download_audio to name the file after the video title.
    """
    filename = safe_filename(name)
    return "" if filename.lower() == DEFAULT_FILENAME.lower() else filename


def ensure_default_folder() -> Path:
    """Create the default download folder if needed.

    Returns:
        The default download folder, guaranteed to exist.
    """
    folder_path = Path(DEFAULT_FOLDER).expanduser()
    folder_path.mkdir(parents=True, exist_ok=True)
    return folder_path


def resolve_folder(folder: str) -> Path | None:
    """Resolve a requested download folder.

    Args:
        folder: The requested folder, possibly empty.
    Returns:
        The folder if it is an existing directory, the (created) default folder if
        folder is empty or names the default folder, otherwise None.
    """
    folder = folder.strip()
    if not folder:
        return ensure_default_folder()
    folder_path = Path(folder).expanduser()
    if folder_path.is_dir():
        return folder_path.resolve()
    if folder_path == Path(DEFAULT_FOLDER).expanduser():
        return ensure_default_folder()
    return None


def artist_from_filename(filename: str) -> str:
    """Derive the artist metadata from a filename.

    Args:
        filename: The final filename without extension, e.g. "Artist - Song".
    Returns:
        The text before the first "-", or UNKNOWN_ARTIST if there is none.
    """
    artist = filename.split("-", 1)[0].strip() if "-" in filename else ""
    return artist or UNKNOWN_ARTIST


def build_download_command(url: str, temp_stem: Path) -> list[str]:
    """Build the yt-dlp command for one download.

    A single call fetches the title (printed after TITLE_MARKER), the audio and
    the raw thumbnail. `--audio-quality 0` is LAME V0, the best VBR preset, which
    encodes as fast as the default V5. `--print` implies `--quiet`, so `--progress`
    keeps the progress lines the GUI parses.

    Args:
        url: The YouTube video URL.
        temp_stem: Output path without extension; yt-dlp appends the extensions.
    Returns:
        The command as an argument list for subprocess.
    """
    return [
        str(YT_DLP_PATH),
        "--no-playlist",
        "--concurrent-fragments", "3",
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "--write-thumbnail",
        "--ffmpeg-location", str(FFMPEG_PATH),
        "--print", f"after_move:{TITLE_MARKER}%(title)s",
        "--progress", "--newline",
        "--encoding", "utf-8",
        "-o", f"{temp_stem}.%(ext)s",
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
        )
    except (OSError, subprocess.CalledProcessError) as e:
        logging.error("taskkill failed, killing only the main process: %s", e)
        process.kill()


def remove_temp_files(temp_stem: Path) -> None:
    """Remove every file belonging to one download (temp MP3, thumbnail, partials).
    After a kill, Windows keeps the files locked until the yt-dlp child has fully
    exited, so locked files are retried a few times. Failures are logged, not raised.

    Args:
        temp_stem: The temp output path without extension.
    """
    for f in temp_stem.parent.glob(f"{temp_stem.name}.*"):
        for attempt in range(1, REMOVE_ATTEMPTS + 1):
            try:
                f.unlink(missing_ok=True)
                break
            except PermissionError as e:
                if attempt == REMOVE_ATTEMPTS:
                    logging.error("Could not delete locked file %s: %s", f, e)
                else:
                    time.sleep(REMOVE_RETRY_DELAY_S)
            except OSError as e:
                logging.error("Error deleting file %s: %s", f, e)
                break


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
            # yt-dlp's output is merged into stdout; ffmpeg reports on stderr
            details = error.output if tool == YT_DLP_PATH.name else error.stderr
            lines = [line.strip() for line in str(details or "").splitlines() if line.strip()]
            errors = [line.removeprefix("ERROR:").strip() for line in lines if line.startswith("ERROR:")]
            reason = (errors or lines or [f"exit code {error.returncode}"])[-1]
            return f"{tool} failed: {reason[:300]}"
        case OSError() if error.strerror:
            return f"{error.strerror}: {error.filename}" if error.filename else error.strerror
        case _:
            return f"Unexpected error: {error}"


def embed_thumbnail(temp_mp3: Path, thumb_file: Path | None, artist: str, output: Path) -> None:
    """Write the final MP3 with cover art and artist in a single ffmpeg pass.

    The audio stream is copied, never re-encoded. The thumbnail is center-cropped
    to 1:1 and encoded as high-quality JPEG ID3v2.3 cover art. Without a thumbnail
    only the artist metadata is added.

    Args:
        temp_mp3: The MP3 yt-dlp produced.
        thumb_file: The thumbnail to embed, or None.
        artist: The artist metadata value.
        output: The final MP3 path. Must not exist yet (ffmpeg runs with -n).
    Raises:
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
        str(output),
    ]
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
        logging.error("Error with thumbnail embedding: %s\n%s", e, e.stderr or "")
        output.unlink(missing_ok=True)
        raise


def _run_yt_dlp(
    url: str,
    temp_stem: Path,
    on_progress: Callable[[float], object] | None,
    on_start: Callable[[subprocess.Popen[str], Path], None] | None,
    timeout: float,
) -> str:
    """Run the yt-dlp download and return the video title.

    Args:
        url: The YouTube video URL.
        temp_stem: Output path without extension.
        on_progress: Optional callback receiving the progress as 0.0-1.0.
        on_start: Optional callback receiving the process and temp stem once started.
        timeout: Seconds after which the whole process tree is killed.

    Returns:
        The video title printed by yt-dlp, or "" if it printed none.

    Raises:
        subprocess.TimeoutExpired: If the download exceeded timeout.
        subprocess.CalledProcessError: If yt-dlp exited with an error. `output`
            holds the last lines of its output.
    """
    with subprocess.Popen(
        build_download_command(url, temp_stem),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    ) as process:
        timed_out = threading.Event()

        def _kill_on_timeout() -> None:
            timed_out.set()
            kill_process_tree(process)

        watchdog = threading.Timer(timeout, _kill_on_timeout)
        watchdog.start()

        title = ""
        output_lines: list[str] = []
        try:
            if on_start:
                on_start(process, temp_stem)
            assert process.stdout is not None  # stdout=PIPE
            for line in process.stdout:
                if line.startswith(TITLE_MARKER):
                    title = line[len(TITLE_MARKER):].strip()
                    continue
                output_lines.append(line)
                if on_progress:
                    match = PROGRESS_RE.search(line)
                    if match:
                        on_progress(float(match.group(1)) / 100.0)
            process.wait()
        finally:
            watchdog.cancel()
            # a raising callback must not leave yt-dlp running (Popen.__exit__ would wait for it)
            kill_process_tree(process)

    if timed_out.is_set():
        raise subprocess.TimeoutExpired(process.args, timeout)
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, process.args, "".join(output_lines[-20:]))
    return title


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
    yt-dlp writes to a unique temp name in folder, so the title is only needed
    after the download. Temp files are always removed, also on failure.

    Args:
        url: The YouTube video URL.
        folder: An existing folder to save the MP3 in.
        filename: The filename without extension, already sanitized. An empty
            filename means: name the file after the video title.
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
        logging.warning(message)
        if on_warning:
            on_warning(message)

    if filename:
        _ensure_not_exists(folder / f"{filename}.mp3")
    if not FFMPEG_PATH.exists():
        raise FileNotFoundError(errno.ENOENT, "ffmpeg.exe not found, please reinstall the app", str(FFMPEG_PATH))
    if not YT_DLP_PATH.exists():
        raise FileNotFoundError(errno.ENOENT, "yt-dlp.exe not found, please reinstall the app", str(YT_DLP_PATH))

    temp_stem = folder / f"_ytmp3_{uuid.uuid4().hex[:12]}"
    try:
        title = _run_yt_dlp(url, temp_stem, on_progress, on_start, timeout)

        if not filename:
            if not title:
                _warn(f"Could not fetch the video title, using '{DEFAULT_FILENAME}'")
            filename = safe_filename(title)
        output = folder / f"{filename}.mp3"
        _ensure_not_exists(output)

        temp_mp3 = temp_stem.with_name(f"{temp_stem.name}.mp3")
        if not temp_mp3.exists():
            raise FileNotFoundError(errno.ENOENT, "yt-dlp did not create the MP3", str(temp_mp3))

        thumb_file = find_thumbnail(temp_stem)
        if thumb_file is None:
            _warn("No thumbnail found, the MP3 is saved without cover art")
        embed_thumbnail(temp_mp3, thumb_file, artist_from_filename(filename), output)
        if not output.exists():  # cheap guard: never report success for a file that is not there
            raise FileNotFoundError(errno.ENOENT, "ffmpeg did not create the MP3", str(output))
        return output
    except subprocess.CalledProcessError as e:
        logging.error("Error while downloading: %s\n%s", e, e.output or "")
        raise
    except Exception as e:
        logging.error("Download error: %s", e)
        raise
    finally:
        remove_temp_files(temp_stem)
