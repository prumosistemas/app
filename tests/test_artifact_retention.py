import gzip
import os
import sys
import time
from pathlib import Path

from PIL import Image


SOLVER_DIR = Path(__file__).resolve().parents[1] / "solver" / "google_ai_mode"
if str(SOLVER_DIR) not in sys.path:
    sys.path.insert(0, str(SOLVER_DIR))

from artifact_retention import maintain_artifacts  # noqa: E402


def _age(path: Path, seconds: int, now: float) -> None:
    os.utime(path, (now - seconds, now - seconds))


def test_compacts_quiet_debug_files_without_touching_active_files(tmp_path) -> None:
    now = time.time()
    html = tmp_path / "dom.html"
    html.write_text("<html>" + "debug " * 10000 + "</html>", encoding="utf-8")
    _age(html, 1800, now)
    active = tmp_path / "active.json"
    active.write_text('{"active": true}', encoding="utf-8")

    result = maintain_artifacts(tmp_path, now=now, min_age_seconds=900)

    assert result["gzipped"] == 1
    assert not html.exists()
    with gzip.open(tmp_path / "dom.html.gz", "rt", encoding="utf-8") as stream:
        assert stream.read().startswith("<html>")
    assert active.exists()


def test_converts_quiet_png_losslessly_and_removes_expired_files(tmp_path) -> None:
    now = time.time()
    image = tmp_path / "canvas.png"
    Image.new("RGB", (800, 600), "white").save(image)
    _age(image, 1800, now)
    expired = tmp_path / "old.txt"
    expired.write_text("old", encoding="utf-8")
    _age(expired, 8 * 86400, now)

    result = maintain_artifacts(tmp_path, now=now, retention_days=7)

    assert result["webp"] == 1
    assert (tmp_path / "canvas.webp").exists()
    assert not image.exists()
    assert result["removed"] == 1
    assert not expired.exists()
