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

; yt-dlp.exe is checked against the SHA-256 list the same release
; publishes, like the host's updater does (updater.YT_DLP_ASSET,
; updater.YT_DLP_SUMS; tests\host\test_versions.py checks the names).
#define YtDlpReleaseURL   "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
#define YtDlpTempName     "yt-dlp.exe"
#define YtDlpSumsName     "SHA2-256SUMS"

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

; mp3.exe and yt-dlp.exe are 64-bit builds. x64compatible also
; allows Windows on ARM, which runs them emulated.
ArchitecturesAllowed=x64compatible

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
; host's updater adds yt-dlp.download.exe, yt-dlp.old*.exe,
; deno.exe (with deno.download.*) and update.lock; versions\ may
; hold folders an update kept because they were still in use.
Type: files; Name: "{app}\yt-dlp.exe"
Type: files; Name: "{app}\yt-dlp.exe.tmp"
Type: files; Name: "{app}\yt-dlp.download.exe"
Type: files; Name: "{app}\yt-dlp.old*.exe"
Type: files; Name: "{app}\deno.exe"
Type: files; Name: "{app}\deno.download.*"
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
  // A reinstall of the installed version: its folder must stay
  // even if the self-check fails, Firefox already runs it.
  VersionDirExisted: Boolean;
  SelfCheckFailed: Boolean;

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
; YtDlpSha256
; ============================================================
; Downloads the release's checksum list into {tmp} and returns
; the SHA-256 of yt-dlp.exe from it ("<hex>  yt-dlp.exe"), or ''
; if it lists none. Raises if the download fails.

function YtDlpSha256(): String;
var
  Lines: TArrayOfString;
  Line: String;
  I: Integer;
begin
  Result := '';
  DownloadTemporaryFile('{#YtDlpReleaseURL}{#YtDlpSumsName}', '{#YtDlpSumsName}', '', nil);
  if not LoadStringsFromFile(ExpandConstant('{tmp}\{#YtDlpSumsName}'), Lines) then
    exit;
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Trim(Lines[I]);
    if (Length(Line) > 64) and (Trim(Copy(Line, 65, Length(Line))) = '{#YtDlpTempName}') then
    begin
      Result := Copy(Line, 1, 64);
      exit;
    end;
  end;
end;

; ============================================================
; DownloadYtDlp
; ============================================================
; Downloads yt-dlp.exe into {tmp}. Returns True on success.
; DownloadTemporaryFile checks the SHA-256 it is given and
; raises (and deletes the file) on a mismatch, so a damaged or
; truncated download is never installed. Both files come from
; "latest", so a yt-dlp release in between the two downloads
; fails the check too; the host downloads yt-dlp itself then.

function DownloadYtDlp(): Boolean;
var
  Sha256: String;
begin
  Result := False;

  try
    Log('Downloading latest yt-dlp from GitHub...');
    Sha256 := YtDlpSha256();
    if Sha256 = '' then
    begin
      Log('The yt-dlp release lists no checksum for {#YtDlpTempName}.');
      exit;
    end;
    DownloadTemporaryFile('{#YtDlpReleaseURL}{#YtDlpTempName}', '{#YtDlpTempName}', Sha256, nil);
    Log('yt-dlp downloaded and checked successfully.');
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
; reinstalling also fixes a broken yt-dlp.exe. A failed download
; does not stop the installation: the host downloads a missing
; yt-dlp.exe itself the next time the sidebar opens.

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  YtDlpDownloaded := False;
  VersionDirExisted := DirExists(ExpandConstant('{app}\{#VersionDir}'));

  if WizardSilent and FileExists(ExpandConstant('{app}\yt-dlp.exe')) then
  begin
    Log('Silent update: keeping the installed yt-dlp.exe.');
    exit;
  end;

  YtDlpDownloaded := DownloadYtDlp();
  if not YtDlpDownloaded and not FileExists(ExpandConstant('{app}\yt-dlp.exe')) then
    SuppressibleMsgBox(
      'yt-dlp could not be downloaded right now. The app will download it ' +
      'by itself the next time you open its sidebar in Firefox.',
      mbInformation, MB_OK, IDOK);
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
; NewVersionWorks
; ============================================================
; Runs the new version's mp3.exe --self-check (selfcheck.py):
; it imports every host module and runs ffmpeg.exe. Exit code
; 0 means it works; the problems are in {app}\mp3.log.

function NewVersionWorks(): Boolean;
var
  ResultCode: Integer;
begin
  Result :=
    Exec(ExpandConstant('{app}\{#VersionDir}\{#NativeExeName}'), '--self-check',
         ExpandConstant('{app}\{#VersionDir}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) and
    (ResultCode = 0);
  if Result then
    Log('Self-check passed.')
  else
    Log('Self-check failed (exit code ' + IntToStr(ResultCode) + '), see mp3.log.');
end;

; ============================================================
; CurStepChanged
; ============================================================
; After the version's files are installed, in this order:
; 0. the new version checks itself. If it does not work,
;    nothing is switched: its folder is removed (unless it is
;    a reinstall of the version Firefox already runs) and the
;    installed version stays, so a broken release never
;    replaces a working host;
; 1. a downloaded yt-dlp.exe goes into {app} (copied next to
;    it first, then moved in one step). If that fails (e.g. a
;    running download keeps the installed one locked), the
;    installation goes on: the host updates or repairs yt-dlp
;    later;
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
    if not NewVersionWorks() then
    begin
      SelfCheckFailed := True;
      if not VersionDirExisted then
        DelTree(ExpandConstant('{app}\{#VersionDir}'), True, True, True);
      SuppressibleMsgBox(
        'This version of the app did not start correctly, so it was not installed ' +
        'and your current version was kept. Please try again later or report it on GitHub.',
        mbCriticalError, MB_OK, IDOK);
      exit;
    end;

    if YtDlpDownloaded then
    begin
      YtDlpSource := ExpandConstant('{tmp}\{#YtDlpTempName}');
      YtDlpDestination := ExpandConstant('{app}\yt-dlp.exe');

      if not CopyFile(YtDlpSource, YtDlpDestination + '.tmp', False) or
         not ReplaceFile(YtDlpDestination + '.tmp', YtDlpDestination) then
      begin
        DeleteFile(YtDlpDestination + '.tmp');
        Log('Could not install the downloaded yt-dlp.exe, the host will repair it.');
      end;
    end;

    CreateNativeMessagingManifest();
    RemoveOldVersions();
  end;
end;

// Exit code 1 after a failed self-check, so whoever started the
// installer can tell it apart from a finished install.
function GetCustomSetupExitCode(): Integer;
begin
  if SelfCheckFailed then
    Result := 1
  else
    Result := 0;
end;
