import importlib.util
import base64
import io
import itertools
import os
import time
from pathlib import Path

import pytest
import numpy as np


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


def test_never_lands_uses_lowest_temporal_occupancy_without_affecting_trajectory(
    tmp_path: Path,
) -> None:
    occupancy = np.zeros((100, 100), dtype=np.uint8)
    occupancy[20:40, 10:30] = 255
    occupancy[20:40, 40:60] = 210
    SOLVER.legacy.Image.fromarray(occupancy, mode="L").save(
        tmp_path / "ocupacao-temporal.png"
    )
    parsed = {
        "objetos": {
            "flor_1": {"nome": "flor esquerda", "caixa": {"x1": 50, "y1": 150, "x2": 350, "y2": 450}},
            "flor_2": {"nome": "flor central", "caixa": {"x1": 350, "y1": 150, "x2": 650, "y2": 450}},
            "flor_3": {"nome": "flor direita", "caixa": {"x1": 650, "y1": 150, "x2": 950, "y2": 450}},
        },
        "escolha": {"objeto": "flor_1", "x": 200, "y": 300},
    }

    result = SOLVER._override_never_choice_from_occupancy(
        parsed, tmp_path, "Click the flower the bee never lands on"
    )

    assert result is not None and result["applied"] is True
    assert parsed["escolha"]["objeto"] == "flor_3"

    trajectory = {**parsed, "escolha": {"objeto": "flor_1", "x": 200, "y": 300}}
    assert (
        SOLVER._override_never_choice_from_occupancy(
            trajectory, tmp_path, "In which basket will the ball land?"
        )
        is None
    )
    assert trajectory["escolha"]["objeto"] == "flor_1"


def test_google_ai_recovery_uses_official_ai_entrypoint() -> None:
    params = SOLVER.google_ai._recovery_search_params(image_required=True)
    url = SOLVER.google_ai._recovery_browser_url(image_required=True)

    assert params["udm"] == "50"
    assert params["aep"] == "11"
    assert url.startswith("https://www.google.com/ai?")
    assert "aep=11" in url


def test_unusual_traffic_detection_stops_same_egress_recovery() -> None:
    assert SOLVER.google_ai._is_unusual_traffic_error(
        SOLVER.google_ai.GoogleAIModeError(
            "Google abriu https://www.google.com/sorry/index por unusual traffic"
        )
    )
    assert not SOLVER.google_ai._is_unusual_traffic_error(
        SOLVER.google_ai.GoogleAIModeError("resposta temporariamente vazia")
    )


def test_linux_chrome_recovery_stops_the_entire_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[int, int]] = []

    class Process:
        pid = 4321

        @staticmethod
        def wait(timeout: float) -> int:
            return 0

    monkeypatch.setattr(SOLVER.google_ai.sys, "platform", "linux")
    monkeypatch.setattr(
        SOLVER.google_ai.os,
        "killpg",
        lambda pid, sig: calls.append((pid, sig)),
        raising=False,
    )
    monkeypatch.setattr(SOLVER.google_ai.time, "sleep", lambda _seconds: None)

    SOLVER.google_ai._stop_firefox_profile(
        Process(), tmp_path, process_group=True
    )

    assert calls == [(4321, SOLVER.google_ai.signal.SIGTERM)]


def test_empty_visual_frame_is_retryable_without_provider_penalty() -> None:
    with pytest.raises(SOLVER.VisualFrameNotReadyError):
        SOLVER._parse_non9_objects({"objetos": {}})


def test_static_canvas_does_not_enable_synthetic_virtual_time(monkeypatch, tmp_path: Path) -> None:
    buffer = io.BytesIO()
    SOLVER.legacy.Image.new("RGB", (1000, 940), "purple").save(buffer, format="JPEG")
    data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    calls: list[tuple[str, dict | None]] = []

    class FakeSocket:
        @staticmethod
        def settimeout(_seconds: float) -> None:
            return None

    class FakeClient:
        ws = FakeSocket()

        def __init__(self, _url: str) -> None:
            pass

        @staticmethod
        def eval(_expression: str, await_promise: bool = False):
            if await_promise:
                return {
                    "width": 1000,
                    "height": 940,
                    "top_cut_native": 0,
                    "interval_ms": 180,
                    "frames": [data_url],
                }
            return {"restored": True}

        @staticmethod
        def call(method: str, params: dict | None = None):
            calls.append((method, params))
            return {}

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(
        SOLVER.legacy,
        "challenge_page",
        lambda _port: {"webSocketDebuggerUrl": "ws://challenge"},
    )
    monkeypatch.setattr(SOLVER.legacy, "CdpClient", FakeClient)
    monkeypatch.setattr(SOLVER.legacy, "png_seems_blank", lambda _path: False)

    frames = SOLVER._capture_visual_canvas_sequence(9222, tmp_path, frame_count=1)
    SOLVER._restore_visual_animation(9222)

    assert frames
    assert not [method for method, _params in calls if method == "Emulation.setVirtualTimePolicy"]


