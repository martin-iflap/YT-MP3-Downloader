from ytmp3_host.config import FFMPEG_PATH, YT_DLP_PATH, DEFAULT_FOLDER
from collections.abc import Callable
from pathlib import Path
import subprocess
import threading
import logging
import errno
import uuid
import re

DOWNLOAD_TIMEOUT_S = 300
TITLE_MARKER = "YTMP3_TITLE:"
THUMBNAIL_EXTS = ("webp", "jpg", "jpeg", "png")
PROGRESS_RE = re.compile(r'(\d+\.?\d*)%')

def safe_filename(name: object) -> str:
    """Sanitize the filename by removing invalid characters
     - also remove common extraneous words from YouTube titles
     - remove empty parentheses/brackets and extra spaces
        - return "YouTube MP3" if the result is empty or input invalid
    """
    if not isinstance(name, str) or not name.strip():
        return "Youtube MP3"
    name = re.sub(r'[\\/*?:"<>|]', '', name.strip())

    words = ["Official", "Music", "Video", "HD", "Lyrics", "Lyric", "Audio", "Explicit", "Clean", "Live"]
    pattern = r'\b(?:' + '|'.join(map(re.escape, words)) + r')\b'
    name = re.sub(pattern, "", name, flags=re.IGNORECASE)

    name = re.sub(r'[(\[]\s*[)\]]', '', name)

    name = re.sub(r'\s+', ' ', name).strip()

    return name if name else "Youtube MP3"

def ensure_default_folder() -> Path:
    """Create and return the default download folder if it does not exist."""
    folder_path = Path(DEFAULT_FOLDER).expanduser()
    folder_path.mkdir(parents=True, exist_ok=True)
    return folder_path

def artist_from_filename(filename: str) -> str:
    """Artist metadata is the text before the first '-' in the filename, else <unknown>"""
    if "-" in filename:
        return filename.split("-")[0].strip()
    return "<unknown>"

def build_download_command(url: str, temp_stem: Path) -> list[str]:
    """yt-dlp arguments shared by the headless and GUI download paths
     - one call fetches the title (printed after TITLE_MARKER), the audio and the raw thumbnail
     - --audio-quality 0 is LAME V0, the best VBR preset; it encodes as fast as the default V5
     - --print implies --quiet, so --progress keeps the progress lines for the GUI
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
        url
    ]

def find_thumbnail(temp_stem: Path) -> Path | None:
    """Return the thumbnail yt-dlp wrote next to the temp MP3 (usually .webp)"""
    for ext in THUMBNAIL_EXTS:
        thumb = temp_stem.with_name(f"{temp_stem.name}.{ext}")
        if thumb.exists():
            return thumb
    return None

def kill_process_tree(process: subprocess.Popen[str]) -> None:
    """Kill yt-dlp together with its children
     - yt-dlp.exe is a PyInstaller onefile build: process.kill() only stops the bootloader,
       the real yt-dlp child (and its ffmpeg) keep running and keep the stdout pipe open
    """
    if process.poll() is not None:
        return
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        logging.error(f"taskkill failed, killing only the main process: {e}")
        process.kill()

def remove_temp_files(temp_stem: Path) -> None:
    """Best-effort removal of every file belonging to one download (temp MP3, thumbnail, partials)"""
    for f in temp_stem.parent.glob(f"{temp_stem.name}.*"):
        try:
            f.unlink()
        except OSError as e:
            logging.error(f"Error deleting file {f}: {e}")

def embed_thumbnail(temp_mp3: Path, thumb_file: Path | None, artist: str, output: Path) -> None:
    """Write the final MP3 in a single ffmpeg pass
     - the audio stream is copied, never re-encoded
     - the thumbnail is center-cropped 1:1 and encoded as high quality JPEG ID3v2.3 cover art
     - without a thumbnail only the artist metadata is added
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
    else:
        logging.warning("⚠️ Thumbnail Not Found")
    cmd += [
        "-c:a", "copy",
        "-id3v2_version", "3",
        "-metadata", f"artist={artist}",
        str(output)
    ]
    try:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True, check=True)
    except subprocess.CalledProcessError as e:
        logging.error(f"❌ Error with thumbnail embedding: {e.stderr}")
        if output.exists():
            output.unlink()
        raise

def download_audio(url: str, folder: Path, filename: str = "",
                   on_progress: Callable[[float], object] | None = None,
                   on_start: Callable[[subprocess.Popen[str], Path], None] | None = None,
                   timeout: float = DOWNLOAD_TIMEOUT_S) -> Path:
    """Download a YouTube video's audio as MP3 and return the final path
     - an empty filename means: name the file after the video title
     - yt-dlp writes to a unique temp name, so the title is only needed after the download
     - on_progress gets 0.0-1.0, on_start gets the yt-dlp process and temp stem (for cancel/cleanup)
     - raises FileExistsError (with .filename) if the target MP3 already exists
    """
    output = folder / f"{filename}.mp3"
    if filename:
        if output.exists():
            raise FileExistsError(errno.EEXIST, "File with this name already exists", str(output))

    if not FFMPEG_PATH.exists():
        raise FileNotFoundError(errno.ENOENT, "ffmpeg.exe not found", str(FFMPEG_PATH))

    temp_stem = folder / f"_ytmp3_{uuid.uuid4().hex[:12]}"
    try:
        process = subprocess.Popen(build_download_command(url, temp_stem),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   encoding="utf-8", errors="replace", bufsize=1)
        if on_start:
            on_start(process, temp_stem)

        timed_out = threading.Event()
        def _kill_on_timeout():
            timed_out.set()
            kill_process_tree(process)
        watchdog = threading.Timer(timeout, _kill_on_timeout)
        watchdog.start()

        title = ""
        output_lines: list[str] = []
        assert process.stdout is not None  # stdout=PIPE
        try:
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

        if timed_out.is_set():
            raise subprocess.TimeoutExpired(process.args, timeout)
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, process.args, "".join(output_lines[-20:]))

        if not filename:
            filename = safe_filename(title)
            output = folder / f"{filename}.mp3"
            if output.exists():
                raise FileExistsError(errno.EEXIST, "File with this name already exists", str(output))

        temp_mp3 = temp_stem.with_name(f"{temp_stem.name}.mp3")
        if not temp_mp3.exists():
            raise FileNotFoundError(errno.ENOENT, "yt-dlp did not create the MP3", str(temp_mp3))

        embed_thumbnail(temp_mp3, find_thumbnail(temp_stem), artist_from_filename(filename), output)
        return output
    except subprocess.CalledProcessError as e:
        logging.error(f"❌ Error while downloading: {e}\n{e.output or ''}")
        raise
    except Exception as e:
        logging.error(f"❌ Download error: {e}")
        raise
    finally:
        remove_temp_files(temp_stem)
