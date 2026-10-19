# Architecture

Technical reference for the extension, the native host, and the protocol between them.
For what the project does and how to install it, see `README.md`.

---

## 1. System overview

Two independently built and deployed components, connected only by Firefox native
messaging over stdio.

```mermaid
flowchart TB
    subgraph FF["Firefox"]
        SB["sidebar.html + sidebar.js<br/>(UI)"]
        CM["Context menu<br/>'Download as MP3'"]
        BG["background.js<br/>action whitelist + timeouts<br/>one native port per request<br/>response relay + notifications"]
        SB -- "runtime.sendMessage" --> BG
        CM -- "contextMenus.onClicked" --> BG
        BG -- "native_response" --> SB
    end

    subgraph HOST["mp3.exe (PyInstaller build of MP3.py)"]
        MAIN["MP3.main()<br/>dispatch by action"]
        RN["native_messaging.run_native<br/>(headless download)"]
        GUI["MP3.py GUI<br/>(customtkinter window, folder picker)"]
        CORE["core.download_audio_headless"]
        GDL["MP3._download_audio_thread"]
        MAIN --> RN --> CORE
        MAIN --> GUI --> GDL
    end

    subgraph TOOLS["External binaries"]
        YT["yt-dlp.exe"]
        FFM["ffmpeg.exe"]
        EMB["core.embed_thumbnail"]
        YT --> FFM --> EMB
    end

    BG <-- "stdin / stdout<br/>4-byte length + JSON" --> MAIN
    CORE --> YT
    GDL --> YT
    EMB --> OUT[("~/Documents/YT-MP3-Downloads/<br/>title.mp3")]
```

| Component | Source | Language | Artifact | Distribution |
|-----------|--------|----------|----------|--------------|
| Extension | `extension/` | JavaScript, Manifest V2, plain scripts (no bundler, no modules) | Zip of `extension/` | AMO |
| Native host | `host/src/ytmp3_host/` | Python ≥ 3.14 | `mp3.exe` (PyInstaller `--onefile`) | Inno Setup installer |

The extension is a UI and message router only. All file system access, process
execution and dialogs live in the host, because WebExtensions cannot do either.

### Where to look

| Task | Start in |
|------|----------|
| Sidebar UI or behaviour | `extension/sidebar.js`, `extension/sidebar.html` |
| Request routing, timeouts, notifications, context menu | `extension/background.js` |
| Add or change a host action | `MP3.main()` + `ACTION_TIMEOUTS_MS` in `background.js` (see §10) |
| Headless download logic | `native_messaging.run_native`, `core.download_audio_headless` |
| yt-dlp arguments | `core.download_audio_headless` **and** `MP3._download_audio_thread` |
| Filename cleanup, cover art | `core.safe_filename`, `core.embed_thumbnail` |
| Binary/icon paths, default folder | `host/src/ytmp3_host/config.py` |
| Installer, host registration | `packaging/installer/mp3_installer.iss` |

---

## 2. Repository layout

```
extension/                  Firefox add-on; zipped as-is for AMO
  manifest.json             MV2 manifest: permissions, background scripts, sidebar, extension ID
  background.js             Message router, native-messaging client, context menu, notifications
  sidebar.html              Sidebar markup and inline CSS
  sidebar.js                Sidebar logic: fields, buttons, request/response tracking
  youtube.js                isYouTubeUrl(), shared by background and sidebar
  icons/                    48/96 px PNGs

host/
  src/ytmp3_host/
    MP3.py                  Entry point (request loop + dispatch) and the customtkinter GUI
    native_messaging.py     stdio framing, run_native() (headless download handler)
    core.py                 Download, filename sanitizing, thumbnail embedding
    config.py               Paths to ffmpeg.exe, yt-dlp.exe, mp3.ico; DEFAULT_FOLDER
  tests/smoke_native.py     Manual script: spawns the host and sends one message
  bin/                      (gitignored) ffmpeg.exe, yt-dlp.exe for running from source

packaging/
  installer/mp3_installer.iss   Inno Setup script
  app_manifest.dev.json         Native-messaging manifest template for dev machines
  assets/                       mp3.ico

tests/extension/            Node test-runner tests; outside extension/ so they are not shipped
legacy/                     Superseded code, reference only. Not packaged or imported.
pyproject.toml, uv.lock     Python dependencies (pyinstaller in the dev group)
```

