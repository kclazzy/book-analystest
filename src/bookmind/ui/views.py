"""Данные для таблиц окна (без Qt): заголовки, строки и подробный текст с источниками."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from bookmind.domain.enums import label_of

TITLES = {
    "characters": "Персонажи", "goals": "Цели", "relationships": "Отношения", "timeline": "Хронология",
    "threads": "Сюжетные линии", "world": "Мир", "questions": "Загадки", "motifs": "Мотивы",
    "contradictions": "Противоречия",
}


@dataclass
class TableData:
    headers: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    details: list[str] = field(default_factory=list)


def _src(sources: list[dict], limit: int = 4) -> str:
    if not sources:
        return "Источники: нет подтверждённых цитатой"
    lines = [f"  • {s['book_title']}, {s['chapter_title']}: «{s['quote']}»" for s in sources[:limit]]
    return "Источники:\n" + "\n".join(lines)


def characters(ctx, sid: str, up_to: int | None = None) -> TableData:
    t = TableData(["Имя", "Псевдонимы", "Первое появление", "Состояние"])
    for e in ctx.knowledge.entities(sid, "character"):
        d = ctx.knowledge.character_dossier(e["id"], up_to_book_order=up_to)
        if d["first_appearance"] is None:
            continue
        t.rows.append([d["name"], ", ".join(d["aliases"]), d["first_appearance"]["chapter_title"], d["current_state"] or "неизвестно"])
        lines = [d["name"], d["description"] or ""]
        for cat, fs in d["facts"].items():
            for f in fs:
                lines.append(f"[{cat}] {f['statement']} ({f['kind_label']})\n    " + _src(f["sources"], 1).replace("\n", "\n    "))
        lines += [f"Цель: {g['description']} — {label_of(g['status'])}" for g in d["goals"]]
        lines += [f"Отношения: {r['other_name']} — {r['rel_type']}" for r in d["relationships"]]
        t.details.append("\n".join(x for x in lines if x))
    return t


def goals(ctx, sid: str, up_to: int | None = None) -> TableData:
    t = TableData(["Персонаж", "Цель", "Статус", "Тип", "Мотивация"])
    for g in ctx.knowledge.goals(sid, up_to_book_order=up_to):
        t.rows.append([g["entity_name"], g["description"], label_of(g["status"]), label_of(g["goal_type"]), g.get("motivation", "")])
        hist = "\n".join(f"  {label_of(h['old_status']) or '—'} → {label_of(h['new_status'])}: {h['note']}" for h in g["history"])
        t.details.append(f"{g['description']}\nИстория статусов:\n{hist}\n{_src(g['sources'])}")
    return t


def relationships(ctx, sid: str, up_to: int | None = None) -> TableData:
    t = TableData(["Персонаж А", "Персонаж Б", "Тип", "Состояние", "Изменений"])
    for r in ctx.knowledge.all_relationships(sid):
        t.rows.append([r["a"], r["b"], r["rel_type"], r["state"], r["changes"]])
        t.details.append(f"{r['a']} — {r['b']}: {r['rel_type']} ({r['state']})")
    return t


def timeline(ctx, sid: str, up_to: int | None = None, story_order: bool = False) -> TableData:
    t = TableData(["Книга", "Глава", "Событие", "Режим", "Время"])
    ev = ctx.knowledge.chronology(sid, order="story_estimate" if story_order else "narrative")
    for e in ev:
        if up_to is not None and e["series_order"] > up_to:
            continue
        t.rows.append([e["book_title"], e["chapter_title"], e["description"], label_of(e["narrative_mode"]), e["story_time"]])
        who = ", ".join(p["name"] for p in e["participants"])
        t.details.append(f"{e['description']}\nУчастники: {who or '—'}\n{_src(e['sources'])}")
    return t


def threads(ctx, sid: str, up_to: int | None = None) -> TableData:
    t = TableData(["Линия", "Статус", "Событий"])
    for th in ctx.knowledge.plot_threads(sid):
        t.rows.append([th["title"], label_of(th["status"]), len(th["events"])])
        t.details.append(f"{th['title']}\n{th.get('description', '')}\n" + "\n".join(f"  • {e['description']}" for e in th["events"]) + "\n" + _src(th["sources"]))
    return t


def world(ctx, sid: str, up_to: int | None = None) -> TableData:
    t = TableData(["Название", "Тип", "Описание"])
    for e in ctx.knowledge.entities(sid):
        if e["type"] == "character":
            continue
        t.rows.append([e["name"], label_of(e["type"]), e["description"]])
        t.details.append(f"{e['name']} ({label_of(e['type'])})\n{e['description']}")
    return t


def questions(ctx, sid: str, up_to: int | None = None) -> TableData:
    t = TableData(["Тип", "Вопрос", "Статус", "Разгадка"])
    for q in ctx.knowledge.questions(sid):
        if up_to is not None and (q["raised_at"] or {}).get("series_order", 0) > up_to:
            continue
        t.rows.append([label_of(q["kind"]), q["text"], label_of(q["status"]), q["resolution"]])
        t.details.append(f"{q['text']}\nПодсказки: {'; '.join(q['clues']) or '—'}\nГипотезы: {'; '.join(q['hypotheses']) or '—'}\n{_src(q['sources'])}")
    return t


def motifs(ctx, sid: str, up_to: int | None = None) -> TableData:
    t = TableData(["Мотив", "Тип", "Эпизодов", "Описание"])
    for m in ctx.knowledge.motifs(sid):
        t.rows.append([m["name"], label_of(m["kind"]), len(m["occurrences"]), m["description"]])
        t.details.append(f"{m['name']}: {m['description']}\nЭпизоды:\n" + "\n".join(f"  • {o['book_title']}, {o['chapter_title']}" for o in m["occurrences"]))
    return t


def contradictions(ctx, sid: str, up_to: int | None = None) -> TableData:
    t = TableData(["Персонаж", "Вид", "Утверждение A", "Утверждение B", "id"])
    for c in ctx.knowledge.contradictions(sid):
        t.rows.append([c["entity_name"], c["kind_label"], c["a"]["statement"], c["b"]["statement"], c["id"]])
        t.details.append(f"{c['explanation']}\n\nA ({c['a']['book_title']}):\n{_src(c['a']['sources'], 1)}\n\nB ({c['b']['book_title']}):\n{_src(c['b']['sources'], 1)}")
    return t


VIEWS: dict[str, Callable[..., TableData]] = {
    "characters": characters, "goals": goals, "relationships": relationships, "timeline": timeline, "threads": threads,
    "world": world, "questions": questions, "motifs": motifs, "contradictions": contradictions,
}
