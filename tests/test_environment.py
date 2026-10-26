import subprocess
from pathlib import Path

import httpx
import pytest

from bookmind.llm.catalog import load_catalog
from bookmind.llm.model_manager import ModelManager
from bookmind.llm.ollama_client import OllamaClient
from bookmind.config import Settings
from bookmind.system import environment as env
from tests.conftest import FakeOllama


def make(fake: FakeOllama | None = None, down: bool = False):
    fake = fake or FakeOllama()
    def handler(request):
        if down:
            raise httpx.ConnectError("refused")
        return fake.handler(request)
    client = OllamaClient("http://test", max_retries=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    settings = Settings(selected_model="qwen3.5:9b-q4_K_M")
    return client, ModelManager(client, load_catalog(None), settings), fake, settings


def by_key(checks):
    return {c.key: c for c in checks}


def test_everything_ok(tmp_path, monkeypatch):
    client, mgr, *_ = make()
    monkeypatch.setattr(env, "find_ollama_exe", lambda *a, **k: Path("C:/ollama.exe"))
    checks = by_key(env.check_environment(client, mgr, tmp_path / "d", gpu_probe=lambda: "NVIDIA RTX 5070, 12288 MiB"))
    assert all(c.ok for c in checks.values()) and "версия 0.99" in checks["ollama_running"].detail
    assert "RTX 5070" in checks["gpu"].detail and "qwen3.5:9b-q4_K_M" in checks["model"].detail
    assert env.blocking_problems(list(checks.values())) == []


def test_installed_but_not_running_offers_start(tmp_path, monkeypatch):
    client, mgr, *_ = make(down=True)
    monkeypatch.setattr(env, "find_ollama_exe", lambda *a, **k: Path("C:/ollama.exe"))
    checks = by_key(env.check_environment(client, mgr, tmp_path, gpu_probe=lambda: None))
    assert checks["ollama_installed"].ok and checks["ollama_running"].level == "error" and checks["ollama_running"].fix == "ollama_start"
    assert checks["model"].level == "warn" and checks["gpu"].level == "warn"


def test_not_installed_offers_download(tmp_path, monkeypatch):
    client, mgr, *_ = make(down=True)
    monkeypatch.setattr(env, "find_ollama_exe", lambda *a, **k: None)
    checks = by_key(env.check_environment(client, mgr, tmp_path, gpu_probe=lambda: None))
    assert checks["ollama_installed"].fix == "ollama_download" and checks["ollama_running"].fix == "ollama_download"
    assert len(env.blocking_problems(list(checks.values()))) == 2


def test_running_without_models_points_to_models_tab(tmp_path, monkeypatch):
    client, mgr, fake, _ = make(FakeOllama(models={}))
    monkeypatch.setattr(env, "find_ollama_exe", lambda *a, **k: None)
    checks = by_key(env.check_environment(client, mgr, tmp_path, gpu_probe=lambda: None))
    assert checks["ollama_installed"].ok                    # сервер отвечает — значит, он есть (возможно, на другом хосте)
    assert checks["model"].level == "error" and checks["model"].fix == "models"


def test_model_installed_but_not_selected_is_warning(tmp_path, monkeypatch):
    client, mgr, fake, settings = make(FakeOllama(models={"qwen3:8b": "x"}))
    settings.selected_model = None
    monkeypatch.setattr(env, "find_ollama_exe", lambda *a, **k: None)
    c = by_key(env.check_environment(client, mgr, tmp_path, gpu_probe=lambda: None))["model"]
    assert c.level == "ok" and "будет использована qwen3:8b" in c.detail


def test_unwritable_data_dir_is_error(tmp_path, monkeypatch):
    client, mgr, *_ = make()
    f = tmp_path / "file.txt"
    f.write_text("x")
    monkeypatch.setattr(env, "find_ollama_exe", lambda *a, **k: None)
    assert by_key(env.check_environment(client, mgr, f / "sub", gpu_probe=lambda: None))["data"].level == "error"


def test_find_exe_in_standard_windows_location(tmp_path):
    exe = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert env.find_ollama_exe({"PATH": "", "LOCALAPPDATA": str(tmp_path)}) == exe
    assert env.find_ollama_exe({"PATH": "", "LOCALAPPDATA": str(tmp_path / "none")}) in (None, Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama"), Path("/opt/homebrew/bin/ollama"))


def test_start_ollama_launches_and_waits(tmp_path):
    state = {"up": False, "cmd": None}
    def handler(request):
        if not state["up"]:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"version": "1"})
    client = OllamaClient("http://t", max_retries=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    exe = tmp_path / "ollama"
    exe.write_text("")
    def popen(cmd, **kw):
        state["cmd"] = cmd
        return object()
    ticks = []
    def sleep(s):
        ticks.append(s)
        if len(ticks) == 3:
            state["up"] = True
    ok, msg = env.start_ollama(client, exe=exe, popen=popen, sleep=sleep)
    assert ok and state["cmd"] == [str(exe), "serve"] and len(ticks) == 3


def test_start_ollama_failure_modes(tmp_path, monkeypatch):
    def down(request):
        raise httpx.ConnectError("down")
    client = OllamaClient("http://t", max_retries=0, transport=httpx.MockTransport(down), sleep=lambda s: None)
    monkeypatch.setattr(env, "find_ollama_exe", lambda *a, **k: None)
    ok, msg = env.start_ollama(client)
    assert not ok and "ollama.com/download" in msg
    exe = tmp_path / "ollama"
    exe.write_text("")
    ok, msg = env.start_ollama(client, exe=exe, popen=lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
    assert not ok and "denied" in msg
    ok, msg = env.start_ollama(client, exe=exe, popen=lambda *a, **k: object(), wait_s=1.0, sleep=lambda s: None)
    assert not ok and "не отвечает" in msg


def test_start_ollama_noop_when_already_up():
    client, *_ = make()
    called = []
    ok, msg = env.start_ollama(client, popen=lambda *a, **k: called.append(1))
    assert ok and not called


def test_data_dir_for_installed_app(monkeypatch, tmp_path):
    from bookmind import config
    monkeypatch.delenv("BOOKMIND_DATA_DIR", raising=False)
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert config.default_data_dir() == tmp_path / "BookMind"
    monkeypatch.setenv("BOOKMIND_DATA_DIR", str(tmp_path / "custom"))
    assert config.default_data_dir() == tmp_path / "custom"


def test_gpu_probe_is_hidden_on_windows_and_cached(monkeypatch):
    monkeypatch.setattr(env, "_gpu_cache", {})
    monkeypatch.setattr(env.shutil, "which", lambda n: "C:/nvidia-smi.exe")
    monkeypatch.setattr(env.sys, "platform", "win32")
    calls = []
    class P:
        returncode, stdout = 0, "NVIDIA RTX 5070, 12288 MiB\n"
    def run(cmd, **kw):
        calls.append(kw)
        return P()
    assert env._gpu_info(run) == "NVIDIA RTX 5070, 12288 MiB"
    assert env._gpu_info(run) == "NVIDIA RTX 5070, 12288 MiB" and len(calls) == 1      # второй раз процесс не запускается
    assert calls[0]["creationflags"] == env.CREATE_NO_WINDOW


def test_gpu_probe_missing_is_cached_too(monkeypatch):
    monkeypatch.setattr(env, "_gpu_cache", {})
    monkeypatch.setattr(env.shutil, "which", lambda n: None)
    assert env._gpu_info() is None and env._gpu_cache == {"gpu": None}


def test_no_other_child_processes_in_app_code():
    import re
    from pathlib import Path
    root = Path(env.__file__).resolve().parents[1]
    offenders = [str(p.relative_to(root)) for p in root.rglob("*.py")
                 if p.name != "environment.py" and re.search(r"\b(subprocess|os\.system|Popen)\b", p.read_text(encoding="utf-8"))]
    assert offenders == []
