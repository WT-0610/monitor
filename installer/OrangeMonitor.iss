#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId=OrangeMonitor.Windows
AppName=OrangeMonitor
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\OrangeMonitor
DisableDirPage=no
DefaultGroupName=OrangeMonitor
UsePreviousGroup=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\OrangeMonitor.exe
OutputDir=..
OutputBaseFilename=OrangeMonitor-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Files]
Source: "..\dist\OrangeMonitor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\OrangeMonitor"; Filename: "{app}\OrangeMonitor.exe"; WorkingDir: "{app}"

[Run]
Filename: "{app}\OrangeMonitor.exe"; Description: "{cm:LaunchProgram,OrangeMonitor}"; Flags: nowait postinstall skipifsilent
