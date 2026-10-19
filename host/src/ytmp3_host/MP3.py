from ytmp3_host.config import DEFAULT_FOLDER
from ytmp3_host.native_messaging import Message, send_message, read_message, run_native, send_current_folder
from collections.abc import Callable
import logging
import sys
# ----------------------------------------   Logging   --------------------------------------------
logging.basicConfig(filename="mp3.log",
                    level=logging.ERROR,
                    format="%(asctime)s [%(levelname)s] %(message)s")
# -----------------------------------------   Actions   ------------------------------------------
# The GUI module is imported only inside the GUI actions, so headless downloads never
# import customtkinter or create a Tk window (faster host startup).
def download(request: Message) -> None:
    run_native(request.get("url", ""), request.get("folder", ""), request.get("filename", ""))

def download_gui(request: Message) -> None:
    from ytmp3_host.gui import open_gui
    open_gui(request.get("url", ""), request.get("folder", ""), request.get("filename", ""))

def select_folder(_request: Message) -> None:
    from ytmp3_host import gui
    gui.select_folder()

def get_current_folder(_request: Message) -> None:
    send_current_folder(str(DEFAULT_FOLDER))

ACTIONS: dict[str, Callable[[Message], None]] = {
    "download": download,
    "download_gui": download_gui,
    "select_folder": select_folder,
    "get_current_folder": get_current_folder,
}
# -----------------------------------------   Main   ------------------------------------------

def main() -> None:
    """Main function to handle backend of the downloader/converter
     - Reads and sends messages via native messaging protocol
     - Supports actions: download, download_gui, select_folder, get_current_folder
    """
    while True:
        try:
            request = read_message()
            action = request.get("action", "download")

            handler = ACTIONS.get(action, download)
            try:
                handler(request)
            except BrokenPipeError as e:
                logging.error(f"BrokenPipeError: {e}")
                sys.exit(1)
            except Exception as e:
                try:
                    send_message({"status": "error", "message": f"Error processing action '{action}': {e}", "final": True})
                except Exception as err:
                    logging.error(f"Failed to send error message: {err}")
                    sys.exit(1)
        except SystemExit:
            raise
        except Exception as e:
            try:
                send_message({"status": "error", "message": f"Native messaging error: {e}", "final": True})
            except Exception as err:
                logging.error(f"Failed to send error message: {err}")
                sys.exit(1) # check edge case Errors and raises


if __name__ == "__main__": # remove logging and add icon for production
    main()


# COMPILE (from repo root): uv run pyinstaller --onedir --noconfirm --name mp3 --icon "packaging\assets\mp3.ico" host\src\ytmp3_host\MP3.py
