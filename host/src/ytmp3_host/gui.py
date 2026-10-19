"""
customtkinter GUI for the download_gui and select_folder actions.
MP3.py imports this module lazily, so headless downloads never load Tk (faster host startup).
"""
from ytmp3_host.config import ICON_PATH, DEFAULT_FOLDER
from ytmp3_host.native_messaging import send_message, send_current_folder
from ytmp3_host.core import safe_filename, ensure_default_folder, download_audio, kill_process_tree
from tkinter import filedialog
import customtkinter as ctk
from pathlib import Path
import subprocess
import threading
import logging
import time


class DownloaderApp(ctk.CTk):
    """YouTube MP3 Converter window"""

    def __init__(self) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("green")
        super().__init__()

        self.title("YouTube MP3 Converter")
        self.geometry("650x430")
        self.current_process: subprocess.Popen[str] | None = None
        self.current_stem: Path | None = None
        self.download_cancelled = False
        self.close_var = False

        # Window icon
        try:
            self.iconbitmap(str(ICON_PATH))
        except Exception as exc:
            logging.error("⚠️ Icon not found : %s", exc)

        # Variables
        self.url_var = ctk.StringVar()
        self.folder_var = ctk.StringVar(value=str(DEFAULT_FOLDER))
        self.filename_var = ctk.StringVar()

        # Visual container
        main_frame = ctk.CTkFrame(self, fg_color="#17181a", corner_radius=12)
        main_frame.pack(fill='both', expand=True, padx=14, pady=14)

        # Layout
        ctk.CTkLabel(main_frame, text="YouTube URL:", font=("Segoe UI", 20, "bold")).pack(in_=main_frame, pady=(12, 6))
        self.url_entry = ctk.CTkEntry(main_frame, textvariable=self.url_var, width=580, height=35, font=("Segoe UI", 14), corner_radius=999)
        self.url_entry.pack(in_=main_frame)

        ctk.CTkLabel(main_frame, text="Folder:", font=("Segoe UI", 20, "bold")).pack(in_=main_frame, pady=(12, 6))
        folder_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        folder_frame.pack(in_=main_frame)
        ctk.CTkEntry(folder_frame, textvariable=self.folder_var, width=450, height=35, font=("Segoe UI", 14), corner_radius=999).pack(side="left", padx=5)
        ctk.CTkButton(folder_frame, text="Choose", command=self.choose_folder, width=120, height=35, font=("Segoe UI", 14, "bold")).pack(side="left")

        ctk.CTkLabel(main_frame, text="Filename:", font=("Segoe UI", 20, "bold")).pack(in_=main_frame, pady=(12, 6))
        self.name_entry = ctk.CTkEntry(main_frame, textvariable=self.filename_var, width=580, height=35, font=("Segoe UI", 14), corner_radius=999)
        self.name_entry.pack(in_=main_frame)

        self.main_button = ctk.CTkButton(
                main_frame,
                text="Download MP3",
                command=self.start_download,
                fg_color="#26282a",
                hover_color="#1f2022",
                text_color="#f3f4f6",
                corner_radius=999,
                border_width=1,
                border_color="#555555",
                font=("Segoe UI", 15, "bold"),
                width=220,
                height=45)
        self.main_button.pack(in_=main_frame, pady=20)

        self.status_label = ctk.CTkLabel(main_frame, text="", font=("Segoe UI", 16, "bold"))
        self.status_label.pack(in_=main_frame)

        self.progress_bar = ctk.CTkProgressBar(main_frame, width=580, height=15)

    def choose_folder(self) -> None:
        """Open a folder selection dialog and update the folder_var
         - set to DEFAULT_FOLDER initially if no folder is selected
        """
        folder = filedialog.askdirectory(initialdir=str(DEFAULT_FOLDER))
        if folder:
            self.folder_var.set(folder)

    def start_download(self) -> None:
        """Start the audio download in a separate thread to allow progress bar updates"""
        thread = threading.Thread(target=self._download_audio_thread, daemon=True)
        thread.start()

    def _download_audio_thread(self) -> None:
        """Download audio from the provided YouTube URL and save it as an MP3 file
         - Uses: core.download_audio (same yt-dlp/ffmpeg pipeline as headless downloads)
         - an empty filename means the file is named after the video title
        """
        url = self.url_var.get().strip()
        folder = self.folder_var.get().strip()
        filename = safe_filename(self.filename_var.get().strip())
        if filename.lower() == "youtube mp3":
            filename = ""

        if not url or "youtube.com" not in url:
            self.after(0, lambda: self.status_label.configure(
                text="❌ Enter valid YouTube URL", text_color="red"))
            return

        if not folder:
            folder_path = ensure_default_folder()
            self.after(0, lambda: self.folder_var.set(str(folder_path)))
        else:
            folder_path = Path(folder).expanduser()
            if not folder_path.is_dir():
                msg = f"❌ Path doesn't exist"
                self.after(0, lambda: self.status_label.configure(text=msg, text_color="red"))
                logging.error(f"{msg}: {folder_path}")
                return

        def _on_start(process: subprocess.Popen[str], temp_stem: Path) -> None:
            self.current_process = process
            self.current_stem = temp_stem

        try:
            self.after(0, lambda: self.progress_bar.pack(pady=10))
            self.after(0, lambda: self.progress_bar.set(0))
            self.after(0, lambda: self.status_label.configure(text="⏳ Downloading...", text_color="white"))
            self.after(0, lambda: self.main_button.configure(text="Cancel", command=self.cancel_download))

            self.download_cancelled = False
            self.current_process = None
            self.current_stem = None
            download_audio(url, folder_path, filename,
                           on_progress=lambda p: self.after(0, self.progress_bar.set, p),
                           on_start=_on_start)

            self.after(0, lambda: self.status_label.configure(text="✅ MP3 downloaded successfully", text_color="green"))
            self.after(0, lambda: self.main_button.configure(text="Next", command=self.reset_fields))
        except Exception as e:
            if self.download_cancelled:
                return
            if isinstance(e, FileExistsError):
                msg = "❌ File with this filename already exists!"
            elif isinstance(e, subprocess.CalledProcessError):
                msg = "❌ Error during download or conversion"
            elif isinstance(e, FileNotFoundError):
                msg = "❌ yt-dlp or ffmpeg not found"
            else:
                msg = "❌ Unexpected error occurred"
            self.after(0, lambda: self.status_label.configure(text=msg, text_color="red"))
            self.after(0, lambda: self.main_button.configure(text="Download MP3", command=self.start_download))
            logging.error(f"{msg}: {e}")
        finally:
            self.current_process = None
            self.after(0, lambda: self.progress_bar.pack_forget())

            if self.close_var:
                def safe_close():
                    """Close the GUI window safely after a delay"""
                    try:
                        self.quit()
                        self.destroy()
                    except Exception as ex:
                        logging.warning(f"❌ Window already closed: {ex}")
                self.after(2500, safe_close)

    def cancel_download(self) -> None:
        """Cancel the ongoing download process from the current_process attribute
         - use a helper function to clean up partial downloads and another one to reset the UI
        """
        self.download_cancelled = True
        if self.current_process:
            kill_process_tree(self.current_process)
            self.current_process = None

        def _update_ui():
            self.status_label.configure(text="❌ Download canceled", text_color="red")
            self.main_button.configure(text="Next", command=self.reset_fields)
            self.progress_bar.pack_forget()
        self.after(0, _update_ui)

        if self.current_stem:
            threading.Thread(target=cleanup_partial_downloads, args=(self.current_stem,), daemon=True).start()

    def reset_fields(self) -> None:
        """Reset input entries for a new download with a helper function"""
        def _reset():
            self.url_entry.delete(0, 'end')
            self.name_entry.delete(0, 'end')
            self.status_label.configure(text="")
            self.main_button.configure(text="Download MP3", command=self.start_download)
        self.after(0, _reset)

    def run_native_gui(self, url: str, folder: str, filename: str) -> None:
        """Run the GUI with or without prefilled arguments from Firefox"""
        if url:
            self.url_var.set(url)
        if folder:
            f_path = Path(folder).expanduser()
            if f_path.is_dir():
                self.folder_var.set(str(f_path))
            elif f_path == Path(DEFAULT_FOLDER).expanduser():
                self.folder_var.set(str(ensure_default_folder()))
            else:
                send_message({"status": "error", "message": "Path doesn't exist", "final": True})
                return
        if filename.strip():
            self.filename_var.set(safe_filename(filename))

        self.after(100, lambda: send_message({"status": "success", "message": "GUI opened", "final": True}))
        self.mainloop()

