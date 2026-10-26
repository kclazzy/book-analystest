"""Сервис библиотеки: импорт книг, управление сериями, ручная правка границ глав."""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from bookmind.config import Settings
from bookmind.domain.exceptions import BookMindError
from bookmind.ingestion.chunking import chunk_text
from bookmind.ingestion.epub import parse_epub
from bookmind.ingestion.fb2 import parse_fb2
from bookmind.ingestion.models import ParsedBook
from bookmind.ingestion.text_utils import normalize_text
from bookmind.ingestion.txt import parse_txt
from bookmind.services.search import SearchIndex
from bookmind.storage.database import Database, now_iso
from bookmind.storage.repositories import LibraryRepo

_NAMESPACE = uuid.UUID("6f1c3b52-8d0e-4f43-9a7e-0b5d2c1a9e11")
PARSERS = {".txt": parse_txt, ".epub": parse_epub, ".fb2": parse_fb2}
SUPPORTED_EXTENSIONS = (".txt", ".epub", ".fb2", ".zip")


class ImportError_(BookMindError):
    pass


@dataclass
class ImportResult:
    book_id: str
    title: str
    chapters: int
    chunks: int
    already_existed: bool
    warnings: list[str]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def book_uuid(sha: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"book:{sha}"))


def chapter_uuid(book_id: str, idx: int) -> str:
    return str(uuid.uuid5(uuid.UUID(book_id), f"chapter:{idx}"))


