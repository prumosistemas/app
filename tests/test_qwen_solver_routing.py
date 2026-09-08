import sys
from pathlib import Path
from types import SimpleNamespace


SOLVER_DIR = Path(__file__).resolve().parents[1] / "solver" / "google_ai_mode"
if str(SOLVER_DIR) not in sys.path:
    sys.path.insert(0, str(SOLVER_DIR))

import api_resolvedora_resolver_google_ia as solver


def test_qwen_temporal_route_survives_open_google_circuit(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "temporal.jpg"
    image.write_bytes(b"image")
    result = SimpleNamespace(
        answer="{}",
        route="huggingface_qwen:primary",
        http_requests=1,
        ai_queries=1,
        sources=(),
    )
    qwen = SimpleNamespace(configured=True, query=lambda *_args, **_kwargs: result)
    monkeypatch.setattr(solver, "HF_QWEN_PROVIDER", qwen)
    monkeypatch.setattr(solver, "HF_QWEN_MODE", "temporal_first")
    monkeypatch.setattr(solver.legacy, "provider_request_allowed", lambda: False)
    monkeypatch.setattr(solver.legacy, "audit_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        solver.google_ai,
        "query_google_ai",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("circuito Google aberto nao deve impedir nem suceder Qwen")
        ),
    )

    actual = solver._query_image(image, "legado", qwen_prompt="temporal")

    assert actual is result


def test_grid_solver_does_not_reference_temporal_state(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "grid.png"
    image.write_bytes(b"image")
    errors = []
    monkeypatch.setattr(solver.legacy, "provider_request_allowed", lambda: False)
    monkeypatch.setattr(
        solver.legacy,
        "provider_circuit_state",
        lambda: {"consecutive_failures": 3},
    )
    monkeypatch.setattr(
        solver.legacy,
        "set_solver_error",
        lambda reason, detail: errors.append((reason, detail)),
    )

    assert solver.solve_with_google_ai(image, "Tap buses") is None
    assert errors[0][0] == "provider_circuit_open"


def test_modal_bundle_mounts_qwen_provider() -> None:
    deploy = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "modal_portal_nacional_google_solver.py"
    ).read_text(encoding="utf-8")

    assert 'HF_QWEN_PROVIDER = SOURCE_ROOT / "hf_qwen_provider.py"' in deploy
    assert '.add_local_file(HF_QWEN_PROVIDER, "/app/hf_qwen_provider.py")' in deploy
