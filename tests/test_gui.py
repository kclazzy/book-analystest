"""Дымовые тесты окна без экрана (QT_QPA_PLATFORM=offscreen). Проверяют сборку окна, фоновый анализ и заполнение вкладок."""
import os

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from bookmind.analysis.pipeline import AnalysisPipeline  # noqa: E402
from bookmind.llm.ollama_client import OllamaClient  # noqa: E402
from bookmind.ui.context import AppContext  # noqa: E402
from bookmind.ui.main_window import MainWindow, TablePage  # noqa: E402
from bookmind.ui import views  # noqa: E402
from bookmind.workers.threads import AdaptThread, AnalysisThread, AskThread, ModelThread  # noqa: E402
from tests.conftest import FakeOllama  # noqa: E402
from tests.fixtures.builders import write_txt  # noqa: E402
from tests.fixtures.novel import BOOK1, BOOK1_TITLE  # noqa: E402
from tests.fixtures.scripted_llm import ScriptedLLM  # noqa: E402

MODEL = "qwen3.5:9b-q4_K_M"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ctx(tmp_path, qapp):
    fake = FakeOllama()
    client = OllamaClient("http://test", max_retries=1, transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)
    c = AppContext(tmp_path / "data", client=client)
    c.fake = fake
    llm = ScriptedLLM()
    c.pipeline = lambda: AnalysisPipeline(c.db, llm, c.settings, c.data_dir, c.prompts)
    c.llm = llm
    yield c
    c.close()


def wait(thread, ms=20000):
    assert thread.wait(ms)
    QCoreApplication.processEvents()


def test_window_builds_with_russian_tabs_and_models(ctx, qapp):
    win = MainWindow(ctx)
    names = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    assert names[0] == "Библиотека" and {"Персонажи", "Цели", "Отношения", "Хронология", "Загадки", "Противоречия", "Поиск", "Вопросы", "Отчёты", "Модели", "Журнал"} <= set(names)
    win.refresh_models()
    tags = [win.models_table.item(r, 0).text() for r in range(win.models_table.rowCount())]
    assert MODEL in tags and "Ollama: подключено" in win.status_label.text()
    installed = {win.models_table.item(r, 0).text(): win.models_table.item(r, 1).text() for r in range(win.models_table.rowCount())}
    assert installed[MODEL] == "да" and installed["qwen3:14b"] == "нет"
    win.close()


def test_window_when_ollama_is_down_shows_message(tmp_path, qapp):
    def down(request):
        raise httpx.ConnectError("refused")
    client = OllamaClient("http://test", max_retries=0, transport=httpx.MockTransport(down), sleep=lambda s: None)
    c = AppContext(tmp_path / "d", client=client)
    win = MainWindow(c)
    win.refresh_models()
    assert "недоступен" in win.status_label.text()
    win.close()
    c.close()


def test_import_analyze_in_background_and_browse(ctx, qapp, tmp_path):
    res = ctx.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    win = MainWindow(ctx)
    assert win.books_table.rowCount() == 1 and win.books_table.item(0, 5).text() == "нет"
    t = AnalysisThread(ctx, res.book_id, MODEL, ("core", "deep", "chapter_summary"))
    seen = []
    t.progress.connect(lambda p: seen.append(p.stage))
    results = []
    t.finished_run.connect(results.append)
    t.start()
    wait(t)
    assert results and results[0].status == "completed" and {"core", "deep"} <= set(seen)
    win.refresh_library()
    assert win.books_table.item(0, 5).text() == "есть"
    expect = {"characters": "Мира Ветрова", "goals": "Найти убийцу", "questions": "серебряный ключ", "timeline": "Мира"}
    for key, needle in expect.items():
        page: TablePage = win.pages[key]
        win.tabs.setCurrentWidget(page)
        page.refresh()
        assert page.table.rowCount() > 0, key
        assert any(needle in page.table.item(r, c).text() for r in range(page.table.rowCount()) for c in range(page.table.columnCount())), key
        page.table.selectRow(0)
        assert page.detail.toPlainText().strip(), key
    win.close()


