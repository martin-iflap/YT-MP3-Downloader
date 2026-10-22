"""Version numbers that must agree across the host, the installer and the extension.

The host version lives in ytmp3_host.__version__; pyproject.toml and the installer
repeat it, and the extension's MIN_HOST_VERSION must never ask for a host that does
not exist yet (every user would be told to update to a version they can't get).
"""

import re
import tomllib
from pathlib import Path

from ytmp3_host import __version__
from ytmp3_host.health import is_outdated, parse_version

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_host_version_is_major_minor_patch() -> None:
    version = parse_version(__version__)
    assert version is not None and len(version) == 3, __version__


def test_pyproject_version_matches() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        assert tomllib.load(f)["project"]["version"] == __version__


def test_installer_version_matches() -> None:
    script = (REPO_ROOT / "packaging" / "installer" / "mp3_installer.iss").read_text(encoding="utf-8")
    match = re.search(r'^#define\s+MyAppVersion\s+"([^"]+)"', script, flags=re.MULTILINE)
    assert match, "MyAppVersion not found in the installer script"
    assert match.group(1) == __version__


def test_extension_requires_no_newer_host_than_this_one() -> None:
    background = (REPO_ROOT / "extension" / "background.js").read_text(encoding="utf-8")
    match = re.search(r"^const MIN_HOST_VERSION = '([^']+)';", background, flags=re.MULTILINE)
    assert match, "MIN_HOST_VERSION not found in background.js"
    assert parse_version(match.group(1)) is not None
    assert is_outdated(__version__, match.group(1)) is False
