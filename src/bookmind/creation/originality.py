"""Проверка на сходство с оригиналом: имена героев/мест, близость событий и глав, совпадение «шинглов» текста.
Метод лексический (по основам слов): ловит копирование и перекраску имён, но не гарантирует обнаружение глубокого пересказа."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from bookmind.services.search import STOPWORDS, stem
from bookmind.storage.database import Database

_WORD = re.compile(r"\w+", re.UNICODE)
EXTRA_STOP = set("""его её ей им ним них ему нее неё она они оно мне нам вам вас нас меня тебя себя сам сама сами этот эта эти
этого этой этом того той тех там тут здесь еще ещё уже только очень был была были было будет будто если чтобы когда потом затем
после перед между через над под про без чем чего кого свой своя свои своё такой такая такие""".split())
SIM_THRESHOLD = {"weak": 0.65, "medium": 0.55, "strong": 0.45}
MIN_SHARED = 4
SHINGLE_N = 5
SHINGLE_THRESHOLD = 0.03


def content_stems(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall(text):
        lw = w.casefold()
        if len(lw) < 4 or lw in STOPWORDS or lw in EXTRA_STOP:
            continue
        out.add(stem(w))
    return out


def _shingles(text: str) -> set[int]:
    toks = [stem(w) for w in _WORD.findall(text)]
    return {hash(tuple(toks[i:i + SHINGLE_N])) for i in range(max(0, len(toks) - SHINGLE_N + 1))}


@dataclass
class Check:
    name_hits: list[str] = field(default_factory=list)        # герои оригинала (жёсткие)
    soft_hits: list[str] = field(default_factory=list)        # одиночные названия мест/предметов (предупреждение)
    similarity: float = 0.0
    matched: str = ""
    shingle_overlap: float = 0.0
    flagged: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class OriginalCorpus:
    def __init__(self, db: Database, series_id: str, book_ids: list[str]):
        self.db, self.series_id, self.book_ids = db, series_id, book_ids
        self._char_stems: dict[str, str] = {}         # основа слова -> исходное написание
        self._phrases: list[tuple[tuple[str, ...], str]] = []
        self._soft: dict[str, str] = {}
        self._load_names()
        self._items = self._load_items()
        self._shingles: set[int] | None = None

    def _load_names(self) -> None:
        for e in self.db.query("SELECT id, type, name FROM entities WHERE series_id = ? AND status != 'merged'", (self.series_id,)):
            names = [e["name"], *[a["alias"] for a in self.db.query("SELECT alias FROM entity_aliases WHERE entity_id = ?", (e["id"],))]]
            for name in names:
                toks = [t for t in _WORD.findall(name)]
                if not toks:
                    continue
                if e["type"] == "character":
                    for t in toks:
                        if len(t) >= 3 and t[0].isupper():
                            self._char_stems[stem(t)] = t
                elif len(toks) > 1:
                    self._phrases.append((tuple(stem(t) for t in toks), name))
                elif len(toks[0]) >= 4 and toks[0][0].isupper():
                    self._soft[stem(toks[0])] = toks[0]

    def _load_items(self) -> list[tuple[str, set[str]]]:
        names = set(self._char_stems) | {s for ph, _ in self._phrases for s in ph} | set(self._soft)
        items = []
        marks = ",".join("?" * len(self.book_ids))
        for r in self.db.query(f"SELECT description FROM events WHERE series_id = ? AND verification = 'verified' AND book_id IN ({marks})",
                               (self.series_id, *self.book_ids)):
            items.append((f"событие: {r['description']}", content_stems(r["description"]) - names))
        for r in self.db.query(f"SELECT content FROM report_versions WHERE series_id = ? AND kind IN ('chapter_summary','arc_summary') "
                               f"AND book_id IN ({marks})", (self.series_id, *self.book_ids)):
            items.append((f"сводка: {r['content']}", content_stems(r["content"]) - names))
        return [(label, s) for label, s in items if len(s) >= 5]

    def _corpus_shingles(self) -> set[int]:
        if self._shingles is None:
            marks = ",".join("?" * len(self.book_ids))
            self._shingles = set()
            for r in self.db.query(f"SELECT text FROM chunks WHERE book_id IN ({marks})", self.book_ids):
                self._shingles |= _shingles(r["text"])
        return self._shingles

    # ---- проверки ----
    def find_names(self, text: str) -> tuple[list[str], list[str]]:
        hard, soft = [], []
        words = list(_WORD.finditer(text))
        stems = [stem(m.group()) for m in words]
        for m, st in zip(words, stems):
            w = m.group()
            if not w[0].isupper():
                continue
            start = text[:m.start()].rstrip()
            sentence_start = not start or start[-1] in ".!?…—–-:\n«\""
            if st in self._char_stems and (not sentence_start or len(w) >= 5):
                hard.append(self._char_stems[st])
            elif st in self._soft and not sentence_start:
                soft.append(self._soft[st])
        low = [s for s in stems]
        for ph, name in self._phrases:
            n = len(ph)
            if any(tuple(low[i:i + n]) == ph for i in range(len(low) - n + 1)):
                hard.append(name)
        return sorted(set(hard)), sorted(set(soft))

    def similarity(self, text: str) -> tuple[float, str]:
        p = content_stems(text)
        best, label = 0.0, ""
        for lab, e in self._items:
            shared = len(e & p)
            if shared < MIN_SHARED:
                continue
            score = shared / len(e)
            if score > best:
                best, label = score, lab
        return round(best, 3), label[:200]

    def check(self, text: str, divergence: str = "medium", with_shingles: bool = False) -> Check:
        c = Check()
        c.name_hits, c.soft_hits = self.find_names(text)
        c.similarity, c.matched = self.similarity(text)
        if with_shingles:
            sh = _shingles(text)
            c.shingle_overlap = round(len(sh & self._corpus_shingles()) / len(sh), 3) if sh else 0.0
        if c.name_hits:
            c.reasons.append("в тексте имена или названия из оригинала: " + ", ".join(c.name_hits))
        if c.similarity >= SIM_THRESHOLD[divergence]:
            c.reasons.append(f"содержание слишком близко к оригиналу ({c.similarity:.0%}): {c.matched}")
        if c.shingle_overlap >= SHINGLE_THRESHOLD:
            c.reasons.append(f"дословные совпадения с текстом оригинала: {c.shingle_overlap:.1%}")
        c.flagged = bool(c.reasons)
        return c
