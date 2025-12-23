from core import safe_filename, download_audio_headless
from config import YT_DLP_PATH, DEFAULT_FOLDER
from notifications import send_notification
from mp3_2 import url_var, folder_var, root
from pathlib import Path
import subprocess
import argparse
import logging


def run_argparse() -> None:
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

def run_auto_mode(arguments: argparse.Namespace) -> None:
    """Run the auto-download mode based on command-line arguments
     - Fetch video title from YouTube else use default
     - Download audio headlessly without GUI interaction
     - Send desktop notifications on completion or errors
    """
    if not arguments.url:
        send_notification("Error", "Missing URL for auto mode")
        exit(1)

    input_url = arguments.url
    logging.info(f"url: {arguments.url}")

    if arguments.folder:
        input_folder = Path(arguments.folder).resolve()
        if not input_folder.is_dir():
            send_notification("Warning", "Provided folder not found, using default")
            input_folder = DEFAULT_FOLDER
    else:
        input_folder = DEFAULT_FOLDER

    try:
        logging.info("Fetching title for auto mode...")
        fetched_title = subprocess.run([
            str(YT_DLP_PATH),
            "--no-playlist",
            "--skip-download",
            "--get-title",
            input_url
        ], capture_output=True, text=True, check=True,
            timeout=15, stdin=subprocess.DEVNULL)
        title = safe_filename(fetched_title.stdout or "Youtube MP3")
        logging.info(f"title: {title}")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        title = "Youtube MP3"
        send_notification("Warning", "Failed to fetch title, using default.")

    download_audio_headless(input_url, input_folder, title)
    send_notification("Download Completed", f"Download completed in auto mode.\nFile: {title} saved to {input_folder}")
    exit(0)