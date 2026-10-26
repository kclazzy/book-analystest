"""Выбор и перенос папки данных (база анализа, книги, экспорт, резервные копии)."""
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from bookmind.config import home_dir, write_pointer
from bookmind.domain.exceptions import BookMindError

DB_REL = Path("database") / "bookmind.sqlite"
SUBDIRS = ("books", "exports", "backups", "logs", "cache")
COPY_FILES = ("settings.json",)


@dataclass
class TargetCheck:
    ok: bool
    has_data: bool = False        # в папке уже есть база BookMind
    message: str = ""


def _inside(a: Path, b: Path) -> bool:
    try:
        a.resolve().relative_to(b.resolve())
        return True
    except ValueError:
        return False


def check_target(current: Path, target: Path) -> TargetCheck:
    target = Path(target)
    if not str(target).strip():
        return TargetCheck(False, message="Папка не выбрана.")
    if target.resolve() == Path(current).resolve():
        return TargetCheck(False, message="Это уже текущая папка данных.")
    if _inside(target, current) or _inside(current, target):
        return TargetCheck(False, message="Новая папка не может находиться внутри текущей (и наоборот).")
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".bookmind_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return TargetCheck(False, message=f"В эту папку нельзя записывать: {e}")
    has = (target / DB_REL).exists()
    if not has and any(target.iterdir()):
        return TargetCheck(False, message="Папка не пуста. Выберите пустую папку или папку с данными BookMind.")
    return TargetCheck(True, has_data=has)


def copy_data(current: Path, target: Path) -> None:
    """Копирует данные; базу — через backup API SQLite (корректно при открытой базе и WAL). Исходные данные не удаляются."""
    current, target = Path(current), Path(target)
    src_db = current / DB_REL
    if src_db.exists():
        dst_db = target / DB_REL
        dst_db.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
        dst = sqlite3.connect(dst_db)
        try:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BookMindError("Копия базы не прошла проверку целостности.")
        finally:
            dst.close()
            src.close()
    for sub in SUBDIRS:
        if (current / sub).is_dir():
            shutil.copytree(current / sub, target / sub, dirs_exist_ok=True)
    for name in COPY_FILES:
        if (current / name).is_file():
            shutil.copy2(current / name, target / name)


def change_data_dir(current: Path, target: Path, *, copy: bool = True, home: Path | None = None) -> None:
    """Проверяет папку, при необходимости копирует данные и запоминает выбор (вступает в силу после перезапуска)."""
    chk = check_target(current, target)
    if not chk.ok:
        raise BookMindError(chk.message)
    if copy and not chk.has_data:
        copy_data(current, target)
    for sub in ("database", *SUBDIRS):
        (Path(target) / sub).mkdir(parents=True, exist_ok=True)
    write_pointer(Path(target), home)


def reset_data_dir(home: Path | None = None) -> None:
    write_pointer(None, home)


__all__ = ["TargetCheck", "check_target", "copy_data", "change_data_dir", "reset_data_dir", "home_dir"]
