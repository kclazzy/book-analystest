"""Генераторы тестовых файлов EPUB/FB2/TXT из эталонных глав."""
from __future__ import annotations

import zipfile
from html import escape
from pathlib import Path

from tests.fixtures.novel import book_as_txt


def _xhtml(title: str, paras: list[str], extra_id: str | None = None) -> str:
    body = "".join(f"<p>{escape(p)}</p>" for p in paras)
    head = f'<h1 id="{extra_id}">{escape(title)}</h1>' if extra_id else f"<h1>{escape(title)}</h1>"
    return (f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{escape(title)}</title></head>'
            f"<body>{head}{body}</body></html>")


def write_txt(path: Path, chapters, encoding: str = "utf-8") -> Path:
    path.write_bytes(book_as_txt(chapters).encode(encoding))
    return path


def write_epub(path: Path, title: str, author: str, chapters, *, split_files: bool = True, with_nav: bool = True,
               with_toc: bool = True, with_ncx: bool = False, anchors_single_file: bool = False) -> Path:
    """split_files: каждая глава занимает 2 файла spine (в оглавлении только первый) + обложка и копирайт вне оглавления."""
    items: list[tuple[str, str, str]] = []  # (id, href, xhtml)
    toc: list[tuple[str, str]] = []
    items.append(("cover", "cover.xhtml", _xhtml("Обложка", [title])))
    if anchors_single_file:
        body = ""
        for i, (t, text) in enumerate(chapters):
            body += f'<h1 id="c{i}">{escape(t)}</h1>' + "".join(f"<p>{escape(p)}</p>" for p in text.split("\n\n"))
            toc.append((t, f"book.xhtml#c{i}"))
        items.append(("book", "book.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml"><body>{body}</body></html>'))
    else:
        for i, (t, text) in enumerate(chapters):
            paras = text.split("\n\n")
            if split_files and len(paras) > 2:
                half = len(paras) // 2
                items.append((f"c{i}a", f"text/c{i}a.xhtml", _xhtml(t, paras[:half])))
                items.append((f"c{i}b", f"text/c{i}b.xhtml", _xhtml("", paras[half:])))
            else:
                items.append((f"c{i}", f"text/c{i}.xhtml", _xhtml(t, paras)))
            toc.append((t, f"text/c{i}{'a' if split_files and len(paras) > 2 else ''}.xhtml"))
    items.append(("copy", "copyright.xhtml", _xhtml("Copyright", ["Все права защищены."])))

    manifest = "".join(f'<item id="{i}" href="{h}" media-type="application/xhtml+xml"/>' for i, h, _ in items)
    if with_nav:
        manifest += '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    if with_ncx:
        manifest += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    spine = "".join(f'<itemref idref="{i}"/>' for i, _, _ in items)
    opf = (f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
           f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{escape(title)}</dc:title>'
           f'<dc:creator>{escape(author)}</dc:creator><dc:language>ru</dc:language><dc:identifier id="id">x</dc:identifier></metadata>'
           f'<manifest>{manifest}</manifest><spine{" toc=" + chr(34) + "ncx" + chr(34) if with_ncx else ""}>{spine}</spine></package>')
    nav = ('<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
           '<nav epub:type="toc"><ol>' + "".join(f'<li><a href="{h}">{escape(t)}</a></li>' for t, h in toc) + "</ol></nav></body></html>")
    ncx = ('<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><navMap>'
           + "".join(f'<navPoint id="n{i}"><navLabel><text>{escape(t)}</text></navLabel><content src="{h}"/></navPoint>'
                     for i, (t, h) in enumerate(toc)) + "</navMap></ncx>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
                   '<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("content.opf", opf)
        for _, href, xhtml in items:
            z.writestr(href, xhtml)
        if with_nav and with_toc:
            z.writestr("nav.xhtml", nav)
        elif with_nav:
            z.writestr("nav.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body></body></html>')
        if with_ncx:
            z.writestr("toc.ncx", ncx if with_toc else '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap/></ncx>')
    return path


def write_fb2(path: Path, title: str, author: str, chapters, *, nested: bool = False, encoding: str = "utf-8") -> Path:
    first, last = (author.split(" ", 1) + [""])[:2]

    def sec(t, text):
        ps = "".join(f"<p>{escape(p)}</p>" for p in text.split("\n\n"))
        return f"<section><title><p>{escape(t)}</p></title>{ps}</section>"

    if nested:
        half = len(chapters) // 2
        parts = [("Часть первая", chapters[:half]), ("Часть вторая", chapters[half:])]
        body = "".join(f"<section><title><p>{p}</p></title>{''.join(sec(t, x) for t, x in chs)}</section>" for p, chs in parts)
    else:
        body = "".join(sec(t, x) for t, x in chapters)
    xml = (f'<?xml version="1.0" encoding="{encoding}"?><FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
           f"<description><title-info><genre>fantasy</genre><author><first-name>{escape(first)}</first-name>"
           f"<last-name>{escape(last)}</last-name></author><book-title>{escape(title)}</book-title><lang>ru</lang></title-info></description>"
           f"<body>{body}</body>"
           '<body name="notes"><section><title><p>Примечания</p></title><p>1. Тестовая сноска.</p></section></body>'
           '<binary id="cover.jpg" content-type="image/jpeg">AAAA</binary></FictionBook>')
    path.write_bytes(xml.encode(encoding))
    return path
