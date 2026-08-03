from __future__ import annotations

import importlib.util
import os
from functools import lru_cache
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOLVER_DIR = ROOT / "solver" / "google_ai_mode"


@lru_cache(maxsize=1)
def _solver():
    os.environ["GOOGLE_AI_PROJECT"] = str(SOLVER_DIR)
    os.environ["MODO_IA_DETECTOR_PROJECT"] = str(SOLVER_DIR)
    path = SOLVER_DIR / "api_resolvedora_resolver_google_ia.py"
    spec = importlib.util.spec_from_file_location("_portal_temporal_solver_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_never_lands_uses_full_sequence_without_trajectory_override():
    solver = _solver()
    question = "Clique na flor onde a abelha nunca pousa"

    assert solver._temporal_capture_plan(question) == (28, 220)
    assert solver._question_needs_full_temporal_sequence(question)
    assert not solver._question_wants_trajectory_destination(question)
    assert solver._classify_visual_challenge(question)["category"] == "temporal_full"


def test_destination_question_keeps_trajectory_override():
    solver = _solver()

    assert solver._question_wants_trajectory_destination(
        "Clique no destino para onde o objeto vai chegar"
    )


def test_montage_coordinates_return_to_original_frame():
    solver = _solver()
    frame_width, frame_height, gap = 500, 320, 6
    columns, rows = 4, 4
    montage_width = frame_width * columns + gap * (columns - 1)
    montage_height = frame_height * rows + gap * (rows - 1)

    # Alvo em x=80%, y=40% do quadro da coluna 3/linha 4.
    center_x_px = 2 * (frame_width + gap) + frame_width * 0.8
    center_y_px = 3 * (frame_height + gap) + frame_height * 0.4
    x = center_x_px / montage_width * 1000
    y = center_y_px / montage_height * 1000
    parsed = {
        "objetos": {
            "alvo": {
                "caixa": {
                    "x1": x - 5,
                    "y1": y - 5,
                    "x2": x + 5,
                    "y2": y + 5,
                }
            }
        },
        "escolha": {"objeto": "alvo", "x": x, "y": y},
    }

    converted = solver._sequence_coordinates_to_frame(
        parsed,
        frame_width,
        frame_height,
        montage_width,
        montage_height,
        columns,
        rows,
        gap,
        gap,
    )

    assert converted["escolha"]["x"] == pytest.approx(800, abs=1)
    assert converted["escolha"]["y"] == pytest.approx(400, abs=1)


def test_temporal_montage_keeps_twenty_eight_frames_under_three_megapixels(tmp_path):
    solver = _solver()
    frames = []
    for index in range(28):
        path = tmp_path / f"quadro-{index + 1:02d}.jpg"
        solver.legacy.Image.effect_noise((1000, 640), 40 + index).convert("RGB").save(path)
        frames.append(path)

    result = solver._build_motion_sequence(tmp_path, frames)
    info = __import__("json").loads(
        (tmp_path / "sequencia-temporal-info.json").read_text(encoding="utf-8")
    )

    assert result and result.is_file()
    assert info["frame_count"] == 28
    assert info["columns"] == 6
    assert info["rows"] == 5
    assert info["montage_width"] * info["montage_height"] < 3_000_000


def test_repeated_complex_scene_is_marked_for_rotation(tmp_path):
    solver = _solver()
    image = tmp_path / "scene.jpg"
    scene = solver.legacy.Image.new("L", (256, 256), 0)
    scene.paste(255, (0, 0, 128, 256))
    scene.save(image)
    classification = solver._classify_visual_challenge(
        "Clique na flor onde a abelha nunca pousa"
    )

    first = solver._register_scene_attempt("request-rotate-test", classification, image)
    second = solver._register_scene_attempt("request-rotate-test", classification, image)

    assert first["same_scene_attempt"] == 1
    assert second["same_scene"]
    assert second["same_scene_attempt"] == 2
    assert second["same_scene_attempt"] > classification["max_same_scene_attempts"]


def test_visual_loop_is_restored_before_click(monkeypatch):
    solver = _solver()
    events = []
    monkeypatch.setattr(
        solver,
        "_restore_visual_animation",
        lambda port: events.append(("restore", port)),
    )
    monkeypatch.setattr(
        solver,
        "_legacy_click_non_9_choice",
        lambda port, choice: events.append(("click", port, choice)) or True,
    )
    monkeypatch.setattr(solver.time, "sleep", lambda _seconds: None)

    choice = {"x_percent_na_imagem": 20, "y_percent_na_imagem": 80}
    assert solver._click_non_9_choice_frozen(9222, choice)
    assert events == [("restore", 9222), ("click", 9222, choice)]
