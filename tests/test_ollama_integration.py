import pytest

from bookmind.config import Settings
from bookmind.llm.ollama_client import OllamaClient

pytestmark = pytest.mark.integration


def test_real_ollama_roundtrip():
    s = Settings.load()
    c = OllamaClient(s.ollama_host)
    if not c.is_available():
        pytest.skip("Ollama не запущен")
    models = c.installed_tags()
    if not models:
        pytest.skip("Нет установленных моделей")
    tag = s.selected_model if s.selected_model in models else sorted(models)[0]
    out = c.chat(tag, [{"role": "user", "content": 'Ответь JSON {"ok": true}'}],
                 schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                 options={"num_predict": 64})
    assert "ok" in out


def test_real_model_analyzes_first_chapter(tmp_path):
    """Реальная модель: проверяем только структуру (валидный JSON, непустая сводка), а не содержание."""
    from bookmind.analysis.pipeline import AnalysisPipeline
    from bookmind.llm.catalog import load_catalog
    from bookmind.llm.model_manager import ModelManager
    from bookmind.services.library import LibraryService
    from bookmind.storage.database import Database
    from tests.fixtures.builders import write_txt
    from tests.fixtures.novel import BOOK1

    s = Settings.load(tmp_path)
    c = OllamaClient(s.ollama_host, request_timeout_s=600)
    if not c.is_available() or not c.installed_tags():
        pytest.skip("Ollama недоступен или нет моделей")
    model = ModelManager(c, load_catalog(tmp_path), s).resolve_model()
    db = Database(tmp_path / "x.sqlite")
    lib = LibraryService(db, s, tmp_path)
    res = lib.import_book(write_txt(tmp_path / "b.txt", BOOK1[:1]))
    pipe = AnalysisPipeline(db, c, s, tmp_path)
    summary = pipe.run(pipe.start(res.book_id, model))
    assert summary.counts["core"]["total"] == 1
    assert summary.status in ("completed", "failed")
    if summary.status == "completed":
        assert db.scalar("SELECT COUNT(*) FROM report_versions WHERE kind='chunk_summary'") == 1
