; Coding Brain per-user installer: the desktop, and (when the build bundles them) the matching
; engine release files plus a one-step setup that installs or updates the engine and prepares the
; environment through Coding Brain's own installer.
; Official release runner must pass /DCodingBrainVersion=<matching engine version>.
#ifndef CodingBrainVersion
#define CodingBrainVersion "0.1.0-preview"
#endif
; dist\engine-bundle is produced by scripts/build_release.py from the same commit.
#define EngineBundle "..\..\dist\engine-bundle"
#if FileExists(EngineBundle + "\release.json")
#define WithEngine
#endif
[Setup]
AppId={{70E4E90D-3BDA-4A4F-AB9C-FAD28EAD6F11}
AppName=Coding Brain Desktop
AppVersion={#CodingBrainVersion}
AppVerName=Coding Brain Desktop {#CodingBrainVersion}
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
; PyInstaller/WebView processes do not reliably support Restart Manager shutdown.
; PrepareToInstall checks only this installation and reports blockers before copying files.
CloseApplications=no
RestartApplications=no
SetupLogging=yes
MinVersion=10.0

[Files]
Source: "Close-CodingBrainDesktop.ps1"; Flags: dontcopy
Source: "..\..\dist\CodingBrainDesktop\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#ifdef WithEngine
Source: "{#EngineBundle}\*"; DestDir: "{app}\engine"; Flags: ignoreversion
Source: "Setup-CodingBrain.ps1"; DestDir: "{app}\engine"; Flags: ignoreversion
Source: "Verify-CodingBrainEngine.py"; DestDir: "{app}\engine"; Flags: ignoreversion
Source: "..\..\desktop_ui\Recover-CodingBrainTasks.py"; DestDir: "{app}\engine"; Flags: ignoreversion
Source: "Setup-CodingBrain.cmd"; DestDir: "{app}\engine"; Flags: ignoreversion
#endif

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Icons]
Name: "{autoprograms}\Coding Brain Desktop"; Filename: "{app}\CodingBrainDesktop.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Coding Brain Desktop"; Filename: "{app}\CodingBrainDesktop.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
#ifdef WithEngine
; Ticking this box opens the setup window; Coding Brain's own installer then shows its plan and asks
; before it changes anything (no -Yes: the box is not consent to an unseen plan). Windows still asks
; before administrator changes, and the desktop opens when it finishes.
; Launched through Explorer, not as Setup's child: Setup runs with Windows redirection trust
; enforced and its children inherit it, which hides tools installed behind junctions (Codex).
Filename: "{win}\explorer.exe"; Parameters: """{app}\engine\Setup-CodingBrain.cmd"""; Description: "Set up Coding Brain engine and tools (recommended)"; Flags: postinstall skipifsilent nowait
Filename: "{app}\CodingBrainDesktop.exe"; Description: "Launch Coding Brain Desktop now (without setup)"; Flags: nowait postinstall skipifsilent unchecked
#else
Filename: "{app}\CodingBrainDesktop.exe"; Description: "Launch Coding Brain Desktop"; Flags: nowait postinstall skipifsilent
#endif

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExitCode: Integer;
  ReportPath: String;
  Details: AnsiString;
begin
  Result := '';
  ExtractTemporaryFile('Close-CodingBrainDesktop.ps1');
  ReportPath := ExpandConstant('{tmp}\desktop-close-report.txt');
  // Native PowerShell can inspect 64-bit Python modules even though Setup is 32-bit.
  if not Exec(ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{tmp}\Close-CodingBrainDesktop.ps1') + '" -InstallDir "' +
    ExpandConstant('{app}') + '" -ReportPath "' + ReportPath + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
    Result := 'Unable to check whether Coding Brain Desktop is running. Close Coding Brain Desktop and retry Setup.'
  else if ExitCode <> 0 then begin
    Result := 'Setup could not stop all processes using Coding Brain files. Close the listed processes and run Setup again. ' +
      'Setup has not updated any files.';
    if LoadStringFromFile(ReportPath, Details) then
      Result := Result + #13#10#13#10 + String(Details);
  end;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
#ifndef WithEngine
  if (not WizardSilent()) and (not FileExists(ExpandConstant('{localappdata}\CodingBrain\app\current.json'))) then
    MsgBox('Coding Brain Desktop requires the separate Coding Brain engine. ' +
      'Please install Coding Brain before launching the desktop application. ' +
      'This installer will not install or change the engine.', mbInformation, MB_OK);
#endif
end;
