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
from bookmind.analysis.deep_persister import DeepPersister
from bookmind.analysis.persister import CorePersister
from bookmind.analysis.series import SeriesMemory
from bookmind.config import Settings
from bookmind.domain.exceptions import (
    BookMindError, CancelledError, ModelNotInstalledError, SkipTask, ModelOutputError, OllamaRequestError, OllamaUnavailableError,
)
from bookmind.domain.schemas import ChapterSummary, CoreAnalysis, DeepAnalysis
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
    STAGES_ORDER: tuple[str, ...] = ("core", "deep", "chapter_summary", "arc_summary", "series")
    DEFAULT_STAGES: tuple[str, ...] = ("core",)

    def __init__(self, db: Database, llm: LLM, settings: Settings, data_dir: Path | None = None,
                 prompts: PromptStore | None = None):
        self.db, self.llm, self.settings = db, llm, settings
        self.prompts = prompts or PromptStore(data_dir)
        self.library = LibraryRepo(db)
        self.tasks = TaskRepo(db)
        self.caller = StructuredCaller(llm, settings)
        self.context = ContextBuilder(db)
        self.core_persister = CorePersister(db)
        self.deep_persister = DeepPersister(db)
        self.series = SeriesMemory(db, self.caller, self.prompts)
        self.handlers: dict[str, Callable[[Any, str, str], tuple[str, list[str], Callable[[], list[str]]]]] = {
            "core": self._core, "deep": self._deep, "chapter_summary": self._chapter_summary, "arc_summary": self._arc_summary,
            "series": self._series}
        self.task_scope: dict[str, str] = {"core": "chunk", "deep": "chunk", "chapter_summary": "chapter", "arc_summary": "arc", "series": "series"}

    # ---- запуск/возобновление ----
    PROMPT_NAMES = {"core": ("core_analysis",), "deep": ("deep_analysis",), "chapter_summary": ("chapter_summary",),
                    "arc_summary": ("arc_summary",), "series": ("contradiction_check", "question_review")}

    def stage_version(self, stage: str) -> str:
        return self.prompts.version(*self.PROMPT_NAMES[stage])

    def prompt_version(self, stages: tuple[str, ...]) -> str:
        return self.prompts.version(*[n for s in stages for n in self.PROMPT_NAMES[s]])

    def _normalize(self, stages) -> tuple[str, ...]:
        return tuple(s for s in self.STAGES_ORDER if s in (stages or self.DEFAULT_STAGES))

    def _reusable(self, run, stages: tuple[str, ...]):
        """Можно ли продолжить существующий запуск этой книги и модели, добавив недостающие этапы.
        Возвращает (запуск, объединённые этапы, версии промптов по этапам) или None, если промпты уже выполненных этапов изменились."""
        if run is None:
            return None
        pj = json.loads(run["params_json"])
        old = tuple(pj.get("stages", ["core"]))
        old_v = pj.get("stage_versions")
        if old_v is None:      # запуск создан старой версией: версия промптов хранилась только для всего набора этапов
            if run["prompt_version"] != self.prompt_version(self._normalize(old)):
                return None
            old_v = {s: self.stage_version(s) for s in old}
        if any(old_v.get(s) != self.stage_version(s) for s in stages if s in old):
            return None
        merged = self._normalize(set(old) | set(stages))
        versions = {**old_v, **{s: self.stage_version(s) for s in merged if s not in old_v}}
        return run, merged, versions

    def can_continue(self, book_id: str, model: str, stages: tuple[str, ...] | None = None) -> bool:
        """True — анализ можно начать или продолжить без замены результатов."""
        if not self.library.has_analysis(book_id):
            return True
        return self._reusable(self.tasks.latest_run(book_id, model), self._normalize(stages)) is not None

    def retry_failed(self, book_id: str, model: str) -> int:
        """Явное «повторить неудавшиеся»: задачам, исчерпавшим попытки, даются новые. Возвращает их число."""
        run = self.tasks.latest_run(book_id, model)
        if run is None:
            return 0
        n = self.tasks.reset_failed(run["id"])
        if n:
            self.tasks.set_run_status(run["id"], "paused")
        return n

    def start(self, book_id: str, model: str, stages: tuple[str, ...] | None = None, replace: bool = False) -> str:
        stages = self._normalize(stages)
        if self.library.get_book(book_id) is None:
            raise BookMindError("Книга не найдена")
        with self.db.transaction():
            reuse = None if replace else self._reusable(self.tasks.latest_run(book_id, model), stages)
            if reuse is not None:
                run, merged, versions = reuse
                run_id = run["id"]
                added = [s for s in merged if s not in json.loads(run["params_json"]).get("stages", ["core"])]
                pj = {**json.loads(run["params_json"]), "stages": list(merged), "stage_versions": versions}
                self.tasks.update_run_params(run_id, pj, self.prompt_version(merged))
                if run["status"] != "completed" or added:
                    self.tasks.reset_interrupted(run_id)
                    self.tasks.set_run_status(run_id, "running")
                self._ensure_tasks(run_id, book_id, merged)
            else:
                if self.library.has_analysis(book_id):
                    if not replace:
                        raise BookMindError("Книга уже анализировалась другой моделью или версией промптов. "
                                            "Чтобы начать заново, подтвердите замену результатов.")
                    purge_book_analysis(self.db, book_id)
                params = {"num_ctx": self.settings.num_ctx, "num_predict": self.settings.num_predict,
                          "chunk_chars": [self.settings.chunk_chars_min, self.settings.chunk_chars_max], "stages": list(stages),
                          "stage_versions": {s: self.stage_version(s) for s in stages}}
                run_id = self.tasks.create_run(book_id, model, self.prompt_version(stages), params)
                self._ensure_tasks(run_id, book_id, stages)
        return run_id

    def _ensure_tasks(self, run_id: str, book_id: str, stages: tuple[str, ...]) -> None:
        chunks = self.library.chunks_of(book_id)
        chapters = self.library.chapters_of(book_id)
        for stage in stages:
            if self.task_scope.get(stage) == "chapter":
                for ch in chapters:
                    self.tasks.ensure_task(run_id, stage, "chapter", ch["id"], ch["idx"])
            elif self.task_scope.get(stage) == "series":
                continue  # задачи создаются непосредственно перед этапом: нужны результаты предыдущих этапов
            elif self.task_scope.get(stage) == "arc":
                size = max(1, self.settings.arc_size)
                if len(chapters) > size:
                    for k in range(0, (len(chapters) + size - 1) // size):
                        self.tasks.ensure_task(run_id, stage, "arc", f"{book_id}:arc:{k}", k)
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
                if self.task_scope.get(stage) == "series":
                    self._ensure_series_tasks(run_id, book_id)
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
                    except SkipTask as exc:
                        self.tasks.mark_done(task["id"], "", [str(exc)], time.monotonic() - t0, status="skipped")
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
                    except Exception as exc:  # непредвиденная ошибка одной задачи не должна ронять весь анализ
                        log.exception("Внутренняя ошибка в задаче %s", task["id"])
                        self.tasks.mark_failed(task["id"], f"Внутренняя ошибка: {exc!r}", "", time.monotonic() - t0)
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
        if task["scope_type"] in ("entity", "question", "book"):
            return ({"entity": "противоречия", "question": "пересмотр загадки", "book": "состояние героев"}[task["scope_type"]], task["seq"])
        if task["scope_type"] == "arc":
            return (f"арка {task['seq'] + 1}", task["seq"])
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

    def _deep(self, task, model: str, run_id: str):
        chunk = load_chunk_ref(self.db, task["scope_id"])
        chapter = self.library.get_chapter(chunk.chapter_id)
        ctx_before = context_before(chapter["text"], chunk.start_char, self.settings.chunk_overlap_chars)
        entities_block, offered = self.context.known_entities_block(chunk)
        prompt = self.prompts.render(
            "deep_analysis", book_title=chunk.book_title, chapter_title=chunk.chapter_title,
            known_entities=entities_block, known_goals=self.context.known_goals_block(chunk, offered),
            open_questions=self.context.open_questions_block(chunk), context_before=ctx_before or "(начало главы)",
            text=chunk.text)
        deep, raw, issues = self.caller.call(model, prompt, DeepAnalysis)

        def apply() -> list[str]:
            return self.deep_persister.save(chunk, run_id, deep, offered).issues

        return raw, issues, apply

    def _chapter_summary(self, task, model: str, run_id: str):
        chapter = self.library.get_chapter(task["scope_id"])
        book = self.library.get_book(chapter["book_id"])
        sums = self.db.query(
            "SELECT r.content FROM report_versions r JOIN chunks c ON c.id = r.scope_id WHERE r.kind = 'chunk_summary' "
            "AND r.run_id = ? AND c.chapter_id = ? ORDER BY c.idx", (run_id, chapter["id"]))
        if not sums:
            raise SkipTask("нет сводок фрагментов главы (этап core не выполнен)")
        events = self.db.query("SELECT description FROM events WHERE chapter_id = ? AND verification = 'verified' ORDER BY seq",
                               (chapter["id"],))
        prompt = self.prompts.render(
            "chapter_summary", book_title=book["title"], chapter_title=chapter["title"],
            chunk_summaries="\n".join(f"- {s['content']}" for s in sums),
            events="\n".join(f"- {e['description']}" for e in events) or "(нет)")
        obj, raw, issues = self.caller.call(model, prompt, ChapterSummary)

        def apply() -> list[str]:
            self._save_report("chapter_summary", book, chapter["id"], chapter["title"], obj, run_id, model)
            return []

        return raw, issues, apply

    def _arc_summary(self, task, model: str, run_id: str):
        book_id, _, k = task["scope_id"].split(":")
        book = self.library.get_book(book_id)
        size = max(1, self.settings.arc_size)
        chapters = self.library.chapters_of(book_id)[int(k) * size:(int(k) + 1) * size]
        lines = []
        for ch in chapters:
            r = self.db.one("SELECT content FROM report_versions WHERE kind = 'chapter_summary' AND scope_id = ? AND run_id = ? "
                            "ORDER BY version DESC LIMIT 1", (ch["id"], run_id))
            if r:
                lines.append(f"{ch['title']}: {r['content']}")
        if not lines:
            raise SkipTask("нет сводок глав для арки")
        title = f"Арка {int(k) + 1}: {chapters[0]['title']} — {chapters[-1]['title']}"
        prompt = self.prompts.render("arc_summary", book_title=book["title"],
                                     chapter_range=f"{chapters[0]['title']} — {chapters[-1]['title']}",
                                     chapter_summaries="\n".join(lines))
        obj, raw, issues = self.caller.call(model, prompt, ChapterSummary)

        def apply() -> list[str]:
            self._save_report("arc_summary", book, task["scope_id"], title, obj, run_id, model)
            return []

        return raw, issues, apply

    def _ensure_series_tasks(self, run_id: str, book_id: str) -> None:
        sid = self.library.get_book(book_id)["series_id"]
        for i, qid in enumerate(self.series.questions_to_review(book_id)):
            self.tasks.ensure_task(run_id, "series", "question", qid, i)
        for i, eid in enumerate(self.series.entities_with_pairs(sid)):
            self.tasks.ensure_task(run_id, "series", "entity", eid, 1000 + i)
        self.tasks.ensure_task(run_id, "series", "book", book_id, 10 ** 6)

    def _series(self, task, model: str, run_id: str):
        kind, scope = task["scope_type"], task["scope_id"]
        if kind == "question":
            return self.series.question_task(scope, self.tasks.get_run(run_id)["book_id"], model)
        if kind == "entity":
            return self.series.contradiction_task(scope, model)

        def apply() -> list[str]:
            self.series.snapshot_book(scope)
            return []

        return "", [], apply

    def _save_report(self, kind: str, book, scope_id: str, title: str, obj: ChapterSummary, run_id: str, model: str) -> None:
        from bookmind.storage.database import new_id, now_iso
        version = (self.db.scalar("SELECT COALESCE(MAX(version), 0) FROM report_versions WHERE kind = ? AND scope_id = ?",
                                  (kind, scope_id)) or 0) + 1
        body = obj.summary + ("\n\nКлючевые моменты:\n" + "\n".join(f"- {p}" for p in obj.key_points) if obj.key_points else "")
        self.db.insert("report_versions", {
            "id": new_id(), "series_id": book["series_id"], "book_id": book["id"], "scope_id": scope_id, "kind": kind,
            "title": title, "content": body,
            "structured_json": json.dumps({"summary": obj.summary, "key_points": obj.key_points, "open_threads": obj.open_threads},
                                          ensure_ascii=False),
            "run_id": run_id, "model": model, "version": version, "created_at": now_iso()})
