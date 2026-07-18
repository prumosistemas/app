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
