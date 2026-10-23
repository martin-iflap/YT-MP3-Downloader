"""Updates and repairs: yt-dlp.exe is replaced in place, the host runs its installer,
and deno.exe is installed once a download needs it.

A lost connection or a killed process must never leave a broken tool behind, so
both updates follow the same rules:
- Every file comes from one release tag, so a file and its checksum always match.
- A download goes to a temp name and is checked against the SHA-256 the release
  publishes before anything uses it; a partial or damaged file is never installed.
- One update at a time: an OS file lock, which Windows releases when the process
  dies, so a killed host never leaves a stale lock.

yt-dlp.exe is swapped by two renames (current -> .old, new -> current): a running
exe can be renamed but not overwritten, so this also works while another host
downloads with it. A host killed between the renames leaves only the .old file,
which recover_yt_dlp() puts back, offline too.

The host cannot replace its own files while it runs, so it downloads its installer
and starts it silently, outside Firefox's job object (Firefox kills the job's
processes with the host). The installer writes each version to its own folder and
switches the Firefox manifest to it last, so a killed installation leaves the old
version working (see mp3_installer.iss).
"""

import hashlib
import http.client
import logging
import msvcrt
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from ytmp3_host import __version__, github
from ytmp3_host.config import DENO_PATH, INSTALLED, UPDATE_LOCK_PATH, YT_DLP_PATH
from ytmp3_host.github import DENO_REPO, HOST_REPO, YT_DLP_REPO, latest_release_tag
from ytmp3_host.health import deno_version_command, is_outdated, parse_version, read_version, yt_dlp_version_command

logger = logging.getLogger(__name__)

YT_DLP_ASSET = "yt-dlp.exe"
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
YT_DLP_DOWNLOAD_S = 180  # whole download; the extension waits 7 min for update_yt_dlp
INSTALLER_DOWNLOAD_S = 360  # and 9 min for update_host (see ACTION_TIMEOUTS_MS for the sums)
DENO_DOWNLOAD_S = 300  # ~43 MB, during a download the user waits for
LOCK_TIMEOUT_S = 60  # waiting for another host's update
LOCK_POLL_S = 0.5
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
MAX_SUMS_BYTES = 1024 * 1024
CHUNK_BYTES = 1024 * 1024
# a started installer is not started again for this long (it may still be running)
INSTALL_GRACE_S = 600

# installer downloads, one folder per tag
UPDATE_DIR = Path(tempfile.gettempdir()) / "ytmp3-update"
STARTED_MARKER = "started"

SHA256_LINE_RE = re.compile(r"([0-9a-fA-F]{64})\s+\*?(.+)")
SHA256_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")

# follows the redirect to GitHub's download host; honors the system proxy settings
_opener = urllib.request.build_opener()


class UpdateError(Exception):
    """An update or repair failed. The message is meant for the user."""


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


def update_yt_dlp() -> str:
    """Install the latest yt-dlp release, or repair a missing or broken yt-dlp.exe.
    Nothing is downloaded when the installed yt-dlp already runs and is current
    (e.g. another host updated it while this one waited for the lock).

    Returns:
        The version of the yt-dlp.exe now installed.
    Raises:
        UpdateError: If no working, current yt-dlp could be installed. The
            installed one is left as it was.
    """
    exe = YT_DLP_PATH
    with update_lock():
        recover_yt_dlp(exe)
        # Deno is installed once, so a killed install would otherwise keep its ~140 MB until the uninstall
        _remove_leftovers(list(_deno_leftovers(DENO_PATH)))
        current = read_version(yt_dlp_version_command(exe)) if exe.exists() else None
        try:
            tag = _checked_tag(YT_DLP_REPO)
        except UpdateError as e:
            if current:  # offline, but the installed one works (perhaps just restored)
                logger.warning("Keeping yt-dlp %s: %s", current, e)
                return current
            raise UpdateError(f"yt-dlp could not be downloaded: {e}") from e
        if current and is_outdated(current, tag) is False:
            return current

        new = _new_path(exe)
        try:
            expected = expected_sha256(YT_DLP_REPO, tag, YT_DLP_SUMS, YT_DLP_ASSET)
            actual = download(asset_url(YT_DLP_REPO, tag, YT_DLP_ASSET), new, YT_DLP_DOWNLOAD_S)
            if actual != expected:
                raise UpdateError(f"The downloaded yt-dlp {tag} is damaged (checksum mismatch)")
            version = read_version(yt_dlp_version_command(new))
            if version is None:
                raise UpdateError(f"The downloaded yt-dlp {tag} does not run")
            _swap_in(new, exe)
        except OSError as e:
            raise UpdateError(f"yt-dlp {tag} could not be installed: {e}") from e
        finally:
            new.unlink(missing_ok=True)
    logger.info("Installed yt-dlp %s (was %s)", version, current)
    return version


