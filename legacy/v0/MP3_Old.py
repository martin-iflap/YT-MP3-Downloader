import os
import re
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

# Helper: Get resource path (for PyInstaller)
def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath("")
    return os.path.join(base_path, relative_path)

# Paths
FFMPEG_PATH = resource_path("ffmpeg-7.1.1-essentials_build\\bin\\ffmpeg.exe")
DEFAULT_FOLDER = os.path.expanduser("~\\Documents\\Martin")
YT_DLP_PATH = resource_path("yt-dlp.exe")


# Utilities
def safe_filename(name):
    if not name:
        return "Youtube MP3"
    return re.sub(r'[\\/*?:"<>|]', "", name)

def choose_folder():
    folder = filedialog.askdirectory(initialdir=DEFAULT_FOLDER)
    if folder:
        folder_var.set(folder)

def download_audio():
    url = url_var.get().strip()
    folder = folder_var.get().strip()
    filename = filename_var.get().strip()
    if not filename:
        filename = "Youtube MP3"
    filename = safe_filename(filename)
    output_template = os.path.join(folder, filename + ".%(ext)s")

    if not url:
        status_label.config(text="❌ Prosím vlož YouTube URL.", fg="red")
        return
    if not folder:
        status_label.config(text="❌ Prosím vyber priečinok.", fg="red")
        return
    if not filename:
        filename = "Youtube MP3"

    output_template = os.path.join(folder, safe_filename(filename) + ".%(ext)s")

    if not os.path.isfile(FFMPEG_PATH):
        status_label.config(text="❌ ffmpeg.exe nebol nájdený!", fg="red")
        return

    try:
        subprocess.run([
            YT_DLP_PATH,
            "--no-playlist",
            "-x", "--audio-format", "mp3",
            "--ffmpeg-location", FFMPEG_PATH,
            "-o", output_template,
            url
        ], check=True)

        status_label.config(text="✅ MP3 bolo úspešne stiahnuté.", fg="green")
    except subprocess.CalledProcessError as exp:
        status_label.config(text=f"❌ Chyba pri sťahovaní: {exp}", fg="red")
    except FileNotFoundError as exp:
        status_label.config(text=f"❌ yt-dlp alebo ffmpeg nebol nájdený: {exp}", fg="red")
    except Exception as exp:
        status_label.config(text=f"❌ Neznáma chyba: {exp}", fg="red")

# GUI Setup
root = tk.Tk()
root.title("YouTube MP3 Konvertor")
root.geometry("650x400")
root.configure(bg="#292929")

# Window icon
try:
    icon_path = Path(__file__).parent.parent.parent / "ico" / "mp3_old.ico"
    icon = tk.PhotoImage(file=icon_path)
    root.iconphoto(True, icon)
except Exception as e:
    print("⚠️ Ikona sa nepodarila načítať:", e)

# Variables
url_var = tk.StringVar()
folder_var = tk.StringVar(value=DEFAULT_FOLDER)
filename_var = tk.StringVar()

# Layout
tk.Label(root, text="YouTube URL:", font=("Arial", 13), bg="#292929", fg="white").pack(pady=(15, 2))
tk.Entry(root, textvariable=url_var, width=60).pack()

tk.Label(root, text="Priečinok:", font=("Arial", 13), bg="#292929", fg="white").pack(pady=(15, 2))
folder_frame = tk.Frame(root, bg="#292929")
folder_frame.pack()
tk.Entry(folder_frame, textvariable=folder_var, width=45).pack(side=tk.LEFT, padx=5)
tk.Button(folder_frame, text="Vybrať", command=choose_folder, bg="#444", fg="white").pack(side=tk.LEFT)

tk.Label(root, text="Názov súboru:", font=("Arial", 13), bg="#292929", fg="white").pack(pady=(15, 2))
tk.Entry(root, textvariable=filename_var, width=60).pack()

tk.Button(
    root, text="Stiahni MP3", command=download_audio,
    bg="#4CAF50", fg="white", font=("Arial", 12), padx=10, pady=5
).pack(pady=20)

status_label = tk.Label(root, text="", font=("Arial", 12), bg="#292929", fg="white")
status_label.pack()

if __name__ == "__main__":
    root.mainloop()
