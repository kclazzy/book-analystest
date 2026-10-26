"""Единый клиент Ollama: доступность, список моделей, pull, chat со структурированным ответом."""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from bookmind.domain.exceptions import (
    CancelledError,
    ModelNotInstalledError,
    OllamaRequestError,
    OllamaUnavailableError,
)

log = logging.getLogger(__name__)
ProgressCb = Callable[[dict[str, Any]], None]


class OllamaClient:
    def __init__(
        self,
        host: str = "http://127.0.0.1:11434",
        request_timeout_s: float = 300.0,
        connect_timeout_s: float = 5.0,
        max_retries: int = 2,
        max_concurrent: int = 1,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.host = host.rstrip("/")
        self.max_retries = max(0, max_retries)  # конечное число повторов
        self._sem = threading.BoundedSemaphore(max(1, max_concurrent))
        self._sleep = sleep
        self._cancel = threading.Event()
        self._http = httpx.Client(
            base_url=self.host,
            timeout=httpx.Timeout(request_timeout_s, connect=connect_timeout_s),
            transport=transport,
        )

    # ---- управление ----
    def cancel(self) -> None:
        """Прервать текущие потоковые операции (pull/chat stream)."""
        self._cancel.set()

    def reset_cancel(self) -> None:
        self._cancel.clear()

    def close(self) -> None:
        self._http.close()

    # ---- низкоуровневое ----
    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """Запрос с ограниченными повторами только для временных ошибок."""
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.request(method, path, **kw)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise OllamaUnavailableError(f"Ollama недоступен по адресу {self.host}: {exc}") from exc
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
                last = exc
            else:
                if resp.status_code >= 500:
                    last = OllamaRequestError(f"HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code)
                elif resp.status_code >= 400:
                    if resp.status_code == 404:
                        raise ModelNotInstalledError(self._err_text(resp))
                    raise OllamaRequestError(self._err_text(resp), resp.status_code)
                else:
                    return resp
            if attempt < self.max_retries:
                delay = min(2.0 * (attempt + 1), 10.0)
                log.warning("Временная ошибка Ollama (%s), повтор %d/%d через %.1f с", last, attempt + 1, self.max_retries, delay)
                self._sleep(delay)
        raise OllamaRequestError(f"Запрос не удался после {self.max_retries + 1} попыток: {last}")

    @staticmethod
    def _err_text(resp: httpx.Response) -> str:
        try:
            return str(resp.json().get("error", resp.text))
        except Exception:
            return resp.text[:300]

    # ---- публичное API ----
    def is_available(self) -> bool:
        try:
            self._http.get("/api/version", timeout=self._http.timeout.connect)
            return True
        except httpx.HTTPError:
            return False

    def version(self) -> str:
        return str(self._request("GET", "/api/version").json().get("version", "unknown"))

    def list_models(self) -> list[dict[str, Any]]:
        """Установленные модели: [{name, digest, size, modified_at, ...}]."""
        return list(self._request("GET", "/api/tags").json().get("models", []))

    def installed_tags(self) -> set[str]:
        return {m["name"] for m in self.list_models()}

    def is_installed(self, tag: str) -> bool:
        tags = self.installed_tags()
        return tag in tags or (":" not in tag and f"{tag}:latest" in tags)

    def show(self, tag: str) -> dict[str, Any]:
        return self._request("POST", "/api/show", json={"model": tag}).json()

    def delete_model(self, tag: str) -> None:
        self._request("DELETE", "/api/delete", json={"model": tag})

    def pull(self, tag: str, on_progress: ProgressCb | None = None) -> str:
        """Скачать/обновить модель. Повторный pull докачивает только изменившиеся слои.
        Возвращает последний статус ('success' при успехе)."""
        status = ""
        try:
            with self._http.stream("POST", "/api/pull", json={"model": tag, "stream": True}) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise OllamaRequestError(self._err_text(resp), resp.status_code)
                for line in resp.iter_lines():
                    if self._cancel.is_set():
                        raise CancelledError("Загрузка модели отменена")
                    if not line:
                        continue
                    event = json.loads(line)
                    if "error" in event:
                        raise OllamaRequestError(str(event["error"]))
                    status = str(event.get("status", status))
                    if on_progress:
                        on_progress(event)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise OllamaUnavailableError(f"Ollama недоступен: {exc}") from exc
        return status

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        think: bool = False,
    ) -> str:
        """Один запрос к модели (не более max_concurrent одновременно).
        schema -> структурированный вывод (JSON Schema в поле format)."""
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False, "think": think}
        if schema is not None:
            payload["format"] = schema
        if options:
            payload["options"] = options
        with self._sem:
            if self._cancel.is_set():
                raise CancelledError("Запрос отменён")
            data = self._request("POST", "/api/chat", json=payload).json()
        return str(data.get("message", {}).get("content", ""))

    def stream_lines(self, path: str, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        with self._http.stream("POST", path, json=payload) as resp:
            for line in resp.iter_lines():
                if line:
                    yield json.loads(line)
