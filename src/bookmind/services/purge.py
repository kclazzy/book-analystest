"""Удаление результатов анализа книги (для переанализа или после правки глав). Выполняется атомарно."""
from __future__ import annotations

from bookmind.storage.database import Database


def purge_book_analysis(db: Database, book_id: str) -> None:
    chunk_ids = "(SELECT id FROM chunks WHERE book_id = :b)"
    p = {"b": book_id}
    with db.transaction():
        # цели: история этой книги удаляется, статус возвращается к предыдущему состоянию
        touched_goals = [r[0] for r in db.query(
            f"SELECT DISTINCT goal_id FROM goal_history WHERE chunk_id IN {chunk_ids}", p)]
        db.execute(f"DELETE FROM goal_history WHERE chunk_id IN {chunk_ids}", p)
        db.execute("DELETE FROM goals WHERE first_book_id = :b", p)
        for gid in touched_goals:
            last = db.one("SELECT new_status FROM goal_history WHERE goal_id = ? ORDER BY id DESC LIMIT 1", (gid,))
            if last:
                db.execute("UPDATE goals SET status = ?, last_chunk_id = NULL WHERE id = ?", (last["new_status"], gid))
        db.execute(f"UPDATE goals SET last_chunk_id = NULL WHERE last_chunk_id IN {chunk_ids}", p)

        # отношения
        touched_rel = [r[0] for r in db.query(
            f"SELECT DISTINCT relationship_id FROM relationship_history WHERE chunk_id IN {chunk_ids}", p)]
        db.execute(f"DELETE FROM relationship_history WHERE chunk_id IN {chunk_ids}", p)
        for rid in touched_rel:
            last = db.one("SELECT rel_type, state, description FROM relationship_history WHERE relationship_id = ? "
                          "ORDER BY id DESC LIMIT 1", (rid,))
            if last is None:
                db.execute("DELETE FROM relationships WHERE id = ?", (rid,))
            else:
                db.execute("UPDATE relationships SET rel_type = ?, state = ?, description = ?, last_chunk_id = NULL "
                           "WHERE id = ?", (last["rel_type"], last["state"], last["description"], rid))
        db.execute(f"UPDATE relationships SET last_chunk_id = NULL WHERE last_chunk_id IN {chunk_ids}", p)

        # вопросы: раскрытые этой книгой возвращаются в «открыт»
        db.execute("DELETE FROM open_questions WHERE book_id = :b", p)
        db.execute(f"UPDATE open_questions SET status = 'open', resolution = '', resolved_chunk_id = NULL "
                   f"WHERE resolved_chunk_id IN {chunk_ids}", p)

        # линии и мотивы
        db.execute(f"DELETE FROM plot_threads WHERE first_chunk_id IN {chunk_ids}", p)
        db.execute(f"UPDATE plot_threads SET last_chunk_id = NULL WHERE last_chunk_id IN {chunk_ids}", p)
        db.execute(f"DELETE FROM motif_occurrences WHERE chunk_id IN {chunk_ids}", p)
        db.execute("DELETE FROM motifs WHERE id NOT IN (SELECT motif_id FROM motif_occurrences) "
                   f"AND first_chunk_id IN {chunk_ids}", p)

        # факты, события, доказательства, упоминания, отчёты, состояния
        db.execute("DELETE FROM contradictions WHERE fact_a IN (SELECT id FROM facts WHERE book_id = :b) "
                   "OR fact_b IN (SELECT id FROM facts WHERE book_id = :b)", p)
        db.execute("DELETE FROM facts WHERE book_id = :b", p)
        db.execute("DELETE FROM events WHERE book_id = :b", p)
        db.execute("DELETE FROM evidence WHERE book_id = :b", p)
        db.execute(f"DELETE FROM entity_mentions WHERE chunk_id IN {chunk_ids}", p)
        db.execute("DELETE FROM report_versions WHERE book_id = :b", p)
        db.execute("DELETE FROM entity_states WHERE book_id = :b", p)
        db.execute(f"DELETE FROM change_log WHERE chunk_id IN {chunk_ids}", p)

        # сущности, появившиеся только в этой книге и больше ни на что не ссылающиеся
        db.execute("""
            DELETE FROM entities WHERE first_book_id = :b
              AND id NOT IN (SELECT entity_id FROM entity_mentions)
              AND id NOT IN (SELECT entity_id FROM facts WHERE entity_id IS NOT NULL)
              AND id NOT IN (SELECT entity_id FROM goals)
              AND id NOT IN (SELECT entity_id FROM event_participants)
              AND id NOT IN (SELECT entity_a FROM relationships UNION SELECT entity_b FROM relationships)
              AND id NOT IN (SELECT merged_into FROM entities WHERE merged_into IS NOT NULL)
        """, p)
        db.execute("UPDATE entities SET first_chunk_id = NULL WHERE first_chunk_id IN " + chunk_ids, p)

        # индекс поиска: остаются только фрагменты текста
        db.execute("DELETE FROM search_index WHERE book_id = :b AND kind != 'chunk'", p)
        db.execute("DELETE FROM analysis_runs WHERE book_id = :b", p)
