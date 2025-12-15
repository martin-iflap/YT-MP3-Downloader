from tkinter import filedialog
import customtkinter as ctk
from pathlib import Path
from PIL import Image
import subprocess
import threading
import re

# -----------------------------------------   Config   ------------------------------------------
SCRIPT_DIR = Path(__file__).parent
FFMPEG_PATH = SCRIPT_DIR / "ffmpeg-7.1.1-essentials_build" / "ffmpeg-7.1.1-essentials_build" / "bin" / "ffmpeg.exe"
YT_DLP_PATH = SCRIPT_DIR / "yt-dlp.exe"
ICON_PATH = SCRIPT_DIR / "mp3_new.ico"
DEFAULT_FOLDER = Path.home() / "Documents" / "Martin"

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
        status_label.configure(text="❌ Prosím vložte platnú YouTube URL.", text_color="red")
        return
    if not folder:
        status_label.configure(text="❌ Prosím vyberte priečinok.", text_color="red")
        return

    filename = safe_filename(filename)

    if "-" in filename:
        parts = filename.split("-")
        artist = parts[0].strip()

    mp3_check = Path(folder) / f"{filename}.mp3"
    if mp3_check.exists():
        status_label.configure(text="❌ Súbor s týmto názvom už existuje!", text_color="red")
        return

    output_template = str(Path(folder) / f"{filename}.%(ext)s")

    if not FFMPEG_PATH.exists():
        status_label.configure(text="❌ ffmpeg.exe nebol nájdený!", text_color="red")
        return

    try:
        root.after(0, lambda: progress_bar.pack(pady=10))
        root.after(0, lambda: progress_bar.set(0))
        root.after(0, lambda: status_label.configure(text="⏳ Sťahovanie...", text_color="white"))
        root.after(0, lambda: main_button.configure(text="Zrušiť", command=cancel_download))

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
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, process.args)

        mp3_file = Path(folder) / f"{filename}.mp3"
        thumb_file = Path(folder) / f"{filename}.jpg"

        if thumb_file.exists() and mp3_file.exists():
            img = Image.open(thumb_file)
            width, height = img.size
            size = min(width, height)
            left = (width - size) // 2
            top = (height - size) // 2
            img_cropped = img.crop((left, top, left + size, top + size))
            img_cropped.save(thumb_file)

            temp_mp3 = Path(folder) / f"{filename}_temp.mp3"
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

        root.after(0,lambda: status_label.configure(text="✅ MP3 bolo úspešne stiahnuté.", text_color="green"))
        root.after(0,lambda: main_button.configure(text="Ďalší", command=reset_fields))
    except subprocess.CalledProcessError as e:
        status_label.configure(text=f"❌ Chyba pri sťahovaní: {e}", text_color="red")
    except FileNotFoundError as e:
        status_label.configure(text=f"❌ yt-dlp alebo ffmpeg nebol nájdený: {e}", text_color="red")
    except Exception as e:
        status_label.configure(text=f"❌ Neznáma chyba: {e}", text_color="red")
    finally:
        root.after(0, lambda: progress_bar.pack_forget())

def cancel_download() -> None:
    """Cancel the ongoing download process form the root.current_process attribute"""
    if hasattr(root, "current_process") and root.current_process and root.current_process.poll() is None:
        root.current_process.terminate()
        root.current_process = None

    root.after(0, status_label.configure(text="❌ Stiahnutie bolo zrušené.", text_color="red"))
    root.after(0, main_button.configure(text="Ďalší", command=reset_fields)) # or download again?
    root.after(0, progress_bar.pack_forget())

def reset_fields() -> None:
    """Reset input entries for a new download"""
    url_var.set("")
    filename_var.set("")
    status_label.configure(text="")
    main_button.configure(text="Stiahni MP3", command=download_audio)
    # reset the entries as well

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
    print("⚠️ Ikona sa nepodarila načítať:", exc)

# Variables
url_var = ctk.StringVar()
folder_var = ctk.StringVar(value=str(DEFAULT_FOLDER))
filename_var = ctk.StringVar()

# Layout
ctk.CTkLabel(root, text="YouTube URL:", font=("Arial", 16)).pack(pady=(20, 5))
ctk.CTkEntry(root, textvariable=url_var, width=580, height=35, font=("Arial", 14)).pack()

ctk.CTkLabel(root, text="Priečinok:", font=("Arial", 16)).pack(pady=(20, 5))
folder_frame = ctk.CTkFrame(root, fg_color="transparent")
folder_frame.pack()
ctk.CTkEntry(folder_frame, textvariable=folder_var, width=450, height=35, font=("Arial", 14)).pack(side="left", padx=5)
ctk.CTkButton(folder_frame, text="Vybrať", command=choose_folder, width=120, height=35, font=("Arial", 14)).pack(side="left")

ctk.CTkLabel(root, text="Názov súboru:", font=("Arial", 16)).pack(pady=(20, 5))
ctk.CTkEntry(root, textvariable=filename_var, width=580, height=35, font=("Arial", 14)).pack()

main_button = ctk.CTkButton(
        root, text="Stiahni MP3", command=download_audio,
        fg_color="#4CAF50", hover_color="#45a049", font=("Arial", 15), width=220, height=45)
main_button.pack(pady=25)

status_label = ctk.CTkLabel(root, text="", font=("Arial", 14))
status_label.pack()

progress_bar = ctk.CTkProgressBar(root, width=580, height=15)


if __name__ == "__main__":
    root.mainloop()

# progress bar (with multithreading), recent folders dropdown, drag-and-drop URL?
# logging, config file?, cancel next buttons,