---

## 3. Native messaging

### 3.1 Host discovery

```mermaid
flowchart LR
    A["background.js<br/>connectNative('app_manifest')"] --> B["Registry<br/>HKCU\Software\Mozilla\<br/>NativeMessagingHosts\app_manifest"]
    B -- "default value = path to" --> C["%APPDATA%\Mozilla\<br/>NativeMessagingHosts\<br/>app_manifest.json"]
    C --> D{"caller's extension ID in<br/>allowed_extensions?"}
    D -- yes --> E["Firefox starts 'path'<br/>(mp3.exe) with stdio<br/>attached to the port"]
    D -- no --> F["port disconnects,<br/>reason on port.error"]
    B -. "missing key / wrong name" .-> F
```

Host manifest, written by the installer:

```json
{
  "name": "app_manifest",
  "description": "YouTube MP3 Converter and Downloader",
  "path": "C:\\Users\\...\\AppData\\Local\\YouTube MP3 Downloader\\mp3.exe",
  "type": "stdio",
  "allowed_extensions": ["yt.downloader@converter.org"]
}
```

Any lookup failure (missing key, wrong name or path, ID not allowed) disconnects the port
immediately with the reason on `port.error`. `background.js` converts this into an error
response; it is what the user sees when the host is not installed.

**Identifiers that must match everywhere.** Changing any of them breaks existing installs
and AMO updates.

| Identifier | Value | Defined in |
|------------|-------|------------|
| Native host name | `app_manifest` | `background.js` (`NATIVE_HOST_NAME`), `mp3_installer.iss` (`NativeHostName`), `app_manifest.dev.json` (`name`), registry subkey, manifest filename |
| Extension ID | `yt.downloader@converter.org` | `extension/manifest.json` (`gecko.id`), `mp3_installer.iss` (`ExtensionID`), `app_manifest.dev.json` (`allowed_extensions`) |
| Executable name | `mp3.exe` | PyInstaller `--name mp3`, `mp3_installer.iss` (`NativeExeName`) |

### 3.2 Wire format

Both directions:

```
[ 4 bytes: length N, unsigned int, native byte order ][ N bytes: UTF-8 JSON ]
```

Implemented in `native_messaging.py`:

- `read_message()`: reads the length via `struct.unpack('@I', ...)`, then N bytes, then
  `json.loads`. On EOF (stdin closed) it calls `sys.exit(0)`; this is the normal shutdown
  path.
- `send_message(dict)`: writes length + JSON to `sys.stdout.buffer` and flushes. On write
  failure (pipe closed) it logs and exits.

stdout is the protocol channel. Any other output to stdout corrupts the stream: no
`print()` in host code. Use `logging` (`mp3.log`, ERROR level).

### 3.3 Process lifetime

`connectNative()` spawns a new process per call, and the extension opens one port per
request, so every request runs in a fresh `mp3.exe`.

```mermaid
sequenceDiagram
    participant BG as background.js
    participant H as mp3.exe

    BG->>H: connectNative() spawns process
    Note over H: module import builds the Tk root
    BG->>H: postMessage(payload) via stdin
    Note over H: main() read_message(), dispatch action
    opt zero or more
        H-->>BG: send_message(final false) via stdout
    end
    H-->>BG: send_message(final true) via stdout
    BG->>H: port.disconnect() closes stdin
    Note over H: main() loops, read_message() gets EOF, sys.exit(0)
```

Exception: `download_gui`. Firefox kills the host when its port disconnects, and the Tk
window must outlive the request, so `background.js` leaves that port open
(`KEEP_ALIVE_ACTIONS`). The host sends its final `"GUI opened"` response, then runs
`root.mainloop()` until the window is closed.

