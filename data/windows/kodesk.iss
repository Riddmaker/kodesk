; Inno Setup 6 script for the Windows installer (B-01 spike: unsigned, see docs/research/windows-signing).
;   iscc /DAppVersion=0.1.0 data\windows\kodesk.iss
; Expects the PyInstaller one-folder bundle in dist\kodesk (data/packaging/kodesk.spec).
; Installs per user without admin rights (least privilege); the user may choose all-users instead.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{15269ECB-C4C4-4D4D-940E-2336F43E3DF5}
AppName=kodesk
AppVersion={#AppVersion}
AppPublisher=Riddmaker
AppPublisherURL=https://github.com/Riddmaker/kodesk
DefaultDirName={autopf}\kodesk
DefaultGroupName=kodesk
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename=kodesk-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\kodesk.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Files]
Source: "..\..\dist\kodesk\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\kodesk"; Filename: "{app}\kodesk.exe"

[Run]
Filename: "{app}\kodesk.exe"; Description: "{cm:LaunchProgram,kodesk}"; Flags: nowait postinstall skipifsilent
