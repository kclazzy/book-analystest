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
from bookmind.workers.threads import AnalysisThread, AskThread, ModelThread  # noqa: E402
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
