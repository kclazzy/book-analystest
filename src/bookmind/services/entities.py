"""Ручное управление сущностями: слияние дубликатов, подтверждение и отклонение кандидатов."""
from __future__ import annotations

from typing import Any

from bookmind.domain.exceptions import BookMindError
from bookmind.services.search import SearchIndex
from bookmind.storage.database import Database, new_id, now_iso


class EntityService:
    def __init__(self, db: Database):
        self.db = db
        self.search = SearchIndex(db)

    # ---- кандидаты ----
    def pending_candidates(self, series_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            """SELECT m.*, a.name AS name_a, a.type AS type_a, b.name AS name_b, b.type AS type_b
               FROM merge_candidates m JOIN entities a ON a.id = m.entity_a JOIN entities b ON b.id = m.entity_b
               WHERE m.series_id = ? AND m.status = 'pending' AND a.status != 'merged' AND b.status != 'merged'
               ORDER BY m.score DESC""", (series_id,))
        return [dict(r) for r in rows]

    def reject(self, candidate_id: str) -> None:
        self.db.execute("UPDATE merge_candidates SET status = 'rejected' WHERE id = ?", (candidate_id,))

    def accept(self, candidate_id: str, keep_id: str | None = None) -> str:
        c = self.db.one("SELECT * FROM merge_candidates WHERE id = ?", (candidate_id,))
        if c is None:
            raise BookMindError("Кандидат не найден")
        a, b = c["entity_a"], c["entity_b"]
        if keep_id is None:  # по умолчанию сохраняем более раннюю сущность
            ra, rb = (self.db.scalar("SELECT rowid FROM entities WHERE id = ?", (x,)) for x in (a, b))
            keep_id = a if ra <= rb else b
        drop_id = b if keep_id == a else a
        self.merge(keep_id, drop_id, reason=f"подтверждён кандидат ({c['reason']})")
        return keep_id

    # ---- слияние ----
    def merge(self, keep_id: str, drop_id: str, reason: str = "ручное слияние") -> None:
        if keep_id == drop_id:
            raise BookMindError("Нельзя объединить сущность с самой собой")
        keep = self.db.one("SELECT * FROM entities WHERE id = ?", (keep_id,))
        drop = self.db.one("SELECT * FROM entities WHERE id = ?", (drop_id,))
        if not keep or not drop:
            raise BookMindError("Сущность не найдена")
        if keep["series_id"] != drop["series_id"]:
            raise BookMindError("Сущности из разных серий объединять нельзя")
        if keep["status"] == "merged" or drop["status"] == "merged":
            raise BookMindError("Одна из сущностей уже объединена")
        if keep["type"] != drop["type"]:
            raise BookMindError("Нельзя объединять сущности разных типов")
        db = self.db
        with db.transaction():
            for r in db.query("SELECT alias, alias_norm, source_chunk_id FROM entity_aliases WHERE entity_id = ?", (drop_id,)):
                db.insert("entity_aliases", {"entity_id": keep_id, "alias": r["alias"], "alias_norm": r["alias_norm"],
                                             "source_chunk_id": r["source_chunk_id"]}, or_ignore=True)
            db.insert("entity_aliases", {"entity_id": keep_id, "alias": drop["name"], "alias_norm": drop["name_norm"],
                                         "source_chunk_id": drop["first_chunk_id"]}, or_ignore=True)
            db.execute("DELETE FROM entity_aliases WHERE entity_id = ?", (drop_id,))

            db.execute("UPDATE OR IGNORE entity_mentions SET entity_id = ?, resolution = 'manual' WHERE entity_id = ?", (keep_id, drop_id))
            db.execute("DELETE FROM entity_mentions WHERE entity_id = ?", (drop_id,))

            db.execute("UPDATE OR IGNORE facts SET entity_id = ? WHERE entity_id = ?", (keep_id, drop_id))
            dup_facts = [r[0] for r in db.query("SELECT id FROM facts WHERE entity_id = ?", (drop_id,))]
            for fid in dup_facts:
                db.execute("DELETE FROM evidence WHERE owner_type = 'fact' AND owner_id = ?", (fid,))
                self.search.remove("fact", fid)
            db.execute("DELETE FROM facts WHERE entity_id = ?", (drop_id,))

            db.execute("UPDATE goals SET entity_id = ? WHERE entity_id = ?", (keep_id, drop_id))
            db.execute("UPDATE OR IGNORE event_participants SET entity_id = ? WHERE entity_id = ?", (keep_id, drop_id))
            db.execute("DELETE FROM event_participants WHERE entity_id = ?", (drop_id,))
            db.execute("UPDATE events SET place_entity_id = ? WHERE place_entity_id = ?", (keep_id, drop_id))
            db.execute("UPDATE OR IGNORE entity_states SET entity_id = ? WHERE entity_id = ?", (keep_id, drop_id))
            db.execute("DELETE FROM entity_states WHERE entity_id = ?", (drop_id,))
            db.execute("UPDATE contradictions SET entity_id = ? WHERE entity_id = ?", (keep_id, drop_id))
            self._merge_relationships(keep_id, drop_id)

            db.execute("UPDATE merge_candidates SET status = 'accepted' WHERE (entity_a = ? AND entity_b = ?) OR (entity_a = ? AND entity_b = ?)",
                       (keep_id, drop_id, drop_id, keep_id))
            for r in db.query("SELECT * FROM merge_candidates WHERE entity_a = ? OR entity_b = ?", (drop_id, drop_id)):
                other = r["entity_b"] if r["entity_a"] == drop_id else r["entity_a"]
                if other == keep_id:
                    continue
                lo, hi = sorted((other, keep_id))
                db.execute("UPDATE OR IGNORE merge_candidates SET entity_a = ?, entity_b = ? WHERE id = ?", (lo, hi, r["id"]))
            db.execute("DELETE FROM merge_candidates WHERE (entity_a = ? OR entity_b = ?) AND status = 'pending'", (drop_id, drop_id))

            if not keep["description"] and drop["description"]:
                db.update("entities", keep_id, {"description": drop["description"]})
            db.update("entities", drop_id, {"status": "merged", "merged_into": keep_id})
            if keep["status"] == "needs_review":
                db.update("entities", keep_id, {"status": "active"})
            db.insert("change_log", {
                "series_id": keep["series_id"], "owner_type": "entity", "owner_id": keep_id, "field": "merge",
                "old_value": f"{drop['name']} ({drop_id})", "new_value": keep["name"], "reason": reason,
                "created_at": now_iso()})
            self.search.remove("entity", drop_id)
            self._reindex(keep_id)

    def _merge_relationships(self, keep_id: str, drop_id: str) -> None:
        db = self.db
        for rel in db.query("SELECT * FROM relationships WHERE entity_a = ? OR entity_b = ?", (drop_id, drop_id)):
            other = rel["entity_b"] if rel["entity_a"] == drop_id else rel["entity_a"]
            if other == keep_id:
                db.execute("DELETE FROM relationships WHERE id = ?", (rel["id"],))
                continue
            lo, hi = sorted((other, keep_id))
            existing = db.one("SELECT id FROM relationships WHERE entity_a = ? AND entity_b = ?", (lo, hi))
            if existing:
                db.execute("UPDATE relationship_history SET relationship_id = ? WHERE relationship_id = ?", (existing["id"], rel["id"]))
                db.execute("DELETE FROM relationships WHERE id = ?", (rel["id"],))
            else:
                db.execute("UPDATE relationships SET entity_a = ?, entity_b = ? WHERE id = ?", (lo, hi, rel["id"]))

    def _reindex(self, entity_id: str) -> None:
        e = self.db.one("SELECT * FROM entities WHERE id = ?", (entity_id,))
        aliases = " ".join(r["alias"] for r in self.db.query("SELECT alias FROM entity_aliases WHERE entity_id = ?", (entity_id,)))
        self.search.index("entity", entity_id, e["series_id"], e["first_book_id"], None, e["name"],
                          f"{e['name']} {aliases} {e['description']}")

    # ---- просмотр ----
    def list_entities(self, series_id: str, etype: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT e.*, (SELECT COUNT(*) FROM entity_mentions m WHERE m.entity_id = e.id) AS mentions "
               "FROM entities e WHERE e.series_id = ? AND e.status != 'merged'")
        params: list[Any] = [series_id]
        if etype:
            sql += " AND e.type = ?"
            params.append(etype)
        sql += " ORDER BY mentions DESC, e.name"
        return [dict(r) for r in self.db.query(sql, params)]
