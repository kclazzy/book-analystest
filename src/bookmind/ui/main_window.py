"""Главное окно BookMind Local (русский интерфейс)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QSpinBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
    QTextBrowser, QVBoxLayout, QWidget,
)

from bookmind.domain.exceptions import BookMindError
from bookmind.ui import views
from bookmind.ui.context import AppContext
from bookmind.creation.schemas import DIVERGENCE_LABELS, KEEP_KINDS, AdaptParams
from bookmind.system.environment import OLLAMA_DOWNLOAD_URL, blocking_problems, start_ollama
from bookmind.workers.threads import AdaptThread, AnalysisThread, AskThread, EnvThread, ModelThread

STAGE_NAMES = [("core", "Основной анализ"), ("deep", "Цели, отношения, загадки"), ("chapter_summary", "Сводки глав"),
               ("arc_summary", "Сводки арок"), ("series", "Память серии (противоречия)")]
BOOK_FILTER = "Книги (*.txt *.epub *.fb2);;Все файлы (*)"


class TablePage(QWidget):
    """Таблица + подробности выбранной строки с источниками."""

    def __init__(self, win: "MainWindow", key: str):
        super().__init__()
        self.win, self.key = win, key
        self.table = QTableWidget()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.detail = QTextBrowser()
        self.detail.setPlaceholderText("Выберите строку, чтобы увидеть подробности и цитаты-источники")
        self.data = views.TableData([])
        self.extra = QHBoxLayout()
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.detail)
        split.setSizes([400, 200])
        lay = QVBoxLayout(self)
        lay.addLayout(self.extra)
        lay.addWidget(split)
        self.table.itemSelectionChanged.connect(self._show)
        if key == "timeline":
            self.story = QCheckBox("Оценочный фактический порядок (по времени событий)")
            self.story.toggled.connect(self.refresh)
            self.extra.addWidget(self.story)
        if key == "contradictions":
            for text, status in (("Подтвердить", "confirmed"), ("Отклонить", "dismissed")):
                b = QPushButton(text)
                b.clicked.connect(lambda _=False, s=status: self._mark(s))
                self.extra.addWidget(b)
        self.extra.addStretch()

    def refresh(self) -> None:
        sid = self.win.current_series()
        if not sid:
            self.table.setRowCount(0)
            return
        fn = views.VIEWS[self.key]
        kwargs = {"story_order": self.story.isChecked()} if self.key == "timeline" else {}
        self.data = fn(self.win.ctx, sid, self.win.spoiler_limit(), **kwargs)
        headers = self.data.headers[:-1] if self.key == "contradictions" else self.data.headers
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(self.data.rows))
        for r, row in enumerate(self.data.rows):
            for c, v in enumerate(row[:len(headers)]):
                self.table.setItem(r, c, QTableWidgetItem(str(v)))
        self.table.resizeColumnsToContents()
        self.detail.clear()

    def _show(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows and rows[0].row() < len(self.data.details):
            self.detail.setPlainText(self.data.details[rows[0].row()])

    def _mark(self, status: str) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self.win.ctx.knowledge.set_contradiction_status(self.data.rows[rows[0].row()][-1], status)
            self.refresh()


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.thread_analysis: AnalysisThread | None = None
        self.thread_model: ModelThread | None = None
        self.thread_ask: AskThread | None = None
        self.thread_adapt: AdaptThread | None = None
        self.thread_env: EnvThread | None = None
        self.env_checks: list = []
        self._env_warned = False
        self.setWindowTitle("BookMind Local — анализ книг и серий")
        self.resize(1280, 800)
        self.tabs = QTabWidget()
        self.series_box = QComboBox()
        self.spoiler_box = QComboBox()
        top = QWidget()
        h = QHBoxLayout(top)
        h.addWidget(QLabel("Серия:"))
        h.addWidget(self.series_box, 2)
        h.addWidget(QLabel("Показывать тома до:"))
        h.addWidget(self.spoiler_box, 1)
        self.status_label = QLabel("")
        h.addWidget(self.status_label, 3)
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addWidget(top)
        lay.addWidget(self.tabs)
        self.setCentralWidget(central)

        self._build_library()
        self.pages: dict[str, TablePage] = {}
        for key in ("characters", "goals", "relationships", "timeline", "threads", "world", "questions", "motifs", "contradictions"):
            self.pages[key] = TablePage(self, key)
            self.tabs.addTab(self.pages[key], views.TITLES[key])
        self._build_adapt()
        self._build_search()
        self._build_qa()
        self._build_reports()
        self._build_models()
        self._build_system()
        self._build_logs()
        self.series_box.currentIndexChanged.connect(self._series_changed)
        self.spoiler_box.currentIndexChanged.connect(lambda _: self.refresh_pages())
        self.tabs.currentChanged.connect(lambda _: self._tab_changed())
        self.reload_series()
        QTimer.singleShot(0, self.refresh_models)
        QTimer.singleShot(0, lambda: self.check_env(first=True))
        self.env_timer = QTimer(self)
        self.env_timer.timeout.connect(self.check_env)
        self.env_timer.start(15000)

    # ---------- общее ----------
    def log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def error(self, text: str) -> None:
        self.log(f"ОШИБКА: {text}")
        QMessageBox.warning(self, "BookMind", text.split("\n\n")[0])

    def current_series(self) -> str | None:
        return self.series_box.currentData()

    def spoiler_limit(self) -> int | None:
        return self.spoiler_box.currentData()

    def selected_model(self) -> str:
        return self.ctx.models.resolve_model()

    def reload_series(self) -> None:
        keep = self.current_series()
        self.series_box.blockSignals(True)
        self.series_box.clear()
        for s in self.ctx.repo.list_series():
            self.series_box.addItem(s["title"], s["id"])
        i = self.series_box.findData(keep)
        self.series_box.setCurrentIndex(max(i, 0))
        self.series_box.blockSignals(False)
        self._series_changed()

    def _series_changed(self) -> None:
        sid = self.current_series()
        self.spoiler_box.blockSignals(True)
        self.spoiler_box.clear()
        self.spoiler_box.addItem("все тома", None)
        for b in (self.ctx.repo.list_books(sid) if sid else []):
            self.spoiler_box.addItem(f"том {b['series_order']}: {b['title']}", b["series_order"])
        self.spoiler_box.blockSignals(False)
        self.refresh_library()
        self.refresh_pages()
        if hasattr(self, "ad_projects"):
            self.adapt_reload()

    def refresh_pages(self) -> None:
        page = self.tabs.currentWidget()
        if isinstance(page, TablePage):
            page.refresh()

    def _tab_changed(self) -> None:
        self.refresh_pages()

    # ---------- библиотека ----------
    def _build_library(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        for text, fn in (("Импорт книги…", self.import_book), ("Новая серия…", self.new_series),
                         ("Удалить книгу", self.delete_book), ("Удалить результаты анализа", self.purge_book)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        self.books_table = QTableWidget(0, 6)
        self.books_table.setHorizontalHeaderLabels(["№", "Название", "Автор", "Глав", "Фрагментов", "Анализ"])
        self.books_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.books_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.books_table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.books_table, 1)
        stages = QHBoxLayout()
        stages.addWidget(QLabel("Этапы:"))
        self.stage_boxes: dict[str, QCheckBox] = {}
        for key, name in STAGE_NAMES:
            cb = QCheckBox(name)
            cb.setChecked(key in ("core", "deep", "chapter_summary"))
            self.stage_boxes[key] = cb
            stages.addWidget(cb)
        stages.addStretch()
        lay.addLayout(stages)
        ctl = QHBoxLayout()
        self.btn_start = QPushButton("Анализировать / продолжить")
        self.btn_stop = QPushButton("Остановить")
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self.start_analysis)
        self.btn_stop.clicked.connect(self.stop_analysis)
        ctl.addWidget(self.btn_start)
        ctl.addWidget(self.btn_stop)
        ctl.addStretch()
        lay.addLayout(ctl)
        self.progress = QProgressBar()
        self.progress_label = QLabel("Анализ не запущен")
        lay.addWidget(self.progress)
        lay.addWidget(self.progress_label)
        self.tabs.addTab(w, "Библиотека")

    def refresh_library(self) -> None:
        sid = self.current_series()
        books = self.ctx.repo.list_books(sid) if sid else []
        self.books_table.setRowCount(len(books))
        for r, b in enumerate(books):
            st = self.ctx.repo.book_stats(b["id"])
            vals = [b["series_order"], b["title"], b["author"] or "", st["chapters"], st["chunks"],
                    "есть" if self.ctx.repo.has_analysis(b["id"]) else "нет"]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c == 0:
                    it.setData(Qt.UserRole, b["id"])
                self.books_table.setItem(r, c, it)
        self.books_table.resizeColumnsToContents()

    def selected_book(self) -> str | None:
        rows = self.books_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "BookMind", "Выберите книгу в таблице.")
            return None
        return self.books_table.item(rows[0].row(), 0).data(Qt.UserRole)

    def new_series(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        title, ok = QInputDialog.getText(self, "Новая серия", "Название серии:")
        if ok and title.strip():
            self.ctx.library.create_series(title)
            self.reload_series()
            self.series_box.setCurrentIndex(self.series_box.findText(title.strip()))

    def import_book(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Импорт книг", "", BOOK_FILTER)
        for p in paths:
            try:
                res = self.ctx.library.import_book(Path(p), series_id=self.current_series())
                self.log(f"Импорт «{res.title}»: глав {res.chapters}, фрагментов {res.chunks}"
                         + (" (уже был в библиотеке)" if res.already_existed else "") + "".join(f"\n  ! {w}" for w in res.warnings))
            except BookMindError as exc:
                self.error(str(exc))
        self.reload_series()

    def delete_book(self) -> None:
        bid = self.selected_book()
        if bid and QMessageBox.question(self, "Удаление", "Удалить книгу и все результаты её анализа?") == QMessageBox.Yes:
            try:
                self.ctx.library.delete_book(bid)
            except BookMindError as exc:
                self.error(str(exc))
            self.reload_series()

    def purge_book(self) -> None:
        bid = self.selected_book()
        if bid and QMessageBox.question(self, "Удаление анализа", "Удалить результаты анализа книги? Текст книги останется.") == QMessageBox.Yes:
            from bookmind.services.purge import purge_book_analysis
            purge_book_analysis(self.ctx.db, bid)
            self.refresh_library()

    # ---------- анализ ----------
    def start_analysis(self) -> None:
        if self.thread_analysis and self.thread_analysis.isRunning():
            return
        bid = self.selected_book()
        if not bid:
            return
        stages = tuple(k for k, cb in self.stage_boxes.items() if cb.isChecked())
        if not self.ensure_ollama():
            return
        if not stages:
            QMessageBox.information(self, "BookMind", "Выберите хотя бы один этап.")
            return
        try:
            model = self.selected_model()
        except BookMindError as exc:
            self.error(f"{exc}\nУстановите модель на вкладке «Модели».")
            return
        replace = False
        if self.ctx.repo.has_analysis(bid):
            run = self.ctx.pipeline().tasks.find_run(bid, model, self.ctx.pipeline().prompt_version(stages))
            if run is None:
                replace = QMessageBox.question(self, "Повторный анализ",
                    "Книга уже анализировалась другой моделью или версией промптов. Заменить результаты?") == QMessageBox.Yes
                if not replace:
                    return
        t = AnalysisThread(self.ctx, bid, model, stages, replace)
        t.progress.connect(self._on_progress)
        t.finished_run.connect(self._on_finished)
        t.failed.connect(self._analysis_failed)
        self.thread_analysis = t
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.log(f"Запуск анализа: модель {model}, этапы {', '.join(stages)}")
        t.start()

    def stop_analysis(self) -> None:
        if self.thread_analysis:
            self.thread_analysis.stop()
            self.progress_label.setText("Остановка… прогресс сохраняется")

    def _on_progress(self, p) -> None:
        self.progress.setMaximum(max(p.total, 1))
        self.progress.setValue(p.done)
        self.progress_label.setText(f"{p.stage}: {p.done}/{p.total}, ошибок {p.failed} — {p.chapter} ({p.elapsed_s:.0f} с)")

    def _on_finished(self, summary) -> None:
        names = {"completed": "завершён", "paused": "приостановлен", "failed": "завершён с ошибками", "cancelled": "отменён"}
        msg = f"Анализ {names.get(summary.status, summary.status)}. {summary.message}"
        self.progress_label.setText(msg)
        self.log(msg)
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.refresh_library()
        self.refresh_pages()

    def _analysis_failed(self, text: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.error(text)

    # ---------- новая книга по мотивам ----------
    def _build_adapt(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.ad_div = QComboBox()
        for k, label in DIVERGENCE_LABELS.items():
            self.ad_div.addItem(label, k)
        self.ad_div.setCurrentIndex(1)
        self.ad_chapters = QSpinBox()
        self.ad_chapters.setRange(0, 100)
        self.ad_chapters.setSpecialValueText("как в оригинале")
        self.ad_genre = QLineEdit()
        self.ad_genre.setPlaceholderText("например: космическая фантастика (пусто — на выбор модели)")
        self.ad_setting = QLineEdit()
        self.ad_setting.setPlaceholderText("мир и эпоха (пусто — придумает модель)")
        form.addRow("Расхождение с оригиналом:", self.ad_div)
        form.addRow("Число глав:", self.ad_chapters)
        form.addRow("Жанр:", self.ad_genre)
        form.addRow("Мир и эпоха:", self.ad_setting)
        keep_row = QHBoxLayout()
        self.ad_keep: dict[str, QCheckBox] = {}
        for k, label in (("mystery", "загадки"), ("relationship", "отношения"), ("goal", "цели"), ("thread", "линии"), ("flashback", "вставки из прошлого")):
            cb = QCheckBox(label)
            self.ad_keep[k] = cb
            keep_row.addWidget(cb)
        keep_row.addStretch()
        form.addRow("Сохранить структуру:", keep_row)
        self.ad_pause = QCheckBox("Остановиться на проверку плана перед продолжением")
        self.ad_pause.setChecked(True)
        form.addRow("", self.ad_pause)
        lay.addLayout(form)
        row = QHBoxLayout()
        self.ad_projects = QComboBox()
        self.ad_projects.setMinimumWidth(260)
        row.addWidget(QLabel("Проект:"))
        row.addWidget(self.ad_projects, 1)
        self.ad_btns = {}
        for key, text, fn in (("new", "Создать и запустить", self.adapt_new), ("go", "Продолжить", self.adapt_continue),
                              ("stop", "Остановить", self.adapt_stop), ("ok", "Подтвердить план", self.adapt_approve),
                              ("bible", "Показать «библию»", self.adapt_bible), ("save", "Сохранить в файл…", self.adapt_save)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            self.ad_btns[key] = b
            row.addWidget(b)
        lay.addLayout(row)
        self.ad_label = QLabel("Выберите серию с проанализированными книгами и нажмите «Создать и запустить».")
        lay.addWidget(self.ad_label)
        self.ad_table = QTableWidget(0, 5)
        self.ad_table.setHorizontalHeaderLabels(["Глава", "Название", "Статус", "Попыток", "Сходство"])
        self.ad_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ad_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.ad_table.horizontalHeader().setStretchLastSection(True)
        self.ad_detail = QTextBrowser()
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.ad_table)
        split.addWidget(self.ad_detail)
        lay.addWidget(split, 1)
        self.ad_projects.currentIndexChanged.connect(lambda _: self.adapt_refresh())
        self.ad_table.itemSelectionChanged.connect(self._adapt_show_chapter)
        self.tabs.addTab(w, "Новая книга")

    def adapt_reload(self) -> None:
        sid = self.current_series()
        self.ad_projects.blockSignals(True)
        self.ad_projects.clear()
        for p in (self.ctx.adaptation().list_projects(sid) if sid else []):
            st = {"completed": "готово", "awaiting_review": "ждёт проверки плана", "paused": "приостановлено", "failed": "ошибка",
                  "running": "идёт работа", "new": "новый"}.get(p["status"], p["status"])
            self.ad_projects.addItem(f"{p['title']} — {st}", p["id"])
        self.ad_projects.blockSignals(False)
        self.adapt_refresh()

    def adapt_refresh(self) -> None:
        pid = self.ad_projects.currentData()
        self.ad_table.setRowCount(0)
        if not pid:
            return
        rep = self.ctx.adaptation().report(pid)
        names = {"ok": "готова", "flagged": "требует внимания", "pending": "в работе"}
        self.ad_table.setRowCount(len(rep["chapters"]))
        for r, c in enumerate(rep["chapters"]):
            sim = f"{c['check'].get('similarity', 0):.0%}" if c["check"] else ""
            for col, v in enumerate([c["idx"] + 1, c["title"], names.get(c["status"], c["status"]), c["attempts"], sim]):
                self.ad_table.setItem(r, col, QTableWidgetItem(str(v)))
        self.ad_table.resizeColumnsToContents()
        status = {"completed": "готово", "awaiting_review": "ждёт вашей проверки плана", "paused": "приостановлено",
                  "failed": "ошибка", "running": "идёт работа", "new": "новый"}.get(rep["status"], rep["status"])
        self.ad_label.setText(f"Проект «{rep['title']}»: {status}" + (f" — {rep['error']}" if rep["error"] else "")
                              + (f". Требуют внимания главы: {', '.join(str(i + 1) for i in rep['flagged'])}" if rep["flagged"] else ""))

    def _adapt_show_chapter(self) -> None:
        rows = self.ad_table.selectionModel().selectedRows()
        pid = self.ad_projects.currentData()
        if not rows or not pid:
            return
        idx = int(self.ad_table.item(rows[0].row(), 0).text()) - 1
        svc = self.ctx.adaptation()
        plan = svc.plans(pid).get(idx)
        rep = next(c for c in svc.report(pid)["chapters"] if c["idx"] == idx)
        if plan:
            lines = [plan.title, "", plan.summary, "", f"Точка зрения: {plan.pov}; место: {plan.location}", f"Цель: {plan.goal}",
                     f"Конфликт: {plan.conflict}", f"Итог: {plan.outcome}", f"Крючок: {plan.hook}"]
            lines += ["", "Замечания проверки:", *[f"  ⚠ {i}" for i in rep["issues"]]] if rep["issues"] else []
            self.ad_detail.setPlainText("\n".join(lines))

    def _adapt_params(self) -> AdaptParams:
        return AdaptParams(divergence=self.ad_div.currentData(), chapters=self.ad_chapters.value() or None,
                           genre=self.ad_genre.text().strip(), setting=self.ad_setting.text().strip(),
                           keep=[k for k, cb in self.ad_keep.items() if cb.isChecked()], pause_after_plan=self.ad_pause.isChecked())

    def _adapt_start(self, pid: str | None) -> None:
        if self.thread_adapt and self.thread_adapt.isRunning():
            return
        sid = self.current_series()
        if not sid:
            QMessageBox.information(self, "BookMind", "Сначала выберите серию.")
            return
        if not self.ensure_ollama():
            return
        try:
            model = self.selected_model()
        except BookMindError as exc:
            self.error(str(exc))
            return
        t = AdaptThread(self.ctx, model, pid, sid, None if pid else self._adapt_params(), "Новая книга по мотивам")
        t.progress.connect(lambda p: self.ad_label.setText(f"{p.stage}: {p.done}/{p.total} {p.message}"))
        t.created.connect(lambda _: self.adapt_reload())
        t.finished_run.connect(self._adapt_finished)
        t.failed.connect(self._adapt_failed)
        self.thread_adapt = t
        t.start()

    def adapt_new(self) -> None:
        self._adapt_start(None)

    def adapt_continue(self) -> None:
        pid = self.ad_projects.currentData()
        if pid:
            self._adapt_start(pid)

    def adapt_stop(self) -> None:
        if self.thread_adapt:
            self.thread_adapt.stop()

    def adapt_approve(self) -> None:
        pid = self.ad_projects.currentData()
        if pid:
            self.ctx.adaptation().approve(pid)
            self._adapt_start(pid)

    def _adapt_finished(self, s) -> None:
        self.log(f"Новая книга: {s.status}. {s.message}")
        idx = self.ad_projects.findData(s.project_id)
        self.adapt_reload()
        self.ad_projects.setCurrentIndex(max(self.ad_projects.findData(s.project_id), 0))
        self.adapt_refresh()

    def _adapt_failed(self, text: str) -> None:
        self.error(text)
        self.adapt_reload()

    def adapt_bible(self) -> None:
        pid = self.ad_projects.currentData()
        if pid:
            self.ad_detail.setMarkdown(self.ctx.adaptation().bible_markdown(pid))

    def adapt_save(self) -> None:
        pid = self.ad_projects.currentData()
        if not pid:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить «библию»", str(self.ctx.data_dir / "exports" / "new_book_bible.md"), "Markdown (*.md)")
        if path:
            self.ctx.adaptation().export_bible(pid, Path(path))
            self.log(f"Сохранено: {path}")

    # ---------- поиск ----------
    def _build_search(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Поиск по книгам и базе знаний…")
        btn = QPushButton("Найти")
        row.addWidget(self.search_edit)
        row.addWidget(btn)
        lay.addLayout(row)
        self.search_results = QTextBrowser()
        lay.addWidget(self.search_results)
        btn.clicked.connect(self.do_search)
        self.search_edit.returnPressed.connect(self.do_search)
        self.tabs.addTab(w, "Поиск")

    def do_search(self) -> None:
        sid, q = self.current_series(), self.search_edit.text().strip()
        if not sid or not q:
            return
        books = [b["id"] for b in self.ctx.repo.list_books(sid) if self.spoiler_limit() is None or b["series_order"] <= self.spoiler_limit()]
        hits = self.ctx.search.search(q, series_id=sid, book_ids=books, limit=30)
        kinds = {"chunk": "текст", "fact": "факт", "event": "событие", "entity": "сущность", "question": "загадка", "motif": "мотив"}
        out = []
        for h in hits:
            row = self.ctx.db.one("SELECT text FROM chunks WHERE id = ?", (h.ref_id,)) if h.kind == "chunk" else None
            out.append(f"[{kinds.get(h.kind, h.kind)}] {h.title}" + (f"\n    {row['text'][:240]}…" if row else ""))
        self.search_results.setPlainText("\n\n".join(out) or "Ничего не найдено")

    # ---------- вопросы ----------
    def _build_qa(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.qa_edit = QLineEdit()
        self.qa_edit.setPlaceholderText("Задайте вопрос о книге. Ответ строится только по найденным источникам…")
        self.qa_btn = QPushButton("Спросить")
        row.addWidget(self.qa_edit)
        row.addWidget(self.qa_btn)
        lay.addLayout(row)
        self.qa_view = QTextBrowser()
        lay.addWidget(self.qa_view)
        self.qa_btn.clicked.connect(self.ask)
        self.qa_edit.returnPressed.connect(self.ask)
        self.tabs.addTab(w, "Вопросы")

    def ask(self) -> None:
        sid, q = self.current_series(), self.qa_edit.text().strip()
        if not sid or not q or (self.thread_ask and self.thread_ask.isRunning()) or not self.ensure_ollama():
            return
        try:
            model = self.selected_model()
        except BookMindError as exc:
            self.error(str(exc))
            return
        self.qa_btn.setEnabled(False)
        self.qa_view.setPlainText("Ищу материалы и формирую ответ…")
        t = AskThread(self.ctx, q, sid, model, self.spoiler_limit())
        t.answered.connect(self._answered)
        t.failed.connect(lambda text: (self.qa_btn.setEnabled(True), self.error(text)))
        self.thread_ask = t
        t.start()

    def _answered(self, a) -> None:
        self.qa_btn.setEnabled(True)
        lines = [a.answer, ""]
        if a.insufficient:
            lines.append("(Недостаточно данных: ответ не подтверждён источниками)")
        for c in a.citations:
            lines.append(f"[{c.label}] {c.text}\n    {c.book_title}, {c.chapter_title}" + (f"\n    «{c.quote}»" if c.quote else ""))
        if a.issues:
            lines += ["", "Замечания: " + "; ".join(a.issues)]
        self.qa_view.setPlainText("\n".join(lines))

    # ---------- отчёты и резервные копии ----------
    def _build_reports(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Экспорт базы знаний выбранной серии (только подтверждённые данные; учитывается фильтр томов сверху):"))
        row = QHBoxLayout()
        for text, fmt in (("Markdown", "md"), ("JSON", "json"), ("CSV", "csv"), ("HTML", "html")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fmt: self.export(f))
            row.addWidget(b)
        b = QPushButton("Отчёт по выбранной книге")
        b.clicked.connect(self.book_report)
        row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        lay.addWidget(QLabel("Резервные копии:"))
        row2 = QHBoxLayout()
        for text, fn in (("Создать копию…", self.make_backup), ("Восстановить из копии…", self.restore_backup)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row2.addWidget(b)
        row2.addStretch()
        lay.addLayout(row2)
        self.report_view = QTextBrowser()
        lay.addWidget(self.report_view, 1)
        self.tabs.addTab(w, "Отчёты")

    def export(self, fmt: str) -> None:
        sid = self.current_series()
        if not sid:
            return
        out = self.ctx.data_dir / "exports"
        try:
            if fmt == "csv":
                folder = QFileDialog.getExistingDirectory(self, "Папка для CSV", str(out))
                if folder:
                    files = self.ctx.exports.export_csv(sid, Path(folder), self.spoiler_limit())
                    self.log(f"CSV: {len(files)} файлов → {folder}")
                return
            ext = {"md": "Markdown (*.md)", "json": "JSON (*.json)", "html": "HTML (*.html)"}[fmt]
            path, _ = QFileDialog.getSaveFileName(self, "Сохранить экспорт", str(out / f"export.{fmt}"), ext)
            if path:
                getattr(self.ctx.exports, {"md": "export_markdown", "json": "export_json", "html": "export_html"}[fmt])(
                    sid, Path(path), self.spoiler_limit())
                self.log(f"Экспорт сохранён: {path}")
        except (BookMindError, OSError, ValueError) as exc:
            self.error(str(exc))

    def book_report(self) -> None:
        bid = self.selected_book()
        if bid:
            self.report_view.setMarkdown(self.ctx.reports.build_book_report(bid))

    def make_backup(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Резервная копия", str(self.ctx.data_dir / "backups" / "bookmind.zip"), "ZIP (*.zip)")
        if path:
            try:
                self.log(f"Резервная копия: {self.ctx.backup.create(Path(path))}")
            except (BookMindError, OSError) as exc:
                self.error(str(exc))

    def restore_backup(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Восстановление", str(self.ctx.data_dir / "backups"), "ZIP (*.zip)")
        if not path:
            return
        try:
            manifest = self.ctx.backup.verify(Path(path))
        except BookMindError as exc:
            self.error(str(exc))
            return
        c = manifest["counts"]
        if QMessageBox.question(self, "Восстановление", f"Копия: книг {c['books']}, фактов {c['facts']}. Текущие данные будут сохранены в backups/pre-restore-*. Продолжить?") != QMessageBox.Yes:
            return
        self.ctx.db.close()
        try:
            keep = self.ctx.backup.restore(Path(path))
            self.log(f"Восстановлено. Прежние данные: {keep}")
        except BookMindError as exc:
            self.error(str(exc))
        finally:
            self.ctx.open_db()
            self.reload_series()

    # ---------- модели ----------
    def _build_models(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.models_table = QTableWidget(0, 5)
        self.models_table.setHorizontalHeaderLabels(["Модель", "Установлена", "Выбрана", "Размер, ГБ", "Описание"])
        self.models_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.models_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.models_table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.models_table, 1)
        row = QHBoxLayout()
        for text, fn in (("Обновить список", self.refresh_models), ("Установить", lambda: self.model_action("install")),
                         ("Обновить модель", lambda: self.model_action("update")), ("Обновить все", lambda: self.model_action("update_all")),
                         ("Выбрать для анализа", self.select_model), ("Прервать", self.cancel_model)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        self.model_progress = QProgressBar()
        self.model_label = QLabel("")
        lay.addWidget(self.model_progress)
        lay.addWidget(self.model_label)
        self.tabs.addTab(w, "Модели")

    def refresh_models(self) -> None:
        try:
            sts = self.ctx.models.statuses()
            self.status_label.setText("Ollama: подключено")
        except (BookMindError, RuntimeError) as exc:
            sts = []
            self.status_label.setText("Ollama недоступен — запустите Ollama")
            self.log(f"Ollama: {exc}")
        self.models_table.setRowCount(len(sts))
        for r, s in enumerate(sts):
            vals = [s.tag, "да" if s.installed else "нет", "★" if s.selected else "",
                    f"{s.size_bytes / 1e9:.1f}" if s.size_bytes else "", s.profile.description if s.profile else "(вне каталога)"]
            for c, v in enumerate(vals):
                self.models_table.setItem(r, c, QTableWidgetItem(v))
        self.models_table.resizeColumnsToContents()

    def _model_tag(self) -> str | None:
        rows = self.models_table.selectionModel().selectedRows()
        return self.models_table.item(rows[0].row(), 0).text() if rows else None

    def select_model(self) -> None:
        tag = self._model_tag()
        if not tag:
            return
        try:
            self.ctx.models.select(tag)
            self.ctx.save_settings()
            self.refresh_models()
        except BookMindError as exc:
            self.error(str(exc))

    def model_action(self, action: str) -> None:
        tag = self._model_tag() or ""
        if action != "update_all" and not tag:
            QMessageBox.information(self, "BookMind", "Выберите модель в таблице.")
            return
        if (self.thread_model and self.thread_model.isRunning()) or not self.ensure_ollama():
            return
        t = ModelThread(self.ctx, action, tag)
        t.progress.connect(self._model_progress)
        t.done.connect(lambda m: (self.model_label.setText(m), self.log(m), self.refresh_models()))
        t.failed.connect(self.error)
        self.thread_model = t
        t.start()

    def cancel_model(self) -> None:
        if self.thread_model:
            self.thread_model.stop()

    def _model_progress(self, e: dict) -> None:
        total, done = e.get("total"), e.get("completed")
        if total and done is not None:
            self.model_progress.setMaximum(100)
            self.model_progress.setValue(done * 100 // total)
        self.model_label.setText(str(e.get("status", "")))

    # ---------- проверка системы ----------
    def _build_system(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Для работы нужны Ollama (запущенный) и хотя бы одна модель Qwen. Состояние обновляется автоматически."))
        self.env_table = QTableWidget(0, 3)
        self.env_table.setHorizontalHeaderLabels(["Проверка", "Состояние", "Подробности"])
        self.env_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.env_table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.env_table, 1)
        row = QHBoxLayout()
        for text, fn in (("Запустить Ollama", self.start_ollama_clicked), ("Скачать Ollama", self.open_ollama_download),
                         ("Перейти к моделям", lambda: self.tabs.setCurrentWidget(self.models_table.parentWidget())),
                         ("Перепроверить", lambda: self.check_env(force=True))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        self.tabs.addTab(w, "Система")
        self.sys_tab = w

    def check_env(self, first: bool = False, force: bool = False) -> None:
        if self.thread_env and self.thread_env.isRunning():
            return
        t = EnvThread(self.ctx)
        t.result.connect(lambda checks: self._env_done(checks, first))
        t.failed.connect(lambda text: self.log(f"Проверка системы: {text.splitlines()[0]}"))
        self.thread_env = t
        t.start()

    def _env_done(self, checks: list, first: bool) -> None:
        self.env_checks = checks
        icons = {"ok": "✔ в порядке", "warn": "⚠ внимание", "error": "✖ проблема"}
        self.env_table.setRowCount(len(checks))
        for r, c in enumerate(checks):
            for col, v in enumerate([c.title, icons[c.level], c.detail]):
                self.env_table.setItem(r, col, QTableWidgetItem(v))
        self.env_table.resizeColumnsToContents()
        bad = blocking_problems(checks)
        if bad:
            self.status_label.setText("⚠ " + bad[0].title.replace("установлен", "не готов").replace("запущен", "не запущен") + " — вкладка «Система»")
            if first and not self._env_warned:
                self._env_warned = True
                self.tabs.setCurrentWidget(self.sys_tab)
                box = QMessageBox(QMessageBox.Warning, "Проверка системы",
                                  "Для работы нужно:\n\n" + "\n".join(f"• {c.title}: {c.detail}" for c in bad), QMessageBox.Ok, self)
                box.setWindowModality(Qt.NonModal)      # не блокирует окно: можно сразу нажать «Запустить Ollama»
                box.show()
                self._env_box = box
        else:
            self.status_label.setText("Ollama запущен, модель готова")
            self._env_warned = False
        self.refresh_models_quiet()

    def refresh_models_quiet(self) -> None:
        if any(c.key == "ollama_running" and c.ok for c in self.env_checks) and self.models_table.rowCount() == 0:
            self.refresh_models()

    def start_ollama_clicked(self) -> None:
        ok, msg = start_ollama(self.ctx.client)
        self.log(msg)
        if not ok:
            QMessageBox.warning(self, "Ollama", msg)
        self.check_env(force=True)

    def open_ollama_download(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(OLLAMA_DOWNLOAD_URL))

    def ensure_ollama(self) -> bool:
        """Перед тяжёлой операцией: если Ollama не отвечает, предлагает запустить его."""
        if self.ctx.client.is_available():
            return True
        if QMessageBox.question(self, "Ollama не запущен", "Для этого действия нужен запущенный Ollama. Запустить его сейчас?") != QMessageBox.Yes:
            return False
        ok, msg = start_ollama(self.ctx.client)
        self.log(msg)
        if not ok:
            QMessageBox.warning(self, "Ollama", msg)
        self.check_env(force=True)
        return ok

    # ---------- журнал ----------
    def _build_logs(self) -> None:
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.tabs.addTab(self.log_view, "Журнал")

    def closeEvent(self, event) -> None:  # noqa: N802
        for t in (self.thread_analysis, self.thread_model):
            if t and t.isRunning():
                if hasattr(t, "stop"):
                    t.stop()
                t.wait(5000)
        super().closeEvent(event)
