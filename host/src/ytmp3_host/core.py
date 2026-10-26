"""Download pipeline and helpers shared by the headless and GUI download paths.

One download is one yt-dlp call (audio as MP3 V0 plus the raw thumbnail) followed by
one ffmpeg pass that adds the cover art and tags and writes the final MP3.
"""

import ctypes
import errno
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from ytmp3_host.config import DEFAULT_FOLDER, DENO_PATH, FFMPEG_PATH, YT_DLP_PATH
# only download_with_repair's failure path (and open_file) uses them (urllib, and health imports this module)
lazy from ytmp3_host import health, updater

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT_S = 300
EMBED_TIMEOUT_S = 50
REMOVE_ATTEMPTS = 6
REMOVE_RETRY_DELAY_S = 0.5
# temp files older than this belong to a host that was killed (e.g. Firefox closed mid-download)
STALE_TEMP_AGE_S = 3600
INFO_MARKER = "YTMP3_INFO:"
# the video info yt-dlp prints as JSON before downloading: the title names the file, the rest
# become tags. artists/album/release_year/track_number exist only for YouTube Music uploads
# and music sites (SoundCloud, Bandcamp, ...); other sites have an uploader instead of a channel.
INFO_FIELDS = (
    "title", "artists", "album", "release_year", "track_number", "channel", "uploader", "webpage_url", "extractor_key"
)
# sites whose `artists` list the tracks of a mix, not who made it
TRACKLIST_EXTRACTORS = frozenset({"Mixcloud"})
TOPIC_SUFFIX = " - Topic"  # YouTube Music's auto-generated artist channels, e.g. "Queen - Topic"
THUMBNAIL_EXTS = ("webp", "jpg", "jpeg", "png")
DEFAULT_FILENAME = "Youtube MP3"
UNKNOWN_ARTIST = "<unknown>"
YOUTUBE_HOSTS = ("youtube.com", "youtu.be/")
# what yt-dlp prints when YouTube asks for a JavaScript challenge it could not solve without a runtime
JS_CHALLENGE_FAILURES = ("Signature solving failed", "n challenge solving failed", "Requested format is not available")
NOISE_WORDS = ("Official", "Music", "Video", "HD", "Lyrics", "Lyric", "Audio", "Explicit", "Clean", "Live", "4K")

WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + [f"{port}{i}" for port in ("COM", "LPT") for i in range(1, 10)]
)

PROGRESS_RE = re.compile(r"(\d+\.?\d*)%")
# control characters \x1c-\x1f count as whitespace and are collapsed by WHITESPACE_RE
INVALID_CHARS_RE = re.compile(r'[\\/*?:"<>|\x00-\x08\x0e-\x1b]')
_NOISE_WORD = "(?:" + "|".join(map(re.escape, NOISE_WORDS)) + ")"
# a bracket group made only of noise words, e.g. "(Official Music Video)" or "[HD]";
# noise words outside brackets are part of the title ("Live and Let Die")
NOISE_GROUP_RE = re.compile(
    rf"[(\[]\s*{_NOISE_WORD}(?:[\s,&/+-]+{_NOISE_WORD})*\s*[)\]]", flags=re.IGNORECASE
)
EMPTY_BRACKETS_RE = re.compile(r"[(\[]\s*[)\]]")
WHITESPACE_RE = re.compile(r"\s+")
ARTIST_SEPARATOR_RE = re.compile(r"\s+[-–—]\s+")


def is_web_url(url: object) -> bool:
    """Check that a URL can be handed to yt-dlp: http(s) with a host.
    yt-dlp supports many sites besides YouTube and reports the ones it can't download.
    The scheme check also keeps the URL from being read as a yt-dlp option ("-...").

    Args:
        url: The URL to check, from a request (so possibly not a string).
    Returns:
        True if the URL is an http or https URL with a host name.
    """
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:  # e.g. a broken IPv6 host
        return False


def is_youtube_url(url: str) -> bool:
    """Loosely check that a URL points to YouTube, for YouTube-only repairs (Deno).

    Args:
        url: The URL to check.
    Returns:
        True if the URL contains a YouTube host name.
    """
    return any(host in url for host in YOUTUBE_HOSTS)


