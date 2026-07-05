import json
import logging
import os
import random
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

import flow_core as _fc

load_dotenv()

APP_DIR = Path(__file__).resolve().parent

WORKER_PUBLIC_URL = os.getenv(
    "WORKER_PUBLIC_URL",
    "https://morning-credit-8a59.prumo-sistema.workers.dev",
).rstrip("/")

ISS_INTERNAL_SECRET = os.getenv("ISS_INTERNAL_SECRET", "").strip()
ALLOW_DIRECT_LOCAL = os.getenv("ISS_ALLOW_DIRECT_LOCAL", "false").lower() in {"1", "true", "yes", "sim"}

OUTPUT_ROOT = os.getenv("ISS_OUTPUT_ROOT", str(APP_DIR / "output"))
DATA_ROOT = os.getenv("ISS_DATA_ROOT", str(Path(OUTPUT_ROOT) / "_api_data"))
DB_FILE = os.path.join(DATA_ROOT, "iss_automacao.db")

MAX_DATASETS = int(os.getenv("MAX_DATASETS", "0"))
MAX_RUNS_PER_MEMBER = int(os.getenv("MAX_RUNS_PER_MEMBER", "8"))
RUN_RETENTION_DAYS = int(os.getenv("RUN_RETENTION_DAYS", "30"))
AUTO_RETRY_HARD_MAX_ATTEMPTS = 3


def clamp_auto_retry_max_attempts(value: Any = None) -> int:
    try:
        parsed = int(value if value is not None else os.getenv("AUTO_RETRY_MAX_ATTEMPTS", "3"))
    except Exception:
        parsed = 3
    return max(1, min(parsed, AUTO_RETRY_HARD_MAX_ATTEMPTS))


AUTO_RETRY_MAX_ATTEMPTS = clamp_auto_retry_max_attempts()


def _env_int(name: str, default: int, *, min_value: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    return max(min_value, value)


def _browser_pool_capacity_from_env(default: int = 15) -> int:
    pool = os.getenv("BROWSER_CDP_POOL", "").strip()
    if not pool:
        return default

    total = 0
    for raw_entry in pool.split(";;"):
        entry = raw_entry.strip()
        if not entry:
            continue

        parts = [part.strip() for part in entry.split("|", 2)]
        if len(parts) == 3:
            _, capacity_raw, _ = parts
        elif len(parts) == 2:
            capacity_raw, _ = parts
        else:
            capacity_raw = "1"

        try:
            total += max(1, int(capacity_raw))
        except Exception:
            total += 1

    return total or default


BASE_BROWSER_SLOTS = _env_int("BASE_BROWSER_SLOTS", 15, min_value=0)
MAX_BROWSERS = int(os.getenv("MAX_BROWSERS", str(_browser_pool_capacity_from_env(BASE_BROWSER_SLOTS))))
MAX_BROWSER_LIMIT = _env_int("MAX_BROWSER_LIMIT", 96, min_value=1)
MAX_BROWSERS = max(1, min(MAX_BROWSERS, MAX_BROWSER_LIMIT))
BROWSER_TURBO_EXTRA = max(0, MAX_BROWSERS - BASE_BROWSER_SLOTS)
BROWSER_POOL_CONFIGURED = bool(os.getenv("BROWSER_CDP_POOL", "").strip())

HEADLESS = os.getenv("ISS_HEADLESS", "true").lower() in {"1", "true", "yes", "sim"}

FLOW_ORDER = ["certidao", "dam", "escrituracao", "notas"]
FLOW_EXECUTION_ORDER = ["certidao", "escrituracao", "dam", "notas"]
FLOW_LABELS = {
    "certidao": "Certidão",
    "dam": "DAM",
    "escrituracao": "Escrituração",
    "notas": "Notas",
}

ACTIVE_STATUSES = {"queued", "running"}
FINAL_STATUSES = {"finished", "failed", "cancelled", "interrompida"}
ITEM_FINAL_STATUSES = {"ok", "erro", "failed", "cancelled", "interrompida"}

Path(OUTPUT_ROOT).mkdir(parents=True, exist_ok=True)
Path(DATA_ROOT).mkdir(parents=True, exist_ok=True)

_fc.BASE_DIR = OUTPUT_ROOT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s :: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("iss.api")

DB_LOCK = threading.RLock()


def now_ms() -> int:
    return int(time.time() * 1000)


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def is_sqlite_lock_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg or "busy" in msg


def db_with_retry(fn, *, attempts: int = 8) -> Any:
    last: Optional[BaseException] = None

    for i in range(attempts):
        try:
            with DB_LOCK:
                return fn()
        except sqlite3.OperationalError as exc:
            last = exc
            if not is_sqlite_lock_error(exc):
                raise
            sleep_for = min(1.5, 0.05 * (2 ** i)) + random.random() * 0.05
            time.sleep(sleep_for)

    raise last or RuntimeError("Falha SQLite sem exceção capturada.")


def init_db() -> None:
    def _op() -> None:
        with closing(db_connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )

            conn.execute("CREATE INDEX IF NOT EXISTS idx_kv_updated_at ON kv(updated_at)")

    db_with_retry(_op)


def db_get_json(key: str, default: Any) -> Any:
    def _op() -> Any:
        with closing(db_connect()) as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()

        if not row:
            return default

        try:
            return json.loads(row["value"])
        except Exception:
            return default

    return db_with_retry(_op)


def db_set_json(key: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False)
    ts = now_ms()

    def _op() -> None:
        with closing(db_connect()) as conn:
            conn.execute(
                """
                INSERT INTO kv (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, payload, ts),
            )

    db_with_retry(_op)


init_db()
