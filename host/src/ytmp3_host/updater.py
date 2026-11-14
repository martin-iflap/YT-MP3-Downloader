"""Updates and repairs: yt-dlp's onedir build is installed per release (the single-file
yt-dlp.exe only as a fallback), the host runs its installer, and deno.exe is installed
once a download needs it.

A lost connection or a killed process must never leave a broken tool behind, so
both updates follow the same rules:
- Every file comes from one release tag, so a file and its checksum always match.
- A download goes to a temp name and is checked against the SHA-256 the release
  publishes before anything uses it; a partial or damaged file is never installed.
- One update at a time: an OS file lock, which Windows releases when the process
  dies, so a killed host never leaves a stale lock.

yt-dlp's onedir build goes into a new folder per release, so nothing that runs is
touched: it is unpacked to <tag>.download\\, test-run and renamed to <tag>\\, the name
config.yt_dlp_path() looks for. The single-file yt-dlp.exe is swapped by two renames
(current -> .old, new -> current): a running exe can be renamed but not overwritten,
so this also works while another host downloads with it. A host killed between the
renames leaves only the .old file, which recover_yt_dlp() puts back, offline too.

The host cannot replace its own files while it runs, so it downloads its installer
and starts it silently, outside Firefox's job object (Firefox kills the job's
processes with the host). The installer writes each version to its own folder and
switches the Firefox manifest to it last, so a killed installation leaves the old
version working (see mp3_installer.iss).
"""

import hashlib
import http.client
import logging
import math
import msvcrt
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

from ytmp3_host import __version__, config, github
from ytmp3_host.config import DENO_PATH, INSTALLED, UPDATE_LOCK_PATH, YT_DLP_EXE, onedir_failed_tag, yt_dlp_path
from ytmp3_host.github import DENO_REPO, HOST_REPO, YT_DLP_REPO, latest_release_tag
from ytmp3_host.health import deno_version_command, is_outdated, parse_version, read_version, yt_dlp_version_command
from ytmp3_host.processes import launch_detached

logger = logging.getLogger(__name__)

YT_DLP_ASSET = "yt-dlp.exe"  # the single-file build
YT_DLP_ZIP = "yt-dlp_win.zip"  # the onedir build: yt-dlp.exe and _internal\ at its root
YT_DLP_SUMS = "SHA2-256SUMS"
# the release assets the release checklist in CLAUDE.md requires
INSTALLER_ASSET = "YT-MP3-Downloader-Setup.exe"
HOST_SUMS = "SHA256SUMS"
DENO_ASSET = "deno-x86_64-pc-windows-msvc.zip"
# one checksum per asset, in PowerShell's Get-FileHash list format ("Hash : <hex>")
DENO_SUMS = f"{DENO_ASSET}.sha256sum"
DENO_EXE = "deno.exe"  # the only file in the zip
# Inno Setup: no wizard, no message boxes (default answers), never reboot, no "This will install" prompt
INSTALLER_ARGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-")

SOCKET_TIMEOUT_S = 30  # per connect or read
YT_DLP_DOWNLOAD_S = 180  # whole download, up to two per update; the extension waits 11 min for update_yt_dlp
INSTALLER_DOWNLOAD_S = 360  # and 9 min for update_host (see ACTION_TIMEOUTS_MS for the sums)
DENO_DOWNLOAD_S = 300  # ~43 MB, during a download the user waits for
LOCK_TIMEOUT_S = 60  # waiting for another host's update
# remove_unused_yt_dlp's check: get_version runs health's own version check after it, within the
# extension's 30 s; a build too slow for it is cleaned up by a later get_version
CLEANUP_VERSION_TIMEOUT_S = 5
LOCK_POLL_S = 0.5
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
MAX_UNPACKED_BYTES = 300 * 1024 * 1024  # yt-dlp's onedir build unpacks to ~41 MB
MAX_SUMS_BYTES = 1024 * 1024
CHUNK_BYTES = 64 * 1024
# a started installer is not started again for this long (it may still be running)
INSTALL_GRACE_S = 600
# after that this host is still outdated, so the installation failed: the same installer is
# tried again only after this long (a release re-published under the same tag at once)
INSTALL_RETRY_S = 24 * 60 * 60

