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


class AdaptThread(_Base):
    """Создание/продолжение проекта новой книги в фоне."""
    progress = Signal(object)
    created = Signal(str)
    finished_run = Signal(object)

    def __init__(self, ctx, model: str, project_id: str | None = None, series_id: str = "", params=None, title: str = "",
                 replan: list[int] | None = None):
        super().__init__()
        self.ctx, self.model, self.project_id, self.series_id, self.params, self.title = ctx, model, project_id, series_id, params, title
        self.replan = replan
        self.cancel_event = threading.Event()

    def stop(self) -> None:
        self.cancel_event.set()
        self.ctx.client.cancel()

    def work(self) -> None:
        svc = self.ctx.adaptation()
        self.ctx.client.reset_cancel()
        if self.project_id is None:
            self.project_id = svc.create(self.series_id, self.params, self.title)
            self.created.emit(self.project_id)
        progress = lambda p: self.progress.emit(p)      # noqa: E731
        if self.replan:
            self.finished_run.emit(svc.replan(self.project_id, self.model, self.replan, on_progress=progress, cancel=self.cancel_event))
        else:
            self.finished_run.emit(svc.run(self.project_id, self.model, on_progress=progress, cancel=self.cancel_event))


class WriteThread(_Base):
    """Написание текста глав в фоне."""
    progress = Signal(object)
    finished_run = Signal(object)

    def __init__(self, ctx, model: str, project_id: str, chapters: list[int] | None = None, rewrite: bool = False, fix: bool = False):
        super().__init__()
        self.ctx, self.model, self.project_id, self.chapters, self.rewrite, self.fix = ctx, model, project_id, chapters, rewrite, fix
        self.cancel_event = threading.Event()

    def stop(self) -> None:
        self.cancel_event.set()
        self.ctx.client.cancel()

    def work(self) -> None:
        self.ctx.client.reset_cancel()
        if self.fix:
            wr = self.ctx.writer()
            fx = wr.fix_book(self.project_id, self.model, on_progress=lambda p: self.progress.emit(p), cancel=self.cancel_event)
            res = wr._summary(self.project_id, "completed", 0, fx.message)
            res.fixed = fx.fixed
            self.finished_run.emit(res)
            return
        res = self.ctx.writer().write(self.project_id, self.model, self.chapters, self.rewrite,
                                      on_progress=lambda p: self.progress.emit(p), cancel=self.cancel_event)
        self.finished_run.emit(res)


class FullBookThread(_Base):
    """Весь конвейер: анализ серии → план → текст → файл."""
    progress = Signal(object)
    finished_run = Signal(object)

    def __init__(self, ctx, model: str, series_id: str, params=None, project_id: str | None = None, export_path=None):
        super().__init__()
        self.ctx, self.model, self.series_id, self.params, self.project_id, self.export_path = ctx, model, series_id, params, project_id, export_path
        self.cancel_event = threading.Event()

    def stop(self) -> None:
        self.cancel_event.set()
        self.ctx.client.cancel()

    def work(self) -> None:
        from bookmind.creation.full import run_full_book
        self.ctx.client.reset_cancel()
        res = run_full_book(self.ctx.pipeline(), self.ctx.adaptation(), self.ctx.writer(), self.series_id, self.model, self.params,
                            self.project_id, self.export_path, on_progress=lambda p: self.progress.emit(p), cancel=self.cancel_event)
        self.finished_run.emit(res)


class EnvThread(_Base):
    """Проверка окружения в фоне, чтобы окно не подвисало, если Ollama отвечает медленно."""
    result = Signal(list)

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx

    def work(self) -> None:
        from bookmind.system.environment import check_environment
        self.result.emit(check_environment(self.ctx.client, self.ctx.models, self.ctx.data_dir))


class UpdateCheckThread(_Base):
    result = Signal(object)

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx

    def work(self) -> None:
        self.result.emit(self.ctx.updater().check())


class UpdateDownloadThread(_Base):
    progress = Signal(int, int)
    done = Signal(object)

    def __init__(self, ctx, info):
        super().__init__()
        self.ctx, self.info = ctx, info
        self.cancel_event = threading.Event()

    def stop(self) -> None:
        self.cancel_event.set()

    def work(self) -> None:
        path = self.ctx.updater().download(self.info, self.ctx.data_dir / "cache" / "updates",
                                           on_progress=lambda d, t: self.progress.emit(d, t), cancel=self.cancel_event)
        self.done.emit(path)
