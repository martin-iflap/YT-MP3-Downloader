# YouTube MP3 Downloader

YouTube MP3 Downloader is a Firefox extension paired with a native Windows backend for downloading the audio track of supported YouTube videos as MP3 files.

The project is split into two deployable parts:

- **Firefox extension** — provides the user interface, context menu entry, and browser integration.
- **Python native host** — performs the actual download, conversion, metadata embedding, and file handling on Windows.

## Architecture

The extension and backend communicate through Firefox native messaging.

1. The user starts a download from the sidebar or the right-click context menu.
2. `background.js` validates the request and opens the native host named `app_manifest`.
3. Firefox sends a JSON message over `stdio` to the packaged Python executable (`mp3.exe`).
4. The backend resolves the target folder, sanitizes the output filename, and calls `yt-dlp` to fetch and convert the audio stream.
5. `ffmpeg` is used to finalize the MP3 file and embed thumbnail metadata when available.
6. The backend sends a structured JSON response back to the extension so the UI can report success, warnings, or errors.

### Backend responsibilities

The Python code under `host/src/ytmp3_host` is responsible for:

- validating and normalizing filenames,
- creating the default download folder,
- downloading audio with `yt-dlp`,
- converting and post-processing with `ffmpeg`,
- embedding thumbnails into the final MP3 file,
- handling native-messaging requests from Firefox.

### Extension responsibilities

The Firefox extension under `extension/` is responsible for:

- exposing the sidebar UI,
- adding the YouTube link context menu item,
- forwarding download requests to the native host,
- showing notifications when the sidebar is not open,
- relaying backend status messages back to the UI.

## Download and installation

The browser extension and the backend installer are distributed separately. Both are required.

### 1. Install the Firefox extension

Open the Firefox Add-ons listing for this extension and install it from there:

https://addons.mozilla.org/en-US/firefox/addon/youtube-mp3-downloader

After installation, the extension will add a sidebar entry and a context menu item for YouTube links.

### 2. Install the backend from the GitHub release

Download the Windows installer from the GitHub latest Release and run it.

The installer performs the backend setup automatically. It installs:

- `mp3.exe` — the native messaging host,
- `ffmpeg.exe` — audio processing and MP3 finalization,
- `yt-dlp.exe` — fetched during installation,
- the Firefox native-messaging manifest in the user profile.

### 3. Restart Firefox

Restart Firefox after installing both components so the extension can connect to the native host.

## Usage

- Open a supported YouTube video page.
- Use the sidebar or right-click a YouTube link and choose **Download as MP3**.
- The backend writes files to the configured download folder and returns the final status to the browser.

The default output directory is:

`%USERPROFILE%\Documents\YT-MP3-Downloads`

## Implementation notes

- Native messaging is configured through the host name `app_manifest`.
- The Firefox extension ID is `yt.downloader@converter.org`.
- The backend sanitizes output filenames to remove invalid path characters and common YouTube title noise.
- Thumbnail embedding is performed only when the downloaded media includes a thumbnail image.

## Repository layout

- `extension/` — Firefox extension source files.
- `host/src/ytmp3_host/` — Python backend and native-messaging implementation.
- `host/tests/` — manual smoke tests for the native host.
- `host/bin/` — local `ffmpeg.exe` / `yt-dlp.exe` for development (not committed).
- `packaging/installer/` — Inno Setup script for the Windows installer.
- `packaging/assets/` — application icons.
- `packaging/app_manifest.dev.json` — native host manifest template for local development.
- `legacy/` — earlier standalone scripts (`v0/`) and the MP4 experiment (`mp4/`); not part of the shipped host.

## Development setup

Dependencies are managed with [uv](https://docs.astral.sh/uv/) (Python 3.14+):

```
uv sync
```

Put `ffmpeg.exe` and `yt-dlp.exe` into `host/bin/` to run the host unpackaged. Build the executable from the repo root with:

```
uv run pyinstaller --onefile --name mp3 --icon "packaging\assets\mp3.ico" host\src\ytmp3_host\MP3.py
```

Then compile `packaging/installer/mp3_installer.iss` with [Inno Setup 6](https://jrsoftware.org/isinfo.php). It picks up `dist\mp3.exe` and `host\bin\ffmpeg.exe` relative to the repo root.