# installer downloads, one folder per tag
UPDATE_DIR = Path(tempfile.gettempdir()) / "ytmp3-update"
STARTED_MARKER = "started"

SHA256_LINE_RE = re.compile(r"([0-9a-fA-F]{64})\s+\*?(.+)")
SHA256_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")

# follows the redirect to GitHub's download host; honors the system proxy settings
_opener = urllib.request.build_opener()


class UpdateError(Exception):
    """An update or repair failed. The message is meant for the user."""


class OnedirUnavailable(UpdateError):
    """yt-dlp's onedir build of a release can't be installed here, for a reason the single-file
    yt-dlp.exe may not share. Not raised for a network problem or a damaged download, which the
    single-file exe would have too."""


# --- downloads ----------------------------------------------------------------


def asset_url(repo: str, tag: str, name: str) -> str:
    """The download URL of a release asset."""
    return f"{github.GITHUB_URL}/{repo}/releases/download/{urllib.parse.quote(tag)}/{urllib.parse.quote(name)}"


def download(url: str, dest: Path, deadline_s: float, max_bytes: int | None = None) -> str:
    """Download url to dest, written to disk before returning.

    Args:
        url: What to download.
        dest: Where to write it; replaced if it exists, removed again on failure.
        deadline_s: Seconds the whole download may take.
        max_bytes: Larger downloads are stopped; None for MAX_DOWNLOAD_BYTES.
    Returns:
        The SHA-256 of the downloaded bytes (lowercase hex).
    Raises:
        UpdateError: offline, an HTTP error, a dropped connection, too slow or too large.
    """
    name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
    max_bytes = max_bytes or MAX_DOWNLOAD_BYTES
    start = time.monotonic()
    digest = hashlib.sha256()
    size = 0
    try:
        with _opener.open(url, timeout=SOCKET_TIMEOUT_S) as response, dest.open("wb") as f:
            while chunk := response.read(CHUNK_BYTES):
                size += len(chunk)
                if size > max_bytes:
                    raise UpdateError(f"The download of {name} is larger than expected")
                if time.monotonic() - start > deadline_s:
                    raise UpdateError(f"The download of {name} took too long")
                digest.update(chunk)
                f.write(chunk)
            # a dropped connection can end the body early without an error
            expected_size = response.headers.get("Content-Length")
            if expected_size and expected_size.isdigit() and size != int(expected_size):
                raise UpdateError(f"Could not download {name}: the connection dropped ({size} of {expected_size} bytes)")
            f.flush()
            os.fsync(f.fileno())  # a power cut must not leave a checked file that is empty on disk
    except UpdateError:
        dest.unlink(missing_ok=True)
        raise
    # HTTPException: e.g. IncompleteRead when the connection drops mid-download
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        dest.unlink(missing_ok=True)
        reason = e.reason if isinstance(e, urllib.error.URLError) else e
        raise UpdateError(f"Could not download {name}: {reason}") from e
    logger.info("Downloaded %s (%d bytes) in %.1f s", url, size, time.monotonic() - start)
    return digest.hexdigest()


def fetch_text(url: str, max_bytes: int = MAX_SUMS_BYTES) -> str:
    """Download a small text file, e.g. a checksum list. Raises UpdateError like download()."""
    try:
        with _opener.open(url, timeout=SOCKET_TIMEOUT_S) as response:
            data = response.read(max_bytes + 1)
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        reason = e.reason if isinstance(e, urllib.error.URLError) else e
        raise UpdateError(f"Could not download {url.rsplit('/', 1)[-1]}: {reason}") from e
    if len(data) > max_bytes:
        raise UpdateError(f"{url.rsplit('/', 1)[-1]} is larger than expected")
    return data.decode("utf-8", errors="replace")


