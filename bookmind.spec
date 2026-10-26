# PyInstaller: сборка Windows 11 (запуск на Windows: pyinstaller bookmind.spec --noconfirm)
# Результат: dist/BookMind/BookMind.exe. Данные пользователя хранятся отдельно (BOOKMIND_DATA_DIR или ./data).
from pathlib import Path

root = Path(SPECPATH)
a = Analysis(
    [str(root / "app.py")],
    pathex=[str(root / "src")],
    datas=[(str(root / "src/bookmind/data"), "bookmind/data"), (str(root / "src/bookmind/prompts"), "bookmind/prompts")],
    hiddenimports=["bookmind.ui.app", "bookmind.main"],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtQuick", "PySide6.Qt3DCore"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BookMind", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="BookMind")
