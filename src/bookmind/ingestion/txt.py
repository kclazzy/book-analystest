"""Импорт TXT: определение кодировки и глав по заголовкам."""
from __future__ import annotations

import re
from pathlib import Path

from bookmind.ingestion.models import ParsedBook, ParsedChapter
from bookmind.ingestion.text_utils import decode_bytes

_WORD_HEADING = re.compile(
    r"^\s*(глава|часть|книга|том|пролог|эпилог|интерлюдия|предисловие|послесловие|prologue|epilogue|chapter|part|book|interlude)\b[^\n]{0,70}$",
    re.IGNORECASE,
)
_ROMAN = re.compile(r"^\s*[IVXLCDM]{1,8}\.?\s*$")
_NUMBER = re.compile(r"^\s*\d{1,3}[.)]?\s*$")


def is_heading_line(line: str, prev_blank: bool, next_blank: bool) -> bool:
    s = line.strip()
    if not s or len(s) > 80 or not prev_blank:
        return False
    if _WORD_HEADING.match(s):
        return True
    if (_ROMAN.match(s) or _NUMBER.match(s)) and next_blank:
        return True
    return False


def split_by_headings(text: str, default_title: str) -> tuple[list[ParsedChapter], list[str]]:
    """Делит текст на главы по строкам-заголовкам. Возвращает (главы, предупреждения)."""
    lines = text.split("\n")
    headings: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        prev_blank = i == 0 or not lines[i - 1].strip()
        next_blank = i + 1 >= len(lines) or not lines[i + 1].strip()
        if is_heading_line(line, prev_blank, next_blank):
            headings.append((i, line.strip()))
    warnings: list[str] = []
    if not headings:
        warnings.append("Заголовки глав не найдены: книга импортирована как одна глава, исправьте границы вручную")
        return [ParsedChapter(default_title, text)], warnings
    chapters: list[ParsedChapter] = []
    if headings[0][0] > 0:
        head = "\n".join(lines[: headings[0][0]]).strip()
        if head:
            chapters.append(ParsedChapter("Начало", head))
    for n, (line_no, title) in enumerate(headings):
        end = headings[n + 1][0] if n + 1 < len(headings) else len(lines)
        body = "\n".join(lines[line_no + 1 : end]).strip()
        chapters.append(ParsedChapter(title, body))
    return [c for c in chapters if c.raw_text.strip()], warnings


def parse_txt(path: Path) -> ParsedBook:
    data = path.read_bytes()
    text, encoding = decode_bytes(data)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    title = path.stem
    book = ParsedBook(title=title, format="txt", metadata={"encoding": encoding})
    if encoding not in ("utf-8", "utf-8-sig"):
        book.warnings.append(f"Кодировка определена как {encoding}")
        book.transformations.append(f"decoded_from_{encoding}")
    chapters, warnings = split_by_headings(text, title)
    book.chapters, book.warnings = chapters, book.warnings + warnings
    if not lines:
        book.warnings.append("Файл пуст")
    return book
