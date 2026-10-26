"""Атомарное сохранение результатов анализа фрагмента. Всё, что не подтверждено цитатой, помечается unverified."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from bookmind.analysis.context import name_in_stems, text_stems
from bookmind.analysis.entity_resolver import EntityResolver
from bookmind.analysis.models import ChunkRef
from bookmind.analysis.verification import QuoteVerifier
from bookmind.domain.enums import EntityType, Verification
from bookmind.domain.schemas import CoreAnalysis
from bookmind.ingestion.text_utils import jaccard, norm_key
from bookmind.services.search import SearchIndex
from bookmind.storage.database import Database, new_id, now_iso

EVENT_DUP_JACCARD = 0.75


@dataclass
class SaveStats:
    entities_new: int = 0
    entities_linked: int = 0
    facts: int = 0
    facts_unverified: int = 0
    events: int = 0
    events_unverified: int = 0
    skipped: int = 0
    issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class ChunkContext:
    """Состояние обработки одного фрагмента: резолвер, проверка цитат, кэш имён."""

    def __init__(self, db: Database, chunk: ChunkRef, run_id: str, offered: frozenset[str], stats: SaveStats):
        self.db, self.chunk, self.run_id, self.offered, self.stats = db, chunk, run_id, offered, stats
        self.resolver = EntityResolver(db, chunk.series_id)
        self.verifier = QuoteVerifier(chunk.text)
        self.stems = text_stems(chunk.text)
        self.cache: dict[tuple[str, str], str] = {}
        self.touched: set[str] = set()

    def entity(self, name: str, etype: EntityType = EntityType.CHARACTER, aliases=(), descriptor: str = "") -> str | None:
        key = (norm_key(name), etype.value)
        if not key[0]:
            return None
        if key in self.cache:
            return self.cache[key]
        in_text = name_in_stems(key[0], self.stems) or any(name_in_stems(norm_key(a), self.stems) for a in aliases)
        if not in_text and not self.resolver.has_candidate(name, etype, aliases):
            self.stats.skipped += 1
            self.stats.issues.append(f"Сущность «{name}» не найдена ни в тексте, ни в базе: пропущена")
            return None
        res = self.resolver.resolve(name, etype, chunk=self.chunk, run_id=self.run_id, aliases=aliases,
                                    descriptor=descriptor, offered_ids=self.offered)
        if res is None:
            return None
        if res.created:
            self.stats.entities_new += 1
        else:
            self.stats.entities_linked += 1
        self.cache[key] = res.entity_id
        self.touched.add(res.entity_id)
        return res.entity_id

    def entity_any(self, name: str, default: EntityType, types: tuple[EntityType, ...] | None = None) -> str | None:
        """Сначала ищет уже известную сущность среди допустимых типов (место может быть организацией), иначе создаёт default."""
        key = norm_key(name)
        if not key:
            return None
        found = self.resolver.find_unique(name, types or tuple(EntityType))
        if found:
            self.cache[(key, "any")] = found
            self.touched.add(found)
            self.stats.entities_linked += 1
            self.resolver._mention(found, name, self.chunk, self.run_id, "exact", 1.0)
            return found
        return self.entity(name, default)

    def add_evidence(self, owner_type: str, owner_id: str, quote: str) -> Verification:
        check = self.verifier.check(quote)
        if quote.strip():
            self.db.insert("evidence", {
                "owner_type": owner_type, "owner_id": owner_id, "book_id": self.chunk.book_id,
                "chapter_id": self.chunk.chapter_id, "chunk_id": self.chunk.chunk_id, "quote": quote.strip()[:600],
                "start_char": check.start, "end_char": check.end, "verified": 1 if check.verified else 0,
                "run_id": self.run_id}, or_ignore=True)
        if not check.verified:
            self.stats.issues.append(f"{owner_type}: {check.reason}")
        return Verification.VERIFIED if check.verified else Verification.UNVERIFIED


class CorePersister:
    def __init__(self, db: Database):
        self.db = db
        self.search = SearchIndex(db)

    def save(self, chunk: ChunkRef, run_id: str, core: CoreAnalysis, offered: frozenset[str] = frozenset()) -> SaveStats:
        stats = SaveStats()
        with self.db.transaction():
            ctx = ChunkContext(self.db, chunk, run_id, offered, stats)
            self._entities(ctx, core)
            self._facts(ctx, core)
            self._events(ctx, core)
            self._summary(ctx, core)
            for eid in ctx.touched:
                self.index_entity(chunk.series_id, eid)
        return stats

    # ---- части ----
    def _entities(self, ctx: ChunkContext, core: CoreAnalysis) -> None:
        for item in core.entities:
            if not item.name.strip():
                continue
            eid = ctx.entity(item.name, item.type, item.aliases, item.description)
            if eid and item.quote:
                ctx.add_evidence("entity", eid, item.quote)

    def _facts(self, ctx: ChunkContext, core: CoreAnalysis) -> None:
        for item in core.facts:
            if not item.statement.strip() or not item.entity.strip():
                ctx.stats.skipped += 1
                continue
            eid = ctx.entity(item.entity, self._known_type(ctx, item.entity))
            if not eid:
                continue
            fid = new_id()
            ver = ctx.verifier.check(item.quote)
            cur = self.db.insert("facts", {
                "id": fid, "series_id": ctx.chunk.series_id, "book_id": ctx.chunk.book_id,
                "chapter_id": ctx.chunk.chapter_id, "chunk_id": ctx.chunk.chunk_id, "run_id": ctx.run_id,
                "entity_id": eid, "category": item.category.value, "statement": item.statement.strip(),
                "statement_norm": norm_key(item.statement), "kind": item.kind.value,
                "verification": (Verification.VERIFIED if ver.verified else Verification.UNVERIFIED).value,
                "confidence": item.confidence, "created_at": now_iso()}, or_ignore=True)
            if cur.rowcount == 0:
                continue  # дубликат (тот же объект, фрагмент и утверждение)
            ctx.add_evidence("fact", fid, item.quote)
            ctx.stats.facts += 1
            if not ver.verified:
                ctx.stats.facts_unverified += 1
            else:
                name = self.db.scalar("SELECT name FROM entities WHERE id = ?", (eid,))
                self.search.index("fact", fid, ctx.chunk.series_id, ctx.chunk.book_id, ctx.chunk.chapter_id, name,
                                  item.statement)

    def _events(self, ctx: ChunkContext, core: CoreAnalysis) -> None:
        chapter_events = [r["description"] for r in self.db.query(
            "SELECT description FROM events WHERE chapter_id = ?", (ctx.chunk.chapter_id,))]
        n = 0
        for item in core.events:
            desc = item.description.strip()
            if not desc:
                ctx.stats.skipped += 1
                continue
            if any(jaccard(desc, d) >= EVENT_DUP_JACCARD for d in chapter_events):
                ctx.stats.skipped += 1
                continue  # то же событие уже записано (например, из перекрытия соседнего фрагмента)
            eid = new_id()
            place_id = (ctx.entity_any(item.place, EntityType.PLACE, (EntityType.PLACE, EntityType.ORGANIZATION, EntityType.STATE))
                        if item.place.strip() else None)
            ver = ctx.verifier.check(item.quote)
            self.db.insert("events", {
                "id": eid, "series_id": ctx.chunk.series_id, "book_id": ctx.chunk.book_id,
                "chapter_id": ctx.chunk.chapter_id, "chunk_id": ctx.chunk.chunk_id, "run_id": ctx.run_id,
                "seq": ctx.chunk.idx * 1000 + n, "description": desc, "description_norm": norm_key(desc),
                "causes": item.causes, "consequences": item.consequences, "place_entity_id": place_id,
                "place_text": item.place, "story_time": item.story_time, "narrative_mode": item.narrative_mode.value,
                "confidence": item.confidence,
                "verification": (Verification.VERIFIED if ver.verified else Verification.UNVERIFIED).value,
                "created_at": now_iso()})
            n += 1
            chapter_events.append(desc)
            ctx.add_evidence("event", eid, item.quote)
            for p in item.participants:
                pid = ctx.entity_any(p.name, EntityType.CHARACTER) if p.name.strip() else None
                if pid:
                    self.db.insert("event_participants", {"event_id": eid, "entity_id": pid, "role": p.role}, or_ignore=True)
            ctx.stats.events += 1
            if not ver.verified:
                ctx.stats.events_unverified += 1
            else:
                self.search.index("event", eid, ctx.chunk.series_id, ctx.chunk.book_id, ctx.chunk.chapter_id,
                                  ctx.chunk.chapter_title, " ".join([desc, item.causes, item.consequences]))

    def _summary(self, ctx: ChunkContext, core: CoreAnalysis) -> None:
        if not core.summary.strip():
            return
        self.db.insert("report_versions", {
            "id": new_id(), "series_id": ctx.chunk.series_id, "book_id": ctx.chunk.book_id, "scope_id": ctx.chunk.chunk_id,
            "kind": "chunk_summary", "title": f"{ctx.chunk.chapter_title} · фрагмент {ctx.chunk.idx + 1}",
            "content": core.summary.strip(), "structured_json": "{}", "run_id": ctx.run_id, "version": 1,
            "created_at": now_iso()})

    # ---- вспомогательное ----
    def _known_type(self, ctx: ChunkContext, name: str) -> EntityType:
        """Для факта о ранее объявленной сущности берём её тип; иначе — персонаж."""
        key = norm_key(name)
        for (k, t), _id in ctx.cache.items():
            if k == key:
                return EntityType(t)
        row = self.db.one("SELECT e.type FROM entities e LEFT JOIN entity_aliases a ON a.entity_id = e.id "
                          "WHERE e.series_id = ? AND e.status != 'merged' AND (e.name_norm = ? OR a.alias_norm = ?) LIMIT 1",
                          (ctx.chunk.series_id, key, key))
        return EntityType(row["type"]) if row else EntityType.CHARACTER

    def index_entity(self, series_id: str, entity_id: str) -> None:
        e = self.db.one("SELECT * FROM entities WHERE id = ?", (entity_id,))
        if not e or e["status"] == "merged":
            return
        aliases = " ".join(r["alias"] for r in self.db.query("SELECT alias FROM entity_aliases WHERE entity_id = ?", (entity_id,)))
        self.search.index("entity", entity_id, series_id, e["first_book_id"], None, e["name"],
                          f"{e['name']} {aliases} {e['description']}")
