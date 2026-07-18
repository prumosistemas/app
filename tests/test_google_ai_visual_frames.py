import importlib.util
import os
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
