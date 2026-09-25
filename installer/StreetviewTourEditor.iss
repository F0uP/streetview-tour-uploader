; Inno Setup script for the Streetview Tour Editor.
; Built by build_exe.py, which passes AppVersion, SourceDir, OutputDir and IconFile
; (plus Sign and a "signcmd" sign tool when SIGN_CMD is set):
;   iscc /DAppVersion=1.2.0 /DSourceDir=..\dist\StreetviewTourEditor ... StreetviewTourEditor.iss
;
; Installs per user into %LOCALAPPDATA%\Programs by default (no admin prompt),
; with an option to install for all users instead. Google credentials live in
; %APPDATA%\StreetviewTourEditor (see run_editor.py), so updates and
; uninstalls never touch them.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\StreetviewTourEditor"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef IconFile
  #define IconFile "app.ico"
#endif

#define AppName "Streetview Tour Editor"
#define AppExe "StreetviewTourEditor.exe"
#define AppUrl "https://github.com/F0uP/streetview-tour-uploader"

[Setup]
; Keep AppId fixed forever - it is how Windows recognises updates of this app.
AppId={{D4036997-C740-4EE5-B264-10D89ED4697B}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Streetview Tour Editor contributors
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppUpdatesURL={#AppUrl}/releases
DefaultDirName={autopf}\StreetviewTourEditor
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=StreetviewTourEditor-{#AppVersion}-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
LicenseFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The running editor is stopped in PrepareToInstall below; let Restart Manager
; force-close anything else still holding the app's files.
CloseApplications=force
RestartApplications=no
; Register the .vrtour file type (see [Registry]) and refresh Explorer icons.
ChangesAssociations=yes
#ifdef Sign
SignTool=signcmd
#endif

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
german.AssocVrtour=.vrtour-Projekte mit dem Streetview Tour Editor öffnen
english.AssocVrtour=Open .vrtour projects with the Streetview Tour Editor
german.VrtourType=Streetview-Tour-Projekt
english.VrtourType=Streetview tour project

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "assoc"; Description: "{cm:AssocVrtour}"; GroupDescription: "{cm:AdditionalIcons}"

[Registry]
; HKA = HKCU for a per-user install, HKLM for an all-users install.
Root: HKA; Subkey: "Software\Classes\.vrtour"; ValueType: string; ValueName: ""; ValueData: "StreetviewTourEditor.vrtour"; Flags: uninsdeletevalue; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\.vrtour\OpenWithProgids"; ValueType: string; ValueName: "StreetviewTourEditor.vrtour"; ValueData: ""; Flags: uninsdeletevalue; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\StreetviewTourEditor.vrtour"; ValueType: string; ValueName: ""; ValueData: "{cm:VrtourType}"; Flags: uninsdeletekey; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\StreetviewTourEditor.vrtour\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExe},0"; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\StreetviewTourEditor.vrtour\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: assoc

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; An update replaces the whole bundle, so clear out files an older version left behind.
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; In-app updates run this installer with /SILENT: start the new version again
; right away, without a new browser tab (the open tab reconnects by itself).
Filename: "{app}\{#AppExe}"; Parameters: "--no-browser"; Flags: nowait skipifnotsilent

[UninstallRun]
; Stop a running editor so its files can be removed.
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopEditor"

[Code]
// The editor lives in the notification area, where Windows' Restart Manager
// can't ask it to close, so stop it before files are replaced. It only serves
// the editor page; the page itself autosaves the tour in the browser.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;
