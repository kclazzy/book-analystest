"""Управление несколькими моделями Qwen: статус, установка, обновление, выбор с запасными вариантами."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import httpx

from bookmind.config import Settings
from bookmind.domain.exceptions import BookMindError, ModelNotInstalledError
from bookmind.llm.catalog import Catalog, ModelProfile
from bookmind.llm.ollama_client import OllamaClient, ProgressCb

REGISTRY = "https://registry.ollama.ai"


@dataclass
class ModelStatus:
    tag: str
    installed: bool
    in_catalog: bool
    selected: bool = False
    digest: str | None = None
    size_bytes: int | None = None
    profile: ModelProfile | None = None


@dataclass
class UpdateResult:
    tag: str
    digest_before: str | None
    digest_after: str | None

    @property
    def changed(self) -> bool:
        return self.digest_before != self.digest_after


def _registry_path(tag: str) -> tuple[str, str]:
    """'qwen3.5:9b' -> ('library/qwen3.5', '9b'); 'user/m:t' -> ('user/m', 't')."""
    name, _, version = tag.partition(":")
    version = version or "latest"
    repo = name if "/" in name else f"library/{name}"
    return repo, version


class ModelManager:
    def __init__(self, client: OllamaClient, catalog: Catalog, settings: Settings):
        self.client, self.catalog, self.settings = client, catalog, settings

    # ---- статус ----
    def statuses(self) -> list[ModelStatus]:
        installed = {m["name"]: m for m in self.client.list_models()}
        out: list[ModelStatus] = []
        seen: set[str] = set()
        for p in self.catalog.models:
            if p.is_cloud and not self.settings.allow_cloud_models:
                continue
            inst = installed.get(p.tag)
            seen.add(p.tag)
            out.append(ModelStatus(p.tag, inst is not None, True, p.tag == self.settings.selected_model,
                                   inst.get("digest") if inst else None, inst.get("size") if inst else None, p))
        # Установленные qwen-модели вне каталога (пользователь поставил сам)
        for tag, m in installed.items():
            if tag not in seen and tag.lower().startswith("qwen"):
                out.append(ModelStatus(tag, True, False, tag == self.settings.selected_model,
                                       m.get("digest"), m.get("size")))
        return out

    # ---- выбор ----
    def select(self, tag: str) -> None:
        self._guard_local(tag)
        if not self.client.is_installed(tag):
            raise ModelNotInstalledError(f"Модель {tag} не установлена. Выполните: bookmind-models install {tag}")
        self.settings.selected_model = tag

    def resolve_model(self) -> str:
        """Выбранная модель → запасные → первая установленная из каталога. Ничего не угадывает."""
        installed = self.client.installed_tags()
        candidates = [self.settings.selected_model, *self.settings.fallback_models]
        d = self.catalog.default_model()
        if d:
            candidates.append(d.tag)
        candidates += [p.tag for p in self.catalog.models if not p.is_cloud]
        for tag in candidates:
            if tag and tag in installed and (self.settings.allow_cloud_models or "cloud" not in tag):
                return tag
        raise ModelNotInstalledError("Нет установленных моделей Qwen. Выполните: bookmind-models install <тег>")

    # ---- установка и обновление ----
    def install(self, tag: str, on_progress: ProgressCb | None = None) -> str:
        self._guard_local(tag)
        status = self.client.pull(tag, on_progress)
        if status != "success":
            raise BookMindError(f"Загрузка {tag} завершилась статусом '{status}'")
        return status

    def update(self, tag: str, on_progress: ProgressCb | None = None) -> UpdateResult:
        """Повторный pull: Ollama докачивает только изменившиеся слои."""
        self._guard_local(tag)
        before = self._digest(tag)
        self.install(tag, on_progress)
        return UpdateResult(tag, before, self._digest(tag))

    def update_all(self, on_progress: Callable[[str, dict[str, Any]], None] | None = None) -> list[UpdateResult]:
        results = []
        for st in self.statuses():
            if st.installed:
                cb = (lambda e, t=st.tag: on_progress(t, e)) if on_progress else None
                results.append(self.update(st.tag, cb))
        return results

    def check_updates(self, http: httpx.Client | None = None) -> dict[str, bool | None]:
        """Сравнить дайджест манифеста в реестре Ollama с локальным.
        True — есть обновление, False — актуально, None — не удалось определить.
        Best-effort: форматы дайджестов могут не совпадать; None/True надёжно проверяются командой update.
        Отправляются только имена моделей, содержимое книг не передаётся."""
        own = http is None
        http = http or httpx.Client(timeout=10.0)
        result: dict[str, bool | None] = {}
        try:
            for st in self.statuses():
                if not st.installed or st.digest is None:
                    continue
                repo, ver = _registry_path(st.tag)
                try:
                    r = http.head(f"{REGISTRY}/v2/{repo}/manifests/{ver}",
                                  headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
                    remote = r.headers.get("Docker-Content-Digest", "").removeprefix("sha256:")
                    if r.status_code != 200 or not remote:
                        result[st.tag] = None
                    else:
                        local = st.digest.removeprefix("sha256:")
                        result[st.tag] = not (remote == local or remote.startswith(local) or local.startswith(remote))
                except httpx.HTTPError:
                    result[st.tag] = None
        finally:
            if own:
                http.close()
        return result

    # ---- внутреннее ----
    def _digest(self, tag: str) -> str | None:
        for m in self.client.list_models():
            if m["name"] == tag:
                return m.get("digest")
        return None

    def _guard_local(self, tag: str) -> None:
        if not self.settings.allow_cloud_models and ("cloud" in tag.split(":")[-1] or tag.endswith("-cloud")):
            raise BookMindError(f"Облачная модель {tag} запрещена настройками (работа только локально)")
