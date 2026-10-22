; ============================================================
; Compile-time constants (Inno Setup Preprocessor)
; ============================================================
; Shared between [Registry] (INI-style, no Pascal access) and
; [Code] (Pascal, no access to Registry section values). Using
; #define instead of duplicating literals in both places means
; there is exactly one source of truth for each identifier.

#define MyAppName         "YouTube MP3 Downloader"
; Must match __version__ in host\src\ytmp3_host\__init__.py
; (tests\host\test_versions.py checks it).
#define MyAppVersion      "2.0.0"
#define MyAppPublisher    "Martin"

; Must match browser_specific_settings.gecko.id in the
; Firefox extension's manifest.json.
#define ExtensionID       "yt.downloader@converter.org"

; Must match the name the extension uses when calling
; browser.runtime.connectNative(...). This is also the
; registry subkey name under NativeMessagingHosts below,
; so the two can never fall out of sync.
#define NativeHostName    "app_manifest"

#define NativeExeName     "mp3.exe"
#define NativeManifestName "app_manifest.json"

#define YtDlpURL          "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
#define YtDlpTempName     "yt-dlp.exe"

; Every version installs into its own folder; the Firefox manifest
; is switched to it only after all its files are in place, so an
; install that is killed or loses power leaves the old version
; working. Must match config.VERSIONS_DIR_NAME in the host.
; yt-dlp.exe stays in {app}: the host keeps it current (updater.py).
#define VersionDir        "versions\" + MyAppVersion

; Repo root, relative to this script (packaging\installer\).
; mp3.exe comes from PyInstaller's dist\mp3\, ffmpeg.exe from host\bin\.
#define RepoRoot          AddBackslash(SourcePath) + "..\.."


[Setup]
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
WizardStyle=modern dynamic

