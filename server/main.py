#!/usr/bin/env python3
"""
main.py

API local para executar fluxos ISS conectada ao Cloudflare Worker.
"""

import asyncio
import json
import os
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    Query,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from db import (
    ACTIVE_STATUSES,
    ALLOW_DIRECT_LOCAL,
    BASE_BROWSER_SLOTS,
    BROWSER_POOL_CONFIGURED,
    BROWSER_TURBO_EXTRA,
    DATA_ROOT,
    DB_FILE,
    FLOW_ORDER,
    MAX_BROWSERS,
    OUTPUT_ROOT,
    WORKER_PUBLIC_URL,
    clamp_auto_retry_max_attempts,
    db_connect,
)

from domain import (
    RUNS,
    RUN_LOCK,
    AccountPayload,
    DatasetPayload,
    RetryRequest,
    RunCreateRequest,
    StopFlowRequest,
    WorkerContext,
    account_usage,
    assert_no_active_run,
    assert_root_not_active,
    build_mes,
    build_state_sync,
    build_tasks_from_dataset,
    clean_alias,
    compact_account,
    compact_dataset_record,
    compact_root_run,
    create_cnpj_zip,
    create_root_zip,
    delete_run_folder,
    delete_run_from_memory_and_db,
    direct_local_context,
    ensure_member_runs_loaded,
    get_dataset_or_404,
    get_worker_context,
    hydrate_tasks_with_current_accounts,
    load_accounts_public,
    load_accounts_raw,
    load_datasets,
    local_run_key,
    logs_by_attempt_for_root,
    logs_by_attempt_for_root_filtered,
    model_to_dict,
    new_account_id,
    normalize_cnpj,
    read_xlsx_items_from_bytes,
    request_stop_for_attempt,
    root_has_active_attempt,
    root_id_of,
    runs_for_member,
    safe_path_inside,
    safe_slug,
    save_accounts,
    save_dataset_record,
    save_datasets,
    should_hide_run_file,
    task_key,
    unprotect_account_from_storage,
    validate_dataset_items,
    visible_root_runs,
    write_run_log,
    member_runs_root,
    owner_key,
    scope_id,
    is_valid_nonempty_file,
    now_ms,
)

from run_queue import (
    GLOBAL_QUEUE,
    active_jobs_for_company,
    active_jobs_for_scope,
    build_state,
    create_attempt_record,
    create_retry_attempt_for_root,
    queue_state_for_ctx,
    runtime_queue_metrics,
    startup_queue_workers,
)
from portal_nacional import router as portal_nacional_router


app = FastAPI(
    title="ISS Automação API",
    version="1.0.36",
    description="API Prumo conectada ao Worker, com ISS Fortaleza e Portal Nacional isolados por membro.",
)

app.include_router(portal_nacional_router)

MONITOR_ROOT = os.path.join(OUTPUT_ROOT, "_monitor")
MONITOR_DB_FILE = os.path.join(MONITOR_ROOT, "metrics.sqlite3")
MONITOR_LATEST_FILE = os.path.join(MONITOR_ROOT, "latest.json")



