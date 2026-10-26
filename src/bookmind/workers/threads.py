"""Фоновые потоки Qt: анализ, установка/обновление моделей, ответ на вопрос. Интерфейс не блокируется."""
from __future__ import annotations

import threading
import traceback

from PySide6.QtCore import QThread, Signal

from bookmind.analysis.pipeline import Progress


class _Base(QThread):
    failed = Signal(str)

    def run(self) -> None:  # noqa: D401
        try:
            self.work()
        except Exception as exc:  # ошибка потока показывается пользователю, а не роняет приложение
            self.failed.emit(f"{exc}\n\n{traceback.format_exc(limit=3)}")

    def work(self) -> None:
        raise NotImplementedError


class AnalysisThread(_Base):
    progress = Signal(object)
    finished_run = Signal(object)

    def __init__(self, ctx, book_id: str, model: str, stages: tuple[str, ...], replace: bool = False, run_id: str | None = None):
        super().__init__()
        self.ctx, self.book_id, self.model, self.stages, self.replace, self.run_id = ctx, book_id, model, stages, replace, run_id
        self.cancel_event = threading.Event()

    def stop(self) -> None:
        self.cancel_event.set()
        self.ctx.client.cancel()

    def work(self) -> None:
        # у потока собственное соединение с БД (Database создаёт соединение на поток)
        p = self.ctx.pipeline()
        self.ctx.client.reset_cancel()
        run_id = self.run_id or p.start(self.book_id, self.model, self.stages, self.replace)
        summary = p.run(run_id, on_progress=lambda pr: self.progress.emit(pr), cancel=self.cancel_event)
        self.finished_run.emit(summary)


class ModelThread(_Base):
    progress = Signal(dict)
    done = Signal(str)

    def __init__(self, ctx, action: str, tag: str = ""):
        super().__init__()
        self.ctx, self.action, self.tag = ctx, action, tag

    def stop(self) -> None:
        self.ctx.client.cancel()

    def work(self) -> None:
        m, cb = self.ctx.models, (lambda e: self.progress.emit(e))
        self.ctx.client.reset_cancel()
        if self.action == "install":
            m.install(self.tag, cb)
            self.done.emit(f"Модель {self.tag} установлена")
        elif self.action == "update":
            r = m.update(self.tag, cb)
            self.done.emit(f"{self.tag}: {'обновлена' if r.changed else 'уже актуальна'}")
        elif self.action == "update_all":
            res = m.update_all(lambda t, e: cb(e))
            self.done.emit("Обновлено: " + (", ".join(r.tag for r in res if r.changed) or "ничего нового"))


class AskThread(_Base):
    answered = Signal(object)

    def __init__(self, ctx, question: str, series_id: str, model: str, up_to: int | None):
        super().__init__()
        self.ctx, self.question, self.series_id, self.model, self.up_to = ctx, question, series_id, model, up_to

    def work(self) -> None:
        self.answered.emit(self.ctx.qa().ask(self.question, self.series_id, self.model, self.up_to))
