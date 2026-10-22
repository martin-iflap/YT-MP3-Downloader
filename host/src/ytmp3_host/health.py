"""The host's version and the state of the tools it runs, for the `get_version` action.

The host reports facts (versions, what is installed, whether the host or yt-dlp is
behind its latest release); the extension decides what the user needs to be told
and what to repair. The yt-dlp version check and the two GitHub lookups run in
parallel, so the answer takes as long as the slowest of them.
"""

import logging
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ytmp3_host import __version__
from ytmp3_host.config import FFMPEG_PATH, INSTALL_DIR, INSTALLED, YT_DLP_PATH
from ytmp3_host.core import kill_process_tree
from ytmp3_host.github import HOST_REPO, YT_DLP_REPO, latest_release_tag

logger = logging.getLogger(__name__)

# yt-dlp.exe is a onefile build that unpacks itself before it can print anything,
# and the first run after an update is often held up by the virus scanner
VERSION_TIMEOUT_S = 20
JS_RUNTIME = "deno"
VERSION_RE = re.compile(r"v?(\d+(?:\.\d+)*)")

type Version = tuple[int, ...]


def parse_version(text: object) -> Version | None:
    """Parse a dotted version like "2.0.0", "v2.0.0" or yt-dlp's "2026.08.19".

    Args:
        text: The version text. Anything that is not a string is treated as invalid.
    Returns:
        The numeric parts, or None if text is not a version.
    """
    if not isinstance(text, str):
        return None
    match = VERSION_RE.fullmatch(text.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def is_outdated(version: str | None, latest: str | None) -> bool | None:
    """Tell whether version is older than latest. Missing parts count as 0, so "2.0" equals "2.0.0".
    A yt-dlp nightly ("2026.08.19.123456") counts as newer than the release of that day.

    Returns:
        True or False, or None if either version is missing or invalid (unknown).
    """
    current, newest = parse_version(version), parse_version(latest)
    if current is None or newest is None:
        return None
    width = max(len(current), len(newest))
    return current + (0,) * (width - len(current)) < newest + (0,) * (width - len(newest))


def yt_dlp_version_command(exe: Path | None = None) -> list[str]:
    """The command that prints the version of a yt-dlp executable (the tests swap the executable).
    Args:
        exe: The executable, e.g. a fresh download; None for the installed YT_DLP_PATH.
    """
    return [str(exe or YT_DLP_PATH), "--version"]


def read_version(command: list[str], timeout: float = VERSION_TIMEOUT_S) -> str | None:
    """Run a tool's version command and return the version it prints.

    Args:
        command: The command, e.g. yt_dlp_version_command().
        timeout: Seconds after which the whole process tree is killed.
    Returns:
        The version, or None if the tool cannot be started (e.g. a truncated
        download that is not a valid executable), exits with an error, hangs, or
        prints something that is not a version. Never raises.
    """
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as e:
        logger.error("Could not start %s: %s", command[0], e)
        return None
    with process:
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.error("%s did not print its version within %s s", command[0], timeout)
            kill_process_tree(process)  # a onefile child would keep the pipe open
            process.communicate()
            return None
    lines = output.strip().splitlines()
    version = lines[0].strip() if lines else ""
    if process.returncode != 0 or parse_version(version) is None:
        logger.error("%s --version failed (exit code %s): %r", command[0], process.returncode, output[-300:])
        return None
    return version


def find_js_runtime() -> Path | None:
    """Find the JavaScript runtime yt-dlp needs for YouTube: next to yt-dlp.exe first, then on PATH."""
    bundled = INSTALL_DIR / f"{JS_RUNTIME}.exe"
    if bundled.exists():
        return bundled
    found = shutil.which(JS_RUNTIME)
    return Path(found) if found else None


def host_status() -> dict[str, object]:
    """Collect the host version and the state of its tools.

    Returns:
        A dict for the `get_version` response:
        - version: this host's version (__version__); latest: the newest host
          release tag (None if GitHub could not be reached); outdated: whether a
          newer host is released (None when unknown); self_update: whether this
          host can install it (only an installed host, see config.INSTALLED).
        - yt_dlp: installed (the file exists), version (None if missing or
          broken), latest and outdated, like the host's.
        - ffmpeg: installed.
        - js_runtime: name and installed.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        host_lookup = pool.submit(latest_release_tag, HOST_REPO)
        yt_dlp_lookup = pool.submit(latest_release_tag, YT_DLP_REPO)
        installed = YT_DLP_PATH.exists()
        version = read_version(yt_dlp_version_command()) if installed else None
        host_latest, latest = host_lookup.result(), yt_dlp_lookup.result()

    return {
        "version": __version__,
        "latest": host_latest,
        "outdated": is_outdated(__version__, host_latest),
        "self_update": INSTALLED,
        "yt_dlp": {
            "installed": installed,
            "version": version,
            "latest": latest,
            "outdated": is_outdated(version, latest),
        },
        "ffmpeg": {"installed": FFMPEG_PATH.exists()},
        "js_runtime": {"name": JS_RUNTIME, "installed": find_js_runtime() is not None},
    }
