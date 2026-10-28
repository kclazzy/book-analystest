"""Экспорт готовой новой книги: Markdown, TXT, FB2, EPUB."""
from __future__ import annotations

import base64
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from bookmind.domain.exceptions import BookMindError

AUTHOR = "BookMind"
FORMATS = {".md": "Markdown", ".txt": "Текст", ".fb2": "FictionBook (FB2)", ".epub": "EPUB"}


@dataclass
class BookChapter:
    title: str
    text: str
    number: int = 0          # номер в плане; 0 — по порядку; меньше нуля — без номера (пролог, эпилог)
    image: bytes | None = None   # PNG-иллюстрация в начале главы (EPUB, FB2)


@dataclass
class Book:
    title: str
    chapters: list[BookChapter] = field(default_factory=list)
    annotation: str = ""
    author: str = AUTHOR
    language: str = "ru"
    cover: bytes | None = None   # PNG-обложка (EPUB, FB2)

    def heading(self, i: int) -> str:
        if self.chapters[i].number < 0:           # пролог, эпилог и т. п.: без номера, заголовок как есть
            return self.chapters[i].title.strip()
        t = re.sub(r"^\s*глава\s*\d+\s*[.:—–-]*\s*", "", self.chapters[i].title, flags=re.I).strip()
        return f"Глава {self.chapters[i].number or i + 1}" + (f". {t}" if t else "")


def paragraphs(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]


def to_markdown(book: Book) -> str:
    out = [f"# {book.title}", ""]
    if book.annotation:
        out += [f"*{book.annotation}*", ""]
    for i, c in enumerate(book.chapters):
        out += [f"## {book.heading(i)}", "", *[p + "\n" for p in paragraphs(c.text)]]
    return "\n".join(out).rstrip() + "\n"


def to_text(book: Book) -> str:
    out = [book.title.upper(), ""]
    if book.annotation:
        out += [book.annotation, ""]
    for i, c in enumerate(book.chapters):
        out += ["", book.heading(i), "", *[p + "\n" for p in paragraphs(c.text)]]
    return "\n".join(out).rstrip() + "\n"


def _b64(data: bytes) -> str:
    return base64.encodebytes(data).decode("ascii")


def to_fb2(book: Book) -> str:
    secs, binaries = [], []
    if book.cover:
        binaries.append(f'<binary id="cover.png" content-type="image/png">{_b64(book.cover)}</binary>')
    for i, c in enumerate(book.chapters):
        ps = "".join(f"<p>{escape(p)}</p>" for p in paragraphs(c.text))
        img = ""
        if c.image:
            binaries.append(f'<binary id="ch{i + 1:03d}.png" content-type="image/png">{_b64(c.image)}</binary>')
            img = f'<image l:href="#ch{i + 1:03d}.png"/>'
        secs.append(f"<section><title><p>{escape(book.heading(i))}</p></title>{img}{ps}</section>")
    cover = '<coverpage><image l:href="#cover.png"/></coverpage>' if book.cover else ""
    ann = f"<annotation><p>{escape(book.annotation)}</p></annotation>" if book.annotation else ""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return ('<?xml version="1.0" encoding="utf-8"?>\n<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">'
            f"<description><title-info><genre>prose_contemporary</genre><author><first-name>{escape(book.author)}</first-name>"
            f"<last-name></last-name></author><book-title>{escape(book.title)}</book-title>{ann}{cover}<lang>{book.language}</lang></title-info>"
            f"<document-info><author><nickname>{escape(book.author)}</nickname></author><date>{date}</date><id>{uuid.uuid4()}</id>"
            f"<version>1.0</version></document-info></description><body><title><p>{escape(book.title)}</p></title>{''.join(secs)}</body>{''.join(binaries)}</FictionBook>\n")


def _xhtml(title: str, body: str) -> str:
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="ru">'
            f"<head><meta charset=\"utf-8\"/><title>{escape(title)}</title></head><body>{body}</body></html>")


