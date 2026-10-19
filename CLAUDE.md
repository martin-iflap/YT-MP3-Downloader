# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A Firefox extension (`extension/`) paired with a Windows-only Python native-messaging host (`host/src/ytmp3_host/`) that downloads YouTube audio as MP3 via `yt-dlp.exe` + `ffmpeg.exe`. The two halves are deployed separately (extension via AMO, backend via an Inno Setup installer) and communicate only through Firefox native messaging over stdio.

## Project priorities

1. **Speed with the best quality.** Downloads must be as fast as possible without lowering audio or cover quality: MP3 is always LAME V0 (`--audio-quality 0`, which encodes as fast as the default V5; 320k CBR is ~2x slower to encode), cover art is a high-quality JPEG (`-q:v 2`). Measure before and after any change to the download pipeline, and never trade quality for speed silently.
2. **Easy to use, install and update.** The main scope of v2 is auto-repair and auto-update logic (e.g. keeping `yt-dlp.exe` current, detecting and fixing a broken host install), so users never have to reinstall manually.

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

Package the native host from the repo root (output `dist/mp3/mp3.exe` plus `dist/mp3/_internal/`; the command is also recorded at the bottom of `MP3.py`). Keep `--name mp3`: installed v1 manifests point at `mp3.exe`. Use `--onedir`, not `--onefile`: onefile unpacks itself to a temp folder on every launch, and every request starts a new host process. When frozen, `ffmpeg.exe`/`yt-dlp.exe`/`mp3.ico` must sit next to `mp3.exe` (copy them into `dist/mp3/` to run the build directly).

```
uv run pyinstaller --onedir --noconfirm --name mp3 --icon "packaging\assets\mp3.ico" host\src\ytmp3_host\MP3.py
```

Build the installer by compiling `packaging/installer/mp3_installer.iss` with Inno Setup 6. Sources resolve via `{#RepoRoot}` relative to the script: `dist\mp3\*` (recursive), `host\bin\ffmpeg.exe`, `packaging\assets\mp3.ico`.

Extension tests use Node's built-in runner (no npm dependencies). `tests/extension/harness.js` loads the extension scripts into a `node:vm` sandbox with a fake `browser` object; tests live outside `extension/` so they don't ship to AMO.

```
node --test tests/extension/
```

Type-check the host with pyrefly (configured in `pyproject.toml` `[tool.pyrefly]`: checks `host/`, excludes `legacy/`; keep it at 0 errors). The explicit `project-includes` matters: `host/src` is on the interpreter path as an editable install, so without it pyrefly skips the host package entirely.

```
uv run pyrefly check
```

There is no Python test suite. `host/tests/smoke_native.py` is not pytest-safe: it shells out to `yt-dlp.exe` at import time and spawns `dist/mp3/mp3.exe` (falling back to `python -m ytmp3_host.MP3`) with a hardcoded message.

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
- `native_messaging.py` — stdio framing (`read_message`/`send_message`, 4-byte native-endian length prefix) and `run_native()`, the headless download path: validates URL, resolves folder (falls back to default with a warning), then calls `core.download_audio` (an empty filename means "name it after the video title").
- `core.py` — shared logic: `safe_filename` (strips invalid chars and YouTube title noise like "Official Video"), `ensure_default_folder`, and `download_audio`, the single download pipeline used by both the headless and GUI paths:
  1. **One** `yt-dlp` call (`build_download_command`) downloads the audio, encodes MP3 V0, writes the raw thumbnail, and prints the title after `TITLE_MARKER`. Output goes to a unique `_ytmp3_<hex>` temp stem, so the title is only needed afterwards; there is no separate `--get-title` call. When the title decides the name, the "file already exists" check runs after the download.
  2. **One** `ffmpeg` pass (`embed_thumbnail`) copies the audio (no re-encode), center-crops the thumbnail to 1:1, encodes it as JPEG ID3v2.3 cover art, sets the artist, and writes the final `<filename>.mp3` directly. Artist metadata is the text before the first `-` in the filename.
  3. Temp files are always removed in `finally`. Raises `FileExistsError` (with `.filename`), `subprocess.TimeoutExpired` (`DOWNLOAD_TIMEOUT_S`, enforced by a watchdog) or `CalledProcessError`.
- `MP3.py` — entry point: the `ACTIONS` dispatch table and `main()` loop. It imports `gui` only inside the `download_gui`/`select_folder` handlers, so headless `download`/`get_current_folder` requests never load customtkinter/Tk.
- `gui.py` — the customtkinter window (`DownloaderApp(ctk.CTk)`) plus `open_gui`/`select_folder`. Downloads go through `core.download_audio` (`on_progress`/`on_start` callbacks for the progress bar and cancel). Tk variables are read from the download thread, which only works while `mainloop()` is running.

### Constraints to keep in mind
- **stdout is the native-messaging channel.** Never `print()` in backend code that runs under the host; use `logging` (writes `mp3.log` in the CWD, ERROR level). Child processes must get `stdin=subprocess.DEVNULL` (and ffmpeg `-nostdin`), or they can consume bytes from the native-messaging stdin.
- `yt-dlp.exe` is a PyInstaller onefile build: `Popen.kill()` only stops its bootloader while the real process keeps running and holds the pipe open. Stop it with `core.kill_process_tree` (`taskkill /T`).
- Keep the host startup path light: no heavy imports (customtkinter, Pillow) at module level in anything the headless `download` path imports. Pillow is a dev-only dependency (icon helper in the smoke test).
- The identifiers `app_manifest` (native host name) and `yt.downloader@converter.org` (extension ID) must match across `background.js`, `extension/manifest.json`, `packaging/app_manifest.dev.json`, and the `#define`s in `packaging/installer/mp3_installer.iss`. Changing either breaks existing v1 installs and AMO updates.
- The installer writes the native-messaging manifest to `%APPDATA%\Mozilla\NativeMessagingHosts\app_manifest.json` and registers it under `HKCU\Software\Mozilla\NativeMessagingHosts\app_manifest`; it downloads the latest `yt-dlp.exe` at install time rather than bundling it.
- URL validation differs between halves: the extension parses the URL strictly (`youtube.js`), while the Python side only checks for `"youtube.com"` or `"youtu.be/"` in the string.
- Downloads can legitimately run for minutes (host: 300s `DOWNLOAD_TIMEOUT_S` for the whole yt-dlp call); keep `ACTION_TIMEOUTS_MS.download` above that.
