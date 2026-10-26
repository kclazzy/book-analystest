import sqlite3

import pytest

from bookmind.storage.database import Database, new_id, now_iso
from bookmind.storage.migrations import MIGRATIONS, current_version, latest_version

EXPECTED_TABLES = {
    "series", "books", "chapters", "chunks", "analysis_runs", "analysis_tasks", "entities", "entity_aliases",
    "entity_mentions", "facts", "evidence", "events", "event_participants", "goals", "goal_history",
    "relationships", "relationship_history", "plot_threads", "open_questions", "report_versions",
    "search_index", "contradictions", "change_log", "merge_candidates", "motifs", "entity_states",
}


def test_all_required_tables_exist(db):
    names = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type IN ('table')")}
    assert EXPECTED_TABLES <= names
    assert current_version(db.conn()) == latest_version()


def test_foreign_keys_enforced(db):
    assert db.scalar("PRAGMA foreign_keys") == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.insert("chapters", {"id": "c", "book_id": "missing", "idx": 0, "title": "t", "text": "x", "raw_text": "x", "char_count": 1})


def test_transaction_rolls_back_and_nested_savepoints(db):
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.insert("series", {"id": "s1", "title": "A", "created_at": now_iso()})
            raise RuntimeError("boom")
    assert db.scalar("SELECT COUNT(*) FROM series") == 0

    with db.transaction():
        db.insert("series", {"id": "s1", "title": "A", "created_at": now_iso()})
        with pytest.raises(ValueError):
            with db.transaction():
                db.insert("series", {"id": "s2", "title": "B", "created_at": now_iso()})
                raise ValueError("inner")
    assert [r["id"] for r in db.query("SELECT id FROM series")] == ["s1"]


def test_migrations_idempotent_and_backup_on_upgrade(data_dir, monkeypatch):
    path = data_dir / "x.sqlite"
    d1 = Database(path)
    d1.insert("series", {"id": new_id(), "title": "Keep", "created_at": now_iso()})
    d1.close()
    d2 = Database(path)  # повторное открытие ничего не ломает
    assert d2.scalar("SELECT COUNT(*) FROM series") == 1
    d2.close()
    from bookmind.storage import migrations
    monkeypatch.setattr(migrations, "MIGRATIONS", MIGRATIONS + [(99, "CREATE TABLE extra_test (id INTEGER PRIMARY KEY);")])
    d3 = Database(path)
    assert d3.scalar("SELECT COUNT(*) FROM extra_test") == 0
    assert d3.scalar("SELECT COUNT(*) FROM series") == 1
    d3.close()
    assert list(data_dir.glob("x.sqlite.pre-v99-*.bak"))


def test_integrity_report(db):
    assert db.integrity_report()["ok"]


def test_fts5_works_with_russian(db):
    from bookmind.services.search import SearchIndex
    idx = SearchIndex(db)
    idx.index("chunk", "k1", "s", "b", "c", "Глава", "Мира нашла серебряный ключ в руке наставника")
    idx.index("chunk", "k2", "s", "b", "c", "Глава", "Тихон работал в кузнице")
    hits = idx.search("Где находится серебряного ключа?")
    assert [h.ref_id for h in hits][:1] == ["k1"]
    assert idx.search("и в на") == []
