"""Вопросы к базе знаний: поиск → материалы с метками [F1]/[C2] → ответ модели → проверка ссылок.
Если материалов нет или ответ не опирается на них, модель не вызывается / ответ отклоняется."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bookmind.analysis.extractor import StructuredCaller
from bookmind.domain.schemas import AnswerDraft
from bookmind.llm.prompts import PromptStore
from bookmind.services.knowledge import KnowledgeService
from bookmind.services.search import SearchIndex
from bookmind.storage.database import Database

KNOWLEDGE_KINDS = ("fact", "event", "entity", "question", "motif")
LABEL = re.compile(r"\[([FC]\d+)\]")
INSUFFICIENT_TEXT = "В загруженных материалах недостаточно данных для ответа на этот вопрос."


@dataclass
class Citation:
    label: str
    kind: str
    text: str
    book_title: str = ""
    chapter_title: str = ""
    quote: str = ""


@dataclass
class Answer:
    question: str
    answer: str
    insufficient: bool
    citations: list[Citation] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    llm_called: bool = False


class QAService:
    def __init__(self, db: Database, caller: StructuredCaller, prompts: PromptStore | None = None):
        self.db, self.caller = db, caller
        self.prompts = prompts or PromptStore(None)
        self.search = SearchIndex(db)
        self.ks = KnowledgeService(db)

    # ---- материалы ----
    def _book_ids(self, series_id: str, up_to_book_order: int | None, book_id: str | None) -> list[str]:
        if book_id:
            return [book_id]
        sql, p = "SELECT id FROM books WHERE series_id = ?", [series_id]
        if up_to_book_order is not None:           # защита от спойлеров: тома после указанного не используются
            sql += " AND series_order <= ?"
            p.append(up_to_book_order)
        return [r["id"] for r in self.db.query(sql, p)]

    def gather(self, question: str, series_id: str, up_to_book_order: int | None = None, book_id: str | None = None,
               max_knowledge: int = 8, max_chunks: int = 3) -> list[Citation]:
        books = self._book_ids(series_id, up_to_book_order, book_id)
        if not books:
            return []
        out: list[Citation] = []
        n_f = n_c = 0
        for kind, limit in (("knowledge", max_knowledge), ("chunk", max_chunks)):
            kinds = KNOWLEDGE_KINDS if kind == "knowledge" else ("chunk",)
            hits = self.search.search(question, series_id=series_id, book_ids=books, kinds=kinds, limit=limit * 2)
            for h in hits:
                c = self._citation(h.kind, h.ref_id, "F" if kind == "knowledge" else "C", n_f + 1 if kind == "knowledge" else n_c + 1)
                if c is None:
                    continue
                out.append(c)
                if kind == "knowledge":
                    n_f += 1
                else:
                    n_c += 1
                if (n_f if kind == "knowledge" else n_c) >= limit:
                    break
        return out

    def _citation(self, kind: str, ref_id: str, prefix: str, n: int) -> Citation | None:
        label = f"{prefix}{n}"
        if kind == "chunk":
            loc = self.ks.chunk_location(ref_id)
            row = self.db.one("SELECT text FROM chunks WHERE id = ?", (ref_id,))
            if not loc or not row:
                return None
            return Citation(label, "chunk", row["text"][:900], loc["book_title"], loc["chapter_title"], "")
        if kind == "fact":
            f = self.db.one("SELECT statement, entity_id FROM facts WHERE id = ? AND verification = 'verified'", (ref_id,))
            text = f["statement"] if f else None
        elif kind == "event":
            f = self.db.one("SELECT description FROM events WHERE id = ? AND verification = 'verified'", (ref_id,))
            text = f["description"] if f else None
        elif kind == "question":
            f = self.db.one("SELECT text, resolution, status FROM open_questions WHERE id = ? AND verification = 'verified'", (ref_id,))
            text = (f"Загадка: {f['text']} (статус: {f['status']})" + (f". Разгадка: {f['resolution']}" if f["resolution"] else "")) if f else None
        elif kind == "motif":
            f = self.db.one("SELECT name, description FROM motifs WHERE id = ? AND verification = 'verified'", (ref_id,))
            text = f"Мотив «{f['name']}»: {f['description']}" if f else None
        else:
            f = self.db.one("SELECT name, description FROM entities WHERE id = ? AND status != 'merged'", (ref_id,))
            text = f"{f['name']}: {f['description']}" if f else None
        if not text:
            return None
        srcs = self.ks.sources_for(kind, ref_id) if kind != "entity" else self.ks.sources_for("entity", ref_id)
        if not srcs and kind != "entity":
            return None
        s = srcs[0] if srcs else {}
        return Citation(label, kind, text, s.get("book_title", ""), s.get("chapter_title", ""), s.get("quote", ""))

    # ---- ответ ----
    def ask(self, question: str, series_id: str, model: str, up_to_book_order: int | None = None,
            book_id: str | None = None) -> Answer:
        question = question.strip()
        mats = self.gather(question, series_id, up_to_book_order, book_id)
        if not question or not mats:
            return Answer(question, INSUFFICIENT_TEXT, True)       # без материалов модель не вызывается
        block = "\n".join(f"[{m.label}] {m.text}" + (f"\n    Цитата: «{m.quote}»" if m.quote else "")
                          + (f"\n    Место: {m.book_title}, {m.chapter_title}" if m.book_title else "") for m in mats)
        prompt = self.prompts.render("answer", question=question, materials=block)
        draft, _raw, issues = self.caller.call(model, prompt, AnswerDraft)
        known = {m.label: m for m in mats}
        used = [l for l in LABEL.findall(draft.answer)] + [s.strip("[] ") for s in draft.sources]
        valid = [l for l in dict.fromkeys(used) if l in known]
        bad = [l for l in dict.fromkeys(LABEL.findall(draft.answer)) if l not in known]
        text = draft.answer
        for l in bad:
            text = text.replace(f"[{l}]", "")
            issues.append(f"ссылка [{l}] отсутствует в материалах и удалена")
        if draft.insufficient or not valid or not text.strip():
            if not draft.insufficient and not valid:
                issues.append("ответ без ссылок на материалы отклонён")
            return Answer(question, INSUFFICIENT_TEXT if not draft.insufficient else (text.strip() or INSUFFICIENT_TEXT),
                          True, [], issues, True)
        return Answer(question, re.sub(r"[ \t]{2,}", " ", text).strip(), False, [known[l] for l in valid], issues, True)
