"""`mp3.exe --self-check`: does this host version work well enough to be switched to?

The installer runs it on a new version's mp3.exe before it points Firefox at it, so
a broken release doesn't destroy the whole host (corrupted version can't update itself).
Exit code 0 means it works; each problem is logged to mp3.log and written to stderr.
Not run under Firefox, but stdout stays unused anyway.

Checked, all offline: every module imports, a second process of this version started
like Firefox starts it answers a request, HTTPS works (the updater needs it), and the
ffmpeg steps of an MP3 and an MP4 download work on generated media. It does not check
yt-dlp.exe: all versions share it, and a missing or broken one is repaired by the host itself.
"""

import importlib
import json
import logging
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from ytmp3_host import __version__
from ytmp3_host.config import FROZEN

logger = logging.getLogger(__name__)

# every module a request can load; the lazy imports in MP3 would only fail on first use
MODULES = (
    "ytmp3_host.naming",
    "ytmp3_host.metadata",
    "ytmp3_host.files",
    "ytmp3_host.processes",
    "ytmp3_host.ytdlp",
    "ytmp3_host.core",
    "ytmp3_host.native_messaging",
    "ytmp3_host.health",
    "ytmp3_host.media_format",
    "ytmp3_host.github",
    "ytmp3_host.updater",
    "ytmp3_host.gui",
)
# the first run of a new exe is often held up by the virus scanner
HOST_TIMEOUT_S = 30
FFMPEG_TIMEOUT_S = 20


class ExpectedFile(NamedTuple):
    """What a finished download's bytes must contain: `header` at `header_at`, and every marker."""

    header_at: int
    header: bytes
    markers: tuple[bytes, ...]


# an ID3v2.3 tag with the artist, title and cover frames
EXPECTED_MP3 = ExpectedFile(0, b"ID3\x03", (b"TPE1", b"TIT2", b"APIC"))
# an MP4 (its first box, after the 4-byte size, is ftyp) with the movie, the cover
# (the attached_pic becomes the covr box, not a third track), the artist and the title
EXPECTED_MP4 = ExpectedFile(4, b"ftyp", (b"moov", b"covr", b"\xa9ART", b"\xa9nam"))


def host_command() -> list[str]:
    """The command that starts this host version the way Firefox does (the tests swap it)."""
    return [sys.executable] if FROZEN else [sys.executable, "-m", "ytmp3_host.MP3"]


def _check_modules() -> list[str]:
    problems = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 - a broken build can fail with anything, not just ImportError
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
            check=False,
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
    except Exception as e:  # noqa: BLE001 - a broken build can fail with anything
        return [f"HTTPS does not work: {e!r}"]
    return []


def _run_ffmpeg_check(commands: list[list[str]], media: Path, cover: Path, expected: ExpectedFile) -> list[str]:
    """Run the ffmpeg commands that make the test media, then the real core.embed_thumbnail
    on it, and check the file it wrote (output.mp3 or output.mp4, next to the media).
    """
    from ytmp3_host import core

    ffmpeg = core.FFMPEG_PATH
    output = media.with_name(f"output{media.suffix}")
    try:
        for command in commands:
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
        core.embed_thumbnail(media, cover, "Self-check", "Tone", output)
        written = output.read_bytes()
    except subprocess.CalledProcessError as e:
        return [f"{ffmpeg} does not work: {e} {(e.stderr or '').strip()[-500:]}"]
    except (OSError, subprocess.SubprocessError) as e:
        return [f"{ffmpeg} does not work: {e}"]
    header = written[expected.header_at : expected.header_at + len(expected.header)]
    missing = [marker.decode("latin-1") for marker in expected.markers if marker not in written]
    if header != expected.header or missing:
        return [f"{ffmpeg} wrote a wrong {media.suffix[1:].upper()} (header {header!r}, missing {missing})"]
    return []


def _check_ffmpeg() -> list[str]:
    """Run the ffmpeg steps of an MP3 and an MP4 download on generated media and a non-square image.

    MP3: the V0 encode yt-dlp asks ffmpeg for on a 1 s tone, then the real
    core.embed_thumbnail (cover cropped to 1:1, JPEG, ID3v2.3 tags). MP4: a 1 s clip,
    then embed_thumbnail's MP4 pass (streams copied, the cover as attached_pic, tags).
    The clip uses ffmpeg's own mpeg4 and aac encoders, which every build has: downloads
    copy the streams, so a check needing libx264 or libopus could fail a working build.
    The MP4 check runs only when the MP3 one passed: a failure there is usually ffmpeg
    itself, and the version is refused either way. Nothing is downloaded; the temp
    folder is removed afterward.
    """
    from ytmp3_host import core

    generate = [str(core.FFMPEG_PATH), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i"]
    tone_source = "sine=frequency=440:duration=1"
    with tempfile.TemporaryDirectory(prefix="ytmp3-selfcheck-") as folder:
        tone, clip, cover = (Path(folder) / name for name in ("tone.mp3", "clip.mp4", "cover.png"))
        make_cover = [*generate, "color=c=red:s=32x18", "-frames:v", "1", str(cover)]
        make_tone = [*generate, tone_source, "-c:a", "libmp3lame", "-q:a", "0", str(tone)]
        make_clip = [
            *generate, "color=c=blue:s=64x36:d=1", "-f", "lavfi", "-i", tone_source,
            "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(clip),
        ]
        return (
            _run_ffmpeg_check([make_cover, make_tone], tone, cover, EXPECTED_MP3)
            or _run_ffmpeg_check([make_clip], clip, cover, EXPECTED_MP4)
        )


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
