"""Экспорт базы знаний серии: Markdown, JSON, CSV, HTML. Экспортируются только подтверждённые данные.
up_to_book_order ограничивает экспорт томами до указанного (без спойлеров)."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Any

from bookmind.services.knowledge import KnowledgeService
from bookmind.storage.database import Database, now_iso

CSV_FILES = ("characters", "goals", "relationships", "events", "questions", "threads", "motifs", "contradictions")


def _csv_safe(v: Any) -> str:
    """Защита от формул в Excel: ячейки, начинающиеся с = + - @, получают префикс-апостроф."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


class ExportService:
    def __init__(self, db: Database):
        self.db = db
        self.ks = KnowledgeService(db)

    # ---- сбор данных ----
    def collect(self, series_id: str, up_to_book_order: int | None = None) -> dict[str, Any]:
        s = self.db.one("SELECT * FROM series WHERE id = ?", (series_id,))
        if s is None:
            raise ValueError("Серия не найдена")
        lim = up_to_book_order
        books = [dict(r) for r in self.db.query(
            "SELECT id, title, author, series_order FROM books WHERE series_id = ? AND series_order <= ? ORDER BY series_order",
            (series_id, lim if lim is not None else 10 ** 6))]
        chars = []
        for e in self.ks.entities(series_id, "character"):
            d = self.ks.character_dossier(e["id"], up_to_book_order=lim)
            if d["first_appearance"] is None:
                continue
            chars.append(d)
        others = [e for e in self.ks.entities(series_id) if e["type"] != "character"]
        big = 10 ** 6
        cap = lim if lim is not None else big
        order_of = lambda cid: (self.ks.chunk_location(cid) or {}).get("series_order", 0) if cid else 0
        qs = [q for q in self.ks.questions(series_id) if (q["raised_at"] or {}).get("series_order", 0) <= cap]
        for q in qs:     # разгадка из более позднего тома скрывается
            if q["resolved_at"] and q["resolved_at"]["series_order"] > cap:
                q.update(status="open", resolution="", resolved_at=None)
        return {
            "exported_at": now_iso(), "series": s["title"], "up_to_book_order": lim, "books": books,
            "characters": chars,
            "entities": [{"name": e["name"], "type": e["type"], "description": e["description"]} for e in others],
            "goals": self.ks.goals(series_id, up_to_book_order=lim),
            "relationships": [r for e in chars for r in e["relationships"] if r["id"]],
            "events": [e for e in self.ks.chronology(series_id) if e["series_order"] <= cap],
            "questions": qs,
            "threads": [t for t in self.ks.plot_threads(series_id) if order_of(t["first_chunk_id"]) <= cap],
            "motifs": [m for m in self.ks.motifs(series_id) if order_of(m["first_chunk_id"]) <= cap],
            "contradictions": [c for c in self.ks.contradictions(series_id, status=None)
                               if max(c["a"]["series_order"], c["b"]["series_order"]) <= cap],
        }

    # ---- JSON ----
    def export_json(self, series_id: str, path: Path, up_to_book_order: int | None = None) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.collect(series_id, up_to_book_order), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    # ---- CSV ----
    def export_csv(self, series_id: str, folder: Path, up_to_book_order: int | None = None) -> list[Path]:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        d = self.collect(series_id, up_to_book_order)
        src = lambda x: "; ".join(f"{s['book_title']}, {s['chapter_title']}: «{s['quote']}»" for s in x.get("sources", [])[:3])
        tables: dict[str, tuple[list[str], list[list[Any]]]] = {
            "characters": (["Имя", "Псевдонимы", "Первое появление", "Состояние"],
                           [[c["name"], ", ".join(c["aliases"]), (c["first_appearance"] or {}).get("chapter_title", ""),
                             c["current_state"] or ""] for c in d["characters"]]),
            "goals": (["Персонаж", "Цель", "Статус", "Тип", "Мотивация", "Источники"],
                      [[g["entity_name"], g["description"], g["status"], g["goal_type"], g.get("motivation", ""), src(g)] for g in d["goals"]]),
            "relationships": (["Персонаж", "С кем", "Тип", "Состояние", "Источники"],
                              [[c["name"], r["other_name"], r["rel_type"], r["state"], src(r)]
                               for c in d["characters"] for r in c["relationships"]]),
            "events": (["Книга", "Глава", "Событие", "Режим", "Время", "Участники"],
                       [[e.get("book_title", ""), e.get("chapter_title", ""), e["description"], e["narrative_mode"],
                         e.get("story_time", ""), ", ".join(p["name"] if isinstance(p, dict) else str(p) for p in e.get("participants", []))]
                        for e in d["events"]]),
            "questions": (["Тип", "Вопрос", "Статус", "Разгадка", "Источники"],
                          [[q["kind"], q["text"], q["status"], q["resolution"], src(q)] for q in d["questions"]]),
            "threads": (["Линия", "Статус", "Описание"], [[t["title"], t["status"], t.get("description", "")] for t in d["threads"]]),
            "motifs": (["Мотив", "Тип", "Описание", "Эпизодов"],
                       [[m["name"], m["kind"], m["description"], len(m["occurrences"])] for m in d["motifs"]]),
            "contradictions": (["Персонаж", "Категория", "Вид", "Утверждение A", "Утверждение B", "Пояснение", "Статус"],
                               [[c["entity_name"], c["category"], c["kind_label"], c["a"]["statement"], c["b"]["statement"],
                                 c["explanation"], c["status"]] for c in d["contradictions"]]),
        }
        out = []
        for name, (head, rows) in tables.items():
            p = folder / f"{name}.csv"
            with p.open("w", encoding="utf-8-sig", newline="") as f:   # BOM: Excel корректно читает кириллицу
                w = csv.writer(f, delimiter=";")
                w.writerow(head)
                for r in rows:
                    w.writerow([_csv_safe(c) for c in r])
            out.append(p)
        return out

    # ---- Markdown / HTML ----
    def _markdown(self, d: dict[str, Any]) -> str:
        L = [f"# {d['series']}", "", f"_Экспорт: {d['exported_at']}. Только подтверждённые цитатами данные._", ""]
        if d["up_to_book_order"] is not None:
            L += [f"> Содержимое ограничено томами до №{d['up_to_book_order']} включительно (без спойлеров).", ""]
        L += ["## Книги", *[f"{b['series_order']}. {b['title']}" for b in d["books"]], "", "## Персонажи", ""]
        for c in d["characters"]:
            L.append(f"### {c['name']}")
            if c["aliases"]:
                L.append(f"Также: {', '.join(c['aliases'])}")
            if c["description"]:
                L.append(c["description"])
            for cat, fs in c["facts"].items():
                for f in fs:
                    L.append(f"- ({cat}) {f['statement']} — _{f['kind_label']}_")
            for g in c["goals"]:
                L.append(f"- Цель: {g['description']} [{g['status']}]")
            for r in c["relationships"]:
                L.append(f"- {r['other_name']}: {r['rel_type']}")
            L.append("")
        L += ["## Хронология", *[f"- {e['description']}" for e in d["events"]], "", "## Загадки"]
        for q in d["questions"]:
            L.append(f"- [{q['status']}] {q['text']}" + (f" → {q['resolution']}" if q["resolution"] else ""))
        L += ["", "## Сюжетные линии", *[f"- {t['title']} ({t['status']})" for t in d["threads"]], "", "## Противоречия"]
        for c in d["contradictions"]:
            if c["status"] != "dismissed":
                L.append(f"- {c['entity_name']}: «{c['a']['statement']}» ↔ «{c['b']['statement']}» ({c['kind_label']})")
        return "\n".join(L).rstrip() + "\n"

    def export_markdown(self, series_id: str, path: Path, up_to_book_order: int | None = None) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._markdown(self.collect(series_id, up_to_book_order)), encoding="utf-8")
        return path

    def export_html(self, series_id: str, path: Path, up_to_book_order: int | None = None) -> Path:
        d = self.collect(series_id, up_to_book_order)
        out, in_list = [], False
        for line in self._markdown(d).splitlines():
            e = html.escape(line)
            if line.startswith("- ") or line[:2].rstrip(".").isdigit() and ". " in line[:4]:
                if not in_list:
                    out.append("<ul>"); in_list = True
                out.append(f"<li>{e[2:] if line.startswith('- ') else e}</li>")
                continue
            if in_list:
                out.append("</ul>"); in_list = False
            if line.startswith("### "):
                out.append(f"<h3>{html.escape(line[4:])}</h3>")
            elif line.startswith("## "):
                out.append(f"<h2>{html.escape(line[3:])}</h2>")
            elif line.startswith("# "):
                out.append(f"<h1>{html.escape(line[2:])}</h1>")
            elif line.strip():
                out.append(f"<p>{e}</p>")
        if in_list:
            out.append("</ul>")
        page = ("<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\"><title>" + html.escape(d["series"])
                + "</title><style>body{font-family:system-ui,sans-serif;max-width:50rem;margin:2rem auto;padding:0 1rem;line-height:1.5}"
                "h2{border-bottom:1px solid #ccc;padding-bottom:.2rem}</style></head><body>\n" + "\n".join(out) + "\n</body></html>\n")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page, encoding="utf-8")
        return path