def test_post_submit_detects_changed_visual_stage_without_checkbox_retry(monkeypatch) -> None:
    states = itertools.repeat(
        {
            "challenge_ready": True,
            "loading": False,
            "fingerprint": "new-stage",
        }
    )
    events: list[tuple[str, dict]] = []
    SOLVER.POST_SUBMIT_STATE_BY_PORT[9222] = {
        "before": {"fingerprint": "old-stage"},
        "submitted_at": 1.0,
    }
    monkeypatch.setattr(SOLVER.legacy, "solver_browser_alive", lambda *_args: True)
    monkeypatch.setattr(SOLVER.legacy, "extract_token_from_page", lambda _port: None)
    monkeypatch.setattr(SOLVER.legacy, "captcha_checkmark_visible", lambda _port: False)
    monkeypatch.setattr(SOLVER.legacy, "captcha_retry_error_visible", lambda _port: False)
    monkeypatch.setattr(SOLVER, "_challenge_wait_state", lambda _port: next(states))
    monkeypatch.setattr(SOLVER.legacy, "TOKEN_POLL_SECONDS", 0.4)
    monkeypatch.setattr(
        SOLVER.legacy,
        "audit_event",
        lambda name, **fields: events.append((name, fields)),
    )
    started = time.time()

    result = SOLVER._wait_token_or_next_stage_google_ai(9222, timeout=2.0)

    assert result is None
    assert time.time() - started < 1.8
    assert 9222 not in SOLVER.POST_SUBMIT_STATE_BY_PORT
    assert events[-1][0] == "post_submit_transition"
    assert events[-1][1]["result"] == "next_stage"


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


def test_point_prompt_omits_grid_contract() -> None:
    prompt = SOLVER._unified_visual_prompt(
        "Click the animal that does not match",
        1000,
        640,
        point_only=True,
    )

    assert '"acao": "clicar_ponto"' in prompt
    assert '"objetos"' in prompt
    assert '"escolha"' in prompt
    assert '"tile_1"' not in prompt
    assert '"resposta_direta":' not in prompt


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

        def send(self, method: str, params: dict | None = None):
            calls.append((method, params))
            return 7

        def eval(self, _expression: str):
            return {
                "ready": True,
                "scriptError": False,
                "hcaptchaLoaded": True,
                "checkboxFrames": 1,
            }

        def wait_event(self, method: str, timeout: float = 10.0):
            assert method == "Fetch.requestPaused"
            assert timeout > 0
            return {"requestId": "request-1"}

        def close(self) -> None:
            calls.append(("close", None))

    monkeypatch.setattr(SOLVER.legacy, "list_pages", lambda _port: [page])
    monkeypatch.setattr(SOLVER.legacy, "CdpClient", FakeClient)

    assert SOLVER.legacy.inject_solver_document(9222, "sitekey") == page
    assert all(method != "Page.setDocumentContent" for method, _params in calls)
    navigate = next(params for method, params in calls if method == "Page.navigate")
    assert navigate["url"].startswith("https://www.nfse.gov.br/")
    fulfill = next(params for method, params in calls if method == "Fetch.fulfillRequest")
    assert fulfill["requestId"] == "request-1"
    assert "Desafio hCaptcha" in __import__("base64").b64decode(fulfill["body"]).decode("utf-8")
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


def test_stuck_widget_renews_browser_after_three_click_cycles(monkeypatch) -> None:
    monkeypatch.setattr(SOLVER.legacy, "extract_token_from_page", lambda _port: None)
    monkeypatch.setattr(SOLVER.legacy, "solver_browser_alive", lambda *_args: True)
    monkeypatch.setattr(
        SOLVER.legacy,
        "fatal_circuit_state",
        lambda: {"open": False, "reason": None, "error": None},
    )
    monkeypatch.setattr(SOLVER.legacy, "ensure_challenge_open", lambda _port: False)
    monkeypatch.setattr(SOLVER.legacy, "click_hcaptcha_checkbox", lambda _port: False)
    monkeypatch.setattr(SOLVER.legacy, "challenge_grid_visible", lambda _port: False)
    monkeypatch.setattr(SOLVER.legacy.time, "sleep", lambda _seconds: None)

    assert SOLVER.legacy.auto_solve_grid(9222, 0, 1, max_refreshes=10) is None
    error = SOLVER.legacy.get_solver_error()
    assert error["reason"] == "visual_challenge_not_opened"
    assert "renovando navegador" in error["detail"]


def test_truthy_but_empty_challenge_state_renews_browser_early(monkeypatch) -> None:
    monkeypatch.setattr(SOLVER.legacy, "extract_token_from_page", lambda _port: None)
    monkeypatch.setattr(SOLVER.legacy, "solver_browser_alive", lambda *_args: True)
    monkeypatch.setattr(
        SOLVER.legacy,
        "fatal_circuit_state",
        lambda: {"open": False, "reason": None, "error": None},
    )
    monkeypatch.setattr(SOLVER.legacy, "ensure_challenge_open", lambda _port: True)
    monkeypatch.setattr(
        SOLVER.legacy,
        "wait_for_stable_9_tile_challenge",
        lambda _port: (None, {"prompt": "selecione a abelha", "tasks": []}),
    )
    monkeypatch.setattr(SOLVER.legacy.time, "sleep", lambda _seconds: None)

    assert SOLVER.legacy.auto_solve_grid(9222, 0, 1, max_refreshes=10) is None
    error = SOLVER.legacy.get_solver_error()
    assert error["reason"] == "desafio_nao_pronto"
    assert "sem grade, canvas ou tarefas" in error["detail"]


def test_timeout_reason_is_not_overwritten_by_generic_grid_error(monkeypatch) -> None:
    monkeypatch.setattr(SOLVER.legacy, "extract_token_from_page", lambda _port: None)
    monkeypatch.setattr(SOLVER.legacy, "solver_browser_alive", lambda *_args: True)
    monkeypatch.setattr(
        SOLVER.legacy,
        "fatal_circuit_state",
        lambda: {"open": False, "reason": None, "error": None},
    )

    assert SOLVER.legacy.auto_solve_grid(9222, 0, 1, deadline=0) is None
    error = SOLVER.legacy.get_solver_error()
    assert error["reason"] == "solve_timeout"