def paginate_list(items: List[Any], page: int, page_size: int) -> Dict[str, Any]:
    total = len(items)
    safe_page_size = max(1, min(page_size, 500))
    safe_page = max(1, page)
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size

    return {
        "items": items[start:end],
        "page": safe_page,
        "page_size": safe_page_size,
        "total": total,
        "total_pages": max(1, (total + safe_page_size - 1) // safe_page_size),
    }


def group_results_by_cnpj_for_response(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}

    for result in results:
        cnpj = str(result.get("cnpj") or "")
        key = normalize_cnpj(cnpj)

        if not key:
            key = cnpj

        group = groups.setdefault(
            key,
            {
                "cnpj": cnpj,
                "cnpj_digits": key,
                "nome_empresa": result.get("empresa") or result.get("nome_empresa") or "",
                "account_alias": result.get("account_alias") or "",
                "flows": {},
            },
        )

        group["nome_empresa"] = group.get("nome_empresa") or result.get("empresa") or result.get("nome_empresa") or ""
        group["account_alias"] = group.get("account_alias") or result.get("account_alias") or ""
        flow = result.get("flow_mode") or ""

        if flow:
            group["flows"][flow] = result

    return list(groups.values())


def _safe_json_loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return default


def _compact_admin_run(run: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": run.get("root_id") or run.get("run_id"),
        "attempt_run_id": run.get("run_id"),
        "attempt_number": run.get("attempt_number"),
        "attempt_type": run.get("attempt_type"),
        "status": run.get("status"),
        "dataset_alias": run.get("dataset_alias", ""),
        "mes": run.get("mes", ""),
        "total": run.get("total", 0),
        "ok": run.get("ok", 0),
        "erros": run.get("erros", 0),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
    }


def _folder_stats(path: str) -> Dict[str, Any]:
    if not os.path.isdir(path):
        return {
            "exists": False,
            "path": path,
            "files": 0,
            "folders": 0,
            "bytes": 0,
            "last_modified_at": None,
        }

    files = 0
    folders = 0
    total_bytes = 0
    last_modified_at = int(os.path.getmtime(path) * 1000)

    for root, dirnames, filenames in os.walk(path):
        folders += len(dirnames)
        for filename in filenames:
            full_path = os.path.join(root, filename)
            try:
                stat = os.stat(full_path)
            except OSError:
                continue
            files += 1
            total_bytes += int(stat.st_size)
            last_modified_at = max(last_modified_at, int(stat.st_mtime * 1000))

    return {
        "exists": True,
        "path": path,
        "files": files,
        "folders": folders,
        "bytes": total_bytes,
        "last_modified_at": last_modified_at,
    }


def _company_storage_summary(company_id: str, known_user_ids: List[str]) -> Dict[str, Any]:
    company_safe = safe_slug(company_id)
    company_dir = safe_path_inside(
        os.path.join(OUTPUT_ROOT, "empresas"),
        os.path.join(OUTPUT_ROOT, "empresas", company_safe),
    )
    collaborators_dir = os.path.join(company_dir, "colaboradores")
    actual_user_ids = []

    if os.path.isdir(collaborators_dir):
        actual_user_ids = sorted(
            item.name
            for item in Path(collaborators_dir).iterdir()
            if item.is_dir()
        )

    known = sorted(set(safe_slug(user_id) for user_id in known_user_ids if user_id))
    orphan_user_folders = sorted(set(actual_user_ids) - set(known))

    return {
        **_folder_stats(company_dir),
        "known_user_ids": known,
        "actual_user_ids": actual_user_ids,
        "orphan_user_folders": orphan_user_folders,
        "healthy": not orphan_user_folders,
    }


def build_company_admin_summary(company_id: str, *, include_account_secrets: bool = False) -> Dict[str, Any]:
    company_safe = safe_slug(company_id)
    prefix = f"empresa:{company_safe}:membro:"
    users: Dict[str, Dict[str, Any]] = {}

    try:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT key, value, updated_at FROM kv WHERE key LIKE ?",
                (prefix + "%",),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"Falha ao ler resumo administrativo: {exc}") from exc

    for row in rows:
        key = str(row["key"])
        rest = key[len(prefix):]
        if ":" not in rest:
            continue

        user_id, name = rest.split(":", 1)
        user = users.setdefault(
            user_id,
            {
                "user_id": user_id,
                "user_email": "",
                "accounts_count": 0,
                "datasets_count": 0,
                "runs_count": 0,
                "retry_count": 0,
                "latest_runs": [],
                "datasets": [],
                "accounts": [],
                "cnpjs_unique": [],
                "items_count": 0,
            },
        )

        payload = _safe_json_loads(row["value"], {})

        if name == "accounts":
            accounts = payload.get("accounts", []) if isinstance(payload, dict) else []
            if isinstance(accounts, list):
                visible_accounts = [
                    unprotect_account_from_storage(acc) if include_account_secrets else acc
                    for acc in accounts
                    if isinstance(acc, dict)
                ]
                user["accounts_count"] = len(accounts)
                user["accounts"] = [
                    {
                        "id": acc.get("id"),
                        "alias": acc.get("alias"),
                        "usuario": acc.get("usuario"),
                        "senha": acc.get("senha") if include_account_secrets else None,
                        "created_at": acc.get("created_at"),
                        "updated_at": acc.get("updated_at"),
                    }
                    for acc in visible_accounts
                ]

        elif name == "datasets":
            datasets = payload.get("datasets", []) if isinstance(payload, dict) else []
            if isinstance(datasets, list):
                user["datasets_count"] = len(datasets)
                user["datasets"] = []
                for ds in datasets:
                    if not isinstance(ds, dict):
                        continue
                    compact = compact_dataset_record(ds)
                    if include_account_secrets:
                        compact["items"] = ds.get("items", [])
                    user["datasets"].append(compact)
                cnpjs = set()
                items_count = 0
                for dataset in datasets:
                    if not isinstance(dataset, dict):
                        continue
                    for item in dataset.get("items", []) or []:
                        if not isinstance(item, dict):
                            continue
                        items_count += 1
                        cnpj = "".join(char for char in str(item.get("cnpj_digits") or item.get("cnpj") or "") if char.isdigit())
                        if cnpj:
                            cnpjs.add(cnpj.zfill(14))
                user["cnpjs_unique"] = sorted(cnpjs)
                user["items_count"] = items_count

        elif name == "runs_state":
            runs_raw = payload.get("runs", {}) if isinstance(payload, dict) else {}
            runs = list(runs_raw.values()) if isinstance(runs_raw, dict) else []
            visible_roots = []
            latest_attempts = []
            retries = 0

            for run in runs:
                if not isinstance(run, dict):
                    continue
                user["user_email"] = user["user_email"] or str(run.get("user_email") or "")
                latest_attempts.append(run)
                if run.get("attempt_type") in {"retry", "auto_retry"}:
                    retries += 1
                if run.get("visible", True) and (run.get("run_id") == (run.get("root_id") or run.get("run_id"))):
                    visible_roots.append(run)

            visible_roots.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)
            latest_attempts.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)
            user["runs_count"] = len(visible_roots)
            user["retry_count"] = retries
            user["latest_runs"] = [_compact_admin_run(run) for run in latest_attempts[:8]]

    for user_id, user in users.items():
        user["storage"] = _folder_stats(
            os.path.join(OUTPUT_ROOT, "empresas", company_safe, "colaboradores", safe_slug(user_id))
        )

    user_list = list(users.values())
    user_list.sort(key=lambda item: (str(item.get("user_email") or ""), str(item.get("user_id") or "")))

    latest_runs = []
    for user in user_list:
        for run in user.get("latest_runs", []):
            latest_runs.append({**run, "user_id": user["user_id"], "user_email": user.get("user_email", "")})
    latest_runs.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)

    company_cnpjs = sorted({
        cnpj
        for user in user_list
        for cnpj in user.get("cnpjs_unique", [])
    })
    storage = _company_storage_summary(company_safe, list(users.keys()))

    return {
        "company_id": company_safe,
        "users": user_list,
        "totals": {
            "users_with_data": len(user_list),
            "accounts": sum(int(user.get("accounts_count") or 0) for user in user_list),
            "datasets": sum(int(user.get("datasets_count") or 0) for user in user_list),
            "runs": sum(int(user.get("runs_count") or 0) for user in user_list),
            "retries": sum(int(user.get("retry_count") or 0) for user in user_list),
            "cnpjs_unique": len(company_cnpjs),
            "items": sum(int(user.get("items_count") or 0) for user in user_list),
        },
        "latest_runs": latest_runs[:12],
        "storage": storage,
    }

