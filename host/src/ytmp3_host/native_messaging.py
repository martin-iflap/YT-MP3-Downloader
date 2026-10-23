"""Firefox native-messaging framing and the headless request handlers.
stdout is the protocol channel: nothing else may write to it (no print()).
"""

import json
import logging
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

from ytmp3_host.core import (
    describe_error,
    download_with_repair,
    ensure_default_folder,
    is_youtube_url,
    requested_filename,
    resolve_folder,
)

logger = logging.getLogger(__name__)

LENGTH_PREFIX = struct.Struct("@I")  # 4-byte unsigned int, native byte order

type Message = dict[str, Any]


def _read_exact(size: int) -> bytes | None:
    """Read exactly size bytes from stdin.
    Args:
        size: The number of bytes to read.
    Returns:
        The bytes, or None if stdin was closed before all of them arrived.
    """
    data = b""
    while len(data) < size:
        chunk = sys.stdin.buffer.read(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def read_message() -> Message:
    """Read one length-prefixed JSON message from stdin (Firefox to host).
    Exits the process with code 0 when stdin is closed, which is how Firefox ends
    the host after port.disconnect().

    Returns:
        The decoded message.
    Raises:
        ValueError: If the message is not valid UTF-8 JSON or not a JSON object.
    """
    raw_length = _read_exact(LENGTH_PREFIX.size)
    if raw_length is None:
        sys.exit(0)
    (length,) = LENGTH_PREFIX.unpack(raw_length)
    raw_message = _read_exact(length)
    if raw_message is None:
        sys.exit(0)
    message = json.loads(raw_message.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("Message is not a JSON object")
    return message


def send_message(message: Message, exit_code: int = 1) -> None:
    """Write one length-prefixed JSON message to stdout (host to Firefox).

    Args:
        message: The response to send. Must be JSON serializable.
        exit_code: Exit code used if the message cannot be sent. A host that cannot
            talk to Firefox is useless, so it exits instead of raising.
    """
    logger.debug("Response: %s", message)
    try:
        encoded = json.dumps(message, separators=(",", ":")).encode("utf-8")
        sys.stdout.buffer.write(LENGTH_PREFIX.pack(len(encoded)))
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
    except (OSError, TypeError, ValueError, struct.error) as e:
        logger.error("Failed to send message, exiting: %s", e)
        sys.exit(exit_code)


def send_current_folder(folder: str) -> None:
    """Send a folder as the final response of a folder action.
    Args:
        folder: The folder to report. Empty or the default folder sends the default
            folder, created if missing.
    """
    folder_path = resolve_folder(folder)
    if folder_path is None:
        send_message({"status": "error", "message": "Path doesn't exist", "final": True})
        return
    send_message({"status": "success", "folder": str(folder_path), "final": True})


def run_native(url: str, folder: str, filename: str) -> None:
    """Handle a headless download request and send its responses.
    Sends intermediate warnings (invalid folder, so the default is used; no title;
    no thumbnail) and infos (a repair after a yt-dlp failure, see
    download_with_repair), then exactly one final success or error response.

    Args:
        url: The YouTube video URL.
        folder: The target folder; empty means the default folder.
        filename: The requested filename; empty means name it after the video title.
    """
    if not url or not is_youtube_url(url):
        send_message({"status": "error", "message": "Invalid YouTube URL", "final": True})
        return

    folder_path = resolve_folder(folder)
    if folder_path is None:
        send_message({"status": "warning", "message": "Invalid folder path, using default", "final": False})
        folder_path = ensure_default_folder()

    warnings: list[str] = []

    def _send_warning(message: str) -> None:
        warnings.append(message)
        send_message({"status": "warning", "message": message, "final": False})

    try:
        mp3_path = download_with_repair(
            url,
            folder_path,
            requested_filename(filename),
            on_warning=_send_warning,
            on_info=lambda message: send_message({"status": "info", "message": message, "final": False}),
        )
    except FileExistsError as e:
        send_message({
            "status": "error",
            "message": describe_error(e),
            "filename": Path(e.filename).stem,
            "final": True,
        })
        return
    except subprocess.TimeoutExpired as e:
        logger.error("Download timed out: %s", e)
        send_message({"status": "error", "message": describe_error(e), "final": True})
        return
    except Exception as e:
        logger.error("Download failed: %s", e)
        send_message({"status": "error", "message": f"Download failed: {describe_error(e)}", "final": True})
        return

    response: Message = {"status": "success", "filename": mp3_path.stem, "path": str(mp3_path), "final": True}
    if warnings:
        # the final message replaces the warnings in the sidebar, so repeat them there
        response["message"] = f"Downloaded: {mp3_path.stem}. {'. '.join(warnings)}."
    send_message(response)
