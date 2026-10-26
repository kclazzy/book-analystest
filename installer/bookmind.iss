; Установщик BookMind Local (Inno Setup 6). Сборка: ISCC.exe /DAppVersion=0.2.0 installer\bookmind.iss
; Предварительно: pyinstaller bookmind.spec --noconfirm  (создаёт dist\BookMind)
#ifndef AppVersion
  #define AppVersion "0.5.0"
#endif

[Setup]
AppId={{8E3C6F4A-5B1D-4C77-9A52-3D1F0B6E7A21}
AppName=BookMind Local
AppVersion={#AppVersion}
AppPublisher=BookMind
DefaultDirName={autopf}\BookMind
DefaultGroupName=BookMind
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=BookMind-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=BookMind Local
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать значок на рабочем столе"; GroupDescription: "Дополнительно:"

[Files]
Source: "..\dist\BookMind\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\BookMind"; Filename: "{app}\BookMind.exe"
Name: "{autodesktop}\BookMind"; Filename: "{app}\BookMind.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\BookMind.exe"; Description: "Запустить BookMind"; Flags: nowait postinstall

[Code]
// Ollama — отдельная программа: установщик её не ставит, но предупреждает, если её нет.
// Сама BookMind при каждом запуске проверяет Ollama и модель (вкладка «Система») и умеет запускать Ollama.
function OllamaFound: Boolean;
begin
  Result := FileExists(ExpandConstant('{localappdata}\Programs\Ollama\ollama.exe'))
         or FileExists(ExpandConstant('{commonpf64}\Ollama\ollama.exe'))
         or FileExists(ExpandConstant('{commonpf}\Ollama\ollama.exe'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
begin
  if (CurStep = ssPostInstall) and (not WizardSilent) and (not OllamaFound) then
  begin
    if MsgBox('Для работы BookMind нужна программа Ollama, но она не найдена на этом компьютере.' + #13#10 + #13#10 +
              'Открыть страницу загрузки Ollama сейчас? После установки Ollama запустите BookMind: на вкладке «Модели» установите модель Qwen.',
              mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', 'https://ollama.com/download', '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
  end;
end;