---

## 4. Message protocol

### 4.1 Requests (extension → host)

```json
{ "action": "download", "url": "https://...", "folder": "C:\\...", "filename": "..." }
```

| `action` | Fields | Host behaviour | Timeout (`background.js`) |
|----------|--------|----------------|---------------------------|
| `download` | `url`, `folder?`, `filename?` | Headless download, `run_native()` | 6 min |
| `download_gui` | `url?`, `folder?`, `filename?` | Opens the pre-filled Tk window, `run_native_gui()` | 30 s (until `"GUI opened"`) |
| `select_folder` | — | Folder picker, returns the selected folder | 10 min (user interaction) |
| `get_current_folder` | — | Returns the host's current/default folder | 15 s |

The host treats a missing or unknown `action` as `download`. `background.js` rejects
unknown actions before spawning the host, so this path is not reached from the extension.

### 4.2 Responses (host → extension)

```json
{ "status": "success" | "error" | "warning",
  "message": "...", "filename": "...", "path": "...", "folder": "...",
  "final": true | false }
```

| Field | Meaning |
|-------|---------|
| `final: false` | Intermediate message; request still running. Currently only warnings ("Invalid folder path, using default", "Could not fetch title, using default"). |
| `final: true` | Last message for the request. Exactly one per request. |
| `filename`, `path` | Set on a successful download. |
| `folder` | Set by folder actions. |

If `final` is absent, `background.js` treats `success`/`error` as final and anything else
as intermediate.

### 4.3 Extension envelope

`background.js` relays every response to extension pages as:

```json
{ "type": "native_response", "requestId": "<uuid>", "final": true, "...": "host fields" }
```

`requestId` correlates a response with the sidebar call that issued it.

---

## 5. Extension

All files are plain scripts sharing one global scope per page. `youtube.js` is loaded
before the page script in both the background page and the sidebar, which makes
`isYouTubeUrl` a global in both.

```mermaid
flowchart LR
    YT["youtube.js<br/>isYouTubeUrl()"]
    BG["background.js"]
    SB["sidebar.js"]
    HTML["sidebar.html"]
    MAN["manifest.json"]

    MAN -- "background.scripts" --> YT
    MAN -- "background.scripts" --> BG
    MAN -- "sidebar_action.default_panel" --> HTML
    HTML -- "script tag" --> YT
    HTML -- "script tag" --> SB
    YT -. "global" .-> BG
    YT -. "global" .-> SB
    SB <-- "native_action / native_response<br/>(runtime messages)" --> BG
```

### 5.1 `background.js`

```mermaid
flowchart TB
    CMC["contextMenus.onClicked<br/>(origin 'menu')"]
    ROM["runtime.onMessage<br/>type 'native_action'<br/>(origin 'sidebar')"]
    RNA["runNativeAction(payload, requestId, origin)"]
    FIN["finish(response, disconnect)<br/>runs once per request (done flag)"]
    REL["relay()<br/>runtime.sendMessage to sidebar"]
    NDR["notifyDownloadResult()<br/>download action only"]
    NOT["notifications.create"]
    DIS["port.disconnect()<br/>unless KEEP_ALIVE_ACTIONS"]

    CMC -- "isYouTubeUrl + lastFolder" --> RNA
    ROM -- "action whitelisted?" --> RNA
    ROM -- "not whitelisted" --> REL
    RNA --> FIN
    RNA -- "final false messages" --> REL
    FIN --> REL
    FIN --> NDR --> NOT
    FIN --> DIS
```

