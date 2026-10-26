"""Конвейер анализа книги: задачи по фрагментам, сохранение после каждого, возобновление, отмена."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from bookmind.analysis.context import ContextBuilder
from bookmind.analysis.extractor import LLM, StructuredCaller
from bookmind.analysis.models import load_chunk_ref
from bookmind.analysis.persister import CorePersister
from bookmind.config import Settings
from bookmind.domain.exceptions import (
    BookMindError, CancelledError, ModelNotInstalledError, ModelOutputError, OllamaRequestError, OllamaUnavailableError,
)
from bookmind.domain.schemas import CoreAnalysis
from bookmind.ingestion.chunking import context_before
from bookmind.llm.prompts import PromptStore
from bookmind.services.purge import purge_book_analysis
from bookmind.storage.database import Database
from bookmind.storage.repositories import LibraryRepo, TaskRepo

log = logging.getLogger(__name__)
TRANSIENT = (ModelOutputError, OllamaRequestError, ModelNotInstalledError)


@dataclass
class Progress:
    run_id: str
    stage: str
    done: int
    total: int
    failed: int
    chapter: str = ""
    chunk_idx: int = 0
    elapsed_s: float = 0.0
    message: str = ""


@dataclass
class RunSummary:
    run_id: str
    status: str                       # completed | paused | failed | cancelled
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    message: str = ""


ProgressCb = Callable[[Progress], None]


class AnalysisPipeline:
    STAGES_ORDER: tuple[str, ...] = ("core",)

    def __init__(self, db: Database, llm: LLM, settings: Settings, data_dir: Path | None = None,
                 prompts: PromptStore | None = None):
        self.db, self.llm, self.settings = db, llm, settings
        self.prompts = prompts or PromptStore(data_dir)
        self.library = LibraryRepo(db)
        self.tasks = TaskRepo(db)
        self.caller = StructuredCaller(llm, settings)
        self.context = ContextBuilder(db)
        self.core_persister = CorePersister(db)
        self.handlers: dict[str, Callable[[Any, str, str], tuple[str, list[str], Callable[[], list[str]]]]] = {"core": self._core}
        self.task_scope: dict[str, str] = {"core": "chunk"}

    # ---- запуск/возобновление ----
    def prompt_version(self, stages: tuple[str, ...]) -> str:
        names = {"core": "core_analysis", "deep": "deep_analysis", "chapter_summary": "chapter_summary"}
        return self.prompts.version(*[names[s] for s in stages])

    def start(self, book_id: str, model: str, stages: tuple[str, ...] | None = None, replace: bool = False) -> str:
        stages = tuple(s for s in self.STAGES_ORDER if s in (stages or self.STAGES_ORDER))
        if self.library.get_book(book_id) is None:
            raise BookMindError("Книга не найдена")
        version = self.prompt_version(stages)
        params = {"num_ctx": self.settings.num_ctx, "num_predict": self.settings.num_predict,
                  "chunk_chars": [self.settings.chunk_chars_min, self.settings.chunk_chars_max], "stages": list(stages)}
        with self.db.transaction():
            run = self.tasks.find_run(book_id, model, version)
            if run is not None and not replace and run["status"] != "completed":
                run_id = run["id"]
                self.tasks.reset_interrupted(run_id)
                self.tasks.set_run_status(run_id, "running")
            elif run is not None and not replace:
                run_id = run["id"]
            else:
                if self.library.has_analysis(book_id):
                    if not replace:
                        raise BookMindError("Книга уже анализировалась другой моделью или версией промптов. "
                                            "Чтобы начать заново, подтвердите замену результатов.")
                    purge_book_analysis(self.db, book_id)
                run_id = self.tasks.create_run(book_id, model, version, params)
            self._ensure_tasks(run_id, book_id, stages)
        return run_id

    def _ensure_tasks(self, run_id: str, book_id: str, stages: tuple[str, ...]) -> None:
        chunks = self.library.chunks_of(book_id)
        chapters = self.library.chapters_of(book_id)
        for stage in stages:
            if self.task_scope.get(stage) == "chapter":
                for ch in chapters:
                    self.tasks.ensure_task(run_id, stage, "chapter", ch["id"], ch["idx"])
            else:
                for c in chunks:
                    self.tasks.ensure_task(run_id, stage, "chunk", c["id"], c["idx"])

    # ---- выполнение ----
    def run(self, run_id: str, on_progress: ProgressCb | None = None, cancel: threading.Event | None = None) -> RunSummary:
        run = self.tasks.get_run(run_id)
        if run is None:
            raise BookMindError("Запуск анализа не найден")
        cancel = cancel or threading.Event()
        started = time.monotonic()
        model, book_id = run["model"], run["book_id"]
        stages = tuple(json.loads(run["params_json"]).get("stages", ["core"]))
        stages = tuple(s for s in self.STAGES_ORDER if s in stages and s in self.handlers)
        self.tasks.set_run_status(run_id, "running")

        def report(stage: str, msg: str = "", chapter: str = "", chunk_idx: int = 0) -> None:
            if on_progress:
                p = self.tasks.progress(run_id, stage)
                on_progress(Progress(run_id, stage, p.get("done", 0) + p.get("skipped", 0), p.get("total", 0),
                                     p.get("failed", 0), chapter, chunk_idx, time.monotonic() - started, msg))

        try:
            for stage in stages:
                report(stage, "начало этапа")
                for task in self.tasks.next_tasks(run_id, stage, self.settings.task_max_attempts):
                    if cancel.is_set():
                        self.tasks.set_run_status(run_id, "paused")
                        return self._summary(run_id, "paused", "Остановлено пользователем; прогресс сохранён")
                    t0 = time.monotonic()
                    self.tasks.mark_running(task["id"])
                    label = self._label(task)
                    report(stage, f"обработка: {label[0]}", label[0], label[1])
                    try:
                        raw, issues, apply = self.handlers[stage](task, model, run_id)  # вызов модели — вне транзакции
                        with self.db.transaction():  # результат и отметка «выполнено» сохраняются атомарно
                            extra = apply() or []
                            self.tasks.mark_done(task["id"], raw, issues + extra, time.monotonic() - t0)
                    except CancelledError:
                        self.db.execute("UPDATE analysis_tasks SET status = 'pending', attempts = attempts - 1 WHERE id = ?", (task["id"],))
                        self.tasks.set_run_status(run_id, "paused")
                        return self._summary(run_id, "paused", "Остановлено пользователем; прогресс сохранён")
                    except OllamaUnavailableError as exc:
                        # сервер недоступен: не «сжигаем» попытки у всех задач, возвращаем задачу в очередь и ставим паузу
                        self.db.execute("UPDATE analysis_tasks SET status = 'pending', attempts = attempts - 1 WHERE id = ?", (task["id"],))
                        self.tasks.set_run_status(run_id, "paused", str(exc))
                        return self._summary(run_id, "paused", f"Ollama недоступен: {exc}")
                    except TRANSIENT as exc:
                        log.warning("Задача %s не выполнена: %s", task["id"], exc)
                        self.tasks.mark_failed(task["id"], str(exc), getattr(exc, "raw", ""), time.monotonic() - t0)
                    report(stage, "готово", label[0], label[1])
            progress = {s: self.tasks.progress(run_id, s) for s in stages}
            failed = sum(p.get("failed", 0) for p in progress.values())
            status = "completed" if failed == 0 else "failed"
            self.tasks.set_run_status(run_id, status, None if failed == 0 else f"Не выполнено задач: {failed}")
            return self._summary(run_id, status, "" if failed == 0 else f"Не выполнено задач: {failed}")
        except BookMindError as exc:
            self.tasks.set_run_status(run_id, "failed", str(exc))
            raise

    def _summary(self, run_id: str, status: str, message: str) -> RunSummary:
        stages = tuple(json.loads(self.tasks.get_run(run_id)["params_json"]).get("stages", ["core"]))
        return RunSummary(run_id, status, {s: self.tasks.progress(run_id, s) for s in stages}, message)

    def _label(self, task) -> tuple[str, int]:
        if task["scope_type"] == "chunk":
            loc = self.library.chunk_location(task["scope_id"])
            return (loc["chapter_title"], loc["idx"]) if loc else ("", 0)
        ch = self.library.get_chapter(task["scope_id"])
        return (ch["title"], ch["idx"]) if ch else ("", 0)

    # ---- обработчики этапов ----
    def _core(self, task, model: str, run_id: str):
        chunk = load_chunk_ref(self.db, task["scope_id"])
        chapter = self.library.get_chapter(chunk.chapter_id)
        ctx_before = context_before(chapter["text"], chunk.start_char, self.settings.chunk_overlap_chars)
        entities_block, offered = self.context.known_entities_block(chunk)
        prompt = self.prompts.render(
            "core_analysis", book_title=chunk.book_title, chapter_title=chunk.chapter_title,
            known_entities=entities_block, context_before=ctx_before or "(начало главы)", text=chunk.text)
        core, raw, issues = self.caller.call(model, prompt, CoreAnalysis)

        def apply() -> list[str]:
            return self.core_persister.save(chunk, run_id, core, offered).issues

        return raw, issues, apply
