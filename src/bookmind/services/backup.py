"""Резервное копирование и восстановление: zip с согласованной копией БД, настройками, пользовательскими промптами и манифестом sha256."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from bookmind.domain.exceptions import BookMindError
from bookmind.storage.migrations import MIGRATIONS

DB_REL = "database/bookmind.sqlite"
EXTRA = ("settings.json", "models_catalog.json")
EXTRA_DIRS = ("prompts",)
FORMAT = 1


class BackupError(BookMindError):
    pass


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class BackupService:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def create(self, dest: Path | None = None) -> Path:
        db_path = self.data_dir / DB_REL
        if not db_path.exists():
            raise BackupError("База данных не найдена")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = Path(dest) if dest else self.data_dir / "backups" / f"bookmind-{stamp}.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "bookmind.sqlite"
            src = sqlite3.connect(db_path)
            try:
                dst = sqlite3.connect(snap)
                src.backup(dst)                      # согласованная копия, безопасная при работающей WAL
                dst.close()
            finally:
                src.close()
            files: dict[str, Path] = {DB_REL: snap}
            for name in EXTRA:
                if (self.data_dir / name).is_file():
                    files[name] = self.data_dir / name
            for d in EXTRA_DIRS:
                for p in sorted((self.data_dir / d).rglob("*")) if (self.data_dir / d).is_dir() else []:
                    if p.is_file():
                        files[p.relative_to(self.data_dir).as_posix()] = p
            conn = sqlite3.connect(snap)
            counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("books", "entities", "facts", "events")}
            conn.close()
            manifest = {"format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "schema_version": MIGRATIONS[-1][0], "counts": counts,
                        "files": {rel: {"sha256": _sha(p), "size": p.stat().st_size} for rel, p in files.items()}}
            tmp_zip = dest.with_suffix(dest.suffix + ".part")
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                for rel, p in files.items():
                    z.write(p, rel)
            tmp_zip.replace(dest)
        return dest

    # ---- проверка ----
    def verify(self, archive: Path) -> dict:
        """Проверяет манифест, хэши, безопасность путей и целостность БД. Ничего не распаковывает в рабочую папку."""
        archive = Path(archive)
        try:
            z = zipfile.ZipFile(archive)
        except (zipfile.BadZipFile, OSError) as exc:
            raise BackupError(f"Файл не является корректным архивом: {exc}") from exc
        with z:
            if "manifest.json" not in z.namelist():
                raise BackupError("В архиве нет manifest.json: это не резервная копия BookMind")
            manifest = json.loads(z.read("manifest.json"))
            if manifest.get("format") != FORMAT:
                raise BackupError("Неподдерживаемый формат резервной копии")
            if manifest.get("schema_version", 0) > MIGRATIONS[-1][0]:
                raise BackupError("Копия создана более новой версией приложения")
            names = set(z.namelist()) - {"manifest.json"}
            for rel in names:
                if rel.startswith("/") or ".." in Path(rel).parts or ":" in rel:
                    raise BackupError(f"Небезопасный путь в архиве: {rel}")
            if names != set(manifest["files"]):
                raise BackupError("Состав архива не совпадает с манифестом")
            for rel, meta in manifest["files"].items():
                h = hashlib.sha256(z.read(rel)).hexdigest()
                if h != meta["sha256"]:
                    raise BackupError(f"Контрольная сумма не совпала: {rel}")
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "check.sqlite"
                p.write_bytes(z.read(DB_REL))
                conn = sqlite3.connect(p)
                try:
                    ok = [r[0] for r in conn.execute("PRAGMA integrity_check")] == ["ok"]
                finally:
                    conn.close()
                if not ok:
                    raise BackupError("База данных в копии повреждена")
        return manifest

    def restore(self, archive: Path) -> Path:
        """Восстанавливает данные. Текущие данные сохраняются в backups/pre-restore-*. БД не должна быть открыта."""
        manifest = self.verify(archive)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        keep = self.data_dir / "backups" / f"pre-restore-{stamp}"
        keep.mkdir(parents=True, exist_ok=True)
        for rel in (DB_REL, DB_REL + "-wal", DB_REL + "-shm", *EXTRA):
            p = self.data_dir / rel
            if p.is_file():
                (keep / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, keep / rel)
        if (self.data_dir / "prompts").is_dir():
            shutil.copytree(self.data_dir / "prompts", keep / "prompts", dirs_exist_ok=True)
        with zipfile.ZipFile(archive) as z:
            for rel in manifest["files"]:
                target = self.data_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".restoring")
                tmp.write_bytes(z.read(rel))
                tmp.replace(target)
        for suffix in ("-wal", "-shm"):
            (self.data_dir / (DB_REL + suffix)).unlink(missing_ok=True)
        return keep