| Function / listener | Responsibility |
|---------------------|----------------|
| `contextMenus.create` (top level) | Registers "Download as MP3" for YouTube watch/shorts/youtu.be links. Runs on every startup because MV2 persistent backgrounds do not keep menus across restarts. |
| `runtime.onMessage` | Accepts `native_action` from the sidebar. Rejects actions not in `ACTION_TIMEOUTS_MS`. Copies only `action`, `url`, `folder`, `filename` into the payload. |
| `contextMenus.onClicked` | Validates the link, adds `storage.local.lastFolder` if set, starts a `download`. |
| `runNativeAction(payload, requestId, origin)` | Opens the port, posts the payload, starts the timeout. Guarantees exactly one final response via `finish()`. |
| `relay(message)` | Sends `native_response` to extension pages. Rejection (no page open) is ignored. |
| `notifyDownloadResult(origin, response)` | `download` only. Context-menu downloads always notify; sidebar downloads notify only if the sidebar is closed when the result arrives. |

`runNativeAction` completion: the first trigger wins, later ones are ignored.

```mermaid
stateDiagram-v2
    [*] --> Waiting: connectNative + postMessage, timer started
    Waiting --> Waiting: host sends final false (relayed)
    Waiting --> Finished: host sends final true
    Waiting --> Finished: timeout (ACTION_TIMEOUTS_MS)
    Waiting --> Finished: port.onDisconnect (crash, exit, not installed)
    [*] --> Finished: connectNative or postMessage throws
    Finished --> [*]: clear timer, relay final, notify, maybe disconnect
```

### 5.2 `sidebar.js`

| Area | Behaviour |
|------|-----------|
| Request tracking | `sendNativeAction(message, onResponse)` generates a `requestId` and stores the callback in `pendingRequests`. The `runtime.onMessage` listener dispatches each response by `requestId` and removes the entry on `final`. |
| URL field | `syncUrlFromActiveTab()` mirrors the active tab's URL when it is a YouTube video, on tab switch and in-page navigation. Skipped once the user types a URL (`autofilledUrl` tracks the last autofilled value). |
| Folder field | `loadFolder()` uses `storage.local.lastFolder`; if unset, requests `get_current_folder` and stores the result. "Choose" sends `select_folder`. Manual edits are saved on `change`; an empty value removes the key. Context-menu downloads use the same key. |
| Download button | Validates with `isYouTubeUrl`, sends `download`. Intermediate responses show as info/warning, the final one as success/error. |
| Open in app button | Sends `download_gui` with whichever fields are filled. |
| `showResult()` / `clearResult()` | Status pill; success messages auto-hide after 5 s. |

### 5.3 `youtube.js`

`isYouTubeUrl(value)` accepts http(s) URLs on `youtube.com`, `www.`, `m.`, `music.` or
`youtu.be`, with path `/watch?v=…`, `/shorts/…`, or a non-empty `youtu.be/<id>`.

The host validates much more loosely (substring `youtube.com` / `youtu.be/` in
`run_native`; only `youtube.com` in the GUI). The extension check is the effective
validation.

---

## 6. Native host

### 6.1 Module dependencies

```mermaid
flowchart BT
    CFG["config.py<br/>paths + DEFAULT_FOLDER<br/>(no project imports)"]
    CORE["core.py"]
    NM["native_messaging.py"]
    MP3["MP3.py<br/>entry point, builds GUI at import"]

    CORE --> CFG
    NM --> CORE
    NM --> CFG
    MP3 --> NM
    MP3 --> CORE
    MP3 --> CFG
```

Arrows point from importer to imported. `MP3.py` is the only entry point; `__init__.py`
is empty.

### 6.2 `config.py`

| Mode | Detection | `ffmpeg.exe`, `yt-dlp.exe` | `mp3.ico` |
|------|-----------|----------------------------|-----------|
| Installed | `sys.frozen` (PyInstaller) | Directory of `sys.executable` | Directory of `sys.executable` |
| Source checkout | otherwise | `host/bin/` (placed manually) | `packaging/assets/` |

`DEFAULT_FOLDER = ~/Documents/YT-MP3-Downloads`.

### 6.3 `MP3.py`

**Module-level GUI construction.** `root = ctk.CTk()`, the `url_var` / `folder_var` /
`filename_var` StringVars and all widgets are created at import. Consequences:

- Every host process creates a Tk root, including headless downloads.
- Importing `ytmp3_host.MP3` anywhere creates a window.
- `folder_var` starts as `DEFAULT_FOLDER` in every process.

