; Coding Brain Desktop per-user installer. Changes no Coding Brain engine or models.
[Setup]
AppId={{70E4E90D-3BDA-4A4F-AB9C-FAD28EAD6F11}
AppName=Coding Brain Desktop
AppVersion=0.1.0-preview
AppVerName=Coding Brain Desktop 0.1.0 Preview
AppPublisher=Coding Brain
DefaultDirName={localappdata}\Programs\CodingBrainDesktop
DefaultGroupName=Coding Brain Desktop
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=CodingBrain-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\CodingBrainDesktop.exe
CloseApplications=yes
MinVersion=10.0

[Files]
Source: "..\..\dist\CodingBrainDesktop\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Icons]
Name: "{autoprograms}\Coding Brain Desktop"; Filename: "{app}\CodingBrainDesktop.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Coding Brain Desktop"; Filename: "{app}\CodingBrainDesktop.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\CodingBrainDesktop.exe"; Description: "Launch Coding Brain Desktop"; Flags: nowait postinstall skipifsilent

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
  if (not WizardSilent()) and (not FileExists(ExpandConstant('{localappdata}\CodingBrain\app\current.json'))) then
    MsgBox('Coding Brain Desktop requires the separate Coding Brain engine. ' +
      'Please install Coding Brain before launching the desktop application. ' +
      'This installer will not install or change the engine.', mbInformation, MB_OK);
end;
