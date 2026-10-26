"""Проверка окружения без Qt. Каждая проверка возвращает уровень (ok/warn/error), пояснение и подсказку, что делать."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from bookmind.domain.exceptions import BookMindError
from bookmind.llm.model_manager import ModelManager
from bookmind.llm.ollama_client import OllamaClient

OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"
MIN_FREE_DATA_MB = 500


@dataclass
class EnvCheck:
    key: str
    title: str
    level: str          # ok | warn | error
    detail: str
    fix: str = ""       # ollama_start | ollama_download | models | ""

    @property
    def ok(self) -> bool:
        return self.level == "ok"


def find_ollama_exe(env: dict | None = None) -> Path | None:
    env = env if env is not None else os.environ
    found = shutil.which("ollama", path=env.get("PATH"))
    if found:
        return Path(found)
    candidates = []
    for var, sub in (("LOCALAPPDATA", r"Programs\Ollama\ollama.exe"), ("PROGRAMFILES", r"Ollama\ollama.exe")):
        if env.get(var):
            candidates.append(Path(env[var]).joinpath(*sub.split("\\")))
    candidates += [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama"), Path("/opt/homebrew/bin/ollama")]
    return next((c for c in candidates if c.is_file()), None)


def start_ollama(client: OllamaClient, *, exe: Path | None = None, wait_s: float = 20.0,
                 popen=subprocess.Popen, sleep: Callable[[float], None] = time.sleep) -> tuple[bool, str]:
    """Запускает `ollama serve` в фоне и ждёт, пока сервер ответит."""
    if client.is_available():
        return True, "Ollama уже запущен"
    exe = exe or find_ollama_exe()
    if exe is None:
        return False, "Ollama не найден на этом компьютере. Установите его: " + OLLAMA_DOWNLOAD_URL
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs.update(hidden_process_kwargs())
        kwargs["creationflags"] = CREATE_NO_WINDOW | 0x00000008     # + DETACHED_PROCESS: Ollama переживает закрытие окна
    else:
        kwargs["start_new_session"] = True
    try:
        popen([str(exe), "serve"], **kwargs)
    except OSError as exc:
        return False, f"Не удалось запустить Ollama: {exc}"
    waited = 0.0
    while waited < wait_s:
        if client.is_available():
            return True, "Ollama запущен"
        sleep(0.5)
        waited += 0.5
    return False, "Ollama запущен, но не отвечает. Подождите минуту и нажмите «Перепроверить»."


CREATE_NO_WINDOW = 0x08000000
_gpu_cache: dict[str, str | None] = {}


def hidden_process_kwargs() -> dict:
    """Параметры запуска дочерних программ без мигающего окна консоли (Windows)."""
    if sys.platform != "win32":
        return {}
    kw: dict = {"creationflags": CREATE_NO_WINDOW}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
    except AttributeError:
        pass
    return kw


def _gpu_info(run=subprocess.run) -> str | None:
    """Название и объём видеокарты NVIDIA. Результат запоминается: видеокарта за время работы не меняется."""
    if "gpu" in _gpu_cache:
        return _gpu_cache["gpu"]
    exe = shutil.which("nvidia-smi")
    result = None
    if exe:
        try:
            out = run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True,
                      timeout=4, stdin=subprocess.DEVNULL, **hidden_process_kwargs())
            line = (out.stdout or "").strip().splitlines()
            result = line[0].strip() if out.returncode == 0 and line else None
        except (OSError, subprocess.SubprocessError):
            result = None
    _gpu_cache["gpu"] = result
    return result


def check_environment(client: OllamaClient, models: ModelManager, data_dir: Path, *, gpu_probe=_gpu_info) -> list[EnvCheck]:
    checks: list[EnvCheck] = []
    running = client.is_available()
    exe = find_ollama_exe()
    if running:
        try:
            version = client.version()
        except BookMindError:
            version = ""
        checks.append(EnvCheck("ollama_installed", "Ollama установлен", "ok", f"найден{(' ' + str(exe)) if exe else ' (сервер отвечает)'}"))
        checks.append(EnvCheck("ollama_running", "Ollama запущен", "ok", f"сервер отвечает по адресу {client.host}" + (f", версия {version}" if version else "")))
    else:
        if exe:
            checks.append(EnvCheck("ollama_installed", "Ollama установлен", "ok", f"найден: {exe}"))
            checks.append(EnvCheck("ollama_running", "Ollama запущен", "error",
                                   "сервер не отвечает. Нажмите «Запустить Ollama» или запустите его из меню «Пуск».", "ollama_start"))
        else:
            checks.append(EnvCheck("ollama_installed", "Ollama установлен", "error",
                                   "Ollama не найден. Скачайте и установите его, затем нажмите «Перепроверить».", "ollama_download"))
            checks.append(EnvCheck("ollama_running", "Ollama запущен", "error", "нечего запускать: Ollama не установлен", "ollama_download"))
    if running:
        try:
            sts = models.statuses()
            installed = [s for s in sts if s.installed]
            if not installed:
                checks.append(EnvCheck("model", "Модель Qwen установлена", "error",
                                       "ни одной модели нет. Перейдите на вкладку «Модели» и установите рекомендуемую.", "models"))
            else:
                chosen = next((s for s in installed if s.selected), None)
                names = ", ".join(s.tag for s in installed)
                if chosen:
                    checks.append(EnvCheck("model", "Модель Qwen установлена", "ok", f"выбрана {chosen.tag}; установлено: {names}"))
                else:
                    try:
                        fallback = models.resolve_model()
                        checks.append(EnvCheck("model", "Модель Qwen установлена", "ok",
                                               f"будет использована {fallback}; установлено: {names}"))
                    except BookMindError:
                        checks.append(EnvCheck("model", "Модель Qwen установлена", "warn",
                                               f"установлено: {names}, но подходящая не выбрана — выберите на вкладке «Модели»", "models"))
        except BookMindError as exc:
            checks.append(EnvCheck("model", "Модель Qwen установлена", "error", str(exc), "models"))
    else:
        checks.append(EnvCheck("model", "Модель Qwen установлена", "warn", "проверить нельзя: Ollama не запущен"))
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=data_dir):
            pass
        free_mb = shutil.disk_usage(data_dir).free // (1 << 20)
        if free_mb < MIN_FREE_DATA_MB:
            checks.append(EnvCheck("data", "Папка данных доступна", "warn", f"{data_dir}: мало места ({free_mb} МБ)"))
        else:
            checks.append(EnvCheck("data", "Папка данных доступна", "ok", f"{data_dir} (свободно {free_mb // 1024} ГБ)"))
    except OSError as exc:
        checks.append(EnvCheck("data", "Папка данных доступна", "error", f"{data_dir}: нет доступа на запись ({exc})"))
    gpu = gpu_probe()
    checks.append(EnvCheck("gpu", "Видеокарта NVIDIA", "ok" if gpu else "warn",
                           gpu or "не обнаружена: модели будут работать на процессоре, анализ будет заметно медленнее"))
    return checks


def blocking_problems(checks: list[EnvCheck]) -> list[EnvCheck]:
    return [c for c in checks if c.level == "error"]
