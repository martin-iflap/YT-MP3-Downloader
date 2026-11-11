"""Starting processes that outlive the host (the host's installer, the player for `play`).

Its own module so that `play` does not load the updater (urllib, ssl, zipfile: ~60 modules).
"""

import logging
import subprocess
import tempfile

logger = logging.getLogger(__name__)


def launch_detached(command: list[str] | str) -> None:
    """Start a process that outlives this host and Firefox's native-messaging job.

    Firefox runs the host in a job object and ends the job's processes with the
    host, so the process breaks away from the job. If the job does not allow that
    (CreateProcess then fails with access denied), it is started without breaking
    away and may not survive; the extension then shows the manual download link
    once the update does not arrive.

    The process starts in the temp folder, not in this host's working directory
    (its version folder): a process's current directory can't be deleted, so the
    installer could not remove the old version folder.

    Args:
        command: The argument list, or a command line passed to Windows as it is
            (for programs like explorer.exe that parse it their own way).
    """
    def _start(creationflags: int) -> None:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
            cwd=tempfile.gettempdir(),
        )

    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        _start(flags | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    except PermissionError as e:
        logger.warning("Could not break away from the job (%s), starting %s inside it", e, command)
        _start(flags)