def test_views_show_sources_and_unknown_state(ctx, tmp_path):
    res = ctx.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    p = ctx.pipeline()
    p.run(p.start(res.book_id, MODEL, ("core", "deep")))
    sid = ctx.repo.get_book(res.book_id)["series_id"]
    chars = views.characters(ctx, sid)
    mira = chars.details[[r[0] for r in chars.rows].index("Мира Ветрова")]
    assert "«" in mira and "Источники" in mira
    goals = views.goals(ctx, sid)
    assert any("Активна → Отложена" in d for d in goals.details)
    assert views.contradictions(ctx, sid).rows == []


def test_spoiler_filter_in_views(ctx, tmp_path):
    from tests.fixtures.novel import BOOK2, BOOK2_TITLE
    r1 = ctx.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    p = ctx.pipeline()
    p.run(p.start(r1.book_id, MODEL, ("core", "deep")))
    sid = ctx.repo.get_book(r1.book_id)["series_id"]
    r2 = ctx.library.import_book(write_txt(tmp_path / "b2.txt", BOOK2), title=BOOK2_TITLE, series_id=sid)
    p.run(p.start(r2.book_id, MODEL, ("core", "deep")))
    all_rows = views.timeline(ctx, sid).rows
    v1 = views.timeline(ctx, sid, up_to=1).rows
    assert len(v1) < len(all_rows) and all(r[0] == BOOK1_TITLE for r in v1)


def test_ask_thread_returns_answer(ctx, tmp_path):
    res = ctx.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    p = ctx.pipeline()
    p.run(p.start(res.book_id, MODEL, ("core",)))
    sid = ctx.repo.get_book(res.book_id)["series_id"]
    ctx.client_chat = None
    ctx.client = ctx.llm   # вопрос отвечает сценарная модель
    ctx.llm.extra["answer"] = {"handler": lambda prompt: {"answer": "Мира носит серебряный ключ [F1].", "sources": ["F1"], "insufficient": False}}
    t = AskThread(ctx, "Что известно о серебряном ключе?", sid, MODEL, None)
    got = []
    t.answered.connect(got.append)
    t.start()
    wait(t)
    assert got and not got[0].insufficient and got[0].citations


def test_model_thread_install_and_update(ctx):
    t = ModelThread(ctx, "install", "qwen3:8b")
    done = []
    t.done.connect(done.append)
    t.start()
    wait(t)
    assert done and "qwen3:8b" in ctx.fake.models
    t2 = ModelThread(ctx, "update", "qwen3:8b")
    t2.done.connect(done.append)
    t2.start()
    wait(t2)
    assert len(done) == 2


def test_new_book_tab_creates_and_shows_plan(ctx, qapp, tmp_path):
    from bookmind.analysis.extractor import StructuredCaller
    from bookmind.creation.schemas import AdaptParams
    from bookmind.creation.service import AdaptationService
    res = ctx.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    p = ctx.pipeline()
    assert p.run(p.start(res.book_id, MODEL, ("core", "deep", "chapter_summary"))).status == "completed"
    ctx.adaptation = lambda: AdaptationService(ctx.db, StructuredCaller(ctx.llm, ctx.settings), ctx.settings, ctx.prompts)
    win = MainWindow(ctx)
    assert "Новая книга" in [win.tabs.tabText(i) for i in range(win.tabs.count())]
    sid = win.current_series()
    t = AdaptThread(ctx, MODEL, None, sid, AdaptParams(seed=1, pause_after_plan=True), "Тест")
    got, made = [], []
    t.finished_run.connect(got.append)
    t.created.connect(made.append)
    t.start()
    wait(t)
    assert got and got[0].status == "awaiting_review" and made
    win.adapt_reload()
    assert win.ad_projects.count() == 1 and win.ad_table.rowCount() == 8
    assert "ждёт вашей проверки" in win.ad_label.text()
    win.ad_table.selectRow(0)
    assert "Отсек 1" in win.ad_detail.toPlainText()
    win.adapt_bible()
    assert "Тишина на орбите" in win.ad_detail.toPlainText()
    win.close()


def test_system_tab_reports_state_and_blocks_nothing_when_ready(ctx, qapp):
    win = MainWindow(ctx)
    win.check_env()
    wait(win.thread_env)
    assert win.env_table.rowCount() >= 5
    rows = {win.env_table.item(r, 0).text(): win.env_table.item(r, 1).text() for r in range(win.env_table.rowCount())}
    assert rows["Ollama запущен"].startswith("✔") and rows["Модель Qwen установлена"].startswith("✔")
    assert "готова" in win.status_label.text()
    win.close()