def parse_checksums(text: str) -> dict[str, str]:
    """Parse a sha256sum-style list ("<hex>  <name>" or "<hex> *<name>") into name -> lowercase hex."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        if match := SHA256_LINE_RE.fullmatch(line.strip()):
            sums[match.group(2).strip()] = match.group(1).lower()
    return sums


def expected_sha256(repo: str, tag: str, sums_asset: str, asset: str) -> str:
    """The SHA-256 a release publishes for one of its assets. Raises UpdateError if it lists none."""
    sums = parse_checksums(fetch_text(asset_url(repo, tag, sums_asset)))
    if asset not in sums:
        raise UpdateError(f"The {tag} release has no checksum for {asset}")
    return sums[asset]


def sha256_file(path: Path) -> str:
    """The SHA-256 of a file (lowercase hex)."""
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


@contextmanager
def update_lock(path: Path | None = None, timeout: float | None = None) -> Generator[None]:
    """Hold the update lock, waiting up to timeout seconds for another host to finish.

    The lock is a Windows byte-range lock on an open file: it belongs to the
    handle, so Windows drops it when the process dies, and a killed host can't
    leave a stale lock. The file itself stays.

    Args:
        path: The lock file; None for UPDATE_LOCK_PATH.
        timeout: Seconds to wait for the lock; None for LOCK_TIMEOUT_S.
    Raises:
        UpdateError: If another update still holds the lock after timeout.
    """
    path = path or UPDATE_LOCK_PATH
    timeout = LOCK_TIMEOUT_S if timeout is None else timeout
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as f:  # a+: create it, never truncate
        deadline = time.monotonic() + timeout
        while True:
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise UpdateError("Another update is still running, try again in a minute") from None
                time.sleep(LOCK_POLL_S)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def _checked_tag(repo: str) -> str:
    """The latest release tag of repo. Raises UpdateError if GitHub can't tell or the tag is not a version."""
    tag = latest_release_tag(repo)
    if tag is None:
        raise UpdateError("GitHub could not be reached")
    if parse_version(tag) is None:  # also keeps odd tags out of URLs and paths
        raise UpdateError(f"The latest release {tag!r} has no version number")
    return tag


# --- yt-dlp -------------------------------------------------------------------


def _new_path(exe: Path) -> Path:
    """Where a new yt-dlp.exe is downloaded to (still an .exe, so it can be test-run)."""
    return exe.with_name(f"{exe.stem}.download{exe.suffix}")


def _old_paths(exe: Path) -> list[Path]:
    """Previous yt-dlp.exe files kept by _swap_in, newest first."""
    return sorted(exe.parent.glob(f"{exe.stem}.old*{exe.suffix}"), key=lambda p: p.stat().st_mtime, reverse=True)


def recover_yt_dlp(exe: Path) -> None:
    """Clean up after an interrupted update. Call with the update lock held.

    - exe missing but an old copy there (killed between the two renames): the
      old copy is put back.
    - A leftover download (killed mid-download) is removed.
    - Old copies next to a working exe are removed; one that still runs in another
      host can't be, and is retried on the next update.
    """
    old = _old_paths(exe)
    if not exe.exists() and old:
        logger.warning("%s is missing, restoring %s", exe.name, old[0].name)
        old[0].replace(exe)
        old = old[1:]
    _remove_leftovers([_new_path(exe), *old])


def _remove_leftovers(paths: list[Path]) -> None:
    """Remove leftovers from a failed update."""
    for leftover in paths:
        try:
            leftover.unlink(missing_ok=True)
        except OSError as e:
            logger.info("Could not remove %s yet: %s", leftover.name, e)


