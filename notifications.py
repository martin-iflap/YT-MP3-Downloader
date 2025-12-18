from win10toast_click import ToastNotifier
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ICON_PATH = SCRIPT_DIR / "mp3_new.ico"

toaster = ToastNotifier()

def send_notification(title: str, message: str) -> None:
    """Send a desktop notification with the given title and message
     - use an icon form ICON_PATH if it exists
     - duration is set to 5 seconds
    """
    try:
        toaster.show_toast(
            title=title,
            msg=message,
            icon_path=ICON_PATH if ICON_PATH.exists() else None,
            duration=5,
            threaded=True,
        )
    except Exception as e:
        print(f"Failed to send notification: {e}") # check this later

if __name__ == "__main__":
    send_notification("Download Complete", "Your YouTube video has been downloaded successfully.")