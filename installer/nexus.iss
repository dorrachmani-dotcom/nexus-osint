; Inno Setup script — wraps the PyInstaller one-folder build into a single,
; offline Nexus-Setup.exe installer.
;
; Prerequisite: build the app folder first so installer\dist\Nexus\ exists:
;     pyinstaller installer/nexus.spec --noconfirm \
;         --distpath installer/dist --workpath installer/build
; (Set NEXUS_BUNDLE_CHROMIUM=1 first for offline evidence screenshots.)
;
; Then compile this script with Inno Setup's ISCC:
;     ISCC installer\nexus.iss
; The result is installer\Output\Nexus-Setup.exe — one file to hand someone on a
; USB stick. They double-click it; it installs to Program Files and adds Start
; Menu / Desktop shortcuts. All user data (database, evidence, API keys in .env)
; is created later under %LOCALAPPDATA%\Nexus by the app itself, never here.

#define AppName "Nexus"
#define AppVersion "1.0.0"
#define AppPublisher "Nexus OSINT"
#define AppExeName "Nexus.exe"

[Setup]
AppId={{B7E9B2A4-3C2E-4E7A-9C1D-6F2A1E8C4D90}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
; Per-machine install under Program Files. Requires admin once, at install time;
; the app itself never needs admin (it binds 127.0.0.1 and writes to LOCALAPPDATA).
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Single self-contained installer, no internet needed.
OutputDir=Output
OutputBaseFilename=Nexus-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\launcher\nexus.ico
UninstallDisplayIcon={app}\{#AppExeName}
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; The entire PyInstaller one-folder output (Nexus.exe + _internal\, including the
; bundled Chromium when built with NEXUS_BUNDLE_CHROMIUM=1).
Source: "dist\Nexus\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; Offer to launch right after install.
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove the app folder cleanly. NOTE: user data under %LOCALAPPDATA%\Nexus
; (database, evidence, .env keys) is intentionally LEFT IN PLACE so an
; uninstall/reinstall never destroys an analyst's collected intelligence.
Type: filesandordirs; Name: "{app}"
