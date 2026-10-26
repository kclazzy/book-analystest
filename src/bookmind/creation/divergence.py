"""«Ручка расхождения»: как далеко новая книга уходит от оригинала. Детерминированно (по seed) раскладывает биты
оригинала по главам новой книги, сдвигает, отбрасывает и помечает на изменение исхода."""
from __future__ import annotations

import random

from pydantic import BaseModel, Field

from bookmind.creation.schemas import AdaptParams
from bookmind.creation.skeleton import Beat, Skeleton

PROFILES = {"weak": dict(shift=0.0, drop=0.0, invert=0.0),
            "medium": dict(shift=0.08, drop=0.15, invert=0.25),
            "strong": dict(shift=0.15, drop=0.30, invert=0.5)}
GROUP_OF_KEEP = {"mystery": "Q", "relationship": "L", "goal": "G", "thread": "T", "flashback": "F"}
INVERTIBLE = ("goal_change", "goal_resolved", "relationship_shift", "mystery_resolved", "thread_close")
BASE_CHANGES = ["Все имена, названия, места и предметы — новые, не связанные с оригиналом",
                "Сцены не повторяют сцены оригинала: те же функции, но другие обстоятельства и действия"]
LEVEL_CHANGES = {
    "weak": [],
    "medium": ["Исход минимум двух ключевых поворотов должен отличаться от оригинального",
               "Мотивация антагониста другая"],
    "strong": ["Смени жанровую оболочку и тон повествования",
               "Поменяй местами функции двух ролей (например, наставника и союзника)",
               "Финал принципиально отличается от оригинала",
               "Мотивация антагониста другая, а часть второстепенных линий заменена новыми"],
}


class PlannedBeat(BaseModel):
    id: str
    kind: str
    group: str
    chapter: int                    # 0-based номер главы новой книги
    roles: list[str] = Field(default_factory=list)
    text: str
    mutation: str = ""              # "" | "invert"
    instruction: str = ""
    source_chapter: int = 0


class Assignment(BaseModel):
    chapters: int
    beats: list[PlannedBeat] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)       # ids выброшенных битов
    global_changes: list[str] = Field(default_factory=list)
    mutations: list[str] = Field(default_factory=list)     # ids битов с изменением исхода

    def for_chapter(self, idx: int) -> list[PlannedBeat]:
        return [b for b in self.beats if b.chapter == idx]


def assign(sk: Skeleton, params: AdaptParams, seed: int) -> Assignment:
    prof = PROFILES[params.divergence]
    rnd = random.Random(seed)
    n = max(3, params.chapters or sk.source_chapters)
    groups = list(dict.fromkeys(b.group for b in sk.beats))
    protected = {g for k in params.keep for g in groups if g.startswith(GROUP_OF_KEEP.get(k, "?"))}
    main = next((b.group for b in sk.beats if b.kind == "goal_set" and b.roles and b.roles[0] == "Протагонист"), None)
    if main:
        protected.add(main)
    droppable = [g for g in groups if g not in protected and (params.divergence == "strong" or not g.startswith("Q"))]
    k = min(round(prof["drop"] * len(droppable)), max(0, len(groups) - 3))
    dropped_groups = set(rnd.sample(droppable, k)) if k else set()

    kept: list[Beat] = [b for b in sk.beats if b.group not in dropped_groups]
    # сдвиг позиций с сохранением порядка внутри линии
    new_pos: dict[str, float] = {}
    for g in dict.fromkeys(b.group for b in kept):
        gb = [b for b in kept if b.group == g]
        shifted = sorted(min(0.999, max(0.001, b.position + rnd.uniform(-prof["shift"], prof["shift"]))) for b in gb)
        for b, p in zip(gb, shifted):
            new_pos[b.id] = p
    chapter = {b.id: min(n - 1, int(new_pos[b.id] * n)) for b in kept}
    # загадка: завязка строго раньше разгадки, подсказки между ними
    for g in {b.group for b in kept if b.group.startswith("Q")}:
        gb = [b for b in kept if b.group == g]
        raised = next((b for b in gb if b.kind == "mystery_raised"), None)
        res = next((b for b in gb if b.kind == "mystery_resolved"), None)
        if raised is None:
            continue
        if res is not None:
            chapter[res.id] = max(chapter[res.id], min(n - 1, chapter[raised.id] + 1))
            if chapter[res.id] <= chapter[raised.id]:      # завязка в последней главе — сдвигаем её раньше
                chapter[raised.id] = chapter[res.id] - 1
        lo = chapter[raised.id] + 1
        hi = max(lo, chapter[res.id] - 1) if res is not None else n - 1
        for b in gb:
            if b.kind == "clue":
                chapter[b.id] = min(n - 1, min(hi, max(lo, chapter[b.id])))

    detailed = params.divergence != "strong"
    planned: list[PlannedBeat] = []
    mutated: list[str] = []
    for b in kept:
        mut, instr = "", ""
        if b.kind in INVERTIBLE and b.group not in protected and rnd.random() < prof["invert"]:
            mut = "invert"
            instr = f"ИЗМЕНИТЬ ИСХОД: в оригинале — «{b.abstract}»; в новой книге результат должен быть иным"
            mutated.append(b.id)
        planned.append(PlannedBeat(id=b.id, kind=b.kind, group=b.group, chapter=chapter[b.id], roles=b.roles,
                                   text=b.detailed if detailed else b.abstract, mutation=mut, instruction=instr,
                                   source_chapter=b.source_chapter))
    planned.sort(key=lambda p: (p.chapter, p.id))
    changes = BASE_CHANGES + LEVEL_CHANGES[params.divergence]
    if params.keep:
        changes.append("Сохранить по структуре: " + ", ".join(params.keep))
    dropped_ids = [b.id for b in sk.beats if b.group in dropped_groups]
    return Assignment(chapters=n, beats=planned, dropped=dropped_ids, global_changes=changes, mutations=mutated)
