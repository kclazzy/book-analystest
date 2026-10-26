"""Каталог моделей Qwen: загрузка, слияние пользовательского каталога, подбор по VRAM."""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from bookmind.domain.exceptions import CatalogError

CATALOG_FILENAME = "models_catalog.json"


class ModelProfile(BaseModel):
    tag: str
    family: str
    params_b: float
    quant: str | None = None
    approx_vram_gb: float
    roles: list[str] = Field(default_factory=list)
    default: bool = False
    description: str = ""

    @property
    def is_cloud(self) -> bool:
        return self.tag.endswith("cloud") or ":cloud" in self.tag or "-cloud" in self.tag


class Catalog(BaseModel):
    schema_version: int = 1
    note: str = ""
    models: list[ModelProfile]

    def get(self, tag: str) -> ModelProfile | None:
        return next((m for m in self.models if m.tag == tag), None)

    def by_role(self, role: str) -> list[ModelProfile]:
        return [m for m in self.models if role in m.roles]

    def default_model(self) -> ModelProfile | None:
        return next((m for m in self.models if m.default), self.models[0] if self.models else None)

    def fitting(self, vram_gb: float) -> list[ModelProfile]:
        """Модели, оценочно помещающиеся в VRAM; самые крупные первыми."""
        ok = [m for m in self.models if m.approx_vram_gb <= vram_gb]
        return sorted(ok, key=lambda m: m.params_b, reverse=True)


def parse_catalog(text: str) -> Catalog:
    try:
        return Catalog(**json.loads(text))
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise CatalogError(f"Некорректный каталог моделей: {exc}") from exc


def load_builtin_catalog() -> Catalog:
    text = resources.files("bookmind").joinpath("data", CATALOG_FILENAME).read_text(encoding="utf-8")
    return parse_catalog(text)


def load_catalog(data_dir: Path | None = None) -> Catalog:
    """Встроенный каталог + пользовательский (из каталога данных) поверх него по tag."""
    catalog = load_builtin_catalog()
    if data_dir is not None:
        user_path = Path(data_dir) / CATALOG_FILENAME
        if user_path.exists():
            user = parse_catalog(user_path.read_text(encoding="utf-8"))
            merged = {m.tag: m for m in catalog.models}
            merged.update({m.tag: m for m in user.models})
            catalog = Catalog(schema_version=user.schema_version, note=user.note or catalog.note,
                              models=list(merged.values()))
    return catalog


def install_catalog(source_text: str, data_dir: Path) -> Catalog:
    """Проверить и сохранить пользовательский каталог (обновление списка моделей без правки кода)."""
    catalog = parse_catalog(source_text)
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / CATALOG_FILENAME
    tmp = target.with_suffix(".tmp")
    tmp.write_text(catalog.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(target)
    return catalog
