"""Espelho enxuto e consulta da auditoria visual do Portal Nacional.

Os frames brutos permanecem sete dias no Volume Modal. O ThinkPad espelha
somente JSONs, imagens-resumo e MP4s, suficientes para investigar rota,
latência, bloqueio, cliques e troca de desafio sem crescer sem limite.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


SYNC_INTERVAL_SECONDS = max(30, int(os.getenv("PORTAL_SOLVER_AUDIT_SYNC_SECONDS", "120")))
SYNC_ACCOUNT_TIMEOUT_SECONDS = max(
    60, int(os.getenv("PORTAL_SOLVER_AUDIT_ACCOUNT_TIMEOUT_SECONDS", "300"))
)
RETENTION_DAYS = max(1, int(os.getenv("PORTAL_DEBUG_RETENTION_DAYS", "7")))
MAX_BOOTSTRAP_CHALLENGES = max(10, int(os.getenv("PORTAL_SOLVER_AUDIT_BOOTSTRAP_DIRS", "40")))

SUMMARY_NAMES = {
    "desafio.png", "desafio.webp", "desafio-anotado-google-ia.png",
    "desafio-anotado-google-ia.webp", "sequencia-temporal.jpg",
    "desafio-temporal-sobreposto.jpg", "evidencia-permanencia-temporal.jpg",
    "quadro-atual-antes-clique.jpg", "clique-planejado.jpg",
    "cliques-planejados.jpg", "navegador-caixas-verdes.png",
    "navegador-caixas-verdes.webp", "captura-temporal.mp4",
    "resposta-google-ia.json", "classificacao-desafio.json", "timing.json",
    "validacao-antes-clique.json", "overlay-browser.json", "canvas-info.json",
    "estado-dom.json", "antes-captura-dom.json", "capturado-dom.json",
    "video-origin.json",
}
SAFE_MEDIA_SUFFIXES = {".json", ".jsonl", ".jpg", ".jpeg", ".png", ".webp", ".mp4"}

_THREAD_LOCK = threading.Lock()
_THREAD_STARTED = False


def _output_root() -> Path:
    return Path(os.getenv("ISS_OUTPUT_ROOT", str(Path(__file__).resolve().parent / "output")))


def mirror_root() -> Path:
    return _output_root() / "_api_data" / "portal_solver_audit"


def source_roots() -> dict[str, Path]:
    return {
        "modal_primary": mirror_root() / "modal_primary",
        "modal_fallback": mirror_root() / "modal_fallback",
        "modal_tertiary": mirror_root() / "modal_tertiary",
        "thinkpad": _output_root() / "_api_data" / "google_ai_solver_artifacts",
    }


def _safe_relative(remote_path: str) -> Path:
    parts = [part for part in PurePosixPath(remote_path).parts if part not in {"", "/", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("invalid_remote_path")
    return Path(*parts)


def _wanted_summary(remote_path: str) -> bool:
    name = PurePosixPath(remote_path).name
    return name in SUMMARY_NAMES or bool(re.fullmatch(r"quadro-\d{2,3}\.(?:jpg|png)", name))


def _create_local_video(folder: Path) -> bool:
    """Monta o MP4 no ThinkPad, fora do caminho critico do solver Modal."""
    target = folder / "captura-temporal.mp4"
    if target.is_file() and target.stat().st_size > 0:
        return False
    frames = sorted(folder.glob("quadro-[0-9][0-9].jpg"))
    if len(frames) < 2:
        return False
    temporary = folder / "captura-temporal.local.mp4"
    try:
        import cv2

        first = cv2.imread(str(frames[0]))
        if first is None:
            return False
        height, width = first.shape[:2]
        interval_ms = 300
        try:
            info = json.loads((folder / "canvas-info.json").read_text(encoding="utf-8"))
            interval_ms = max(1, int(info.get("interval_ms") or interval_ms))
        except (OSError, ValueError, TypeError):
            pass
        writer = cv2.VideoWriter(
            str(temporary),
            cv2.VideoWriter_fourcc(*"mp4v"),
            max(2.0, min(12.0, 1000.0 / interval_ms)),
            (width, height),
        )
        if not writer.isOpened():
            return False
        try:
            for path in frames:
                frame = cv2.imread(str(path))
                if frame is None:
                    continue
                if frame.shape[1] != width or frame.shape[0] != height:
                    frame = cv2.resize(frame, (width, height))
                writer.write(frame)
        finally:
            writer.release()
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            temporary.unlink(missing_ok=True)
            return False
        os.replace(temporary, target)
        _write_json_atomic(folder / "video-origin.json", {
            "created_on": "thinkpad",
            "frame_count": len(frames),
            "interval_ms": interval_ms,
            "foreground_solver_cpu_used": False,
        })
        return True
    except Exception:
        temporary.unlink(missing_ok=True)
        return False


def _build_missing_local_videos(root: Path, limit: int = 4) -> tuple[int, int]:
    challenge_root = root / "desafios" / "unificados"
    if not challenge_root.is_dir():
        return 0
    folders = sorted(
        (path for path in challenge_root.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    created = 0
    frames_removed = 0
    for folder in folders:
        video = folder / "captura-temporal.mp4"
        if not video.is_file() or video.stat().st_size <= 0:
            if created >= max(1, limit):
                continue
            if not _create_local_video(folder):
                continue
            created += 1
        # O MP4, a imagem de ocupacao e os resumos permanecem no ThinkPad.
        # Os quadros brutos continuam sete dias no Volume Modal e nao precisam
        # ocupar duas vezes o disco local depois da conversao comprovada.
        for frame in [*folder.glob("quadro-*.jpg"), *folder.glob("quadro-*.png")]:
            try:
                frame.unlink()
                frames_removed += 1
            except OSError:
                pass
    return created, frames_removed


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


async def _download_file(volume: Any, remote_path: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("wb") as handle:
        async for chunk in volume.read_file.aio(remote_path):
            handle.write(chunk)
    os.replace(temporary, target)


def _load_manifest(root: Path) -> dict[str, str]:
    try:
        value = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _compact_manifest(manifest: dict[str, str], candidates: list[Any]) -> dict[str, str]:
    """Descarta assinaturas que nao pertencem mais à janela sincronizada."""
    wanted = {str(entry.path) for entry in candidates}
    return {path: signature for path, signature in manifest.items() if path in wanted}


async def _sync_account(role: str, token_id: str, token_secret: str) -> dict[str, Any]:
    import modal

    target_root = mirror_root() / role
    manifest = _load_manifest(target_root)
    client = await modal.Client.from_credentials.aio(token_id, token_secret)
    volume = modal.Volume.from_name(
        "prumo-portal-debug-artifacts-v2", version=2, client=client
    )
    challenge_entries = []
    try:
        async for entry in volume.iterdir.aio("/desafios/unificados", recursive=False):
            challenge_entries.append(entry)
    except Exception:
        challenge_entries = []

    selected = sorted(challenge_entries, key=lambda item: (int(item.mtime or 0), item.path))[-MAX_BOOTSTRAP_CHALLENGES:]
    summary_candidates = []
    for directory in selected:
        try:
            async for entry in volume.iterdir.aio(directory.path, recursive=True):
                if _wanted_summary(entry.path):
                    summary_candidates.append(entry)
        except Exception:
            continue

    cutoff = int(time.time() - RETENTION_DAYS * 86400)
    audit_candidates = []
    try:
        async for entry in volume.iterdir.aio("/auditoria", recursive=True):
            if entry.path.endswith(".jsonl") and int(entry.mtime or 0) >= cutoff:
                audit_candidates.append(entry)
    except Exception:
        pass

    # Eventos primeiro: o painel fica útil antes de terminar o espelho visual.
    candidates = list({entry.path: entry for entry in [*audit_candidates, *summary_candidates]}.values())
    manifest = _compact_manifest(manifest, candidates)
    semaphore = asyncio.Semaphore(6)

    async def transfer(entry: Any) -> int:
        signature = f"{int(entry.mtime or 0)}:{int(entry.size or 0)}"
        if manifest.get(entry.path) == signature:
            return 0
        async with semaphore:
            target = target_root / _safe_relative(entry.path)
            await _download_file(volume, entry.path, target)
        manifest[entry.path] = signature
        return 1

    downloaded = sum(await asyncio.gather(*(transfer(entry) for entry in candidates)))

    videos_created, frames_removed = _build_missing_local_videos(target_root)
    _write_json_atomic(target_root / "manifest.json", manifest)
    return {
        "ok": True,
        "source": role,
        "downloaded": downloaded,
        "challenge_dirs_seen": len(challenge_entries),
        "summary_candidates": len(candidates),
        "videos_created": videos_created,
        "redundant_frames_removed": frames_removed,
    }


def _prune_local() -> int:
    cutoff = time.time() - RETENTION_DAYS * 86400
    removed = 0
    root = mirror_root()
    if not root.exists():
        return 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and path.name not in {"manifest.json", "sync-status.json"} and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


async def sync_once() -> dict[str, Any]:
    started = time.monotonic()
    results = []
    for role, prefix in (
        ("modal_primary", "MODAL_PRIMARY"),
        ("modal_fallback", "MODAL_FALLBACK"),
        ("modal_tertiary", "MODAL_TERTIARY"),
    ):
        token_id = os.getenv(f"{prefix}_TOKEN_ID", "").strip()
        token_secret = os.getenv(f"{prefix}_TOKEN_SECRET", "").strip()
        if not token_id or not token_secret:
            results.append({"ok": False, "source": role, "error": "credentials_not_configured"})
            continue
        try:
            results.append(
                await asyncio.wait_for(
                    _sync_account(role, token_id, token_secret),
                    timeout=SYNC_ACCOUNT_TIMEOUT_SECONDS,
                )
            )
        except asyncio.TimeoutError:
            results.append({"ok": False, "source": role, "error": "sync_timeout"})
        except Exception as exc:
            results.append({"ok": False, "source": role, "error": type(exc).__name__})
    status = {
        "ok": any(item.get("ok") for item in results),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "removed": _prune_local(),
        "sources": results,
    }
    _write_json_atomic(mirror_root() / "sync-status.json", status)
    return status


def _sync_worker() -> None:
    while True:
        try:
            asyncio.run(sync_once())
        except Exception as exc:
            _write_json_atomic(mirror_root() / "sync-status.json", {
                "ok": False,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "error": type(exc).__name__,
            })
        time.sleep(SYNC_INTERVAL_SECONDS)


def start_solver_audit_sync() -> None:
    global _THREAD_STARTED
    with _THREAD_LOCK:
        if _THREAD_STARTED:
            return
        _THREAD_STARTED = True
        threading.Thread(target=_sync_worker, name="portal-solver-audit", daemon=True).start()


def _read_events(path: Path) -> list[dict[str, Any]]:
    events = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                events.append(value)
    except OSError:
        pass
    return events


def _artifact_list(root: Path, request_id: str) -> list[dict[str, str]]:
    challenge_root = root / "desafios" / "unificados"
    if not challenge_root.is_dir():
        return []
    result = []
    for folder in challenge_root.iterdir():
        if not folder.is_dir() or request_id not in folder.name:
            continue
        for path in folder.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SAFE_MEDIA_SUFFIXES:
                continue
            if path.name not in SUMMARY_NAMES:
                continue
            result.append({
                "name": path.name,
                "path": path.relative_to(root).as_posix(),
                "kind": "video" if path.suffix.lower() == ".mp4" else "image" if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} else "json",
            })
    return result


def _summarize(source: str, root: Path, path: Path) -> dict[str, Any] | None:
    events = _read_events(path)
    if not events:
        return None
    first, last = events[0], events[-1]
    request_id = str(first.get("request_id") or path.stem)
    attempts = [event for event in events if event.get("event") == "provider_attempt"]
    successes = [event for event in events if event.get("event") == "provider_success"]
    failures = [event for event in events if event.get("event") == "provider_failure"]
    finished = next((event for event in reversed(events) if event.get("event") == "solve_finished"), None)
    clicks = [event for event in events if str(event.get("event", "")).startswith("click_")]
    refreshes = [event for event in events if event.get("event") == "challenge_refreshed"]
    return {
        "source": source,
        "request_id": request_id,
        "started_at": first.get("timestamp") or first.get("at"),
        "last_at": last.get("timestamp") or last.get("at"),
        "duration_seconds": last.get("elapsed_seconds"),
        "success": None if finished is None else bool(finished.get("success")),
        "reason": None if finished is None else finished.get("reason"),
        "location": last.get("location") or first.get("location") or source,
        "max_browsers": max((int(event.get("max_browsers") or 0) for event in events), default=0),
        "peak_active_browsers": max((int(event.get("active_browsers") or 0) for event in events), default=0),
        "route_attempts": [event.get("route") for event in attempts],
        "route_successes": [event.get("route") for event in successes],
        "provider_failures": len(failures),
        "unusual_traffic": any(bool(event.get("unusual_traffic")) for event in failures),
        "clicks": clicks,
        "refreshes": refreshes,
        "timeline": events[-120:],
        "artifacts": _artifact_list(root, request_id),
    }


def list_solver_audits(limit: int = 40) -> dict[str, Any]:
    rows = []
    for source, root in source_roots().items():
        audit_root = root / "auditoria"
        if not audit_root.is_dir():
            continue
        files = sorted(audit_root.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)[:max(limit, 20)]
        for path in files:
            summary = _summarize(source, root, path)
            if summary:
                rows.append(summary)
    rows.sort(key=lambda item: str(item.get("last_at") or ""), reverse=True)
    try:
        sync_status = json.loads((mirror_root() / "sync-status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sync_status = {"ok": False, "error": "sync_not_started"}
    return {"ok": True, "sync": sync_status, "audits": rows[:max(1, min(limit, 200))]}


def resolve_audit_file(source: str, relative_path: str) -> Path:
    roots = source_roots()
    root = roots.get(source)
    if root is None:
        raise ValueError("invalid_source")
    target = (root / _safe_relative(relative_path)).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError("invalid_path")
    if target.suffix.lower() not in SAFE_MEDIA_SUFFIXES or not target.is_file():
        raise FileNotFoundError(relative_path)
    return target
