import httpx
import pytest

from bookmind.domain.exceptions import CancelledError, ModelNotInstalledError, OllamaRequestError, OllamaUnavailableError
from bookmind.llm.ollama_client import OllamaClient


def test_available_and_models(client):
    assert client.is_available()
    assert client.version() == "0.99"
    assert client.installed_tags() == {"qwen3.5:9b-q4_K_M"}
    assert client.is_installed("qwen3.5:9b-q4_K_M")
    assert not client.is_installed("qwen3.5:4b")


def test_unavailable_server_raises_and_is_not_retried_forever():
    calls = []

    def boom(req):
        calls.append(1)
        raise httpx.ConnectError("refused")

    c = OllamaClient("http://x", max_retries=5, transport=httpx.MockTransport(boom), sleep=lambda s: None)
    assert c.is_available() is False
    with pytest.raises(OllamaUnavailableError):
        c.list_models()
    assert len(calls) == 2  # 1 для is_available + 1 для list_models: без повторов при недоступности


def test_chat_returns_content(client):
    assert client.chat("qwen3.5:9b-q4_K_M", [{"role": "user", "content": "hi"}], schema={"type": "object"}) == '{"ok": true}'


def test_chat_retries_transient_5xx_then_succeeds(client, fake):
    fake.fail_chat_times = 2
    assert client.chat("qwen3.5:9b-q4_K_M", [{"role": "user", "content": "x"}]) == '{"ok": true}'


def test_chat_gives_up_after_max_retries(client, fake):
    fake.fail_chat_times = 99
    with pytest.raises(OllamaRequestError):
        client.chat("qwen3.5:9b-q4_K_M", [{"role": "user", "content": "x"}])
    assert sum(1 for _, p in fake.calls if p == "/api/chat") == 3  # 1 + 2 повтора


def test_chat_missing_model(client):
    with pytest.raises(ModelNotInstalledError):
        client.chat("qwen3.5:27b", [{"role": "user", "content": "x"}])


def test_pull_streams_progress_and_registers_model(client, fake):
    events = []
    assert client.pull("qwen3.5:4b", events.append) == "success"
    assert events[0]["completed"] == 50 and "qwen3.5:4b" in fake.models


def test_pull_error_event(client):
    with pytest.raises(OllamaRequestError):
        client.pull("bad:tag")


def test_cancel_stops_pull(client):
    client.cancel()
    with pytest.raises(CancelledError):
        client.pull("qwen3.5:4b")