def _swap_in(new: Path, exe: Path) -> None:
    """Make new the installed exe by two renames, then delete the replaced one.

    The old copy is only needed between the renames (recover_yt_dlp puts it back
    after a kill there). If another host still runs it, Windows can't delete it,
    and a later update removes it.
    """
    old = None
    if exe.exists():
        old = exe.with_name(f"{exe.stem}.old{exe.suffix}")
        if old.exists():  # recover_yt_dlp could not remove it: it still runs
            old = exe.with_name(f"{exe.stem}.old-{uuid.uuid4().hex[:8]}{exe.suffix}")
        exe.rename(old)
    try:
        new.rename(exe)
    except OSError:  # e.g. a virus scanner still holds the new file: keep the working one
        if old:
            old.rename(exe)
        raise
    if old:
        _remove_leftovers([old])


def _remove_tree(folder: Path) -> None:
    """Delete a folder; one with files still in use (e.g. another host runs them) is retried by a later call."""
    try:
        shutil.rmtree(folder)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.info("Could not remove %s yet: %s", folder.name, e)


def _remove_onedir_leftovers() -> None:
    """Remove what a onedir install killed midway leaves (<tag>.download\\ and <tag>.download.zip),
    and what _remove_build could not delete."""
    for leftover in config.YT_DLP_DIR.glob("*.download*"):
        if leftover.is_dir():
            _remove_tree(leftover)
        else:
            _remove_leftovers([leftover])


def _yt_dlp_version(exe: Path) -> str | None:
    """The version a yt-dlp prints, None if it is missing or does not run."""
    return read_version(yt_dlp_version_command(exe)) if exe.exists() else None


def _unpack(archive: Path, dest: Path) -> None:
    """Unpack a whole zip into the new folder dest.

    Raises:
        OnedirUnavailable: A bad zip, a member path outside dest (absolute, a drive or
            ".."), more than MAX_UNPACKED_BYTES, or a file that can't be written.
    """
    try:
        with zipfile.ZipFile(archive) as zf:
            members = zf.infolist()
            for member in members:
                path = PureWindowsPath(member.filename)
                if path.anchor or ".." in path.parts:
                    raise OnedirUnavailable(f"{archive.name} has a file outside its folder: {member.filename}")
            if sum(member.file_size for member in members) > MAX_UNPACKED_BYTES:
                raise OnedirUnavailable(f"{archive.name} unpacks to more than expected")
            zf.extractall(dest)
    # zlib.error and EOFError for damaged data, RuntimeError for encryption or an unknown compression
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError, EOFError, zlib.error) as e:
        raise OnedirUnavailable(f"Could not unpack {archive.name}: {e}") from e


def _install_onedir(tag: str, sums: dict[str, str]) -> str:
    """Install the onedir build of release tag as YT_DLP_DIR\\<tag>\\.

    It is unpacked to <tag>.download\\ and gets its tag name only once its yt-dlp.exe
    has run, so config.yt_dlp_path() never finds an incomplete build. Not fsynced (143
    files): a power cut can leave a <tag> folder that does not run, and the next update
    replaces it.

    Args:
        tag: The release.
        sums: The release's checksums (YT_DLP_SUMS).
    Returns:
        The version of the installed build.
    Raises:
        OnedirUnavailable: The release lists no onedir build, or it can't be unpacked,
            run or put in place.
        UpdateError: A network problem or a damaged download.
    """
    if YT_DLP_ZIP not in sums:
        raise OnedirUnavailable(f"The {tag} release has no {YT_DLP_ZIP}")
    folder = config.YT_DLP_DIR / tag
    new, archive = folder.with_name(f"{tag}.download"), folder.with_name(f"{tag}.download.zip")
    try:
        config.YT_DLP_DIR.mkdir(parents=True, exist_ok=True)
        if download(asset_url(YT_DLP_REPO, tag, YT_DLP_ZIP), archive, YT_DLP_DOWNLOAD_S) != sums[YT_DLP_ZIP]:
            raise UpdateError(f"The downloaded yt-dlp {tag} is damaged (checksum mismatch)")
        _unpack(archive, new)
        _remove_leftovers([archive])
        version = _yt_dlp_version(new / YT_DLP_EXE)
        if version is None:
            raise OnedirUnavailable(f"The downloaded yt-dlp {tag} does not run")
        if folder.exists():  # rename fails if folder exists. It shouldn't but a killed or malformed installation could leave it there.
            shutil.rmtree(folder)
        new.rename(folder)
    except OSError as e:
        raise OnedirUnavailable(f"yt-dlp {tag} could not be installed: {e}") from e
    finally:
        _remove_leftovers([archive])
        _remove_tree(new)
    return version