app.add_middleware(
    CORSMiddleware,
    allow_origins=[WORKER_PUBLIC_URL] if WORKER_PUBLIC_URL else [],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup() -> None:
    await startup_queue_workers()


@app.websocket("/ws")
async def websocket_state(websocket: WebSocket):
    if not ALLOW_DIRECT_LOCAL:
        await websocket.close(code=1008)
        return

    client_host = websocket.client.host if websocket.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    ctx = direct_local_context()
    ensure_member_runs_loaded(ctx)
    last = ""

    try:
        while True:
            payload = json.dumps(await build_state(ctx), ensure_ascii=False)

            if payload != last:
                await websocket.send_text(payload)
                last = payload

            await asyncio.sleep(0.7)

    except WebSocketDisconnect:
        return


@app.get("/")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": "Prumo API",
        "version": "1.0.36",
        "worker_public_url": WORKER_PUBLIC_URL,
        "allow_direct_local": ALLOW_DIRECT_LOCAL,
        "max_browsers": MAX_BROWSERS,
        "base_browsers": BASE_BROWSER_SLOTS,
        "browser_turbo_extra": BROWSER_TURBO_EXTRA,
        "browser_pool_configured": BROWSER_POOL_CONFIGURED,
        "queue": "global_fair_round_robin_by_member",
        "storage_scope": "member",
    }


def _monitor_sample_severity(sample: Dict[str, Any]) -> float:
    host = sample.get("host") or {}
    runtime = sample.get("runtime") or {}
    queue = runtime.get("queue") or {}
    errors = sample.get("errors") or {}
    containers = sample.get("containers") or {}
    oom_penalty = 1000 if any(bool((item or {}).get("oom_killed")) for item in containers.values()) else 0
    return max(
        float(host.get("cpu_percent") or 0),
        float(host.get("memory_percent") or 0),
        float(queue.get("workers_busy") or 0) * 10,
        float(queue.get("pending_groups") or 0) * 10,
        float(errors.get("total") or 0) * 20,
    ) + oom_penalty


