from src.mp3.config import FFMPEG_PATH, YT_DLP_PATH, ICON_PATH, DEFAULT_FOLDER
from tkinter import filedialog
import customtkinter as ctk
from pathlib import Path
from PIL import Image
import subprocess
import threading
import argparse
import logging
import time
import re
import os

# ----------------------------------------   Logging   --------------------------------------------
logging.basicConfig(filename="MP3_Customtkinter.log", # use absolute path for log files
                    level=logging.ERROR,
                    format="%(asctime)s [%(levelname)s] %(message)s")
# -----------------------------------------   Functions   ------------------------------------------
def safe_filename(name) -> str:
    """Sanitize the filename by removing invalid characters"""
    if not isinstance(name, str) or not name.strip():
        return "Youtube MP3"
    return re.sub(r'[\\/*?:"<>|]', "", name.strip())

def choose_folder() -> None:
    """Open a folder selection dialog and update the folder_var
     - set to DEFAULT_FOLDER initially if no folder is selected
    """
    folder = filedialog.askdirectory(initialdir=str(DEFAULT_FOLDER))
    if folder:
        folder_var.set(folder)

def send_current_folder() -> None:
    """Write the current folder path to state.json for the main app to read"""
    try:
        with open("state.json", "w", encoding="utf-8") as f:
            f.write(f'{{"current_folder": "{folder_var.get()}"}}')
    except Exception as e:
        logging.error(f"❌ Error writing state.json: {e}")

def download_audio() -> None:
    """Start the audio download in a separate thread to allow progress bar updates"""
    thread = threading.Thread(target=_download_audio_thread, daemon=True)
    thread.start()

