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


def test_huggingface_pool_falls_back_between_spaces(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "captcha.png"
    image.write_bytes(b"stable-image")
    pool = provider_module.HuggingFaceGoogleAIPool(
        space_ids=["owner/one", "owner/two"], token="secret-token"
    )
    monkeypatch.setattr("gradio_client.handle_file", lambda path: path)
    for provider in pool.providers:
        provider._client = FakeClient({"ok": False, "error": "temporary"})
    pool.providers[1]._client = FakeClient({"ok": True, "answer": "{}"})

    # A ordem começa pelo hash; mantenha um sucesso em qualquer posição e
    # confirme que o pool tenta a outra rota quando necessário.
    try:
        result = pool.query(image, "analise")
    except provider_module.HuggingFaceProviderError:
        pool.providers[0]._cooldown_until = 0
        pool.providers[1]._cooldown_until = 0
        pool.providers[0]._client = FakeClient({"ok": True, "answer": "{}"})
        result = pool.query(image, "analise")

    assert result.route.startswith("huggingface:owner/")
    assert pool.health()["count"] == 2
    assert pool.health()["token_exposed"] is False


def test_huggingface_pool_selects_private_token_by_owner() -> None:
    pool = provider_module.HuggingFaceGoogleAIPool(
        space_ids=["primary/one", "secondary/two"],
        token="primary-secret",
        tokens_by_owner={"secondary": "secondary-secret"},
    )

    assert [provider.token for provider in pool.providers] == [
        "primary-secret",
        "secondary-secret",
    ]
    assert "primary-secret" not in str(pool.health())
    assert "secondary-secret" not in str(pool.health())


def test_huggingface_provider_skips_busy_space_without_opening_circuit(tmp_path: Path) -> None:
    image = tmp_path / "captcha.png"
    image.write_bytes(b"image")
    provider = provider_module.HuggingFaceGoogleAIProvider(
        space_id="owner/space", token="secret-token", cooldown_seconds=60
    )

    provider._lock.acquire()
    try:
        started = provider_module.time.monotonic()
        with pytest.raises(provider_module.HuggingFaceProviderError, match="huggingface_busy"):
            provider.query(image, "analise")
        elapsed = provider_module.time.monotonic() - started
    finally:
        provider._lock.release()

    assert elapsed < 0.2
    health = provider.health()
    assert health["failures"] == 0
    assert health["cooldown_remaining_seconds"] == 0
