from pathlib import Path
import sys


if getattr(sys, 'frozen', False):
    SCRIPT_DIR = Path(sys.executable).parent
else:
    SCRIPT_DIR = Path(__file__).parent

FFMPEG_PATH = SCRIPT_DIR / "ffmpeg.exe" # for .exe no "ffmpeg-7.1.1-essentials_build" / "ffmpeg-7.1.1-essentials_build"/ "bin" /
YT_DLP_PATH = SCRIPT_DIR.parent.parent / "yt-dlp.exe" # for .exe no .parent
ICON_PATH = SCRIPT_DIR.parent.parent / "ico" / "mp3.ico" # for .exe no .parent and "ico" /
DEFAULT_FOLDER = Path.home() / "Documents" / "Martin"