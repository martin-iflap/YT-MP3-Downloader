# YouTube MP3 Downloader

YouTube MP3 Downloader is a Firefox extension paired with a small Windows app that saves
the audio of YouTube videos (and SoundCloud, Bandcamp, Mixcloud tracks and other
sites) as MP3 files, with cover art and tags.

The project is split into two parts:

- **Firefox extension**: the sidebar, the right-click menu entry and the browser
  integration. Installed from Firefox Add-ons, which keeps it up to date.
- **Windows app (native host)**: does the actual download, conversion, cover art and
  tagging with `yt-dlp` and `ffmpeg`. Installed once from GitHub; from then on it keeps
  itself and `yt-dlp` up to date.

## Download and installation

Both parts are required.

### 1. Install the Firefox extension

https://addons.mozilla.org/en-US/firefox/addon/youtube-mp3-downloader

It adds a sidebar and a **Download as MP3** entry to the right-click menu of video and
track links.

### 2. Install the Windows app

Download and run the installer of the latest release:

https://github.com/martin-iflap/YT-MP3-Downloader/releases/latest/download/YT-MP3-Downloader-Setup.exe

The sidebar shows the same link whenever the app is missing or needs a reinstallation.
The installer is not code-signed, so Firefox may say the file is not commonly
downloaded, and Windows may show "Windows protected your PC": click **More info**, then
**Run anyway** or add an exception in your antivirus (add to excluded folders).

It installs per user (no administrator rights) into
`%LOCALAPPDATA%\YouTube MP3 Downloader`:

- `mp3.exe`, the native messaging host, with `ffmpeg.exe`,
- `yt-dlp.exe`, downloaded during the installation (if that fails, the app downloads it
  itself the first time you open the sidebar),
- the Firefox native-messaging registration for your user.

Windows 10/11, 64-bit (x64, or ARM64 through emulation).

### 3. Restart Firefox

Restart Firefox after installing both parts so the extension can find the app.

### Upgrading from 1.x

Version 1.x of the app can't update itself: the sidebar says so and links the installer.
Install it once over the old version (same folder, nothing to uninstall). From then on
the app updates itself.

## Usage

- Open a video or track page and use the sidebar, or right-click a link and choose
  **Download as MP3**. Any other link can be pasted into the sidebar's URL field.
- The file is named after the title (or the name you type), with cover art, artist,
  title and, when the site has them, album, year and track number.
- After a download, **Show in folder** and **Play** open the new MP3. Clicking the
  download notification shows it in its folder.
- **Open in app** opens the same download form in its own window.

The default folder is `YT-MP3-Downloads` in your Documents folder (also when Windows moved
it, e.g. into OneDrive); choose another one in
the sidebar (the right-click menu uses it too).

## Updates and repairs

Every time the sidebar opens, the extension asks the app for its version and the state
of its tools, and fixes what it can:

- **A newer app release** on GitHub is downloaded, checked against its published SHA-256
  checksum and installed silently. Each version goes into its own folder and must pass a
  self-check before Firefox is switched to it, so an interrupted or broken update leaves
  the working version in place.
- **An outdated, missing or broken `yt-dlp.exe`** is replaced by the latest release, also
  checksum-verified and test-run before it is swapped in.
- **A download that fails in yt-dlp** (from the sidebar, the right-click menu or the app
  window) updates yt-dlp and tries once more. If YouTube asks for a JavaScript challenge,
  the app downloads Deno (about 45 MB) once and uses it from then on.

Anything it can't fix is shown in the sidebar with a link to the installer, which
repairs every problem with the app. Offline, nothing is checked or changed.

## Uninstall

Uninstall **YouTube MP3 Downloader** in Windows Settings → Apps, and remove the extension
in Firefox. Your downloaded MP3s are not touched: the default download folder is removed
only if it is empty.

## Troubleshooting

- **"The app is not installed"** right after installing: restart Firefox.
- **Logs**: `%LOCALAPPDATA%\YouTube MP3 Downloader\mp3.log` (errors only). Set the
  environment variable `YTMP3_LOG_LEVEL=DEBUG` before starting Firefox for more detail.
  The log of the last self-update is in `%TEMP%\ytmp3-update\<version>\install.log`.

## How it works

The extension and the app communicate through Firefox native messaging over stdio:

1. The sidebar or the context menu sends a request to `background.js`, which starts the
   app (`mp3.exe`, registered as the native host `app_manifest`) for each request.
2. The app makes one `yt-dlp` call (audio as LAME V0 MP3 plus the thumbnail) and one
   `ffmpeg` pass (cover art and tags, the audio is copied), then answers with JSON.
3. The extension shows the result in the sidebar or as a notification.

`ARCHITECTURE.md` is the technical reference: the protocol, every module, and the update
and repair design.

## Repository layout

- `extension/`: Firefox extension source files (zipped for AMO).
- `host/src/ytmp3_host/`: the Windows app (native host), including the updater.
- `host/tests/`: a manual smoke test for the native host.
- `host/bin/`: local `ffmpeg.exe` / `yt-dlp.exe` for development (not committed).
- `tests/`: automated tests of the extension (`tests/extension/`) and the host
  (`tests/host/`).
- `packaging/installer/`: Inno Setup script for the Windows installer;
  `packaging/write_checksums.py` writes a release's `SHA256SUMS`.
- `packaging/assets/`: application icons.
- `packaging/app_manifest.dev.json`: native host manifest template for local development.
- `legacy/`: earlier scripts and experiments; not part of the shipped app.

## Development setup

Dependencies are managed with [uv](https://docs.astral.sh/uv/) (Python 3.15+):

```
uv sync
```

Put `ffmpeg.exe` and `yt-dlp.exe` into `host/bin/` to run the host unpackaged. Tests and
type check:

```
uv run pytest                  # host tests (network tests are opt-in: -m network)
uv run pyrefly check           # type check
npm install                    # once: ESLint and addons-linter (dev tooling only, nothing ships)
npm test                       # extension tests (node --test tests/extension/)
npm run lint                   # ESLint, then addons-linter (the checks AMO runs on an upload)
```

Build the executable from the repo root with:

```
uv run pyinstaller --onedir --noconfirm --name mp3 --exclude-module PIL --icon "packaging\assets\mp3.ico" host\src\ytmp3_host\MP3.py
```

Then compile `packaging/installer/mp3_installer.iss` with
[Inno Setup 6](https://jrsoftware.org/isinfo.php). It picks up `dist\mp3\` (`mp3.exe`
plus `_internal\`), `host\bin\ffmpeg.exe` and `packaging\assets\mp3.ico` relative to the
repo root, and writes `packaging\installer\output\YT-MP3-Downloader-Setup.exe`.

Installed apps update themselves from the latest GitHub release, so publishing one is a
deployment: follow the release checklist in `CLAUDE.md` (version bump, `SHA256SUMS`, tag
`vX.Y.Z`).
