; BlueX Dental Planlama — Inno Setup kurulum betiği
; Derleme: ISCC.exe dental_setup.iss  (çıktı: installer\BlueXDental_Setup.exe)

#define AppName "BlueX Dental Planlama"
#define AppVersion "1.1.0"
#define AppExe "dental_planlama.exe"

[Setup]
AppId={{7B2F4E9A-6C1D-4A8B-9E3F-BLUEXDENTAL1}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=BlueX
DefaultDirName={localappdata}\BlueXDental
DefaultGroupName={#AppName}
; yönetici gerektirmesin — klinik bilgisayarlarında admin şifresi sorunu olmasın
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=.
OutputBaseFilename=BlueXDental_Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\app.ico
UninstallDisplayIcon={app}\{#AppExe}

[Languages]
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; hasta verisi (%APPDATA%\BlueX) bilerek SİLİNMEZ — yalnız program klasörü kalkar
Type: filesandordirs; Name: "{app}"
