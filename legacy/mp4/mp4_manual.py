from pathlib import Path
import subprocess

FFMPEG_PATH: str = "../../dist/mp3/ffmpeg.exe"
YT_DLP_PATH: str = "../../dist/mp3/yt-dlp.exe"
FOLDER: str = "C:\\Users\\User\\Downloads"
FILENAME: str = "" # name of the file without extension
URL: str = "" # YouTube url to download

def main() -> None:
    """Downloads MP4 from YouTube using yt-dlp and ffmpeg."""
    output_template = str(Path(FOLDER) / f"{FILENAME}.%(ext)s")

    try:
        process = subprocess.run([
            str(YT_DLP_PATH),
            "--no-playlist",
            "--concurrent-fragments", "3",
            "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--write-thumbnail",
            "--convert-thumbnails", "jpg",
            "--ffmpeg-location", str(FFMPEG_PATH),
            "--newline",
            "-o", output_template,
            URL
        ], check=True)

        print(f"Video {FILENAME} downloaded successfully to {FOLDER}.")

    except subprocess.CalledProcessError as e:
        print(f"Error during download: {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")
if __name__ == "__main__":
    main()

    # for best quality mp4 use:
    # "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",