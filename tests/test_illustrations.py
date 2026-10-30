import zipfile

import pytest

pytest.importorskip("PIL")

from bookmind.creation import book_export as be
from bookmind.creation.illustrations import chapter_art, cover_art
from bookmind.ingestion.epub import parse_epub

PNG = b"\x89PNG\r\n\x1a\n"


def test_chapter_art_is_png_deterministic_and_varied():
    a, b = chapter_art(1, 0.1), chapter_art(1, 0.1)
    assert a.startswith(PNG) and a == b
    assert chapter_art(2, 0.1) != a and chapter_art(1, 0.9) != a
    assert len(a) < 200_000


def test_cover_art_has_title_and_author_png():
    c = cover_art("Тишина", "Mister.Ti", seed=5)
    assert c.startswith(PNG) and len(c) < 1_500_000
    assert cover_art("Другое название", "Mister.Ti", seed=5) != c


def _book():
    ch = [be.BookChapter("Глава 1", "Первый абзац.\n\nВторой абзац.", 1, image=chapter_art(1, 0.0)),
          be.BookChapter("Глава 2", "Текст второй главы.", 2)]
    return be.Book("Тест", ch, annotation="Аннотация", author="Mister.Ti", cover=cover_art("Тест", "Mister.Ti", 1))


def test_epub_contains_cover_images_and_author(tmp_path):
    p = tmp_path / "b.epub"
    be.export_book(_book(), p)
    with zipfile.ZipFile(p) as z:
        names = z.namelist()
        assert "OEBPS/images/cover.png" in names and "OEBPS/images/ch001.png" in names and "OEBPS/images/ch002.png" not in names
        opf = z.read("OEBPS/content.opf").decode("utf-8")
        assert 'properties="cover-image"' in opf and "<dc:creator>Mister.Ti</dc:creator>" in opf and 'name="cover"' in opf
        assert '<img src="images/ch001.png"' in z.read("OEBPS/ch001.xhtml").decode("utf-8")
    parsed = parse_epub(p)
    assert parsed.author == "Mister.Ti" and len(parsed.chapters) == 2


def test_fb2_has_binary_cover_and_images(tmp_path):
    p = tmp_path / "b.fb2"
    be.export_book(_book(), p)
    x = p.read_text(encoding="utf-8")
    assert '<coverpage><image l:href="#cover.png"/></coverpage>' in x and x.count("<binary ") == 2 and "Mister.Ti" in x


def test_markdown_and_txt_ignore_images(tmp_path):
    p = tmp_path / "b.md"
    be.export_book(_book(), p)
    assert "Первый абзац." in p.read_text(encoding="utf-8")


def test_unnumbered_chapters_keep_their_own_heading():
    b = be.Book("К", [be.BookChapter("Пролог. Начало", "Текст.", -1), be.BookChapter("Глава 1", "Текст.", 1), be.BookChapter("Эпилог", "Конец.", -1)])
    assert [b.heading(i) for i in range(3)] == ["Пролог. Начало", "Глава 1", "Эпилог"]


# ---------- требования площадок: язык, авторские права, оглавление ----------
def _en_book():
    return be.Book(title="Test Title", author="A. Writer", language="en", chapter_label="Chapter", toc_label="Contents",
                   rights="Copyright © 2026 A. Writer. All rights reserved.",
                   chapters=[be.BookChapter("Prologue. Start", "Text one.", -1), be.BookChapter("Chapter 1. Road", "Text two.", 1),
                             be.BookChapter("Road", "Text three.", 2)])


def test_headings_use_the_book_language():
    b = _en_book()
    assert [b.heading(i) for i in range(3)] == ["Prologue. Start", "Chapter 1. Road", "Chapter 2. Road"]
    assert be.to_markdown(b).count("## Chapter") == 2


def test_epub_has_rights_page_visible_contents_and_language(tmp_path):
    p = be.export_book(_en_book(), tmp_path / "b.epub")
    z = zipfile.ZipFile(p)
    opf = z.read("OEBPS/content.opf").decode()
    assert "<dc:language>en</dc:language>" in opf and 'idref="rights"' in opf and 'idref="contents"' in opf
    assert opf.index('idref="title"') < opf.index('idref="rights"') < opf.index('idref="contents"') < opf.index('idref="ch001"')
    assert "Copyright © 2026" in z.read("OEBPS/rights.xhtml").decode()
    c = z.read("OEBPS/contents.xhtml").decode()
    assert "Contents" in c and 'href="ch002.xhtml"' in c and "Chapter 2. Road" in c
    assert 'lang="en"' in z.read("OEBPS/ch001.xhtml").decode()


def test_epub_without_rights_has_no_rights_page(tmp_path):
    b = _en_book()
    b.rights = ""
    z = zipfile.ZipFile(be.export_book(b, tmp_path / "b.epub"))
    assert "OEBPS/rights.xhtml" not in z.namelist() and 'idref="rights"' not in z.read("OEBPS/content.opf").decode()


def test_reimport_ignores_front_matter(tmp_path):
    b = parse_epub(be.export_book(_en_book(), tmp_path / "b.epub"))
    assert [c.title for c in b.chapters][-1] == "Chapter 2. Road" and len(b.chapters) == 3


def test_epub_is_valid_for_epubcheck(tmp_path):
    ec = pytest.importorskip("epubcheck")
    r = ec.EpubCheck(str(be.export_book(_en_book(), tmp_path / "b.epub")))
    assert r.valid, r.messages


def test_cover_size_can_follow_store_requirements():
    pytest.importorskip("PIL")
    from io import BytesIO

    from PIL import Image
    png = cover_art("A Title", "A. Writer", seed=1, size=(1600, 2560))
    assert Image.open(BytesIO(png)).size == (1600, 2560)
