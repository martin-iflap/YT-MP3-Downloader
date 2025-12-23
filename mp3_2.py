from config import FFMPEG_PATH, YT_DLP_PATH, ICON_PATH, DEFAULT_FOLDER
from native_messaging import send_message, read_message, run_native
from core import safe_filename, embed_thumbnail
from tkinter import filedialog
import customtkinter as ctk
from pathlib import Path
import subprocess
import threading
import logging
import time
import re
# ----------------------------------------   Logging   --------------------------------------------
logging.basicConfig(filename="mp3.log",
                    level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
# -----------------------------------------   Functions   ------------------------------------------
def choose_folder() -> None:
    """Open a folder selection dialog and update the folder_var
     - set to DEFAULT_FOLDER initially if no folder is selected
    """
    folder = filedialog.askdirectory(initialdir=str(DEFAULT_FOLDER))
    if folder:
        folder_var.set(folder)

def send_current_folder() -> None:
    """Send the current folder path via native messaging"""
    folder = folder_var.get().strip()
    if not folder:
        folder = str(DEFAULT_FOLDER)
    send_message({"status": "success", "folder": folder})

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
            text="❌ Choose a folder", text_color="red"))
        return

    filename = safe_filename(filename)

    if "-" in filename:
        parts = filename.split("-")
        artist = parts[0].strip()

    mp3_check = Path(folder) / f"{filename}.mp3"
    if mp3_check.exists():
        root.after(0, lambda: status_label.configure(
            text="❌ File with this filename already exists!", text_color="red"))
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

        root.after(0,lambda: status_label.configure(text="✅ MP3 downloaded successfully", text_color="green"))
        root.after(0,lambda: main_button.configure(text="Next", command=reset_fields))
    except subprocess.CalledProcessError as e:
        msg = f"❌ Error during download or conversion"
        root.after(0, lambda: status_label.configure(
            text=msg, text_color="red"))
        logging.error(f"{msg}: {e}")
    except FileNotFoundError as e:
        msg = f"❌ yt-dlp or ffmpeg not found"
        root.after(0, lambda: status_label.configure(
            text=msg, text_color="red"))
        logging.error(f"{msg}: {e}")
    except Exception as e:
        msg = f"❌ Unexpected error occurred"
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
# move cleanup to core and use it in auto mode too?
def reset_fields() -> None:
    """Reset input entries for a new download with a helper function"""
    def _reset():
        url_entry.delete(0, 'end')
        name_entry.delete(0, 'end')
        status_label.configure(text="")
        main_button.configure(text="Download MP3", command=download_audio)
    root.after(0, _reset)

def run_native_gui(url: str, folder: str) -> None:
    """Run the GUI with or without prefilled arguments from Firefox"""
    if url:
        url_var.set(url)
    if folder:
        f_path = Path(folder).resolve()
        if f_path.is_dir():
            folder_var.set(str(f_path))
        else:
            send_message({"status": "warning", "message": "Invalid folder path, using default"})
            folder_var.set(str(DEFAULT_FOLDER))
        # if close:
        #     root.close_var = True # add close reading
    root.mainloop()

# -----------------------------------------   GUI Setup   ------------------------------------------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

root = ctk.CTk()
root.title("YouTube MP3 Downloader")
root.geometry("650x430")
root.current_process = None

# Window icon
try:
    root.iconbitmap(str(ICON_PATH))
except Exception as exc:
    logging.error("⚠️ Icon not found:", exc)

# Variables
url_var = ctk.StringVar()
folder_var = ctk.StringVar(value=str(DEFAULT_FOLDER))
filename_var = ctk.StringVar()

# Layout
ctk.CTkLabel(root, text="YouTube URL:", font=("Arial", 16)).pack(pady=(20, 5))
url_entry = ctk.CTkEntry(root, textvariable=url_var, width=580, height=35, font=("Arial", 14))
url_entry.pack()

ctk.CTkLabel(root, text="Folder:", font=("Arial", 16)).pack(pady=(20, 5))
folder_frame = ctk.CTkFrame(root, fg_color="transparent")
folder_frame.pack()
ctk.CTkEntry(folder_frame, textvariable=folder_var, width=450, height=35, font=("Arial", 14)).pack(side="left", padx=5)
ctk.CTkButton(folder_frame, text="Choose", command=choose_folder, width=120, height=35, font=("Arial", 14)).pack(side="left")

ctk.CTkLabel(root, text="Filename:", font=("Arial", 16)).pack(pady=(20, 5))
name_entry = ctk.CTkEntry(root, textvariable=filename_var, width=580, height=35, font=("Arial", 14))
name_entry.pack()

main_button = ctk.CTkButton(
        root, text="Download MP3", command=download_audio,
        fg_color="#4CAF50", hover_color="#45a049", font=("Arial", 15), width=220, height=45)
main_button.pack(pady=25)

status_label = ctk.CTkLabel(root, text="", font=("Arial", 14))
status_label.pack()

progress_bar = ctk.CTkProgressBar(root, width=580, height=15)
# -----------------------------------------   Main   ------------------------------------------

def main():
    """Main function to handle backend of the downloader/converter
     - Reads and sends messages via native messaging protocol
     - Supports actions: download, download_gui, select_folder, get_default_folder
    """
    while True:
        try:
            request = read_message()
            action = request.get("action", "download_gui")

            actions = {
                "download": lambda r: run_native(r.get("url", ""), r.get("folder", "")),
                "download_gui": lambda r: run_native_gui(r.get("url", ""), r.get("folder", "")),
                "select_folder": lambda r: (choose_folder(), send_current_folder()),
                "get_current_folder": lambda r: send_current_folder(),
            }

            handler = actions.get(action, lambda r: run_native_gui(r.get("url", ""), r.get("folder", "")))
            try:
                handler(request)
            except BrokenPipeError as e:
                logging.error(f"BrokenPipeError: {e}")
                sys.exit(1)
            except Exception as e:
                try:
                    send_message({"status": "error", "message": f"Error processing action '{action}': {e}"})
                except Exception as err:
                    logging.error(f"Failed to send error message: {err}")
                    sys.exit(1)
        except SystemExit:
            raise
        except Exception as e:
            try:
                send_message({"status": "error", "message": f"Native messaging error: {e}"})
            except Exception as err:
                logging.error(f"Failed to send error message: {err}")
                sys.exit(1) # check edge case Errors and raises


if __name__ == "__main__":
    main()



# RECEIVING
# {"url": "..."} - url only
# {"url": "...", "folder": "..."} - with folder

# {"action": "download", "url": "...", "folder": "..."} - Silent download
# {"action": "download_gui", "url": "..."} - Open GUI with prefilled URL
# {"action": "select_folder"} - Open folder picker, return path
# {"action": "get_current_folder"} - Return default folder path

# SENDING
# status: "success" / "error" / "warning"
# message: "..."
# filename: "..." - file already exists
# path: "..." - download successful
# folder: "..." - current folder path !!! not implemented properly yet