# Установка BookMind из исходников (запасной вариант, если нет готового установщика).
# Запуск: powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Host "Не найден Python. Установите Python 3.12 с https://www.python.org/downloads/ (галочка 'Add python.exe to PATH') и запустите скрипт снова." -ForegroundColor Red
    exit 1
}
& py -3.12 --version 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Нужен Python 3.12 (команда 'py -3.12')." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path ".venv")) { & py -3.12 -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[gui]"
if ($LASTEXITCODE -ne 0) { Write-Host "Установка зависимостей не удалась." -ForegroundColor Red; exit 1 }

# ярлык на рабочем столе
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "BookMind.lnk"))
$lnk.TargetPath = Join-Path $root ".venv\Scripts\pythonw.exe"
$lnk.Arguments = "`"$(Join-Path $root 'app.py')`""
$lnk.WorkingDirectory = $root
$lnk.Save()

$ollama = (Get-Command ollama -ErrorAction SilentlyContinue) -or (Test-Path "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe")
if (-not $ollama) {
    Write-Host "Ollama не найден. Скачайте и установите: https://ollama.com/download" -ForegroundColor Yellow
    Start-Process "https://ollama.com/download"
}
Write-Host "Готово. Запускайте ярлык BookMind на рабочем столе. Проверка Ollama и модели выполняется при старте приложения." -ForegroundColor Green
