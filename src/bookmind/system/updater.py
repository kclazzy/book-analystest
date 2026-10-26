"""Обновление приложения изнутри: проверка последнего релиза на GitHub, скачивание установщика с проверкой SHA-256,
резервная копия данных и тихий запуск установщика. Единственное обращение в сеть, не связанное с Ollama; отключается в настройках."""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from bookmind import __version__
from bookmind.domain.exceptions import BookMindError, CancelledError

API = "https://api.github.com/repos/{repo}/releases/latest"
INSTALLER_RE = re.compile(r"^BookMind-Setup-.+\.exe$", re.IGNORECASE)
ALLOWED_HOSTS = ("github.com", "githubusercontent.com")
SHA_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


class UpdateError(BookMindError):
    pass


@dataclass
class UpdateInfo:
    version: str
    tag: str
    url: str
    name: str
    size: int
    sha256: str | None
    notes: str
    page_url: str


@dataclass
class UpdateCheck:
    current: str
    latest: UpdateInfo | None = None
    available: bool = False
    error: str = ""


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v.split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _trusted(url: str) -> bool:
    p = urlparse(url)
    return p.scheme == "https" and any(p.hostname == h or (p.hostname or "").endswith("." + h) for h in ALLOWED_HOSTS)


class Updater:
    def __init__(self, repo: str, current: str = __version__, transport: httpx.BaseTransport | None = None):
        self.repo, self.current = repo, current
        self._transport = transport

    def _client(self, timeout: float = 10.0) -> httpx.Client:
        return httpx.Client(transport=self._transport, timeout=httpx.Timeout(timeout, connect=5.0), follow_redirects=True,
                            headers={"Accept": "application/vnd.github+json", "User-Agent": f"BookMind/{self.current}"})

    # ---- проверка ----
    def check(self) -> UpdateCheck:
        res = UpdateCheck(self.current)
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", self.repo or ""):
            res.error = "Некорректный адрес репозитория обновлений в настройках"
            return res
        try:
            with self._client() as c:
                r = c.get(API.format(repo=self.repo))
                if r.status_code == 404:
                    res.error = "Релизы не найдены (репозиторий приватный или релиз ещё не опубликован)"
                    return res
                if r.status_code in (403, 429):
                    res.error = "GitHub временно ограничил число запросов. Повторите проверку позже."
                    return res
                r.raise_for_status()
                data = r.json()
                tag = str(data.get("tag_name", ""))
                asset = next((a for a in data.get("assets", []) if INSTALLER_RE.match(a.get("name", ""))), None)
                if not tag or asset is None:
                    res.error = "В последнем релизе нет установщика"
                    return res
                version = tag.lstrip("vV")
                url = asset.get("browser_download_url", "")
                if not _trusted(url):
                    res.error = "Адрес загрузки не прошёл проверку безопасности"
                    return res
                sha = None
                digest = str(asset.get("digest") or "")
                if digest.startswith("sha256:") and SHA_RE.fullmatch(digest[7:]):
                    sha = digest[7:].lower()
                else:
                    side = next((a for a in data.get("assets", []) if a.get("name") == asset["name"] + ".sha256"), None)
                    if side and _trusted(side.get("browser_download_url", "")):
                        m = SHA_RE.search(c.get(side["browser_download_url"]).text)
                        sha = m.group(0).lower() if m else None
                res.latest = UpdateInfo(version, tag, url, asset["name"], int(asset.get("size") or 0), sha,
                                        str(data.get("body") or "")[:2000], str(data.get("html_url") or ""))
                res.available = is_newer(version, self.current)
        except httpx.HTTPError as exc:
            res.error = f"Не удалось проверить обновления (нет сети?): {type(exc).__name__}"
        except ValueError:
            res.error = "GitHub вернул непонятный ответ"
        return res

    # ---- скачивание ----
    def download(self, info: UpdateInfo, dest_dir: Path, on_progress: Callable[[int, int], None] | None = None,
                 cancel: threading.Event | None = None) -> Path:
        if not _trusted(info.url):
            raise UpdateError("Адрес загрузки не прошёл проверку безопасности")
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / re.sub(r"[^\w.\-]", "_", info.name)
        part = target.with_suffix(target.suffix + ".part")
        h = hashlib.sha256()
        done = 0
        try:
            with self._client(timeout=120.0) as c, c.stream("GET", info.url) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or info.size or 0)
                with part.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        if cancel is not None and cancel.is_set():
                            raise CancelledError("Загрузка отменена")
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if on_progress:
                            on_progress(done, total)
            if info.size and done != info.size:
                raise UpdateError(f"Файл скачан не полностью ({done} из {info.size} байт)")
            if info.sha256 and h.hexdigest() != info.sha256:
                raise UpdateError("Контрольная сумма не совпала: файл повреждён или подменён. Установка отменена.")
            part.replace(target)
            return target
        except httpx.HTTPError as exc:
            raise UpdateError(f"Ошибка загрузки: {type(exc).__name__}") from exc
        finally:
            part.unlink(missing_ok=True)


def launch_installer(path: Path, *, popen=subprocess.Popen, frozen: bool | None = None, platform: str | None = None) -> None:
    """Запускает установщик в тихом режиме (с окном прогресса). После вызова приложение должно завершиться."""
    if not (is_frozen() if frozen is None else frozen):
        raise UpdateError("Автоустановка работает только в установленной версии. При запуске из исходников выполните `git pull`.")
    if (platform or sys.platform) != "win32":
        raise UpdateError("Автоустановка доступна только в Windows")
    kwargs = {"close_fds": True, "creationflags": DETACHED_PROCESS | CREATE_NO_WINDOW}
    try:
        popen([str(path), "/SILENT", "/CLOSEAPPLICATIONS", "/NORESTART", "/SUPPRESSMSGBOXES"], **kwargs)
    except OSError as exc:
        raise UpdateError(f"Не удалось запустить установщик: {exc}") from exc