**`main()` request loop.**

```mermaid
flowchart TB
    R["read_message()"] -- "EOF" --> X["sys.exit(0)"]
    R --> D{"action"}
    D -- "download / unknown / missing" --> RN["native_messaging.run_native(url, folder, filename)"]
    D -- "download_gui" --> RG["run_native_gui(url, folder, filename)"]
    D -- "select_folder" --> SF["choose_folder() then send_current_folder()"]
    D -- "get_current_folder" --> GC["send_current_folder()"]
    RN --> R
    RG --> R
    SF --> R
    GC --> R
    D -. "handler raises" .-> E["send_message(status error, final true)"] --> R
    D -. "BrokenPipeError" .-> X1["sys.exit(1)"]
```

**GUI functions.**

| Function | Behaviour |
|----------|-----------|
| `run_native_gui(url, folder, filename)` | Pre-fills the StringVars. Invalid folder → error response, window not opened. Otherwise schedules the final `"GUI opened"` response (100 ms) and blocks in `root.mainloop()`. |
| `choose_folder()` | `filedialog.askdirectory`; sets `folder_var` on selection. |
| `send_current_folder()` | Responds with `folder_var`. Empty → creates and returns the default folder. Non-existent path → error. |
| `download_audio()` | Starts `_download_audio_thread()` on a daemon thread to keep the UI responsive. |
| `_download_audio_thread()` | GUI download path (§7.2). Updates widgets only via `root.after(0, …)`, since Tk is not thread-safe. Parses yt-dlp `NN.N%` output for the progress bar. |
| `cancel_download()` | Sets `root.download_cancelled`, kills yt-dlp if still running, resets the UI, starts `cleanup_partial_downloads()` on a thread. |
| `cleanup_partial_downloads(folder, filename)` | Waits 10 s for file locks to release, then deletes `.part`, `.ytdl`, `.jpg`, `.webp`, `_temp*.mp3`, `.mp3` for that name, retrying locked files. |
| `reset_fields()` | Clears the form and restores the "Download MP3" button. |

Main button states:

```mermaid
stateDiagram-v2
    [*] --> DownloadMP3
    DownloadMP3 --> Cancel: click, download_audio()
    Cancel --> Next: yt-dlp + embed_thumbnail succeed
    Cancel --> Next: click, cancel_download()
    Cancel --> Cancel: error (status label shows it)
    Next --> DownloadMP3: click, reset_fields()
```

### 6.4 `native_messaging.py`

`read_message()` / `send_message()`: see §3.2.

`run_native(url, folder, i_filename)` handles a headless `download`:

```mermaid
flowchart TB
    A{"URL contains youtube.com<br/>or youtu.be/?"} -- no --> E1["error: Invalid YouTube URL"]
    A -- yes --> B{"folder given?"}
    B -- no --> BD["ensure_default_folder()"]
    B -- yes --> BV{"is a directory?"}
    BV -- yes --> C
    BV -- no --> W1["warning, final false:<br/>Invalid folder path, using default"] --> BD
    BD --> C{"safe_filename(filename)<br/>meaningful?"}
    C -- yes --> F["use it"]
    C -- no --> T["yt-dlp --get-title (15 s)"]
    T -- ok --> FT["safe_filename(title)"]
    T -- fails --> W2["warning, final false:<br/>Could not fetch title"] --> FD["'Youtube MP3'"]
    F --> X{"folder/filename.mp3<br/>exists?"}
    FT --> X
    FD --> X
    X -- yes --> E2["error: File with this name already exists"]
    X -- no --> DL["core.download_audio_headless()"]
    DL -- raises --> E3["error: Download failed / timed out"]
    DL --> CHK{"mp3 exists now?"}
    CHK -- yes --> S["success: filename + path"]
    CHK -- no --> E4["error: file not created"]
```

### 6.5 `core.py`

