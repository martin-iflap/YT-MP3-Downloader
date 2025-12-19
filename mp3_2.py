from core_and_auto import safe_filename, embed_thumbnail, run_auto_mode
from config import FFMPEG_PATH, YT_DLP_PATH, ICON_PATH, DEFAULT_FOLDER
from tkinter import filedialog
import customtkinter as ctk
from pathlib import Path
import subprocess
import threading
import argparse
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
            text="❌ Prosím vložte platnú YouTube URL.", text_color="red"))
        return

    if not folder:
        root.after(0, lambda: status_label.configure(
            text="❌ Prosím vyberte priečinok.", text_color="red"))
        return

    filename = safe_filename(filename)

    if "-" in filename:
        parts = filename.split("-")
        artist = parts[0].strip()

    mp3_check = Path(folder) / f"{filename}.mp3"
    if mp3_check.exists():
        root.after(0, lambda: status_label.configure(
            text="❌ Súbor s týmto názvom už existuje!", text_color="red"))
        return

    output_template = str(Path(folder) / f"{filename}.%(ext)s")

    if not FFMPEG_PATH.exists():
        root.after(0, lambda: status_label.configure(
            text="❌ ffmpeg.exe nebol nájdený!", text_color="red"))
        return

    try:
        root.after(0, lambda: progress_bar.pack(pady=10))
        root.after(0, lambda: progress_bar.set(0))
        root.after(0, lambda: status_label.configure(text="⏳ Sťahovanie...", text_color="white"))
        root.after(0, lambda: main_button.configure(text="Zrušiť", command=cancel_download))

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

        root.after(0,lambda: status_label.configure(text="✅ MP3 bolo úspešne stiahnuté.", text_color="green"))
        root.after(0,lambda: main_button.configure(text="Ďalší", command=reset_fields))
    except subprocess.CalledProcessError as e:
        msg = f"❌ Chyba pri sťahovaní"
        root.after(0, lambda: status_label.configure(
            text=msg, text_color="red"))
        logging.error(f"{msg}: {e}")
    except FileNotFoundError as e:
        msg = f"❌ yt-dlp alebo ffmpeg nebol nájdený"
        root.after(0, lambda: status_label.configure(
            text=msg, text_color="red"))
        logging.error(f"{msg}: {e}")
    except Exception as e:
        msg = f"❌ Neznáma chyba"
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
        status_label.configure(text="❌ Stiahovanie bolo zrušené.", text_color="red")
        main_button.configure(text="Ďalší", command=reset_fields)
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
        main_button.configure(text="Stiahni MP3", command=download_audio)
    root.after(0, _reset)

# -----------------------------------------   GUI Setup   ------------------------------------------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

root = ctk.CTk()
root.title("YouTube MP3 Konvertor")
root.geometry("650x430")
root.current_process = None

# Window icon
try:
    root.iconbitmap(str(ICON_PATH))
except Exception as exc:
    Logging.error("⚠️ Ikona sa nepodarila načítať:", exc)

# Variables
url_var = ctk.StringVar()
folder_var = ctk.StringVar(value=str(DEFAULT_FOLDER))
filename_var = ctk.StringVar()

# Layout
ctk.CTkLabel(root, text="YouTube URL:", font=("Arial", 16)).pack(pady=(20, 5))
url_entry = ctk.CTkEntry(root, textvariable=url_var, width=580, height=35, font=("Arial", 14))
url_entry.pack()

ctk.CTkLabel(root, text="Priečinok:", font=("Arial", 16)).pack(pady=(20, 5))
folder_frame = ctk.CTkFrame(root, fg_color="transparent")
folder_frame.pack()
ctk.CTkEntry(folder_frame, textvariable=folder_var, width=450, height=35, font=("Arial", 14)).pack(side="left", padx=5)
ctk.CTkButton(folder_frame, text="Vybrať", command=choose_folder, width=120, height=35, font=("Arial", 14)).pack(side="left")

ctk.CTkLabel(root, text="Názov súboru:", font=("Arial", 16)).pack(pady=(20, 5))
name_entry = ctk.CTkEntry(root, textvariable=filename_var, width=580, height=35, font=("Arial", 14))
name_entry.pack()

main_button = ctk.CTkButton(
        root, text="Stiahni MP3", command=download_audio,
        fg_color="#4CAF50", hover_color="#45a049", font=("Arial", 15), width=220, height=45)
main_button.pack(pady=25)

status_label = ctk.CTkLabel(root, text="", font=("Arial", 14))
status_label.pack()

progress_bar = ctk.CTkProgressBar(root, width=580, height=15)
# -----------------------------------------   Main   ------------------------------------------

if __name__ == "__main__":
    """Parse command-line arguments and run the appropriate mode
     - Native messaging mode if --native, --url required
     - Auto-download mode if --auto, --url required
     - else run GUI mode --folder --url and --close optional
    """
    parser = argparse.ArgumentParser(description="YouTube to MP3 Converter GUI")
    parser.add_argument("--url", type=str, help="YouTube video URL")
    parser.add_argument("--folder", type=str, help="Override default folder")
    parser.add_argument("--close", action="store_true", help="Close the GUI after download")
    parser.add_argument("--auto", action="store_true", help="Auto-download mode")
    parser.add_argument("--native", action="store_true", help="Run in native messaging mode")
    args = parser.parse_args()

    if args.native:
        from native_messaging import run_native #fix this import
        run_native()
    else:
        if args.auto:
            run_auto_mode(args)
        else:
            if args.url:
                url_var.set(args.url)
            if args.folder:
                f_path = Path(args.folder).resolve()
                if f_path.is_dir():
                    folder_var.set(str(f_path))
                else:
                    logging.warning(f"Specified folder not found: {f_path}, using default")
                    folder_var.set(str(DEFAULT_FOLDER))
            if args.close:
                root.close_var = True

            root.mainloop()

# settings? recent folders dropdown, check project folders
# extension