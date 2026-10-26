"""Замена имён и названий оригинала нейтральными ярлыками («Протагонист», «место 1»), чтобы скелет не нёс конкретики."""
from __future__ import annotations

import re

from bookmind.ingestion.text_utils import norm_key

TYPE_LABELS = {"place": "место", "organization": "организация", "state": "государство", "item": "предмет",
               "ability": "способность", "world_rule": "правило мира", "historical_event": "историческое событие", "term": "термин"}


def _variants(name: str) -> list[str]:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    out = [name.strip()]
    if len(parts) > 1:
        out += [p for p in parts if len(p) >= 3]
    return list(dict.fromkeys(out))


def _pattern(text: str, case_sensitive_first: bool) -> re.Pattern:
    """Русские окончания: отсекаем последнюю букву у слов от 4 символов и допускаем до 3 букв окончания."""
    words = []
    for w in text.split():
        core = re.escape(w[:-1] if len(w) >= 4 else w)
        tail = r"\w{0,3}" if len(w) >= 4 else r"\w{0,1}"
        words.append(core + tail)
    flags = 0 if case_sensitive_first else re.IGNORECASE
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", flags)


class Anonymizer:
    def __init__(self) -> None:
        self._rules: list[tuple[int, re.Pattern, str]] = []
        self.names: dict[str, str] = {}

    def add(self, names: list[str], label: str, *, person: bool) -> None:
        for n in names:
            for v in _variants(n):
                if len(v.strip()) < 3:
                    continue
                self._rules.append((len(v), _pattern(v, case_sensitive_first=person), label))
                self.names[norm_key(v)] = label
        self._rules.sort(key=lambda r: -r[0])

    def apply(self, text: str) -> str:
        for _, pat, label in self._rules:
            text = pat.sub(label, text)
        return text
