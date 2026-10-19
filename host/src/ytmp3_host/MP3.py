"""Entry point of the native host: the request loop and the action dispatch table.
The GUI module is imported only inside the GUI actions, so headless requests never
import customtkinter or create a Tk window (faster host startup).
"""

import logging
import sys
from collections.abc import Callable

from ytmp3_host.native_messaging import Message, read_message, run_native, send_current_folder, send_message

logging.basicConfig(
    filename="mp3.log",
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def download(request: Message) -> None:
    """Handle the `download` action: a headless download."""
    run_native(request.get("url", ""), request.get("folder", ""), request.get("filename", ""))


def download_gui(request: Message) -> None:
    """Handle the `download_gui` action: open the prefilled window, then exit.

    The extension keeps this port open so Firefox does not kill the window, so the
    host exits itself once the window is closed instead of waiting on stdin forever.
    """
    try:
        from ytmp3_host.gui import open_gui

        open_gui(request.get("url", ""), request.get("folder", ""), request.get("filename", ""))
    except Exception as e:
        # report it here: after main() reported it, the host would wait on stdin forever
        logging.exception("Could not open the GUI")
        send_message({"status": "error", "message": f"Could not open the app window: {e}", "final": True})
    sys.exit(0)


def select_folder(request: Message) -> None:
    """Handle the `select_folder` action: folder picker starting at the request's folder."""
    from ytmp3_host.gui import select_folder as pick_folder

    pick_folder(request.get("folder", ""))


def get_current_folder(_request: Message) -> None:
    """Handle the `get_current_folder` action: report the default folder, creating it."""
    send_current_folder("")


ACTIONS: dict[str, Callable[[Message], None]] = {
    "download": download,
    "download_gui": download_gui,
    "select_folder": select_folder,
    "get_current_folder": get_current_folder,
}


def main() -> None:
    """Serve native-messaging requests until Firefox closes stdin.
    Each request is dispatched by its `action`; a missing or unknown action is
    treated as `download`. A handler that raises is reported as a final error
    response, so the extension always gets exactly one final message.
    """
    while True:
        try:
            request = read_message()
        except Exception as e:
            logging.exception("Could not read native message")
            send_message({"status": "error", "message": f"Native messaging error: {e}", "final": True})
            continue

        action = str(request.get("action", "download"))
        try:
            ACTIONS.get(action, download)(request)
        except BrokenPipeError as e:
            logging.error("BrokenPipeError: %s", e)
            sys.exit(1)
        except Exception as e:
            logging.exception("Error processing action '%s'", action)
            send_message({"status": "error", "message": f"Error processing action '{action}': {e}", "final": True})


if __name__ == "__main__":
    main()


# COMPILE (from repo root): uv run pyinstaller --onedir --noconfirm --name mp3 --icon "packaging\assets\mp3.ico" host\src\ytmp3_host\MP3.py
