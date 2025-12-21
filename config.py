from pathlib import Path
import sys


if getattr(sys, 'frozen', False):
    SCRIPT_DIR = Path(sys.executable).parent
else:
    SCRIPT_DIR = Path(__file__).parent

FFMPEG_PATH = SCRIPT_DIR / "ffmpeg.exe"
YT_DLP_PATH = SCRIPT_DIR / "yt-dlp.exe"
ICON_PATH = SCRIPT_DIR / "ico" / "mp3_new.ico"
DEFAULT_FOLDER = Path.home() / "Documents" / "Martin"