def _download_audio_thread() -> None:
    """Download audio from the provided YouTube URL and save it as an MP3 file
     - Uses: yt-dlp and ffmpeg for downloading and conversion
     - Adds: thumbnail to the mp3 file and crops it
           - artist metadata if filename contains '-' else <unknown>
    """
    url = url_var.get().strip()
    folder = folder_var.get().strip()
    filename = filename_var.get().strip() or "Youtube MP3"
    artist = "<unknown>"

    if not url or "youtube.com" not in url:
        root.after(0, lambda: status_label.configure(
            text="❌ Enter valid YouTube URL", text_color="red"))
        return

    if not folder:
        root.after(0, lambda: status_label.configure(
            text="❌ Choose folder", text_color="red"))
        return

    filename = safe_filename(filename)

    if "-" in filename:
        parts = filename.split("-")
        artist = parts[0].strip()

    mp3_check = Path(folder) / f"{filename}.mp3"
    if mp3_check.exists():
        root.after(0, lambda: status_label.configure(
            text="❌ This file already exists!", text_color="red"))
        return

    output_template = str(Path(folder) / f"{filename}.%(ext)s")

    if not FFMPEG_PATH.exists():
        root.after(0, lambda: status_label.configure(
            text="❌ ffmpeg.exe not found!", text_color="red"))
        return

    try:
        root.after(0, lambda: progress_bar.pack(pady=10))
        root.after(0, lambda: progress_bar.set(0))
        root.after(0, lambda: status_label.configure(text="⏳ Downloading...", text_color="white"))
        root.after(0, lambda: main_button.configure(text="Cancel", command=cancel_download))

        root.download_cancelled = False
        root.current_process = None
        process = subprocess.Popen([
            str(YT_DLP_PATH),
            "--no-playlist",
            "--concurrent-fragments", "3",
            "-x", "--audio-format", "mp3",
            "--write-thumbnail",
            "--convert-thumbnails", "jpg",
            "--ffmpeg-location", str(FFMPEG_PATH),
            "--newline",
            "-o", output_template,
            url
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        root.current_process = process

        for line in process.stdout:
            match = re.search(r'(\d+\.?\d*)%', line)
            if match:
                try:
                    percent = float(match.group(1)) / 100.0
                    root.after(0, lambda p=percent: progress_bar.set(p))
                except ValueError:
                    pass

        process.wait()
        if root.download_cancelled:
            return

        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, process.args)
        root.current_process = None

        mp3_file = Path(folder) / f"{filename}.mp3"
        thumb_file = Path(folder) / f"{filename}.jpg"
        embed_thumbnail(mp3_file, thumb_file, artist)

        root.after(0,lambda: status_label.configure(text="✅ MP3 Downloaded successfully", text_color="green"))
        root.after(0,lambda: main_button.configure(text="Next", command=reset_fields))
    except subprocess.CalledProcessError as e:
        msg = f"❌ Error during download"
        root.after(0, lambda: status_label.configure(
            text=msg, text_color="red"))
        logging.error(f"{msg}: {e}")
    except FileNotFoundError as e:
        msg = f"❌ yt-dlp or ffmpeg not found"
        root.after(0, lambda: status_label.configure(
            text=msg, text_color="red"))
        logging.error(f"{msg}: {e}")
    except Exception as e:
        msg = f"❌ Unknown error occurred"
        root.after(0, lambda: status_label.configure(
            text=msg, text_color="red"))
        logging.error(f"{msg}: {e}")
    finally:
        root.after(0, lambda: progress_bar.pack_forget())

        if hasattr(root, "close_var") and root.close_var:
            def safe_close():
                """Close the GUI window safely after a delay"""
                try:
                    root.quit()
                    root.destroy()
                except Exception as ex:
                    logging.warning(f"❌ Window already closed: {ex}")
            root.after(2500, safe_close)

def embed_thumbnail(mp3_file: Path, thumb_file: Path, artist: str) -> None:
    """Crop the thumbnail 1:1 and embed it into the MP3 file using ffmpeg
     - create a temp_mp3 file during the process to avoid corruption
    """
    folder = mp3_file.parent
    filename = mp3_file.stem

    if thumb_file.exists() and mp3_file.exists():
        try:
            img = Image.open(thumb_file)
            width, height = img.size
            size = min(width, height)
            left = (width - size) // 2
            top = (height - size) // 2
            img_cropped = img.crop((left, top, left + size, top + size))
            img_cropped.save(thumb_file)

            temp_mp3 = Path(folder) / f"{filename}_temp{os.getpid()}.mp3"
            subprocess.run([
                str(FFMPEG_PATH),
                "-i", str(mp3_file),
                "-i", str(thumb_file),
                "-map", "0:a",
                "-map", "1:0",
                "-c", "copy",
                "-id3v2_version", "3",
                "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)",
                "-metadata", f"artist={artist}",
                str(temp_mp3)
            ], check=True)

            mp3_file.unlink()
            temp_mp3.rename(mp3_file)
            thumb_file.unlink()

        except Exception as e:
            logging.error(f"❌ Error with thumbnail creation: {e}")
            temp_mp3 = Path(folder) / f"{filename}_temp{os.getpid()}.mp3"
            if temp_mp3.exists():
                temp_mp3.unlink()
            raise
    else:
        logging.warning("⚠️ Thumbnail or MP3 Not Found")
        return

def cancel_download() -> None:
    """Cancel the ongoing download process from the root.current_process attribute
     - use a helper function to clean up partial downloads and another one to reset the UI
    """
    root.download_cancelled = True
    if hasattr(root, "current_process") and root.current_process and root.current_process.poll() is None:
        try:
            root.current_process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            root.current_process.kill()
            root.current_process.wait()
        root.current_process = None

    def _update_ui():
        status_label.configure(text="❌ Download canceled", text_color="red")
        main_button.configure(text="Next", command=reset_fields)
        progress_bar.pack_forget()
    root.after(0, _update_ui)

    folder = folder_var.get().strip()
    filename = safe_filename(filename_var.get().strip() or "Youtube MP3")
    if folder and filename:
        def _cleanup_thread():
            cleanup_partial_downloads(Path(folder), filename)
        threading.Thread(target=_cleanup_thread, daemon=True).start()

def cleanup_partial_downloads(folder: Path, filename: str) -> None:
    """Remove any partial download files from yt-dlp after cancellation"""
    folder_path = Path(folder)
    logging.info(f"📁 Cleanup folder: {folder_path}")
    logging.info(f"🔍 Looking for filename: {filename}")

    files_to_delete = [
        folder_path / f"{filename}.part",
        folder_path / f"{filename}.*.part",
        folder_path / f"{filename}.ytdl",
        folder_path / f"{filename}.jpg",
        folder_path / f"{filename}.webp",
        folder_path / f"{filename}_temp*.mp3",
        folder_path / f"{filename}.mp3",
        ]

    time.sleep(10)
    for f in files_to_delete:
        if f.exists():
            logging.info(f"🗑️ Deleting file: {f}")
            for attempt in range(3):
                try:
                    f.unlink()
                    logging.info(f"Unlinked partial file: {f}")
                    break
                except PermissionError:
                    if attempt < 2:
                        logging.info(f"⏳ Retry {attempt + 1}/5 (file locked)")
                        time.sleep(0.5)
                except Exception as e:
                    logging.error(f"Error deleting file {f}: {e}")
                    break

def reset_fields() -> None:
    """Reset input entries for a new download with a helper function"""
    def _reset():
        url_entry.delete(0, 'end')
        name_entry.delete(0, 'end')
        status_label.configure(text="")
        main_button.configure(text="Download MP3", command=download_audio)
    root.after(0, _reset)

def download_audio_headless(url: str, folder: str, filename: str) -> None:
    """Headless download function for auto mode
     - download audio without GUI interaction
    """
    artist = "<unknown>"
    if "-" in filename:
        parts = filename.split("-")
        artist = parts[0].strip()

    mp3_check = Path(folder) / f"{filename}.mp3" # check this
    if mp3_check.exists():
        logging.error("❌ File with this name already exists!")
        sys.exit(1)

    output_template = str(Path(folder) / f"{filename}.%(ext)s")

    if not FFMPEG_PATH.exists():
        logging.error("❌ ffmpeg.exe not found!")
        sys.exit(1)

    try:
        subprocess.run([ # edit this to work
            str(YT_DLP_PATH),
            "--no-playlist",
            "--concurrent-fragments", "3",
            "-x", "--audio-format", "mp3",
            "--write-thumbnail",
            "--convert-thumbnails", "jpg",
            "--ffmpeg-location", str(FFMPEG_PATH),
            "--newline",
            "-o", output_template,
            url
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, timeout=300, check=True)

        mp3_file = Path(folder) / f"{filename}.mp3"
        thumb_file = Path(folder) / f"{filename}.jpg"
        embed_thumbnail(mp3_file, thumb_file, artist)
    except FileNotFoundError as e:
        logging.error(f"❌ yt-dlp or ffmpeg not found: {e}")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        logging.error(f"❌ Error while downloading: {e}")
        sys.exit(1)
    except Exception as e:
        logging.error(f"❌ Unknown error: {e}")
        sys.exit(1)

def download(url: str, folder: str) -> None:
    """Perform headless download with auto-fetched title"""
    if not url:
        print("Error: --auto requires --url")
        exit(1)

    logging.info(f"url: {url}")

    if folder:
        input_folder = Path(folder).resolve()
        if not input_folder.is_dir():
            logging.warning("Provided folder not found, using default")
            input_folder = DEFAULT_FOLDER
    else:
        input_folder = DEFAULT_FOLDER

    try:
        logging.info("Fetching title for auto mode...")
        fetched_title = subprocess.run([
            str(YT_DLP_PATH),
            "--no-playlist",
            "--get-title",
            input_url
        ], capture_output=True, text=True, check=True,
            timeout=15, stdin=subprocess.DEVNULL)
        title = safe_filename(fetched_title.stdout.strip() or "Youtube MP3")
        logging.info(f"title: {title}")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        title = "Youtube MP3"
        logging.warning("Failed to fetch title, using default.")

    download_audio_headless(input_url, input_folder, title)
    logging.info("Download completed in auto mode.")
    exit(0)

def download_gui(url: str, folder: str, close: bool) -> None:
    """Launch the GUI with pre-filled URL and folder if provided"""
    if url:
        url_var.set(url)
    if folder:
        f_path = Path(folder).resolve()
        if f_path.is_dir():
            folder_var.set(str(f_path))
        else:
            logging.warning(f"Specified folder not found: {f_path}, using default")
            folder_var.set(str(DEFAULT_FOLDER))
    if close:
        root.close_var = True
    root.mainloop()
# -----------------------------------------   GUI Setup   ------------------------------------------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

# Create main window
root = ctk.CTk()
root.title("YouTube MP3 Converter")
root.geometry("650x430")
root.current_process = None

# Window icon
try:
    root.iconbitmap(str(ICON_PATH))
except Exception as exc:
    logging.error("⚠️ Icon not found : %s", exc)

# Variables
url_var = ctk.StringVar()
folder_var = ctk.StringVar(value=str(DEFAULT_FOLDER))
filename_var = ctk.StringVar()

# Visual container to match extension look: dark background + rounded card
main_frame = ctk.CTkFrame(root, fg_color="#17181a", corner_radius=12)
main_frame.pack(fill='both', expand=True, padx=14, pady=14)

# Layout (pack into main_frame to preserve visual grouping)
ctk.CTkLabel(main_frame, text="YouTube URL:", font=("Arial", 16)).pack(in_=main_frame, pady=(12, 6))
url_entry = ctk.CTkEntry(main_frame, textvariable=url_var, width=580, height=35, font=("Arial", 14))
url_entry.pack(in_=main_frame)

ctk.CTkLabel(main_frame, text="Folder:", font=("Arial", 16)).pack(in_=main_frame, pady=(12, 6))
folder_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
folder_frame.pack(in_=main_frame)
ctk.CTkEntry(folder_frame, textvariable=folder_var, width=450, height=35, font=("Arial", 14)).pack(side="left", padx=5)
ctk.CTkButton(folder_frame, text="Choose", command=choose_folder, width=120, height=35, font=("Arial", 14)).pack(side="left")

ctk.CTkLabel(main_frame, text="Filename:", font=("Arial", 16)).pack(in_=main_frame, pady=(12, 6))
name_entry = ctk.CTkEntry(main_frame, textvariable=filename_var, width=580, height=35, font=("Arial", 14))
name_entry.pack(in_=main_frame)

main_button = ctk.CTkButton(
        main_frame,
        text="Download MP3",
        command=download_audio,
        fg_color="#26282a",            # dark pill background
        hover_color="#1f2022",
        text_color="#f3f4f6",
        corner_radius=999,              # pill shape
        border_width=1,
        border_color="#555555",     # subtle border
        font=("Arial", 15),
        width=220,
        height=45)
main_button.pack(in_=main_frame, pady=20)

status_label = ctk.CTkLabel(main_frame, text="", font=("Arial", 14))
status_label.pack(in_=main_frame)

progress_bar = ctk.CTkProgressBar(main_frame, width=580, height=15)
# -----------------------------------------   Main   ------------------------------------------
def main():
    """Main function to parse arguments and execute actions"""
    parser = argparse.ArgumentParser(description="YouTube to MP3 Converter GUI")
    parser.add_argument("action", choices=["download", "download_gui", "get_current_folder", "select_folder"],
                        nargs=1, default="download_gui", help="Action to perform")
    parser.add_argument("--url", type=str, help="YouTube video URL")
    parser.add_argument("--folder", type=str, help="Override default folder")
    parser.add_argument("--close", action="store_true", help="Close the GUI after download")
    args = parser.parse_args()

    actions = {
        "download": lambda r: download(a.url, a.folder),
        "download_gui": lambda r: download_gui(a.url, a.folder, a.close),
        "select_folder": lambda r: (choose_folder(), send_current_folder()),
        "get_current_folder": lambda r: send_current_folder(),
    }

    action = args.action[0]
    handler = actions.get(action)
    try:
        handler(args)
    except Exception as excep:
        logging.error(f"❌ Unhandled exception in main: {excep}")

def just_gui_test():
    """Test the gui looks"""
    download_gui("", "", False)

if __name__ == "__main__":
    root.mainloop()