def _install_single_exe(tag: str, sums: dict[str, str]) -> str:
    """Install the single-file yt-dlp.exe of release tag as config.YT_DLP_PATH (the fallback).

    Args:
        tag: The release.
        sums: The release's checksums (YT_DLP_SUMS).
    Returns:
        The version of the installed exe.
    Raises:
        UpdateError: If it could not be installed; the installed one is left as it was.
    """
    exe = config.YT_DLP_PATH
    if YT_DLP_ASSET not in sums:
        raise UpdateError(f"The {tag} release has no checksum for {YT_DLP_ASSET}")
    new = _new_path(exe)
    try:
        if download(asset_url(YT_DLP_REPO, tag, YT_DLP_ASSET), new, YT_DLP_DOWNLOAD_S) != sums[YT_DLP_ASSET]:
            raise UpdateError(f"The downloaded yt-dlp {tag} is damaged (checksum mismatch)")
        version = read_version(yt_dlp_version_command(new))
        if version is None:
            raise UpdateError(f"The downloaded yt-dlp {tag} does not run")
        _swap_in(new, exe)
    except OSError as e:
        raise UpdateError(f"yt-dlp {tag} could not be installed: {e}") from e
    finally:
        _remove_leftovers([new])  # one a virus scanner holds must not replace the UpdateError
    return version


