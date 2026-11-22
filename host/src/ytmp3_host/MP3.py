"""Entry point of the native host: the request loop and the action dispatch table.
The GUI, health, updater and selfcheck modules are lazy imports (PEP 810): they load on first
use inside their actions, so headless downloads never import customtkinter, create a
Tk window or load urllib (faster host startup).
"""

import logging
import sys
from collections.abc import Callable

from ytmp3_host import __version__
from ytmp3_host.config import setup_logging
from ytmp3_host.media_format import Mp3Format, media_format_from_request
from ytmp3_host.native_messaging import (
    Message,
    read_message,
    run_native,
    run_open_file,
    send_current_folder,
    send_message,
)
lazy from ytmp3_host.gui import open_gui, select_folder as pick_folder
lazy from ytmp3_host.health import host_status
lazy from ytmp3_host.selfcheck import self_check
lazy from ytmp3_host.updater import (
    UpdateError,
    remove_installed_downloads,
    remove_unused_yt_dlp,
    update_host as install_host_update,
    update_yt_dlp as install_yt_dlp,
)

# not __name__: run as a script (and when frozen) that is "__main__", outside the
# "ytmp3_host" logger whose level setup_logging() sets
logger = logging.getLogger("ytmp3_host.MP3")

# The installer runs `mp3.exe --self-check`, then `mp3.exe --install-yt-dlp`, before switching
# Firefox to a new version. Firefox starts the host with other arguments (the manifest path
# and the extension ID).
SELF_CHECK_ARG = "--self-check"
INSTALL_YT_DLP_ARG = "--install-yt-dlp"


def download(request: Message) -> None:
    """Handle the `download` action: a headless download."""
    try:
        media_format = media_format_from_request(request)
    except ValueError as e:
        send_message({"status": "error", "message": str(e), "final": True})
        return
    run_native(
        url=request.get("url", ""),
        folder=request.get("folder", ""),
        filename=request.get("filename", ""),
        media_format=media_format,
    )


def download_gui(request: Message) -> None:
    """Handle the `download_gui` action: open the prefilled window, then exit.

    The extension keeps this port open so Firefox does not kill the window, so the
    host exits itself once the window is closed instead of waiting on stdin forever.
    The format only preselects the window's switch, so an unknown one selects MP3
    instead of keeping the window closed.
    """
    try:
        media_format = media_format_from_request(request)
    except ValueError as e:
        logger.warning("Opening the GUI with MP3: %s", e)
        media_format = Mp3Format()
    try:
        open_gui(request.get("url", ""), request.get("folder", ""), request.get("filename", ""), media_format)
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


def show_in_folder(request: Message) -> None:
    """Handle the `show_in_folder` action: show a downloaded MP3 (`path`) selected in Explorer."""
    run_open_file(request.get("path", ""), select=True)


def play(request: Message) -> None:
    """Handle the `play` action: open a downloaded MP3 (`path`) in the default player."""
    run_open_file(request.get("path", ""), select=False)


def get_version(_request: Message) -> None:
    """Handle the `get_version` action: the host version and the state of its tools.

    1.x hosts do not know this action and answer it as a download without a URL
    ("Invalid YouTube URL"); the extension relies on that to recognize them. It also
    deletes the installer download of an update that is done and the yt-dlp files no
    longer used (a few ms when there is nothing to delete; the sidebar sends this on
    every open, and the new version answers it after an update).
    """
    remove_installed_downloads()
    remove_unused_yt_dlp()
    send_message({"status": "success", **host_status(), "final": True})


def ping(_request: Message) -> None:
    """Handle the `ping` action: answer with the version, nothing else.

    Not used by the extension: the self-check sends it to a second process of this
    host version, started like Firefox starts it, to prove the build serves requests.
    """
    send_message({"status": "success", "version": __version__, "final": True})


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


def install_yt_dlp_for_setup() -> int:
    """What `mp3.exe --install-yt-dlp` runs: install or update yt-dlp, for the installer.

    The installer runs it on the new version before switching Firefox to it, instead of
    downloading yt-dlp itself, so a new installation gets the onedir build too. It installs
    nothing next to the old version's files: the yt-dlp files the old version may still
    run are removed later by remove_unused_yt_dlp.

    Returns:
        The exit code: 0 when a working yt-dlp is installed, 1 when not (the reason is in
        mp3.log; the installation goes on, and the host repairs yt-dlp on its next check).
    """
    try:
        version = install_yt_dlp()
    except Exception:  # an exit code is the only answer the installer reads
        logger.exception("Could not install yt-dlp")
        return 1
    logger.info("yt-dlp %s is installed", version)
    return 0


ACTIONS: dict[str, Callable[[Message], None]] = {
    "download": download,
    "download_gui": download_gui,
    "select_folder": select_folder,
    "get_current_folder": get_current_folder,
    "show_in_folder": show_in_folder,
    "play": play,
    "get_version": get_version,
    "update_yt_dlp": update_yt_dlp,
    "update_host": update_host,
    "ping": ping,
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
    if sys.argv[1:] == [SELF_CHECK_ARG]:
        sys.exit(self_check())
    elif sys.argv[1:] == [INSTALL_YT_DLP_ARG]:
        sys.exit(install_yt_dlp_for_setup())
    main()


# COMPILE (from repo root): uv run pyinstaller --onedir --noconfirm --name mp3 --exclude-module PIL --icon "packaging\assets\mp3.ico" host\src\ytmp3_host\MP3.py
