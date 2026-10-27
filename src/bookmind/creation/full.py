"""Полный конвейер: анализ книг серии → план новой книги → текст глав → файл. Каждый шаг возобновляем и пропускает сделанное."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from bookmind.creation.book_export import export_book
from bookmind.creation.schemas import AdaptParams
from bookmind.creation.service import AdaptationService
from bookmind.creation.writer import BookWriter
from bookmind.domain.exceptions import BookMindError

FULL_STAGES = ("core", "chapter_summary", "arc_summary", "series")


@dataclass
class FullProgress:
    phase: str              # analysis | plan | write | export
    message: str
    done: int = 0
    total: int = 0


@dataclass
class FullResult:
    status: str             # completed | paused | failed | needs_review
    project_id: str = ""
    path: Path | None = None
    flagged: list[int] = field(default_factory=list)
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    qa_errors: int = 0
    qa_warnings: int = 0


def run_full_book(pipeline, adaptation: AdaptationService, writer: BookWriter, series_id: str, model: str,
                  params: AdaptParams | None = None, project_id: str | None = None, export_path: Path | None = None,
                  stages: tuple[str, ...] = FULL_STAGES, on_progress: Callable[[FullProgress], None] | None = None,
                  cancel: threading.Event | None = None) -> FullResult:
    cancel = cancel or threading.Event()
    say = on_progress or (lambda p: None)
    db = adaptation.db

    # 1. анализ всех книг серии
    books = db.query("SELECT id, title FROM books WHERE series_id = ? ORDER BY series_order, title", (series_id,))
    if not books:
        return FullResult("failed", message="В серии нет книг")
    warnings: list[str] = []
    for n, b in enumerate(books):
        say(FullProgress("analysis", f"анализ книги «{b['title']}» ({n + 1} из {len(books)})", n, len(books)))
        try:
            run_id = pipeline.start(b["id"], model, stages)
        except BookMindError:
            continue                    # книга уже проанализирована другой моделью — используем готовое
        summary = pipeline.run(run_id, on_progress=lambda pr, t=b["title"]: say(FullProgress("analysis", f"«{t}»: {pr.stage} {pr.message}".strip())), cancel=cancel)
        if summary.status == "cancelled" or cancel.is_set():
            return FullResult("paused", message="Остановлено на этапе анализа; прогресс сохранён")
        if summary.status == "paused":
            return FullResult("paused", message=f"Анализ книги «{b['title']}» приостановлен: {summary.message}".strip())
        if summary.status == "failed":
            # часть фрагментов не обработана — это не повод бросать всё: книга создаётся из того, что удалось извлечь
            bad = sum(c.get("failed", 0) for c in summary.counts.values())
            warnings.append(f"Анализ «{b['title']}»: не обработано задач — {bad}. Книга создана по неполным данным; "
                            f"нажмите «Повторить неудавшиеся» и создайте книгу заново, чтобы улучшить результат.")
            say(FullProgress("analysis", warnings[-1]))

    # 2. план новой книги
    if project_id is None:
        params = params or AdaptParams()
        params.pause_after_plan = False
        project_id = adaptation.create(series_id, params, "Новая книга по мотивам")
    say(FullProgress("plan", "составляю сюжет, героев и план глав"))
    ps = adaptation.run(project_id, model, on_progress=lambda p: say(FullProgress("plan", p.message or p.stage, p.done, p.total)), cancel=cancel)
    if ps.status in ("paused", "failed"):
        return FullResult(ps.status, project_id, message=ps.message, warnings=warnings)
    if ps.status == "awaiting_review":
        return FullResult("needs_review", project_id, message=ps.message, warnings=warnings)

    # 3. текст глав
    ws = writer.write(project_id, model, on_progress=lambda p: say(FullProgress("write", p.message, p.chapter * p.parts + p.part, p.total * p.parts)), cancel=cancel)
    if ws.status != "completed":
        return FullResult(ws.status, project_id, flagged=ws.flagged, message=ws.message, warnings=warnings)

    rep = writer.book_report(project_id) or writer.check_book(project_id)
    if rep.errors:
        warnings.append(f"Проверка книги: ошибок — {len(rep.errors)}, замечаний — {len(rep.warnings)}. Откройте «Проверить книгу», чтобы увидеть, где именно.")

    # 4. файл
    path = None
    if export_path:
        say(FullProgress("export", f"сохраняю книгу: {export_path}"))
        path = export_book(writer.book(project_id), Path(export_path))
    return FullResult("completed", project_id, path, ws.flagged, warnings=warnings, qa_errors=len(rep.errors), qa_warnings=len(rep.warnings))
