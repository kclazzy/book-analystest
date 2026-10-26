import httpx
import pytest

from bookmind.config import Settings
from bookmind.domain.exceptions import BookMindError, ModelNotInstalledError
from bookmind.llm.catalog import Catalog, install_catalog, load_builtin_catalog, load_catalog, parse_catalog
from bookmind.llm.model_manager import ModelManager, _registry_path


@pytest.fixture
def mgr(client):
    return ModelManager(client, load_builtin_catalog(), Settings())


def test_builtin_catalog_has_several_qwen_models():
    cat = load_builtin_catalog()
    assert len(cat.models) >= 5
    assert {m.family for m in cat.models} >= {"qwen3.5", "qwen3", "qwen2.5"}
    assert cat.default_model().tag == "qwen3.5:9b-q4_K_M"
    assert not any(m.is_cloud for m in cat.models)


def test_fitting_12gb_excludes_27b():
    tags = [m.tag for m in load_builtin_catalog().fitting(12)]
    assert "qwen3.5:27b" not in tags and "qwen3.5:9b-q4_K_M" in tags


def test_user_catalog_overrides_and_extends(tmp_path):
    install_catalog('{"models":[{"tag":"qwen3.5:9b","family":"qwen3.5","params_b":9,"approx_vram_gb":7},'
                    '{"tag":"qwen4:9b","family":"qwen4","params_b":9,"approx_vram_gb":7}]}', tmp_path)
    cat = load_catalog(tmp_path)
    assert cat.get("qwen4:9b") is not None
    assert cat.get("qwen3.5:9b").approx_vram_gb == 7
    assert cat.get("qwen3:8b") is not None  # встроенные сохранены


def test_bad_catalog_rejected():
    with pytest.raises(BookMindError):
        parse_catalog('{"models": [{"tag": 1}]}')


def test_statuses_marks_installed_and_unknown_qwen(mgr, fake):
    fake.models["qwen9-custom:latest"] = "sha-x"
    st = {s.tag: s for s in mgr.statuses()}
    assert st["qwen3.5:9b-q4_K_M"].installed and not st["qwen3.5:4b"].installed
    assert st["qwen9-custom:latest"].in_catalog is False


def test_select_requires_installed(mgr):
    with pytest.raises(ModelNotInstalledError):
        mgr.select("qwen3.5:4b")
    mgr.select("qwen3.5:9b-q4_K_M")
    assert mgr.settings.selected_model == "qwen3.5:9b-q4_K_M"


def test_resolve_falls_back_when_selected_missing(mgr, fake):
    mgr.settings.selected_model = "qwen3.5:27b"
    mgr.settings.fallback_models = ["qwen3:8b"]
    fake.models = {"qwen3:8b": "s1"}
    assert mgr.resolve_model() == "qwen3:8b"


def test_resolve_raises_when_nothing_installed(mgr, fake):
    fake.models = {}
    with pytest.raises(ModelNotInstalledError):
        mgr.resolve_model()


def test_install_and_multiple_models(mgr, fake):
    mgr.install("qwen3.5:4b")
    mgr.install("qwen3:8b")
    assert {"qwen3.5:4b", "qwen3:8b", "qwen3.5:9b-q4_K_M"} <= set(fake.models)


def test_cloud_models_blocked(mgr):
    with pytest.raises(BookMindError):
        mgr.install("qwen3.5:cloud")
    with pytest.raises(BookMindError):
        mgr.install("qwen3.5:397b-cloud")


def test_update_detects_changed_digest(mgr, fake):
    fake.pull_digest_after["qwen3.5:9b-q4_K_M"] = "sha-b"
    r = mgr.update("qwen3.5:9b-q4_K_M")
    assert r.changed and (r.digest_before, r.digest_after) == ("sha-a", "sha-b")


def test_update_unchanged(mgr, fake):
    fake.pull_digest_after["qwen3.5:9b-q4_K_M"] = "sha-a"
    assert not mgr.update("qwen3.5:9b-q4_K_M").changed


def test_update_all_only_installed(mgr, fake):
    fake.models["qwen3:8b"] = "s"
    assert {r.tag for r in mgr.update_all()} == {"qwen3.5:9b-q4_K_M", "qwen3:8b"}


def test_check_updates_registry_digest(mgr):
    def reg(req: httpx.Request):
        assert req.url.path == "/v2/library/qwen3.5/manifests/9b-q4_K_M"
        return httpx.Response(200, headers={"Docker-Content-Digest": "sha256:sha-NEW"})

    http = httpx.Client(transport=httpx.MockTransport(reg))
    assert mgr.check_updates(http) == {"qwen3.5:9b-q4_K_M": True}


def test_check_updates_unknown_on_network_error(mgr):
    def reg(req):
        raise httpx.ConnectError("offline")

    assert mgr.check_updates(httpx.Client(transport=httpx.MockTransport(reg))) == {"qwen3.5:9b-q4_K_M": None}


def test_registry_path():
    assert _registry_path("qwen3.5:9b") == ("library/qwen3.5", "9b")
    assert _registry_path("user/m") == ("user/m", "latest")
