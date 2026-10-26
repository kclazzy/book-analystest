"""Импорт EPUB. Главы определяются по оглавлению (nav/NCX), а не по каждому файлу spine."""
from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from bookmind.ingestion.html_text import HtmlText, html_to_text
from bookmind.ingestion.models import ParsedBook, ParsedChapter
from bookmind.ingestion.txt import split_by_headings
from bookmind.ingestion.text_utils import decode_bytes

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "x": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
}
_SERVICE_TITLES = re.compile(
    r"^(cover|обложка|title\s*page|титул|copyright|права|оглавление|содержание|contents|table of contents|"
    r"аннотация|annotation|about the author|об авторе)\b", re.IGNORECASE)
MIN_FRONT_MATTER_CHARS = 500
MIN_SPINE_CHAPTER_CHARS = 1000


@dataclass
class _Doc:
    path: str
    text: str
    anchors: dict[str, int]
    headings: list[tuple[int, int, str]]
    offset: int = 0


def _resolve(base_file: str, href: str) -> tuple[str, str]:
    path, _, frag = href.partition("#")
    path = unquote(path)
    full = posixpath.normpath(posixpath.join(posixpath.dirname(base_file), path)) if path else base_file
    return full, unquote(frag)


def _read(z: zipfile.ZipFile, name: str) -> bytes:
    try:
        return z.read(name)
    except KeyError:
        for n in z.namelist():  # некоторые EPUB различаются регистром/кодированием пути
            if n.lower() == name.lower():
                return z.read(n)
        raise


def _toc_nav(z, nav_path: str) -> list[tuple[str, str]]:
    root = ET.fromstring(_read(z, nav_path))
    nav = None
    for n in root.iter(f"{{{NS['x']}}}nav"):
        if n.get(f"{{{NS['epub']}}}type") == "toc" or nav is None:
            nav = n
            if n.get(f"{{{NS['epub']}}}type") == "toc":
                break
    entries: list[tuple[str, str]] = []
    if nav is None:
        return entries
    for a in nav.iter(f"{{{NS['x']}}}a"):
        href = a.get("href")
        title = " ".join("".join(a.itertext()).split())
        if href:
            entries.append((title, href))
    return entries


def _toc_ncx(z, ncx_path: str) -> list[tuple[str, str]]:
    root = ET.fromstring(_read(z, ncx_path))
    entries: list[tuple[str, str]] = []
    for np_ in root.iter(f"{{{NS['ncx']}}}navPoint"):
        label = np_.find("ncx:navLabel/ncx:text", NS)
        content = np_.find("ncx:content", NS)
        if content is not None and content.get("src"):
            title = " ".join((label.text or "").split()) if label is not None else ""
            entries.append((title, content.get("src")))
    return entries


