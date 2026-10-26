"""Полнотекстовый поиск (SQLite FTS5) с лёгкой русской стеммизацией по окончаниям."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from bookmind.storage.database import Database

_ENDINGS = sorted([
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ать", "ять", "ует", "уют", "ает", "ают", "ешь", "ишь",
    "ала", "ила", "ыла", "ули", "али", "или", "ыли", "ует", "ой", "ом", "ем", "ам", "ям", "ах", "ях", "ов", "ев",
    "ей", "ий", "ый", "ая", "яя", "ое", "ее", "ые", "ие", "ую", "юю", "ыл", "ил", "ал", "ял", "ла", "ли", "ло",
    "ть", "ся", "сь", "ет", "ут", "ют", "ат", "ят", "ит", "ые", "а", "я", "у", "ю", "ы", "и", "е", "о", "ь", "й",
], key=len, reverse=True)
_WORD = re.compile(r"[\w]+", re.UNICODE)
STOPWORDS = set("""и в во на с со что как это не по из за он она они оно к о об а но же ли я мы ты вы его ее их от до для при
у бы то так все всё был была были было есть быть который которая которые какие какой какая каково
почему когда кто где зачем куда откуда сколько ли ни или да нет""".split())


def stem(token: str) -> str:
    t = token.casefold().replace("ё", "е")
    if len(t) <= 3 or not re.search(r"[а-я]", t):
        return t
    for end in _ENDINGS:
        if t.endswith(end) and len(t) - len(end) >= 3:
            return t[: -len(end)]
    return t


def stem_text(text: str) -> str:
    return " ".join(stem(w) for w in _WORD.findall(text))


def query_terms(query: str) -> list[str]:
    terms = []
    for w in _WORD.findall(query.casefold().replace("ё", "е")):
        if w in STOPWORDS or len(w) < 2:
            continue
        terms.append(stem(w))
    return list(dict.fromkeys(terms))


@dataclass
class SearchHit:
    kind: str
    ref_id: str
    book_id: str | None
    chapter_id: str | None
    title: str
    rank: float


class SearchIndex:
    def __init__(self, db: Database):
        self.db = db

    def index(self, kind: str, ref_id: str, series_id: str, book_id: str | None, chapter_id: str | None,
              title: str, body: str) -> None:
        self.db.execute("DELETE FROM search_index WHERE kind = ? AND ref_id = ?", (kind, ref_id))
        self.db.execute(
            "INSERT INTO search_index(kind, ref_id, series_id, book_id, chapter_id, title, body) VALUES (?,?,?,?,?,?,?)",
            (kind, ref_id, series_id, book_id or "", chapter_id or "", stem_text(title), stem_text(body)))

    def remove(self, kind: str, ref_id: str) -> None:
        self.db.execute("DELETE FROM search_index WHERE kind = ? AND ref_id = ?", (kind, ref_id))

    def count(self, kind: str | None = None) -> int:
        if kind:
            return self.db.scalar("SELECT COUNT(*) FROM search_index WHERE kind = ?", (kind,))
        return self.db.scalar("SELECT COUNT(*) FROM search_index")

    def search(self, query: str, *, series_id: str | None = None, book_ids: Iterable[str] | None = None,
               kinds: Iterable[str] | None = None, limit: int = 20) -> list[SearchHit]:
        terms = query_terms(query)
        if not terms:
            return []
        match = " OR ".join(f'"{t}"*' if len(t) >= 3 else f'"{t}"' for t in terms)
        sql = ("SELECT kind, ref_id, book_id, chapter_id, title, bm25(search_index, 4.0, 1.0) AS rank "
               "FROM search_index WHERE search_index MATCH ?")
        params: list = [match]
        if series_id:
            sql += " AND series_id = ?"
            params.append(series_id)
        books = list(book_ids or [])
        if books:
            sql += f" AND book_id IN ({','.join('?' * len(books))})"
            params += books
        ks = list(kinds or [])
        if ks:
            sql += f" AND kind IN ({','.join('?' * len(ks))})"
            params += ks
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        return [SearchHit(r["kind"], r["ref_id"], r["book_id"] or None, r["chapter_id"] or None, r["title"], r["rank"])
                for r in self.db.query(sql, params)]