# --- Deno ---------------------------------------------------------------------


def _deno_leftovers(exe: Path) -> tuple[Path, Path]:
    """The zip and the unpacked exe install_deno works on; a killed install leaves them behind."""
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
        _remove_leftovers([archive, new])  # from an install that was killed
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


def launch_detached(command: list[str]) -> None:
    """Start a process that outlives this host and Firefox's native-messaging job.

    Firefox runs the host in a job object and ends the job's processes with the
    host, so the process breaks away from the job. If the job does not allow that
    (CreateProcess then fails with access denied), it is started without breaking
    away and may not survive; the extension then shows the manual download link
    once the update does not arrive.
    """
    def _start(creationflags: int) -> None:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )

    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        _start(flags | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    except PermissionError as e:
        logger.warning("Could not break away from the job (%s), starting %s inside it", e, command[0])
        _start(flags)


def _installer(tag: str, folder: Path) -> Path:
    """The verified installer of release tag in folder, downloading it unless a verified copy is there."""
    installer = folder / INSTALLER_ASSET
    expected = expected_sha256(HOST_REPO, tag, HOST_SUMS, INSTALLER_ASSET)
    if installer.exists() and sha256_file(installer) == expected:
        return installer
    partial = folder / f"{INSTALLER_ASSET}.download"
    actual = download(asset_url(HOST_REPO, tag, INSTALLER_ASSET), partial, INSTALLER_DOWNLOAD_S)
    if actual != expected:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"The downloaded app installer {tag} is damaged (checksum mismatch)")
    partial.replace(installer)
    return installer


def update_host() -> str | None:
    """Start installing the latest host release, if it is newer than this host.

    The installer runs on after this host exits; the new version answers the
    next request. An installer started less than INSTALL_GRACE_S ago is not
    started again.

    Returns:
        The tag being installed, or None if this host is already the latest.
    Raises:
        UpdateError: If this host can't update itself (not installed, e.g. a
            source checkout), or the installer could not be downloaded, checked
            or started.
    """
    if not INSTALLED:
        raise UpdateError("Only an installed app can update itself")
    with update_lock():
        tag = _checked_tag(HOST_REPO)
        if not is_outdated(__version__, tag):
            return None

        folder = UPDATE_DIR / tag
        folder.mkdir(parents=True, exist_ok=True)
        marker = folder / STARTED_MARKER
        if marker.exists() and time.time() - marker.stat().st_mtime < INSTALL_GRACE_S:
            logger.info("The %s installer was started recently, not starting it again", tag)
            return tag

        installer = _installer(tag, folder)
        try:
            launch_detached([str(installer), *INSTALLER_ARGS, f"/LOG={folder / 'install.log'}"])
        except OSError as e:
            raise UpdateError(f"Could not start the app installer: {e}") from e
        marker.touch()
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
            if is_outdated(__version__, folder.name) is not True:
                shutil.rmtree(folder, ignore_errors=True)
        if UPDATE_DIR.is_dir() and not any(UPDATE_DIR.iterdir()):
            UPDATE_DIR.rmdir()
    except OSError as e:
        logger.info("Could not remove old installer downloads yet: %s", e)
