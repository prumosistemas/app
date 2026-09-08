import json
import sys
from pathlib import Path

import pytest

SOLVER_DIR = Path(__file__).resolve().parents[1] / "solver" / "google_ai_mode"
if str(SOLVER_DIR) not in sys.path:
    sys.path.insert(0, str(SOLVER_DIR))

import hf_qwen_provider as qwen


class FakeResponse:
    def __init__(self, status_code=200, answer="", usage=None):
        self.status_code = status_code
        self._answer = answer
        self._usage = usage or {}

    def json(self):
        return {
            "choices": [{"message": {"content": self._answer}}],
            "usage": self._usage,
        }


def test_qwen_normalizes_compact_target_into_solver_contract(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "temporal.jpg"
    image.write_bytes(b"image")
    monkeypatch.setattr(
        qwen.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(
            answer='```json\n{"label":"flor laranja","box":[300,200,430,450],"x":365,"y":335,"confidence":0.98}\n```',
            usage={"prompt_tokens": 200, "completion_tokens": 30},
        ),
    )
    provider = qwen.HuggingFaceQwenProvider(label="primary", token="secret")

    result = provider.query(image, "pergunta")
    parsed = json.loads(result.answer)

    assert parsed["escolha"]["x"] == 365
    assert parsed["objetos"]["objeto_1"]["caixa"]["x2"] == 430
    assert result.route == "huggingface_qwen:primary"
    assert provider.health()["prompt_tokens"] == 200
    assert "secret" not in str(provider.health())


def test_qwen_repairs_bare_key_and_packed_coordinates(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "temporal.jpg"
    image.write_bytes(b"image")
    monkeypatch.setattr(
        qwen.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(
            answer='{"label":"flor","x":[375,298], confidence:0.95}'
        ),
    )
    provider = qwen.HuggingFaceQwenProvider(label="secondary", token="secret")

    parsed = json.loads(provider.query(image, "pergunta").answer)

    assert parsed["escolha"]["x"] == 375
    assert parsed["escolha"]["y"] == 298
    assert parsed["objetos"]["objeto_1"]["caixa"]["x1"] == 320


def test_qwen_pool_falls_back_without_exposing_tokens(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "temporal.jpg"
    image.write_bytes(b"stable")
    pool = qwen.HuggingFaceQwenPool(
        tokens=[("primary", "first-secret"), ("secondary", "second-secret")]
    )
    calls = []

    def fake_query(provider, _image, _prompt):
        calls.append(provider.label)
        if len(calls) == 1:
            raise qwen.HuggingFaceQwenError("quota")
        return qwen.HuggingFaceQwenResult(answer="{}", route=f"huggingface_qwen:{provider.label}")

    for provider in pool.providers:
        monkeypatch.setattr(
            provider,
            "query",
            lambda image, prompt, p=provider: fake_query(p, image, prompt),
        )

    result = pool.query(image, "pergunta")

    assert len(calls) == 2
    assert result.route.startswith("huggingface_qwen:")
    assert "first-secret" not in str(pool.health())
    assert "second-secret" not in str(pool.health())


def test_qwen_rejects_low_confidence(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "temporal.jpg"
    image.write_bytes(b"image")
    monkeypatch.setattr(
        qwen.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(
            answer='{"label":"duvida","x":500,"y":500,"confidence":0.4}'
        ),
    )
    provider = qwen.HuggingFaceQwenProvider(label="primary", token="secret")

    with pytest.raises(qwen.HuggingFaceQwenError, match="low_confidence"):
        provider.query(image, "pergunta")
