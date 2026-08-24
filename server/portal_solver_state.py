"""Estado compartilhado do circuito do resolvedor do Portal Nacional.

O scheduler da API e cada subprocesso de download precisam enxergar a mesma
indisponibilidade. O arquivo e pequeno, nao contem payload de captcha nem
credenciais e e protegido por lock de sistema operacional.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


_THREAD_LOCK = threading.RLock()
_OUTAGE_DELAYS = (30, 60, 120, 300)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Lock entre threads e processos, compativel com Linux e Windows."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK, path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_unlocked(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {
        "schema": 1,
        "state": "healthy",
        "failure_streak": 0,
        "updated_at": _iso(_now()),
    }


def _write_unlocked(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def snapshot(path: Path) -> dict[str, Any]:
    with _file_lock(path.with_suffix(path.suffix + ".lock")):
        return dict(_load_unlocked(path))


def blocked_seconds(path: Path, *, now: datetime | None = None) -> float:
    state = snapshot(path)
    blocked_until = _parse(state.get("blocked_until"))
    if blocked_until is None:
        return 0.0
    return max(0.0, (blocked_until - (now or _now())).total_seconds())


def probe_owned_by_other(
    path: Path,
    owner: str,
    *,
    now: datetime | None = None,
) -> bool:
    state = snapshot(path)
    lease_owner = str(state.get("probe_owner") or "")
    lease_until = _parse(state.get("probe_expires_at"))
    current = now or _now()
    return bool(lease_owner and lease_owner != owner and lease_until and lease_until > current)


def acquire_probe(
    path: Path,
    owner: str,
    *,
    lease_seconds: int = 900,
    now: datetime | None = None,
) -> bool:
    current = now or _now()
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _file_lock(lock_path):
        state = _load_unlocked(path)
        lease_owner = str(state.get("probe_owner") or "")
        lease_until = _parse(state.get("probe_expires_at"))
        blocked_until = _parse(state.get("blocked_until"))
        if lease_owner and lease_owner != owner and lease_until and lease_until > current:
            return False
        if blocked_until and blocked_until > current and lease_owner != owner:
            return False
        state.update(
            {
                "schema": 1,
                "state": "probing",
                "probe_owner": owner[:96],
                "probe_expires_at": _iso(
                    current + timedelta(seconds=max(30, int(lease_seconds)))
                ),
                "updated_at": _iso(current),
            }
        )
        _write_unlocked(path, state)
        return True


def mark_outage(
    path: Path,
    owner: str,
    reason_class: str,
    *,
    minimum_delay: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _now()
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _file_lock(lock_path):
        state = _load_unlocked(path)
        streak = max(0, int(state.get("failure_streak") or 0)) + 1
        delay = max(
            int(minimum_delay or 0),
            _OUTAGE_DELAYS[min(streak - 1, len(_OUTAGE_DELAYS) - 1)],
        )
        state.update(
            {
                "schema": 1,
                "state": "blocked",
                "failure_streak": streak,
                "reason_class": str(reason_class or "solver_outage")[:80],
                "blocked_until": _iso(current + timedelta(seconds=delay)),
                "probe_owner": owner[:96],
                "probe_expires_at": _iso(current + timedelta(seconds=max(delay + 60, 180))),
                "last_failure_at": _iso(current),
                "updated_at": _iso(current),
            }
        )
        _write_unlocked(path, state)
        return dict(state)


def mark_success(
    path: Path,
    owner: str,
    *,
    provider: str | None = None,
    duration_seconds: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _now()
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _file_lock(lock_path):
        state = _load_unlocked(path)
        state.update(
            {
                "schema": 1,
                "state": "healthy",
                "failure_streak": 0,
                "blocked_until": None,
                "probe_owner": None,
                "probe_expires_at": None,
                "reason_class": None,
                "last_success_at": _iso(current),
                "last_success_owner": owner[:96],
                "updated_at": _iso(current),
            }
        )
        if provider:
            state["last_success_provider"] = str(provider)[:80]
        if duration_seconds is not None:
            state["last_success_duration_seconds"] = round(
                max(0.0, float(duration_seconds)), 3
            )
        _write_unlocked(path, state)
        return dict(state)


def release_probe(path: Path, owner: str, *, now: datetime | None = None) -> None:
    current = now or _now()
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _file_lock(lock_path):
        state = _load_unlocked(path)
        if str(state.get("probe_owner") or "") != owner:
            return
        state["probe_owner"] = None
        state["probe_expires_at"] = None
        if state.get("state") == "probing":
            state["state"] = "healthy"
        state["updated_at"] = _iso(current)
        _write_unlocked(path, state)