| Function | Behaviour |
|----------|-----------|
| `safe_filename(name)` | Strips `\/*?:"<>|`; removes whole-word noise (`Official`, `Music`, `Video`, `HD`, `Lyrics`, `Lyric`, `Audio`, `Explicit`); removes resulting empty `()`/`[]`; collapses whitespace. Empty → `"Youtube MP3"`. |
| `ensure_default_folder()` | Creates `DEFAULT_FOLDER` if missing; returns it. |
| `download_audio_headless(url, folder, filename)` | Artist = text before the first `-` in the filename, else `<unknown>`. Runs yt-dlp (300 s timeout), then `embed_thumbnail`. If the target exists or ffmpeg is missing it only logs and returns; `run_native`'s post-download existence check reports the failure. |
| `embed_thumbnail(mp3_file, thumb_file, artist)` | Center-crops the `.jpg` to 1:1 (Pillow). ffmpeg stream-copies audio + image into `<name>_temp<pid>.mp3` as ID3v2.3 with cover art and `artist`. Replaces the original mp3 and deletes the jpg. On error removes the temp file and re-raises. Writing to a temp file avoids corrupting the mp3 if ffmpeg fails. |

### 6.6 yt-dlp invocation

Duplicated in `core.download_audio_headless` and `MP3._download_audio_thread`; keep both
in sync.

```
yt-dlp.exe --no-playlist --concurrent-fragments 3
           -x --audio-format mp3
           --write-thumbnail --convert-thumbnails jpg
           --ffmpeg-location <ffmpeg.exe>
           --newline
           -o "<folder>/<filename>.%(ext)s"
           <url>
```

```mermaid
flowchart LR
    U["YouTube URL"] --> Y["yt-dlp.exe"]
    Y -- "audio stream, converted by ffmpeg" --> M["filename.mp3"]
    Y -- "thumbnail, converted to jpg" --> J["filename.jpg"]
    J --> P["Pillow: center-crop 1:1"]
    M --> F["ffmpeg: copy audio + cover art<br/>ID3v2.3, artist metadata"]
    P --> F
    F --> T["filename_temp(pid).mp3"]
    T -- "replaces original, jpg deleted" --> OUT[("filename.mp3<br/>with cover art")]
```

`--newline` emits one progress line per update, which the GUI parses.

---

## 7. Request flows

### 7.1 Sidebar download (headless)

```mermaid
sequenceDiagram
    actor User
    participant SB as sidebar.js
    participant BG as background.js
    participant H as mp3.exe
    participant T as yt-dlp + ffmpeg

    User->>SB: click Download MP3
    SB->>SB: isYouTubeUrl(url)
    SB->>BG: native_action download, requestId
    BG->>BG: whitelist check, runNativeAction(origin sidebar)
    BG->>H: connectNative + postMessage, start 6 min timer
    H->>H: main() then run_native()
    opt bad folder or title lookup failed
        H-->>BG: warning, final false
        BG-->>SB: native_response (shown as warning)
    end
    H->>T: yt-dlp --get-title (if no filename)
    H->>T: core.download_audio_headless
    T-->>H: filename.mp3 + filename.jpg
    H->>H: embed_thumbnail
    H-->>BG: success, filename, path, final true
    BG-->>SB: native_response final, shows Downloaded
    BG->>BG: notify only if the sidebar is closed now
    BG->>H: port.disconnect()
    H->>H: read_message() EOF, sys.exit(0)
```

### 7.2 Open in app (GUI)

```mermaid
sequenceDiagram
    actor User
    participant SB as sidebar.js
    participant BG as background.js
    participant H as mp3.exe (Tk)
    participant T as yt-dlp + ffmpeg

    User->>SB: click Open in app
    SB->>BG: native_action download_gui (url, folder, filename optional)
    BG->>H: connectNative + postMessage, 30 s timer
    H->>H: run_native_gui fills StringVars
    H-->>BG: success, GUI opened, final true (after 100 ms)
    BG-->>SB: native_response, shows App launched
    Note over BG,H: port stays open (KEEP_ALIVE_ACTIONS)
    H->>H: root.mainloop() blocks
    User->>H: click Download MP3 in window
    H->>T: _download_audio_thread runs yt-dlp via Popen
    T-->>H: progress lines, progress bar updates
    H->>H: embed_thumbnail, status shows success, button becomes Next
    User->>H: closes window
    H->>H: mainloop returns, main() loops to read_message()
```

