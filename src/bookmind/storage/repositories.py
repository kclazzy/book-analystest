"""Репозитории: библиотека (серии, книги, главы, фрагменты) и задачи анализа."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from bookmind.storage.database import Database, new_id, now_iso


class LibraryRepo:
    def __init__(self, db: Database):
        self.db = db

    # ---- серии ----
    def create_series(self, title: str, description: str = "") -> str:
        sid = new_id()
        self.db.insert("series", {"id": sid, "title": title, "description": description, "created_at": now_iso()})
        return sid

    def get_series(self, series_id: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM series WHERE id = ?", (series_id,))

    def list_series(self) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM series ORDER BY title COLLATE NOCASE")

    def rename_series(self, series_id: str, title: str) -> None:
        self.db.update("series", series_id, {"title": title})

    # ---- книги ----
    def find_book_by_sha(self, sha: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM books WHERE source_sha256 = ?", (sha,))

    def get_book(self, book_id: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM books WHERE id = ?", (book_id,))

    def list_books(self, series_id: str | None = None) -> list[sqlite3.Row]:
        if series_id:
            return self.db.query("SELECT * FROM books WHERE series_id = ? ORDER BY series_order, title", (series_id,))
        return self.db.query("SELECT * FROM books ORDER BY series_id, series_order, title")

    def book_stats(self, book_id: str) -> dict[str, Any]:
        return {
            "chapters": self.db.scalar("SELECT COUNT(*) FROM chapters WHERE book_id = ?", (book_id,)),
            "chunks": self.db.scalar("SELECT COUNT(*) FROM chunks WHERE book_id = ?", (book_id,)),
            "chars": self.db.scalar("SELECT COALESCE(SUM(char_count),0) FROM chapters WHERE book_id = ?", (book_id,)),
        }

    def move_book(self, book_id: str, series_id: str, order: int) -> None:
        self.db.update("books", book_id, {"series_id": series_id, "series_order": order})

    def delete_book(self, book_id: str) -> None:
        with self.db.transaction():
            self.db.execute("DELETE FROM search_index WHERE book_id = ?", (book_id,))
            self.db.execute("DELETE FROM books WHERE id = ?", (book_id,))

    # ---- главы и фрагменты ----
    def chapters_of(self, book_id: str) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM chapters WHERE book_id = ? ORDER BY idx", (book_id,))

    def get_chapter(self, chapter_id: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM chapters WHERE id = ?", (chapter_id,))

    def chunks_of(self, book_id: str) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM chunks WHERE book_id = ? ORDER BY idx", (book_id,))

    def chunks_of_chapter(self, chapter_id: str) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM chunks WHERE chapter_id = ? ORDER BY idx_in_chapter", (chapter_id,))

    def get_chunk(self, chunk_id: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM chunks WHERE id = ?", (chunk_id,))

    def chunk_location(self, chunk_id: str) -> dict[str, Any] | None:
        row = self.db.one(
            """SELECT c.id AS chunk_id, c.idx, c.idx_in_chapter, c.start_char, c.end_char,
                      ch.id AS chapter_id, ch.idx AS chapter_idx, ch.title AS chapter_title,
                      b.id AS book_id, b.title AS book_title, b.series_order
               FROM chunks c JOIN chapters ch ON ch.id = c.chapter_id JOIN books b ON b.id = c.book_id
               WHERE c.id = ?""", (chunk_id,))
        return dict(row) if row else None

    def has_analysis(self, book_id: str) -> bool:
        return bool(self.db.scalar("SELECT COUNT(*) FROM analysis_runs WHERE book_id = ?", (book_id,)))


class TaskRepo:
    def __init__(self, db: Database):
        self.db = db

    def create_run(self, book_id: str, model: str, prompt_version: str, params: dict[str, Any]) -> str:
        rid = new_id()
        self.db.insert("analysis_runs", {
            "id": rid, "book_id": book_id, "model": model, "prompt_version": prompt_version,
            "params_json": json.dumps(params, ensure_ascii=False), "status": "running", "started_at": now_iso()})
        return rid

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM analysis_runs WHERE id = ?", (run_id,))

    def find_run(self, book_id: str, model: str, prompt_version: str) -> sqlite3.Row | None:
        return self.db.one(
            "SELECT * FROM analysis_runs WHERE book_id = ? AND model = ? AND prompt_version = ? "
            "ORDER BY started_at DESC LIMIT 1", (book_id, model, prompt_version))

    def latest_run(self, book_id: str, model: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM analysis_runs WHERE book_id = ? AND model = ? ORDER BY started_at DESC LIMIT 1", (book_id, model))

    def update_run_params(self, run_id: str, params: dict[str, Any], prompt_version: str) -> None:
        self.db.execute("UPDATE analysis_runs SET params_json = ?, prompt_version = ? WHERE id = ?",
                        (json.dumps(params, ensure_ascii=False), prompt_version, run_id))

    def runs_of(self, book_id: str) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM analysis_runs WHERE book_id = ? ORDER BY started_at DESC", (book_id,))

    def set_run_status(self, run_id: str, status: str, error: str | None = None) -> None:
        finished = now_iso() if status in ("completed", "failed", "cancelled") else None
        self.db.update("analysis_runs", run_id, {"status": status, "finished_at": finished, "error": error})

    def ensure_task(self, run_id: str, stage: str, scope_type: str, scope_id: str, seq: int) -> None:
        self.db.insert("analysis_tasks", {
            "id": new_id(), "run_id": run_id, "stage": stage, "scope_type": scope_type, "scope_id": scope_id,
            "seq": seq, "status": "pending"}, or_ignore=True)

    def reset_interrupted(self, run_id: str) -> int:
        """Задачи, оставшиеся в running после аварийной остановки, возвращаются в очередь."""
        cur = self.db.execute("UPDATE analysis_tasks SET status = 'pending' WHERE run_id = ? AND status = 'running'", (run_id,))
        return cur.rowcount

    def reset_failed(self, run_id: str) -> int:
        """Явное «продолжить» даёт упавшим задачам новые попытки."""
        cur = self.db.execute("UPDATE analysis_tasks SET status = 'pending', attempts = 0 WHERE run_id = ? AND status = 'failed'", (run_id,))
        return cur.rowcount

    def next_tasks(self, run_id: str, stage: str, max_attempts: int) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM analysis_tasks WHERE run_id = ? AND stage = ? AND "
            "(status = 'pending' OR (status = 'failed' AND attempts < ?)) ORDER BY seq",
            (run_id, stage, max_attempts))

    def mark_running(self, task_id: str) -> None:
        self.db.execute("UPDATE analysis_tasks SET status = 'running', attempts = attempts + 1, started_at = ?, error = NULL WHERE id = ?",
                        (now_iso(), task_id))

    def mark_done(self, task_id: str, raw: str, issues: list[str], duration: float, status: str = "done") -> None:
        self.db.update("analysis_tasks", task_id, {
            "status": status, "raw_response": raw, "issues_json": json.dumps(issues, ensure_ascii=False),
            "finished_at": now_iso(), "duration_s": duration, "error": None})

    def mark_failed(self, task_id: str, error: str, raw: str = "", duration: float = 0.0) -> None:
        self.db.update("analysis_tasks", task_id, {
            "status": "failed", "error": error[:2000], "raw_response": raw, "finished_at": now_iso(), "duration_s": duration})

    def progress(self, run_id: str, stage: str | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) AS n FROM analysis_tasks WHERE run_id = ?"
        params: list[Any] = [run_id]
        if stage:
            sql += " AND stage = ?"
            params.append(stage)
        counts = {r["status"]: r["n"] for r in self.db.query(sql + " GROUP BY status", params)}
        counts["total"] = sum(counts.values())
        return counts

    def tasks_of(self, run_id: str, stage: str | None = None) -> list[sqlite3.Row]:
        if stage:
            return self.db.query("SELECT * FROM analysis_tasks WHERE run_id = ? AND stage = ? ORDER BY seq", (run_id, stage))
        return self.db.query("SELECT * FROM analysis_tasks WHERE run_id = ? ORDER BY stage, seq", (run_id,))
