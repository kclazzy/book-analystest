from bookmind.config import Settings


def test_settings_roundtrip_and_host_normalize(tmp_path, monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    s = Settings(ollama_host="localhost:11434", selected_model="qwen3:8b")
    s.save(tmp_path)
    loaded = Settings.load(tmp_path)
    assert loaded.selected_model == "qwen3:8b"
    assert loaded.ollama_host == "http://localhost:11434"


def test_env_host(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "10.0.0.5:11434")
    assert Settings.load(tmp_path).ollama_host == "http://10.0.0.5:11434"