def test_system_tab_when_ollama_missing(tmp_path, qapp, monkeypatch):
    from bookmind.system import environment as env
    monkeypatch.setattr(env, "find_ollama_exe", lambda *a, **k: None)
    def down(request):
        raise httpx.ConnectError("refused")
    c = AppContext(tmp_path / "d", client=OllamaClient("http://test", max_retries=0, transport=httpx.MockTransport(down), sleep=lambda s: None))
    win = MainWindow(c)
    win.check_env(first=True)
    wait(win.thread_env)
    rows = {win.env_table.item(r, 0).text(): win.env_table.item(r, 1).text() for r in range(win.env_table.rowCount())}
    assert rows["Ollama установлен"].startswith("✖") and rows["Ollama запущен"].startswith("✖")
    assert "Система" in win.status_label.text() and win.tabs.currentWidget() is win.sys_tab
    win.close()
    c.close()


def _fake_update(available=True, frozen=True):
    from bookmind.system.updater import UpdateCheck, UpdateInfo
    info = UpdateInfo("0.9.0", "v0.9.0", "https://github.com/x/BookMind-Setup-0.9.0.exe", "BookMind-Setup-0.9.0.exe", 10, "a" * 64,
                      "Новые возможности", "https://github.com/x/releases/tag/v0.9.0")
    return UpdateCheck("0.2.1", info, available)


def test_update_ui_shows_available_version(ctx, qapp, monkeypatch):
    from bookmind.ui import main_window as mw
    monkeypatch.setattr(mw, "is_frozen", lambda: True)
    win = MainWindow(ctx)
    win._update_checked(_fake_update(), manual=True)
    assert "0.9.0" in win.up_label.text() and win.up_install_btn.isEnabled() and "Новые возможности" in win.up_notes.toPlainText()
    assert not win.update_btn.isHidden() and "0.9.0" in win.update_btn.text()
    win._update_checked(_fake_update(available=False), manual=True)
    assert win.update_btn.isHidden() and not win.up_install_btn.isEnabled() and "последняя" in win.up_label.text()
    win.close()


def test_update_ui_from_source_blocks_auto_install(ctx, qapp, monkeypatch):
    from bookmind.ui import main_window as mw
    monkeypatch.setattr(mw, "is_frozen", lambda: False)
    win = MainWindow(ctx)
    win._update_checked(_fake_update(), manual=True)
    assert not win.up_install_btn.isEnabled() and "git pull" in win.up_label.text()
    win.close()


def test_update_error_is_quiet_on_startup_and_visible_on_manual(ctx, qapp):
    from bookmind.system.updater import UpdateCheck
    win = MainWindow(ctx)
    res = UpdateCheck("0.2.1", error="Не удалось проверить обновления (нет сети?): ConnectError")
    win._update_checked(res, manual=False)
    assert "нет сети" not in win.up_label.text()
    win._update_checked(res, manual=True)
    assert "нет сети" in win.up_label.text()
    win.close()


def test_apply_update_makes_backup_then_launches(ctx, tmp_path):
    res = ctx.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    launched = []
    backup = ctx.apply_update(tmp_path / "setup.exe", launch=launched.append)
    assert launched == [tmp_path / "setup.exe"] and backup and backup.exists() and backup.name.startswith("pre-update-")
    assert ctx.backup.verify(backup)["counts"]["books"] == 1


def test_gui_change_data_dir_and_reset(ctx, qapp, tmp_path, monkeypatch):
    from bookmind import config
    monkeypatch.setattr(config, "home_dir", lambda: tmp_path / "home")
    monkeypatch.delenv(config.ENV_DATA_DIR, raising=False)
    win = MainWindow(ctx)
    assert str(ctx.data_dir) in win.data_label.text()
    assert win.apply_data_dir(tmp_path / "gui_target")
    assert "Перезапустите" in win.data_label.text() and config.read_pointer() == tmp_path / "gui_target"
    win.reset_data_dir()
    assert config.read_pointer() is None
    win.close()


