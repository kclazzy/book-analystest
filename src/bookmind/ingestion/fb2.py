"""Импорт FB2 (в том числе .fb2.zip). Листовые секции становятся главами, примечания (body notes) пропускаются."""
from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from bookmind.ingestion.models import ParsedBook, ParsedChapter

_BLOCK_TAGS = {"p", "v", "subtitle", "text-author", "th", "td"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _inline_text(el: ET.Element) -> str:
    parts: list[str] = [el.text or ""]
    for child in el:
        name = _local(child.tag)
        if name == "a" and child.get("type") == "note":
            pass  # ссылка на сноску: текст [1] не включаем
        elif name == "image":
            pass
        else:
            parts.append(_inline_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _paragraphs(el: ET.Element) -> list[str]:
    """Абзацы элемента-контейнера (epigraph, cite, poem и т.д.) в порядке документа."""
    out: list[str] = []
    for child in el:
        name = _local(child.tag)
        if name in _BLOCK_TAGS:
            t = " ".join(_inline_text(child).split())
            if t:
                out.append(t)
        elif name == "empty-line":
            out.append("")
        elif name in ("epigraph", "cite", "poem", "stanza", "annotation", "table", "tr"):
            out.extend(_paragraphs(child))
        elif name == "title":
            continue
    return out


def _title_of(section: ET.Element) -> str:
    for child in section:
        if _local(child.tag) == "title":
            return " ".join(" ".join(_inline_text(p) for p in child.iter() if _local(p.tag) == "p").split())
    return ""


def _walk(section: ET.Element, trail: list[str], out: list[ParsedChapter]) -> None:
    title = _title_of(section)
    trail = trail + ([title] if title else [])
    subs = [c for c in section if _local(c.tag) == "section"]
    own = [c for c in section if _local(c.tag) != "section"]
    box = ET.Element("box")
    box.extend(own)
    paras = _paragraphs(box)
    text = "\n\n".join(p for p in paras)
    if not subs:
        out.append(ParsedChapter(" / ".join(trail) or f"Глава {len(out) + 1}", text))
        return
    if text.strip():
        out.append(ParsedChapter(" / ".join(trail) or "Начало", text))
    for sub in subs:
        _walk(sub, trail, out)


def parse_fb2(path: Path) -> ParsedBook:
    data = path.read_bytes()
    transformations: list[str] = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".fb2")), None)
            if name is None:
                raise ValueError("В архиве нет .fb2 файла")
            data = z.read(name)
            transformations.append("unzipped_fb2")
    root = ET.fromstring(data)
    ti = next((e for e in root.iter() if _local(e.tag) == "title-info"), None)

    def text_of(parent, tag):
        if parent is None:
            return ""
        el = next((e for e in parent if _local(e.tag) == tag), None)
        return " ".join((el.text or "").split()) if el is not None and el.text else ""

    author = ""
    if ti is not None:
        a = next((e for e in ti if _local(e.tag) == "author"), None)
        if a is not None:
            author = " ".join(filter(None, (text_of(a, "first-name"), text_of(a, "middle-name"), text_of(a, "last-name"))))
    book = ParsedBook(
        title=text_of(ti, "book-title") or path.stem, author=author, language=text_of(ti, "lang"), format="fb2",
        metadata={"genre": text_of(ti, "genre")}, transformations=transformations,
    )
    chapters: list[ParsedChapter] = []
    for body in (e for e in root if _local(e.tag) == "body"):
        if body.get("name") == "notes":
            book.warnings.append("Раздел примечаний пропущен")
            continue
        top_secs = [c for c in body if _local(c.tag) == "section"]
        if not top_secs:
            text = "\n\n".join(_paragraphs(body))
            if text.strip():
                chapters.append(ParsedChapter(book.title, text))
        for sec in top_secs:
            _walk(sec, [], chapters)
    book.chapters = [c for c in chapters if c.raw_text.strip()]
    if not book.chapters:
        book.warnings.append("Текст в FB2 не найден")
    return book
