"""Разбор JSON из ответа модели: снятие markdown-ограждений и <think>, ремонт частых дефектов, валидация схемой."""
from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from bookmind.domain.exceptions import ModelOutputError

T = TypeVar("T", bound=BaseModel)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def _extract_object(text: str) -> str | None:
    """Первый сбалансированный JSON-объект (с учётом строк). Если не закрыт — возвращает остаток для ремонта."""
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def _close_truncated(s: str) -> str:
    """Закрывает обрезанный ответ (num_predict исчерпан): строки, скобки; отбрасывает хвост после последней запятой."""
    stack: list[str] = []
    in_str = esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        s += '"'
    s = re.sub(r",\s*$", "", s)
    s = re.sub(r'"[^"]*"\s*:\s*$', "", s).rstrip().rstrip(",")
    return s + "".join(reversed(stack))


def load_json_object(raw: str) -> tuple[dict, list[str]]:
    issues: list[str] = []
    text = _THINK.sub("", raw).strip()
    fence = _FENCE.search(text)
    if fence:
        text = fence.group(1).strip()
        issues.append("убрано markdown-ограждение")
    obj = _extract_object(text)
    if obj is None:
        raise ModelOutputError("В ответе модели нет JSON-объекта", raw)
    for attempt, candidate in enumerate((obj, _TRAILING_COMMA.sub(r"\1", obj), _close_truncated(_TRAILING_COMMA.sub(r"\1", obj)))):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if attempt == 1:
            issues.append("исправлены лишние запятые")
        elif attempt == 2:
            issues.append("JSON был обрезан и достроен: часть данных могла быть потеряна")
        if not isinstance(data, dict):
            raise ModelOutputError("Ожидался JSON-объект", raw)
        return data, issues
    raise ModelOutputError("Некорректный JSON в ответе модели", raw)


def parse_model_json(raw: str, model: type[T]) -> tuple[T, list[str]]:
    data, issues = load_json_object(raw)
    try:
        obj = model.model_validate(data)
        return obj, issues + list(getattr(obj, "dropped", []) or [])
    except ValidationError as exc:
        brief = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
        raise ModelOutputError(f"Ответ не соответствует схеме: {brief}", raw) from exc
