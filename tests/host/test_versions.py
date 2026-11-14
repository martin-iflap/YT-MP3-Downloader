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

from ytmp3_host import MP3, __version__, config, core, github, updater
from ytmp3_host.github import HOST_REPO, LOOKUP_TIMEOUT_S
from ytmp3_host.health import VERSION_TIMEOUT_S, is_outdated, parse_version
from ytmp3_host.naming import MAX_FILENAME_CHARS, MAX_PATH_CHARS

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


def test_the_installer_runs_the_self_check_and_the_yt_dlp_install_the_host_knows() -> None:
    script = INSTALLER_SCRIPT.read_text(encoding="utf-8")
    assert f"'{MP3.SELF_CHECK_ARG}'" in script
    assert f"'{MP3.INSTALL_YT_DLP_ARG}'" in script


def test_the_installer_knows_the_yt_dlp_folder_and_the_uninstaller_removes_it() -> None:
    # the folder sits in {app}, which is config.INSTALL_DIR in the installed layout
    folder = iss_setting(r'^#define\s+YtDlpDir\s+"([^"]+)"')
    assert (folder, config.YT_DLP_DIR.parent) == (config.YT_DLP_DIR.name, config.INSTALL_DIR)
    uninstall = INSTALLER_SCRIPT.read_text(encoding="utf-8").split("[UninstallDelete]")[1].split("\n[")[0]
    assert 'Type: filesandordirs; Name: "{app}\\{#YtDlpDir}"' in uninstall


def test_the_extension_and_the_installer_use_the_same_identifiers() -> None:
    manifest = json.loads((REPO_ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
    extension_id = manifest["browser_specific_settings"]["gecko"]["id"]
    background = (REPO_ROOT / "extension" / "background.js").read_text(encoding="utf-8")
    host_name = re.search(r"^const NATIVE_HOST_NAME = '([^']+)';", background, flags=re.MULTILINE)
    assert host_name, "NATIVE_HOST_NAME not found in background.js"
    assert iss_setting(r'^#define\s+ExtensionID\s+"([^"]+)"') == extension_id
    assert iss_setting(r'^#define\s+NativeHostName\s+"([^"]+)"') == host_name.group(1)
    dev_manifest = json.loads((REPO_ROOT / "packaging" / "app_manifest.dev.json").read_text(encoding="utf-8"))
    assert (dev_manifest["name"], dev_manifest["allowed_extensions"]) == (host_name.group(1), [extension_id])


def action_timeouts() -> str:
    background = (REPO_ROOT / "extension" / "background.js").read_text(encoding="utf-8")
    block = re.search(r"const ACTION_TIMEOUTS_MS = \{(.*?)};", background, flags=re.DOTALL)
    assert block, "ACTION_TIMEOUTS_MS not found in background.js"
    return block.group(1)


def test_every_action_the_extension_sends_exists_in_the_host() -> None:
    # the host treats an unknown action as a download
    actions = re.findall(r"^\s*(\w+):", action_timeouts(), flags=re.MULTILINE)
    assert actions and set(actions) <= set(MP3.ACTIONS)


def timeout_s(action: str) -> int:
    minutes = re.search(rf"^\s*{action}: (\d+) \* 60 \* 1000", action_timeouts(), flags=re.MULTILINE)
    assert minutes, f"the {action} timeout not found in ACTION_TIMEOUTS_MS"
    return int(minutes.group(1)) * 60


# an update waits for another host's update, checks the installed tool, looks up the tag and
# fetches the checksums; each download may take its deadline plus a last read, then a test-run
UPDATE_START_S = updater.LOCK_TIMEOUT_S + VERSION_TIMEOUT_S + LOOKUP_TIMEOUT_S + updater.SOCKET_TIMEOUT_S
YT_DLP_DOWNLOAD_S = updater.YT_DLP_DOWNLOAD_S + updater.SOCKET_TIMEOUT_S + VERSION_TIMEOUT_S
# the onedir build fails its test-run, so the single-file exe is checked, then downloaded too
YT_DLP_UPDATE_S = UPDATE_START_S + YT_DLP_DOWNLOAD_S + VERSION_TIMEOUT_S + YT_DLP_DOWNLOAD_S
DENO_INSTALL_S = UPDATE_START_S + updater.DENO_DOWNLOAD_S + updater.SOCKET_TIMEOUT_S + VERSION_TIMEOUT_S


def test_the_extension_waits_longer_than_the_slowest_download() -> None:
    worst_case = 2 * (core.DOWNLOAD_TIMEOUT_S + core.EMBED_TIMEOUT_S) + YT_DLP_UPDATE_S + DENO_INSTALL_S
    assert timeout_s("download") > worst_case


def test_the_extension_waits_longer_than_the_slowest_yt_dlp_update() -> None:
    assert timeout_s("update_yt_dlp") > YT_DLP_UPDATE_S


def test_the_extension_waits_longer_than_the_slowest_get_version() -> None:
    seconds = re.search(r"^\s*get_version: (\d+) \* 1000", action_timeouts(), flags=re.MULTILINE)
    assert seconds, "the get_version timeout not found in ACTION_TIMEOUTS_MS"
    # the cleanup's version check, then host_status's, with the GitHub lookups running alongside
    worst_case = updater.CLEANUP_VERSION_TIMEOUT_S + max(VERSION_TIMEOUT_S, LOOKUP_TIMEOUT_S)
    assert int(seconds.group(1)) > worst_case


def test_the_sidebar_limits_the_filename_like_the_host() -> None:
    naming = (REPO_ROOT / "extension" / "naming.js").read_text(encoding="utf-8")
    limits = dict(re.findall(r"^const (MAX_FILENAME_CHARS|MAX_PATH_CHARS) = (\d+);", naming, flags=re.MULTILINE))
    assert limits == {"MAX_FILENAME_CHARS": str(MAX_FILENAME_CHARS), "MAX_PATH_CHARS": str(MAX_PATH_CHARS)}


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
