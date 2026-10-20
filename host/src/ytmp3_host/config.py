"""Paths to the bundled binaries, the window icon, the log file and the default
download folder, plus the logging setup.

Installed (PyInstaller build), everything sits next to mp3.exe. In a source
checkout the binaries come from host/bin/ (placed there manually), the icon from
packaging/assets/ and the log goes to the repo root.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    BIN_DIR = Path(sys.executable).parent
    ICON_DIR = BIN_DIR
    LOG_DIR = BIN_DIR
else:
    # host/src/ytmp3_host/config.py -> the repo root is parents[3]
    REPO_ROOT = Path(__file__).resolve().parents[3]
    BIN_DIR = REPO_ROOT / "host" / "bin"
    ICON_DIR = REPO_ROOT / "packaging" / "assets"
    LOG_DIR = REPO_ROOT

FFMPEG_PATH = BIN_DIR / "ffmpeg.exe"
YT_DLP_PATH = BIN_DIR / "yt-dlp.exe"
ICON_PATH = ICON_DIR / "mp3.ico"
LOG_PATH = LOG_DIR / "mp3.log"
DEFAULT_FOLDER = Path.home() / "Documents" / "YT-MP3-Downloads"

LOG_MAX_BYTES = 1_000_000  # rotated once: mp3.log + mp3.log.1, at most ~2 MB in total
LOG_LEVEL_ENV = "YTMP3_LOG_LEVEL"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging() -> None:
    """Configure logging for the host process. Call once, at startup.

    Installed, the level is ERROR; in a source checkout it is DEBUG and the log is
    also written to stderr (Firefox shows a native host's stderr in the Browser
    Console). The YTMP3_LOG_LEVEL environment variable overrides the level, e.g.
    to debug an installed copy. The level applies to the ytmp3_host loggers;
    third-party libraries only log warnings and up, so DEBUG stays readable.
    Never logs to stdout, the native-messaging channel.
    """
    default_level = "ERROR" if FROZEN else "DEBUG"
    level_name = os.environ.get(LOG_LEVEL_ENV, default_level).strip().upper()
    level = logging.getLevelNamesMapping().get(level_name, logging.ERROR)

    # delay: the file is only created once something is logged
    handlers: list[logging.Handler] = [
        RotatingFileHandler(LOG_PATH, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8", delay=True)
    ]
    if not FROZEN:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=max(level, logging.WARNING), format=LOG_FORMAT, handlers=handlers)
    logging.getLogger("ytmp3_host").setLevel(level)
