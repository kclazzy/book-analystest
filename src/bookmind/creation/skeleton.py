"""Структурный скелет оригинала: роли и «биты» (цели, смены отношений, линии, загадки, вставки из прошлого) с позициями
в книге. Конкретные имена и названия заменены ярлыками; из базы берутся только подтверждённые цитатой данные."""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from bookmind.creation.anonymizer import TYPE_LABELS, Anonymizer
from bookmind.domain.enums import label_of
from bookmind.services.knowledge import KnowledgeService
from bookmind.storage.database import Database

POSITIVE = ("друг", "союзник", "довер", "соратник", "любов", "дружб", "верн")
NEGATIVE = ("враг", "предател", "противник", "антагонист", "ненавист")
MENTOR = ("наставник", "учитель", "ментор")
KIN = ("сестр", "брат", "дочь", "сын", "ребён", "семь", "родител", "мать", "отец", "подопечн")
MAX_ROLES = 8


class Role(BaseModel):
    key: str
    label: str
    mentions: int = 0
    goal_count: int = 0
    arc: str = ""            # обезличенное описание арки роли


class Beat(BaseModel):
    id: str
    kind: str                # goal_set | goal_change | goal_resolved | relationship_shift | thread_open | thread_close |
                             # mystery_raised | clue | mystery_resolved | flashback
    group: str               # сквозная линия: G1 (цель), L1 (отношения), T1 (линия), Q1 (загадка), F (вставки)
    position: float          # 0..1 — относительное место в оригинале
    source_chapter: int      # номер главы оригинала (с 1)
    roles: list[str] = Field(default_factory=list)
    detailed: str = ""       # обезличенный текст с деталями
    abstract: str = ""       # только тип события, без деталей


class Skeleton(BaseModel):
    source_chapters: int
    roles: list[Role] = Field(default_factory=list)
    beats: list[Beat] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)

    def role_labels(self) -> list[str]:
        return [r.label for r in self.roles]


def _has(text: str, words: tuple[str, ...]) -> bool:
    t = text.casefold()
    return any(w in t for w in words)


