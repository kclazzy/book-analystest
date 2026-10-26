"""Детерминированные отчёты по книге: собираются из проверенных данных без вызова модели; версии сохраняются."""
from __future__ import annotations

from bookmind.services.knowledge import KnowledgeService
from bookmind.storage.database import Database, new_id, now_iso


class ReportService:
    def __init__(self, db: Database):
        self.db = db
        self.ks = KnowledgeService(db)

    def build_book_report(self, book_id: str, save: bool = True) -> str:
        book = self.db.one("SELECT * FROM books WHERE id = ?", (book_id,))
        if not book:
            raise ValueError("Книга не найдена")
        sid = book["series_id"]
        out = [f"# {book['title']}", ""]
        arcs = self.db.query("SELECT title, content FROM report_versions WHERE book_id=? AND kind='arc_summary' "
                             "ORDER BY title, version DESC", (book_id,))
        seen = set()
        out.append("## Краткое содержание")
        for a in arcs:
            if a["title"] in seen:
                continue
            seen.add(a["title"])
            out += [f"**{a['title']}.** {a['content']}", ""]
        if not seen:
            out += ["Сводки по аркам ещё не созданы.", ""]
        out.append("## Персонажи")
        for e in self.ks.entities(sid, "character"):
            out.append(f"- **{e['name']}**" + (f" — {e['description']}" if e.get("description") else ""))
        out += ["", "## Цели"]
        for g in self.ks.goals(sid):
            out.append(f"- {g['entity_name']}: {g['description']} _({g['status']})_")
        out += ["", "## Отношения"]
        for r in self.ks.all_relationships(sid):
            out.append(f"- {r.get('a_name', '')} — {r.get('b_name', '')}: {r['rel_type']}")
        out += ["", "## Открытые вопросы"]
        for q in self.ks.questions(sid, status="open"):
            out.append(f"- {q['text']}")
        out += ["", "## Сюжетные линии"]
        for t in self.ks.plot_threads(sid):
            out.append(f"- {t['title']} _({t['status']})_")
        text = "\n".join(out).strip() + "\n"
        if save:
            ver = (self.db.scalar("SELECT MAX(version) FROM report_versions WHERE book_id=? AND kind='book_report'",
                                  (book_id,)) or 0) + 1
            self.db.insert("report_versions", {
                "id": new_id(), "series_id": sid, "book_id": book_id, "scope_id": book_id, "kind": "book_report",
                "title": f"Отчёт: {book['title']}", "content": text, "structured_json": "{}", "version": ver,
                "created_at": now_iso()})
        return text
