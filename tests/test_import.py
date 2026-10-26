import json

import pytest

from bookmind.ingestion.epub import parse_epub
from bookmind.ingestion.fb2 import parse_fb2
from bookmind.ingestion.txt import parse_txt
from bookmind.services.library import ImportError_, book_uuid, sha256_file
from tests.fixtures.builders import write_epub, write_fb2, write_txt
from tests.fixtures.novel import BOOK1, BOOK1_AUTHOR, BOOK1_TITLE

TITLES = [t for t, _ in BOOK1]


def _words(s):
    return " ".join(s.split())


# ---------- TXT ----------
def test_txt_chapters_in_order(tmp_path):
    p = write_txt(tmp_path / "b.txt", BOOK1)
    book = parse_txt(p)
    assert [c.title for c in book.chapters] == TITLES
    assert _words(book.chapters[0].raw_text) == _words(BOOK1[0][1])


def test_txt_cp1251(tmp_path):
    p = write_txt(tmp_path / "b.txt", BOOK1, encoding="cp1251")
    book = parse_txt(p)
    assert book.metadata["encoding"] == "cp1251"
    assert [c.title for c in book.chapters] == TITLES


def test_txt_without_headings_single_chapter_with_warning(tmp_path):
    p = tmp_path / "plain.txt"
    p.write_text("Просто длинный текст без глав. " * 50, encoding="utf-8")
    book = parse_txt(p)
    assert len(book.chapters) == 1 and book.warnings


def test_txt_numeric_and_roman_headings(tmp_path):
    p = tmp_path / "n.txt"
    p.write_text("Пролог\n\nТекст пролога достаточно длинный для проверки.\n\n1\n\nПервая глава рассказа.\n\nII.\n\nВторая глава.", encoding="utf-8")
    assert [c.title for c in parse_txt(p).chapters] == ["Пролог", "1", "II."]


def test_txt_long_line_with_word_chapter_is_not_heading(tmp_path):
    p = tmp_path / "n.txt"
    p.write_text("Глава 1\n\nГлава семьи собралась за столом, и разговор долго не мог найти нужных слов для начала беседы вечером.\n\nКонец.", encoding="utf-8")
    assert [c.title for c in parse_txt(p).chapters] == ["Глава 1"]


# ---------- EPUB ----------
def test_epub_toc_nav_does_not_make_chapter_per_spine_file(tmp_path):
    p = write_epub(tmp_path / "b.epub", BOOK1_TITLE, BOOK1_AUTHOR, BOOK1, split_files=True)
    book = parse_epub(p)
    assert book.title == BOOK1_TITLE and book.author == BOOK1_AUTHOR and book.language == "ru"
    assert [c.title for c in book.chapters] == TITLES  # 8, хотя в spine > 16 файлов
    assert "Все права защищены" not in book.chapters[-1].raw_text  # служебные страницы не попали в главу
    assert "Обложка" not in " ".join(c.title for c in book.chapters)
    for c, (_, src) in zip(book.chapters, BOOK1):
        assert _words(src.split("\n\n")[0])[:60] in _words(c.raw_text)


def test_epub_ncx_fallback(tmp_path):
    p = write_epub(tmp_path / "b.epub", BOOK1_TITLE, BOOK1_AUTHOR, BOOK1, with_nav=False, with_ncx=True)
    assert [c.title for c in parse_epub(p).chapters] == TITLES


def test_epub_anchors_inside_single_file(tmp_path):
    p = write_epub(tmp_path / "b.epub", BOOK1_TITLE, BOOK1_AUTHOR, BOOK1, anchors_single_file=True)
    book = parse_epub(p)
    assert [c.title for c in book.chapters] == TITLES
    assert "ключ" in book.chapters[0].raw_text and "Тихон" not in book.chapters[0].raw_text


def test_epub_without_toc_uses_headings(tmp_path):
    p = write_epub(tmp_path / "b.epub", BOOK1_TITLE, BOOK1_AUTHOR, BOOK1, split_files=False, with_toc=False)
    book = parse_epub(p)
    assert [c.title for c in book.chapters] == TITLES
    assert book.chapters[1].raw_text.startswith("Дома Миру ждала")


def test_corrupt_epub_raises(tmp_path):
    p = tmp_path / "bad.epub"
    p.write_bytes(b"not a zip")
    with pytest.raises(Exception):
        parse_epub(p)


# ---------- FB2 ----------
def test_fb2_chapters_and_notes_skipped(tmp_path):
    p = write_fb2(tmp_path / "b.fb2", BOOK1_TITLE, BOOK1_AUTHOR, BOOK1)
    book = parse_fb2(p)
    assert [c.title for c in book.chapters] == TITLES
    assert book.author == BOOK1_AUTHOR and book.title == BOOK1_TITLE
    assert all("Тестовая сноска" not in c.raw_text for c in book.chapters)
    assert any("примечан" in w.lower() for w in book.warnings)


def test_fb2_nested_sections_and_cp1251(tmp_path):
    p = write_fb2(tmp_path / "b.fb2", BOOK1_TITLE, BOOK1_AUTHOR, BOOK1, nested=True, encoding="windows-1251")
    book = parse_fb2(p)
    assert len(book.chapters) == 8
    assert book.chapters[0].title.startswith("Часть первая / Глава 1")
    assert book.chapters[-1].title.startswith("Часть вторая / Глава 8")