class SkeletonBuilder:
    def __init__(self, db: Database):
        self.db = db
        self.ks = KnowledgeService(db)

    # ---- привязка к главам ----
    def _ordinals(self, book_ids: list[str]) -> tuple[dict[str, int], dict[str, int]]:
        rows = self.db.query(
            f"SELECT ch.id AS cid FROM chapters ch JOIN books b ON b.id = ch.book_id WHERE b.id IN ({','.join('?' * len(book_ids))}) "
            "ORDER BY b.series_order, ch.idx", book_ids)
        chap = {r["cid"]: i for i, r in enumerate(rows)}
        chunks = {r["id"]: chap[r["chapter_id"]] for r in self.db.query(
            f"SELECT id, chapter_id FROM chunks WHERE book_id IN ({','.join('?' * len(book_ids))})", book_ids) if r["chapter_id"] in chap}
        return chap, chunks

    def build(self, series_id: str, book_ids: list[str] | None = None) -> tuple[Skeleton, Anonymizer]:
        books = book_ids or [r["id"] for r in self.db.query(
            "SELECT id FROM books WHERE series_id = ? ORDER BY series_order", (series_id,))]
        if not books:
            raise ValueError("В серии нет книг")
        chap, chunk_ord = self._ordinals(books)
        total = len(chap)
        if total == 0:
            raise ValueError("В выбранных книгах нет глав")
        pos = lambda ordinal: round((ordinal + 0.5) / total, 4)
        in_books = f"({','.join('?' * len(books))})"

        # ----- роли -----
        chars = []
        for e in self.ks.entities(series_id, "character"):
            m = self.db.scalar(
                f"SELECT COUNT(*) FROM entity_mentions x JOIN chunks c ON c.id = x.chunk_id WHERE x.entity_id = ? AND c.book_id IN {in_books}",
                (e["id"], *books))
            if m:
                chars.append({**e, "m": m})
        goals = [g for g in self.ks.goals(series_id) if g["first_book_id"] in books]
        gcount: dict[str, int] = {}
        for g in goals:
            gcount[g["entity_id"]] = gcount.get(g["entity_id"], 0) + 1
        if not chars:
            raise ValueError("Нет проанализированных персонажей: сначала выполните анализ книги")
        chars.sort(key=lambda e: -(gcount.get(e["id"], 0) * 3 + e["m"]))
        protagonist = chars[0]
        rels = {}   # other_id -> список типов связи с протагонистом в хронологическом порядке
        for r in self.ks.relationships_of(protagonist["id"]):
            rels[r["other_id"]] = [h["rel_type"] for h in r["history"]]

        labels: dict[str, str] = {protagonist["id"]: "Протагонист"}
        others = [e for e in chars[1:]]
        used = set()
        def assign(label: str, pred) -> None:
            for e in others:
                if e["id"] in labels:
                    continue
                if pred(rels.get(e["id"], [])):
                    labels[e["id"]] = label
                    return
        assign("Наставник", lambda h: any(_has(t, MENTOR) for t in h))
        def betrayer(h):
            pos_i = [i for i, t in enumerate(h) if _has(t, POSITIVE)]
            neg_i = [i for i, t in enumerate(h) if _has(t, NEGATIVE)]
            return bool(pos_i and neg_i and min(pos_i) < max(neg_i))
        assign("Союзник-предатель", betrayer)
        assign("Антагонист", lambda h: any(_has(t, NEGATIVE) for t in h))
        assign("Близкий человек", lambda h: any(_has(t, KIN) for t in h))
        n = 0
        for e in others:
            if e["id"] in labels or len(labels) >= MAX_ROLES:
                continue
            if gcount.get(e["id"]) or e["id"] in rels:
                n += 1
                labels[e["id"]] = f"Второстепенный персонаж {n}"

        anon = Anonymizer()
        for e in chars:
            names = [e["name"], *[a["alias"] for a in self.db.query("SELECT alias FROM entity_aliases WHERE entity_id = ?", (e["id"],))]]
            anon.add(names, labels.get(e["id"], "второстепенный персонаж"), person=True)
        counters: dict[str, int] = {}
        for e in self.ks.entities(series_id):
            if e["type"] == "character":
                continue
            base = TYPE_LABELS.get(e["type"], "объект")
            counters[base] = counters.get(base, 0) + 1
            anon.add([e["name"]], f"{base} {counters[base]}", person=False)

        roles = [Role(key=f"R{i + 1}", label=lab, mentions=next(e["m"] for e in chars if e["id"] == eid), goal_count=gcount.get(eid, 0))
                 for i, (eid, lab) in enumerate(labels.items())]

        # ----- биты -----
        beats: list[Beat] = []
        for gi, g in enumerate(sorted(goals, key=lambda x: x["created_at"]), 1):
            role = labels.get(g["entity_id"])
            if not role:
                continue
            group = f"G{gi}"
            for hi, h in enumerate(g["history"]):
                if h["chunk_id"] not in chunk_ord:
                    continue
                o = chunk_ord[h["chunk_id"]]
                if hi == 0:
                    kind, abstract = "goal_set", f"{role} обретает личную цель ({label_of(g['goal_type']).lower()})"
                    detailed = f"{role} обретает цель: {anon.apply(g['description'])}"
                else:
                    new = h["new_status"]
                    kind = "goal_resolved" if new in ("achieved", "failed", "cancelled") else "goal_change"
                    abstract = f"цель роли «{role}» меняет статус: {label_of(h['old_status']).lower()} → {label_of(new).lower()}"
                    detailed = f"{abstract}" + (f" ({anon.apply(h['note'])})" if h["note"] else "")
                beats.append(Beat(id="", kind=kind, group=group, position=pos(o), source_chapter=o + 1, roles=[role],
                                  detailed=detailed, abstract=abstract))
        relnames = {r["id"]: r for r in self.db.query("SELECT * FROM relationships WHERE series_id = ? AND verification = 'verified'", (series_id,))}
        for li, (rid, r) in enumerate(relnames.items(), 1):
            ra, rb = labels.get(r["entity_a"]), labels.get(r["entity_b"])
            if not ra or not rb:
                continue
            hist = self.db.query("SELECT * FROM relationship_history WHERE relationship_id = ? ORDER BY id", (rid,))
            prev = None
            for h in hist:
                if h["chunk_id"] not in chunk_ord:
                    continue
                if prev is not None and (h["rel_type"], h["state"]) == prev:
                    continue
                o = chunk_ord[h["chunk_id"]]
                abstract = (f"отношения «{ra}» и «{rb}»: " + (f"{prev[0]} → {h['rel_type']}" if prev else f"устанавливаются ({h['rel_type']})"))
                detailed = abstract + (f"; состояние: {anon.apply(h['state'])}" if h["state"] else "") + (f"; причина: {anon.apply(h['note'])}" if h["note"] else "")
                beats.append(Beat(id="", kind="relationship_shift", group=f"L{li}", position=pos(o), source_chapter=o + 1, roles=[ra, rb],
                                  detailed=detailed, abstract=abstract))
                prev = (h["rel_type"], h["state"])
        for ti, t in enumerate(self.ks.plot_threads(series_id), 1):
            first = chunk_ord.get(t["first_chunk_id"])
            last = chunk_ord.get(t["last_chunk_id"])
            if first is None:
                continue
            beats.append(Beat(id="", kind="thread_open", group=f"T{ti}", position=pos(first), source_chapter=first + 1,
                              detailed=f"открывается сюжетная линия: {anon.apply(t['title'])}", abstract="открывается сюжетная линия"))
            if last is not None and t["status"] == "resolved" and last != first:
                beats.append(Beat(id="", kind="thread_close", group=f"T{ti}", position=pos(last), source_chapter=last + 1,
                                  detailed=f"линия завершается: {anon.apply(t['title'])}", abstract="сюжетная линия завершается"))
        for qi, q in enumerate(self.ks.questions(series_id), 1):
            raised = chunk_ord.get(q["raised_chunk_id"])
            if raised is None:
                continue
            kind_ru = label_of(q["kind"]).lower()
            resolved = chunk_ord.get(q["resolved_chunk_id"]) if q["status"] == "resolved" else None
            beats.append(Beat(id="", kind="mystery_raised", group=f"Q{qi}", position=pos(raised), source_chapter=raised + 1,
                              detailed=f"возникает {kind_ru}: {anon.apply(q['text'])}", abstract=f"возникает {kind_ru} (загадка для читателя)"))
            clue_chapters = sorted({chunk_ord[s["chunk_id"]] for s in q["sources"] if s["chunk_id"] in chunk_ord}
                                   - {raised, resolved if resolved is not None else -1})
            for c in [c for c in clue_chapters if c > raised and (resolved is None or c < resolved)][:4]:
                beats.append(Beat(id="", kind="clue", group=f"Q{qi}", position=pos(c), source_chapter=c + 1,
                                  detailed="подсказка к загадке", abstract="подсказка к загадке"))
            if resolved is not None and resolved > raised:
                beats.append(Beat(id="", kind="mystery_resolved", group=f"Q{qi}", position=pos(resolved), source_chapter=resolved + 1,
                                  detailed=f"загадка раскрывается: {anon.apply(q['resolution'])}", abstract="загадка раскрывается"))
        for ev in self.ks.chronology(series_id):
            if ev["narrative_mode"] in ("flashback", "memory") and ev["chunk_id"] in chunk_ord:
                o = chunk_ord[ev["chunk_id"]]
                beats.append(Beat(id="", kind="flashback", group="F", position=pos(o), source_chapter=o + 1,
                                  detailed="вставка из прошлого, объясняющая предысторию героя или конфликта",
                                  abstract="вставка из прошлого, объясняющая предысторию"))
        order = {"goal_set": 0, "mystery_raised": 1, "thread_open": 2, "relationship_shift": 3, "clue": 4, "flashback": 5,
                 "goal_change": 6, "thread_close": 7, "mystery_resolved": 8, "goal_resolved": 9}
        beats.sort(key=lambda b: (b.position, order.get(b.kind, 5), b.group))
        for i, b in enumerate(beats, 1):
            b.id = f"B{i:02d}"
        for r in roles:
            mine = [b for b in beats if r.label in b.roles and b.kind in ("goal_set", "goal_change", "goal_resolved", "relationship_shift")]
            r.arc = "; ".join(b.abstract for b in mine[:4])
        sk = Skeleton(source_chapters=total, roles=roles, beats=beats,
                      stats={"goals": len({b.group for b in beats if b.group.startswith("G")}),
                             "relationships": len({b.group for b in beats if b.group.startswith("L")}),
                             "threads": len({b.group for b in beats if b.group.startswith("T")}),
                             "mysteries": len({b.group for b in beats if b.group.startswith("Q")})})
        return sk, anon