def update_yt_dlp(*, damaged: bool = False) -> str:
    """Install the latest yt-dlp release, or repair a missing or broken yt-dlp.

    Installs the release's onedir build. Only when that build can't be installed here
    (OnedirUnavailable) is the release's single-file yt-dlp.exe used instead: the
    release is written to ONEDIR_FAILED_PATH, so config.yt_dlp_path() picks that exe
    and onedir is not tried again for this release. The next release tries onedir
    first again. A network problem or a damaged download fails without the fallback,
    which would fail the same way.

    Nothing is downloaded when the yt-dlp which the host runs works and is current (e.g.
    another host updated it while this one waited for the lock): a onedir build, or
    the single-file exe of a release whose onedir build failed. A current single-file
    exe without that (from a host before onedir, or the dev layout) gets the onedir
    build, and is kept when a network problem stops that.

    Args:
        damaged: A download showed the yt-dlp which the host runs is damaged, although it
            runs `--version` (core.download_with_repair), e.g. a virus scanner removed a
            DLL of the onedir build that `--version` does not load. A current onedir
            build is then handled like one that can't be installed here: the release
            gets the single-file fallback. It is not installed again, since the scanner
            would remove the file again and every download would download it again;
            the next release tries onedir once more. Offline, the installed one is kept.
    Returns:
        The version of the yt-dlp now installed.
    Raises:
        UpdateError: If no working, current yt-dlp could be installed. The
            installed one is left as it was.
    """
    with update_lock():
        recover_yt_dlp(config.YT_DLP_PATH)
        _remove_onedir_leftovers()
        # Deno is installed once, so a killed install would otherwise keep its ~140 MB until the uninstallation
        _remove_leftovers(list(_deno_leftovers(DENO_PATH)))
        exe = yt_dlp_path()
        current = _yt_dlp_version(exe)
        try:
            tag = _checked_tag(YT_DLP_REPO)
        except UpdateError as e:
            if current:  # offline, but the installed one works (perhaps just restored)
                logger.warning("Keeping yt-dlp %s: %s", current, e)
                return current
            raise UpdateError(f"yt-dlp could not be downloaded: {e}") from e
        onedir_failed = onedir_failed_tag() == tag
        up_to_date = current is not None and is_outdated(current, tag) is False
        damaged_build = damaged and up_to_date and exe != config.YT_DLP_PATH
        if up_to_date and (exe != config.YT_DLP_PATH or onedir_failed) and not damaged_build:
            return current

        sums: dict[str, str] = {}
        try:
            sums = parse_checksums(fetch_text(asset_url(YT_DLP_REPO, tag, YT_DLP_SUMS)))
            if damaged_build:
                raise OnedirUnavailable(f"The yt-dlp {tag} onedir build is damaged")
            if not onedir_failed:
                version = _install_onedir(tag, sums)
                logger.info("Installed the yt-dlp %s onedir build (was %s)", version, current)
                return version
        except OnedirUnavailable as e:
            logger.warning("Falling back to the single-file yt-dlp %s: %s", tag, e)
            try:
                config.YT_DLP_DIR.mkdir(parents=True, exist_ok=True)
                config.ONEDIR_FAILED_PATH.write_text(tag, encoding="utf-8")
            except OSError as write_error:
                raise UpdateError(f"yt-dlp {tag} could not be installed: {write_error}") from write_error
        except UpdateError as e:
            if not up_to_date:
                raise
            # a current single-file exe from before onedir: it works, the next update tries onedir again
            logger.warning("Keeping yt-dlp %s: %s", current, e)
            return current

        single = current if exe == config.YT_DLP_PATH else _yt_dlp_version(config.YT_DLP_PATH)
        if single and is_outdated(single, tag) is False:
            return single
        version = _install_single_exe(tag, sums)
    logger.info("Installed the single-file yt-dlp %s (was %s)", version, current)
    return version


def _has_unused_yt_dlp() -> bool:
    """Whether remove_unused_yt_dlp may have something to delete (a few file checks, no lock)."""
    exe = config.YT_DLP_PATH
    return (
        len(config.onedir_builds()) > 1
        or any(p.exists() for p in (exe, _new_path(exe), config.ONEDIR_FAILED_PATH))
        or bool(_old_paths(exe))
        or any(config.YT_DLP_DIR.glob("*.download*"))
    )


def _remove_build(folder: Path) -> None:
    """Delete a onedir build no host runs.

    It is renamed to <tag>.download\\ first. A build another host still runs can't be
    renamed, so it is left whole for a later call: deleting only the files Windows allows
    would break that host's download. Whatever rmtree can't delete after the rename is a
    leftover the lookup ignores and the next cleanup or update removes.
    """
    unused = folder.with_name(f"{folder.name}.download")
    try:
        folder.rename(unused)
    except OSError as e:
        logger.info("Could not remove %s yet, it may still run: %s", folder.name, e)
        return
    _remove_tree(unused)


