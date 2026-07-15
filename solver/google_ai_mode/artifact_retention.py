"""Retencao e compactacao conservadora dos artefatos de debug do solver.

Mantem a janela operacional configurada, compacta somente arquivos inativos e
nao toca em XML/PDF das empresas. Pode ser usado no ThinkPad e no Modal.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any


TEXT_SUFFIXES = {".html", ".json", ".txt"}


def _gzip_file(path: Path) -> tuple[bool, int]:
    stat = path.stat()
    target = path.with_suffix(path.suffix + ".gz")
    temporary = target.with_suffix(target.suffix + ".tmp")
    if target.exists():
        return False, 0
    try:
        with path.open("rb") as source, gzip.open(temporary, "wb", compresslevel=6) as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        os.utime(temporary, (stat.st_atime, stat.st_mtime))
        temporary.replace(target)
        path.unlink()
        return True, max(0, stat.st_size - target.stat().st_size)
    finally:
        temporary.unlink(missing_ok=True)


def _png_to_lossless_webp(path: Path) -> tuple[bool, int]:
    from PIL import Image

    stat = path.stat()
    target = path.with_suffix(".webp")
    temporary = target.with_suffix(".webp.tmp")
    if target.exists():
        return False, 0
    try:
        with Image.open(path) as image:
            image.save(temporary, format="WEBP", lossless=True, method=4)
        if temporary.stat().st_size >= stat.st_size:
            return False, 0
        os.utime(temporary, (stat.st_atime, stat.st_mtime))
        temporary.replace(target)
        path.unlink()
        return True, max(0, stat.st_size - target.stat().st_size)
    finally:
        temporary.unlink(missing_ok=True)


def maintain_artifacts(
    root: Path,
    *,
    retention_days: int = 7,
    min_age_seconds: int = 900,
    now: float | None = None,
) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    cutoff = current - max(1, retention_days) * 86400
    quiet_cutoff = current - max(300, min_age_seconds)
    stats: dict[str, Any] = {
        "root": str(root),
        "removed": 0,
        "gzipped": 0,
        "webp": 0,
        "bytes_saved": 0,
        "errors": 0,
    }
    if not root.is_dir():
        return stats

    for path in list(root.rglob("*")):
        if not path.is_file() or path.name == ".retention.lock":
            continue
        try:
            stat = path.stat()
            if stat.st_mtime < cutoff:
                stats["bytes_saved"] += stat.st_size
                path.unlink()
                stats["removed"] += 1
                continue
            if stat.st_mtime > quiet_cutoff:
                continue
            if path.suffix.lower() in TEXT_SUFFIXES and stat.st_size >= 1024:
                changed, saved = _gzip_file(path)
                if changed:
                    stats["gzipped"] += 1
                    stats["bytes_saved"] += saved
            elif path.suffix.lower() == ".png" and stat.st_size >= 1024:
                changed, saved = _png_to_lossless_webp(path)
                if changed:
                    stats["webp"] += 1
                    stats["bytes_saved"] += saved
        except (OSError, ValueError):
            stats["errors"] += 1

    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--retention-days", type=int, default=7)
    parser.add_argument("--min-age-seconds", type=int, default=900)
    args = parser.parse_args()
    result = maintain_artifacts(
        args.root,
        retention_days=args.retention_days,
        min_age_seconds=args.min_age_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
