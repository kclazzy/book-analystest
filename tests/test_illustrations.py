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