def _book_ctx(ctx, tmp_path):
    from bookmind.analysis.extractor import StructuredCaller
    from bookmind.creation.service import AdaptationService
    from bookmind.creation.writer import BookWriter
    ctx.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    ctx.adaptation = lambda: AdaptationService(ctx.db, StructuredCaller(ctx.llm, ctx.settings), ctx.settings, ctx.prompts)
    ctx.writer = lambda: BookWriter(ctx.adaptation())


def test_full_book_button_runs_whole_conveyor(ctx, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    _book_ctx(ctx, tmp_path)
    win = MainWindow(ctx)
    out = tmp_path / "result.epub"
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    win.ad_words.setValue(300)
    win.ad_pause.setChecked(True)               # для полного конвейера пауза на проверку плана не должна мешать
    win.full_book()
    wait(win.thread_adapt, 60000)
    assert out.exists() and "Книга готова" in win.ad_label.text()
    assert win.ad_table.rowCount() == 8 and win.ad_table.item(0, 5).text().startswith("готов")
    win.ad_table.selectRow(0)
    assert "ТЕКСТ ГЛАВЫ" in win.ad_detail.toPlainText()
    win.close()


def test_write_buttons_and_save_book(ctx, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    from bookmind.creation.schemas import AdaptParams
    _book_ctx(ctx, tmp_path)
    p = ctx.pipeline()
    assert p.run(p.start(ctx.repo.list_books()[0]["id"], MODEL, ("core", "chapter_summary"))).status == "completed"
    win = MainWindow(ctx)
    t = AdaptThread(ctx, MODEL, None, win.current_series(), AdaptParams(seed=1, chapter_words=300), "Тест")
    t.start()
    wait(t)
    win.adapt_reload()
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    win.write_selected()
    assert shown and "Выберите главу" in shown[0]
    win.ad_table.selectRow(2)
    win.ad_words.setValue(300)
    win.write_selected()
    wait(win.thread_adapt)
    assert win.ad_table.item(2, 5).text().startswith("готов") and win.ad_table.item(0, 5).text() == "—"
    out = tmp_path / "part.md"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    win.book_save()
    assert "## Глава 3" in out.read_text(encoding="utf-8")
    win.write_all()
    wait(win.thread_adapt)
    assert all(win.ad_table.item(r, 5).text().startswith("готов") for r in range(8))
    win.close()


def test_error_journal_tab_shows_filters_and_reports(ctx, qapp, tmp_path, monkeypatch):
    import zipfile
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    win = MainWindow(ctx)
    idx = win.tabs.indexOf(win.err_tab)
    assert win.tabs.tabText(idx) == "Ошибки" and win.err_table.rowCount() == 0
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    win.error("Не удалось что-то сделать\n\nTraceback (most recent call last):\nValueError: x")
    ctx.errors.record("мелочь", source="тест", level="WARNING")
    win._errors_poll()
    assert win.tabs.tabText(idx) == "Ошибки (2)" and win.err_table.rowCount() == 2
    win.err_level.setCurrentIndex(win.err_level.findData("ERROR"))
    assert win.err_table.rowCount() == 1
    win.err_table.selectRow(0)
    assert "Traceback" in win.err_detail.toPlainText() and "Не удалось что-то сделать" in win.err_detail.toPlainText()
    win.errors_copy()
    assert "ValueError: x" in QApplication.clipboard().text()
    out = tmp_path / "rep.zip"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    win.errors_report()
    with zipfile.ZipFile(out) as z:
        assert "Проверка: " in z.read("system.txt").decode() and "Не удалось что-то сделать" in z.read("errors.txt").decode()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    win.errors_clear()
    assert win.err_table.rowCount() == 0 and win.tabs.tabText(idx) == "Ошибки"
    win.close()


def test_thread_failure_lands_in_error_journal(ctx, qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    win = MainWindow(ctx)
    win._analysis_failed("Ollama упал\n\nTraceback: ...")
    assert ctx.errors.entries()[0].message == "Ollama упал" and "Traceback" in ctx.errors.entries()[0].traceback
    win.close()


def test_changing_folder_asks_about_transfer(ctx, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    from bookmind import config
    monkeypatch.setattr(config, "home_dir", lambda: tmp_path / "home")
    monkeypatch.delenv(config.ENV_DATA_DIR, raising=False)
    win = MainWindow(ctx)
    asked = []

    def pick(folder, answer):
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(folder))
        monkeypatch.setattr(win, "_ask_transfer", lambda path, t, c: asked.append((t, c)) or answer)
        win.choose_data_dir()

    pick(tmp_path / "t0", "copy")                                   # данных нет — вопроса нет, папка просто меняется
    assert asked == [] and config.read_pointer() == tmp_path / "t0"
    config.write_pointer(None)
    ctx.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    pick(tmp_path / "t1", None)                                     # отмена — ничего не меняется
    assert asked == [(False, True)] and config.read_pointer() is None and not (tmp_path / "t1" / "database").exists()
    pick(tmp_path / "t2", "empty")                                  # начать с пустой
    assert config.read_pointer() == tmp_path / "t2" and not (tmp_path / "t2" / "database" / "bookmind.sqlite").exists()
    pick(tmp_path / "t3", "copy")                                   # перенести
    import sqlite3
    con = sqlite3.connect(tmp_path / "t3" / "database" / "bookmind.sqlite")
    assert con.execute("select count(*) from books").fetchone()[0] == 1
    con.close()
    assert (ctx.data_dir / "database" / "bookmind.sqlite").exists()
    asked.clear()
    pick(tmp_path / "t3", "use")                                    # в t3 уже есть данные — спрашивает про использование
    assert asked == [(True, True)] and config.read_pointer() == tmp_path / "t3"
    win.close()


def test_retry_failed_button(ctx, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    ctx.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    win = MainWindow(ctx)
    win.refresh_library()
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    win.retry_failed_analysis()
    assert shown == ["Выберите книгу в таблице."]
    win.books_table.selectRow(0)
    win.retry_failed_analysis()
    assert shown[-1] == "Неудавшихся задач нет." and win.thread_analysis is None
    win.close()


def test_dossier_buttons_save_files_and_copy(ctx, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    res = ctx.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    p = ctx.pipeline()
    assert p.run(p.start(res.book_id, MODEL, ("core", "deep", "chapter_summary"))).status == "completed"
    win = MainWindow(ctx)
    out = tmp_path / "x" / "dossier.md"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    assert win.dossier_quotes.currentData() == "none"                  # безопасный режим по умолчанию
    win.export_dossier("md")
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Досье") and "Цитаты:" not in text
    win.dossier_parts.setValue(4)
    win.export_dossier("md")
    assert len(list(out.parent.glob("dossier_*of*.md"))) >= 2
    jpath = tmp_path / "x" / "d.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(jpath), ""))
    win.export_dossier("json")
    assert jpath.exists()
    win.copy_dossier()
    assert QApplication.clipboard().text().startswith("# Досье")
    win.close()


def test_dossier_tab_shows_copyright_notice_and_modes(ctx, qapp):
    win = MainWindow(ctx)
    text = win.dossier_notice.text()
    assert "личного использования" in text and "облач" in text
    assert [win.dossier_quotes.itemData(i) for i in range(win.dossier_quotes.count())] == ["none", "limited"]
    win.close()


def test_replan_button_rebuilds_selected_chapter(ctx, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from bookmind.analysis.extractor import StructuredCaller
    from bookmind.creation.schemas import AdaptParams
    from bookmind.creation.service import AdaptationService
    from bookmind.creation.writer import BookWriter
    ctx.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    p = ctx.pipeline()
    assert p.run(p.start(ctx.repo.list_books()[0]["id"], MODEL, ("core", "chapter_summary"))).status == "completed"
    broken = {"on": True}

    def handler(stage, prompt, default):
        d = default(prompt)
        if stage == "adapt_chapter" and "план главы 2 из" in prompt and broken["on"]:
            d.update(summary="", goal="", conflict="", outcome="")
        return d
    ctx.llm.extra["adapt_handler"] = handler
    ctx.adaptation = lambda: AdaptationService(ctx.db, StructuredCaller(ctx.llm, ctx.settings), ctx.settings, ctx.prompts)
    ctx.writer = lambda: BookWriter(ctx.adaptation())
    win = MainWindow(ctx)
    t = AdaptThread(ctx, MODEL, None, win.current_series(), AdaptParams(seed=1, chapter_words=300, max_replans=1), "Тест")
    t.start()
    wait(t)
    win.adapt_reload()
    assert win.ad_table.item(1, 2).text() == "требует внимания"
    broken["on"] = False
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    win.ad_table.clearSelection()
    win.replan_selected()
    assert shown == ["Выберите главу в таблице."]
    win.ad_table.selectRow(1)
    win.replan_selected()
    wait(win.thread_adapt)
    assert win.ad_table.item(1, 2).text() == "готова"
    win.close()


def test_review_empty_button_fixes_all_empty_chapters(ctx, qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from bookmind.analysis.extractor import StructuredCaller
    from bookmind.creation.schemas import AdaptParams
    from bookmind.creation.service import AdaptationService
    from bookmind.creation.writer import BookWriter
    ctx.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    p = ctx.pipeline()
    assert p.run(p.start(ctx.repo.list_books()[0]["id"], MODEL, ("core", "chapter_summary"))).status == "completed"
    broken = {"on": True}

    def handler(stage, prompt, default):
        d = default(prompt)
        if stage == "adapt_chapter" and broken["on"] and any(f"план главы {n} из" in prompt for n in (2, 5)):
            d.update(summary="", goal="", conflict="", outcome="")
        return d
    ctx.llm.extra["adapt_handler"] = handler
    ctx.adaptation = lambda: AdaptationService(ctx.db, StructuredCaller(ctx.llm, ctx.settings), ctx.settings, ctx.prompts)
    ctx.writer = lambda: BookWriter(ctx.adaptation())
    win = MainWindow(ctx)
    asked, shown = [], []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes)
    win.review_empty()
    assert shown == ["Сначала создайте проект и дождитесь плана глав."]
    t = AdaptThread(ctx, MODEL, None, win.current_series(), AdaptParams(seed=1, chapter_words=300, max_replans=0), "Тест")
    t.start()
    wait(t)
    win.adapt_reload()
    assert [win.ad_table.item(r, 2).text() for r in (1, 4)] == ["требует внимания"] * 2
    broken["on"] = False
    win.review_empty()
    assert "Главы без описания или названия: 2, 5" in asked[0]
    wait(win.thread_adapt)
    assert [win.ad_table.item(r, 2).text() for r in range(8)] == ["готова"] * 8
    win.review_empty()
    assert shown[-1] == "Пустых глав нет."
    win.close()


def test_selecting_chapter_whose_plan_was_removed_does_not_crash(ctx, qapp, tmp_path):
    """Регрессия: при переделке плана строка главы удаляется из базы, а в таблице она ещё выделена — было StopIteration."""
    from bookmind.analysis.extractor import StructuredCaller
    from bookmind.creation.schemas import AdaptParams
    from bookmind.creation.service import AdaptationService
    from bookmind.creation.writer import BookWriter
    ctx.library.import_book(write_txt(tmp_path / "b.txt", BOOK1), title=BOOK1_TITLE)
    p = ctx.pipeline()
    assert p.run(p.start(ctx.repo.list_books()[0]["id"], MODEL, ("core", "chapter_summary"))).status == "completed"
    ctx.adaptation = lambda: AdaptationService(ctx.db, StructuredCaller(ctx.llm, ctx.settings), ctx.settings, ctx.prompts)
    ctx.writer = lambda: BookWriter(ctx.adaptation())
    win = MainWindow(ctx)
    t = AdaptThread(ctx, MODEL, None, win.current_series(), AdaptParams(seed=1, chapter_words=300), "Тест")
    t.start()
    wait(t)
    win.adapt_reload()
    pid = win.ad_projects.currentData()
    win.ad_table.selectRow(2)
    ctx.db.execute("DELETE FROM adaptation_chapters WHERE project_id = ? AND idx = 2", (pid,))      # как при переделке плана
    win._adapt_show_chapter()
    assert "ещё не составлен" in win.ad_detail.toPlainText()
    win.adapt_refresh()                                                                             # таблица обновилась без падений
    win.ad_table.selectRow(1)
    win.ad_table.setRowCount(0)
    win._adapt_show_chapter()
    assert win._selected_chapter() is None
    win.close()
