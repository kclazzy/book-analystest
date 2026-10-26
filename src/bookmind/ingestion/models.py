"""Результат разбора книги (до сохранения в БД)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedChapter:
    title: str
    raw_text: str


@dataclass
class ParsedBook:
    title: str
    author: str = ""
    language: str = ""
    format: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    chapters: list[ParsedChapter] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    transformations: list[str] = field(default_factory=list)