After `"GUI opened"`, the GUI reports nothing back to the extension.

### 7.3 Context-menu download

```mermaid
sequenceDiagram
    actor User
    participant BG as background.js
    participant ST as storage.local
    participant H as mp3.exe

    User->>BG: right-click YouTube link, Download as MP3
    alt not isYouTubeUrl(linkUrl)
        BG->>User: Invalid URL notification
    else valid
        BG->>ST: get lastFolder
        ST-->>BG: folder or nothing
        BG->>H: runNativeAction(download, origin menu)
        Note over BG,H: same host path as 7.1
        H-->>BG: final response
        BG->>BG: relay (nobody listening, ignored)
        BG->>User: desktop notification (always)
    end
```

### 7.4 Folder actions

```mermaid
sequenceDiagram
    actor User
    participant SB as sidebar.js
    participant ST as storage.local
    participant BG as background.js
    participant H as mp3.exe

    Note over SB: sidebar opens, loadFolder()
    SB->>ST: get lastFolder
    alt saved folder exists
        ST-->>SB: lastFolder, put in field
    else nothing saved
        SB->>BG: get_current_folder
        BG->>H: new host process
        H-->>BG: success, folder (send_current_folder)
        BG-->>SB: native_response
        SB->>ST: save lastFolder
    end

    User->>SB: click Choose
    SB->>BG: select_folder
    BG->>H: new host process
    H->>User: Tk folder dialog (choose_folder)
    User-->>H: picks folder or cancels
    H-->>BG: success, folder (send_current_folder)
    BG-->>SB: native_response
    SB->>ST: save lastFolder
```

Each process starts with `folder_var = DEFAULT_FOLDER`, so cancelling the dialog returns
the default folder, not the previously selected one.

---

## 8. Build and release

```mermaid
flowchart LR
    SRC["host/src/ytmp3_host/*.py"] -- "uv run pyinstaller --onefile<br/>--name mp3 --icon mp3.ico MP3.py" --> EXE["dist/mp3.exe"]
    EXE --> ISS["packaging/installer/<br/>mp3_installer.iss<br/>(Inno Setup 6)"]
    FFB["host/bin/ffmpeg.exe"] --> ISS
    ICO["packaging/assets/mp3.ico"] --> ISS
    ISS --> SETUP["output/YouTube MP3<br/>Downloader-Setup.exe"]

    EXT["extension/"] -- "zip" --> AMO["AMO upload"]
```

Installer steps (per-user, no elevation):

```mermaid
flowchart TB
    P["PrepareToInstall<br/>download latest yt-dlp.exe from GitHub to tmp"] -- "fails" --> STOP["install stops with error"]
    P -- ok --> F["[Files]<br/>mp3.exe, mp3.ico, ffmpeg.exe to<br/>%LOCALAPPDATA%\YouTube MP3 Downloader\"]
    F --> C["CurStepChanged(ssPostInstall)<br/>copy yt-dlp.exe next to mp3.exe"]
    C --> M["CreateNativeMessagingManifest()<br/>%APPDATA%\Mozilla\NativeMessagingHosts\<br/>app_manifest.json, path = mp3.exe"]
    M --> R["[Registry]<br/>HKCU\Software\Mozilla\NativeMessagingHosts\<br/>app_manifest = manifest path"]
```

- yt-dlp is downloaded at install time, not bundled: YouTube changes regularly break older
  yt-dlp versions. A failed download aborts the install.
- All binaries and the icon land in one folder, matching `config.py`'s frozen-mode paths.
- Uninstall removes the registry key, the host manifest, `yt-dlp.exe` and `mp3.log`
  (listed under `[UninstallDelete]` because they are created at runtime, not via `[Files]`).
