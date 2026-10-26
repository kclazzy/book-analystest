"""Журнал ошибок: понятная запись каждой ошибки (время, где, что, трассировка) в файл JSON-lines.
Хранится отдельно от базы (чтобы работать, даже если сломана база); не содержит текстов книг. Из него собирается отчёт для разработчика."""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import sys
import threading
import time
import traceback
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

LEVELS = ("WARNING", "ERROR", "CRITICAL")
MAX_BYTES = 1_000_000          # при превышении старые записи отбрасываются (остаётся ~половина)
DEDUP_WINDOW_S = 60.0


@dataclass
class ErrorEntry:
    ts: str
    level: str
    source: str
    message: str
    traceback: str = ""
    version: str = ""
    count: int = 1
    context: dict[str, Any] = field(default_factory=dict)

    def title(self) -> str:
        return self.message.strip().split("\n")[0][:200]


def scrub(text: str) -> str:
    """Убирает из текста имя пользователя Windows/Linux и домашний путь: журнал можно отправлять другому человеку."""
    if not text:
        return text
    home = str(Path.home())
    out = text.replace(home, "~")
    out = re.sub(r"(?i)([A-Z]:[\\/]+Users[\\/]+)[^\\/\s'\"]+", r"\1<user>", out)
    out = re.sub(r"(/home/|/Users/)[^/\s'\"]+", r"\1<user>", out)
    return out


class ErrorLog:
    def __init__(self, path: Path, version: str = ""):
        self.path = Path(path)
        self.version = version
        self._lock = threading.RLock()
        self._last: dict[str, tuple[float, int]] = {}      # ключ записи -> (время, номер строки среди недавних)
        self._revision = 0                                   # растёт при каждой записи: окно по нему понимает, что есть новое
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    @property
    def revision(self) -> int:
        return self._revision

    # ---------- запись ----------
    def record(self, message: str, exc: BaseException | None = None, *, source: str = "", level: str = "ERROR",
               context: dict[str, Any] | None = None, tb: str = "") -> ErrorEntry:
        level = level if level in LEVELS else "ERROR"
        if exc is not None and not tb:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        entry = ErrorEntry(datetime.now().isoformat(timespec="seconds"), level, source, scrub(str(message)), scrub(tb), self.version,
                           1, {k: scrub(str(v)) for k, v in (context or {}).items()})
        key = f"{level}|{source}|{entry.title()}|{entry.traceback[-300:]}"
        with self._lock:
            self._revision += 1
            now = time.monotonic()
            prev = self._last.get(key)
            if prev and now - prev[0] < DEDUP_WINDOW_S:
                self._bump_last(entry)                       # повтор той же ошибки — увеличиваем счётчик, не плодим строки
                self._last[key] = (now, prev[1] + 1)
                return entry
            self._last[key] = (now, 1)
            self._append(entry)
        return entry

    def _bump_last(self, entry: ErrorEntry) -> None:
        rows = self._read_raw()
        for d in reversed(rows):
            if d.get("level") == entry.level and d.get("source") == entry.source and d.get("message") == entry.message:
                d["count"] = int(d.get("count", 1)) + 1
                d["ts"] = entry.ts
                self._write_raw(rows)
                return
        self._append(entry)

    def _append(self, entry: ErrorEntry) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            if self.path.stat().st_size > MAX_BYTES:
                rows = self._read_raw()
                self._write_raw(rows[len(rows) // 2:])
        except OSError:
            pass                                             # журнал не должен ронять приложение

    def _read_raw(self) -> list[dict]:
        rows: list[dict] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue                                 # оборванная строка после сбоя
        except OSError:
            pass
        return rows

    def _write_raw(self, rows: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        tmp.replace(self.path)

    # ---------- чтение ----------
    def entries(self, level: str | None = None, limit: int = 500) -> list[ErrorEntry]:
        """Новые записи первыми."""
        with self._lock:
            rows = self._read_raw()
        out = []
        for d in reversed(rows):
            if level and d.get("level") != level:
                continue
            try:
                out.append(ErrorEntry(**{k: d[k] for k in ErrorEntry.__dataclass_fields__ if k in d}))
            except TypeError:
                continue
            if len(out) >= limit:
                break
        return out

    def count(self, level: str | None = None) -> int:
        return len(self.entries(level, limit=10**6))

    def clear(self) -> None:
        with self._lock:
            self._last.clear()
            self._revision += 1
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass

    # ---------- отчёт ----------
    def system_info(self, extra: dict[str, Any] | None = None) -> str:
        lines = [f"BookMind {self.version}", f"Дата: {datetime.now().isoformat(timespec='seconds')}",
                 f"ОС: {platform.platform()}", f"Python: {sys.version.split()[0]}",
                 f"Установленная версия (exe): {bool(getattr(sys, 'frozen', False))}"]
        for k, v in (extra or {}).items():
            lines.append(f"{k}: {scrub(str(v))}")
        return "\n".join(lines) + "\n"

    def export_report(self, dest: Path, *, log_file: Path | None = None, extra: dict[str, Any] | None = None) -> Path:
        """Zip для разработчика: ошибки, системные сведения и хвост общего лога. Тексты книг и база не включаются."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("system.txt", self.system_info(extra))
            body = []
            for e in reversed(self.entries(limit=1000)):
                head = f"[{e.ts}] {e.level} · {e.source or '—'}" + (f" · ×{e.count}" if e.count > 1 else "")
                ctx = "".join(f"\n    {k}: {v}" for k, v in e.context.items())
                body.append(f"{head}\n{e.message}{ctx}\n{e.traceback}".rstrip() + "\n")
            z.writestr("errors.txt", ("\n" + "-" * 60 + "\n").join(body) if body else "Ошибок нет.\n")
            if log_file and Path(log_file).exists():
                try:
                    tail = Path(log_file).read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
                    z.writestr("bookmind.log", scrub("\n".join(tail)) + "\n")
                except OSError:
                    pass
        return dest


class _LogHandler(logging.Handler):
    def __init__(self, errors: ErrorLog):
        super().__init__(level=logging.WARNING)
        self.errors = errors

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("bookmind.errorlog"):
            return
        try:
            exc = record.exc_info[1] if record.exc_info else None
            level = "WARNING" if record.levelno < logging.ERROR else ("CRITICAL" if record.levelno >= logging.CRITICAL else "ERROR")
            self.errors.record(record.getMessage(), exc, source=f"лог:{record.name}", level=level)
        except Exception:      # noqa: BLE001 — обработчик журнала никогда не бросает исключений
            pass


def install_hooks(errors: ErrorLog) -> None:
    """Перехват неперехваченных исключений (главный поток, фоновые потоки) и записей logging уровня WARNING и выше."""
    prev_exc, prev_thread = sys.excepthook, threading.excepthook

    def on_exception(tp, value, tb):
        if not issubclass(tp, KeyboardInterrupt):
            errors.record(f"Необработанная ошибка: {value}", value, source="приложение", level="CRITICAL",
                          tb="".join(traceback.format_exception(tp, value, tb)))
        prev_exc(tp, value, tb)

    def on_thread(args):
        if args.exc_type is not SystemExit:
            errors.record(f"Ошибка в фоновом потоке: {args.exc_value}", args.exc_value, source="поток", level="CRITICAL",
                          tb="".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        prev_thread(args)

    sys.excepthook = on_exception
    threading.excepthook = on_thread
    logging.getLogger().addHandler(_LogHandler(errors))
