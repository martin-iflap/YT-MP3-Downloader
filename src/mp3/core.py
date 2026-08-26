from mp3.config import FFMPEG_PATH, YT_DLP_PATH, DEFAULT_FOLDER
from pathlib import Path
from PIL import Image
import subprocess
import logging
import os
import re

def safe_filename(name) -> str:
    """Sanitize the filename by removing invalid characters
     - also remove common extraneous words from YouTube titles
     - remove empty parentheses/brackets and extra spaces
        - return "YouTube MP3" if the result is empty or input invalid
    """
    if not isinstance(name, str) or not name.strip():
        return "Youtube MP3"
    name = re.sub(r'[\\/*?:"<>|]', '', name.strip())

    words = ["Official", "Music", "Video", "HD", "Lyrics", "Lyric", "Audio", "Explicit"]
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

def embed_thumbnail(mp3_file: Path, thumb_file: Path, artist: str) -> None:
    """Crop the thumbnail 1:1 and embed it into the MP3 file using ffmpeg
     - create a temp_mp3 file during the process to avoid corruption
    """
    folder = mp3_file.parent
    filename = mp3_file.stem

    if thumb_file.exists() and mp3_file.exists():
        try:
            img = Image.open(thumb_file)
            width, height = img.size
            size = min(width, height)
            left = (width - size) // 2
            top = (height - size) // 2
            img_cropped = img.crop((left, top, left + size, top + size))
            img_cropped.save(thumb_file)

            temp_mp3 = Path(folder) / f"{filename}_temp{os.getpid()}.mp3"
            subprocess.run([
                str(FFMPEG_PATH),
                "-i", str(mp3_file),
                "-i", str(thumb_file),
                "-map", "0:a",
                "-map", "1:0",
                "-c", "copy",
                "-id3v2_version", "3",
                "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)",
                "-metadata", f"artist={artist}",
                str(temp_mp3)
            ], check=True)

            mp3_file.unlink()
            temp_mp3.rename(mp3_file)
            thumb_file.unlink()

        except Exception as e:
            logging.error(f"❌ Error with thumbnail embedding: {e}")
            temp_mp3 = Path(folder) / f"{filename}_temp{os.getpid()}.mp3"
            if temp_mp3.exists():
                temp_mp3.unlink()
            raise
    else:
        logging.warning("⚠️ Thumbnail or MP3 Not Found")
        return

def download_audio_headless(url: str, folder: str, filename: str) -> None:
    """Headless download function for auto mode
     - download audio without GUI interaction
    """
    artist = "<unknown>"
    if "-" in filename:
        parts = filename.split("-")
        artist = parts[0].strip()

    mp3_check = Path(folder) / f"{filename}.mp3" # check this
    if mp3_check.exists():
        logging.error("❌ File with this name already exists!")
        return

    output_template = str(Path(folder) / f"{filename}.%(ext)s")

    if not FFMPEG_PATH.exists():
        logging.error("❌ ffmpeg.exe not found!")
        return

    try:
        subprocess.run([
            str(YT_DLP_PATH),
            "--no-playlist",
            "--concurrent-fragments", "3",
            "-x", "--audio-format", "mp3",
            "--write-thumbnail",
            "--convert-thumbnails", "jpg",
            "--ffmpeg-location", str(FFMPEG_PATH),
            "--newline",
            "-o", output_template,
            url
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, timeout=300, check=True)

        mp3_file = Path(folder) / f"{filename}.mp3"
        thumb_file = Path(folder) / f"{filename}.jpg"
        embed_thumbnail(mp3_file, thumb_file, artist)
    except FileNotFoundError as e:
        logging.error(f"❌ yt-dlp or ffmpeg not found: {e}")
        raise
    except subprocess.CalledProcessError as e:
        logging.error(f"❌ Error while downloading: {e}")
        raise
    except Exception as e:
        logging.error(f"❌ Unknown error: {e}")
        raise