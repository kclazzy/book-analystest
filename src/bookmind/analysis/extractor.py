"""Вызов модели со структурированным ответом, разбор JSON и валидация по схеме Pydantic."""
from __future__ import annotations

from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from bookmind.config import Settings
from bookmind.domain.exceptions import ModelOutputError
from bookmind.domain.schemas import inline_schema
from bookmind.llm.json_parser import parse_model_json
from bookmind.llm.prompts import SYSTEM_JSON

T = TypeVar("T", bound=BaseModel)


class LLM(Protocol):
    def chat(self, model: str, messages: list[dict[str, str]], *, schema: dict[str, Any] | None = None,
             options: dict[str, Any] | None = None, think: bool = False) -> str: ...


class StructuredCaller:
    def __init__(self, llm: LLM, settings: Settings):
        self.llm, self.settings = llm, settings

    def options(self, temperature: float | None = None) -> dict[str, Any]:
        return {"num_ctx": self.settings.num_ctx, "num_predict": self.settings.num_predict,
                "temperature": self.settings.temperature if temperature is None else temperature}

    def call(self, model: str, user_prompt: str, schema_model: type[T],
             temperature: float | None = None) -> tuple[T, str, list[str]]:
        """Возвращает (объект, сырой ответ, замечания). После исчерпания попыток — ModelOutputError."""
        user = user_prompt
        last: ModelOutputError | None = None
        schema = inline_schema(schema_model)
        raw = ""
        for attempt in range(self.settings.parse_retries + 1):
            raw = self.llm.chat(model, [{"role": "system", "content": SYSTEM_JSON}, {"role": "user", "content": user}],
                                schema=schema, options=self.options(temperature), think=False)
            try:
                obj, issues = parse_model_json(raw, schema_model)
                if attempt:
                    issues.append(f"ответ получен с попытки {attempt + 1}")
                return obj, raw, issues
            except ModelOutputError as exc:
                last = exc
                user = user_prompt + f"\n\nТвой предыдущий ответ был некорректен ({exc}). Верни только корректный JSON по схеме."
        assert last is not None
        raise ModelOutputError(str(last), raw)
