"""Проверка цитат модели по оригинальному тексту фрагмента."""
from __future__ import annotations

from dataclasses import dataclass

from bookmind.ingestion.text_utils import build_index, find_quote


@dataclass(frozen=True)
class QuoteCheck:
    verified: bool
    start: int | None = None
    end: int | None = None
    reason: str = ""


class QuoteVerifier:
    def __init__(self, text: str):
        self.text = text
        self._index = build_index(text)

    def check(self, quote: str) -> QuoteCheck:
        quote = (quote or "").strip()
        if not quote:
            return QuoteCheck(False, reason="цитата не указана")
        hit = find_quote(quote, self._index, len(self.text))
        if hit is None:
            return QuoteCheck(False, reason="цитата не найдена в оригинале")
        return QuoteCheck(True, hit[0], hit[1])
