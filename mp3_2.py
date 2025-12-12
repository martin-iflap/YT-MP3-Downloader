from tkinter import filedialog
import customtkinter as ctk
from pathlib import Path
import subprocess
import re

# -----------------------------------------   Config   ------------------------------------------
SCRIPT_DIR = Path(__file__).parent
FFMPEG_PATH = SCRIPT_DIR / "ffmpeg-7.1.1-essentials_build" / "ffmpeg-7.1.1-essentials_build" / "bin" / "ffmpeg.exe"
YT_DLP_PATH = SCRIPT_DIR / "yt-dlp.exe"
ICON_PATH = SCRIPT_DIR / "mp3.png"
DEFAULT_FOLDER = Path.home() / "Documents" / "Martin"

# -----------------------------------------   Functions   ------------------------------------------
def safe_filename(name):
    if not isinstance(name, str) or not name.strip():
        return "Youtube MP3"
    return re.sub(r'[\\/*?:"<>|]', "", name.strip())

def choose_folder():
    folder = filedialog.askdirectory(initialdir=str(DEFAULT_FOLDER))
    if folder:
        folder_var.set(folder)

def download_audio():
    url = url_var.get().strip()
    folder = folder_var.get().strip()
    filename = filename_var.get().strip()

    if not url:
        status_label.configure(text="❌ Prosím vlož YouTube URL.", text_color="red")
        return
    if not folder:
        status_label.configure(text="❌ Prosím vyber priečinok.", text_color="red")
        return

    if not filename:
        filename = "Youtube MP3"
    filename = safe_filename(filename)
    output_template = str(Path(folder) / f"{filename}.%(ext)s")

    if not FFMPEG_PATH.exists():
        status_label.configure(text="❌ ffmpeg.exe nebol nájdený!", text_color="red")
        return

    try:
        subprocess.run([
            str(YT_DLP_PATH),
            "--no-playlist",
            "-x", "--audio-format", "mp3",
            "--ffmpeg-location", str(FFMPEG_PATH),
            "-o", output_template,
            url
        ], check=True)

        status_label.configure(text="✅ MP3 bolo úspešne stiahnuté.", text_color="green")
    except subprocess.CalledProcessError as e:
        status_label.configure(text=f"❌ Chyba pri sťahovaní: {e}", text_color="red")
    except FileNotFoundError as e:
        status_label.configure(text=f"❌ yt-dlp alebo ffmpeg nebol nájdený: {e}", text_color="red")
    except Exception as e:
        status_label.configure(text=f"❌ Neznáma chyba: {e}", text_color="red")

# -----------------------------------------   GUI Setup   ------------------------------------------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

root = ctk.CTk()
root.title("YouTube MP3 Konvertor")
root.geometry("650x400")

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

ctk.CTkButton(
    root, text="Stiahni MP3", command=download_audio,
    fg_color="#4CAF50", hover_color="#45a049", font=("Arial", 15), width=220, height=45
    ).pack(pady=25)

status_label = ctk.CTkLabel(root, text="", font=("Arial", 14))
status_label.pack()

if __name__ == "__main__":
    root.mainloop()