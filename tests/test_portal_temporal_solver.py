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

    assert solver._temporal_capture_plan(question) == (30, 300)
    assert solver._question_needs_full_temporal_sequence(question)
    assert not solver._question_wants_trajectory_destination(question)
    assert solver._classify_visual_challenge(question)["category"] == "temporal_full"


@pytest.mark.parametrize(
    "question",
    [
        "Click the flower the bee never lands on",
        "Click the flower the bee always visits",
        "Clique na flor onde a abelha nunca pousa",
    ],
)
def test_full_temporal_semantics_are_multilingual(question):
    solver = _solver()

    assert solver._question_needs_full_temporal_sequence(question)
    assert solver._temporal_capture_plan(question) == (30, 300)
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


def test_temporal_montage_keeps_eighty_frames_under_three_megapixels(tmp_path):
    solver = _solver()
    frames = []
    for index in range(80):
        path = tmp_path / f"quadro-{index + 1:02d}.jpg"
        solver.legacy.Image.effect_noise((1000, 640), 40 + index).convert("RGB").save(path)
        frames.append(path)

    result = solver._build_motion_sequence(tmp_path, frames)
    info = __import__("json").loads(
        (tmp_path / "sequencia-temporal-info.json").read_text(encoding="utf-8")
    )

    assert result and result.is_file()
    assert info["frame_count"] == 80
    assert info["columns"] == 8
    assert info["rows"] == 10
    assert info["montage_width"] * info["montage_height"] < 3_000_000


def test_temporal_occupancy_board_preserves_click_geometry(tmp_path):
    solver = _solver()
    frames = []
    for index in range(8):
        image = solver.legacy.Image.effect_noise((320, 200), 24).convert("RGB")
        image.paste("black", (20 + index * 20, 80, 45 + index * 20, 105))
        path = tmp_path / f"quadro-{index + 1:02d}.jpg"
        image.save(path)
        frames.append(path)

    result = solver._build_temporal_occupancy_board(tmp_path, frames)
    info = __import__("json").loads(
        (tmp_path / "evidencia-permanencia-temporal-info.json").read_text(encoding="utf-8")
    )

    assert result and result.is_file()
    assert info["frame_count"] == 8
    assert info["columns"] == 1
    assert info["rows"] == 1
    assert info["frame_width"] == 320
    assert info["frame_height"] == 200
    assert info["montage_width"] == 320
    assert info["montage_height"] == 200


def test_multistage_temporal_scene_is_not_rotated_on_second_cycle(tmp_path):
    solver = _solver()
    image = tmp_path / "scene.jpg"
    scene = solver.legacy.Image.new("L", (256, 256), 0)
    scene.paste(255, (0, 0, 128, 256))
    scene.save(image)
    classification = solver._classify_visual_challenge(
        "Clique na flor onde a abelha nunca pousa"
    )

    attempts = [
        solver._register_scene_attempt("request-multistage-test", classification, image)
        for _ in range(9)
    ]

    assert attempts[0]["same_scene_attempt"] == 1
    assert attempts[1]["same_scene"]
    assert attempts[1]["same_scene_attempt"] == 2
    assert attempts[1]["sequence_attempt"] == 2
    assert classification["max_sequence_attempts"] == 4
    assert all(
        attempt["same_scene_attempt"] <= classification["max_same_scene_attempts"]
        for attempt in attempts[:8]
    )
    assert attempts[8]["same_scene_attempt"] > classification["max_same_scene_attempts"]


def test_unknown_repeated_scene_still_rotates_early():
    solver = _solver()
    classification = solver._classify_visual_challenge("")

    assert classification["category"] == "unknown"
    assert classification["max_same_scene_attempts"] == 1


def test_click_does_not_wait_after_legacy_visual_cleanup(monkeypatch):
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
    monkeypatch.setattr(
        solver.time,
        "sleep",
        lambda _seconds: pytest.fail("click wrapper must not pause the live animation"),
    )

    choice = {"x_percent_na_imagem": 20, "y_percent_na_imagem": 80}
    assert solver._click_non_9_choice_frozen(9222, choice)
    assert events == [("restore", 9222), ("click", 9222, choice)]
