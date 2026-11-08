"""Download folders, temp file cleanup and opening finished downloads."""

import ctypes
import errno
import logging
import os
import time
import uuid
from pathlib import Path

from ytmp3_host.config import DEFAULT_FOLDER
# only open_file uses it (urllib)
lazy from ytmp3_host import updater

logger = logging.getLogger(__name__)

REMOVE_ATTEMPTS = 6
REMOVE_RETRY_DELAY_S = 0.5
# temp files older than this belong to a host that was killed (e.g. Firefox closed mid-download)
STALE_TEMP_AGE_S = 3600


def ensure_default_folder() -> Path:
    """Create the default download folder if needed.

    Returns:
        The default download folder, guaranteed to exist.
    """
    folder_path = Path(DEFAULT_FOLDER).expanduser()
    folder_path.mkdir(parents=True, exist_ok=True)
    return folder_path


def resolve_folder(folder: str) -> Path | None:
    """Resolve a requested download folder.

    Args:
        folder: The requested folder, possibly empty.
    Returns:
        The folder if it is an existing directory, the (created) default folder if
        folder is empty or names the default folder, otherwise None. A relative path
        is treated as None.
    """
    folder = folder.strip()
    if not folder:
        return ensure_default_folder()
    folder_path = Path(folder).expanduser()
    if not folder_path.is_absolute():
        return None
    if folder_path.is_dir():
        return folder_path.resolve()
    if folder_path == Path(DEFAULT_FOLDER).expanduser():
        return ensure_default_folder()
    return None


def folder_is_writable(folder: Path) -> bool:
    """Whether a file can be created in folder.

    An existing folder can still refuse it: protected folders like C:\\ or Program Files,
    Windows Security's Controlled folder access, read-only drives. yt-dlp would then fail
    with "Permission denied", which looks like a broken yt-dlp. The probe is opened with
    O_TEMPORARY (FILE_FLAG_DELETE_ON_CLOSE), so Windows deletes it when the handle closes,
    even when the host is killed first. That also needs delete access, which a download
    needs too (yt-dlp renames its .part file, and the temp files are removed).

    Args:
        folder: An existing folder.
    Returns:
        True if a file could be created in folder.
    """
    probe = folder / f"_ytmp3_{uuid.uuid4().hex[:12]}.probe"
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_TEMPORARY)
    except OSError as e:
        logger.warning("Can't write to %s: %s", folder, e)
        return False
    os.close(fd)
    return True


def remove_temp_files(temp_stem: Path) -> None:
    """Remove every file belonging to one download (temp MP3, thumbnail, partials).
    After a kill, Windows keeps the files locked until the yt-dlp child has fully
    exited, so locked files are retried a few times. Failures are logged, not raised.

    Args:
        temp_stem: The temp output path without extension.
    """
    for f in temp_stem.parent.glob(f"{temp_stem.name}.*"):
        for attempt in range(1, REMOVE_ATTEMPTS + 1):
            try:
                f.unlink(missing_ok=True)
                break
            except PermissionError as e:
                if attempt == REMOVE_ATTEMPTS:
                    logger.error("Could not delete locked file %s: %s", f, e)
                else:
                    time.sleep(REMOVE_RETRY_DELAY_S)
            except OSError as e:
                logger.error("Error deleting file %s: %s", f, e)
                break


def remove_stale_temp_files(folder: Path) -> None:
    """Remove temp files that killed hosts left in folder.
    When Firefox closes during a download it kills the host, so download_audio's
    cleanup never runs. Files younger than STALE_TEMP_AGE_S may belong to a running
    download and are kept; locked files are skipped. Failures are logged, not raised.

    Args:
        folder: The download folder.
    """
    cutoff = time.time() - STALE_TEMP_AGE_S
    for f in folder.glob("_ytmp3_*.*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                logger.info("Removed stale temp file %s", f)
        except OSError as e:
            logger.warning("Could not remove stale temp file %s: %s", f, e)


def downloaded_mp3(path: str) -> Path | None:
    """The file a `show_in_folder` or `play` request names, if it is an existing MP3.
    Only MP3s: play opens the file in its default app, which must never run a program.
    Args:
        path: The absolute path the extension got from the download's success response.
    Returns:
        The file, or None if it is not an absolute path to an existing .mp3 file.
    """
    file = Path(path)
    return file if file.is_absolute() and file.suffix.lower() == ".mp3" and file.is_file() else None


def show_in_folder(file: Path) -> None:
    """Open the folder of file in Explorer with file selected (an open window of it is reused).
    Uses the shell API instead of `explorer.exe /select,<path>`, which splits the path
    at commas, and song titles often have them.

    Raises:
        OSError: If the shell could not open the folder.
    """
    shell32 = ctypes.windll.shell32
    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
    shell32.ILFree.argtypes = [ctypes.c_void_p]
    shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]
    ctypes.windll.ole32.CoInitialize(None)  # the shell API needs COM; a no-op if this thread has it
    pidl = shell32.ILCreateFromPathW(str(file))
    if not pidl:
        raise OSError(errno.ENOENT, "Could not find the file", str(file))
    try:
        result = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
    finally:
        shell32.ILFree(pidl)
    if result != 0:
        raise OSError(0, f"Explorer could not open the folder (0x{result & 0xFFFFFFFF:08X})", str(file.parent))


def open_file(file: Path) -> None:
    """Open file in its default app (play an MP3).
    Through explorer.exe, which hands it to the running shell: an app this host
    started itself would belong to Firefox's native-messaging job and be ended with
    the host. The path is quoted (Windows paths can't contain quotes).
    Raises:
        OSError: If explorer.exe could not be started.
    """
    updater.launch_detached(f'explorer.exe "{file}"')
