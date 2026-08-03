import importlib.util
import os
import time
from pathlib import Path

import pytest


SOLVER_DIR = Path(__file__).resolve().parents[1] / "solver" / "google_ai_mode"
os.environ.setdefault("GOOGLE_AI_PROJECT", str(SOLVER_DIR))
os.environ.setdefault("MODO_IA_DETECTOR_PROJECT", str(SOLVER_DIR))

SPEC = importlib.util.spec_from_file_location(
    "_test_google_ai_visual_solver",
    SOLVER_DIR / "api_resolvedora_resolver_google_ia.py",
)
assert SPEC is not None and SPEC.loader is not None
SOLVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SOLVER)


def test_empty_visual_frame_is_retryable_without_provider_penalty() -> None:
    with pytest.raises(SOLVER.VisualFrameNotReadyError):
        SOLVER._parse_non9_objects({"objetos": {}})


def test_malformed_visual_objects_remain_provider_errors() -> None:
    malformed = {
        "objetos": {
            "objeto_1": {
                "nome": "alvo",
                "caixa": {"x1": "invalido", "y1": 10, "x2": 20, "y2": 30},
            }
        }
    }
    with pytest.raises(ValueError) as exc_info:
        SOLVER._parse_non9_objects(malformed)
    assert not isinstance(exc_info.value, SOLVER.VisualFrameNotReadyError)


def test_provider_circuit_records_open_time_and_rearms() -> None:
    original_limit = SOLVER.legacy.PROVIDER_FAILURE_LIMIT
    original_cooldown = SOLVER.legacy.PROVIDER_CIRCUIT_COOLDOWN_SECONDS
    try:
        SOLVER.reset_provider_circuit()
        SOLVER.legacy.PROVIDER_FAILURE_LIMIT = 1
        SOLVER.legacy.PROVIDER_CIRCUIT_COOLDOWN_SECONDS = 1

        state = SOLVER.record_provider_failure("falha controlada")

        assert state["open"] is True
        assert SOLVER.legacy.PROVIDER_CIRCUIT_OPENED_AT > 0

        SOLVER.legacy.PROVIDER_CIRCUIT_OPENED_AT = time.monotonic() - 2
        assert SOLVER.provider_circuit_state()["open"] is False
    finally:
        SOLVER.legacy.PROVIDER_FAILURE_LIMIT = original_limit
        SOLVER.legacy.PROVIDER_CIRCUIT_COOLDOWN_SECONDS = original_cooldown
        SOLVER.reset_provider_circuit()


def test_solver_origin_is_restricted_to_official_nfse_hosts() -> None:
    normalize = SOLVER.legacy.normalized_solver_origin_url

    assert normalize("https://www.nfse.gov.br/EmissorNacional/Dashboard") == (
        "https://www.nfse.gov.br/EmissorNacional/Dashboard"
    )
    assert normalize("https://nfse.gov.br/") == "https://nfse.gov.br/"
    assert normalize("http://www.nfse.gov.br/") == "https://www.nfse.gov.br/"
    assert normalize("https://www.nfse.gov.br.evil.example/") == "https://www.nfse.gov.br/"


def test_official_origin_document_keeps_token_in_page() -> None:
    page = SOLVER.legacy.solver_page_html('sitekey"segura', local_callback=False)

    assert 'id="hcaptcha-root"' in page
    assert 'data-sitekey=' not in page
    assert "js.hcaptcha.com" not in page
    assert "window.__lastHcaptchaToken" in page
    assert "fetch('/token" not in page


def test_inject_solver_document_uses_top_frame(monkeypatch) -> None:
    calls: list[tuple[str, dict | None]] = []
    page = {
        "type": "page",
        "title": "Portal Nacional",
        "url": "https://www.nfse.gov.br/",
        "webSocketDebuggerUrl": "ws://solver",
    }

    class FakeClient:
        def __init__(self, websocket_url: str):
            assert websocket_url == "ws://solver"

        def call(self, method: str, params: dict | None = None):
            calls.append((method, params))
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"id": "top-frame"}}}
            return {}

        def eval(self, _expression: str):
            return {
                "ready": True,
                "scriptError": False,
                "hcaptchaLoaded": True,
                "checkboxFrames": 1,
            }

        def close(self) -> None:
            calls.append(("close", None))

    monkeypatch.setattr(SOLVER.legacy, "list_pages", lambda _port: [page])
    monkeypatch.setattr(SOLVER.legacy, "CdpClient", FakeClient)

    assert SOLVER.legacy.inject_solver_document(9222, "sitekey") == page
    document_call = next(params for method, params in calls if method == "Page.setDocumentContent")
    assert document_call["frameId"] == "top-frame"
    assert "Desafio hCaptcha" in document_call["html"]
    runtime_expressions = [
        params["expression"]
        for method, params in calls
        if method == "Runtime.evaluate" and params and "expression" in params
    ]
    assert all("hcaptcha.execute" not in expression for expression in runtime_expressions)
    assert any("size: 'normal'" in expression for expression in runtime_expressions)
    assert any("__hcaptchaWidgetError" in expression for expression in runtime_expressions)


def test_visual_canvas_detection_does_not_depend_on_portuguese_label() -> None:
    for name in (
        "api_resolvedora_resolver.py",
        "api_resolvedora_resolver_google_ia.py",
    ):
        source = (SOLVER_DIR / name).read_text(encoding="utf-8")
        assert "Desafio de CAPTCHA baseado em imagem" not in source
        assert "querySelectorAll('canvas')" in source


def test_solver_accepts_token_without_opening_visual_challenge(monkeypatch) -> None:
    monkeypatch.setattr(SOLVER.legacy, "extract_token_from_page", lambda _port: "token-direto")
    monkeypatch.setattr(
        SOLVER.legacy,
        "ensure_challenge_open",
        lambda _port: (_ for _ in ()).throw(AssertionError("nao deveria abrir grade")),
    )

    assert SOLVER.legacy.auto_solve_grid(9222, 0, 1, max_refreshes=1) == "token-direto"
