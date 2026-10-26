import hashlib
import threading

import httpx
import pytest

from bookmind.domain.exceptions import CancelledError
from bookmind.system import updater as up
from bookmind.system.updater import UpdateError, Updater, is_newer, launch_installer, parse_version

PAYLOAD = b"MZ" + b"installer-bytes" * 5000
SHA = hashlib.sha256(PAYLOAD).hexdigest()
BASE = "https://github.com/kclazzy/book-analystest/releases/download/v0.3.0"


def release(tag="v0.3.0", assets=None, digest=True):
    assets = assets if assets is not None else [
        {"name": "BookMind-Setup-0.3.0.exe", "browser_download_url": f"{BASE}/BookMind-Setup-0.3.0.exe", "size": len(PAYLOAD),
         **({"digest": f"sha256:{SHA}"} if digest else {})},
        {"name": "BookMind-portable-0.3.0.zip", "browser_download_url": f"{BASE}/BookMind-portable-0.3.0.zip", "size": 5}]
    return {"tag_name": tag, "body": "Что нового: исправления", "html_url": "https://github.com/kclazzy/book-analystest/releases/tag/" + tag, "assets": assets}


def make(handler, current="0.2.1"):
    return Updater("kclazzy/book-analystest", current, transport=httpx.MockTransport(handler))


def api(payload=None, status=200):
    def h(request):
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(status, json=payload if payload is not None else release())
        if request.url.path.endswith(".exe"):
            return httpx.Response(200, content=PAYLOAD, headers={"content-length": str(len(PAYLOAD))})
        if request.url.path.endswith(".sha256"):
            return httpx.Response(200, text=f"{SHA}  BookMind-Setup-0.3.0.exe\n")
        return httpx.Response(404)
    return h


def test_version_comparison():
    assert parse_version("v0.10.2") == (0, 10, 2) and parse_version("1.0.0-beta+5") == (1, 0, 0)
    assert is_newer("0.3.0", "0.2.1") and is_newer("0.10.0", "0.9.9") and is_newer("1.0", "0.9.9")
    assert not is_newer("0.2.1", "0.2.1") and not is_newer("0.2", "0.2.0") and not is_newer("0.1.9", "0.2.1")


def test_newer_release_is_detected_with_digest():
    r = make(api()).check()
    assert r.available and not r.error and r.latest.version == "0.3.0" and r.latest.sha256 == SHA
    assert r.latest.name == "BookMind-Setup-0.3.0.exe" and "исправления" in r.latest.notes


def test_same_or_older_release_is_not_an_update():
    assert not make(api(), current="0.3.0").check().available
    assert not make(api(), current="0.4.0").check().available


def test_checksum_from_sidecar_file_when_no_digest():
    p = release(digest=False)
    p["assets"].append({"name": "BookMind-Setup-0.3.0.exe.sha256", "browser_download_url": f"{BASE}/BookMind-Setup-0.3.0.exe.sha256", "size": 90})
    assert make(api(p)).check().latest.sha256 == SHA


def test_no_checksum_available_is_reported_as_none():
    assert make(api(release(digest=False))).check().latest.sha256 is None


@pytest.mark.parametrize("status,needle", [(404, "не найдены"), (403, "ограничил"), (429, "ограничил")])
def test_api_errors_have_friendly_messages(status, needle):
    r = make(api({}, status)).check()
    assert not r.available and needle in r.error


def test_network_failure_is_not_an_exception():
    def down(request):
        raise httpx.ConnectError("offline")
    r = make(down).check()
    assert not r.available and "нет сети" in r.error


def test_release_without_installer_or_with_foreign_host():
    assert "нет установщика" in make(api(release(assets=[]))).check().error
    evil = release(assets=[{"name": "BookMind-Setup-9.9.9.exe", "browser_download_url": "https://evil.example.com/BookMind-Setup-9.9.9.exe", "size": 1}])
    assert "безопасности" in make(api(evil)).check().error
    http_only = release(assets=[{"name": "BookMind-Setup-9.9.9.exe", "browser_download_url": "http://github.com/x/BookMind-Setup-9.9.9.exe", "size": 1}])
    assert "безопасности" in make(api(http_only)).check().error


def test_bad_repo_setting_is_rejected_without_request():
    called = []
    u = Updater("../../etc/passwd", "0.1", transport=httpx.MockTransport(lambda r: called.append(r) or httpx.Response(200)))
    assert "репозитория" in u.check().error and not called


def test_download_verifies_checksum_and_reports_progress(tmp_path):
    u = make(api())
    info = u.check().latest
    seen = []
    path = u.download(info, tmp_path / "up", on_progress=lambda d, t: seen.append((d, t)))
    assert path.read_bytes() == PAYLOAD and path.name == "BookMind-Setup-0.3.0.exe"
    assert seen[-1] == (len(PAYLOAD), len(PAYLOAD)) and not list((tmp_path / "up").glob("*.part"))


def test_download_rejects_tampered_file_and_cleans_up(tmp_path):
    u = make(api())
    info = u.check().latest
    info.sha256 = "0" * 64
    with pytest.raises(UpdateError, match="Контрольная сумма"):
        u.download(info, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_detects_truncated_file(tmp_path):
    u = make(api())
    info = u.check().latest
    info.size += 100
    with pytest.raises(UpdateError, match="не полностью"):
        u.download(info, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_can_be_cancelled(tmp_path):
    u = make(api())
    info = u.check().latest
    ev = threading.Event()
    ev.set()
    with pytest.raises(CancelledError):
        u.download(info, tmp_path, cancel=ev)
    assert list(tmp_path.iterdir()) == []


def test_download_refuses_untrusted_url(tmp_path):
    info = make(api()).check().latest
    info.url = "https://evil.example.com/BookMind-Setup.exe"
    with pytest.raises(UpdateError, match="безопасности"):
        make(api()).download(info, tmp_path)


def test_launch_installer_silent_args_and_guards(tmp_path):
    calls = []
    launch_installer(tmp_path / "s.exe", popen=lambda cmd, **kw: calls.append((cmd, kw)), frozen=True, platform="win32")
    cmd, kw = calls[0]
    assert cmd[0].endswith("s.exe") and "/SILENT" in cmd and "/CLOSEAPPLICATIONS" in cmd
    assert kw["creationflags"] & up.DETACHED_PROCESS
    with pytest.raises(UpdateError, match="исходников"):
        launch_installer(tmp_path / "s.exe", popen=lambda *a, **k: None, frozen=False, platform="win32")
    with pytest.raises(UpdateError, match="Windows"):
        launch_installer(tmp_path / "s.exe", popen=lambda *a, **k: None, frozen=True, platform="linux")
    with pytest.raises(UpdateError, match="Не удалось"):
        launch_installer(tmp_path / "s.exe", popen=lambda *a, **k: (_ for _ in ()).throw(OSError("denied")), frozen=True, platform="win32")
