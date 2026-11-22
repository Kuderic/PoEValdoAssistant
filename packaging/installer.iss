; Inno Setup script: iscc /DAppVersion=x.y.z packaging\installer.iss
; Builds dist\ValdoSniper-Setup.exe from the PyInstaller folder dist\ValdoSniper.
;
; Per-user install (no admin): the app writes config.yaml edits, logs\,
; searches.txt, price_cache.json and scoring_overrides.yaml beside the exe,
; which Program Files would forbid.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F3D2B8E-0C1A-4E5B-9A7D-3C2F1B0E9D41}
AppName=Valdo Sniper
AppVersion={#AppVersion}
AppPublisher=Valdo Sniper
DefaultDirName={localappdata}\Programs\ValdoSniper
DefaultGroupName=Valdo Sniper
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ValdoSniper-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
InfoBeforeFile=before-install.txt
UninstallDisplayIcon={app}\ValdoSniper.exe
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\ValdoSniper\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
; user files: installed once, never overwritten by an upgrade, kept on uninstall
Source: "..\config.yaml"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "..\.env.example"; DestDir: "{app}"; DestName: ".env"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "..\SETUP.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Valdo Sniper"; Filename: "{app}\ValdoSniper.exe"; WorkingDir: "{app}"
Name: "{group}\Setup guide"; Filename: "{app}\SETUP.md"
Name: "{group}\Uninstall Valdo Sniper"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Valdo Sniper"; Filename: "{app}\ValdoSniper.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\ValdoSniper.exe"; WorkingDir: "{app}"; Description: "Start Valdo Sniper now"; Flags: nowait postinstall skipifsilent

[Code]
function ChromeInstalled(): Boolean;
var
  Path: String;
begin
  Result :=
    RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe', '', Path) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe', '', Path);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpInfoBefore) and not ChromeInstalled() then
    Result := MsgBox('Google Chrome was not found. Valdo Sniper needs Chrome for the trade site.' + #13#10 +
      'Install it from google.com/chrome first.' + #13#10#13#10 + 'Continue anyway?',
      mbConfirmation, MB_YESNO) = IDYES;
end;
