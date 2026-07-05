#!/usr/bin/env python3
"""Coletor leve de infraestrutura para o painel master da Prumo."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


OUTPUT_ROOT = Path(os.getenv("PRUMO_MONITOR_ROOT", "/opt/prumo/data/_monitor"))
DB_FILE = OUTPUT_ROOT / "metrics.sqlite3"
LATEST_FILE = OUTPUT_ROOT / "latest.json"
API_URL = os.getenv("PRUMO_API_URL", "http://127.0.0.1:8000").rstrip("/")
INTERNAL_SECRET = os.getenv("ISS_INTERNAL_SECRET", "").strip()
BROWSERLESS_PRESSURE_URL = os.getenv("BROWSERLESS_PRESSURE_URL", "").strip()
COLLECT_INTERVAL = max(5, int(os.getenv("MONITOR_COLLECT_INTERVAL", "10")))
PERSIST_INTERVAL = max(10, int(os.getenv("MONITOR_PERSIST_INTERVAL", "30")))
RETENTION_SECONDS = 5 * 24 * 60 * 60
CONTAINERS = tuple(
    item.strip()
    for item in os.getenv("PRUMO_MONITOR_CONTAINERS", "prumo-api").split(",")
    if item.strip()
)

_last_cpu: Optional[Tuple[int, int]] = None


def run(command: list[str], timeout: int = 8) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Comando falhou.")
    return completed.stdout


def read_json_url(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_percent(value: Any) -> float:
    try:
        return float(str(value or "0").replace("%", "").strip())
    except ValueError:
        return 0.0


def parse_bytes(value: Any) -> int:
    raw = str(value or "0").strip().split(" / ", 1)[0]
    units = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3}
    number = "".join(char for char in raw if char.isdigit() or char == ".")
    unit = "".join(char for char in raw if char.isalpha()).upper()
    try:
        return int(float(number or "0") * units.get(unit, 1))
    except ValueError:
        return 0


def host_cpu_percent() -> float:
    global _last_cpu
    with open("/proc/stat", "r", encoding="utf-8") as handle:
        fields = [int(item) for item in handle.readline().split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    total = sum(fields)
    previous = _last_cpu
    _last_cpu = (idle, total)
    if not previous:
        return 0.0
    idle_delta = idle - previous[0]
    total_delta = total - previous[1]
    return round(100.0 * (1.0 - idle_delta / total_delta), 2) if total_delta > 0 else 0.0


def host_metrics() -> Dict[str, Any]:
    mem = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as handle:
        for line in handle:
            key, value = line.split(":", 1)
            mem[key] = int(value.strip().split()[0]) * 1024
    memory_total = int(mem.get("MemTotal", 0))
    memory_available = int(mem.get("MemAvailable", 0))
    disk = shutil.disk_usage("/")
    with open("/proc/uptime", "r", encoding="utf-8") as handle:
        uptime_seconds = int(float(handle.read().split()[0]))
    load = os.getloadavg()
    return {
        "cpu_percent": host_cpu_percent(),
        "memory_percent": round(100 * (memory_total - memory_available) / memory_total, 2) if memory_total else 0,
        "memory_used_bytes": memory_total - memory_available,
        "memory_total_bytes": memory_total,
        "disk_percent": round(100 * disk.used / disk.total, 2) if disk.total else 0,
        "disk_used_bytes": disk.used,
        "disk_total_bytes": disk.total,
        "load_1m": load[0],
        "load_5m": load[1],
        "load_15m": load[2],
        "uptime_seconds": uptime_seconds,
    }


def docker_metrics() -> Dict[str, Any]:
    stats: Dict[str, Any] = {}
    try:
        rows = run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *CONTAINERS]).splitlines()
        for row in rows:
            data = json.loads(row)
            name = str(data.get("Name") or "")
            stats[name] = {
                "cpu_percent": parse_percent(data.get("CPUPerc")),
                "memory_percent": parse_percent(data.get("MemPerc")),
                "memory_used_bytes": parse_bytes(data.get("MemUsage")),
                "network_io": data.get("NetIO") or "",
                "block_io": data.get("BlockIO") or "",
                "pids": int(data.get("PIDs") or 0),
            }
    except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        pass

    try:
        inspections = json.loads(run(["docker", "inspect", *CONTAINERS]))
        for item in inspections:
            name = str(item.get("Name") or "").lstrip("/")
            state = item.get("State") or {}
            stats.setdefault(name, {}).update({
                "status": state.get("Status") or "unknown",
                "running": bool(state.get("Running")),
                "started_at": state.get("StartedAt"),
                "finished_at": state.get("FinishedAt"),
                "exit_code": int(state.get("ExitCode") or 0),
                "oom_killed": bool(state.get("OOMKilled")),
                "error": state.get("Error") or "",
                "health": (state.get("Health") or {}).get("Status") or "",
                "restart_count": int(item.get("RestartCount") or 0),
                "image": item.get("Config", {}).get("Image") or "",
            })
    except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError):
        pass

    for name in CONTAINERS:
        stats.setdefault(name, {"status": "indisponível", "running": False})
    return stats


def runtime_metrics() -> Dict[str, Any]:
    if not INTERNAL_SECRET:
        return {"error": "ISS_INTERNAL_SECRET não configurado no agente."}
    try:
        return read_json_url(
            f"{API_URL}/api/internal/runtime-metrics",
            {"X-Internal-Secret": INTERNAL_SECRET},
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"error": str(exc)}


def browserless_pressure() -> Optional[Dict[str, Any]]:
    if not BROWSERLESS_PRESSURE_URL:
        return None
    try:
        return read_json_url(BROWSERLESS_PRESSURE_URL)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def sanitize_event(line: str) -> str:
    text = re.sub(r"([?&]token=)[^&\s]+", r"\1<masked>", str(line or ""), flags=re.IGNORECASE)
    text = re.sub(r"(\btoken\s*=\s*)[^&\s]+", r"\1<masked>", text, flags=re.IGNORECASE)
    text = re.sub(r"(authorization:\s*)(\S+)", r"\1<masked>", text, flags=re.IGNORECASE)
    return text.strip()[-700:]


def log_errors() -> Dict[str, Any]:
    text = ""
    for container in CONTAINERS:
        try:
            completed = subprocess.run(
                ["docker", "logs", "--since", "5m", "--tail", "2500", container],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            text += completed.stdout + completed.stderr
        except subprocess.SubprocessError:
            continue
    try:
        text += run(["journalctl", "-k", "--since", "5 minutes ago", "--no-pager", "-n", "500"], timeout=10)
    except (RuntimeError, subprocess.SubprocessError):
        pass

    lower = text.lower()
    flow = text.count("[ITEM_ERROR]")
    queue = text.count("[QUEUE_WORKER_FAILED]") + text.count("[queue] worker=")
    browser = lower.count("falha ao criar browser/context") + lower.count("429 too many requests")
    oom = lower.count("out of memory") + lower.count("oomkilled") + lower.count("oom-kill")
    killed = lower.count("killed process") + lower.count("sigkill")
    timeout = lower.count("timeout") + lower.count("timed out") + lower.count("tempo excedido")
    alert_terms = (
        "[item_error]",
        "[queue_worker_failed]",
        "429 too many requests",
        "out of memory",
        "oomkilled",
        "oom-kill",
        "killed process",
        "sigkill",
        "no space left",
        "traceback",
        "fatal",
    )
    recent_events = []
    for line in text.splitlines():
        if any(term in line.lower() for term in alert_terms):
            recent_events.append(sanitize_event(line))
    return {
        "flow": flow,
        "queue": queue,
        "browser_connect": browser,
        "oom": oom,
        "killed": killed,
        "timeout": timeout,
        "total": flow + queue + browser + oom + killed + timeout,
        "recent_events": recent_events[-30:],
    }


def init_db() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_FILE)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS metrics (ts INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts)")
        conn.commit()


def save_latest(payload: Dict[str, Any]) -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = LATEST_FILE.with_suffix(".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(temporary, LATEST_FILE)


def persist(payload: Dict[str, Any]) -> None:
    with closing(sqlite3.connect(DB_FILE)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO metrics (ts, payload) VALUES (?, ?)",
            (int(payload["ts"]), json.dumps(payload, ensure_ascii=False)),
        )
        conn.execute("DELETE FROM metrics WHERE ts < ?", (int(time.time()) - RETENTION_SECONDS,))
        conn.commit()


def collect() -> Dict[str, Any]:
    runtime = runtime_metrics()
    return {
        "ts": int(time.time()),
        "host": host_metrics(),
        "containers": docker_metrics(),
        "runtime": runtime,
        "errors": log_errors(),
        "browserless_pressure": browserless_pressure(),
    }


def sample_severity(payload: Dict[str, Any]) -> tuple:
    host = payload.get("host") or {}
    errors = payload.get("errors") or {}
    pressure = payload.get("browserless_pressure") or {}
    return (
        int(errors.get("oom") or 0),
        int(errors.get("killed") or 0),
        int(errors.get("total") or 0),
        float(pressure.get("queued") or 0),
        float(host.get("memory_percent") or 0),
        float(host.get("cpu_percent") or 0),
    )


def main() -> None:
    init_db()
    last_persist = 0
    pending_peak = None
    while True:
        started = time.time()
        try:
            payload = collect()
            save_latest(payload)
            if pending_peak is None or sample_severity(payload) > sample_severity(pending_peak):
                pending_peak = payload
            if started - last_persist >= PERSIST_INTERVAL:
                persist(pending_peak or payload)
                pending_peak = None
                last_persist = started
        except Exception as exc:  # noqa: BLE001 - o serviço deve continuar coletando
            save_latest({"ts": int(time.time()), "error": f"{type(exc).__name__}: {exc}"})
        time.sleep(max(1, COLLECT_INTERVAL - int(time.time() - started)))


if __name__ == "__main__":
    main()
