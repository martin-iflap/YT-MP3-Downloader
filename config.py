from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
FFMPEG_PATH = SCRIPT_DIR / "ffmpeg-7.1.1-essentials_build" / "ffmpeg-7.1.1-essentials_build" / "bin" / "ffmpeg.exe"
YT_DLP_PATH = SCRIPT_DIR / "yt-dlp.exe"
ICON_PATH = SCRIPT_DIR / "ico" / "mp3_new.ico"
DEFAULT_FOLDER = Path.home() / "Documents" / "Martin"