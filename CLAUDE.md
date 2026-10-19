# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A Firefox extension (`extension/`) paired with a Windows-only Python native-messaging host (`host/src/ytmp3_host/`) that downloads YouTube audio as MP3 via `yt-dlp.exe` + `ffmpeg.exe`. The two halves are deployed separately (extension via AMO, backend via an Inno Setup installer) and communicate only through Firefox native messaging over stdio.

## Layout

- `extension/` — the Firefox add-on (zip this folder for AMO).
- `host/src/ytmp3_host/` — the native host package; `host/tests/` — manual smoke test; `host/bin/` — dev-only `ffmpeg.exe`/`yt-dlp.exe` (gitignored).
- `packaging/installer/` — Inno Setup script (+ `.build/*.build.xml` IDE configs); `packaging/assets/` — icons; `packaging/app_manifest.dev.json` — dev native-host manifest template.
- `legacy/v0/`, `legacy/mp4/`, `legacy/popup/` (old toolbar popup) — superseded/experimental code kept for reference. Not packaged; `legacy/v0` still imports the old `mp3` package name, so it does not run as-is.

## Commands

Dependencies are managed with **uv** (Python >= 3.14, see `pyproject.toml`; PyInstaller is in the `dev` dependency group).

```
uv sync                                   # install deps into .venv
uv run python -m ytmp3_host.MP3           # run the native host (blocks reading length-prefixed JSON on stdin)
uv run python host/tests/smoke_native.py  # manual native-messaging smoke test
```

Package the native host from the repo root (output `dist/mp3.exe`; the command is also recorded at the bottom of `MP3.py`). Keep `--name mp3`: installed v1 manifests point at `mp3.exe`.

```
uv run pyinstaller --onefile --name mp3 --icon "packaging\assets\mp3.ico" host\src\ytmp3_host\MP3.py
```

Build the installer by compiling `packaging/installer/mp3_installer.iss` with Inno Setup 6. Sources resolve via `{#RepoRoot}` relative to the script: `dist\mp3.exe`, `host\bin\ffmpeg.exe`, `packaging\assets\mp3.ico`.

Extension tests use Node's built-in runner (no npm dependencies). `tests/extension/harness.js` loads the extension scripts into a `node:vm` sandbox with a fake `browser` object; tests live outside `extension/` so they don't ship to AMO.

```
node --test tests/extension/
```

There is no Python test suite or linter configured. `host/tests/smoke_native.py` is not pytest-safe: it shells out to `yt-dlp.exe` at import time and spawns `dist/mp3.exe` (falling back to `python -m ytmp3_host.MP3`) with a hardcoded message.

## Architecture

### Message flow
1. `sidebar.js` / context menu → `browser.runtime.sendMessage({type: 'native_action', action, url, folder, filename})`.
2. `background.js` whitelists actions (`download`, `download_gui`, `select_folder`, `get_current_folder`) with a per-action timeout (`ACTION_TIMEOUTS_MS`), opens a **new** `connectNative('app_manifest')` port per request, and tags responses with the caller's `requestId`. After the final response it disconnects the port (which ends the host process), except for `download_gui`, whose process must stay alive to run the Tk window.
3. `MP3.main()` loops on `read_message()` and dispatches by `action` (unknown/missing action falls back to `download`).
4. Responses are dicts with `status` (`success`/`error`/`warning`), optional `message`/`filename`/`path`/`folder`, and `final`. `background.js` relays every response to the UI as `type: 'native_response'` and guarantees exactly one `final: true` message per request: the host's own, a synthesized error on timeout, or one on port disconnect (Firefox puts the reason, e.g. host not installed, on `port.error`). Intermediate warnings must use `final: false`.
5. Context-menu downloads send the sidebar's saved folder (`storage.local.lastFolder`) when there is one. Desktop notifications are shown for `download` results only: always for context-menu downloads, and for sidebar downloads only if the sidebar is closed when the result arrives.
6. `extension/youtube.js` holds `isYouTubeUrl`, loaded as a plain script by both the background and the sidebar.

### Backend modules (`host/src/ytmp3_host/`)
- `config.py` — resolves `ffmpeg.exe`, `yt-dlp.exe`, `mp3.ico` next to `sys.executable` when frozen (PyInstaller). Unfrozen, the binaries come from `host/bin/` (place them there manually) and the icon from `packaging/assets/`. `DEFAULT_FOLDER` is `~/Documents/YT-MP3-Downloads`.
- `native_messaging.py` — stdio framing (`read_message`/`send_message`, 4-byte native-endian length prefix) and `run_native()`, the headless download path: validates URL, resolves folder (falls back to default with a warning), fetches the title via `yt-dlp --get-title` when no filename is given, then calls `core.download_audio_headless`.
- `core.py` — shared logic: `safe_filename` (strips invalid chars and YouTube title noise like "Official Video"), `ensure_default_folder`, `embed_thumbnail` (Pillow center-crops the thumbnail to 1:1, ffmpeg remuxes it as ID3v2.3 cover art via a `_temp<pid>.mp3` file), and `download_audio_headless`. Artist metadata is the text before the first `-` in the filename.
- `MP3.py` — entry point. Also contains the customtkinter GUI used by `download_gui`/`select_folder`; the GUI (`root = ctk.CTk()`) is **constructed at module import time**, so importing `ytmp3_host.MP3` creates a Tk window. The GUI download path duplicates the yt-dlp invocation from `core.py` (with progress parsing and cancel/cleanup), so changes to yt-dlp arguments must be made in both places.

### Constraints to keep in mind
- **stdout is the native-messaging channel.** Never `print()` in backend code that runs under the host; use `logging` (writes `mp3.log` in the CWD, ERROR level).
- The identifiers `app_manifest` (native host name) and `yt.downloader@converter.org` (extension ID) must match across `background.js`, `extension/manifest.json`, `packaging/app_manifest.dev.json`, and the `#define`s in `packaging/installer/mp3_installer.iss`. Changing either breaks existing v1 installs and AMO updates.
- The installer writes the native-messaging manifest to `%APPDATA%\Mozilla\NativeMessagingHosts\app_manifest.json` and registers it under `HKCU\Software\Mozilla\NativeMessagingHosts\app_manifest`; it downloads the latest `yt-dlp.exe` at install time rather than bundling it.
- URL validation differs between halves: the extension parses the URL strictly (`youtube.js`), while the Python side only checks for `"youtube.com"` or `"youtu.be/"` in the string.
- Downloads can legitimately run for minutes (host: 15s title lookup + 300s yt-dlp timeout); keep `ACTION_TIMEOUTS_MS.download` above that.
