from .core import download_audio_headless, safe_filename
from .config import YT_DLP_PATH, DEFAULT_FOLDER
from pathlib import Path
import subprocess
import logging
import struct
import json
import sys

logging.basicConfig( # remove logging for .exe?
    filename="mp3.log", # for now at least
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

def read_message() -> dict:
    """Read a JSON message from stdin (Firefox → Python)
     - first 4 bytes contain the length of the following JSON message
    """
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length:
        sys.exit(0)
    length = struct.unpack('@I', raw_length)[0]
    message = sys.stdin.buffer.read(length).decode('utf-8')
    return json.loads(message)

def send_message(message_content: dict, exit_code: int = 1) -> None:
    """Send a JSON message to stdout (Python → Firefox)
     - encode the length as first 4 bytes followed by the JSON message
     - on failure, log the error and exit with the given exit_code
    """
    try:
        encoded = json.dumps(message_content, separators=(',', ':')).encode('utf-8')

        sys.stdout.buffer.write(struct.pack('@I', len(encoded)))
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
    except (BrokenPipeError, OSError, TypeError, ValueError, struct.error) as e:
        logging.error(f"Failed to send error message, exiting: {e}")
        try:
            sys.stdout.buffer.flush()
        except (OSError, AttributeError):
            pass
        sys.exit(exit_code)

def run_native(url: str, folder: str, i_filename: str) -> None:
    """Native messaging loop
     - read JSON requests and send JSON responses
     - each request should contain 'url' and optional 'folder'
     - basically mp3_2.download_audio_headless wrapped for native messaging
     """
    if not url or "youtube.com" not in url:
        send_message({"status": "error", "message": "Invalid YouTube URL", "final": True})
        return

    if folder:
        input_folder = Path(folder).resolve()
        if not input_folder.is_dir():
            send_message({"status": "warning", "message": "Invalid folder path, using default", "final": False})
            input_folder = DEFAULT_FOLDER
    else:
        input_folder = DEFAULT_FOLDER

    if safe_filename(i_filename).strip() and safe_filename(i_filename).lower() != "youtube mp3":
        filename = safe_filename(i_filename)
    else:
        try:
            title = subprocess.run([
                str(YT_DLP_PATH),
                "--no-playlist",
                "--skip-download",
                "--get-title",
                url
            ], capture_output=True, text=True, check=True, timeout=15)
            filename = safe_filename(title.stdout.strip() or "Youtube MP3")
        except Exception as e:
            logging.error(f"Failed to fetch title: {e}")
            send_message({"status": "warning", "message": "Could not fetch title, using default", "final": False})
            filename = "Youtube MP3"

    mp3_path = input_folder / f"{filename}.mp3"
    if mp3_path.exists():
        send_message({
            "status": "error",
            "message": "File with this name already exists",
            "filename": filename,
            "final": True
        })
        return
    try:
        download_audio_headless(url, str(input_folder), filename)
    except subprocess.TimeoutExpired as e:
        send_message({"status": "error", "message": f"Download timed out: {e}", "final": True})
        logging.error(f"Download timed out: {e}")
        return
    except Exception as e:
        logging.error(f"Download failed: {e}")
        send_message({"status": "error", "message": f"Download failed: {e}", "final": True})
        return

    if mp3_path.exists():
        send_message({
            "status": "success",
            "filename": filename,
            "path": str(mp3_path),
            "final": True
        })
    else:
        send_message({
            "status": "error",
            "message": "Download failed, file not created",
            "final": True
        })


# git rm = delete, git rm --cached = only git
# remove logging for .exe, make the extension persistent
# add right click context menu, add option to name the file?
# tests?, better code and file structure, add video and playlist download options?