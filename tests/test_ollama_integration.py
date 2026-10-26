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