def write_epub(book: Book, path: Path) -> None:
    uid = f"urn:uuid:{uuid.uuid4()}"
    items, nav_li, ncx_pts = [], [], []
    files: dict[str, str] = {}
    images: dict[str, bytes] = {}
    if book.cover:
        images["images/cover.png"] = book.cover
    for i, c in enumerate(book.chapters):
        name = f"ch{i + 1:03d}.xhtml"
        h = book.heading(i)
        pic = ""
        if c.image:
            pic = f'<p style="text-align:center"><img src="images/ch{i + 1:03d}.png" alt="" style="max-width:100%"/></p>'
            images[f"images/ch{i + 1:03d}.png"] = c.image
        body = f"<h2>{escape(h)}</h2>" + pic + "".join(f"<p>{escape(p)}</p>" for p in paragraphs(c.text))
        files[name] = _xhtml(h, body)
        items.append((f"ch{i + 1:03d}", name))
        nav_li.append(f'<li><a href="{name}">{escape(h)}</a></li>')
        ncx_pts.append(f'<navPoint id="n{i + 1}" playOrder="{i + 2}"><navLabel><text>{escape(h)}</text></navLabel><content src="{name}"/></navPoint>')
    title_page = _xhtml(book.title, f"<h1>{escape(book.title)}</h1><p><i>{escape(book.author)}</i></p>"
                        + (f"<p>{escape(book.annotation)}</p>" if book.annotation else ""))
    nav = _xhtml("Оглавление", f'<nav epub:type="toc"><h2>Оглавление</h2><ol>{"".join(nav_li)}</ol></nav>')
    ncx = ('<?xml version="1.0" encoding="utf-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           f'<head><meta name="dtb:uid" content="{uid}"/></head><docTitle><text>{escape(book.title)}</text></docTitle><navMap>'
           f'<navPoint id="n0" playOrder="1"><navLabel><text>{escape(book.title)}</text></navLabel><content src="title.xhtml"/></navPoint>'
           f'{"".join(ncx_pts)}</navMap></ncx>')
    manifest = ('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
                '<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>'
                + "".join(f'<item id="{i}" href="{n}" media-type="application/xhtml+xml"/>' for i, n in items)
                + "".join(f'<item id="img{k}" href="{n}" media-type="image/png"' + (' properties="cover-image"' if n.endswith("cover.png") else "") + "/>"
                          for k, n in enumerate(images)))
    spine = '<itemref idref="title"/>' + "".join(f'<itemref idref="{i}"/>' for i, _ in items)
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:identifier id="uid">{uid}</dc:identifier><dc:title>{escape(book.title)}</dc:title>'
           f'<dc:creator>{escape(book.author)}</dc:creator><dc:language>{book.language}</dc:language>'
           + ('<meta name="cover" content="img0"/>' if book.cover else "") +
           f'<meta property="dcterms:modified">{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</meta></metadata>'
           f'<manifest>{manifest}</manifest><spine toc="ncx">{spine}</spine></package>')
    container = ('<?xml version="1.0" encoding="utf-8"?>\n<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)   # первым и без сжатия
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/title.xhtml", title_page, compress_type=zipfile.ZIP_DEFLATED)
        for name, content in files.items():
            z.writestr(f"OEBPS/{name}", content, compress_type=zipfile.ZIP_DEFLATED)
        for name, data in images.items():
            z.writestr(f"OEBPS/{name}", data, compress_type=zipfile.ZIP_STORED)


def export_book(book: Book, path: Path) -> Path:
    """Формат выбирается по расширению файла: .md, .txt, .fb2, .epub."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in FORMATS:
        raise BookMindError("Неизвестный формат: " + (ext or "без расширения") + ". Доступны: " + ", ".join(FORMATS))
    if not book.chapters:
        raise BookMindError("В книге ещё нет написанных глав")
    path.parent.mkdir(parents=True, exist_ok=True)
    if ext == ".epub":
        write_epub(book, path)
    else:
        text = {".md": to_markdown, ".txt": to_text, ".fb2": to_fb2}[ext](book)
        path.write_text(text, encoding="utf-8")
    return path