def cleanup_partial_downloads(temp_stem: Path) -> None:
    """Remove any partial download files from yt-dlp after cancellation
     - core.download_audio already removes them when it exits; this is the fallback for locked files
    """
    folder_path = temp_stem.parent
    logging.info(f"📁 Cleanup folder: {folder_path}")
    logging.info(f"🔍 Looking for temp files: {temp_stem.name}")

    time.sleep(2) # the process tree is already killed, give Windows a moment to release file locks
    for f in folder_path.glob(f"{temp_stem.name}.*"):
        if f.exists():
            logging.info(f"🗑️ Deleting file: {f}")
            for attempt in range(3):
                try:
                    f.unlink()
                    logging.info(f"Unlinked partial file: {f}")
                    break
                except PermissionError:
                    if attempt < 2:
                        logging.info(f"⏳ Retry {attempt + 1}/3 (file locked)")
                        time.sleep(0.5)
                except Exception as e:
                    logging.error(f"Error deleting file {f}: {e}")
                    break

def open_gui(url: str, folder: str, filename: str) -> None:
    """Build the GUI and run it with the prefilled arguments (download_gui action)"""
    DownloaderApp().run_native_gui(url, folder, filename)

def select_folder() -> None:
    """Open the folder picker and send the chosen (or default) folder (select_folder action)"""
    app = DownloaderApp()
    app.choose_folder()
    send_current_folder(app.folder_var.get())
