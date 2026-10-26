"""Проверки плана без модели: покрытие битов, порядок загадок, известные персонажи, полнота полей."""
from __future__ import annotations

from bookmind.creation.divergence import Assignment
from bookmind.creation.schemas import Cast, ChapterPlan, Premise
from bookmind.services.search import stem
import re

_WORD = re.compile(r"\w+", re.UNICODE)


def _stems(s: str) -> set[str]:
    return {stem(w) for w in _WORD.findall(s) if len(w) >= 3}


def validate_premise(p: Premise) -> list[str]:
    out = []
    for f, label in (("title", "название"), ("logline", "краткое описание"), ("central_conflict", "центральный конфликт"), ("setting", "мир")):
        if not getattr(p, f).strip():
            out.append(f"в предпосылке не заполнено: {label}")
    return out


def validate_cast(cast: Cast, role_labels: list[str]) -> list[str]:
    out = []
    names = [m.name.strip() for m in cast.members]
    if any(not n for n in names):
        out.append("у героя нет имени")
    if len({n.casefold() for n in names if n}) != len([n for n in names if n]):
        out.append("есть повторяющиеся имена героев")
    roles = {m.role.strip().casefold() for m in cast.members}
    for lab in role_labels:
        if lab.casefold() not in roles:
            out.append(f"нет героя для роли «{lab}»")
    return out


def cast_name_stems(cast: Cast) -> list[set[str]]:
    return [_stems(m.name) for m in cast.members if m.name.strip()]


def validate_chapter(plan: ChapterPlan, cast: Cast) -> list[str]:
    out = []
    if not plan.title.strip():
        out.append("нет названия главы")
    if len(plan.summary.strip()) < 20:
        out.append("слишком короткое описание главы")
    known = cast_name_stems(cast)
    for name in plan.characters:
        s = _stems(name)
        if s and not any(s & k for k in known):
            out.append(f"в плане неизвестный персонаж «{name}» (нет в списке героев)")
    return out


def validate_global(plans: dict[int, ChapterPlan], asg: Assignment) -> dict[int, list[str]]:
    """Замечания по главам: непокрытые биты и нарушение порядка загадок."""
    issues: dict[int, list[str]] = {i: [] for i in range(asg.chapters)}
    covered_at: dict[str, int] = {}
    for idx, plan in plans.items():
        for bid in plan.beats_covered:
            covered_at.setdefault(bid.strip().strip("[]").upper(), idx)
    for b in asg.beats:
        where = [i for i, p in plans.items() if abs(i - b.chapter) <= 1 and b.id in {x.strip().strip("[]").upper() for x in p.beats_covered}]
        if b.chapter in plans and not where:
            issues[b.chapter].append(f"не отражён бит [{b.id}] ({b.text[:80]})")
            continue
        if where:
            covered_at[b.id] = min(where, key=lambda i: abs(i - b.chapter))
    for g in {b.group for b in asg.beats if b.group.startswith("Q")}:
        gb = [b for b in asg.beats if b.group == g]
        raised = next((b for b in gb if b.kind == "mystery_raised"), None)
        res = next((b for b in gb if b.kind == "mystery_resolved"), None)
        if raised and res and raised.id in covered_at and res.id in covered_at and covered_at[res.id] <= covered_at[raised.id]:
            issues[covered_at[res.id]].append("загадка раскрывается не позже, чем возникает")
        if res and res.id in covered_at:
            for c in (b for b in gb if b.kind == "clue" and b.id in covered_at):
                if covered_at[c.id] > covered_at[res.id]:
                    issues[covered_at[res.id]].append("подсказка к загадке идёт после её разгадки")
    return {i: v for i, v in issues.items() if v}


def unused_cast(plans: dict[int, ChapterPlan], cast: Cast) -> list[str]:
    seen: set[str] = set()
    for p in plans.values():
        for n in [*p.characters, p.pov]:
            seen |= _stems(n)
    return [m.name for m in cast.members if m.name.strip() and not (_stems(m.name) & seen)]