def _downsample_monitor_metrics(samples: List[Dict[str, Any]], max_points: int = 900) -> List[Dict[str, Any]]:
    if len(samples) <= max_points:
        return samples
    bucket_size = max(1, (len(samples) + max_points - 1) // max_points)
    output = []
    for start in range(0, len(samples), bucket_size):
        bucket = samples[start:start + bucket_size]
        output.append(max(bucket, key=_monitor_sample_severity))
    if samples and output[-1].get("ts") != samples[-1].get("ts"):
        output.append(samples[-1])
    return output


def _load_monitor_metrics(range_key: str) -> Dict[str, Any]:
    latest: Dict[str, Any] = {}
    samples: List[Dict[str, Any]] = []
    range_seconds = {
        "1h": 60 * 60,
        "6h": 6 * 60 * 60,
        "24h": 24 * 60 * 60,
        "5d": 5 * 24 * 60 * 60,
    }.get(range_key, 6 * 60 * 60)

    try:
        with open(MONITOR_LATEST_FILE, "r", encoding="utf-8") as handle:
            latest = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        latest = {}

    if os.path.isfile(MONITOR_DB_FILE):
        cutoff = int(time.time()) - range_seconds
        try:
            with closing(sqlite3.connect(MONITOR_DB_FILE)) as conn:
                rows = conn.execute(
                    "SELECT ts, payload FROM metrics WHERE ts >= ? ORDER BY ts ASC LIMIT 20000",
                    (cutoff,),
                ).fetchall()
            for ts, payload in rows:
                item = _safe_json_loads(payload, {})
                if isinstance(item, dict):
                    samples.append({"ts": int(ts), **item})
        except sqlite3.Error:
            samples = []

    raw_sample_count = len(samples)
    samples = _downsample_monitor_metrics(samples)
    latest_ts = int(latest.get("ts") or 0)
    return {
        "latest": latest,
        "samples": samples,
        "range": range_key,
        "raw_sample_count": raw_sample_count,
        "resolution_seconds": 30,
        "agent": {
            "available": bool(latest),
            "stale": not latest_ts or int(time.time()) - latest_ts > 45,
            "last_seen_at": latest_ts or None,
        },
    }


def _env_decimal(name: str, default: str = "0") -> Decimal:
    raw = str(os.getenv(name, default) or default).strip().replace(",", ".")
    try:
        return Decimal(raw)
    except Exception:
        return Decimal(default)


def _modal_billing_snapshot() -> Dict[str, Any]:
    monthly_credit = _env_decimal("MODAL_MONTHLY_CREDIT_USD", "30.00")
    target_app = str(os.getenv("MODAL_BILLING_APP_NAME", "prumo-browserless") or "").strip()
    now_dt = datetime.now(timezone.utc)
    month_start = datetime(now_dt.year, now_dt.month, 1, tzinfo=timezone.utc)

    try:
        from modal import billing as modal_billing

        rows = modal_billing.workspace_billing_report(
            start=month_start,
            end=now_dt,
            resolution="d",
        )
        normalized_rows = []
        total_cost = Decimal("0")
        target_cost = Decimal("0")

        for row in rows:
            cost = Decimal(str(row.get("cost", "0")))
            description = str(row.get("description") or "")
            total_cost += cost
            if not target_app or description == target_app:
                target_cost += cost
            interval = row.get("interval_start")
            normalized_rows.append({
                "object_id": row.get("object_id"),
                "description": description,
                "environment": row.get("environment_name") or row.get("environment"),
                "interval_start": interval.isoformat() if hasattr(interval, "isoformat") else str(interval or ""),
                "cost_usd": float(cost),
            })

        remaining = monthly_credit - total_cost
        if remaining < Decimal("0"):
            remaining = Decimal("0")

        return {
            "ok": True,
            "source": "modal.billing.workspace_billing_report",
            "workspace": os.getenv("MODAL_WORKSPACE", ""),
            "target_app": target_app,
            "period_start": month_start.isoformat(),
            "period_end": now_dt.isoformat(),
            "monthly_credit_usd": float(monthly_credit),
            "month_to_date_cost_usd": float(total_cost),
            "target_app_cost_usd": float(target_cost),
            "credits_remaining_usd": float(remaining),
            "rows": normalized_rows,
        }
    except Exception as exc:
        manual_remaining = os.getenv("MODAL_CREDITS_REMAINING_USD", "").strip()
        if manual_remaining:
            remaining = _env_decimal("MODAL_CREDITS_REMAINING_USD", "0")
            return {
                "ok": True,
                "source": "manual_env_fallback",
                "error": f"{type(exc).__name__}: {exc}",
                "monthly_credit_usd": float(monthly_credit),
                "month_to_date_cost_usd": None,
                "target_app_cost_usd": None,
                "credits_remaining_usd": float(remaining),
                "rows": [],
            }
        return {
            "ok": False,
            "source": "modal.billing.workspace_billing_report",
            "error": f"{type(exc).__name__}: {exc}",
            "monthly_credit_usd": float(monthly_credit),
            "rows": [],
        }


def _require_internal_secret(x_internal_secret: str) -> None:
    from db import ISS_INTERNAL_SECRET

    if not ISS_INTERNAL_SECRET or x_internal_secret != ISS_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Segredo interno inválido.")


@app.get("/api/state")
async def state(ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    return await build_state(ctx)


@app.get("/api/config")
async def config(ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    return build_state_sync(ctx)["config"]


@app.get("/api/admin/company-summary")
async def admin_company_summary(
    company_id: str = Query(default=""),
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    target_company_id = safe_slug(company_id or ctx.company_id)

    if ctx.user_role != "master" and target_company_id != safe_slug(ctx.company_id):
        raise HTTPException(status_code=403, detail="Permissão negada.")

    summary = await asyncio.to_thread(
        build_company_admin_summary,
        target_company_id,
        include_account_secrets=ctx.user_role == "master",
    )
    return {"ok": True, "summary": summary}


def _member_context(company_id: str, user_id: str, user_email: str = "") -> WorkerContext:
    return WorkerContext(
        company_id=safe_slug(company_id),
        company_name=safe_slug(company_id),
        user_id=safe_slug(user_id),
        user_email=str(user_email or user_id),
        user_role="member",
        via_worker=True,
    )


def _loaded_company_contexts(company_id: str) -> List[WorkerContext]:
    company_safe = safe_slug(company_id)
    contexts: Dict[str, WorkerContext] = {}

    for run in RUNS.values():
        if safe_slug(run.get("company_id", "")) != company_safe:
            continue
        user_id = safe_slug(run.get("user_id", ""))
        if not user_id:
            continue
        contexts[user_id] = _member_context(company_safe, user_id, str(run.get("user_email") or user_id))

    return list(contexts.values())


async def _request_stop_for_ctx(ctx: WorkerContext) -> List[Dict[str, Any]]:
    attempts = [run for run in runs_for_member(ctx) if run.get("status") in ACTIVE_STATUSES]
    output = []

    for attempt in attempts:
        run_key = local_run_key(ctx, attempt.get("run_id", ""))
        run_log_file = attempt.get("run_log_file") or os.path.join(attempt.get("run_dir", ""), "logs.txt")
        write_run_log(run_log_file, "[STOP_REQUESTED] Parada solicitada para sincronizar exclusão administrativa.")
        output.append(
            await request_stop_for_attempt(
                ctx,
                run_key,
                reason="Fluxo interrompido para sincronizar exclusão administrativa.",
                code="ADMIN_DELETE",
            )
        )

    return output


async def _delete_member_data(company_id: str, user_id: str, user_email: str = "") -> Dict[str, Any]:
    ctx = _member_context(company_id, user_id, user_email)
    await _request_stop_for_ctx(ctx)
    removed_queued = await GLOBAL_QUEUE.remove_owner(owner_key(ctx))

    if active_jobs_for_scope(scope_id(ctx)):
        return {
            "ok": True,
            "completed": False,
            "status": "stopping",
            "removed_queued_groups": removed_queued,
        }

    async with RUN_LOCK:
        removed_runs = 0
        for run_key, run in list(RUNS.items()):
            if run.get("scope_id") == scope_id(ctx):
                RUNS.pop(run_key, None)
                removed_runs += 1

    prefix = f"empresa:{safe_slug(company_id)}:membro:{safe_slug(user_id)}:%"

    def _delete() -> Dict[str, Any]:
        conn = db_connect()
        try:
            cur = conn.execute("DELETE FROM kv WHERE key LIKE ?", (prefix,))
            deleted_keys = cur.rowcount if cur.rowcount is not None else 0
            conn.commit()
        finally:
            conn.close()

        member_dir = safe_path_inside(
            os.path.join(OUTPUT_ROOT, "empresas", safe_slug(company_id), "colaboradores"),
            os.path.join(OUTPUT_ROOT, "empresas", safe_slug(company_id), "colaboradores", safe_slug(user_id)),
        )
        removed_folder = False
        if os.path.isdir(member_dir):
            shutil.rmtree(member_dir)
            removed_folder = True

        return {"deleted_keys": deleted_keys, "removed_folder": removed_folder}

    deleted = await asyncio.to_thread(_delete)
    return {
        "ok": True,
        "completed": True,
        "status": "completed",
        "removed_runs": removed_runs,
        "removed_queued_groups": removed_queued,
        **deleted,
    }


async def _delete_company_data(company_id: str) -> Dict[str, Any]:
    company_safe = safe_slug(company_id)
    for member_ctx in _loaded_company_contexts(company_safe):
        await _request_stop_for_ctx(member_ctx)

    removed_queued = await GLOBAL_QUEUE.remove_company(company_safe)

    if active_jobs_for_company(company_safe):
        return {
            "ok": True,
            "completed": False,
            "status": "stopping",
            "removed_queued_groups": removed_queued,
        }

    async with RUN_LOCK:
        removed_runs = 0
        for run_key, run in list(RUNS.items()):
            if safe_slug(run.get("company_id", "")) == company_safe:
                RUNS.pop(run_key, None)
                removed_runs += 1

    prefix = f"empresa:{company_safe}:membro:%"

    def _delete() -> Dict[str, Any]:
        conn = db_connect()
        try:
            cur = conn.execute("DELETE FROM kv WHERE key LIKE ?", (prefix,))
            deleted_keys = cur.rowcount if cur.rowcount is not None else 0
            conn.commit()
        finally:
            conn.close()

        company_dir = safe_path_inside(
            os.path.join(OUTPUT_ROOT, "empresas"),
            os.path.join(OUTPUT_ROOT, "empresas", company_safe),
        )
        removed_folder = False
        if os.path.isdir(company_dir):
            shutil.rmtree(company_dir)
            removed_folder = True

        return {"deleted_keys": deleted_keys, "removed_folder": removed_folder}

    deleted = await asyncio.to_thread(_delete)
    return {
        "ok": True,
        "completed": True,
        "status": "completed",
        "removed_runs": removed_runs,
        "removed_queued_groups": removed_queued,
        **deleted,
    }


@app.get("/api/admin/company-detail")
async def admin_company_detail(
    company_id: str = Query(default=""),
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    target_company_id = safe_slug(company_id or ctx.company_id)
    if ctx.user_role != "master" and target_company_id != safe_slug(ctx.company_id):
        raise HTTPException(status_code=403, detail="Permissão negada.")
    detail = await asyncio.to_thread(
        build_company_admin_summary,
        target_company_id,
        include_account_secrets=ctx.user_role == "master",
    )
    return {"ok": True, "detail": detail}


@app.post("/api/admin/member-data/delete")
async def admin_delete_member_data(
    company_id: str = Query(default=""),
    user_id: str = Query(default=""),
    ctx: WorkerContext = Depends(get_worker_context),
) -> JSONResponse:
    target_company_id = safe_slug(company_id or ctx.company_id)
    target_user_id = safe_slug(user_id) if str(user_id or "").strip() else ""
    if ctx.user_role not in {"master", "owner"}:
        raise HTTPException(status_code=403, detail="Permissão negada.")
    if ctx.user_role != "master" and target_company_id != safe_slug(ctx.company_id):
        raise HTTPException(status_code=403, detail="Permissão negada.")
    if not target_user_id:
        raise HTTPException(status_code=400, detail="Usuário inválido.")
    result = await _delete_member_data(target_company_id, target_user_id)
    return JSONResponse(status_code=200 if result["completed"] else 202, content=result)


@app.post("/api/admin/company-data/delete")
async def admin_delete_company_data(
    company_id: str = Query(default=""),
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    if ctx.user_role != "master":
        raise HTTPException(status_code=403, detail="Permissão negada.")

    target_company_id = safe_slug(company_id) if str(company_id or "").strip() else ""
    if not target_company_id:
        raise HTTPException(status_code=400, detail="Empresa inválida.")

    result = await _delete_company_data(target_company_id)
    return JSONResponse(
        status_code=200 if result["completed"] else 202,
        content={"company_id": target_company_id, **result},
    )


@app.post("/api/admin/company-runs/stop")
async def admin_stop_company_runs(
    company_id: str = Query(default=""),
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    if ctx.user_role != "master":
        raise HTTPException(status_code=403, detail="Permissão negada.")

    target_company_id = safe_slug(company_id) if str(company_id or "").strip() else ""
    if not target_company_id:
        raise HTTPException(status_code=400, detail="Empresa inválida.")
    stopped = 0
    for member_ctx in _loaded_company_contexts(target_company_id):
        stopped += len(await _request_stop_for_ctx(member_ctx))

    removed_queued = await GLOBAL_QUEUE.remove_company(target_company_id)
    return {
        "ok": True,
        "company_id": target_company_id,
        "stop_requests": stopped,
        "removed_queued_groups": removed_queued,
        "active_workers": active_jobs_for_company(target_company_id),
    }


@app.get("/api/accounts")
async def list_accounts(ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    usage = account_usage(ctx)

    return {
        "accounts": [
            {**acc, "linked_cnpjs": usage.get(acc["id"], [])}
            for acc in load_accounts_public(ctx)
        ]
    }


@app.post("/api/accounts")
async def create_account(payload: AccountPayload, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    alias = clean_alias(payload.alias)
    usuario = str(payload.usuario or "").strip()
    senha = str(payload.senha or "").strip()

    if not alias:
        raise HTTPException(status_code=400, detail="Informe o alias da conta.")

    if not usuario or not senha:
        raise HTTPException(status_code=400, detail="Informe usuário e senha.")

    accounts = load_accounts_raw(ctx)
    now = now_ms()

    acc = {
        "id": new_account_id(),
        "alias": alias,
        "usuario": usuario,
        "senha": senha,
        "created_at": now,
        "updated_at": now,
        "created_by_user_id": ctx.user_id,
        "created_by_user_email": ctx.user_email,
    }

    accounts.insert(0, acc)
    save_accounts(ctx, accounts)

    return {
        "created": True,
        "account": compact_account(acc),
        "accounts": load_accounts_public(ctx),
    }


@app.put("/api/accounts/{account_id}")
async def update_account(
    account_id: str,
    payload: AccountPayload,
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    accounts = load_accounts_raw(ctx)
    found = None

    for acc in accounts:
        if acc.get("id") == account_id:
            found = acc
            break

    if not found:
        raise HTTPException(status_code=404, detail="Conta não encontrada.")

    alias = clean_alias(payload.alias)
    usuario = str(payload.usuario or "").strip()
    senha = str(payload.senha or "").strip()

    if not alias:
        raise HTTPException(status_code=400, detail="Informe o alias da conta.")

    if not usuario or not senha:
        raise HTTPException(status_code=400, detail="Informe usuário e senha.")

    found.update(
        {
            "alias": alias,
            "usuario": usuario,
            "senha": senha,
            "updated_at": now_ms(),
        }
    )

    save_accounts(ctx, accounts)

    return {
        "updated": True,
        "account": compact_account(found),
        "accounts": load_accounts_public(ctx),
    }


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    usage = account_usage(ctx)

    if usage.get(account_id):
        raise HTTPException(
            status_code=400,
            detail="Esta conta está vinculada a CNPJs em conjuntos. Remova ou troque os vínculos antes de excluir.",
        )

    accounts = load_accounts_raw(ctx)
    new_accounts = [acc for acc in accounts if acc.get("id") != account_id]

    if len(new_accounts) == len(accounts):
        raise HTTPException(status_code=404, detail="Conta não encontrada.")

    save_accounts(ctx, new_accounts)

    return {
        "deleted": True,
        "accounts": load_accounts_public(ctx),
    }


@app.post("/api/upload-xlsx")
async def upload_xlsx(file: UploadFile = File(...), ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    filename = file.filename or ""

    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Envie um arquivo .xlsx.")

    try:
        data = await file.read()
        items = await asyncio.to_thread(read_xlsx_items_from_bytes, ctx, data)
        validation = validate_dataset_items(ctx, items)

        return {
            "uploaded_file": None,
            "persisted": False,
            "validation": validation,
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Falha ao ler XLSX: {e}") from e


@app.get("/api/datasets")
async def list_datasets(ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    return {
        "max_datasets": None,
        "datasets": load_datasets(ctx),
    }


@app.get("/api/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
    paginate: bool = Query(False),
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    dataset = get_dataset_or_404(ctx, dataset_id)
    raw_items = list(dataset.get("items", []))
    validation = validate_dataset_items(ctx, raw_items)

    if paginate:
      pg = paginate_list(raw_items, page, page_size)
      dataset = {**dataset, "items": pg["items"]}
      return {
          "dataset": dataset,
          "validation": validation,
          "pagination": {
              "page": pg["page"],
              "page_size": pg["page_size"],
              "total": pg["total"],
              "total_pages": pg["total_pages"],
          },
      }

    return {
        "dataset": dataset,
        "validation": validation,
    }


@app.post("/api/datasets")
async def create_dataset(payload: DatasetPayload, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    items = [model_to_dict(item) for item in payload.items]
    return save_dataset_record(ctx, None, payload.alias, items)


@app.put("/api/datasets/{dataset_id}")
async def update_dataset(
    dataset_id: str,
    payload: DatasetPayload,
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    items = [model_to_dict(item) for item in payload.items]
    return save_dataset_record(ctx, dataset_id, payload.alias, items)


@app.delete("/api/datasets/{dataset_id}")
async def delete_dataset(dataset_id: str, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    datasets = load_datasets(ctx)
    new_datasets = [ds for ds in datasets if ds.get("id") != dataset_id]

    if len(new_datasets) == len(datasets):
        raise HTTPException(status_code=404, detail="Conjunto não encontrado.")

    save_datasets(ctx, new_datasets)

    return {
        "deleted": True,
        "datasets": new_datasets,
    }


@app.post("/api/run")
async def create_run(payload: RunCreateRequest, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    assert_no_active_run(ctx)

    mes = build_mes(payload.mes_num, payload.ano)
    dataset = get_dataset_or_404(ctx, payload.dataset_id)

    try:
        runtime_tasks = build_tasks_from_dataset(
            ctx,
            dataset,
            payload.flow_selection,
            usar_codigo_dominio=payload.usar_codigo_dominio,
            reabrir_escrituracao_fechada=payload.reabrir_escrituracao_fechada,
        )
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)

    if not runtime_tasks:
        raise HTTPException(status_code=400, detail="Nenhum fluxo selecionado para execução.")

    return await create_attempt_record(
        ctx,
        mes=mes,
        dataset_id=dataset.get("id"),
        dataset_alias=dataset.get("alias", ""),
        runtime_tasks=runtime_tasks,
        root_id=None,
        parent_run_id=None,
        attempt_type="manual",
        visible=True,
        inherit_credential_snapshots_from_root=None,
        usar_codigo_dominio=payload.usar_codigo_dominio,
        reabrir_escrituracao_fechada=payload.reabrir_escrituracao_fechada,
        auto_retry_enabled=payload.auto_retry_enabled,
    )


@app.get("/api/runs")
async def list_runs(ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    return {"runs": visible_root_runs(ctx)}


@app.get("/api/runs/{run_id}")
async def get_run(
    run_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(200, ge=1, le=500),
    paginate: bool = Query(False),
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    ensure_member_runs_loaded(ctx)

    key = local_run_key(ctx, run_id)
    run = RUNS.get(key)

    if not run:
        run = next((r for r in runs_for_member(ctx) if r.get("run_id") == run_id or root_id_of(r) == run_id), None)

    if not run:
        raise HTTPException(status_code=404, detail="Run não encontrada.")

    root_id = root_id_of(run)
    root_run = next((r for r in runs_for_member(ctx) if r.get("run_id") == root_id), None)

    if not root_run:
        raise HTTPException(status_code=404, detail="Run raiz não encontrada.")

    compact = compact_root_run(
        ctx,
        root_run,
        include_logs=not paginate,
        include_attempts=not paginate,
        include_logs_by_attempt=not paginate,
    )

    if not paginate:
        return compact

    results_raw = list(compact.get("results", []))
    groups_raw = group_results_by_cnpj_for_response(results_raw)
    pg = paginate_list(groups_raw, page, page_size)
    compact["results"] = []
    compact["files"] = []
    compact.pop("logs", None)
    compact.pop("logs_by_attempt", None)
    compact["result_groups"] = pg["items"]
    compact["pagination"] = {
        "page": pg["page"],
        "page_size": pg["page_size"],
        "total": pg["total"],
        "total_pages": pg["total_pages"],
    }
    return compact


@app.get("/api/runs/{run_id}/logs-tail")
async def get_run_logs_tail(
    run_id: str,
    limit_chars: int = 60_000,
    cnpj: str = Query(default=""),
    flow: str = Query(default=""),
    attempt_run_id: str = Query(default=""),
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    ensure_member_runs_loaded(ctx)

    run = next((r for r in runs_for_member(ctx) if r.get("run_id") == run_id or root_id_of(r) == run_id), None)

    if not run:
        raise HTTPException(status_code=404, detail="Run não encontrada.")

    root_id = root_id_of(run)

    cnpj_norm = normalize_cnpj(cnpj) if str(cnpj or "").strip() else ""
    flow_norm = str(flow or "").strip()
    attempt_run_id = str(attempt_run_id or "").strip()
    log_limit = max(5_000, min(limit_chars, 200_000))

    if cnpj_norm:
        logs_by_attempt = logs_by_attempt_for_root_filtered(
            ctx,
            root_id,
            cnpj=cnpj_norm,
            flow=flow_norm,
            attempt_run_id=attempt_run_id,
            limit_chars=log_limit,
        )
    else:
        logs_by_attempt = logs_by_attempt_for_root(ctx, root_id, limit_chars=log_limit)
        if attempt_run_id:
            logs_by_attempt = [attempt for attempt in logs_by_attempt if attempt.get("run_id") == attempt_run_id]

    return {
        "run_id": root_id,
        "logs_by_attempt": logs_by_attempt,
        "ts": now_ms(),
    }


@app.post("/api/runs/{run_id}/duplicate")
async def duplicate_run(run_id: str, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    assert_no_active_run(ctx)

    run = next((r for r in runs_for_member(ctx) if r.get("run_id") == run_id or root_id_of(r) == run_id), None)

    if not run:
        raise HTTPException(status_code=404, detail="Run não encontrada.")

    root_id = root_id_of(run)
    root_run = next((r for r in runs_for_member(ctx) if r.get("run_id") == root_id), None)

    if not root_run:
        raise HTTPException(status_code=404, detail="Run raiz não encontrada.")

    runtime_tasks = hydrate_tasks_with_current_accounts(ctx, root_run.get("input_tasks", []))
    usar_codigo_dominio = bool(root_run.get("usar_codigo_dominio", True))
    reabrir_escrituracao_fechada = bool(root_run.get("reabrir_escrituracao_fechada", True))
    auto_retry_enabled = bool(root_run.get("auto_retry_enabled", True))
    auto_retry_max_attempts = clamp_auto_retry_max_attempts(root_run.get("auto_retry_max_attempts"))

    return await create_attempt_record(
        ctx,
        mes=root_run.get("mes", ""),
        dataset_id=root_run.get("dataset_id"),
        dataset_alias=root_run.get("dataset_alias", ""),
        runtime_tasks=runtime_tasks,
        root_id=None,
        parent_run_id=None,
        attempt_type="duplicate",
        visible=True,
        inherit_credential_snapshots_from_root=None,
        usar_codigo_dominio=usar_codigo_dominio,
        reabrir_escrituracao_fechada=reabrir_escrituracao_fechada,
        auto_retry_enabled=auto_retry_enabled,
        auto_retry_max_attempts=auto_retry_max_attempts,
    )


@app.post("/api/runs/{run_id}/retry")
async def retry_run(
    run_id: str,
    payload: RetryRequest,
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    assert_no_active_run(ctx)

    run = next((r for r in runs_for_member(ctx) if r.get("run_id") == run_id or root_id_of(r) == run_id), None)

    if not run:
        raise HTTPException(status_code=404, detail="Run não encontrada.")

    root_id = root_id_of(run)
    root_run = next((r for r in runs_for_member(ctx) if r.get("run_id") == root_id), None)

    if not root_run:
        raise HTTPException(status_code=404, detail="Run raiz não encontrada.")

    return await create_retry_attempt_for_root(
        ctx,
        root_id=root_id,
        parent_run_id=run_id,
        only_retryable=payload.only_retryable,
        include_cancelled=payload.include_cancelled,
        include_interrupted=payload.include_interrupted,
    )


@app.post("/api/runs/stop-all")
async def stop_all_runs_for_member(ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    attempts = [
        r
        for r in runs_for_member(ctx)
        if r.get("status") in ACTIVE_STATUSES
    ]

    if not attempts:
        return {
            "stopped": False,
            "message": "Não há runs ativas para este colaborador.",
            "attempts": [],
        }

    output = []

    for attempt in attempts:
        attempt_run_key = local_run_key(ctx, attempt.get("run_id", ""))
        run_log_file = attempt.get("run_log_file") or os.path.join(attempt.get("run_dir", ""), "logs.txt")

        write_run_log(
            run_log_file,
            "[STOP_REQUESTED] Parada solicitada porque o colaborador foi removido ou desconectado.",
        )

        output.append(
            await request_stop_for_attempt(
                ctx,
                attempt_run_key,
                reason="Fluxo não iniciado porque o colaborador foi removido ou desconectado.",
                code="USER_REMOVED",
            )
        )

    return {
        "stopped": True,
        "attempts": output,
    }


@app.post("/api/runs/{run_id}/stop")
async def stop_run_gracefully(run_id: str, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    run = next((r for r in runs_for_member(ctx) if r.get("run_id") == run_id or root_id_of(r) == run_id), None)

    if not run:
        raise HTTPException(status_code=404, detail="Run não encontrada.")

    root_id = root_id_of(run)
    attempts = [
        r
        for r in runs_for_member(ctx)
        if root_id_of(r) == root_id and r.get("status") in ACTIVE_STATUSES
    ]

    if not attempts:
        return {
            "stopped": False,
            "message": "A run já terminou.",
            "run_id": root_id,
        }

    output = []

    for attempt in attempts:
        attempt_run_key = local_run_key(ctx, attempt.get("run_id", ""))
        run_log_file = attempt.get("run_log_file") or os.path.join(attempt.get("run_dir", ""), "logs.txt")

        write_run_log(
            run_log_file,
            "[STOP_REQUESTED] Parada da run solicitada. Itens já em execução serão concluídos; itens ainda não iniciados serão cancelados.",
        )

        output.append(
            await request_stop_for_attempt(
                ctx,
                attempt_run_key,
                reason="Fluxo não iniciado porque a run foi parada pelo usuário.",
                code="RUN_STOPPED",
            )
        )

    return {
        "stopped": True,
        "run_id": root_id,
        "message": "A run já foi parada e está finalizando." if output and all(item.get("already_requested") for item in output) else "Parada da run solicitada.",
        "attempts": output,
    }


@app.post("/api/runs/{run_id}/stop-flow")
async def stop_run_flow(
    run_id: str,
    payload: StopFlowRequest,
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    run = next((r for r in runs_for_member(ctx) if r.get("run_id") == run_id or root_id_of(r) == run_id), None)

    if not run:
        raise HTTPException(status_code=404, detail="Run não encontrada.")

    flow_mode = str(payload.flow_mode or "").strip()

    if flow_mode not in FLOW_ORDER:
        raise HTTPException(status_code=400, detail="Fluxo inválido.")

    cnpj_norm = normalize_cnpj(payload.cnpj)
    key = f"{cnpj_norm}|{flow_mode}"

    root_id = root_id_of(run)
    attempts = [
        r
        for r in runs_for_member(ctx)
        if root_id_of(r) == root_id and r.get("status") in ACTIVE_STATUSES
    ]

    if not attempts:
        return {
            "stopped": False,
            "message": "A run já terminou.",
            "run_id": root_id,
        }

    output = []
    found = False

    for attempt in attempts:
        attempt_run_key = local_run_key(ctx, attempt.get("run_id", ""))

        if key not in {task_key(item) for item in attempt.get("input_tasks", [])}:
            continue

        found = True
        run_log_file = attempt.get("run_log_file") or os.path.join(attempt.get("run_dir", ""), "logs.txt")

        write_run_log(
            run_log_file,
            f"[STOP_FLOW_REQUESTED] flow={flow_mode} cnpj={cnpj_norm} :: Parada solicitada.",
        )

        output.append(
            await request_stop_for_attempt(
                ctx,
                attempt_run_key,
                only_key=key,
                reason="Fluxo não iniciado porque foi parado pelo usuário.",
                code="FLOW_STOPPED",
            )
        )

    if not found:
        raise HTTPException(status_code=404, detail="Fluxo não encontrado nesta run.")

    return {
        "stopped": True,
        "run_id": root_id,
        "cnpj": cnpj_norm,
        "flow_mode": flow_mode,
        "message": "A parada deste fluxo já foi solicitada e ele está finalizando." if output and all(item.get("already_requested") for item in output) else "Parada do fluxo solicitada.",
        "attempts": output,
    }


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    run = next((r for r in runs_for_member(ctx) if r.get("run_id") == run_id or root_id_of(r) == run_id), None)

    if not run:
        raise HTTPException(status_code=404, detail="Run não encontrada.")

    root_id = root_id_of(run)

    async with RUN_LOCK:
        assert_root_not_active(ctx, root_id)

    try:
        folder_result = await asyncio.to_thread(delete_run_folder, ctx, root_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Falha ao remover pasta da run: {type(e).__name__}: {e}",
        ) from e

    async with RUN_LOCK:
        db_result = delete_run_from_memory_and_db(ctx, root_id)

    return {
        "deleted": True,
        "run_id": root_id,
        **db_result,
        **folder_result,
    }


@app.get("/api/runs/{run_id}/download")
async def download_run_zip(
    run_id: str,
    cnpj: str = Query(default=""),
    ctx: WorkerContext = Depends(get_worker_context),
):
    run = next((r for r in runs_for_member(ctx) if r.get("run_id") == run_id or root_id_of(r) == run_id), None)

    if not run:
        raise HTTPException(status_code=404, detail="Run não encontrada.")

    root_id = root_id_of(run)

    if root_has_active_attempt(ctx, root_id):
        raise HTTPException(status_code=409, detail="Não é possível baixar o ZIP enquanto a run está em execução ou na fila.")

    if cnpj:
        cnpj_norm = normalize_cnpj(cnpj)
        zip_path = await asyncio.to_thread(create_cnpj_zip, ctx, root_id, cnpj_norm)
        return FileResponse(zip_path, filename=f"{root_id}_{cnpj_norm}.zip", media_type="application/zip")

    zip_path = await asyncio.to_thread(create_root_zip, ctx, root_id)
    return FileResponse(zip_path, filename=f"{root_id}.zip", media_type="application/zip")

@app.get("/api/runs/{run_id}/file")
async def download_run_file(
    run_id: str,
    path: str,
    ctx: WorkerContext = Depends(get_worker_context),
):
    run = next((r for r in runs_for_member(ctx) if r.get("run_id") == run_id), None)

    if not run:
        raise HTTPException(status_code=404, detail="Run não encontrada.")

    run_dir = run.get("run_dir") or os.path.join(
        member_runs_root(ctx),
        root_id_of(run),
        f"tentativa_{run.get('attempt_number', 1)}",
    )

    run_dir = safe_path_inside(member_runs_root(ctx), run_dir)

    full_path = os.path.join(run_dir, path)
    full_path = safe_path_inside(run_dir, full_path)

    if should_hide_run_file(os.path.basename(full_path)):
        raise HTTPException(status_code=404, detail="Arquivo interno não disponível para download.")

    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")

    if not is_valid_nonempty_file(full_path):
        raise HTTPException(status_code=404, detail="Arquivo vazio ou corrompido ignorado.")

    return FileResponse(full_path, filename=os.path.basename(full_path))


@app.get("/api/debug/state")
async def debug_state(ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    return await build_state(ctx)


@app.get("/api/debug/queue")
async def debug_queue(ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    return await queue_state_for_ctx(ctx)


@app.get("/api/admin/system-metrics")
async def admin_system_metrics(
    range: str = Query(default="6h"),
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    if ctx.user_role != "master":
        raise HTTPException(status_code=403, detail="Permissão negada.")

    range_key = range if range in {"1h", "6h", "24h", "5d"} else "6h"
    return {"ok": True, **await asyncio.to_thread(_load_monitor_metrics, range_key)}


@app.get("/api/admin/modal-billing")
async def admin_modal_billing(ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    if ctx.user_role != "master":
        raise HTTPException(status_code=403, detail="Permissão negada.")
    return await asyncio.to_thread(_modal_billing_snapshot)


@app.get("/api/internal/runtime-metrics")
async def internal_runtime_metrics(
    x_internal_secret: str = Header(default="", alias="X-Internal-Secret"),
) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    queue = await runtime_queue_metrics()
    runs = list(RUNS.values())
    active_runs = [run for run in runs if run.get("status") in ACTIVE_STATUSES]
    return {
        "ok": True,
        "ts": int(time.time()),
        "queue": queue,
        "runs": {
            "loaded": len(runs),
            "active": len(active_runs),
            "errors": sum(int(run.get("erros") or 0) for run in runs),
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
