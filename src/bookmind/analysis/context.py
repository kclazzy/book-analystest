"""Формирование компактного контекста для промпта: только релевантные сведения базы, а не вся книга."""
from __future__ import annotations

import re

from bookmind.analysis.models import ChunkRef
from bookmind.services.search import stem
from bookmind.storage.database import Database

_WORD = re.compile(r"\w+", re.UNICODE)


def text_stems(text: str) -> set[str]:
    return {stem(w) for w in _WORD.findall(text)}


def name_in_stems(name_norm: str, stems: set[str]) -> bool:
    toks = [t for t in name_norm.split() if t]
    return bool(toks) and all(stem(t) in stems for t in toks)


class ContextBuilder:
    def __init__(self, db: Database, max_entities: int = 15, recent_chunks: int = 3):
        self.db, self.max_entities, self.recent_chunks = db, max_entities, recent_chunks

    def relevant_entities(self, chunk: ChunkRef) -> list[dict]:
        stems = text_stems(chunk.text)
        rows = self.db.query(
            """SELECT e.id, e.name, e.type, e.description, e.name_norm,
                      (SELECT COUNT(*) FROM entity_mentions m WHERE m.entity_id = e.id) AS mentions
               FROM entities e WHERE e.series_id = ? AND e.status != 'merged'""", (chunk.series_id,))
        recent = {r[0] for r in self.db.query(
            """SELECT DISTINCT m.entity_id FROM entity_mentions m JOIN chunks c ON c.id = m.chunk_id
               WHERE c.book_id = ? AND c.idx BETWEEN ? AND ?""",
            (chunk.book_id, chunk.idx - self.recent_chunks, chunk.idx - 1))}
        scored = []
        for r in rows:
            aliases = [a["alias_norm"] for a in self.db.query("SELECT alias_norm FROM entity_aliases WHERE entity_id = ?", (r["id"],))]
            in_text = name_in_stems(r["name_norm"], stems) or any(name_in_stems(a, stems) for a in aliases)
            if in_text or r["id"] in recent:
                scored.append((2 if in_text else 1, r["mentions"], r, aliases))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        out = []
        for _, _, r, _al in scored[: self.max_entities]:
            names = [a["alias"] for a in self.db.query(
                "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY id LIMIT 6", (r["id"],))
                if a["alias"].casefold() != r["name"].casefold()]
            out.append({"id": r["id"], "name": r["name"], "type": r["type"], "aliases": names[:4],
                        "description": r["description"][:140]})
        return out

    def known_entities_block(self, chunk: ChunkRef) -> tuple[str, frozenset[str]]:
        ents = self.relevant_entities(chunk)
        if not ents:
            return "(пока нет)", frozenset()
        lines = []
        for e in ents:
            line = f"- {e['name']} [{e['type']}]"
            if e["aliases"]:
                line += "; также: " + ", ".join(e["aliases"])
            if e["description"]:
                line += f"; {e['description']}"
            lines.append(line)
        return "\n".join(lines), frozenset(e["id"] for e in ents)

    def known_goals_block(self, chunk: ChunkRef, entity_ids: frozenset[str], per_entity: int = 3) -> str:
        lines = []
        for eid in entity_ids:
            name = self.db.scalar("SELECT name FROM entities WHERE id = ?", (eid,))
            for g in self.db.query(
                    "SELECT description, status FROM goals WHERE entity_id = ? AND status IN ('active','postponed','unknown','changed') "
                    "ORDER BY updated_at DESC LIMIT ?", (eid, per_entity)):
                lines.append(f"- {name}: [{g['status']}] {g['description'][:140]}")
        return "\n".join(lines) or "(пока нет)"

    def open_questions_block(self, chunk: ChunkRef, limit: int = 6) -> str:
        rows = self.db.query(
            "SELECT kind, text FROM open_questions WHERE series_id = ? AND status != 'resolved' AND verification = 'verified' "
            "ORDER BY created_at DESC LIMIT ?", (chunk.series_id, limit))
        return "\n".join(f"- [{r['kind']}] {r['text'][:160]}" for r in rows) or "(пока нет)"
