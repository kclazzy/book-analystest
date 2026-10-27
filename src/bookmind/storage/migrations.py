"""Миграции схемы SQLite. Каждая миграция применяется атомарно; перед миграцией существующей БД делается копия."""
from __future__ import annotations

import shutil
import sqlite3
import sqlite3 as _s
from datetime import datetime, timezone
from pathlib import Path

MIGRATION_001 = """
CREATE TABLE series (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE books (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    series_order INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    format TEXT NOT NULL,
    source_filename TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    source_sha256 TEXT NOT NULL UNIQUE,
    source_size INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    import_log_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_books_series ON books(series_id, series_order);

CREATE TABLE chapters (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    transformations_json TEXT NOT NULL DEFAULT '[]',
    manual_edit INTEGER NOT NULL DEFAULT 0,
    UNIQUE(book_id, idx)
);

CREATE TABLE chunks (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    idx_in_chapter INTEGER NOT NULL,
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    text TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    UNIQUE(book_id, idx)
);
CREATE INDEX idx_chunks_chapter ON chunks(chapter_id, idx_in_chapter);

CREATE TABLE analysis_runs (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);
CREATE INDEX idx_runs_book ON analysis_runs(book_id, started_at);

CREATE TABLE analysis_tasks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    raw_response TEXT,
    issues_json TEXT NOT NULL DEFAULT '[]',
    started_at TEXT,
    finished_at TEXT,
    duration_s REAL,
    UNIQUE(run_id, stage, scope_id)
);
CREATE INDEX idx_tasks_run ON analysis_tasks(run_id, status, seq);

CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    attributes_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',          -- active | merged | needs_review
    merged_into TEXT REFERENCES entities(id),
    first_book_id TEXT REFERENCES books(id) ON DELETE SET NULL,
    first_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    created_run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_entities_series ON entities(series_id, type, name_norm);

CREATE TABLE entity_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    source_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    UNIQUE(entity_id, alias_norm)
);
CREATE INDEX idx_aliases_norm ON entity_aliases(alias_norm);

CREATE TABLE entity_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    run_id TEXT,
    surface TEXT NOT NULL,
    surface_norm TEXT NOT NULL,
    resolution TEXT NOT NULL,                       -- exact | alias | new | ambiguous | manual
    score REAL NOT NULL DEFAULT 0,
    UNIQUE(entity_id, chunk_id, surface_norm)
);
CREATE INDEX idx_mentions_chunk ON entity_mentions(chunk_id);

CREATE TABLE merge_candidates (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    entity_a TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_b TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    score REAL NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',          -- pending | accepted | rejected
    created_at TEXT NOT NULL,
    UNIQUE(entity_a, entity_b)
);

CREATE TABLE facts (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    run_id TEXT,
    entity_id TEXT REFERENCES entities(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    statement TEXT NOT NULL,
    statement_norm TEXT NOT NULL,
    kind TEXT NOT NULL,
    verification TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(entity_id, chunk_id, statement_norm)
);
CREATE INDEX idx_facts_entity ON facts(entity_id, category);
CREATE INDEX idx_facts_book ON facts(book_id, run_id);

CREATE TABLE evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    quote TEXT NOT NULL,
    start_char INTEGER,
    end_char INTEGER,
    verified INTEGER NOT NULL DEFAULT 0,
    run_id TEXT,
    UNIQUE(owner_type, owner_id, chunk_id, quote)
);
CREATE INDEX idx_evidence_owner ON evidence(owner_type, owner_id);
CREATE INDEX idx_evidence_chunk ON evidence(chunk_id);

CREATE TABLE events (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    run_id TEXT,
    seq INTEGER NOT NULL,
    description TEXT NOT NULL,
    description_norm TEXT NOT NULL,
    causes TEXT NOT NULL DEFAULT '',
    consequences TEXT NOT NULL DEFAULT '',
    place_entity_id TEXT REFERENCES entities(id) ON DELETE SET NULL,
    place_text TEXT NOT NULL DEFAULT '',
    story_time TEXT NOT NULL DEFAULT '',
    narrative_mode TEXT NOT NULL DEFAULT 'linear',
    confidence REAL NOT NULL DEFAULT 0.5,
    verification TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_events_book ON events(book_id, seq);

CREATE TABLE event_participants (
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (event_id, entity_id)
);

CREATE TABLE goals (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    description_norm TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT '',
    motivation TEXT NOT NULL DEFAULT '',
    desired_result TEXT NOT NULL DEFAULT '',
    obstacles TEXT NOT NULL DEFAULT '',
    methods TEXT NOT NULL DEFAULT '',
    allies TEXT NOT NULL DEFAULT '',
    opponents TEXT NOT NULL DEFAULT '',
    goal_type TEXT NOT NULL DEFAULT 'stated',
    status TEXT NOT NULL DEFAULT 'active',
    origin_event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
    first_book_id TEXT REFERENCES books(id) ON DELETE SET NULL,
    first_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    last_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    verification TEXT NOT NULL,
    created_run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_goals_entity ON goals(entity_id, status);

CREATE TABLE event_goals (
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, goal_id)
);

CREATE TABLE goal_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    run_id TEXT,
    old_status TEXT,
    new_status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_goal_history ON goal_history(goal_id, id);

CREATE TABLE relationships (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    entity_a TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_b TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    rel_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    first_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    last_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    verification TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(entity_a, entity_b)
);

CREATE TABLE relationship_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relationship_id TEXT NOT NULL REFERENCES relationships(id) ON DELETE CASCADE,
    chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    run_id TEXT,
    rel_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_rel_history ON relationship_history(relationship_id, id);

CREATE TABLE plot_threads (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    title_norm TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    first_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    last_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    verification TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(series_id, title_norm)
);

CREATE TABLE plot_thread_events (
    thread_id TEXT NOT NULL REFERENCES plot_threads(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    PRIMARY KEY (thread_id, event_id)
);

CREATE TABLE open_questions (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    text_norm TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    raised_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    clues_json TEXT NOT NULL DEFAULT '[]',
    hypotheses_json TEXT NOT NULL DEFAULT '[]',
    resolution TEXT NOT NULL DEFAULT '',
    resolved_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    verification TEXT NOT NULL,
    created_run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_questions_series ON open_questions(series_id, status);

CREATE TABLE motifs (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    verification TEXT NOT NULL,
    first_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE(series_id, name_norm)
);

CREATE TABLE motif_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    motif_id TEXT NOT NULL REFERENCES motifs(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    run_id TEXT,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(motif_id, chunk_id)
);

CREATE TABLE contradictions (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    entity_id TEXT REFERENCES entities(id) ON DELETE CASCADE,
    category TEXT NOT NULL DEFAULT '',
    fact_a TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    fact_b TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    explanation TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',          -- pending | confirmed | dismissed
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(fact_a, fact_b)
);

CREATE TABLE entity_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    summary TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(entity_id, book_id)
);

CREATE TABLE report_versions (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    book_id TEXT REFERENCES books(id) ON DELETE CASCADE,
    scope_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,                              -- chapter_summary | arc_summary | book_report | series_report
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    structured_json TEXT NOT NULL DEFAULT '{}',
    run_id TEXT,
    model TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_reports ON report_versions(series_id, kind, scope_id, version);

CREATE TABLE change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason TEXT NOT NULL DEFAULT '',
    chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_changelog_owner ON change_log(owner_type, owner_id);

CREATE VIRTUAL TABLE search_index USING fts5(
    kind UNINDEXED, ref_id UNINDEXED, series_id UNINDEXED, book_id UNINDEXED, chapter_id UNINDEXED,
    title, body,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

MIGRATION_002 = """
CREATE TABLE adaptation_projects (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    params_json TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'new',               -- new | skeleton | premise | cast | plan | review | done
    status TEXT NOT NULL DEFAULT 'new',              -- new | running | paused | awaiting_review | completed | failed
    model TEXT NOT NULL DEFAULT '',
    skeleton_json TEXT NOT NULL DEFAULT '{}',
    premise_json TEXT NOT NULL DEFAULT '{}',
    cast_json TEXT NOT NULL DEFAULT '{}',
    issues_json TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE adaptation_chapters (
    project_id TEXT NOT NULL REFERENCES adaptation_projects(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    plan_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',          -- pending | ok | flagged
    attempts INTEGER NOT NULL DEFAULT 0,
    check_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (project_id, idx)
);
"""

MIGRATION_003 = """
ALTER TABLE adaptation_chapters ADD COLUMN text TEXT NOT NULL DEFAULT '';
ALTER TABLE adaptation_chapters ADD COLUMN text_status TEXT NOT NULL DEFAULT 'none';   -- none | partial | ok | flagged
ALTER TABLE adaptation_chapters ADD COLUMN text_parts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE adaptation_chapters ADD COLUMN text_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE adaptation_chapters ADD COLUMN text_check_json TEXT NOT NULL DEFAULT '{}';
"""

MIGRATION_004 = """
ALTER TABLE adaptation_projects ADD COLUMN qa_json TEXT NOT NULL DEFAULT '{}';
"""

# (версия, SQL-скрипт). Новые изменения схемы добавляются только новыми элементами.
MIGRATIONS: list[tuple[int, str]] = [
    (1, MIGRATION_001),
    (2, MIGRATION_002),
    (3, MIGRATION_003),
    (4, MIGRATION_004),
]


def _statements(script: str):
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if _s.complete_statement(buf):
            if buf.strip():
                yield buf
            buf = ""
    if buf.strip():
        yield buf


def current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
    if not exists:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def latest_version() -> int:
    return MIGRATIONS[-1][0]


def migrate(conn: sqlite3.Connection, db_path: Path | None = None) -> list[int]:
    """Применяет недостающие миграции. Перед изменением непустой БД создаётся резервная копия файла."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    have = current_version(conn)
    pending = [(v, sql) for v, sql in MIGRATIONS if v > have]
    if not pending:
        return []
    if have > 0 and db_path is not None and db_path.exists():
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        shutil.copy2(db_path, db_path.with_name(f"{db_path.name}.pre-v{pending[0][0]}-{stamp}.bak"))
    applied: list[int] = []
    for version, script in pending:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _statements(script):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                         (version, datetime.now(timezone.utc).isoformat()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied
