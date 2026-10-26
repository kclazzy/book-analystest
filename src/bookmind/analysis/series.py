"""Память серии: противоречия между фактами разных мест/томов, пересмотр открытых загадок, состояние героев по томам.
Модель только сопоставляет уже подтверждённые цитатами утверждения; новых фактов этот слой не создаёт."""
from __future__ import annotations

import json
from typing import Callable

from bookmind.analysis.context import text_stems
from bookmind.analysis.extractor import StructuredCaller
from bookmind.analysis.verification import QuoteVerifier
from bookmind.domain.schemas import ContradictionJudgement, QuestionReview
from bookmind.ingestion.text_utils import jaccard
from bookmind.llm.prompts import PromptStore
from bookmind.services.knowledge import KnowledgeService
from bookmind.services.search import SearchIndex, query_terms
from bookmind.storage.database import Database, now_iso

# категории, в которых значение обычно постоянно; действия, знания и состояние меняются естественно
STABLE_CATEGORIES = ("appearance", "age", "biography", "skill", "weakness", "personality")
SAME_STATEMENT = 0.85
MAX_PAIRS_PER_ENTITY = 30
REVIEW_CHUNKS = 2
REAL_KINDS = ("contradiction", "retcon", "deception", "change_of_circumstances")

Task = tuple[str, list[str], Callable[[], list[str]]]


class SeriesMemory:
    def __init__(self, db: Database, caller: StructuredCaller, prompts: PromptStore):
        self.db, self.caller, self.prompts = db, caller, prompts
        self.ks = KnowledgeService(db)
        self.search = SearchIndex(db)

    # ---- какие задачи нужны ----
    def entities_with_pairs(self, series_id: str) -> list[str]:
        return [r["entity_id"] for r in self.db.query(
            f"SELECT entity_id FROM facts WHERE series_id = ? AND verification = 'verified' "
            f"AND category IN ({','.join('?' * len(STABLE_CATEGORIES))}) AND entity_id IS NOT NULL "
            f"GROUP BY entity_id HAVING COUNT(*) > 1 ORDER BY entity_id", (series_id, *STABLE_CATEGORIES))]

    def questions_to_review(self, book_id: str) -> list[str]:
        """Не раскрытые вопросы предыдущих томов (по порядку серии)."""
        book = self.db.one("SELECT series_id, series_order FROM books WHERE id = ?", (book_id,))
        return [r["id"] for r in self.db.query(
            "SELECT q.id FROM open_questions q JOIN books b ON b.id = q.book_id WHERE q.series_id = ? AND b.series_order < ? "
            "AND q.status != 'resolved' AND q.verification = 'verified' ORDER BY q.created_at, q.rowid",
            (book["series_id"], book["series_order"]))]

    # ---- противоречия ----
    def candidate_pairs(self, entity_id: str) -> list[tuple]:
        facts = self.db.query(
            f"SELECT f.*, b.series_order, ch.idx AS chapter_idx, c.idx AS chunk_idx, b.title AS book_title, ch.title AS chapter_title "
            f"FROM facts f JOIN books b ON b.id = f.book_id JOIN chapters ch ON ch.id = f.chapter_id JOIN chunks c ON c.id = f.chunk_id "
            f"WHERE f.entity_id = ? AND f.verification = 'verified' AND f.category IN ({','.join('?' * len(STABLE_CATEGORIES))}) "
            f"ORDER BY b.series_order, ch.idx, c.idx, f.rowid", (entity_id, *STABLE_CATEGORIES))
        judged = {(r["fact_a"], r["fact_b"]) for r in self.db.query(
            "SELECT fact_a, fact_b FROM contradictions WHERE entity_id = ?", (entity_id,))}
        pairs = []
        for i, a in enumerate(facts):
            for b in facts[i + 1:]:
                if a["category"] != b["category"] or a["chunk_id"] == b["chunk_id"]:
                    continue
                if jaccard(a["statement"], b["statement"]) >= SAME_STATEMENT:
                    continue
                if (a["id"], b["id"]) in judged or (b["id"], a["id"]) in judged:
                    continue
                pairs.append((a, b))
        return pairs[:MAX_PAIRS_PER_ENTITY]

    def _where(self, f) -> str:
        return f"{f['book_title']}, {f['chapter_title']}"

    def _quote(self, fact_id: str) -> str:
        s = self.ks.sources_for("fact", fact_id)
        return s[0]["quote"] if s else ""

    def contradiction_task(self, entity_id: str, model: str) -> Task:
        ent = self.db.one("SELECT name, series_id FROM entities WHERE id = ?", (entity_id,))
        raws, issues, results = [], [], []
        for a, b in self.candidate_pairs(entity_id):
            prompt = self.prompts.render(
                "contradiction_check", entity=ent["name"], category=a["category"],
                where_a=self._where(a), statement_a=a["statement"], quote_a=self._quote(a["id"]),
                where_b=self._where(b), statement_b=b["statement"], quote_b=self._quote(b["id"]))
            obj, raw, iss = self.caller.call(model, prompt, ContradictionJudgement)
            raws.append(raw)
            issues += iss
            results.append((a, b, obj))

        def apply() -> list[str]:
            found = 0
            for a, b, j in results:
                fa, fb = a["id"], b["id"]   # a — более раннее место текста
                kind = j.relation.value
                self.db.insert("contradictions", {
                    "series_id": ent["series_id"], "entity_id": entity_id, "category": a["category"], "fact_a": fa,
                    "fact_b": fb, "kind": kind, "explanation": j.explanation,
                    "status": "pending" if kind in REAL_KINDS else "dismissed", "model": model,
                    "created_at": now_iso(), "id": f"{fa}:{fb}"}, or_ignore=True)
                found += kind in REAL_KINDS
            return [f"проверено пар: {len(results)}, найдено расхождений: {found}"] if results else []

        return "\n".join(raws), issues, apply

    # ---- пересмотр загадок ----
    def question_task(self, question_id: str, book_id: str, model: str) -> Task:
        q = self.db.one("SELECT * FROM open_questions WHERE id = ?", (question_id,))
        if q is None or q["status"] == "resolved":
            return "", ["вопрос уже раскрыт"], lambda: []
        chunks = self._relevant_chunks(q["text"], book_id)
        if not chunks:
            return "", ["в новом томе нет релевантных фрагментов"], lambda: []
        text = "\n\n".join(c["text"] for c in chunks)
        prompt = self.prompts.render("question_review", question=q["text"],
                                     clues="; ".join(json.loads(q["clues_json"])) or "(нет)", text=text)
        obj, raw, issues = self.caller.call(model, prompt, QuestionReview)

        def apply() -> list[str]:
            if obj.status.value == "open":
                return []
            check = None
            for c in chunks:
                r = QuoteVerifier(c["text"]).check(obj.quote)
                if r.verified:
                    check, chunk = r, c
                    break
            if check is None:
                return ["пересмотр загадки отклонён: цитата не найдена в тексте нового тома"]
            order = {"open": 0, "partial": 1, "resolved": 2}
            cur = self.db.one("SELECT status FROM open_questions WHERE id = ?", (question_id,))["status"]
            if order[obj.status.value] <= order[cur]:
                return []
            clues = list(dict.fromkeys([*json.loads(q["clues_json"]), *obj.new_clues]))
            self.db.update("open_questions", question_id, {
                "status": obj.status.value, "resolution": obj.resolution or q["resolution"],
                "resolved_chunk_id": chunk["id"] if obj.status.value == "resolved" else q["resolved_chunk_id"],
                "clues_json": json.dumps(clues, ensure_ascii=False), "updated_at": now_iso()})
            self.db.insert("evidence", {
                "owner_type": "question", "owner_id": question_id, "book_id": chunk["book_id"], "chapter_id": chunk["chapter_id"],
                "chunk_id": chunk["id"], "quote": obj.quote.strip()[:600], "start_char": check.start, "end_char": check.end,
                "verified": 1, "run_id": None}, or_ignore=True)
            self.db.insert("change_log", {
                "series_id": q["series_id"], "owner_type": "question", "owner_id": question_id, "field": "status",
                "old_value": cur, "new_value": obj.status.value, "reason": obj.resolution, "chunk_id": chunk["id"],
                "run_id": None, "created_at": now_iso()})
            self.db.insert("change_log", {
                "series_id": q["series_id"], "owner_type": "question", "owner_id": question_id, "field": "resolution",
                "old_value": q["resolution"], "new_value": obj.resolution, "reason": "", "chunk_id": chunk["id"],
                "run_id": None, "created_at": now_iso()})
            return []

        return raw, issues, apply

    def _relevant_chunks(self, text: str, book_id: str) -> list:
        terms = set(query_terms(text))
        if not terms:
            return []
        hits = self.search.search(text, book_ids=[book_id], kinds=["chunk"], limit=REVIEW_CHUNKS * 3)
        out = []
        for h in hits:
            row = self.db.one("SELECT id, book_id, chapter_id, text FROM chunks WHERE id = ?", (h.ref_id,))
            if row and len(terms & {t for t in query_terms(row["text"])}) >= 2:
                out.append(row)
            if len(out) >= REVIEW_CHUNKS:
                break
        return out

    # ---- состояние героев по томам ----
    def snapshot_book(self, book_id: str) -> int:
        book = self.db.one("SELECT * FROM books WHERE id = ?", (book_id,))
        order, sid = book["series_order"], book["series_id"]
        ids = [r[0] for r in self.db.query(
            "SELECT DISTINCT m.entity_id FROM entity_mentions m JOIN chunks c ON c.id = m.chunk_id "
            "JOIN entities e ON e.id = m.entity_id WHERE c.book_id = ? AND e.type = 'character' AND e.status != 'merged'", (book_id,))]
        n = 0
        with self.db.transaction():
            self.db.execute("DELETE FROM entity_states WHERE book_id = ?", (book_id,))
            for eid in ids:
                d = self.ks.character_dossier(eid, up_to_book_order=order)
                snap = {
                    "status": d["current_state"],
                    "goals": [{"description": g["description"], "status": g["status"]} for g in d["goals"]],
                    "relationships": [{"with": r["other_name"], "type": r["rel_type"], "state": r["state"]} for r in d["relationships"]],
                    "facts": {c: [f["statement"] for f in fs] for c, fs in d["facts"].items()},
                }
                self.db.insert("entity_states", {
                    "entity_id": eid, "book_id": book_id, "summary": d["current_state"] or "",
                    "snapshot_json": json.dumps(snap, ensure_ascii=False), "created_at": now_iso()})
                n += 1
        return n