def remove_unused_yt_dlp() -> None:
    """Delete the yt-dlp files the host no longer runs, once the lookup finds a working onedir build.

    That is the single-file yt-dlp.exe (with its update leftovers), older onedir builds,
    the onedir-failed marker and the leftovers of a killed onedir install. While the lookup
    takes the single-file fallback instead, it deletes the onedir builds, which the lookup
    can't take (e.g. a damaged one, see update_yt_dlp), and keeps the exe and the marker.
    Run on every get_version: its answer comes from the host version Firefox uses, so an installer's
    switch to it has happened. Never in update_yt_dlp, which the installer runs while
    Firefox still uses the previous host, perhaps with that yt-dlp.exe.

    Does not wait for the update lock: while an update runs, nothing is deleted and the
    next call does it. With nothing to delete it takes a few file checks; the build's
    --version runs only when there is, for at most CLEANUP_VERSION_TIMEOUT_S. A file or
    build another host still runs can't be deleted and is retried by a later call. Never
    raises: get_version must answer.
    """
    try:
        if not _has_unused_yt_dlp():
            return
        with update_lock(timeout=0):
            exe = yt_dlp_path()
            if exe == config.YT_DLP_PATH:
                # the single-file fallback (or no build at all)
                _remove_onedir_leftovers()
                for build in config.onedir_builds():
                    _remove_build(build)
                return
            if read_version(yt_dlp_version_command(exe), CLEANUP_VERSION_TIMEOUT_S) is None:
                return  # the build is broken (or slow): the update needs what is there
            _remove_onedir_leftovers()
            for build in config.onedir_builds()[1:]:
                _remove_build(build)
            single = config.YT_DLP_PATH
            _remove_leftovers([_new_path(single), *_old_paths(single), single, config.ONEDIR_FAILED_PATH])
    except UpdateError:  # update_lock: an update is running
        logger.info("An update is running, unused yt-dlp files are removed later")
    except OSError as e:
        logger.info("Could not remove unused yt-dlp files yet: %s", e)


# --- Deno ---------------------------------------------------------------------


def _deno_leftovers(exe: Path) -> tuple[Path, Path]:
    """The zip and the unpacked exe install_deno works on; a killed installation leaves them behind."""
    return exe.with_name(f"{exe.stem}.download.zip"), _new_path(exe)


def _extract(archive: Path, member: str, dest: Path) -> None:
    """Unpack one file of a zip to dest. Raises UpdateError for a bad zip or a missing or oversized member."""
    try:
        with zipfile.ZipFile(archive) as zf:
            info = zf.getinfo(member)
            if info.file_size > MAX_DOWNLOAD_BYTES:
                raise UpdateError(f"{member} in {archive.name} is larger than expected")
            with zf.open(info) as src, dest.open("wb") as f:
                shutil.copyfileobj(src, f, CHUNK_BYTES)
                f.flush()
                os.fsync(f.fileno())
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        raise UpdateError(f"Could not unpack {member} from {archive.name}: {e}") from e


def install_deno() -> str:
    """Install Deno, the JavaScript runtime yt-dlp needs once YouTube makes it solve its challenges.
    Only called when a download failed for that reason, and never updated afterward
    (like ffmpeg). Nothing is downloaded when a working deno.exe is already there
    (e.g. another host installed it while this one waited for the lock).

    Returns:
        The version of the deno.exe now installed.
    Raises:
        UpdateError: If no working Deno could be installed.
    """
    exe = DENO_PATH
    archive, new = _deno_leftovers(exe)
    with update_lock():
        _remove_leftovers([archive, new])  # from an installation that was killed
        if exe.exists() and (version := read_version(deno_version_command(exe))):
            return version
        try:
            tag = _checked_tag(DENO_REPO)
            sums = SHA256_RE.search(fetch_text(asset_url(DENO_REPO, tag, DENO_SUMS)))
            if not sums:
                raise UpdateError(f"The Deno {tag} release has no checksum for {DENO_ASSET}")
            actual = download(asset_url(DENO_REPO, tag, DENO_ASSET), archive, DENO_DOWNLOAD_S)
            if actual != sums.group(0).lower():
                raise UpdateError(f"The downloaded Deno {tag} is damaged (checksum mismatch)")
            _extract(archive, DENO_EXE, new)
            _remove_leftovers([archive])  # frees 43 MB now; one a scanner holds goes in finally
            version = read_version(deno_version_command(new))
            if version is None:
                raise UpdateError(f"The downloaded Deno {tag} does not run")
            new.replace(exe)  # a deno.exe that is there does not run, nothing uses it
        except UpdateError as e:
            raise UpdateError(f"Deno could not be installed: {e}") from e
        finally:
            _remove_leftovers([archive, new])
    logger.info("Installed Deno %s", version)
    return version