# ---------- сервис библиотеки ----------
@pytest.mark.parametrize("fmt", ["txt", "epub", "fb2"])
def test_library_import_preserves_order_and_text(library, db, tmp_path, fmt):
    writers = {"txt": lambda p: write_txt(p, BOOK1), "epub": lambda p: write_epub(p, BOOK1_TITLE, BOOK1_AUTHOR, BOOK1),
               "fb2": lambda p: write_fb2(p, BOOK1_TITLE, BOOK1_AUTHOR, BOOK1)}
    path = writers[fmt](tmp_path / f"book.{fmt}")
    res = library.import_book(path)
    assert res.chapters == 8 and res.chunks == 8 and not res.already_existed
    chapters = library.repo.chapters_of(res.book_id)
    assert [c["idx"] for c in chapters] == list(range(8))
    assert [c["title"] for c in chapters] == TITLES
    for ch, (_, src) in zip(chapters, BOOK1):
        assert _words(ch["text"]) == _words(src)  # содержание не изменено
    chunks = library.repo.chunks_of(res.book_id)
    assert [c["idx"] for c in chunks] == list(range(len(chunks)))
    book = library.repo.get_book(res.book_id)
    assert book["source_sha256"] == sha256_file(path) and book["id"] == book_uuid(book["source_sha256"])
    assert (library.data_dir / "books").exists() and book["source_path"].endswith(path.name)


def test_reimport_same_file_is_idempotent_and_ids_stable(library, db, tmp_path):
    path = write_txt(tmp_path / "b.txt", BOOK1)
    first = library.import_book(path)
    ids_before = [c["id"] for c in library.repo.chunks_of(first.book_id)]
    again = library.import_book(path)
    assert again.already_existed and again.book_id == first.book_id
    assert db.scalar("SELECT COUNT(*) FROM books") == 1
    assert [c["id"] for c in library.repo.chunks_of(first.book_id)] == ids_before


def test_same_content_other_name_is_detected_as_duplicate(library, tmp_path):
    a = write_txt(tmp_path / "a.txt", BOOK1)
    b = write_txt(tmp_path / "copy.txt", BOOK1)
    library.import_book(a)
    assert library.import_book(b).already_existed


def test_stable_chunk_ids_across_databases(tmp_path, settings):
    from bookmind.services.library import LibraryService
    from bookmind.storage.database import Database
    path = write_txt(tmp_path / "b.txt", BOOK1)
    ids = []
    for n in range(2):
        d = tmp_path / f"d{n}"
        d.mkdir()
        db = Database(d / "x.sqlite")
        lib = LibraryService(db, settings, d)
        r = lib.import_book(path)
        ids.append([c["id"] for c in lib.repo.chunks_of(r.book_id)])
        db.close()
    assert ids[0] == ids[1]


def test_unsupported_and_missing_and_corrupt(library, tmp_path):
    (tmp_path / "x.docx").write_bytes(b"x")
    with pytest.raises(ImportError_):
        library.import_book(tmp_path / "x.docx")
    with pytest.raises(ImportError_):
        library.import_book(tmp_path / "nope.txt")
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"garbage")
    with pytest.raises(ImportError_):
        library.import_book(bad)
    assert library.repo.list_books() == []  # ничего не сохранилось


def test_failed_import_is_atomic(library, db, tmp_path, monkeypatch):
    path = write_txt(tmp_path / "b.txt", BOOK1)
    calls = {"n": 0}
    real = library.search.index

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 5:
            raise RuntimeError("сбой посреди импорта")
        return real(*a, **k)

    monkeypatch.setattr(library.search, "index", flaky)
    with pytest.raises(RuntimeError):
        library.import_book(path)
    assert db.scalar("SELECT COUNT(*) FROM books") == 0
    assert db.scalar("SELECT COUNT(*) FROM chapters") == 0
    assert db.scalar("SELECT COUNT(*) FROM series") == 0
    monkeypatch.setattr(library.search, "index", real)
    assert library.import_book(path).chapters == 8


def test_manual_chapter_split_merge_rename(library, tmp_path):
    res = library.import_book(write_txt(tmp_path / "b.txt", BOOK1))
    ch1 = library.repo.chapters_of(res.book_id)[0]
    cut = ch1["text"].index("Аларик лежал")
    library.split_chapter(res.book_id, 0, cut, "Глава 1б")
    chs = library.repo.chapters_of(res.book_id)
    assert len(chs) == 9 and chs[1]["title"] == "Глава 1б" and chs[1]["manual_edit"] == 1
    library.merge_with_next(res.book_id, 0)
    assert len(library.repo.chapters_of(res.book_id)) == 8
    library.rename_chapter(res.book_id, 2, "Новое имя")
    assert library.repo.chapters_of(res.book_id)[2]["title"] == "Новое имя"
    chunks = library.repo.chunks_of(res.book_id)
    assert [c["idx"] for c in chunks] == list(range(len(chunks)))


def test_series_assignment_and_order(library, tmp_path):
    sid = library.create_series("Тестовая серия")
    a = library.import_book(write_txt(tmp_path / "a.txt", BOOK1), series_id=sid)
    other = [(t, x + " Дополнение.") for t, x in BOOK1]
    b = library.import_book(write_txt(tmp_path / "b.txt", other), series_id=sid)
    books = library.repo.list_books(sid)
    assert [x["id"] for x in books] == [a.book_id, b.book_id] and [x["series_order"] for x in books] == [1, 2]
