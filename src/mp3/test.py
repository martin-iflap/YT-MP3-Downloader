from mp3.MP3 import safe_filename
from pathlib import Path
from PIL import Image
import subprocess
import struct
import json
import sys



ROOT_DIR = Path(__file__).parent.parent.parent
YT_DLP_PATH = ROOT_DIR / "yt-dlp.exe"
url = "https://www.youtube.com/watch?v=1wYNFfgrXTI&list=RD1wYNFfgrXTI&start_radio=1"

fetched_title = subprocess.run([
                str(YT_DLP_PATH),
                "--no-playlist",
                "--skip-download",
                "--get-title",
                url
            ], capture_output=True, text=True, check=True,
                timeout=15, stdin=subprocess.DEVNULL)

def test_safe_filename():
    print(safe_filename(fetched_title.stdout))


def create_extension_icons(source_ico: Path, output_folder: Path) -> None:
    """Convert ICO to 48x48 and 96x96 PNG icons for Firefox extension
    - creates icon-48.png and icon-96.png
    """
    output_folder.mkdir(parents=True, exist_ok=True)

    img = Image.open(source_ico)

    # Create 48x48 icon
    icon_48 = img.resize((48, 48), Image.Resampling.LANCZOS)
    icon_48.save(output_folder / "icon-48.png", "PNG")

    # Create 96x96 icon
    icon_96 = img.resize((96, 96), Image.Resampling.LANCZOS)
    icon_96.save(output_folder / "icon-96.png", "PNG")

    print(f"✓ Created icon-48.png and icon-96.png in {output_folder}")

source = ROOT_DIR / "ico/mp3.ico"
output = ROOT_DIR / "extension/icons"



def test_native_messaging():
    def send_native_message(message_dict):
        encoded = json.dumps(message_dict).encode('utf-8')
        length = struct.pack('@I', len(encoded))  # little-endian unsigned int
        return length + encoded

    message = {
        "action": "download_gui", # download_gui, download, select_folder, get_current_folder
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "folder": r"C:\Users\User\Documents\YT-MP3-Downloads",
        "filename": "   "
    }

    # prefer the bundled exe if present, otherwise run the local python module
    exe_path = ROOT_DIR / "dist" / "mp3.exe"
    if exe_path.exists():
        cmd = [str(exe_path)]
        print("exe found.")
    else:
        cmd = [sys.executable, str(ROOT_DIR / "MP3.py")]
        print("Native messaging executable not found.")
        # return

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    try:
        stdout, stderr = process.communicate(input=send_native_message(message), timeout=50)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        print("Timeout waiting for native process")

    # parse native messaging response (length-prefixed)
    if stdout and len(stdout) >= 4:
        response_length = struct.unpack('@I', stdout[:4])[0]
        response_json = stdout[4:4 + response_length].decode('utf-8', errors='replace')
        try:
            print("Response:", json.loads(response_json))
        except json.JSONDecodeError:
            print("Invalid JSON response:", response_json)
    else:
        print("No response or invalid format")

    if stderr:
        print("Errors:", stderr.decode('utf-8', errors='replace'))


if __name__ == "__main__":
    test_native_messaging()

# pyinstaller --onefile --add-binary "yt-dlp.exe;." native_messaging.py
