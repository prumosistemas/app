import json
import sys
from pathlib import Path

import pytest


SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import solver_audit


class ManifestEntry:
    def __init__(self, path: str) -> None:
        self.path = path


def test_solver_audit_mirrors_temporal_frames_without_unrelated_files() -> None:
    assert solver_audit._wanted_summary("/desafios/unificados/x/quadro-01.jpg")
    assert solver_audit._wanted_summary("/desafios/unificados/x/quadro-120.png")
    assert not solver_audit._wanted_summary("/desafios/unificados/x/certificado.pfx")


def test_solver_audit_compacts_manifest_to_current_window() -> None:
    manifest = {"/current.json": "1:10", "/expired.json": "2:20"}

    compact = solver_audit._compact_manifest(
        manifest,
        [ManifestEntry("/current.json"), ManifestEntry("/new.json")],
    )

    assert compact == {"/current.json": "1:10"}


def test_solver_audit_removes_frames_only_after_video_exists(tmp_path: Path) -> None:
    challenge = tmp_path / "desafios" / "unificados" / "challenge"
    challenge.mkdir(parents=True)
    (challenge / "quadro-01.jpg").write_bytes(b"frame")
    (challenge / "quadro-02.jpg").write_bytes(b"frame")

    assert solver_audit._build_missing_local_videos(tmp_path, limit=0) == (0, 0)
    assert (challenge / "quadro-01.jpg").exists()

    (challenge / "captura-temporal.mp4").write_bytes(b"video")
    assert solver_audit._build_missing_local_videos(tmp_path, limit=0) == (0, 2)
    assert not (challenge / "quadro-01.jpg").exists()


def test_solver_audit_summarizes_route_clicks_and_unusual(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "thinkpad"
    audit = root / "auditoria" / "2026-08-07" / "req-1.jsonl"
    audit.parent.mkdir(parents=True)
    events = [
        {"timestamp": "2026-08-07T12:00:00+00:00", "elapsed_seconds": 0, "event": "solve_received", "request_id": "req-1", "location": "thinkpad", "active_browsers": 1, "max_browsers": 4},
        {"timestamp": "2026-08-07T12:00:01+00:00", "elapsed_seconds": 1, "event": "provider_failure", "request_id": "req-1", "route": "huggingface:one", "unusual_traffic": True},
        {"timestamp": "2026-08-07T12:00:03+00:00", "elapsed_seconds": 3, "event": "provider_success", "request_id": "req-1", "route": "huggingface:two"},
        {"timestamp": "2026-08-07T12:00:04+00:00", "elapsed_seconds": 4, "event": "click_point", "request_id": "req-1", "x": 100, "y": 200},
        {"timestamp": "2026-08-07T12:00:05+00:00", "elapsed_seconds": 5, "event": "solve_finished", "request_id": "req-1", "success": True, "reason": "token"},
    ]
    audit.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
    monkeypatch.setattr(solver_audit, "source_roots", lambda: {"thinkpad": root})
    monkeypatch.setattr(solver_audit, "mirror_root", lambda: tmp_path / "mirror")

    result = solver_audit.list_solver_audits(10)

    row = result["audits"][0]
    assert row["success"] is True
    assert row["unusual_traffic"] is True
    assert row["route_successes"] == ["huggingface:two"]
    assert row["clicks"][0]["x"] == 100


def test_solver_audit_rejects_path_escape(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(solver_audit, "source_roots", lambda: {"thinkpad": tmp_path})
    with pytest.raises((ValueError, FileNotFoundError)):
        solver_audit.resolve_audit_file("thinkpad", "../secret.txt")
