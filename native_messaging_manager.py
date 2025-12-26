from native_messaging import send_message, read_message
from config import DEFAULT_FOLDER
from pathlib import Path
import subprocess
import logging
import json
# ----------------------------------------   Logging   --------------------------------------------
logging.basicConfig(filename="mp3_gui.log",
                    level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

EXE_PATH = None # add later, must be a Path

def main():
    if not EXE_PATH or not EXE_PATH.is_file():
        logging.error("Executable path is not set or invalid")
        send_message({"status": "error", "message": "Executable path is not set or invalid"})
        return

    while True:
        request = read_message()
        if not request:
            if request is None:
                break
            continue

        url = request.get("url", "")
        folder = request.get("folder", "")
        if folder:
            f_path = Path(folder).resolve()
            if f_path.is_dir():
                folder = str(f_path)
            else:
                send_message({"status": "warning", "message": "Invalid folder path, using default"})
                folder = DEFAULT_FOLDER

        action = request.get("action", "download_gui")
        if action == "download" and (not folder or not url):
            send_message({"status": "error", "message": "Missing URL or folder for download"})
            continue

        if action == "download":
            cmd = [str(EXE_PATH), "download", "--url", url, "--folder", folder]
        elif action == "download_gui":
            cmd = [str(EXE_PATH), "download_gui", "--url", url, "--folder", folder]
        elif action == "select_folder":
            cmd = [str(EXE_PATH), "select-folder"]
        elif action == "get_current_folder":
            cmd = [str(EXE_PATH), "get-current-folder"]
        else:
            logging.error("Unknown action attempted")
            send_message({"status": "error", "message": "Unknown action attempted"})
            continue

        if action in ["download", "download_gui"]:
            try:
                process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.DETACHED_PROCESS # Windows only
                )
                send_message({"status": "success", "message": "Download started"})
            except Exception as e:
                logging.error(f"Failed to execute command: {e}")
                send_message({"status": "error", "message": f"Failed to execute command: {e}"})

        elif action in ["select_folder", "get_current_folder"]:
            try:
                subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=10
                )
            except Exception as e:
                logging.error(f"Failed to execute command: {e}")
                send_message({"status": "error", "message": f"Failed to execute command: {e}"})

            with open("status.json", "r", encoding="utf-8") as f:
                try:
                    status_data = json.load(f)
                    path = status_data.get("current_folder", str(DEFAULT_FOLDER))
                    send_message({"status": "success", "folder": path})
                except FileNotFoundError:
                    logging.error(f"Folder not found: {path}")
                    send_message({"status": "error", "message": f"Folder not found: {path}"})
                except json.JSONDecodeError as e:
                    logging.error(f"Failed to parse status file: {e}")
                    send_message({"status": "error", "message": f"Failed to parse status file: {e}"})


if __name__  == "__main__":
    main()


# RECEIVING
# {"url": "..."} - url only
# {"url": "...", "folder": "..."} - with folder

# {"action": "download", "url": "...", "folder": "..."} - Silent download
# {"action": "download_gui", "url": "..."} - Open GUI with prefilled URL
# {"action": "select_folder"} - Open folder picker, return path
# {"action": "get_current_folder"} - Return default folder path

# SENDING
# status: "success" / "error" / "warning"
# message: "..."
# filename: "..." - file already exists
# path: "..." - download successful
# folder: "..." - current folder path !!! not implemented properly yet