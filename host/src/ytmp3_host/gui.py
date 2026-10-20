"""customtkinter GUI for the `download_gui` and `select_folder` actions.

MP3.py imports this module lazily, so headless downloads never load Tk (faster host
startup). Tk is not thread-safe: the download runs on a worker thread that touches
widgets only through `self.after(0, ...)` and never reads Tk variables.
"""

import logging
import subprocess
import threading
import time
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from ytmp3_host.config import ICON_PATH
from ytmp3_host.core import (
    describe_error,
    download_audio,
    ensure_default_folder,
    is_youtube_url,
    kill_process_tree,
    remove_temp_files,
    requested_filename,
    resolve_folder,
    safe_filename,
)
from ytmp3_host.native_messaging import send_current_folder, send_message

logger = logging.getLogger(__name__)

FONT = "Segoe UI"
CLEANUP_DELAY_S = 2


class DownloaderApp(ctk.CTk):
    """The YouTube MP3 Converter window.

    Attributes:
        url_var: The URL field.
        folder_var: The folder field, initially the default folder.
        filename_var: The filename field; empty means name the file after the video title.
        current_process: The running yt-dlp process, used to cancel it.
        current_stem: The temp output stem of the running download, used for cleanup.
        download_cancelled: Set by cancel_download, read by the download thread.
    """

    def __init__(self) -> None:
        """Build the window and its widgets (shown once mainloop() runs)."""
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("green")
        super().__init__()

        self.title("YouTube MP3 Converter")
        self.geometry("650x430")
        self.current_process: subprocess.Popen[str] | None = None
        self.current_stem: Path | None = None
        self.download_cancelled = False

        # Window icon
        try:
            self.iconbitmap(str(ICON_PATH))
        except Exception as e:
            logger.error("Icon not found: %s", e)

        # Variables
        self.url_var = ctk.StringVar()
        self.folder_var = ctk.StringVar(value=str(ensure_default_folder()))
        self.filename_var = ctk.StringVar()

        main_frame = ctk.CTkFrame(self, fg_color="#17181a", corner_radius=12)
        main_frame.pack(fill="both", expand=True, padx=14, pady=14)

        # Layout
        ctk.CTkLabel(main_frame, text="YouTube URL:", font=(FONT, 20, "bold")).pack(pady=(12, 6))
        self.url_entry = ctk.CTkEntry(
            main_frame, textvariable=self.url_var, width=580, height=35, font=(FONT, 14), corner_radius=999
        )
        self.url_entry.pack()

        ctk.CTkLabel(main_frame, text="Folder:", font=(FONT, 20, "bold")).pack(pady=(12, 6))
        folder_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        folder_frame.pack()
        ctk.CTkEntry(
            folder_frame, textvariable=self.folder_var, width=450, height=35, font=(FONT, 14), corner_radius=999
        ).pack(side="left", padx=5)
        ctk.CTkButton(
            folder_frame, text="Choose", command=self.choose_folder, width=120, height=35, font=(FONT, 14, "bold")
        ).pack(side="left")

        ctk.CTkLabel(main_frame, text="Filename:", font=(FONT, 20, "bold")).pack(pady=(12, 6))
        self.name_entry = ctk.CTkEntry(
            main_frame, textvariable=self.filename_var, width=580, height=35, font=(FONT, 14), corner_radius=999
        )
        self.name_entry.pack()

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
            font=(FONT, 15, "bold"),
            width=220,
            height=45,
        )
        self.main_button.pack(pady=20)

        self.status_label = ctk.CTkLabel(main_frame, text="", font=(FONT, 16, "bold"), wraplength=580)
        self.status_label.pack()

        self.progress_bar = ctk.CTkProgressBar(main_frame, width=580, height=15)

    def show_status(self, text: str, color: str) -> None:
        """Show a status message below the main button. Main thread only."""
        self.status_label.configure(text=text, text_color=color)

    def choose_folder(self) -> None:
        """Let the user pick a folder, starting at the current one; keep it on cancel."""
        initial = resolve_folder(self.folder_var.get()) or ensure_default_folder()
        folder = filedialog.askdirectory(parent=self, initialdir=str(initial))
        if folder:
            self.folder_var.set(folder)

    def start_download(self) -> None:
        """Validate the form and start the download on a worker thread.

        Reads the Tk variables here, on the main thread, and switches the main
        button to Cancel before the thread starts, so a second click cannot start
        a second download.
        """
        url = self.url_var.get().strip()
        if not url or not is_youtube_url(url):
            self.show_status("❌ Enter valid YouTube URL", "red")
            return

        folder_path = resolve_folder(self.folder_var.get())
        if folder_path is None:
            self.show_status("❌ Path doesn't exist", "red")
            return
        self.folder_var.set(str(folder_path))

        self.download_cancelled = False
        self.current_process = None
        self.current_stem = None
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=10)
        self.show_status("⏳ Downloading...", "white")
        self.main_button.configure(text="Cancel", command=self.cancel_download)

        filename = requested_filename(self.filename_var.get())
        threading.Thread(target=self._download_thread, args=(url, folder_path, filename), daemon=True).start()

    def _download_thread(self, url: str, folder_path: Path, filename: str) -> None:
        """Run core.download_audio and report the result in the window.

        Args:
            url: The YouTube video URL.
            folder_path: An existing folder to save the MP3 in.
            filename: The sanitized filename; empty means name it after the video title.
        """
        def _on_start(process: subprocess.Popen[str], temp_stem: Path) -> None:
            self.current_process = process
            self.current_stem = temp_stem
            if self.download_cancelled:  # canceled before yt-dlp was running
                kill_process_tree(process)

        warnings: list[str] = []
        try:
            mp3_path = download_audio(
                url,
                folder_path,
                filename,
                on_progress=lambda p: self.after(0, self.progress_bar.set, p),
                on_start=_on_start,
                on_warning=warnings.append,
            )
        except Exception as e:
            if self.download_cancelled:
                return
            msg = f"❌ {describe_error(e)}"
            logger.error("%s: %s", msg, e)
            self.after(0, self.show_status, msg, "red")
            self.after(0, lambda: self.main_button.configure(text="Download MP3", command=self.start_download))
        else:
            # also after a late Cancel: the file is saved, so say so instead of "canceled"
            msg = "\n".join([f"✅ Saved {mp3_path.name}", *(f"⚠️ {w}" for w in warnings)])
            self.after(0, self.show_status, msg, "orange" if warnings else "green")
            self.after(0, lambda: self.main_button.configure(text="Next", command=self.reset_fields))
        finally:
            self.current_process = None
            self.after(0, self.progress_bar.pack_forget)

    def cancel_download(self) -> None:
        """Cancel the running download, reset the UI and clean up its partial files."""
        self.download_cancelled = True
        if self.current_process:
            kill_process_tree(self.current_process)
            self.current_process = None

        self.show_status("❌ Download canceled", "red")
        self.main_button.configure(text="Next", command=self.reset_fields)
        self.progress_bar.pack_forget()

        if self.current_stem:
            threading.Thread(target=cleanup_partial_downloads, args=(self.current_stem,), daemon=True).start()

    def reset_fields(self) -> None:
        """Clear the URL and filename fields for the next download."""
        self.url_entry.delete(0, "end")
        self.name_entry.delete(0, "end")
        self.show_status("", "white")
        self.main_button.configure(text="Download MP3", command=self.start_download)

    def run_native_gui(self, url: str, folder: str, filename: str) -> None:
        """Prefill the form from a `download_gui` request and run the window.
        An invalid folder keeps the default folder; the window and the final
        "GUI opened" response both say so. Blocks until the window is closed.

        Args:
            url: The URL to prefill, possibly empty.
            folder: The folder to prefill, possibly empty.
            filename: The filename to prefill, possibly empty.
        """
        opened_message = "GUI opened"
        if url:
            self.url_var.set(url)
        if folder:
            folder_path = resolve_folder(folder)
            if folder_path is None:
                self.show_status("⚠️ Folder not found, using the default folder", "orange")
                opened_message = "GUI opened, folder not found so the default folder is used"
            else:
                self.folder_var.set(str(folder_path))
        if filename.strip():
            self.filename_var.set(safe_filename(filename))

        self.after(100, lambda: send_message({"status": "success", "message": opened_message, "final": True}))
        self.mainloop()


def cleanup_partial_downloads(temp_stem: Path) -> None:
    """Remove a canceled download's leftover files.
    core.download_audio already removes them when it exits; this is the fallback
    for files Windows still had locked at that moment.

    Args:
        temp_stem: The temp output stem of the canceled download.
    """
    time.sleep(CLEANUP_DELAY_S)  # the process tree is already killed, give Windows a moment to release locks
    remove_temp_files(temp_stem)


def open_gui(url: str, folder: str, filename: str) -> None:
    """Handle `download_gui`: open the window prefilled with the request's fields.
    Blocks until the window is closed.
    """
    DownloaderApp().run_native_gui(url, folder, filename)


def select_folder(folder: str) -> None:
    """Handle `select_folder`: show the folder picker and send the result.
    Args:
        folder: The folder currently set in the extension. The picker starts there,
            and it is sent back unchanged if the user cancels.
    """
    initial = resolve_folder(folder) or ensure_default_folder()
    root = ctk.CTk()
    root.withdraw()
    root.attributes("-topmost", True)  # the dialog would otherwise open behind Firefox
    try:
        chosen = filedialog.askdirectory(parent=root, initialdir=str(initial))
    finally:
        root.destroy()
    send_current_folder(chosen or str(initial))
