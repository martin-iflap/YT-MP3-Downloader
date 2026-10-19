from pathlib import Path
import sys


if getattr(sys, 'frozen', False):
    # installed: everything sits next to mp3.exe
    BIN_DIR = Path(sys.executable).parent
    ICON_DIR = BIN_DIR
else:
    # dev checkout: host/src/ytmp3_host/config.py -> repo root is parents[3]
    REPO_ROOT = Path(__file__).resolve().parents[3]
    BIN_DIR = REPO_ROOT / "host" / "bin"
    ICON_DIR = REPO_ROOT / "packaging" / "assets"

FFMPEG_PATH = BIN_DIR / "ffmpeg.exe"
YT_DLP_PATH = BIN_DIR / "yt-dlp.exe"
ICON_PATH = ICON_DIR / "mp3.ico"
DEFAULT_FOLDER = Path.home() / "Documents" / "YT-MP3-Downloads"
