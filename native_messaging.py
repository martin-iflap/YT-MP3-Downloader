from core_and_auto import download_audio_headless, safe_filename
from pathlib import Path
import subprocess
import logging
import struct
import json
import sys

logging.basicConfig(
    filename="native_messaging.log",
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

def send_message(message_content: dict) -> None:
    """Send a JSON message to stdout (Python → Firefox)
     - encode the length as first 4 bytes followed by the JSON message
    """
    encoded = json.dumps(message_content, separators=(',', ':')).encode('utf-8')
    sys.stdout.buffer.write(struct.pack('@I', len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()

def run_native() -> None:
    """Native messaging loop
     - read JSON requests and send JSON responses
     - each request should contain 'url' and optional 'folder'
     - basically mp3_2.download_audio_headless wrapped for native messaging
     """
    try:
        while True:
            request = read_message()
            url = request.get("url", "")
            folder = request.get("folder", "")

            if not url or "youtube.com" not in url:
                send_message({"status": "error", "message": "Invalid YouTube URL"})
                continue

            if folder:
                input_folder = Path(folder).resolve()
                if not input_folder.is_dir():
                    send_message({"status": "warning", "message": "Invalid folder path, using default"})
                    input_folder = DEFAULT_FOLDER
            else:
                input_folder = DEFAULT_FOLDER

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
                filename = "Youtube MP3"

            mp3_path = input_folder / f"{filename}.mp3"
            if mp3_path.exists():
                send_message({
                    "status": "error",
                    "message": "File with this name already exists",
                    "filename": filename
                })
                continue
            try:
                download_audio_headless(url, str(input_folder), filename)
                send_message({
                    "status": "success",
                    "filename": filename,
                    "path": str(mp3_path)
                })
            except subprocess.TimeoutExpired as e:
                send_message({"status": "error", "message": f"Download timed out: {e}"})
                logging.error(f"Download timed out: {e}")
            except Exception as e:
                logging.error(f"Download failed: {e}")
                send_message({"status": "error", "message": f"Download failed: {e}"})
    except Exception as e:
        logging.error(f"Native messaging error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run_native()