def chunk_uuid(chapter_id: str, start: int, end: int, text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return str(uuid.uuid5(uuid.UUID(chapter_id), f"chunk:{start}:{end}:{digest}"))


def sniff_format(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".zip" and path.name.lower().endswith(".fb2.zip"):
        return ".fb2"
    if ext in PARSERS:
        return ext
    raise ImportError_(f"Формат {ext or '(без расширения)'} пока не поддерживается. Доступны: TXT, EPUB, FB2")


class LibraryService:
    def __init__(self, db: Database, settings: Settings, data_dir: Path):
        self.db, self.settings, self.data_dir = db, settings, Path(data_dir)
        self.repo = LibraryRepo(db)
        self.search = SearchIndex(db)

    # ---- импорт ----
    def import_book(self, path: Path | str, series_id: str | None = None, series_order: int | None = None,
                    title: str | None = None) -> ImportResult:
        path = Path(path)
        if not path.exists():
            raise ImportError_(f"Файл не найден: {path}")
        ext = sniff_format(path)
        sha = sha256_file(path)
        book_id = book_uuid(sha)
        existing = self.repo.find_book_by_sha(sha)
        if existing:
            st = self.repo.book_stats(existing["id"])
            return ImportResult(existing["id"], existing["title"], st["chapters"], st["chunks"], True,
                                ["Книга с таким содержимым уже импортирована"])
        try:
            parsed = PARSERS[ext](path)
        except Exception as exc:  # повреждённые файлы не должны ронять приложение
            raise ImportError_(f"Не удалось разобрать {path.name}: {exc}") from exc
        if not parsed.chapters:
            raise ImportError_(f"В файле {path.name} не найден текст")
        if title:
            parsed.title = title

        stored = self._store_source(path, sha)
        with self.db.transaction():
            if series_id is None:
                series_id = self.repo.create_series(parsed.title)
            elif not self.repo.get_series(series_id):
                raise ImportError_("Серия не найдена")
            order = series_order or (self.db.scalar("SELECT COALESCE(MAX(series_order),0)+1 FROM books WHERE series_id = ?", (series_id,)))
            self.db.insert("books", {
                "id": book_id, "series_id": series_id, "series_order": order, "title": parsed.title,
                "author": parsed.author, "language": parsed.language, "format": parsed.format,
                "source_filename": path.name, "source_path": str(stored), "source_sha256": sha,
                "source_size": path.stat().st_size, "metadata_json": json.dumps(parsed.metadata, ensure_ascii=False),
                "import_log_json": json.dumps({"warnings": parsed.warnings, "transformations": parsed.transformations},
                                              ensure_ascii=False),
                "created_at": now_iso()})
            n_chapters, n_chunks = self._write_chapters(book_id, series_id, parsed)
        return ImportResult(book_id, parsed.title, n_chapters, n_chunks, False, parsed.warnings)

    def _store_source(self, path: Path, sha: str) -> Path:
        target_dir = self.data_dir / "books" / sha[:16]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        if not target.exists():
            shutil.copy2(path, target)
        return target

    def _write_chapters(self, book_id: str, series_id: str, parsed: ParsedBook) -> tuple[int, int]:
        idx = 0
        chunk_idx = 0
        for ch in parsed.chapters:
            text, ops = normalize_text(ch.raw_text)
            if not text:
                continue
            chunk_idx = self._insert_chapter(book_id, series_id, idx, ch.title.strip() or f"Глава {idx + 1}",
                                             ch.raw_text, text, ops, chunk_idx, manual=False)
            idx += 1
        return idx, chunk_idx

    def _insert_chapter(self, book_id: str, series_id: str, idx: int, title: str, raw: str, text: str,
                        ops: list[str], chunk_idx: int, manual: bool) -> int:
        cid = chapter_uuid(book_id, idx)
        self.db.insert("chapters", {
            "id": cid, "book_id": book_id, "idx": idx, "title": title, "text": text, "raw_text": raw,
            "char_count": len(text), "transformations_json": json.dumps(ops), "manual_edit": 1 if manual else 0})
        for n, span in enumerate(chunk_text(text, self.settings.chunk_chars_min, self.settings.chunk_chars_max)):
            body = text[span.start:span.end]
            kid = chunk_uuid(cid, span.start, span.end, body)
            self.db.insert("chunks", {
                "id": kid, "book_id": book_id, "chapter_id": cid, "idx": chunk_idx, "idx_in_chapter": n,
                "start_char": span.start, "end_char": span.end, "text": body,
                "text_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()})
            self.search.index("chunk", kid, series_id, book_id, cid, title, body)
            chunk_idx += 1
        return chunk_idx

    # ---- серии ----
    def create_series(self, title: str, description: str = "") -> str:
        return self.repo.create_series(title.strip(), description)

    # ---- ручная правка глав ----
    def _guard_edit(self, book_id: str, purge: bool) -> None:
        if self.repo.has_analysis(book_id):
            if not purge:
                raise BookMindError("Книга уже анализировалась: правка глав сбросит результаты анализа. Подтвердите сброс.")
            from bookmind.services.purge import purge_book_analysis
            purge_book_analysis(self.db, book_id)

    def rebuild_chapters(self, book_id: str, chapters: list[tuple[str, str]], purge_analysis: bool = False) -> None:
        """Заменяет главы книги списком (название, текст). Используется операциями split/merge/rename."""
        book = self.repo.get_book(book_id)
        if not book:
            raise BookMindError("Книга не найдена")
        self._guard_edit(book_id, purge_analysis)
        with self.db.transaction():
            self.db.execute("DELETE FROM search_index WHERE book_id = ? AND kind = 'chunk'", (book_id,))
            self.db.execute("DELETE FROM chapters WHERE book_id = ?", (book_id,))
            chunk_idx = 0
            n = 0
            for title, raw in chapters:
                text, ops = normalize_text(raw)
                if not text:
                    continue
                chunk_idx = self._insert_chapter(book_id, book["series_id"], n, title, raw, text, ops + ["manual_edit"],
                                                 chunk_idx, manual=True)
                n += 1

    def _chapter_list(self, book_id: str) -> list[tuple[str, str]]:
        return [(c["title"], c["text"]) for c in self.repo.chapters_of(book_id)]

    def rename_chapter(self, book_id: str, chapter_idx: int, new_title: str, purge_analysis: bool = False) -> None:
        chapters = self._chapter_list(book_id)
        chapters[chapter_idx] = (new_title, chapters[chapter_idx][1])
        self.rebuild_chapters(book_id, chapters, purge_analysis)

    def split_chapter(self, book_id: str, chapter_idx: int, at_char: int, new_title: str,
                      purge_analysis: bool = False) -> None:
        chapters = self._chapter_list(book_id)
        title, text = chapters[chapter_idx]
        if not 0 < at_char < len(text):
            raise BookMindError("Позиция разделения вне главы")
        chapters[chapter_idx:chapter_idx + 1] = [(title, text[:at_char]), (new_title, text[at_char:])]
        self.rebuild_chapters(book_id, chapters, purge_analysis)

    def merge_with_next(self, book_id: str, chapter_idx: int, purge_analysis: bool = False) -> None:
        chapters = self._chapter_list(book_id)
        if chapter_idx + 1 >= len(chapters):
            raise BookMindError("Нет следующей главы")
        (t1, x1), (_t2, x2) = chapters[chapter_idx], chapters[chapter_idx + 1]
        chapters[chapter_idx:chapter_idx + 2] = [(t1, x1 + "\n\n" + x2)]
        self.rebuild_chapters(book_id, chapters, purge_analysis)

    def delete_book(self, book_id: str) -> None:
        self.repo.delete_book(book_id)
