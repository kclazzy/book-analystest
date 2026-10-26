"""Доступ к SQLite: соединение на поток, внешние ключи, WAL, атомарные транзакции с вложенными savepoint."""
from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from bookmind.storage.migrations import migrate


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


class Database:
    def __init__(self, path: Path | str, *, auto_migrate: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        if auto_migrate:
            migrate(self.conn(), self.path)

    # ---- соединения ----
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("PRAGMA synchronous = NORMAL")
            c.execute("PRAGMA busy_timeout = 30000")
            self._local.conn = c
            self._local.depth = 0
            with self._lock:
                self._all.append(c)
        return c

    def close(self) -> None:
        with self._lock:
            for c in self._all:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
        self._local = threading.local()

    # ---- транзакции ----
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Атомарный блок. Вложенные вызовы используют SAVEPOINT; исключение откатывает свой уровень."""
        c = self.conn()
        depth = self._local.depth
        if depth == 0:
            c.execute("BEGIN IMMEDIATE")
        else:
            c.execute(f"SAVEPOINT sp{depth}")
        self._local.depth = depth + 1
        try:
            yield c
        except BaseException:
            self._local.depth = depth
            if depth == 0:
                c.execute("ROLLBACK")
            else:
                c.execute(f"ROLLBACK TO sp{depth}")
                c.execute(f"RELEASE sp{depth}")
            raise
        else:
            self._local.depth = depth
            if depth == 0:
                c.execute("COMMIT")
            else:
                c.execute(f"RELEASE sp{depth}")

    # ---- удобные запросы ----
    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self.conn().execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        row = self.conn().execute(sql, params).fetchone()
        return row[0] if row else None

    def insert(self, table: str, values: dict[str, Any], *, or_ignore: bool = False) -> sqlite3.Cursor:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
        return self.conn().execute(f"{verb} INTO {table} ({cols}) VALUES ({marks})", list(values.values()))

    def update(self, table: str, row_id: Any, values: dict[str, Any], id_col: str = "id") -> None:
        sets = ", ".join(f"{k} = ?" for k in values)
        self.conn().execute(f"UPDATE {table} SET {sets} WHERE {id_col} = ?", [*values.values(), row_id])

    # ---- диагностика ----
    def integrity_report(self) -> dict[str, Any]:
        c = self.conn()
        integrity = [r[0] for r in c.execute("PRAGMA integrity_check").fetchall()]
        fk = [tuple(r) for r in c.execute("PRAGMA foreign_key_check").fetchall()]
        return {"integrity": integrity, "foreign_key_violations": fk,
                "ok": integrity == ["ok"] and not fk}
