"""Paths to the bundled binaries, the window icon and the default download folder.

Installed (PyInstaller build), everything sits next to mp3.exe. In a source
checkout the binaries come from host/bin/ (placed there manually) and the icon from
packaging/assets/.
"""

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    BIN_DIR = Path(sys.executable).parent
    ICON_DIR = BIN_DIR
else:
    # host/src/ytmp3_host/config.py -> the repo root is parents[3]
    REPO_ROOT = Path(__file__).resolve().parents[3]
    BIN_DIR = REPO_ROOT / "host" / "bin"
    ICON_DIR = REPO_ROOT / "packaging" / "assets"

FFMPEG_PATH = BIN_DIR / "ffmpeg.exe"
YT_DLP_PATH = BIN_DIR / "yt-dlp.exe"
ICON_PATH = ICON_DIR / "mp3.ico"
DEFAULT_FOLDER = Path.home() / "Documents" / "YT-MP3-Downloads"
