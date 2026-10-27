"""`mp3.exe --self-check`: does this host version work well enough to be switched to?

The installer runs it on a new version's mp3.exe before it points Firefox at it, so
a broken release doesn't destroy the whole host (corrupted version can't update itself).
Exit code 0 means it works; each problem is logged to mp3.log and written to stderr.
Not run under Firefox, but stdout stays unused anyway.

Checked, all offline: every module imports, a second process of this version started
like Firefox starts it answers a request, HTTPS works (the updater needs it), and the
ffmpeg steps of a download work on a generated tone. It does not check yt-dlp.exe: all
versions share it, and a missing or broken one is repaired by the host itself.
"""

import importlib
import json
import logging
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from ytmp3_host import __version__
from ytmp3_host.config import FROZEN

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
# the first run of a new exe is often held up by the virus scanner
HOST_TIMEOUT_S = 30
FFMPEG_TIMEOUT_S = 20
# the ID3v2.3 frames of a finished download: artist, title, cover
EXPECTED_FRAMES = (b"TPE1", b"TIT2", b"APIC")


def host_command() -> list[str]:
    """The command that starts this host version the way Firefox does (the tests swap it)."""
    return [sys.executable] if FROZEN else [sys.executable, "-m", "ytmp3_host.MP3"]


def _check_modules() -> list[str]:
    problems = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:  # a broken build can fail with anything, not just ImportError
            problems.append(f"Could not import {name}: {e!r}")
    return problems


def _check_request() -> list[str]:
    """Start this host version as Firefox does, send it a ping and expect exactly its answer.

    Covers what the in-process checks can't: the exe starts as a host, reads a framed
    request from stdin, dispatches it, answers on stdout and exits when stdin closes.
    """
    from ytmp3_host.native_messaging import LENGTH_PREFIX

    request = json.dumps({"action": "ping"}).encode()
    command = host_command()
    try:
        result = subprocess.run(
            command,
            input=LENGTH_PREFIX.pack(len(request)) + request,  # then stdin closes, which ends the host
            capture_output=True,
            timeout=HOST_TIMEOUT_S,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return [f"The host does not start: {e}"]

    expected = {"status": "success", "version": __version__, "final": True}
    answer, size = result.stdout, LENGTH_PREFIX.size
    try:
        (length,) = LENGTH_PREFIX.unpack(answer[:size])
        reply = json.loads(answer[size:]) if len(answer) == size + length else None
    except (struct.error, ValueError):  # none or a short length prefix, or not JSON
        reply = None
    if result.returncode != 0 or reply != expected:
        stderr = result.stderr.decode(errors="replace").strip()[-500:]
        return [f"The host did not answer a request (exit code {result.returncode}): {answer[:200]!r} {stderr}"]
    return []


def _check_https() -> list[str]:
    """Check that the updater can make HTTPS connections (no network needed).
    Without the ssl module urllib still imports, it just can't open https:// URLs,
    so a build missing it would pass the imports and never be able to update itself.
    """
    try:
        import ssl
        import urllib.request

        ssl.create_default_context()  # also loads the Windows certificate store
        if not hasattr(urllib.request, "HTTPSHandler"):
            return ["urllib has no HTTPS support"]
    except Exception as e:  # a broken build can fail with anything
        return [f"HTTPS does not work: {e!r}"]
    return []


def _check_ffmpeg() -> list[str]:
    """Run the ffmpeg steps of a download on a generated 1 s tone and a non-square image.

    The MP3 V0 encode yt-dlp asks ffmpeg for, then the real core.embed_thumbnail
    (cover cropped to 1:1, JPEG, ID3v2.3 tags). Nothing is downloaded; the temp
    folder is removed afterward.
    """
    from ytmp3_host import core

    ffmpeg = core.FFMPEG_PATH
    with tempfile.TemporaryDirectory(prefix="ytmp3-selfcheck-") as folder:
        tone, cover, output = (Path(folder) / name for name in ("tone.mp3", "cover.png", "output.mp3"))
        generate = [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i"]
        try:
            for command in (
                [*generate, "sine=frequency=440:duration=1", "-c:a", "libmp3lame", "-q:a", "0", str(tone)],
                [*generate, "color=c=red:s=32x18", "-frames:v", "1", str(cover)],
            ):
                subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=FFMPEG_TIMEOUT_S,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    check=True,
                )
            core.embed_thumbnail(tone, cover, "Self-check", "Tone", output)
            written = output.read_bytes()
        except subprocess.CalledProcessError as e:
            return [f"{ffmpeg} does not work: {e} {(e.stderr or '').strip()[-500:]}"]
        except (OSError, subprocess.SubprocessError) as e:
            return [f"{ffmpeg} does not work: {e}"]
    missing = [frame.decode() for frame in EXPECTED_FRAMES if frame not in written]
    if not written.startswith(b"ID3\x03") or missing:
        return [f"{ffmpeg} wrote a wrong MP3 (no ID3v2.3 tag or missing {missing})"]
    return []


def self_check() -> int:
    """Run all checks and report the problems.
    Returns:
        The exit code: 0 if this host version works, 1 if not.
    """
    problems = _check_modules()
    if not problems:  # the other checks need the host modules
        problems += _check_request()
        problems += _check_ffmpeg()
    problems += _check_https()
    for problem in problems:
        logger.error("Self-check: %s", problem)
        sys.stderr.write(f"{problem}\n")
    return 1 if problems else 0