def safe_filename(name: object) -> str:
    """Turn a video title or user input into a valid Windows filename.
    Removes characters Windows forbids in filenames, bracketed YouTube title noise
    ("(Official Video)", "[Lyrics]", ...), empty brackets and repeated whitespace.
    A name Windows reserves for a device (CON, NUL, COM1, ...) gets a "_" prefix.

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
    name = NOISE_GROUP_RE.sub("", name)
    name = EMPTY_BRACKETS_RE.sub("", name)
    name = WHITESPACE_RE.sub(" ", name).strip()
    # Windows ignores everything after the first dot (and trailing spaces) here
    if name.split(".", 1)[0].rstrip().upper() in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
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
        folder is empty or names the default folder, otherwise None. A relative path
        is treated as None.
    """
    folder = folder.strip()
    if not folder:
        return ensure_default_folder()
    folder_path = Path(folder).expanduser()
    if not folder_path.is_absolute():
        return None
    if folder_path.is_dir():
        return folder_path.resolve()
    if folder_path == Path(DEFAULT_FOLDER).expanduser():
        return ensure_default_folder()
    return None


def folder_is_writable(folder: Path) -> bool:
    """Whether a file can be created in folder.

    An existing folder can still refuse it: protected folders like C:\\ or Program Files,
    Windows Security's Controlled folder access, read-only drives. yt-dlp would then fail
    with "Permission denied", which looks like a broken yt-dlp. The probe is opened with
    O_TEMPORARY (FILE_FLAG_DELETE_ON_CLOSE), so Windows deletes it when the handle closes,
    even when the host is killed first. That also needs delete access, which a download
    needs too (yt-dlp renames its .part file, and the temp files are removed).

    Args:
        folder: An existing folder.
    Returns:
        True if a file could be created in folder.
    """
    probe = folder / f"_ytmp3_{uuid.uuid4().hex[:12]}.probe"
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_TEMPORARY)
    except OSError as e:
        logger.warning("Can't write to %s: %s", folder, e)
        return False
    os.close(fd)
    return True


def artist_from_filename(filename: str) -> str:
    """Derive the artist metadata from a filename.

    Args:
        filename: The final filename without extension, e.g. "Artist - Song".
    Returns:
        The text before the first spaced dash (" - ", " – ", " — "), so "Jay-Z - Song"
        gives "Jay-Z"; without one the text before the first "-"; UNKNOWN_ARTIST if
        there is no dash at all.
    """
    if match := ARTIST_SEPARATOR_RE.search(filename):
        artist = filename[:match.start()].strip()
    else:
        artist = filename.split("-", 1)[0].strip() if "-" in filename else ""
    return artist or UNKNOWN_ARTIST


def title_from_filename(filename: str) -> str:
    """Derive the title metadata from a filename, the counterpart of artist_from_filename.

    Args:
        filename: The final filename without extension, e.g. "Artist - Song".
    Returns:
        The text after the dash that artist_from_filename splits at, or the whole
        filename if there is no dash (or nothing after it).
    """
    if match := ARTIST_SEPARATOR_RE.search(filename):
        title = filename[match.end():].strip()
    else:
        title = filename.split("-", 1)[1].strip() if "-" in filename else ""
    return title or filename


def parse_info(text: str) -> dict[str, object]:
    """Parse the video info yt-dlp printed after INFO_MARKER.

    Args:
        text: The JSON object of INFO_FIELDS; yt-dlp leaves out the fields a video lacks.
    Returns:
        The info, or {} if it is not a JSON object (logged).
    """
    try:
        info = json.loads(text)
    except ValueError:
        logger.warning("Could not parse the video info: %r", text)
        return {}
    return info if isinstance(info, dict) else {}


def info_text(info: dict[str, object], key: str) -> str:
    """A video info field as stripped text, "" if it is missing or not a string or number.

    Args:
        info: The video info (see INFO_FIELDS).
        key: The field name.
    Returns:
        The text; a list, dict or null (yt-dlp's JSON for odd videos) gives "".
    """
    value = info.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


def info_artists(info: dict[str, object]) -> str:
    """The artists the site names for the track e.g. "ACDC"; "" if none.
    Mixcloud's `artists` are the artists of the tracks in a mix, so they are left out.

    Args:
        info: The video info (see INFO_FIELDS).
    Returns:
        The artists joined with ", ".
    """
    artists = info.get("artists")
    if info_text(info, "extractor_key") in TRACKLIST_EXTRACTORS or not isinstance(artists, list):
        return ""
    return ", ".join(a.strip() for a in artists if isinstance(a, str) and a.strip())


def title_filename(info: dict[str, object]) -> str:
    """The filename of a download named after its title.
    A title that names no artist (no spaced dash, as on YouTube Music and SoundCloud)
    gets the site's artists in front, so the file is "Artist - Title" like most titles.

    Args:
        info: The video info (see INFO_FIELDS).
    Returns:
        The sanitized filename without extension; DEFAULT_FILENAME without a title.
    """
    title = info_text(info, "title")
    name = safe_filename(title)
    artists = info_artists(info)
    if title and artists and not ARTIST_SEPARATOR_RE.search(name):
        return safe_filename(f"{artists} - {title}")
    return name


