"""Сопоставление упоминаний с известными сущностями серии. Неоднозначные случаи не объединяются автоматически:
создаётся отдельная сущность и кандидат на слияние для ручного подтверждения."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from bookmind.analysis.models import ChunkRef
from bookmind.domain.enums import EntityType
from bookmind.ingestion.text_utils import jaccard, norm_key
from bookmind.services.search import stem
from bookmind.storage.database import Database, new_id, now_iso

AUTO_LINK_THRESHOLD = 0.7
RECENT_CHUNKS = 5


@dataclass
class ResolveResult:
    entity_id: str
    resolution: str            # exact | alias | new | ambiguous
    score: float
    created: bool
    candidate_ids: list[str] = field(default_factory=list)


def _stems(name_norm: str) -> set[str]:
    return {stem(t) for t in name_norm.split() if len(t) > 2}


class EntityResolver:
    def __init__(self, db: Database, series_id: str, threshold: float = AUTO_LINK_THRESHOLD):
        self.db, self.series_id, self.threshold = db, series_id, threshold

    # ---- публичное ----
    def resolve(self, name: str, etype: EntityType, *, chunk: ChunkRef, run_id: str | None,
                aliases: Iterable[str] = (), descriptor: str = "",
                offered_ids: frozenset[str] = frozenset()) -> ResolveResult | None:
        key = norm_key(name)
        if not key:
            return None
        alias_keys = {k for k in (norm_key(a) for a in aliases) if k and k != key}
        matches, via_alias_only = self._exact_matches(key, alias_keys, etype.value)

        if len(matches) == 1:
            ent = matches[0]
            score = self._score(ent, key, alias_keys, chunk, descriptor, offered_ids, via_alias_only)
            if score >= self.threshold:
                res = "exact" if ent["name_norm"] == key else "alias"
                self._link(ent["id"], name, aliases, descriptor, chunk, run_id, res, score)
                return ResolveResult(ent["id"], res, score, False)
            created = self._create(name, etype, aliases, descriptor, chunk, run_id, review=True)
            self._candidate(ent["id"], created, score, "совпадение имени без достаточных подтверждений")
            self._mention(created, name, chunk, run_id, "new", score)
            return ResolveResult(created, "new", score, True, [ent["id"]])

        if len(matches) > 1:
            created = self._create(name, etype, aliases, descriptor, chunk, run_id, review=True)
            scores = []
            for ent in matches:
                s = self._score(ent, key, alias_keys, chunk, descriptor, offered_ids, via_alias_only)
                scores.append(s)
                self._candidate(ent["id"], created, s, "имя подходит нескольким сущностям: нужен выбор вручную")
            self._mention(created, name, chunk, run_id, "ambiguous", max(scores))
            return ResolveResult(created, "ambiguous", max(scores), True, [e["id"] for e in matches])

        created = self._create(name, etype, aliases, descriptor, chunk, run_id, review=False)
        partial = self._partial_matches(key, etype.value, exclude=created)
        for ent in partial:
            self._candidate(ent["id"], created, 0.4, "частичное совпадение имени")
        self._mention(created, name, chunk, run_id, "new", 0.0)
        return ResolveResult(created, "new", 0.0, True, [e["id"] for e in partial])

    def has_candidate(self, name: str, etype: EntityType, aliases: Iterable[str] = ()) -> bool:
        """Есть ли уже сущность с таким именем/алиасом (без создания и без записи)."""
        key = norm_key(name)
        if not key:
            return False
        alias_keys = {k for k in (norm_key(a) for a in aliases) if k and k != key}
        return bool(self._exact_matches(key, alias_keys, etype.value)[0])

    def find_unique(self, name: str, types: Iterable[EntityType]) -> str | None:
        """Единственная существующая сущность с таким именем среди заданных типов (иначе None)."""
        key = norm_key(name)
        ts = [t.value for t in types]
        if not key or not ts:
            return None
        marks = ",".join("?" * len(ts))
        rows = self.db.query(
            f"""SELECT DISTINCT e.id FROM entities e LEFT JOIN entity_aliases a ON a.entity_id = e.id
                WHERE e.series_id = ? AND e.status != 'merged' AND e.type IN ({marks}) AND (e.name_norm = ? OR a.alias_norm = ?)""",
            (self.series_id, *ts, key, key))
        return rows[0]["id"] if len(rows) == 1 else None

    def follow(self, entity_id: str) -> str:
        """Итоговый идентификатор с учётом слияний."""
        seen = set()
        while entity_id not in seen:
            seen.add(entity_id)
            row = self.db.one("SELECT merged_into FROM entities WHERE id = ?", (entity_id,))
            if not row or not row["merged_into"]:
                break
            entity_id = row["merged_into"]
        return entity_id

    # ---- поиск ----
    def _exact_matches(self, key: str, alias_keys: set[str], etype: str):
        rows = {}
        for r in self.db.query(
            """SELECT DISTINCT e.* FROM entities e LEFT JOIN entity_aliases a ON a.entity_id = e.id
               WHERE e.series_id = ? AND e.type = ? AND e.status != 'merged' AND (e.name_norm = ? OR a.alias_norm = ?)""",
                (self.series_id, etype, key, key)):
            rows[r["id"]] = r
        via_alias_only = False
        if not rows and alias_keys:
            marks = ",".join("?" * len(alias_keys))
            for r in self.db.query(
                f"""SELECT DISTINCT e.* FROM entities e LEFT JOIN entity_aliases a ON a.entity_id = e.id
                    WHERE e.series_id = ? AND e.type = ? AND e.status != 'merged'
                      AND (e.name_norm IN ({marks}) OR a.alias_norm IN ({marks}))""",
                    (self.series_id, etype, *alias_keys, *alias_keys)):
                rows[r["id"]] = r
            via_alias_only = bool(rows)
        return list(rows.values()), via_alias_only

    def _partial_matches(self, key: str, etype: str, exclude: str):
        mine = _stems(key)
        if not mine:
            return []
        out = []
        for r in self.db.query("SELECT * FROM entities WHERE series_id = ? AND type = ? AND status != 'merged' AND id != ?",
                               (self.series_id, etype, exclude)):
            theirs = _stems(r["name_norm"])
            if theirs and (mine <= theirs or theirs <= mine) and mine != theirs:
                out.append(r)
        return out[:5]

    # ---- оценка ----
    def _score(self, ent, key, alias_keys, chunk: ChunkRef, descriptor: str, offered: frozenset[str],
               via_alias_only: bool) -> float:
        score = 0.5 if via_alias_only else 0.65
        same_book = ent["first_book_id"] == chunk.book_id or bool(self.db.scalar(
            "SELECT 1 FROM entity_mentions m JOIN chunks c ON c.id = m.chunk_id WHERE m.entity_id = ? AND c.book_id = ? LIMIT 1",
            (ent["id"], chunk.book_id)))
        if same_book:
            score += 0.15
        if self.db.scalar(
                "SELECT 1 FROM entity_mentions m JOIN chunks c ON c.id = m.chunk_id WHERE m.entity_id = ? AND c.book_id = ? "
                "AND c.idx BETWEEN ? AND ? LIMIT 1", (ent["id"], chunk.book_id, chunk.idx - RECENT_CHUNKS, chunk.idx)):
            score += 0.1
        if ent["id"] in offered:
            score += 0.15
        if descriptor and jaccard(descriptor, ent["description"]) >= 0.12:
            score += 0.1
        known_alias = {r[0] for r in self.db.query("SELECT alias_norm FROM entity_aliases WHERE entity_id = ?", (ent["id"],))}
        if alias_keys & known_alias:
            score += 0.1
        return min(1.0, round(score, 3))

    # ---- запись ----
    def _create(self, name, etype: EntityType, aliases, descriptor, chunk: ChunkRef, run_id, review: bool) -> str:
        eid = new_id()
        self.db.insert("entities", {
            "id": eid, "series_id": self.series_id, "type": etype.value, "name": name.strip(), "name_norm": norm_key(name),
            "description": descriptor.strip(), "status": "needs_review" if review else "active",
            "first_book_id": chunk.book_id, "first_chunk_id": chunk.chunk_id, "created_run_id": run_id, "created_at": now_iso()})
        self._add_aliases(eid, [name, *aliases], chunk.chunk_id, canonical=name)
        return eid

    def _add_aliases(self, entity_id: str, aliases: Iterable[str], chunk_id: str, canonical: str) -> None:
        etype = self.db.scalar("SELECT type FROM entities WHERE id = ?", (entity_id,))
        for a in aliases:
            k = norm_key(a)
            if not k:
                continue
            conflict = self.db.scalar(
                """SELECT 1 FROM entity_aliases a JOIN entities e ON e.id = a.entity_id
                   WHERE a.alias_norm = ? AND e.series_id = ? AND e.type = ? AND e.id != ? AND e.status != 'merged' LIMIT 1""",
                (k, self.series_id, etype, entity_id))
            if conflict and norm_key(canonical) != k:
                continue  # алиас уже принадлежит другой сущности: не создаём новую неоднозначность
            self.db.insert("entity_aliases", {"entity_id": entity_id, "alias": a.strip(), "alias_norm": k,
                                              "source_chunk_id": chunk_id}, or_ignore=True)

    def _link(self, entity_id, name, aliases, descriptor, chunk: ChunkRef, run_id, resolution, score) -> None:
        ent = self.db.one("SELECT name, description FROM entities WHERE id = ?", (entity_id,))
        self._add_aliases(entity_id, [name, *aliases], chunk.chunk_id, canonical=ent["name"])
        if descriptor and not ent["description"]:
            self.db.update("entities", entity_id, {"description": descriptor.strip()})
        self._mention(entity_id, name, chunk, run_id, resolution, score)

    def _mention(self, entity_id, surface, chunk: ChunkRef, run_id, resolution, score) -> None:
        self.db.insert("entity_mentions", {
            "entity_id": entity_id, "chunk_id": chunk.chunk_id, "run_id": run_id, "surface": surface.strip(),
            "surface_norm": norm_key(surface), "resolution": resolution, "score": score}, or_ignore=True)

    def _candidate(self, a: str, b: str, score: float, reason: str) -> None:
        lo, hi = sorted((a, b))
        self.db.insert("merge_candidates", {
            "id": new_id(), "series_id": self.series_id, "entity_a": lo, "entity_b": hi, "score": score,
            "reason": reason, "status": "pending", "created_at": now_iso()}, or_ignore=True)
