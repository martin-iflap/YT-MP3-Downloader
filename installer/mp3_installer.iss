; ============================================================
; Compile-time constants (Inno Setup Preprocessor)
; ============================================================
; Shared between [Registry] (INI-style, no Pascal access) and
; [Code] (Pascal, no access to Registry section values). Using
; #define instead of duplicating literals in both places means
; there is exactly one source of truth for each identifier.

#define MyAppName         "YouTube MP3 Downloader"
#define MyAppVersion      "1.0.0"
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

; Name of the generated installer.
OutputBaseFilename={#MyAppName}-Setup

; Where Inno Setup puts the generated installer.
OutputDir=output

; Compression.
Compression=lzma
SolidCompression=yes

; Modern Windows permissions.
PrivilegesRequired=lowest

; Installer icon.
SetupIconFile=C:\Users\User\PycharmProjects\MP3\ico\mp3.ico


[Files]

; Your native application
Source: "C:\Users\User\PycharmProjects\MP3\dist\mp3.exe"; DestDir: "{app}"

; Your application icon
Source: "C:\Users\User\PycharmProjects\MP3\ico\mp3.ico"; DestDir: "{app}"

; FFmpeg
Source: "C:\Users\User\PycharmProjects\MP3\ffmpeg-7.1.1-essentials_build\bin\ffmpeg.exe"; DestDir: "{app}"


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
; uninstaller has no record of them unless declared here.
Type: files; Name: "{app}\yt-dlp.exe"
Type: files; Name: "{app}\mp3.log"
Type: files; Name: "{userappdata}\Mozilla\NativeMessagingHosts\{#NativeManifestName}"

[Code]

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
; Runs before the actual installation. Downloads yt-dlp here;
; if the download fails, installation is stopped and the user
; gets an error message instead of a broken installation.

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';

  if not DownloadYtDlp() then
  begin
    Result :=
      'The latest version of yt-dlp could not be downloaded.' +
      'Please check your internet connection and try again.';
  end;
end;

; ============================================================
; CreateNativeMessagingManifest
; ============================================================
; Writes the native messaging manifest JSON into the folder
; Firefox expects for per-user native messaging on Windows.

procedure CreateNativeMessagingManifest();
var
  ManifestPath: String;
  ExePath: String;
  JSON: String;
begin
  ManifestPath := ExpandConstant('{userappdata}\Mozilla\NativeMessagingHosts\{#NativeManifestName}');
  ExePath := ExpandConstant('{app}\{#NativeExeName}');

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

  if not SaveStringToFile(ManifestPath, JSON, False) then
    RaiseException('Could not create the Firefox native messaging manifest.');
end;

; ============================================================
; CurStepChanged
; ============================================================
; After files are installed: move the downloaded yt-dlp.exe
; into {app}, then generate the native messaging manifest.

procedure CurStepChanged(CurStep: TSetupStep);
var
  YtDlpSource: String;
  YtDlpDestination: String;
begin
  if CurStep = ssPostInstall then
  begin
    YtDlpSource := ExpandConstant('{tmp}\{#YtDlpTempName}');
    YtDlpDestination := ExpandConstant('{app}\yt-dlp.exe');

    if not CopyFile(YtDlpSource, YtDlpDestination, False) then
      RaiseException('Could not install yt-dlp.');

    CreateNativeMessagingManifest();
  end;
end;
