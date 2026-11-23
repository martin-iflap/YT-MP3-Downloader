"""customtkinter GUI for the `download_gui` and `select_folder` actions.

MP3.py imports this module lazily, so headless downloads never load Tk (faster host
startup). Tk is not thread-safe: the download runs on a worker thread that touches
widgets only through `self.after(0, ...)` and never reads Tk variables.
"""

import logging
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from tkinter import TclError, filedialog

import customtkinter as ctk

from ytmp3_host.config import ICON_PATH
from ytmp3_host.core import describe_error, download_with_repair
from ytmp3_host.files import (
    ensure_default_folder,
    folder_is_writable,
    open_file,
    remove_temp_files,
    resolve_folder,
    show_in_folder,
)
from ytmp3_host.media_format import MediaFormat, Mp3Format, Mp4Codec, Mp4Format, Mp4Quality
from ytmp3_host.naming import (
    filename_too_long,
    filename_too_long_message,
    is_web_url,
    requested_filename,
    safe_filename,
)
from ytmp3_host.native_messaging import send_current_folder, send_message
from ytmp3_host.ytdlp import Progress, kill_process_tree

logger = logging.getLogger(__name__)

FONT = "Segoe UI"
FONT_SEMIBOLD = "Segoe UI Semibold"
CLEANUP_DELAY_S = 2

# The sidebar's MP4 choices (extension/formats.js), shortened to fit one row
QUALITY_LABELS: dict[Mp4Quality, str] = {"best": "Best", "1080": "1080p", "720": "720p", "480": "480p"}
CODEC_LABELS: dict[Mp4Codec, str] = {"best": "Best (AV1/VP9)", "compatible": "Compatible (H.264)"}

# Palette of extension/sidebar.html (its translucent whites blended onto the background)
BG = "#17181a"
CARD = "#1c1d1f"
CARD_BORDER = "#242528"
INPUT = "#202123"
INPUT_BORDER = "#2a2c2f"
INPUT_BORDER_FOCUS = "#45484d"
TEXT = "#e6e6e6"
TEXT_MUTED = "#9aa0a6"
PLACEHOLDER = "#7e8488"
PILL = "#26282a"
PILL_HOVER = "#2e3033"
PILL_BORDER = "#34363a"
# Primary pill: the sidebar's .action-pill.primary, Apple system blue (#0a84ff) blended onto BG
ACCENT = "#14304c"
ACCENT_HOVER = "#13385f"
ACCENT_BORDER = "#114e8c"
ACCENT_TEXT = "#eaf3ff"

# Status pill styles, as #result.<kind> in the sidebar: (dot, text, border)
STATUS_STYLES = {
    "success": ("#2ecc71", "#bfecc8", "#1c3c2b"),
    "error": ("#ff6b6b", "#ffd6d6", "#45292a"),
    "warning": ("#f39c12", "#ffecc5", "#433218"),
    "info": ("#4a90e2", "#cfe9ff", "#213042"),
}

DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35


