"""Сохранение глубокого анализа: цели, отношения, сюжетные линии, загадки, мотивы — с историей изменений.
Изменение состояния уже известной записи принимается только при подтверждённой цитате."""
from __future__ import annotations

import json

from bookmind.analysis.persister import ChunkContext, SaveStats
from bookmind.analysis.models import ChunkRef
from bookmind.domain.enums import (
    EntityType, GoalStatus, GoalType, MotifKind, QuestionKind, QuestionStatus, ThreadStatus, Verification,
)
from bookmind.domain.schemas import DeepAnalysis
from bookmind.ingestion.text_utils import jaccard, norm_key
from bookmind.services.search import SearchIndex
from bookmind.storage.database import Database, new_id, now_iso

GOAL_MATCH = 0.5
QUESTION_MATCH = 0.6
EVENT_LINK_MATCH = 0.3
_TYPE_RANK = {GoalType.STATED.value: 3, GoalType.DEMONSTRATED.value: 2, GoalType.HYPOTHESIS.value: 1}


def _ver(ok: bool) -> str:
    return (Verification.VERIFIED if ok else Verification.UNVERIFIED).value


class DeepPersister:
    def __init__(self, db: Database):
        self.db = db
        self.search = SearchIndex(db)

    def save(self, chunk: ChunkRef, run_id: str, deep: DeepAnalysis, offered: frozenset[str] = frozenset()) -> SaveStats:
        stats = SaveStats()
        with self.db.transaction():
            ctx = ChunkContext(self.db, chunk, run_id, offered, stats)
            goal_ids = self._goals(ctx, deep)
            self._relationships(ctx, deep)
            self._threads(ctx, deep)
            self._questions(ctx, deep)
            self._motifs(ctx, deep)
            self._link_goals_to_events(ctx, goal_ids)
        return stats

    # ---- цели ----
    def _goals(self, ctx: ChunkContext, deep: DeepAnalysis) -> list[str]:
        touched: list[str] = []
        ch = ctx.chunk
        for g in deep.goals:
            if not g.description.strip() or not g.character.strip():
                ctx.stats.skipped += 1
                continue
            eid = ctx.entity_any(g.character, EntityType.CHARACTER, (EntityType.CHARACTER,))
            if not eid:
                continue
            check = ctx.verifier.check(g.quote)
            ok = check.verified
            existing = self._match_goal(eid, g.description)
            if existing is None:
                gid = new_id()
                first_event = self.db.one(
                    "SELECT e.id FROM events e JOIN event_participants p ON p.event_id = e.id "
                    "WHERE e.chunk_id = ? AND p.entity_id = ? ORDER BY e.seq LIMIT 1", (ch.chunk_id, eid))
                self.db.insert("goals", {
                    "id": gid, "series_id": ch.series_id, "entity_id": eid, "description": g.description.strip(),
                    "description_norm": norm_key(g.description), "origin": g.origin, "motivation": g.motivation,
                    "desired_result": g.desired_result, "obstacles": g.obstacles, "methods": g.methods,
                    "allies": g.allies, "opponents": g.opponents, "goal_type": g.goal_type.value,
                    "status": g.status.value, "origin_event_id": first_event["id"] if first_event else None,
                    "first_book_id": ch.book_id, "first_chunk_id": ch.chunk_id, "last_chunk_id": ch.chunk_id,
                    "verification": _ver(ok), "created_run_id": ctx.run_id, "created_at": now_iso(), "updated_at": now_iso()})
                self._goal_history(gid, ch, ctx.run_id, None, g.status.value, g.change_note or "цель обнаружена", g)
                ctx.add_evidence("goal", gid, g.quote)
                if ok:
                    self.search.index("goal", gid, ch.series_id, ch.book_id, ch.chapter_id,
                                      self._name(eid), f"{g.description} {g.motivation} {g.desired_result}")
                else:
                    ctx.stats.issues.append("цель без подтверждающей цитаты сохранена как unverified")
                touched.append(gid)
                continue
            # обновление известной цели
            if not ok:
                ctx.stats.issues.append(f"обновление цели «{existing['description'][:40]}» не принято: цитата не подтверждена")
                continue
            gid = existing["id"]
            updates: dict = {"last_chunk_id": ch.chunk_id, "updated_at": now_iso()}
            for field_name in ("origin", "motivation", "desired_result", "obstacles", "methods", "allies", "opponents"):
                new_val = getattr(g, field_name)
                if new_val and not existing[field_name]:
                    updates[field_name] = new_val
            if _TYPE_RANK[g.goal_type.value] > _TYPE_RANK[existing["goal_type"]]:
                updates["goal_type"] = g.goal_type.value
            if existing["verification"] != "verified":
                updates["verification"] = "verified"
            if g.status.value != existing["status"] and g.status != GoalStatus.UNKNOWN:
                updates["status"] = g.status.value
                self._goal_history(gid, ch, ctx.run_id, existing["status"], g.status.value, g.change_note, g)
                self.db.insert("change_log", {
                    "series_id": ch.series_id, "owner_type": "goal", "owner_id": gid, "field": "status",
                    "old_value": existing["status"], "new_value": g.status.value, "reason": g.change_note,
                    "chunk_id": ch.chunk_id, "run_id": ctx.run_id, "created_at": now_iso()})
            self.db.update("goals", gid, updates)
            ctx.add_evidence("goal", gid, g.quote)
            touched.append(gid)
        return touched

    def _match_goal(self, entity_id: str, description: str):
        best, best_score = None, 0.0
        for r in self.db.query("SELECT * FROM goals WHERE entity_id = ?", (entity_id,)):
            score = jaccard(description, r["description"])
            if score > best_score:
                best, best_score = r, score
        return best if best_score >= GOAL_MATCH else None

    def _goal_history(self, goal_id: str, ch: ChunkRef, run_id: str, old: str | None, new: str, note: str, g) -> None:
        snap = {"description": g.description, "motivation": g.motivation, "goal_type": g.goal_type.value}
        self.db.insert("goal_history", {
            "goal_id": goal_id, "chunk_id": ch.chunk_id, "run_id": run_id, "old_status": old, "new_status": new,
            "note": note, "snapshot_json": json.dumps(snap, ensure_ascii=False), "created_at": now_iso()})

    def _link_goals_to_events(self, ctx: ChunkContext, goal_ids: list[str]) -> None:
        for gid in goal_ids:
            eid = self.db.scalar("SELECT entity_id FROM goals WHERE id = ?", (gid,))
            for e in self.db.query(
                    "SELECT e.id FROM events e JOIN event_participants p ON p.event_id = e.id WHERE e.chunk_id = ? AND p.entity_id = ?",
                    (ctx.chunk.chunk_id, eid)):
                self.db.insert("event_goals", {"event_id": e["id"], "goal_id": gid}, or_ignore=True)

    # ---- отношения ----
    def _relationships(self, ctx: ChunkContext, deep: DeepAnalysis) -> None:
        ch = ctx.chunk
        for r in deep.relationships:
            if not r.rel_type.strip():
                ctx.stats.skipped += 1
                continue
            a = ctx.entity_any(r.a, EntityType.CHARACTER)
            b = ctx.entity_any(r.b, EntityType.CHARACTER)
            if not a or not b or a == b:
                ctx.stats.skipped += 1
                continue
            lo, hi = sorted((a, b))
            ok = ctx.verifier.check(r.quote).verified
            rel_type = r.rel_type.strip().casefold()
            existing = self.db.one("SELECT * FROM relationships WHERE entity_a = ? AND entity_b = ?", (lo, hi))
            if existing is None:
                rid = new_id()
                self.db.insert("relationships", {
                    "id": rid, "series_id": ch.series_id, "entity_a": lo, "entity_b": hi, "rel_type": rel_type,
                    "state": r.state, "description": r.description, "first_chunk_id": ch.chunk_id,
                    "last_chunk_id": ch.chunk_id, "verification": _ver(ok), "created_at": now_iso(), "updated_at": now_iso()})
                self._rel_history(rid, ch, ctx.run_id, rel_type, r, "отношение обнаружено")
                ctx.add_evidence("relationship", rid, r.quote)
                if not ok:
                    ctx.stats.issues.append("отношение без подтверждающей цитаты сохранено как unverified")
                continue
            if not ok:
                ctx.stats.issues.append("изменение отношения не принято: цитата не подтверждена")
                continue
            rid = existing["id"]
            changed = rel_type != existing["rel_type"] or (r.state and norm_key(r.state) != norm_key(existing["state"]))
            updates = {"last_chunk_id": ch.chunk_id, "updated_at": now_iso()}
            if existing["verification"] != "verified":
                updates["verification"] = "verified"
            if changed:
                updates.update({"rel_type": rel_type, "state": r.state or existing["state"],
                                "description": r.description or existing["description"]})
                self._rel_history(rid, ch, ctx.run_id, rel_type, r, r.change_note or "отношение изменилось")
                self.db.insert("change_log", {
                    "series_id": ch.series_id, "owner_type": "relationship", "owner_id": rid, "field": "rel_type/state",
                    "old_value": f"{existing['rel_type']}/{existing['state']}", "new_value": f"{rel_type}/{r.state}",
                    "reason": r.change_note, "chunk_id": ch.chunk_id, "run_id": ctx.run_id, "created_at": now_iso()})
            self.db.update("relationships", rid, updates)
            ctx.add_evidence("relationship", rid, r.quote)

    def _rel_history(self, rid: str, ch: ChunkRef, run_id: str, rel_type: str, r, note: str) -> None:
        self.db.insert("relationship_history", {
            "relationship_id": rid, "chunk_id": ch.chunk_id, "run_id": run_id, "rel_type": rel_type, "state": r.state,
            "description": r.description, "note": note, "created_at": now_iso()})

    # ---- сюжетные линии ----
    def _threads(self, ctx: ChunkContext, deep: DeepAnalysis) -> None:
        ch = ctx.chunk
        chunk_events = self.db.query("SELECT id, description FROM events WHERE chunk_id = ?", (ch.chunk_id,))
        for t in deep.plot_threads:
            if not t.title.strip():
                ctx.stats.skipped += 1
                continue
            key = norm_key(t.title)
            ok = ctx.verifier.check(t.quote).verified
            existing = self.db.one("SELECT * FROM plot_threads WHERE series_id = ? AND title_norm = ?", (ch.series_id, key))
            if existing is None:
                tid = new_id()
                self.db.insert("plot_threads", {
                    "id": tid, "series_id": ch.series_id, "title": t.title.strip(), "title_norm": key,
                    "description": t.description, "status": t.status.value, "first_chunk_id": ch.chunk_id,
                    "last_chunk_id": ch.chunk_id, "verification": _ver(ok), "created_at": now_iso()})
                if ok:
                    self.search.index("thread", tid, ch.series_id, ch.book_id, ch.chapter_id, t.title, t.description)
            else:
                tid = existing["id"]
                if not ok:
                    ctx.stats.issues.append("обновление сюжетной линии не принято: цитата не подтверждена")
                    continue
                upd = {"last_chunk_id": ch.chunk_id, "verification": "verified"}
                if t.description and not existing["description"]:
                    upd["description"] = t.description
                if t.status.value != existing["status"]:
                    upd["status"] = t.status.value
                    self.db.insert("change_log", {
                        "series_id": ch.series_id, "owner_type": "plot_thread", "owner_id": tid, "field": "status",
                        "old_value": existing["status"], "new_value": t.status.value, "reason": "",
                        "chunk_id": ch.chunk_id, "run_id": ctx.run_id, "created_at": now_iso()})
                self.db.update("plot_threads", tid, upd)
            ctx.add_evidence("thread", tid, t.quote)
            for wanted in t.events:
                best = max(chunk_events, key=lambda e: jaccard(wanted, e["description"]), default=None)
                if best is not None and jaccard(wanted, best["description"]) >= EVENT_LINK_MATCH:
                    self.db.insert("plot_thread_events", {"thread_id": tid, "event_id": best["id"]}, or_ignore=True)

    # ---- загадки, секреты, предсказания ----
    def _questions(self, ctx: ChunkContext, deep: DeepAnalysis) -> None:
        ch = ctx.chunk
        for q in deep.questions:
            if not q.text.strip():
                ctx.stats.skipped += 1
                continue
            ok = ctx.verifier.check(q.quote).verified
            existing = self._match_question(ch.series_id, q.text)
            if existing is None:
                qid = new_id()
                resolved = q.status == QuestionStatus.RESOLVED
                self.db.insert("open_questions", {
                    "id": qid, "series_id": ch.series_id, "book_id": ch.book_id, "kind": q.kind.value, "text": q.text.strip(),
                    "text_norm": norm_key(q.text), "status": q.status.value, "raised_chunk_id": ch.chunk_id,
                    "clues_json": json.dumps(q.clues, ensure_ascii=False),
                    "hypotheses_json": json.dumps(q.hypotheses, ensure_ascii=False), "resolution": q.resolution,
                    "resolved_chunk_id": ch.chunk_id if resolved else None, "verification": _ver(ok),
                    "created_run_id": ctx.run_id, "created_at": now_iso(), "updated_at": now_iso()})
                ctx.add_evidence("question", qid, q.quote)
                if ok:
                    self.search.index("question", qid, ch.series_id, ch.book_id, ch.chapter_id, q.kind.value,
                                      f"{q.text} {q.resolution}")
                else:
                    ctx.stats.issues.append("загадка без подтверждающей цитаты сохранена как unverified")
                continue
            if not ok:
                ctx.stats.issues.append("обновление загадки не принято: цитата не подтверждена")
                continue
            qid = existing["id"]
            clues = list(dict.fromkeys([*json.loads(existing["clues_json"]), *q.clues]))
            hyps = list(dict.fromkeys([*json.loads(existing["hypotheses_json"]), *q.hypotheses]))
            upd: dict = {"clues_json": json.dumps(clues, ensure_ascii=False),
                         "hypotheses_json": json.dumps(hyps, ensure_ascii=False), "updated_at": now_iso(),
                         "verification": "verified"}
            order = {"open": 0, "partial": 1, "resolved": 2}
            if order[q.status.value] > order[existing["status"]]:
                upd.update({"status": q.status.value, "resolution": q.resolution or existing["resolution"],
                            "resolved_chunk_id": ch.chunk_id if q.status == QuestionStatus.RESOLVED else existing["resolved_chunk_id"]})
                self.db.insert("change_log", {
                    "series_id": ch.series_id, "owner_type": "question", "owner_id": qid, "field": "status",
                    "old_value": existing["status"], "new_value": q.status.value, "reason": q.resolution,
                    "chunk_id": ch.chunk_id, "run_id": ctx.run_id, "created_at": now_iso()})
            self.db.update("open_questions", qid, upd)
            ctx.add_evidence("question", qid, q.quote)
            self.search.index("question", qid, ch.series_id, ch.book_id, ch.chapter_id, existing["kind"],
                              f"{existing['text']} {upd.get('resolution', existing['resolution'])}")

    def _match_question(self, series_id: str, text: str):
        best, best_score = None, 0.0
        for r in self.db.query("SELECT * FROM open_questions WHERE series_id = ?", (series_id,)):
            score = jaccard(text, r["text"])
            if score > best_score:
                best, best_score = r, score
        return best if best_score >= QUESTION_MATCH else None

    # ---- мотивы ----
    def _motifs(self, ctx: ChunkContext, deep: DeepAnalysis) -> None:
        ch = ctx.chunk
        for m in deep.motifs:
            if not m.name.strip():
                ctx.stats.skipped += 1
                continue
            key = norm_key(m.name)
            ok = ctx.verifier.check(m.quote).verified
            existing = self.db.one("SELECT * FROM motifs WHERE series_id = ? AND name_norm = ?", (ch.series_id, key))
            if existing is None:
                mid = new_id()
                self.db.insert("motifs", {
                    "id": mid, "series_id": ch.series_id, "name": m.name.strip(), "name_norm": key, "kind": m.kind.value,
                    "description": m.description, "verification": _ver(ok), "first_chunk_id": ch.chunk_id, "created_at": now_iso()})
            else:
                mid = existing["id"]
                if ok and m.kind == MotifKind.DIRECT_EXPLANATION and existing["kind"] != MotifKind.DIRECT_EXPLANATION.value:
                    self.db.update("motifs", mid, {"kind": m.kind.value, "verification": "verified"})
                elif ok and existing["verification"] != "verified":
                    self.db.update("motifs", mid, {"verification": "verified"})
            if ok:
                self.db.insert("motif_occurrences", {"motif_id": mid, "chunk_id": ch.chunk_id, "run_id": ctx.run_id,
                                                     "note": m.description[:200]}, or_ignore=True)
                self.search.index("motif", mid, ch.series_id, ch.book_id, ch.chapter_id, m.name, m.description)
            else:
                ctx.stats.issues.append("мотив без подтверждающей цитаты: эпизод не засчитан")
            ctx.add_evidence("motif", mid, m.quote)

    def _name(self, entity_id: str) -> str:
        return self.db.scalar("SELECT name FROM entities WHERE id = ?", (entity_id,))
