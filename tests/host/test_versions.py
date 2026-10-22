"""Version numbers and names that must agree across the host, the installer and the extension.

The host version lives in ytmp3_host.__version__; pyproject.toml and the installer
repeat it, and the extension's MIN_HOST_VERSION must never ask for a host that does
not exist yet (every user would be told to update to a version they can't get).
"""

import json
import re
import sys
import tomllib
from pathlib import Path

from ytmp3_host import MP3, __version__, config, github, updater
from ytmp3_host.github import HOST_REPO
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


# --- names the installer, the release and the updater must agree on ---------------------

INSTALLER_SCRIPT = REPO_ROOT / "packaging" / "installer" / "mp3_installer.iss"


def iss_setting(pattern: str) -> str:
    match = re.search(pattern, INSTALLER_SCRIPT.read_text(encoding="utf-8"), flags=re.MULTILINE)
    assert match, f"{pattern!r} not found in the installer script"
    return match.group(1)


def test_installer_file_name_is_the_asset_the_updater_downloads() -> None:
    # GitHub renames assets with spaces, so the name must not have any
    name = iss_setting(r"^OutputBaseFilename=(\S+)\s*$")
    assert f"{name}.exe" == updater.INSTALLER_ASSET


def test_installer_version_folder_is_the_one_config_recognizes() -> None:
    folder = iss_setting(r'^#define\s+VersionDir\s+"([^"\\]+)\\"')
    assert folder == config.VERSIONS_DIR_NAME


def test_the_extension_links_the_installer_the_updater_downloads() -> None:
    background = (REPO_ROOT / "extension" / "background.js").read_text(encoding="utf-8")
    url = re.search(r"^const INSTALLER_URL = '([^']+)';", background, flags=re.MULTILINE)
    assert url, "INSTALLER_URL not found in background.js"
    assert url.group(1) == f"{github.GITHUB_URL}/{HOST_REPO}/releases/latest/download/{updater.INSTALLER_ASSET}"


def test_the_installer_runs_the_self_check_the_host_knows() -> None:
    assert f"'{MP3.SELF_CHECK_ARG}'" in INSTALLER_SCRIPT.read_text(encoding="utf-8")


# The permissions of the extension 1.x users have. Firefox holds back an update that asks
# for a new one until the user approves it, so users who never do would stay on 1.x.
# Ask for a new permission at runtime (optional_permissions) instead.
V1_PERMISSIONS = {"contextMenus", "nativeMessaging", "notifications", "activeTab", "tabs", "storage"}


def test_the_extension_asks_for_no_new_permissions() -> None:
    manifest = json.loads((REPO_ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["permissions"]) <= V1_PERMISSIONS


def test_release_checksum_file_is_the_one_the_updater_reads(tmp_path: Path) -> None:
    sys.path.insert(0, str(REPO_ROOT / "packaging"))
    try:
        import write_checksums
    finally:
        sys.path.pop(0)
    installer = tmp_path / updater.INSTALLER_ASSET
    installer.write_bytes(b"installer bytes")
    write_checksums.main([installer])
    sums = (tmp_path / updater.HOST_SUMS).read_text(encoding="utf-8")
    assert updater.parse_checksums(sums) == {installer.name: updater.sha256_file(installer)}