class DownloaderApp(ctk.CTk):
    """The YouTube MP3 Converter window.

    Attributes:
        url_entry: The URL field.
        folder_var: The folder field, initially the default folder.
        name_entry: The filename field; empty means name the file after the video title.
        format_switch: The MP3/MP4 switch; quality_menu and codec_menu show while MP4 is selected.
        current_process: The running yt-dlp process, used to cancel it.
        current_stem: The temp output stem of the running download, used for cleanup.
        download_cancelled: Set by cancel_download, read by the download thread.
        download_id: Raised by every start. A download thread whose id is no longer
            current was canceled and a newer download owns the window (the old thread
            may still be repairing yt-dlp), so it leaves the window alone.
    """

    def __init__(self) -> None:
        """Build the window and its widgets (shown once mainloop() runs)."""
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("green")
        super().__init__(fg_color=BG)

        self.title("MP3 & MP4 Downloader")
        self.geometry("560x580")
        # room for the MP4 choices in one row, a two-line status and the file buttons below it
        self.minsize(470, 580)
        self.current_process: subprocess.Popen[str] | None = None
        self.current_stem: Path | None = None
        self.download_cancelled = False
        self.download_id = 0
        self.protocol("WM_DELETE_WINDOW", self._close)

        # Window icon
        try:
            self.iconbitmap(str(ICON_PATH))
        except TclError as e:
            logger.error("Icon not found: %s", e)
        self._match_title_bar()

        self.folder_var = ctk.StringVar(value=str(ensure_default_folder()))

        main_frame = ctk.CTkFrame(self, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=18, pady=(16, 18))

        ctk.CTkLabel(main_frame, text="MP3 & MP4 Downloader", text_color="#ffffff", font=(FONT_SEMIBOLD, 17)).pack(
            pady=(0, 12)
        )

        card = ctk.CTkFrame(main_frame, fg_color=CARD, corner_radius=12, border_width=1, border_color=CARD_BORDER)
        card.pack(fill="x")
        fields = ctk.CTkFrame(card, fg_color="transparent")
        fields.pack(fill="x", padx=14, pady=14)

        self._field_label(fields, "FORMAT", top=0)
        format_row = ctk.CTkFrame(fields, fg_color="transparent")
        format_row.pack(fill="x")
        self.format_switch = ctk.CTkSegmentedButton(
            format_row,
            values=["MP3", "MP4"],
            command=lambda _value: self._on_format_change(),
            fg_color=PILL,
            selected_color=ACCENT,
            selected_hover_color=ACCENT_HOVER,
            unselected_color=PILL,
            unselected_hover_color=PILL_HOVER,
            text_color=TEXT,
            corner_radius=8,
            font=(FONT_SEMIBOLD, 12),
            width=110,
            height=32,
        )
        self.format_switch.set("MP3")
        self.format_switch.pack(side="left")
        self.mp4_options = ctk.CTkFrame(format_row, fg_color="transparent")
        self.quality_menu = self._option_menu(self.mp4_options, list(QUALITY_LABELS.values()), width=90)
        self.quality_menu.pack(side="left", padx=(10, 0))
        self.codec_menu = self._option_menu(self.mp4_options, list(CODEC_LABELS.values()), width=160)
        self.codec_menu.pack(side="left", padx=(8, 0))

        self._field_label(fields, "URL")
        self.url_entry = self._entry(fields, placeholder="https://www.youtube.com/watch?v=...")
        self.url_entry.pack(fill="x")

        self._field_label(fields, "DOWNLOAD FOLDER")
        folder_row = ctk.CTkFrame(fields, fg_color="transparent")
        folder_row.pack(fill="x")
        ctk.CTkButton(
            folder_row,
            text="Choose",
            command=self.choose_folder,
            fg_color="transparent",
            hover_color=INPUT,
            text_color="#d7dadd",
            border_width=1,
            border_color=INPUT_BORDER,
            corner_radius=8,
            font=(FONT, 13),
            width=84,
            height=36,
        ).pack(side="right")
        self._entry(folder_row, textvariable=self.folder_var).pack(side="left", fill="x", expand=True, padx=(0, 8))

        self._field_label(fields, "FILENAME (OPTIONAL)")
        self.name_entry = self._entry(fields, placeholder="Leave empty to use video title")
        self.name_entry.pack(fill="x")

        self.main_button = ctk.CTkButton(
            main_frame, corner_radius=999, border_width=1, font=(FONT_SEMIBOLD, 14), width=180, height=42
        )
        self._main_command: Callable[[], None] | None = None
        self._show_download_button()
        self.main_button.pack(pady=(18, 0))

        self.progress_bar = ctk.CTkProgressBar(
            main_frame, height=4, corner_radius=2, fg_color=PILL, progress_color="#cfd6da", border_width=0
        )

        self.status_frame = ctk.CTkFrame(main_frame, fg_color=CARD, corner_radius=10, border_width=1)
        self.status_dot = ctk.CTkLabel(self.status_frame, text="●", font=(FONT, 11), width=10)
        self.status_dot.pack(side="left", padx=(12, 8), pady=10)
        self.status_label = ctk.CTkLabel(
            self.status_frame, text="", font=(FONT, 13), justify="left", anchor="w", wraplength=440
        )
        self.status_label.pack(side="left", fill="x", expand=True, padx=(0, 12), pady=10)

        # "Show in folder" and "Play" for the last saved file, shown below its status
        self.saved_file: Path | None = None
        self.file_actions = ctk.CTkFrame(main_frame, fg_color="transparent")
        for text, action in (("Show in folder", show_in_folder), ("Play", open_file)):
            ctk.CTkButton(
                self.file_actions,
                text=text,
                command=lambda act=action: self._open_saved_file(act),
                fg_color=PILL,
                hover_color=PILL_HOVER,
                text_color="#d7dadd",
                border_width=1,
                border_color=PILL_BORDER,
                corner_radius=8,
                font=(FONT, 12),
                width=110,
                height=30,
            ).pack(side="left", padx=4)

    @staticmethod
    def _field_label(master: ctk.CTkFrame, text: str, top: int = 12) -> None:
        """Add a small uppercase caption above a field, like the sidebar's input labels."""
        ctk.CTkLabel(master, text=text, text_color=TEXT_MUTED, font=(FONT_SEMIBOLD, 10), anchor="w", height=14).pack(
            fill="x", pady=(top, 6)
        )

    @staticmethod
    def _entry(master: ctk.CTkFrame, placeholder: str | None = None, textvariable: ctk.StringVar | None = None) -> ctk.CTkEntry:
        """Create a sidebar-styled entry whose border brightens while it has focus."""
        entry = ctk.CTkEntry(
            master,
            textvariable=textvariable,
            placeholder_text=placeholder,
            placeholder_text_color=PLACEHOLDER,
            fg_color=INPUT,
            border_color=INPUT_BORDER,
            border_width=1,
            text_color=TEXT,
            corner_radius=8,
            height=36,
            font=(FONT, 13),
        )
        entry.bind("<FocusIn>", lambda _: entry.configure(border_color=INPUT_BORDER_FOCUS))
        entry.bind("<FocusOut>", lambda _: entry.configure(border_color=INPUT_BORDER))
        return entry

    @staticmethod
    def _option_menu(master: ctk.CTkFrame, labels: list[str], width: int) -> ctk.CTkOptionMenu:
        """Create a sidebar-styled dropdown of labels, the first one selected."""
        return ctk.CTkOptionMenu(
            master,
            values=labels,
            fg_color=INPUT,
            button_color=INPUT,
            button_hover_color=PILL_HOVER,
            text_color=TEXT,
            dropdown_fg_color=CARD,
            dropdown_hover_color=PILL_HOVER,
            dropdown_text_color=TEXT,
            corner_radius=8,
            font=(FONT, 12),
            dropdown_font=(FONT, 12),
            dynamic_resizing=False,
            width=width,
            height=32,
        )

    def selected_format(self) -> MediaFormat:
        """The format and options chosen in the window. Main thread only."""
        if self.format_switch.get() == "MP4":
            return Mp4Format(
                quality=_choice(QUALITY_LABELS, self.quality_menu.get()),
                codec=_choice(CODEC_LABELS, self.codec_menu.get()),
            )
        return Mp3Format()

    def set_format(self, media_format: MediaFormat) -> None:
        """Select media_format and its options in the window. Main thread only."""
        match media_format:
            case Mp4Format(quality=quality, codec=codec):
                self.format_switch.set("MP4")
                self.quality_menu.set(QUALITY_LABELS[quality])
                self.codec_menu.set(CODEC_LABELS[codec])
            case Mp3Format():
                self.format_switch.set("MP3")
        self._on_format_change()

    def _on_format_change(self) -> None:
        """Show the MP4 choices only for MP4; a Download button follows the format. Main thread only."""
        if self.format_switch.get() == "MP4":
            self.mp4_options.pack(side="left")
        else:
            self.mp4_options.pack_forget()
        if self._main_command == self.start_download:  # not while it is Cancel or Next
            self._show_download_button()

    def _show_download_button(self) -> None:
        """Make the main button "Download <format>". Main thread only."""
        self._set_main_button(f"Download {self.format_switch.get()}", self.start_download, primary=True)

    def _match_title_bar(self) -> None:
        """Color the Windows 11 title bar and border like the window background.
        Older Windows ignores these attributes and keeps the dark title bar ctk sets.
        """
        try:
            import ctypes

            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            for attribute, color in ((DWMWA_CAPTION_COLOR, BG), (DWMWA_BORDER_COLOR, CARD_BORDER)):
                colorref = ctypes.c_int(int(color[5:7] + color[3:5] + color[1:3], 16))  # COLORREF is 0x00BBGGRR
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(colorref), ctypes.sizeof(colorref)
                )
        except Exception as e:  # noqa: BLE001 - cosmetic, must never keep the window from opening
            logger.debug("Could not color the title bar: %s", e)

    def _set_main_button(self, text: str, command: Callable[[], None], primary: bool = False) -> None:
        """Switch the main button's label and action; only the Download state is blue. Main thread only."""
        self._main_command = command
        self.main_button.configure(
            text=text,
            command=command,
            fg_color=ACCENT if primary else PILL,
            hover_color=ACCENT_HOVER if primary else PILL_HOVER,
            border_color=ACCENT_BORDER if primary else PILL_BORDER,
            text_color=ACCENT_TEXT if primary else "#f3f4f6",
        )

    def show_status(self, text: str, kind: str) -> None:
        """Show a status pill below the main button; empty text hides it. Main thread only.

        Args:
            text: The message, possibly multi-line.
            kind: A STATUS_STYLES key: success, error, warning or info.
        """
        self.file_actions.pack_forget()  # they belong to the "Saved" status only
        if not text:
            self.status_frame.pack_forget()
            return
        dot, color, border = STATUS_STYLES[kind]
        self.status_dot.configure(text_color=dot)
        self.status_label.configure(text=text, text_color=color)
        self.status_frame.configure(border_color=border)
        self.status_frame.pack(fill="x", pady=(14, 0))

    def show_progress(self, progress: Progress | None, media_format: MediaFormat) -> None:
        """Show where the download is, as text and in the bar. Main thread only.

        Only the download itself has a known length, so the bar fills only then; before it
        (preparing, progress None) and after it (yt-dlp's encode or merge, then the cover
        pass) it runs as a moving bar instead of sitting at 0 or 100 %.
        """
        fraction = None
        if progress is None:
            text = "Preparing..."
        elif progress.stage == "processing":
            text = "Converting to MP3..." if isinstance(media_format, Mp3Format) else "Saving the MP4..."
        elif (fraction := progress.fraction) is None:
            text = "Downloading..."
        else:
            text = f"Downloading {fraction:.0%}"
        self.show_status(text, "info")

        bar = self.progress_bar
        if fraction is None:
            if bar.cget("mode") != "indeterminate":
                bar.configure(mode="indeterminate")
                bar.start()
        else:
            if bar.cget("mode") == "indeterminate":
                bar.stop()
                bar.configure(mode="determinate")
            bar.set(fraction)
        if not bar.winfo_ismapped():
            bar.pack(fill="x", padx=60, pady=(16, 0), after=self.main_button)

    def _hide_progress(self) -> None:
        """Hide the progress bar and stop its animation. Main thread only."""
        self.progress_bar.stop()
        self.progress_bar.pack_forget()

    def show_saved(self, saved_path: Path, warnings: list[str]) -> None:
        """Show the "Saved" status with the file buttons. Main thread only."""
        self.show_status("\n".join([f"Saved {saved_path.name}", *warnings]), "warning" if warnings else "success")
        self.saved_file = saved_path
        self.file_actions.pack(pady=(10, 0))

    def _open_saved_file(self, action: Callable[[Path], None]) -> None:
        """Run show_in_folder or open_file on the saved file; a failure replaces the status."""
        if self.saved_file is None or not self.saved_file.is_file():
            self.show_status("The file was moved or deleted", "error")
            return
        try:
            action(self.saved_file)
        except OSError as e:
            logger.error("Could not open %s: %s", self.saved_file, e)
            self.show_status(f"Could not open the file: {describe_error(e)}", "error")

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
        url = self.url_entry.get().strip()
        if not is_web_url(url):
            self.show_status("Enter a valid URL", "error")
            return

        folder_path = resolve_folder(self.folder_var.get())
        if folder_path is None:
            self.show_status("Folder doesn't exist", "error")
            return
        if not folder_is_writable(folder_path):
            self.show_status("Windows doesn't allow saving in this folder, pick another one", "error")
            return
        filename = requested_filename(self.name_entry.get())
        if filename_too_long(filename, folder_path):
            self.show_status(filename_too_long_message(folder_path), "error")
            return
        self.folder_var.set(str(folder_path))
        media_format = self.selected_format()

        self.download_cancelled = False
        self.download_id += 1
        self.current_process = None
        self.current_stem = None
        self.show_progress(None, media_format)
        self._set_main_button("Cancel", self.cancel_download)

        args = (url, folder_path, filename, media_format, self.download_id)
        threading.Thread(target=self._download_thread, args=args, daemon=True).start()

    def _download_thread(
        self, url: str, folder_path: Path, filename: str, media_format: MediaFormat, download_id: int
    ) -> None:
        """Run core.download_with_repair and report the result in the window.

        Args:
            url: The video or track URL.
            folder_path: An existing folder to save the file in.
            filename: The sanitized filename; empty means name it after the video title.
            media_format: The format chosen when the download started.
            download_id: The download_id this download started with.
        """
        def _owns_window() -> bool:
            return self.download_id == download_id

        def _cancelled() -> bool:
            return self.download_cancelled or not _owns_window()

        def _show(func: Callable[..., object], *args: object) -> None:
            if _owns_window():
                self.after(0, func, *args)

        def _on_start(process: subprocess.Popen[str], temp_stem: Path) -> None:
            if not _owns_window():  # canceled during a repair and a newer download started
                kill_process_tree(process)
                return
            self.current_process = process
            self.current_stem = temp_stem
            if self.download_cancelled:  # canceled before yt-dlp was running
                kill_process_tree(process)

        def _show_progress(progress: Progress) -> None:
            # checked when it runs on the main thread: one queued before a Cancel must not replace "canceled"
            _show(lambda: _cancelled() or self.show_progress(progress, media_format))

        warnings: list[str] = []
        try:
            saved_path = download_with_repair(
                url,
                folder_path,
                filename,
                media_format=media_format,
                on_progress=_show_progress,
                on_start=_on_start,
                on_warning=warnings.append,
                # not after a Cancel: a running repair must not replace "Download canceled"
                on_info=lambda message: _cancelled() or _show(self.show_status, message, "info"),
                is_cancelled=_cancelled,
            )
        except Exception as e:
            if _cancelled():
                return
            msg = describe_error(e)
            logger.exception(msg)
            _show(self.show_status, msg, "error")
            _show(self._show_download_button)
        else:
            # also after a late Cancel: the file is saved, say so instead of "canceled"
            _show(self.show_saved, saved_path, warnings)
            _show(lambda: self._set_main_button("Next", self.reset_fields))
        finally:
            if _owns_window():
                self.current_process = None
                self.after(0, self._hide_progress)

    def cancel_download(self) -> None:
        """Cancel the running download, reset the UI and clean up its partial files."""
        self.download_cancelled = True
        if self.current_process:
            kill_process_tree(self.current_process)
            self.current_process = None

        self.show_status("Download canceled", "error")
        self._set_main_button("Next", self.reset_fields)
        self._hide_progress()

        if self.current_stem:
            threading.Thread(target=cleanup_partial_downloads, args=(self.current_stem,), daemon=True).start()

    def _close(self) -> None:
        """Close the window; a running yt-dlp is stopped and its partial files removed first."""
        self.download_id += 1
        process, stem = self.current_process, self.current_stem
        if process and process.poll() is None:
            self.withdraw()
            kill_process_tree(process)
            if stem:
                cleanup_partial_downloads(stem)
        self.destroy()

    def reset_fields(self) -> None:
        """Clear the URL and filename fields for the next download."""
        self.url_entry.delete(0, "end")
        self.name_entry.delete(0, "end")
        self.show_status("", "info")
        self._show_download_button()

    def run_native_gui(self, url: str, folder: str, filename: str, media_format: MediaFormat) -> None:
        """Prefill the form from a `download_gui` request and run the window.
        An invalid folder keeps the default folder; the window and the final
        "GUI opened" response both say so. Blocks until the window is closed.

        Args:
            url: The URL to prefill, possibly empty.
            folder: The folder to prefill, possibly empty.
            filename: The filename to prefill, possibly empty.
            media_format: The format to select.
        """
        self.set_format(media_format)
        opened_message = "GUI opened"
        if url:
            self.url_entry.insert(0, url)
        if folder:
            folder_path = resolve_folder(folder)
            if folder_path is None:
                self.show_status("Folder not found, using the default folder", "warning")
                opened_message = "GUI opened, folder not found so the default folder is used"
            else:
                self.folder_var.set(str(folder_path))
        if filename.strip():
            self.name_entry.insert(0, safe_filename(filename))

        self.after(100, lambda: send_message({"status": "success", "message": opened_message, "final": True}))
        self.mainloop()


def cleanup_partial_downloads(temp_stem: Path) -> None:
    """Remove a canceled download's leftover files.
    core.download_media already removes them when it exits; this is the fallback
    for files Windows still had locked at that moment.

    Args:
        temp_stem: The temp output stem of the canceled download.
    """
    time.sleep(CLEANUP_DELAY_S)  # the process tree is already killed, give Windows a moment to release locks
    remove_temp_files(temp_stem)


def _choice[T](labels: dict[T, str], label: str) -> T:
    """The choice a dropdown label stands for (the dropdowns only offer labels from labels)."""
    return next(choice for choice, known in labels.items() if known == label)


def open_gui(url: str, folder: str, filename: str, media_format: MediaFormat) -> None:
    """Handle `download_gui`: open the window prefilled with the request's fields.
    Blocks until the window is closed.
    """
    DownloaderApp().run_native_gui(url, folder, filename, media_format)


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


if __name__ == "__main__":
    # run as a script for testing the GUI without the extension
    open_gui("https://www.youtube.com/watch?v=jCF4dKjIM2c&list=RDjCF4dKjIM2c&start_radio=1", "", "", Mp3Format())
