from ytmp3_host.core import download_audio, safe_filename, ensure_default_folder
from pathlib import Path
from typing import Any
import subprocess
import logging
import struct
import json
import sys

logging.basicConfig( # remove logging for .exe?
    filename="mp3.log", # for now at least
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

type Message = dict[str, Any]

def read_message() -> Message:
    """Read a JSON message from stdin (Firefox → Python)
     - first 4 bytes contain the length of the following JSON message
    """
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length:
        sys.exit(0)
    length = struct.unpack('@I', raw_length)[0]
    message = sys.stdin.buffer.read(length).decode('utf-8')
    return json.loads(message)

def send_message(message_content: Message, exit_code: int = 1) -> None:
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

def send_current_folder(folder: str) -> None:
    """Send the given folder path via native messaging"""
    folder = folder.strip()
    if not folder:
        folder_path = ensure_default_folder()
        send_message({"status": "success", "folder": str(folder_path), "final": True})
        return

    f_path = Path(folder).expanduser()
    if not f_path.is_dir():
        send_message({"status": "error", "message": f"Path doesn't exist", "final": True})
        return

    send_message({"status": "success", "folder": str(f_path), "final": True})

def run_native(url: str, folder: str, i_filename: str) -> None:
    """Native messaging loop
     - read JSON requests and send JSON responses
     - each request should contain 'url' and optional 'folder'
     - basically core.download_audio wrapped for native messaging
     """
    if not url or not any(host in url for host in ("youtube.com", "youtu.be/")):
        send_message({"status": "error", "message": "Invalid YouTube URL", "final": True})
        return

    if folder:
        input_folder = Path(folder).resolve()
        if not input_folder.is_dir():
            send_message({"status": "warning", "message": "Invalid folder path, using default", "final": False})
            input_folder = ensure_default_folder()
    else:
        input_folder = ensure_default_folder()

    # empty filename: core names the file after the video title, fetched in the same yt-dlp call
    filename = safe_filename(i_filename)
    if filename.lower() == "youtube mp3":
        filename = ""

    try:
        mp3_path = download_audio(url, input_folder, filename)
    except FileExistsError as e:
        send_message({
            "status": "error",
            "message": "File with this name already exists",
            "filename": Path(e.filename).stem,
            "final": True
        })
        return
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
            "filename": mp3_path.stem,
            "path": str(mp3_path),
            "final": True
        })
    else:
        send_message({
            "status": "error",
            "message": "Download failed, file not created",
            "final": True
        })


# remove logging for .exe
# add right click context menu
# tests? add video and playlist download options?