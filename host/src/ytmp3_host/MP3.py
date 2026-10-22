"""Entry point of the native host: the request loop and the action dispatch table.
The GUI, health and updater modules are lazy imports (PEP 810): they load on first
use inside their actions, so headless downloads never import customtkinter, create a
Tk window or load urllib (faster host startup).
"""

import logging
import sys
from collections.abc import Callable

from ytmp3_host.config import setup_logging
lazy from ytmp3_host.gui import open_gui, select_folder as pick_folder
lazy from ytmp3_host.health import host_status
from ytmp3_host.native_messaging import Message, read_message, run_native, send_current_folder, send_message
lazy from ytmp3_host.updater import UpdateError, update_host as install_host_update, update_yt_dlp as install_yt_dlp

# not __name__: run as a script (and when frozen) that is "__main__", outside the
# "ytmp3_host" logger whose level setup_logging() sets
logger = logging.getLogger("ytmp3_host.MP3")


def download(request: Message) -> None:
    """Handle the `download` action: a headless download."""
    run_native(request.get("url", ""), request.get("folder", ""), request.get("filename", ""))


def download_gui(request: Message) -> None:
    """Handle the `download_gui` action: open the prefilled window, then exit.

    The extension keeps this port open so Firefox does not kill the window, so the
    host exits itself once the window is closed instead of waiting on stdin forever.
    """
    try:
        open_gui(request.get("url", ""), request.get("folder", ""), request.get("filename", ""))
    except Exception as e:
        # report it here: after main() reported it, the host would wait on stdin forever
        logger.exception("Could not open the GUI")
        send_message({"status": "error", "message": f"Could not open the app window: {e}", "final": True})
    sys.exit(0)


def select_folder(request: Message) -> None:
    """Handle the `select_folder` action: folder picker starting at the request's folder."""
    pick_folder(request.get("folder", ""))


def get_current_folder(_request: Message) -> None:
    """Handle the `get_current_folder` action: report the default folder, creating it."""
    send_current_folder("")


def get_version(_request: Message) -> None:
    """Handle the `get_version` action: the host version and the state of its tools.

    1.x hosts do not know this action and answer it as a download without a URL
    ("Invalid YouTube URL"); the extension relies on that to recognize them.
    """
    send_message({"status": "success", **host_status(), "final": True})


def update_yt_dlp(_request: Message) -> None:
    """Handle the `update_yt_dlp` action: install the latest yt-dlp, or repair a missing or broken one."""
    try:
        version = install_yt_dlp()
    except UpdateError as e:
        send_message({"status": "error", "message": str(e), "final": True})
        return
    send_message({"status": "success", "yt_dlp": {"version": version}, "final": True})


def update_host(_request: Message) -> None:
    """Handle the `update_host` action: start the installer of a newer host release.

    `version` is the tag being installed, or None if this host is the latest. The
    installer keeps running after this host exits.
    """
    try:
        tag = install_host_update()
    except UpdateError as e:
        send_message({"status": "error", "message": str(e), "final": True})
        return
    send_message({"status": "success", "version": tag, "final": True})


ACTIONS: dict[str, Callable[[Message], None]] = {
    "download": download,
    "download_gui": download_gui,
    "select_folder": select_folder,
    "get_current_folder": get_current_folder,
    "get_version": get_version,
    "update_yt_dlp": update_yt_dlp,
    "update_host": update_host,
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
            logger.exception("Could not read native message")
            send_message({"status": "error", "message": f"Native messaging error: {e}", "final": True})
            continue

        action = str(request.get("action", "download"))
        logger.debug("Request: %s", request)
        try:
            ACTIONS.get(action, download)(request)
        except BrokenPipeError as e:
            logger.error("BrokenPipeError: %s", e)
            sys.exit(1)
        except Exception as e:
            logger.exception("Error processing action '%s'", action)
            send_message({"status": "error", "message": f"Error processing action '{action}': {e}", "final": True})


if __name__ == "__main__":
    setup_logging()
    main()


# COMPILE (from repo root): uv run pyinstaller --onedir --noconfirm --name mp3 --icon "packaging\assets\mp3.ico" host\src\ytmp3_host\MP3.py
