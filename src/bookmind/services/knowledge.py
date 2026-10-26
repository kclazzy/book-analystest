"""Чтение базы знаний: досье персонажей, цели, отношения, хронология, линии, загадки, мотивы. Всегда с источниками.
По умолчанию показываются только подтверждённые цитатой записи (verified)."""
from __future__ import annotations

import json
from typing import Any

from bookmind.domain.enums import FactKind
from bookmind.storage.database import Database


class KnowledgeService:
    def __init__(self, db: Database):
        self.db = db

    # ---- источники ----
    def sources_for(self, owner_type: str, owner_id: str, verified_only: bool = True) -> list[dict[str, Any]]:
        sql = ("SELECT e.id, e.quote, e.start_char, e.end_char, e.verified, e.chunk_id, ch.id AS chapter_id, ch.idx AS chapter_idx, "
               "ch.title AS chapter_title, b.id AS book_id, b.title AS book_title, b.series_order, c.start_char AS chunk_start "
               "FROM evidence e JOIN chunks c ON c.id = e.chunk_id JOIN chapters ch ON ch.id = e.chapter_id "
               "JOIN books b ON b.id = e.book_id WHERE e.owner_type = ? AND e.owner_id = ?")
        if verified_only:
            sql += " AND e.verified = 1"
        sql += " ORDER BY b.series_order, ch.idx, c.idx"
        out = []
        for r in self.db.query(sql, (owner_type, owner_id)):
            d = dict(r)
            d["chapter_start"] = (r["chunk_start"] + r["start_char"]) if r["start_char"] is not None else None
            out.append(d)
        return out

    def chunk_location(self, chunk_id: str) -> dict[str, Any] | None:
        r = self.db.one(
            "SELECT c.id AS chunk_id, ch.idx AS chapter_idx, ch.title AS chapter_title, b.id AS book_id, b.title AS book_title, "
            "b.series_order FROM chunks c JOIN chapters ch ON ch.id = c.chapter_id JOIN books b ON b.id = c.book_id WHERE c.id = ?",
            (chunk_id,))
        return dict(r) if r else None

    # ---- персонажи и сущности ----
    def entities(self, series_id: str, etype: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT e.*, (SELECT COUNT(*) FROM entity_mentions m WHERE m.entity_id = e.id) AS mentions, "
               "(SELECT COUNT(*) FROM facts f WHERE f.entity_id = e.id AND f.verification = 'verified') AS facts "
               "FROM entities e WHERE e.series_id = ? AND e.status != 'merged'")
        params: list[Any] = [series_id]
        if etype:
            sql += " AND e.type = ?"
            params.append(etype)
        sql += " ORDER BY mentions DESC, e.name"
        return [dict(r) for r in self.db.query(sql, params)]

    def character_dossier(self, entity_id: str, up_to_book_order: int | None = None,
                          include_unverified: bool = False) -> dict[str, Any]:
        e = self.db.one("SELECT * FROM entities WHERE id = ?", (entity_id,))
        if e is None:
            raise KeyError(entity_id)
        limit = up_to_book_order if up_to_book_order is not None else 10 ** 6
        ver_sql = "" if include_unverified else " AND f.verification = 'verified'"
        aliases = [r["alias"] for r in self.db.query("SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY id", (entity_id,))
                   if r["alias"].casefold() != e["name"].casefold()]

        mentions = self.db.query(
            "SELECT c.id AS chunk_id FROM entity_mentions m JOIN chunks c ON c.id = m.chunk_id JOIN books b ON b.id = c.book_id "
            "WHERE m.entity_id = ? AND b.series_order <= ? ORDER BY b.series_order, c.idx", (entity_id, limit))
        first = self.chunk_location(mentions[0]["chunk_id"]) if mentions else None
        last = [self.chunk_location(m["chunk_id"]) for m in mentions[-3:]]

        facts_by_cat: dict[str, list[dict[str, Any]]] = {}
        status_by_book: dict[str, dict[str, Any]] = {}
        for f in self.db.query(
                f"SELECT f.*, b.series_order, b.title AS book_title FROM facts f JOIN books b ON b.id = f.book_id "
                f"WHERE f.entity_id = ? AND b.series_order <= ?{ver_sql} ORDER BY b.series_order, f.chunk_id, f.created_at",
                (entity_id, limit)):
            item = {"id": f["id"], "statement": f["statement"], "kind": f["kind"], "kind_label": FactKind(f["kind"]).label,
                    "verification": f["verification"], "confidence": f["confidence"], "book_title": f["book_title"],
                    "series_order": f["series_order"], "sources": self.sources_for("fact", f["id"], not include_unverified)}
            facts_by_cat.setdefault(f["category"], []).append(item)
            if f["category"] == "status" and f["verification"] == "verified":
                status_by_book[f["book_id"]] = {"book_title": f["book_title"], "statement": f["statement"],
                                                 "series_order": f["series_order"]}
        ordered_status = sorted(status_by_book.values(), key=lambda s: s["series_order"])

        goals = self.goals(e["series_id"], entity_id=entity_id, up_to_book_order=up_to_book_order,
                           include_unverified=include_unverified)
        rels = self.relationships_of(entity_id, up_to_book_order, include_unverified)
        events = [dict(r) for r in self.db.query(
            "SELECT v.id, v.description, v.narrative_mode, v.story_time, p.role, ch.title AS chapter_title, b.title AS book_title, "
            "b.series_order FROM event_participants p JOIN events v ON v.id = p.event_id JOIN chapters ch ON ch.id = v.chapter_id "
            "JOIN books b ON b.id = v.book_id WHERE p.entity_id = ? AND b.series_order <= ?"
            + ("" if include_unverified else " AND v.verification = 'verified'") + " ORDER BY b.series_order, v.seq",
            (entity_id, limit))]
        return {
            "id": e["id"], "name": e["name"], "type": e["type"], "aliases": aliases, "description": e["description"] or None,
            "needs_review": e["status"] == "needs_review", "first_appearance": first, "last_appearances": last,
            "facts": facts_by_cat,
            "current_state": ordered_status[-1]["statement"] if ordered_status else None,   # не заполняется, если неизвестно
            "status_by_book": ordered_status, "goals": goals, "relationships": rels, "events": events,
        }

    # ---- цели ----
    def goals(self, series_id: str, entity_id: str | None = None, status: str | None = None,
              up_to_book_order: int | None = None, include_unverified: bool = False) -> list[dict[str, Any]]:
        sql = ("SELECT g.*, e.name AS entity_name, b.series_order FROM goals g JOIN entities e ON e.id = g.entity_id "
               "JOIN books b ON b.id = g.first_book_id WHERE g.series_id = ? AND b.series_order <= ?")
        params: list[Any] = [series_id, up_to_book_order if up_to_book_order is not None else 10 ** 6]
        if entity_id:
            sql += " AND g.entity_id = ?"
            params.append(entity_id)
        if status:
            sql += " AND g.status = ?"
            params.append(status)
        if not include_unverified:
            sql += " AND g.verification = 'verified'"
        sql += " ORDER BY b.series_order, g.created_at, g.rowid"
        out = []
        for g in self.db.query(sql, params):
            d = dict(g)
            d["history"] = self.goal_history(g["id"], up_to_book_order)
            d["sources"] = self.sources_for("goal", g["id"], not include_unverified)
            d["status_at"] = d["history"][-1]["new_status"] if d["history"] else g["status"]
            if up_to_book_order is not None and d["history"]:
                d["status"] = d["status_at"]
            out.append(d)
        return out

    def goal_history(self, goal_id: str, up_to_book_order: int | None = None) -> list[dict[str, Any]]:
        limit = up_to_book_order if up_to_book_order is not None else 10 ** 6
        rows = self.db.query(
            "SELECT h.*, ch.title AS chapter_title, b.title AS book_title, b.series_order FROM goal_history h "
            "LEFT JOIN chunks c ON c.id = h.chunk_id LEFT JOIN chapters ch ON ch.id = c.chapter_id "
            "LEFT JOIN books b ON b.id = c.book_id WHERE h.goal_id = ? AND COALESCE(b.series_order, 0) <= ? ORDER BY h.id",
            (goal_id, limit))
        return [dict(r) for r in rows]

    # ---- отношения ----
    def relationships_of(self, entity_id: str, up_to_book_order: int | None = None,
                         include_unverified: bool = False) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT r.*, a.name AS name_a, b.name AS name_b FROM relationships r JOIN entities a ON a.id = r.entity_a "
            "JOIN entities b ON b.id = r.entity_b WHERE r.entity_a = ? OR r.entity_b = ?", (entity_id, entity_id))
        out = []
        for r in rows:
            if not include_unverified and r["verification"] != "verified":
                continue
            other_id = r["entity_b"] if r["entity_a"] == entity_id else r["entity_a"]
            hist = self.relationship_history(r["id"], up_to_book_order)
            if not hist:
                continue
            out.append({"id": r["id"], "other_id": other_id, "other_name": r["name_b"] if r["entity_a"] == entity_id else r["name_a"],
                        "rel_type": hist[-1]["rel_type"], "state": hist[-1]["state"], "history": hist,
                        "sources": self.sources_for("relationship", r["id"], not include_unverified)})
        return out

    def relationship_between(self, entity_a: str, entity_b: str, up_to_book_order: int | None = None) -> dict[str, Any] | None:
        lo, hi = sorted((entity_a, entity_b))
        r = self.db.one("SELECT * FROM relationships WHERE entity_a = ? AND entity_b = ?", (lo, hi))
        if not r:
            return None
        hist = self.relationship_history(r["id"], up_to_book_order)
        return {"id": r["id"], "history": hist, "rel_type": hist[-1]["rel_type"] if hist else None,
                "sources": self.sources_for("relationship", r["id"])}

    def relationship_history(self, rel_id: str, up_to_book_order: int | None = None) -> list[dict[str, Any]]:
        limit = up_to_book_order if up_to_book_order is not None else 10 ** 6
        rows = self.db.query(
            "SELECT h.*, ch.title AS chapter_title, b.title AS book_title, b.series_order FROM relationship_history h "
            "LEFT JOIN chunks c ON c.id = h.chunk_id LEFT JOIN chapters ch ON ch.id = c.chapter_id "
            "LEFT JOIN books b ON b.id = c.book_id WHERE h.relationship_id = ? AND COALESCE(b.series_order, 0) <= ? ORDER BY h.id",
            (rel_id, limit))
        return [dict(r) for r in rows]

    def all_relationships(self, series_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT r.id, a.name AS name_a, b.name AS name_b FROM relationships r JOIN entities a ON a.id = r.entity_a "
            "JOIN entities b ON b.id = r.entity_b WHERE r.series_id = ? AND r.verification = 'verified'", (series_id,))
        out = []
        for r in rows:
            hist = self.relationship_history(r["id"])
            out.append({"id": r["id"], "a": r["name_a"], "b": r["name_b"], "rel_type": hist[-1]["rel_type"] if hist else "",
                        "state": hist[-1]["state"] if hist else "", "changes": len(hist)})
        return out

    # ---- хронология ----
    def chronology(self, series_id: str, book_id: str | None = None, order: str = "narrative",
                   include_unverified: bool = False) -> list[dict[str, Any]]:
        """order='narrative' — порядок повествования. order='story_estimate' — ОЦЕНОЧНАЯ фактическая хронология:
        флешбэки и воспоминания с указанным временем ставятся раньше линейных событий своей книги (точных дат модель не даёт)."""
        sql = ("SELECT v.*, ch.title AS chapter_title, ch.idx AS chapter_idx, b.title AS book_title, b.series_order, "
               "pl.name AS place_name FROM events v JOIN chapters ch ON ch.id = v.chapter_id JOIN books b ON b.id = v.book_id "
               "LEFT JOIN entities pl ON pl.id = v.place_entity_id WHERE v.series_id = ?")
        params: list[Any] = [series_id]
        if book_id:
            sql += " AND v.book_id = ?"
            params.append(book_id)
        if not include_unverified:
            sql += " AND v.verification = 'verified'"
        sql += " ORDER BY b.series_order, v.seq"
        events = []
        for r in self.db.query(sql, params):
            d = dict(r)
            d["participants"] = [dict(p) for p in self.db.query(
                "SELECT p.role, e.id AS entity_id, e.name FROM event_participants p JOIN entities e ON e.id = p.entity_id "
                "WHERE p.event_id = ?", (r["id"],))]
            d["goals"] = [g["goal_id"] for g in self.db.query("SELECT goal_id FROM event_goals WHERE event_id = ?", (r["id"],))]
            d["sources"] = self.sources_for("event", r["id"])
            d["story_time_known"] = bool(r["story_time"].strip())
            events.append(d)
        if order == "story_estimate":
            def key(ev):
                earlier = ev["narrative_mode"] in ("flashback", "memory") and ev["story_time_known"]
                return (ev["series_order"], 0 if earlier else 1, ev["seq"])
            events.sort(key=key)
        return events

    # ---- линии, загадки, мотивы ----
    def plot_threads(self, series_id: str, include_unverified: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM plot_threads WHERE series_id = ?" + ("" if include_unverified else " AND verification = 'verified'")
        out = []
        for t in self.db.query(sql + " ORDER BY created_at, rowid", (series_id,)):
            d = dict(t)
            d["events"] = [dict(e) for e in self.db.query(
                "SELECT v.id, v.description FROM plot_thread_events x JOIN events v ON v.id = x.event_id WHERE x.thread_id = ? ORDER BY v.seq",
                (t["id"],))]
            d["sources"] = self.sources_for("thread", t["id"])
            out.append(d)
        return out

    def questions(self, series_id: str, status: str | None = None, kind: str | None = None,
                  include_unverified: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM open_questions WHERE series_id = ?"
        params: list[Any] = [series_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if not include_unverified:
            sql += " AND verification = 'verified'"
        out = []
        for q in self.db.query(sql + " ORDER BY created_at, rowid", params):
            d = dict(q)
            d["clues"] = json.loads(q["clues_json"])
            d["hypotheses"] = json.loads(q["hypotheses_json"])
            d["raised_at"] = self.chunk_location(q["raised_chunk_id"]) if q["raised_chunk_id"] else None
            d["resolved_at"] = self.chunk_location(q["resolved_chunk_id"]) if q["resolved_chunk_id"] else None
            d["sources"] = self.sources_for("question", q["id"])
            out.append(d)
        return out

    def motifs(self, series_id: str, include_unverified: bool = False) -> list[dict[str, Any]]:
        out = []
        sql = "SELECT * FROM motifs WHERE series_id = ?" + ("" if include_unverified else " AND verification = 'verified'")
        for m in self.db.query(sql + " ORDER BY created_at, rowid", (series_id,)):
            occ = self.db.query(
                "SELECT o.chunk_id, ch.title AS chapter_title, b.title AS book_title FROM motif_occurrences o "
                "JOIN chunks c ON c.id = o.chunk_id JOIN chapters ch ON ch.id = c.chapter_id JOIN books b ON b.id = c.book_id "
                "WHERE o.motif_id = ? ORDER BY b.series_order, c.idx", (m["id"],))
            kind = m["kind"]
            # «повторяющийся мотив» подтверждён только несколькими разными эпизодами; иначе — лишь предположение
            effective = kind if not (kind == "recurring_motif" and len(occ) < 2) else "hypothesis"
            out.append({**dict(m), "claimed_kind": kind, "kind": effective, "occurrences": [dict(o) for o in occ],
                        "sources": self.sources_for("motif", m["id"])})
        return out

    def character_knowledge(self, series_id: str, topic: str) -> list[dict[str, Any]]:
        """Кто знает тайну/сведение: факты категорий knowledge и secret, содержащие тему."""
        from bookmind.services.search import stem_text
        needle = stem_text(topic).split()
        out = []
        for f in self.db.query(
                "SELECT f.*, e.name FROM facts f JOIN entities e ON e.id = f.entity_id WHERE f.series_id = ? "
                "AND f.verification = 'verified' AND f.category IN ('knowledge','secret')", (series_id,)):
            hay = stem_text(f["statement"]).split()
            if all(any(h.startswith(n) for h in hay) for n in needle):
                out.append({"entity": f["name"], "statement": f["statement"], "sources": self.sources_for("fact", f["id"])})
        return out

    # ---- память серии ----
    def contradictions(self, series_id: str, status: str | None = "pending", entity_id: str | None = None) -> list[dict[str, Any]]:
        from bookmind.domain.enums import ContradictionKind
        sql = ("SELECT c.*, e.name AS entity_name FROM contradictions c LEFT JOIN entities e ON e.id = c.entity_id "
               "WHERE c.series_id = ?")
        params: list[Any] = [series_id]
        if status:
            sql += " AND c.status = ?"
            params.append(status)
        if entity_id:
            sql += " AND c.entity_id = ?"
            params.append(entity_id)
        out = []
        for c in self.db.query(sql + " ORDER BY c.created_at, c.rowid", params):
            side = {}
            for key in ("fact_a", "fact_b"):
                f = self.db.one("SELECT f.statement, b.title AS book_title, b.series_order FROM facts f "
                                "JOIN books b ON b.id = f.book_id WHERE f.id = ?", (c[key],))
                side[key] = {"id": c[key], **dict(f), "sources": self.sources_for("fact", c[key])}
            out.append({"id": c["id"], "entity_id": c["entity_id"], "entity_name": c["entity_name"], "category": c["category"],
                        "kind": c["kind"], "kind_label": ContradictionKind(c["kind"]).label, "explanation": c["explanation"],
                        "status": c["status"], "a": side["fact_a"], "b": side["fact_b"]})
        return out

    def set_contradiction_status(self, contradiction_id: str, status: str) -> None:
        if status not in ("pending", "confirmed", "dismissed"):
            raise ValueError(status)
        self.db.execute("UPDATE contradictions SET status = ? WHERE id = ?", (status, contradiction_id))

    def state_at(self, entity_id: str, book_order: int) -> dict[str, Any] | None:
        """Сохранённый снимок состояния героя на конец тома (последнего, не позже book_order)."""
        r = self.db.one(
            "SELECT s.*, b.title AS book_title, b.series_order FROM entity_states s JOIN books b ON b.id = s.book_id "
            "WHERE s.entity_id = ? AND b.series_order <= ? ORDER BY b.series_order DESC LIMIT 1", (entity_id, book_order))
        if not r:
            return None
        return {"book_title": r["book_title"], "series_order": r["series_order"], "summary": r["summary"] or None,
                **json.loads(r["snapshot_json"])}