- The extension is versioned and released separately (`manifest.json` `version` vs.
  `MyAppVersion` in the `.iss`).

---

## 9. Development workflow

| Task | How |
|------|-----|
| Python environment | `uv sync`; place `ffmpeg.exe` and `yt-dlp.exe` in `host/bin/`. |
| Register a dev host | Copy `packaging/app_manifest.dev.json` to `%APPDATA%\Mozilla\NativeMessagingHosts\app_manifest.json`, set `path` to your `dist\mp3.exe`, remove the `"\\"` comment key, and create the `HKCU` key from §3.1 (or run the installer once). |
| Test host changes through Firefox | Rebuild `mp3.exe` with PyInstaller; the manifest points at an executable. |
| Test the host without Firefox | `uv run python host/tests/smoke_native.py`. Sends one hard-coded message to `dist/mp3.exe` (fallback: `python -m ytmp3_host.MP3`) and prints the reply. Edit `message` to change the action. Not pytest-safe: calls yt-dlp at import. |
| Load the extension | `about:debugging` → Load Temporary Add-on → `extension/manifest.json`. |
| Extension tests | `node --test tests/extension/` |

Extension test harness:

```mermaid
flowchart LR
    T["background.test.js<br/>youtube.test.js"] --> H["harness.js<br/>loadBackground()"]
    H -- "node:vm sandbox" --> E["extension/youtube.js<br/>extension/background.js<br/>(real files)"]
    H -- "fake browser global" --> FB["runtime, contextMenus,<br/>notifications, sidebarAction,<br/>storage.local"]
    FB --> FP["fake ports<br/>reply() = host message<br/>exit() = host disconnect"]
```

`background.test.js` covers the request lifecycle: single final response, keep-alive,
timeouts, disconnect errors, notifications, context menu. `youtube.test.js` covers
`isYouTubeUrl`. `sidebar.js` and the Python host have no automated tests.

---

## 10. Invariants and change checklist

**Invariants**

- No stdout output in host code other than `send_message`.
- Exactly one `final: true` response per request. Intermediate responses must set
  `final: false` explicitly.
- `ACTION_TIMEOUTS_MS.download` (6 min) must exceed host title lookup (15 s) + yt-dlp
  timeout (300 s); otherwise the extension times out while the host is still working.
- `app_manifest`, `yt.downloader@converter.org` and `mp3.exe` are fixed (§3.1).
- Reusable host logic belongs in `core.py` / `native_messaging.py`, not `MP3.py`
  (importing `MP3.py` creates a window).

**Adding a host action**

1. `ACTION_TIMEOUTS_MS` in `background.js` (whitelist + timeout).
2. `actions` dict in `MP3.main()`.
3. Caller in `sidebar.js`.
4. `KEEP_ALIVE_ACTIONS` if the host must outlive its final response.
5. A test in `tests/extension/background.test.js` if lifecycle behaviour changes.

**Changing yt-dlp arguments:** update both `core.download_audio_headless` and
`MP3._download_audio_thread`.

**Logging:** `mp3.log` in the process working directory (the installer assumes the
install folder), ERROR level.

---

## 11. Known issues

| Issue | Location | Effect |
|-------|----------|--------|
| `get_current_folder` does not create the default folder | `send_current_folder()` | `folder_var` is pre-set to `DEFAULT_FOLDER`, so the "empty → create" branch never runs. If the folder does not exist, the response is `"Path doesn't exist"`. |
| Host lingers after a rejected `download_gui` | `run_native_gui()` + `KEEP_ALIVE_ACTIONS` | Invalid folder → error, no window, but the port stays open. The process waits on stdin until the extension reloads or Firefox exits. |
| Dead auto-close code | `_download_audio_thread()` | Checks `root.close_var`, which is never set. |
| Redundant checks in headless download | `download_audio_headless()` | "File exists" and "ffmpeg missing" are only logged; the user sees the generic "Download failed, file not created" from `run_native`. |
