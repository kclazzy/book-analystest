"""Шаблоны промптов: файлы .md внутри пакета; можно переопределить своими в <data_dir>/prompts/."""
from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path

NAMES = ("core_analysis", "deep_analysis", "chapter_summary", "contradiction_check", "question_review", "answer")
SYSTEM_JSON = ("Ты возвращаешь только валидный JSON без пояснений. Все текстовые значения пиши по-русски. "
               "Никогда не придумывай факты, которых нет во фрагменте.")


class PromptStore:
    def __init__(self, data_dir: Path | None = None):
        self.override_dir = Path(data_dir) / "prompts" if data_dir else None
        self._cache: dict[str, str] = {}

    def template(self, name: str) -> str:
        if name in self._cache:
            return self._cache[name]
        text = None
        if self.override_dir and (self.override_dir / f"{name}.md").exists():
            text = (self.override_dir / f"{name}.md").read_text(encoding="utf-8")
        if text is None:
            text = resources.files("bookmind").joinpath("prompts", f"{name}.md").read_text(encoding="utf-8")
        self._cache[name] = text
        return text

    def render(self, name: str, **values: str) -> str:
        text = self.template(name)
        for key, val in values.items():
            text = text.replace("{{" + key + "}}", val)
        return text

    def version(self, *names: str) -> str:
        """Отпечаток шаблонов: при изменении промпта анализ считается новой версией."""
        names = names or NAMES
        h = hashlib.sha256()
        for n in names:
            h.update(self.template(n).encode("utf-8"))
        return h.hexdigest()[:10]