def parse_epub(path: Path) -> ParsedBook:
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(_read(z, "META-INF/container.xml"))
        rootfile = container.find(".//c:rootfile", NS)
        if rootfile is None:
            raise ValueError("EPUB: не найден OPF-файл")
        opf_path = rootfile.get("full-path")
        opf = ET.fromstring(_read(z, opf_path))
        md = opf.find("opf:metadata", NS)

        def meta(tag: str) -> str:
            el = md.find(f"dc:{tag}", NS) if md is not None else None
            return " ".join((el.text or "").split()) if el is not None and el.text else ""

        title = meta("title") or path.stem
        book = ParsedBook(title=title, author=meta("creator"), language=meta("language"), format="epub",
                          metadata={"publisher": meta("publisher"), "date": meta("date"), "identifier": meta("identifier")})

        manifest: dict[str, dict] = {}
        for item in opf.findall("opf:manifest/opf:item", NS):
            href = _resolve(opf_path, item.get("href", ""))[0]
            manifest[item.get("id")] = {"href": href, "type": item.get("media-type", ""), "props": item.get("properties", "")}
        spine_ids = [ir.get("idref") for ir in opf.findall("opf:spine/opf:itemref", NS)]
        spine = [manifest[i] for i in spine_ids if i in manifest and "html" in manifest[i]["type"]]
        if not spine:
            raise ValueError("EPUB: пустой spine")

        docs: list[_Doc] = []
        for item in spine:
            raw, _enc = decode_bytes(_read(z, item["href"]))
            parsed: HtmlText = html_to_text(raw)
            docs.append(_Doc(item["href"], parsed.text, parsed.anchors, parsed.headings))

        toc: list = []
        nav_item = next((m for m in manifest.values() if "nav" in m["props"].split()), None)
        ncx_id = opf.find("opf:spine", NS).get("toc") if opf.find("opf:spine", NS) is not None else None
        ncx_item = manifest.get(ncx_id) if ncx_id else next((m for m in manifest.values() if m["type"] == "application/x-dtbncx+xml"), None)
        for source, parser in ((nav_item, _toc_nav), (ncx_item, _toc_ncx)):
            if source and not toc:
                try:
                    base = source["href"]
                    toc = [(t, _resolve(base, h)) for t, h in parser(z, base)]
                except (KeyError, ET.ParseError):
                    toc = []

    # ---- склейка документов в единый текст ----
    full = ""
    for d in docs:
        if full:
            full += "\n\n"
        d.offset = len(full)
        full += d.text
    # служебные файлы в самом конце (копирайт, выходные данные) не принадлежат последней главе
    for d in reversed(docs):
        if d.headings and _SERVICE_TITLES.match(d.headings[0][2]) and len(d.text) < MIN_FRONT_MATTER_CHARS:
            full = full[: d.offset].rstrip()
            book.warnings.append(f"Служебный раздел «{d.headings[0][2]}» в конце книги пропущен")
        else:
            break
    index_by_path = {d.path: d for d in docs}
    heading_at = {d.offset + pos: txt for d in docs for _lvl, pos, txt in d.headings}

    targets: list[tuple[int, str]] = []
    for t, (fpath, frag) in toc:
        d = index_by_path.get(fpath)
        if d is None:
            continue
        pos = d.offset + (d.anchors.get(frag, 0) if frag else 0)
        targets.append((pos, t))
    # уникальные позиции в порядке текста (оглавление может ссылаться на одну точку несколько раз)
    seen: set[int] = set()
    uniq = []
    for pos, t in sorted(targets, key=lambda x: x[0]):
        if pos not in seen:
            seen.add(pos)
            uniq.append((pos, t))
    targets = uniq

    chapters: list[ParsedChapter] = []
    if len(targets) >= 2:
        if targets[0][0] > 0:
            head = full[: targets[0][0]].strip()
            if len(head) >= MIN_FRONT_MATTER_CHARS:
                chapters.append(ParsedChapter("Начало", head))
            elif head:
                book.warnings.append("Короткий вводный блок (обложка/титул) пропущен")
        for i, (pos, t) in enumerate(targets):
            end = targets[i + 1][0] if i + 1 < len(targets) else len(full)
            body = full[pos:end].strip()
            if pos in heading_at and body.startswith(heading_at[pos]):
                body = body[len(heading_at[pos]):].strip()  # заголовок главы хранится в title, а не в тексте
            if not body:
                continue
            if _SERVICE_TITLES.match(t or "") and len(body) < MIN_FRONT_MATTER_CHARS:
                book.warnings.append(f"Служебный раздел «{t}» пропущен")
                continue
            chapters.append(ParsedChapter(t or f"Глава {len(chapters) + 1}", body))
        book.transformations.append("chapters_from_toc")
    else:
        # без оглавления: заголовки h1/h2 в HTML
        heads = []
        for d in docs:
            heads += [(d.offset + pos, lvl, txt) for lvl, pos, txt in d.headings]
        levels = sorted({lvl for _, lvl, _ in heads})
        use = next((lv for lv in levels if sum(1 for _, l, _ in heads if l == lv) >= 2), None)
        if use is not None:
            marks = [(p, t) for p, l, t in heads if l == use]
            if marks[0][0] > 0:
                head = full[: marks[0][0]].strip()
                if len(head) >= MIN_FRONT_MATTER_CHARS:
                    chapters.append(ParsedChapter("Начало", head))
            for i, (pos, t) in enumerate(marks):
                end = marks[i + 1][0] if i + 1 < len(marks) else len(full)
                body = full[pos:end].strip()
                body = body[len(t):].strip() if body.startswith(t) else body
                if not body:
                    continue
                if _SERVICE_TITLES.match(t) and len(body) < MIN_FRONT_MATTER_CHARS:
                    book.warnings.append(f"Служебный раздел «{t}» пропущен")
                    continue
                chapters.append(ParsedChapter(t, body))
            book.transformations.append(f"chapters_from_h{use}")
        else:
            chapters, w = split_by_headings(full, title)
            book.warnings += w
            if len(chapters) <= 1:
                # крайний случай: файлы spine, мелкие склеиваются с предыдущими
                chapters = []
                for d in docs:
                    if chapters and len(d.text) < MIN_SPINE_CHAPTER_CHARS:
                        chapters[-1].raw_text += "\n\n" + d.text
                    elif d.text.strip():
                        chapters.append(ParsedChapter(f"Раздел {len(chapters) + 1}", d.text))
                book.warnings.append("Структура глав в EPUB не определена, проверьте границы вручную")
                book.transformations.append("chapters_from_spine_merged")
    book.chapters = chapters
    if not chapters:
        book.warnings.append("Текст в EPUB не найден")
    return book
