import sys
from pathlib import Path

import pytest

SOLVER_DIR = Path(__file__).resolve().parents[1] / "solver" / "google_ai_mode"
if str(SOLVER_DIR) not in sys.path:
    sys.path.insert(0, str(SOLVER_DIR))

import hf_google_ai_provider as provider_module


class FakeJob:
    def __init__(self, payload):
        self.payload = payload

    def result(self, timeout=None):
        return self.payload

    def cancel(self):
        return True


class FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def submit(self, *args, **kwargs):
        return FakeJob(self.payload)


def test_huggingface_provider_returns_compatible_result(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "captcha.png"
    image.write_bytes(b"image")
    provider = provider_module.HuggingFaceGoogleAIProvider(
        space_id="owner/space", token="secret-token"
    )
    provider._client = FakeClient(
        {
            "ok": True,
            "answer": "{\"objetos\": []}",
            "http_requests": 6,
            "ai_queries": 1,
            "source_count": 0,
        }
    )
    monkeypatch.setattr("gradio_client.handle_file", lambda path: path)

    result = provider.query(image, "analise")

    assert result.answer == '{"objetos": []}'
    assert result.http_requests == 6
    assert result.route == "huggingface"
    assert provider.health()["successes"] == 1
    assert provider.health()["token_exposed"] is False


def test_huggingface_provider_opens_circuit_without_leaking_token(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "captcha.png"
    image.write_bytes(b"image")
    provider = provider_module.HuggingFaceGoogleAIProvider(
        space_id="owner/space", token="secret-token", cooldown_seconds=60
    )
    provider._client = FakeClient(
        {"ok": False, "error": "unusual traffic secret-token", "unusual_traffic": True}
    )
    monkeypatch.setattr("gradio_client.handle_file", lambda path: path)

    with pytest.raises(provider_module.HuggingFaceProviderError):
        provider.query(image, "analise")

    health = provider.health()
    assert health["failures"] == 1
    assert health["cooldown_remaining_seconds"] > 0
    assert "secret-token" not in str(health)
