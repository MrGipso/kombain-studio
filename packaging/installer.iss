; Inno Setup script for the Windows installer of Комбайн Studio.
;
; Packs the PyInstaller folder dist\KombainStudio (see kombain-studio.spec).
; Built in CI (.github/workflows/windows-installer.yml):
;   ISCC.exe /DAppVersion=1.0.0-rc.3 packaging\installer.iss
;
; Per-user install (no administrator rights) into
; %LOCALAPPDATA%\Programs\Kombain Studio. Settings and job history live in
; %LOCALAPPDATA%\PipelineStudio and projects in Documents\Комбайн Studio, so
; installing a newer version over an older one, or uninstalling, keeps them.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
; Never change AppId: it is how a newer installer finds the installed copy.
AppId={{FAD415B2-6CEC-46E0-8010-D460CE3D035A}
AppName=Комбайн Studio
AppVersion={#AppVersion}
AppVerName=Комбайн Studio {#AppVersion}
AppPublisher=MrGipso
AppPublisherURL=https://github.com/MrGipso/kombain-studio
AppSupportURL=https://github.com/MrGipso/kombain-studio/issues
AppUpdatesURL=https://github.com/MrGipso/kombain-studio/releases
DefaultDirName={localappdata}\Programs\Kombain Studio
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=KombainStudio-{#AppVersion}-Setup
SetupIconFile=kombain-studio.ico
UninstallDisplayIcon={app}\KombainStudio.exe
UninstallDisplayName=Комбайн Studio
LicenseFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Close a running copy before replacing its files.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Files of the previous version that the new one no longer ships.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\KombainStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Комбайн Studio"; Filename: "{app}\KombainStudio.exe"
Name: "{autodesktop}\Комбайн Studio"; Filename: "{app}\KombainStudio.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\KombainStudio.exe"; Description: "{cm:LaunchProgram,Комбайн Studio}"; Flags: nowait postinstall skipifsilent

[Code]
// The window is drawn by Microsoft Edge WebView2. Windows 11 and updated
// Windows 10 already have it; without it the app cannot show its interface.
const
  WebView2Key = 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2KeyUser = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2Url = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';

function WebView2Installed(): Boolean;
var
  Version: String;
begin
  Result :=
    (RegQueryStringValue(HKLM, WebView2Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, WebView2KeyUser, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
begin
  if (CurStep = ssPostInstall) and not WizardSilent() and not WebView2Installed() then
  begin
    if MsgBox('Для окна программы нужен компонент Microsoft Edge WebView2, а он не найден.' + #13#10 +
              'Открыть страницу загрузки Microsoft?', mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', WebView2Url, '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
  end;
end;