; The folder shown by default in the installer.
DefaultDirName={localappdata}\{#MyAppName}

; Name shown in the Start Menu. No [Icons] section below
; creates an actual shortcut yet; this is metadata only.
DefaultGroupName={#MyAppName}

; Name of the generated installer. No spaces: GitHub renames
; release assets with spaces, and the host's updater downloads
; it by this exact name (updater.INSTALLER_ASSET).
OutputBaseFilename=YT-MP3-Downloader-Setup

; The host starts this installer silently to update itself
; (updater.py). A new version never replaces files that a
; running host uses, so there is nothing to close.
CloseApplications=no

; Where Inno Setup puts the generated installer.
OutputDir=output

; Compression.
Compression=lzma
SolidCompression=yes

; Modern Windows permissions.
PrivilegesRequired=lowest

; Installer icon.
SetupIconFile={#RepoRoot}\packaging\assets\mp3.ico


[Files]

; Your native application (PyInstaller --onedir: mp3.exe + _internal\)
Source: "{#RepoRoot}\dist\mp3\*"; DestDir: "{app}\{#VersionDir}"; Flags: recursesubdirs createallsubdirs ignoreversion

; Your application icon
Source: "{#RepoRoot}\packaging\assets\mp3.ico"; DestDir: "{app}\{#VersionDir}"

; FFmpeg
Source: "{#RepoRoot}\host\bin\ffmpeg.exe"; DestDir: "{app}\{#VersionDir}"


[Registry]

; Subkey name must equal NativeHostName exactly, or Firefox
; will not find the native messaging host.
Root: HKCU; \
    Subkey: "Software\Mozilla\NativeMessagingHosts\{#NativeHostName}"; \
    ValueType: string; \
    ValueName: ""; \
    ValueData: "{userappdata}\Mozilla\NativeMessagingHosts\{#NativeManifestName}"; \
    Flags: uninsdeletekey

[UninstallDelete]
; yt-dlp.exe and the manifest are written at runtime via
; FileCopy / SaveStringToFile, not listed in [Files], so the
; uninstaller has no record of them unless declared here. The
; host's updater adds yt-dlp.download.exe, yt-dlp.old*.exe and
; update.lock; versions\ may hold folders an update kept because
; they were still in use.
Type: files; Name: "{app}\yt-dlp.exe"
Type: files; Name: "{app}\yt-dlp.download.exe"
Type: files; Name: "{app}\yt-dlp.old*.exe"
Type: files; Name: "{app}\update.lock"
Type: files; Name: "{app}\mp3.log"
Type: files; Name: "{app}\mp3.log.1"
Type: filesandordirs; Name: "{app}\versions"
Type: files; Name: "{userappdata}\Mozilla\NativeMessagingHosts\{#NativeManifestName}"

[Code]

const
  MOVEFILE_REPLACE_EXISTING = $1;
  MOVEFILE_WRITE_THROUGH = $8;

var
  // False in a silent update that keeps the installed yt-dlp.exe
  // (see PrepareToInstall). Not a { } comment: those end at the
  // first closing brace, so they can't mention Inno constants.
  YtDlpDownloaded: Boolean;

// Replaces a file in one step: the target is either the old or
// the new file, never missing or half written.
function MoveFileEx(lpExistingFileName, lpNewFileName: String; dwFlags: Cardinal): Boolean;
  external 'MoveFileExW@kernel32.dll stdcall';

function ReplaceFile(const Source, Target: String): Boolean;
begin
  Result := MoveFileEx(Source, Target, MOVEFILE_REPLACE_EXISTING or MOVEFILE_WRITE_THROUGH);
  if not Result then
    Log('Could not move ' + Source + ' to ' + Target + ': ' + SysErrorMessage(DLLGetLastError));
end;

; ============================================================
; JsonEscape
; ============================================================
; Escapes backslashes and double quotes so a Windows path can
; be embedded safely inside a JSON string.

function JsonEscape(const S: String): String;
begin
  Result := S;
  StringChangeEx(Result, '\', '\\', True);
  StringChangeEx(Result, '"', '\"', True);
end;

; ============================================================
; DownloadYtDlp
; ============================================================
; Downloads yt-dlp.exe into {tmp}. Returns True on success.
; DownloadTemporaryFile saves the file as {tmp}\BaseName and
; returns the file size (Int64), not a path, so there is
; nothing to capture from its return value here.

function DownloadYtDlp(): Boolean;
begin
  Result := False;

  try
    Log('Downloading latest yt-dlp from GitHub...');
    DownloadTemporaryFile('{#YtDlpURL}', '{#YtDlpTempName}', '', nil);
    Log('yt-dlp downloaded successfully.');
    Result := True;
  except
    Log('Failed to download yt-dlp: ' + GetExceptionMessage);
  end;
end;

; ============================================================
; PrepareToInstall
; ============================================================
; Runs before the actual installation. Downloads yt-dlp here,
; except in a silent install that finds one in {app}: that is
; the host updating itself, and the host keeps yt-dlp current
; and repairs it. A manual (re)install always downloads it, so
; reinstalling also fixes a broken yt-dlp.exe. If the download
; fails, installation is stopped and the user gets an error
; message instead of a broken installation.

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  YtDlpDownloaded := False;

  if WizardSilent and FileExists(ExpandConstant('{app}\yt-dlp.exe')) then
  begin
    Log('Silent update: keeping the installed yt-dlp.exe.');
    exit;
  end;

  YtDlpDownloaded := DownloadYtDlp();
  if not YtDlpDownloaded then
  begin
    Result :=
      'The latest version of yt-dlp could not be downloaded. ' +
      'Please check your internet connection and try again.';
  end;
end;

; ============================================================
; CreateNativeMessagingManifest
; ============================================================
; Writes the native messaging manifest JSON into the folder
; Firefox expects for per-user native messaging on Windows.
; This switches Firefox to the new version, so it runs last,
; and atomically: written to a temp file, then moved over the
; old manifest in one step.

procedure CreateNativeMessagingManifest();
var
  ManifestPath: String;
  ExePath: String;
  JSON: String;
begin
  ManifestPath := ExpandConstant('{userappdata}\Mozilla\NativeMessagingHosts\{#NativeManifestName}');
  ExePath := ExpandConstant('{app}\{#VersionDir}\{#NativeExeName}');

  if not ForceDirectories(ExtractFilePath(ManifestPath)) then
    RaiseException('Could not create the Firefox native messaging host directory.');

  JSON :=
    '{' + #13#10 +
    '  "name": "' + JsonEscape('{#NativeHostName}') + '",' + #13#10 +
    '  "description": "YouTube MP3 Converter and Downloader",' + #13#10 +
    '  "path": "' + JsonEscape(ExePath) + '",' + #13#10 +
    '  "type": "stdio",' + #13#10 +
    '  "allowed_extensions": [' + #13#10 +
    '    "' + JsonEscape('{#ExtensionID}') + '"' + #13#10 +
    '  ]' + #13#10 +
    '}' + #13#10;

  if not SaveStringToFile(ManifestPath + '.tmp', JSON, False) or
     not ReplaceFile(ManifestPath + '.tmp', ManifestPath) then
    RaiseException('Could not create the Firefox native messaging manifest.');
end;

; ============================================================
; RemoveVersionFolder
; ============================================================
; Removes an old version's folder, unless its mp3.exe still
; runs (a download that started before the update): Windows
; can't delete a running exe, and deleting the rest would
; break that download. Such a folder is removed by the next
; update or the uninstaller.

procedure RemoveVersionFolder(const Dir: String);
begin
  if FileExists(Dir + '\{#NativeExeName}') and not DeleteFile(Dir + '\{#NativeExeName}') then
    Log('Keeping ' + Dir + ': its mp3.exe is still running.')
  else if not DelTree(Dir, True, True, True) then
    Log('Could not remove all of ' + Dir + '.');
end;

; ============================================================
; RemoveOldVersions
; ============================================================
; After the manifest points at the new version: remove the
; other version folders and the files of a 1.x install, which
; sat directly in {app}.

procedure RemoveOldVersions();
var
  FindRec: TFindRec;
  VersionsDir: String;
begin
  VersionsDir := ExpandConstant('{app}\versions');
  if FindFirst(VersionsDir + '\*', FindRec) then
  try
    repeat
      if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0) and
         (FindRec.Name <> '.') and (FindRec.Name <> '..') and
         (CompareText(FindRec.Name, '{#MyAppVersion}') <> 0) then
        RemoveVersionFolder(VersionsDir + '\' + FindRec.Name);
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;

  // 1.x layout
  if FileExists(ExpandConstant('{app}\{#NativeExeName}')) and
     DeleteFile(ExpandConstant('{app}\{#NativeExeName}')) then
  begin
    DelTree(ExpandConstant('{app}\_internal'), True, True, True);
    DeleteFile(ExpandConstant('{app}\ffmpeg.exe'));
    DeleteFile(ExpandConstant('{app}\mp3.ico'));
  end;
end;

; ============================================================
; CurStepChanged
; ============================================================
; After the version's files are installed, in this order:
; 1. a downloaded yt-dlp.exe goes into {app} (copied next to
;    it first, then moved in one step). If a running download
;    keeps the installed one locked, that one stays: the host
;    updates or repairs it later;
; 2. the manifest is switched to the new version;
; 3. old versions are removed.
; A kill before step 2 leaves Firefox on the old version.

procedure CurStepChanged(CurStep: TSetupStep);
var
  YtDlpSource: String;
  YtDlpDestination: String;
begin
  if CurStep = ssPostInstall then
  begin
    if YtDlpDownloaded then
    begin
      YtDlpSource := ExpandConstant('{tmp}\{#YtDlpTempName}');
      YtDlpDestination := ExpandConstant('{app}\yt-dlp.exe');

      if not CopyFile(YtDlpSource, YtDlpDestination + '.tmp', False) or
         not ReplaceFile(YtDlpDestination + '.tmp', YtDlpDestination) then
      begin
        DeleteFile(YtDlpDestination + '.tmp');
        if FileExists(YtDlpDestination) then
          Log('Keeping the installed yt-dlp.exe, it is in use.')
        else
          RaiseException('Could not install yt-dlp.');
      end;
    end;

    CreateNativeMessagingManifest();
    RemoveOldVersions();
  end;
end;
