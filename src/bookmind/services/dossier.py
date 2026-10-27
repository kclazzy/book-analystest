"""Досье для нейросети: вся подтверждённая аналитика серии в одном компактном тексте (Markdown) или JSON.
Подходит, чтобы вставить в чат или в контекст другой модели. Длинное досье можно разрезать на части по границам разделов."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from bookmind.services.exports import ExportService
from bookmind.storage.database import Database, now_iso

CHARS_PER_TOKEN = 2.7        # для русского текста; оценка грубая

PREAMBLE = ("Это досье по книгам, собранное программой BookMind. Все факты подтверждены цитатами из текста, "
            "непроверенные данные не включены. Используй его как источник правды о сюжете, героях и мире; "
            "если в досье чего-то нет, не выдумывай, а так и скажи.")


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


class DossierService:
    def __init__(self, db: Database):
        self.db = db
        self.exports = ExportService(db)

    # ---------- сбор ----------
    def summaries(self, series_id: str, cap: int) -> dict[str, list[dict[str, Any]]]:
        """Последние версии сводок глав и арок."""
        rows = self.db.query(
            "SELECT r.kind, r.scope_id, r.title, r.content, r.structured_json, b.title AS book_title, b.series_order, "
            "ch.title AS chapter_title, ch.idx AS chapter_idx "
            "FROM report_versions r JOIN books b ON b.id = r.book_id LEFT JOIN chapters ch ON ch.id = r.scope_id "
            "WHERE r.series_id = ? AND r.kind IN ('chapter_summary', 'arc_summary') AND b.series_order <= ? AND r.version = "
            "(SELECT MAX(version) FROM report_versions x WHERE x.series_id = r.series_id AND x.kind = r.kind AND x.scope_id = r.scope_id) "
            "ORDER BY b.series_order, ch.idx, r.title", (series_id, cap))
        out: dict[str, list[dict[str, Any]]] = {"chapters": [], "arcs": []}
        for r in rows:
            try:
                st = json.loads(r["structured_json"] or "{}")
            except ValueError:
                st = {}
            body, _, tail = r["content"].partition("Ключевые моменты:")
            body = re.sub(r"^\s*Сводка (главы|арки):\s*", "", body).strip()
            points = st.get("key_points") or [l.lstrip("- ").strip() for l in tail.split("\n") if l.strip().startswith("-")]
            item = {"book": r["book_title"], "title": r["chapter_title"] or r["title"], "summary": body,
                    "key_points": points, "open_threads": st.get("open_threads", [])}
            out["chapters" if r["kind"] == "chapter_summary" else "arcs"].append(item)
        return out

    def completeness(self, series_id: str) -> list[dict[str, Any]]:
        """Сколько задач анализа выполнено и сколько нет, по книгам: чтобы по досье было видно, где данные неполные."""
        out = []
        for b in self.db.query("SELECT id, title FROM books WHERE series_id = ? ORDER BY series_order", (series_id,)):
            counts = {r["status"]: r["n"] for r in self.db.query(
                "SELECT t.status, COUNT(*) AS n FROM analysis_tasks t JOIN analysis_runs r ON r.id = t.run_id "
                "WHERE r.book_id = ? GROUP BY t.status", (b["id"],))}
            hidden = self.db.scalar("SELECT COUNT(*) FROM facts WHERE book_id = ? AND verification != 'verified'", (b["id"],)) or 0
            total = sum(counts.values())
            out.append({"book": b["title"], "tasks_total": total, "done": counts.get("done", 0) + counts.get("skipped", 0),
                        "failed": counts.get("failed", 0), "pending": counts.get("pending", 0) + counts.get("running", 0),
                        "unverified_facts_hidden": hidden})
        return out

    def build(self, series_id: str, with_quotes: bool = True, up_to_book_order: int | None = None) -> dict[str, Any]:
        d = self.exports.collect(series_id, up_to_book_order)
        cap = up_to_book_order if up_to_book_order is not None else 10 ** 6
        d["summaries"] = self.summaries(series_id, cap)
        d["completeness"] = self.completeness(series_id)
        d["with_quotes"] = with_quotes
        if not with_quotes:
            for c in d["characters"]:
                for fs in c["facts"].values():
                    for f in fs:
                        f["sources"] = []
        return d

    # ---------- Markdown ----------
    @staticmethod
    def _quote(sources: list[dict[str, Any]], n: int = 2) -> str:
        q = [f"«{s['quote'].strip()}» ({s['book_title']}, {s['chapter_title']})" for s in sources[:n] if s.get("quote")]
        return "  \n  Цитаты: " + " | ".join(q) if q else ""

    def to_markdown(self, d: dict[str, Any]) -> str:
        wq = d["with_quotes"]
        q = (lambda x: self._quote(x.get("sources", []))) if wq else (lambda x: "")
        L = [f"# Досье: {d['series']}", "", f"_{PREAMBLE}_", ""]
        if d["up_to_book_order"] is not None:
            L += [f"> Только тома до №{d['up_to_book_order']} включительно (без спойлеров).", ""]
        L += ["## Книги", *[f"{b['series_order']}. {b['title']}" + (f" — {b['author']}" if b.get("author") else "") for b in d["books"]], ""]
        bad = [c for c in d["completeness"] if c["failed"] or c["pending"]]
        if bad:
            L += ["## Полнота данных", "Анализ этих книг завершён не полностью, часть сведений может отсутствовать:"]
            L += [f"- «{c['book']}»: выполнено {c['done']} из {c['tasks_total']} задач, не выполнено {c['failed']}, не начато {c['pending']}" for c in bad]
            L.append("")
        s = d["summaries"]
        if s["arcs"] or s["chapters"]:
            L.append("## Краткое содержание")
            if s["arcs"]:
                L.append("### Сюжетные арки")
                for a in s["arcs"]:
                    L.append(f"- **{a['book']} · {a['title']}.** {a['summary']}")
                    L += [f"  - {k}" for k in a["key_points"]]
                L.append("")
            if s["chapters"]:
                L.append("### По главам")
                cur = None
                for c in s["chapters"]:
                    if c["book"] != cur:
                        cur = c["book"]
                        L += ["", f"#### {cur}"]
                    L.append(f"- **{c['title']}.** {c['summary']}")
                    L += [f"  - {k}" for k in c["key_points"]]
                    L += [f"  - открытая линия: {k}" for k in c["open_threads"]]
                L.append("")
        L += ["## Персонажи", ""]
        for c in d["characters"]:
            head = c["name"] + (f" (также: {', '.join(c['aliases'])})" if c["aliases"] else "")
            L += [f"### {head}"]
            if c["description"]:
                L.append(c["description"])
            if c.get("current_state"):
                L.append(f"**Состояние сейчас:** {c['current_state']}")
            for cat, fs in c["facts"].items():
                for f in fs:
                    L.append(f"- [{cat}] {f['statement']} ({f['kind_label']}){q(f)}")
            for g in c["goals"]:
                bits = [x for x in (g.get("motivation"), g.get("desired_result"), g.get("obstacles")) if x]
                L.append(f"- **Цель:** {g['description']} [{g['status']}]" + (f" — {'; '.join(bits)}" if bits else "") + q(g))
                for h in g.get("history", []):
                    note = h.get("note") or h.get("change_note") or ""
                    if note:
                        L.append(f"  - изменение: {note}")
            for r in c["relationships"]:
                L.append(f"- **{r['other_name']}:** {r['rel_type']}" + (f", {r['state']}" if r.get("state") else "") + q(r))
            L.append("")
        if d["entities"]:
            L += ["## Места, организации, предметы, правила мира", ""]
            for e in d["entities"]:
                L.append(f"- **{e['name']}** ({e['type']})" + (f": {e['description']}" if e.get("description") else ""))
            L.append("")
        L += ["## Хронология событий", ""]
        for e in d["events"]:
            who = ", ".join(p["name"] if isinstance(p, dict) else str(p) for p in e.get("participants", []))
            L.append(f"- {e.get('book_title', '')} · {e.get('chapter_title', '')}: {e['description']}" + (f" (участвуют: {who})" if who else ""))
        L += ["", "## Загадки, тайны и пророчества", ""]
        for x in d["questions"]:
            L.append(f"- [{x['kind']}, {x['status']}] {x['text']}")
            if x.get("clues"):
                L.append(f"  - подсказки: {'; '.join(x['clues'])}")
            if x.get("resolution"):
                L.append(f"  - разгадка: {x['resolution']}")
        L += ["", "## Сюжетные линии", ""]
        for t in d["threads"]:
            L.append(f"- **{t['title']}** [{t['status']}]" + (f": {t['description']}" if t.get("description") else ""))
        L += ["", "## Мотивы и символы", ""]
        for m in d["motifs"]:
            L.append(f"- **{m['name']}** ({m['kind']})" + (f": {m['description']}" if m.get("description") else ""))
        live = [c for c in d["contradictions"] if c["status"] != "dismissed"]
        if live:
            L += ["", "## Противоречия в тексте", ""]
            L += [f"- {c['entity_name']}: «{c['a']['statement']}» ↔ «{c['b']['statement']}» ({c['kind_label']})"
                  + (f". {c['explanation']}" if c.get("explanation") else "") for c in live]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(L)).rstrip() + "\n"

    # ---------- разрезание и сохранение ----------
    @staticmethod
    def split_markdown(text: str, max_chars: int) -> list[str]:
        """Режет по границам заголовков (##, ###, ####); слишком длинный блок режется по строкам. Каждая часть самодостаточна."""
        if max_chars <= 0 or len(text) <= max_chars:
            return [text]
        blocks, cur = [], []
        for line in text.split("\n"):
            if re.match(r"^#{2,4} ", line) and cur:
                blocks.append("\n".join(cur))
                cur = []
            cur.append(line)
        blocks.append("\n".join(cur))
        pieces: list[str] = []
        for b in blocks:
            while len(b) > max_chars:
                cut = b.rfind("\n", 0, max_chars)
                cut = cut if cut > max_chars // 2 else max_chars
                pieces.append(b[:cut])
                b = b[cut:].lstrip("\n")
            if b.strip():
                pieces.append(b)
        parts, buf = [], ""
        for p in pieces:
            if buf and len(buf) + len(p) + 1 > max_chars:
                parts.append(buf)
                buf = ""
            buf = (buf + "\n" + p) if buf else p
        if buf:
            parts.append(buf)
        n = len(parts)
        return [f"_Часть {i} из {n}. Досье разрезано по разделам; не делай выводов о том, чего нет в этой части, пока не прочитаешь все._\n\n{p.rstrip()}\n"
                for i, p in enumerate(parts, 1)] if n > 1 else parts

    def export(self, series_id: str, path: Path, *, fmt: str = "md", max_chars: int = 0, with_quotes: bool = True,
               up_to_book_order: int | None = None) -> list[Path]:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        d = self.build(series_id, with_quotes, up_to_book_order)
        if fmt == "json":
            path.write_text(json.dumps({**d, "exported_at": now_iso(), "preamble": PREAMBLE}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            return [path]
        parts = self.split_markdown(self.to_markdown(d), max_chars)
        if len(parts) == 1:
            path.write_text(parts[0], encoding="utf-8")
            return [path]
        out = []
        for i, p in enumerate(parts, 1):
            f = path.with_name(f"{path.stem}_{i:02d}of{len(parts):02d}{path.suffix}")
            f.write_text(p, encoding="utf-8")
            out.append(f)
        return out
