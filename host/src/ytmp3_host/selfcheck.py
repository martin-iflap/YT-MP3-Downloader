"""`mp3.exe --self-check`: does this host version work well enough to be switched to?

The installer runs it on a new version's mp3.exe before it points Firefox at it, so
a broken release doesn't destroy the whole host (corrupted version can't update itself).
Exit code 0 means it works; each problem is logged to mp3.log and written to stderr.
Not run under Firefox, but stdout stays unused anyway.

It does not check yt-dlp.exe: all versions share it, and a missing or broken one is
repaired by the host itself.
"""

import importlib
import io
import logging
import subprocess
import sys

from ytmp3_host.config import FFMPEG_PATH

logger = logging.getLogger(__name__)

# every module a request can load; the lazy imports in MP3 would only fail on first use
MODULES = (
    "ytmp3_host.core",
    "ytmp3_host.native_messaging",
    "ytmp3_host.health",
    "ytmp3_host.github",
    "ytmp3_host.updater",
    "ytmp3_host.gui",
)
FFMPEG_TIMEOUT_S = 20  # the first run of a new exe is often held up by the virus scanner


def ffmpeg_version_command() -> list[str]:
    """The command that checks ffmpeg.exe runs (the tests swap it)."""
    return [str(FFMPEG_PATH), "-version"]


def _check_modules() -> list[str]:
    problems = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:  # a broken build can fail with anything, not just ImportError
            problems.append(f"Could not import {name}: {e!r}")
    return problems


def _check_framing() -> list[str]:
    """Send a message and read it back the way every request is answered and read."""
    from ytmp3_host.native_messaging import read_message, send_message

    message = {"status": "success", "final": True}
    stdin, stdout = sys.stdin, sys.stdout
    try:
        sys.stdout = io.TextIOWrapper(io.BytesIO())
        send_message(message)
        sys.stdin = io.TextIOWrapper(io.BytesIO(sys.stdout.buffer.getvalue()))
        echoed = read_message()
    except BaseException as e:  # send_message exits if it can't write
        return [f"Native messaging does not work: {e!r}"]
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    return [] if echoed == message else [f"Native messaging garbled {message} into {echoed}"]


def _check_ffmpeg() -> list[str]:
    command = ffmpeg_version_command()
    try:
        subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=FFMPEG_TIMEOUT_S,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return [f"{command[0]} does not run: {e}"]
    return []


def self_check() -> int:
    """Run all checks and report the problems.
    Returns:
        The exit code: 0 if this host version works, 1 if not.
    """
    problems = _check_modules()
    if not problems:  # the framing check needs native_messaging
        problems += _check_framing()
    problems += _check_ffmpeg()
    for problem in problems:
        logger.error("Self-check: %s", problem)
        sys.stderr.write(f"{problem}\n")
    return 1 if problems else 0