def artist_for(filename: str, info: dict[str, object]) -> str:
    """The artist tag: from the filename, else from the video info.

    Args:
        filename: The final filename without extension.
        info: The video info (see INFO_FIELDS).
    Returns:
        artist_from_filename's artist if the filename has one, else the video's
        artists (info_artists), else its channel without " - Topic", else its uploader,
        else UNKNOWN_ARTIST.
    """
    artist = artist_from_filename(filename)
    if artist != UNKNOWN_ARTIST:
        return artist
    channel = info_text(info, "channel").removesuffix(TOPIC_SUFFIX).strip()
    return info_artists(info) or channel or info_text(info, "uploader") or UNKNOWN_ARTIST


def extra_tags(info: dict[str, object]) -> dict[str, str]:
    """The tags besides artist and title, as ffmpeg metadata keys (ID3v2.3 frame in the comment).
    The year is the release year only: the upload year of an official video is
    often decades after the song.

    Args:
        info: The video info (see INFO_FIELDS).
    Returns:
        The tags the video has values for.
    """
    tags = {
        "album": info_text(info, "album"),  # TALB
        "date": info_text(info, "release_year"),  # TYER
        "track": info_text(info, "track_number"),  # TRCK
        "comment": info_text(info, "webpage_url"),  # COMM: where the song came from
    }
    return {key: value for key, value in tags.items() if value}


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
        logger.error("taskkill failed, killing only the main process: %s", e)
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
                    logger.error("Could not delete locked file %s: %s", f, e)
                else:
                    time.sleep(REMOVE_RETRY_DELAY_S)
            except OSError as e:
                logger.error("Error deleting file %s: %s", f, e)
                break


def remove_stale_temp_files(folder: Path) -> None:
    """Remove temp files that killed hosts left in folder.
    When Firefox closes during a download it kills the host, so download_audio's
    cleanup never runs. Files younger than STALE_TEMP_AGE_S may belong to a running
    download and are kept; locked files are skipped. Failures are logged, not raised.

    Args:
        folder: The download folder.
    """
    cutoff = time.time() - STALE_TEMP_AGE_S
    for f in folder.glob("_ytmp3_*.*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                logger.info("Removed stale temp file %s", f)
        except OSError as e:
            logger.warning("Could not remove stale temp file %s: %s", f, e)


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


def _run_yt_dlp(
    url: str,
    temp_stem: Path,
    on_progress: Callable[[float], object] | None,
    on_start: Callable[[subprocess.Popen[str], Path], None] | None,
    on_video_info: Callable[[dict[str, object]], None],
    timeout: float,
) -> dict[str, object]:
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

        info: dict[str, object] = {}
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

    if timed_out.is_set():
        raise subprocess.TimeoutExpired(process.args, timeout)
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, process.args, "".join(output_lines[-20:]))
    return info


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
        info = _run_yt_dlp(url, temp_stem, on_progress, on_start, _check_title, timeout)
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


def downloaded_mp3(path: str) -> Path | None:
    """The file a `show_in_folder` or `play` request names, if it is an existing MP3.
    Only MP3s: play opens the file in its default app, which must never run a program.
    Args:
        path: The absolute path the extension got from the download's success response.
    Returns:
        The file, or None if it is not an absolute path to an existing .mp3 file.
    """
    file = Path(path)
    return file if file.is_absolute() and file.suffix.lower() == ".mp3" and file.is_file() else None


def show_in_folder(file: Path) -> None:
    """Open the folder of file in Explorer with file selected (an open window of it is reused).
    Uses the shell API instead of `explorer.exe /select,<path>`, which splits the path
    at commas, and song titles often have them.

    Raises:
        OSError: If the shell could not open the folder.
    """
    shell32 = ctypes.windll.shell32
    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
    shell32.ILFree.argtypes = [ctypes.c_void_p]
    shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]
    ctypes.windll.ole32.CoInitialize(None)  # the shell API needs COM; a no-op if this thread has it
    pidl = shell32.ILCreateFromPathW(str(file))
    if not pidl:
        raise OSError(errno.ENOENT, "Could not find the file", str(file))
    try:
        result = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
    finally:
        shell32.ILFree(pidl)
    if result != 0:
        raise OSError(0, f"Explorer could not open the folder (0x{result & 0xFFFFFFFF:08X})", str(file.parent))


def open_file(file: Path) -> None:
    """Open file in its default app (play an MP3).
    Through explorer.exe, which hands it to the running shell: an app this host
    started itself would belong to Firefox's native-messaging job and be ended with
    the host. The path is quoted (Windows paths can't contain quotes).
    Raises:
        OSError: If explorer.exe could not be started.
    """
    updater.launch_detached(f'explorer.exe "{file}"')
