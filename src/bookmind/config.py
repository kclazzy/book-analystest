"""Конфигурация приложения. Каталог данных отделён от исходного кода."""
from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

ENV_DATA_DIR = "BOOKMIND_DATA_DIR"
ENV_OLLAMA_HOST = "OLLAMA_HOST"
SETTINGS_FILE = "settings.json"


def default_data_dir() -> Path:
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        return Path(env)
    return Path.cwd() / "data"


def _normalize_host(raw: str) -> str:
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw.rstrip("/")


class Settings(BaseModel):
    """Пользовательские настройки (хранятся в <data_dir>/settings.json)."""

    ollama_host: str = "http://127.0.0.1:11434"
    selected_model: str | None = None
    fallback_models: list[str] = Field(default_factory=list)
    request_timeout_s: float = 300.0
    connect_timeout_s: float = 5.0
    max_retries: int = 2
    num_ctx: int = 8192
    num_predict: int = 2048
    max_concurrent_requests: int = 1
    chunk_chars_min: int = 1500
    chunk_chars_max: int = 3000
    chunk_overlap_chars: int = 200
    allow_cloud_models: bool = False  # по ТЗ облачные модели запрещены

    @classmethod
    def load(cls, data_dir: Path | None = None) -> "Settings":
        data_dir = data_dir or default_data_dir()
        path = data_dir / SETTINGS_FILE
        values: dict = {}
        if path.exists():
            values = json.loads(path.read_text(encoding="utf-8"))
        env_host = os.environ.get(ENV_OLLAMA_HOST)
        if env_host and "ollama_host" not in values:
            values["ollama_host"] = env_host
        settings = cls(**values)
        settings.ollama_host = _normalize_host(settings.ollama_host)
        return settings

    def save(self, data_dir: Path | None = None) -> Path:
        data_dir = data_dir or default_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / SETTINGS_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)  # атомарная замена
        return path