# --- host ---------------------------------------------------------------------


def _installer(tag: str, folder: Path) -> tuple[Path, str]:
    """The verified installer of release tag in folder and its SHA-256, downloading it unless a verified copy is there."""
    installer = folder / INSTALLER_ASSET
    expected = expected_sha256(HOST_REPO, tag, HOST_SUMS, INSTALLER_ASSET)
    if installer.exists() and sha256_file(installer) == expected:
        return installer, expected
    partial = folder / f"{INSTALLER_ASSET}.download"
    actual = download(asset_url(HOST_REPO, tag, INSTALLER_ASSET), partial, INSTALLER_DOWNLOAD_S)
    if actual != expected:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"The downloaded app installer {tag} is damaged (checksum mismatch)")
    partial.replace(installer)
    return installer, expected


def update_host() -> str | None:
    """Start installing the latest host release, if it is newer than this host.

    The installer runs on after this host exits; the new version answers the
    next request. An installer started less than INSTALL_GRACE_S ago is not
    started again. One started longer ago failed (this host is still outdated),
    so the same installer is not retried until INSTALL_RETRY_S after its start;
    a release re-published with a different installer is started right away.

    Returns:
        The tag being installed, or None if this host is already the latest.
    Raises:
        UpdateError: If this host can't update itself (not installed, e.g. a
            source checkout), the installer could not be downloaded, checked
            or started, or it failed to install less than INSTALL_RETRY_S ago.
    """
    if not INSTALLED:
        raise UpdateError("Only an installed app can update itself")
    with update_lock():
        tag = _checked_tag(HOST_REPO)
        if not is_outdated(__version__, tag):
            return None

        folder = UPDATE_DIR / tag
        folder.mkdir(parents=True, exist_ok=True)
        installer, digest = _installer(tag, folder)
        marker = folder / STARTED_MARKER  # holds the SHA-256 of the installer started
        try:
            age = time.time() - marker.stat().st_mtime if marker.read_text().strip() == digest else None
        except OSError:
            age = None
        if age is not None and age < INSTALL_GRACE_S:
            logger.info("The %s installer was started recently, not starting it again", tag)
            return tag
        if age is not None and age < INSTALL_RETRY_S:
            hours = math.ceil((INSTALL_RETRY_S - age) / 3600)
            raise UpdateError(f"The {tag} update did not install, next try in about {hours} h")

        try:
            launch_detached([str(installer), *INSTALLER_ARGS, f"/LOG={folder / 'install.log'}"])
        except OSError as e:
            raise UpdateError(f"Could not start the app installer: {e}") from e
        marker.write_text(digest)
        logger.info("Started the %s installer", tag)

        for other in UPDATE_DIR.iterdir():  # downloads of older releases
            if other.is_dir() and other.name != tag:
                shutil.rmtree(other, ignore_errors=True)
    return tag


def remove_installed_downloads() -> None:
    """Delete the downloaded installers of this host's release and older ones, once this host runs.

    A release newer than this host is kept: its installer may be about to run. No
    lock is needed, since only an outdated host downloads, and only newer releases.
    Files the installer still has open (it may still be finishing) can't be deleted;
    the next call removes them.
    """
    try:
        folders = [p for p in UPDATE_DIR.iterdir() if p.is_dir()] if UPDATE_DIR.is_dir() else []
        for folder in folders:
            if not is_outdated(__version__, folder.name):
                shutil.rmtree(folder, ignore_errors=True)
        if UPDATE_DIR.is_dir() and not any(UPDATE_DIR.iterdir()):
            UPDATE_DIR.rmdir()
    except OSError as e:
        logger.info("Could not remove old installer downloads yet: %s", e)
