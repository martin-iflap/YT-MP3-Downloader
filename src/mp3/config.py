from pathlib import Path
import sys


if getattr(sys, 'frozen', False):
    SCRIPT_DIR = Path(sys.executable).parent
else:
    SCRIPT_DIR = Path(__file__).parent

FFMPEG_PATH = SCRIPT_DIR / "ffmpeg.exe" # SCRIPT_DIR.parent.parent.parent / "ffmpeg-7.1.1-essentials_build" / "bin" / "ffmpeg.exe" for not .exe
YT_DLP_PATH = SCRIPT_DIR / "yt-dlp.exe" # SCRIPT_DIR.parent.parent.parent / "yt-dlp.exe" for not .exe
ICON_PATH = SCRIPT_DIR / "mp3.ico" # SCRIPT_DIR.parent.parent / "ico" / "mp3.ico" for not .exe
DEFAULT_FOLDER = Path.home() / "Documents" / "YT-MP3-Downloads"
