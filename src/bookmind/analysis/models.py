"""Общие структуры анализа."""
from __future__ import annotations

from dataclasses import dataclass

from bookmind.domain.exceptions import BookMindError
from bookmind.storage.database import Database


@dataclass(frozen=True)
class ChunkRef:
    chunk_id: str
    chapter_id: str
    book_id: str
    series_id: str
    idx: int
    chapter_idx: int
    chapter_title: str
    book_title: str
    series_order: int
    start_char: int
    text: str


def load_chunk_ref(db: Database, chunk_id: str) -> ChunkRef:
    r = db.one(
        """SELECT c.id, c.chapter_id, c.book_id, b.series_id, c.idx, ch.idx AS chapter_idx, ch.title AS chapter_title,
                  b.title AS book_title, b.series_order, c.start_char, c.text
           FROM chunks c JOIN chapters ch ON ch.id = c.chapter_id JOIN books b ON b.id = c.book_id WHERE c.id = ?""",
        (chunk_id,))
    if r is None:
        raise BookMindError(f"Фрагмент не найден: {chunk_id}")
    return ChunkRef(r["id"], r["chapter_id"], r["book_id"], r["series_id"], r["idx"], r["chapter_idx"],
                    r["chapter_title"], r["book_title"], r["series_order"], r["start_char"], r["text"])
