import json
import httpx
import pytest

from bookmind.llm.ollama_client import OllamaClient


class FakeOllama:
    """Подменный сервер Ollama на httpx.MockTransport (без сети и без модели)."""

    def __init__(self, models=None):
        self.models = models if models is not None else {"qwen3.5:9b-q4_K_M": "sha-a"}
        self.pull_digest_after = {}
        self.fail_chat_times = 0
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        body = json.loads(request.content) if request.content else {}
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.99"})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": n, "digest": d, "size": 123} for n, d in self.models.items()]})
        if path == "/api/pull":
            tag = body["model"]
            if tag.startswith("bad"):
                return httpx.Response(200, content=json.dumps({"error": "pull model manifest: file does not exist"}) + "\n")
            self.models[tag] = self.pull_digest_after.get(tag, "sha-new")
            lines = [{"status": "pulling", "total": 100, "completed": 50}, {"status": "success"}]
            return httpx.Response(200, content="\n".join(json.dumps(l) for l in lines) + "\n")
        if path == "/api/chat":
            if self.fail_chat_times > 0:
                self.fail_chat_times -= 1
                return httpx.Response(503, text="busy")
            if body["model"] not in self.models:
                return httpx.Response(404, json={"error": f"model '{body['model']}' not found"})
            return httpx.Response(200, json={"message": {"content": '{"ok": true}'}})
        return httpx.Response(404, json={"error": "unknown"})


@pytest.fixture
def fake():
    return FakeOllama()


@pytest.fixture
def client(fake):
    c = OllamaClient("http://test", max_retries=2, transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)
    yield c
    c.close()


# ---------- общие фикстуры хранилища ----------
from bookmind.config import Settings  # noqa: E402
from bookmind.services.library import LibraryService  # noqa: E402
from bookmind.storage.database import Database  # noqa: E402


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def settings():
    # одна глава эталонного романа должна помещаться в один фрагмент
    return Settings(chunk_chars_min=1500, chunk_chars_max=3000, max_retries=1)


@pytest.fixture
def db(data_dir):
    database = Database(data_dir / "database" / "bookmind.sqlite")
    yield database
    database.close()


@pytest.fixture
def library(db, settings, data_dir):
    return LibraryService(db, settings, data_dir)
