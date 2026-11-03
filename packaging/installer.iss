; Inno Setup 6 script for Voice Recorder Pro (FR-PKG-002, FR-PKG-003)
; Build with build.bat, or: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
#define AppName "Voice Recorder Pro"
#ifndef AppVersion
  #define AppVersion "1.0.2"
#endif
#define AppExe "VoiceRecorderPro.exe"

[Setup]
AppId={{6E0C6F5A-2C1B-4C47-9F1E-7A6D2B1C9E21}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Neo Time Consulting
AppPublisherURL=https://software.4u.hk
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
; per-user install without admin rights by default, all-users optional
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\Output
OutputBaseFilename=VoiceRecorderPro-Setup-{#AppVersion}
SetupIconFile=vrp.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
; build.bat passes /DSIGN and /Svrpsign=... when a certificate is configured, so the installer
; and the uninstaller it contains are signed too.
#ifdef SIGN
SignTool=vrpsign
SignedUninstaller=yes
#endif

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "autostart"; Description: "Start &minimised in the tray when I sign in"; GroupDescription: "Start-up:"; Flags: unchecked

[Files]
Source: "..\dist\VoiceRecorderPro\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} Help"; Filename: "{app}\_internal\docs\HELP.html"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "VoiceRecorderPro"; ValueData: """{app}\{#AppExe}"" --minimised"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#AppExe} /F"; Flags: runhidden; RunOnceId: "StopApp"

[UninstallDelete]
Type: dirifempty; Name: "{app}"

[Code]
// Recordings are never removed. Settings are removed only if the user agrees.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox('Also remove your Voice Recorder Pro settings and logs?' + #13#10 +
              'Your recordings are kept either way.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      DelTree(ExpandConstant('{userappdata}\VoiceRecorderPro'), True, True, True);
      DelTree(ExpandConstant('{localappdata}\VoiceRecorderPro\Logs'), True, True, True);
    end;
  end;
end;
