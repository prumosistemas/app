import asyncio
import os
import contextlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from fastapi import HTTPException

from flow_errors import classify_exception
from flow_escrituracao import job_escrituracao
from flow_dam import job_dam
from flow_certidao import job_certidao
from flow_notas import job_notas

from db import (
    ACTIVE_STATUSES,
    AUTO_RETRY_MAX_ATTEMPTS,
    FLOW_EXECUTION_ORDER,
    FLOW_LABELS,
    HEADLESS,
    ITEM_FINAL_STATUSES,
    MAX_BROWSERS,
    logger,
    clamp_auto_retry_max_attempts,
    now_ms,
)

from domain import (
    RUNS,
    RUN_LOCK,
    WorkerContext,
    assert_no_active_run,
    attempt_dir,
    aggregate_results_for_root,
    build_state_sync,
    cancel_item_before_start,
    compact_root_run,
    credential_snapshot_from_tasks,
    flow_dir_for_task,
    group_tasks_by_cnpj,
    hydrate_tasks_with_retry_snapshot,
    item_stop_requested,
    list_run_files,
    local_run_key,
    new_run_id,
    normalize_cnpj,
    owner_key,
    recompute_run_counters,
    root_id_of,
    runs_for_member,
    save_runs_state,
    scope_id,
    strip_runtime_tasks,
    task_key,
    upsert_run_result,
    validate_output_integrity,
    write_run_log,
)


AUTO_RETRY_BLOCKED_CODES = {
    "CNPJ_INEXISTENTE",
    "CNPJ_MISMATCH",
    "MENSAGEM_NA_TELA",
    "LOGIN_ERROR",
    "PORTAL_ACCESS_BLOCKED",
    "ESCRITURACAO_FECHADA_REABERTURA_DESATIVADA",
}


def is_safe_retryable_result(result: Dict[str, Any]) -> bool:
    code = str(result.get("erro_code") or result.get("code") or "").strip().upper()
    return bool(result.get("retryable")) and code not in AUTO_RETRY_BLOCKED_CODES


def _attempts_for_root(ctx: WorkerContext, root_id: str) -> List[Dict[str, Any]]:
    attempts = [run for run in runs_for_member(ctx) if root_id_of(run) == root_id]
    attempts.sort(key=lambda item: int(item.get("created_at") or 0))
    return attempts


@dataclass(frozen=True)
class QueueJob:
    scope_id: str
    owner_key: str
    run_id: str
    group_key: str
    mes: str
    items: List[Dict[str, Any]]
    ctx: WorkerContext


class FairRunQueue:
    """
    Fila global justa:
    - Cada membro tem um bucket.
    - Workers pegam 1 grupo por membro em round-robin.
    - Evita que um usuário com 500 itens bloqueie todo mundo.
    """

    def __init__(self) -> None:
        self._buckets: Dict[str, Deque[QueueJob]] = {}
        self._order: Deque[str] = deque()
        self._cond = asyncio.Condition()

    async def put(self, job: QueueJob) -> None:
        async with self._cond:
            existed = job.owner_key in self._buckets
            if not existed:
                self._buckets[job.owner_key] = deque()
                self._order.append(job.owner_key)
            self._buckets[job.owner_key].append(job)
            self._cond.notify()

    async def get(self) -> QueueJob:
        async with self._cond:
            while not self._order:
                await self._cond.wait()

            owner_key = self._order.popleft()
            bucket = self._buckets.get(owner_key)

            while not bucket:
                self._buckets.pop(owner_key, None)
                if not self._order:
                    await self._cond.wait()
                owner_key = self._order.popleft()
                bucket = self._buckets.get(owner_key)

            job = bucket.popleft()

            if bucket:
                self._order.append(owner_key)
            else:
                self._buckets.pop(owner_key, None)

            return job

    async def snapshot(self) -> Dict[str, Any]:
        async with self._cond:
            per_owner = {owner: len(bucket) for owner, bucket in self._buckets.items()}
            return {
                "pending_groups": sum(per_owner.values()),
                "active_members_waiting": len(per_owner),
                "per_member": per_owner,
                "round_robin_order": list(self._order),
            }

    async def position_for_owner(self, owner_key: str) -> Optional[int]:
        async with self._cond:
            if owner_key not in self._buckets:
                return None

            pos = 0
            seen = set()

            for owner in self._order:
                if owner in seen:
                    continue
                seen.add(owner)
                pos += 1
                if owner == owner_key:
                    return pos

            return None

    async def remove_owner(self, owner_key: str) -> int:
        async with self._cond:
            bucket = self._buckets.pop(owner_key, deque())
            self._order = deque(item for item in self._order if item != owner_key)
            return len(bucket)

    async def remove_company(self, company_id: str) -> int:
        prefix = f"{company_id}:"
        async with self._cond:
            owners = [owner for owner in self._buckets if owner.startswith(prefix)]
            removed = sum(len(self._buckets.pop(owner, deque())) for owner in owners)
            if owners:
                owner_set = set(owners)
                self._order = deque(item for item in self._order if item not in owner_set)
            return removed


GLOBAL_QUEUE = FairRunQueue()
QUEUE_WORKERS_STARTED = False
QUEUE_WORKER_TASKS: List[asyncio.Task] = []
ACTIVE_QUEUE_JOBS: Dict[int, QueueJob] = {}


async def queue_state_for_ctx(ctx: WorkerContext) -> Dict[str, Any]:
    snap = await GLOBAL_QUEUE.snapshot()
    pos = await GLOBAL_QUEUE.position_for_owner(owner_key(ctx))
    return {
        "workers": MAX_BROWSERS,
        "my_queue_position": pos,
        **snap,
    }


async def runtime_queue_metrics() -> Dict[str, Any]:
    snap = await GLOBAL_QUEUE.snapshot()
    active_jobs = list(ACTIVE_QUEUE_JOBS.values())
    return {
        "workers": MAX_BROWSERS,
        "workers_busy": len(active_jobs),
        "workers_idle": max(0, MAX_BROWSERS - len(active_jobs)),
        "active_jobs": [
            {
                "worker": worker,
                "scope_id": job.scope_id,
                "group_key": job.group_key,
            }
            for worker, job in sorted(ACTIVE_QUEUE_JOBS.items())
        ],
        **snap,
    }


def active_jobs_for_scope(scope: str) -> int:
    return sum(1 for job in ACTIVE_QUEUE_JOBS.values() if job.scope_id == scope)


def active_jobs_for_company(company_id: str) -> int:
    prefix = f"{company_id}:"
    return sum(1 for job in ACTIVE_QUEUE_JOBS.values() if job.scope_id.startswith(prefix))


async def build_state(ctx: WorkerContext) -> Dict[str, Any]:
    state = build_state_sync(ctx)
    state["queue"] = await queue_state_for_ctx(ctx)
    return state


async def execute_flow(
    *,
    item: Dict[str, Any],
    mes: str,
    run_key: str,
    attempt_run_dir: str,
    run_log_file: str,
) -> Dict[str, Any]:
    cnpj = item.get("cnpj", "")
    codigo_dominio = item.get("codigo_dominio", "")
    usuario = item.get("usuario", "")
    senha = item.get("senha", "")
    flow_mode = item.get("flow_mode", "")

    if not usuario or not senha:
        raise RuntimeError("Conta sem usuário ou senha.")

    task_run_dir = flow_dir_for_task(attempt_run_dir, flow_mode)
    Path(task_run_dir).mkdir(parents=True, exist_ok=True)

    common_kwargs = dict(
        cnpj=cnpj,
        mes=mes,
        usuario=usuario,
        senha=senha,
        run_id=run_key,
        run_dir=task_run_dir,
        run_log_file=run_log_file,
        headless=HEADLESS,
    )

    if flow_mode == "escrituracao":
        coro = job_escrituracao(
            **common_kwargs,
            should_stop=lambda: item_stop_requested(run_key, item),
            reabrir_fechada=bool(item.get("reabrir_escrituracao_fechada", True)),
        )
        cancel_on_stop = False
    elif flow_mode == "dam":
        coro = job_dam(**common_kwargs)
        cancel_on_stop = True
    elif flow_mode == "certidao":
        coro = job_certidao(**common_kwargs)
        cancel_on_stop = True
    elif flow_mode == "notas":
        coro = job_notas(
            **common_kwargs,
            codigo_dominio=codigo_dominio,
            usar_codigo_dominio=bool(item.get("usar_codigo_dominio", True)),
        )
        cancel_on_stop = True
    else:
        raise ValueError(f"FLOW_MODE inválido: {flow_mode}")

    task = asyncio.create_task(coro)

    try:
        while not task.done():
            if cancel_on_stop and item_stop_requested(run_key, item):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise RuntimeError("Execução interrompida após solicitação de parada.")
            await asyncio.sleep(0.35)

        result = await task
    except asyncio.CancelledError:
        task.cancel()
        raise

    pasta = result.get("pasta") or task_run_dir
    validate_output_integrity(pasta)

    return result


def _transient_flow_retries() -> int:
    try:
        return max(0, min(int(os.getenv("FLOW_TRANSIENT_RETRIES", "2")), 3))
    except Exception:
        return 2


def _retry_backoff_seconds(attempt: int) -> float:
    return min(18.0, 2.0 + (attempt * 3.0))


async def run_one_item_unlocked(
    ctx: WorkerContext,
    *,
    item: Dict[str, Any],
    mes: str,
    run_key: str,
    attempt_run_dir: str,
    run_log_file: str,
) -> Dict[str, Any]:
    cnpj = item.get("cnpj", "")
    account_alias = item.get("account_alias", "")
    nome_empresa = item.get("nome_empresa", "")
    flow_mode = item.get("flow_mode", "")
    flow_label = FLOW_LABELS.get(flow_mode, flow_mode)

    if item_stop_requested(run_key, item):
        return await cancel_item_before_start(
            ctx,
            run_key,
            item,
            run_log_file,
            reason="Fluxo não iniciado porque uma parada foi solicitada.",
            code="FLOW_STOPPED" if not RUNS.get(run_key, {}).get("stop_requested") else "RUN_STOPPED",
        )

    try:
        logger.info(
            f"[INICIO] scope={scope_id(ctx)} run={run_key} flow={flow_mode} cnpj={cnpj} conta={account_alias}"
        )

        await upsert_run_result(
            ctx,
            run_key,
            {
                "cnpj": cnpj,
                "codigo_dominio": item.get("codigo_dominio", ""),
                "nome_empresa": nome_empresa,
                "account_id": item.get("account_id", ""),
                "account_alias": account_alias,
                "flow_mode": flow_mode,
                "flow_label": flow_label,
                "status": "running",
                "retryable": False,
                "started_at": now_ms(),
                "usar_codigo_dominio": bool(item.get("usar_codigo_dominio", True)),
            },
            save=False,
        )

        write_run_log(
            run_log_file,
            f"[ITEM_START] flow={flow_mode} cnpj={normalize_cnpj(cnpj)} conta={account_alias}",
        )

        flow_retry = 0
        run_auto_retry = bool(RUNS.get(run_key, {}).get("auto_retry_enabled", True))
        max_flow_retries = _transient_flow_retries() if run_auto_retry else 0
        while True:
            try:
                result = await execute_flow(
                    item=item,
                    mes=mes,
                    run_key=run_key,
                    attempt_run_dir=attempt_run_dir,
                    run_log_file=run_log_file,
                )
                break
            except Exception as e:
                if item_stop_requested(run_key, item):
                    raise

                err = classify_exception(e)
                if not err.retryable or flow_retry >= max_flow_retries:
                    raise

                flow_retry += 1
                delay = _retry_backoff_seconds(flow_retry)
                logger.warning(
                    f"[RETRY] scope={scope_id(ctx)} run={run_key} flow={flow_mode} "
                    f"cnpj={cnpj} conta={account_alias} code={err.code} "
                    f"attempt={flow_retry}/{max_flow_retries} delay={delay:.1f}s"
                )
                write_run_log(
                    run_log_file,
                    (
                        f"[ITEM_RETRY] flow={flow_mode} cnpj={normalize_cnpj(cnpj)} "
                        f"conta={account_alias} code={err.code} "
                        f"attempt={flow_retry}/{max_flow_retries} delay={delay:.1f}s"
                    ),
                )
                await upsert_run_result(
                    ctx,
                    run_key,
                    {
                        "cnpj": cnpj,
                        "codigo_dominio": item.get("codigo_dominio", ""),
                        "nome_empresa": nome_empresa,
                        "account_id": item.get("account_id", ""),
                        "account_alias": account_alias,
                        "flow_mode": flow_mode,
                        "flow_label": flow_label,
                        "status": "running",
                        "aviso": f"Retry automático após {err.code}.",
                        "aviso_code": "AUTO_RETRY",
                        "retryable": False,
                        "usar_codigo_dominio": bool(item.get("usar_codigo_dominio", True)),
                    },
                    save=True,
                )
                await asyncio.sleep(delay)

        if item_stop_requested(run_key, item):
            final_result = {
                "cnpj": cnpj,
                "codigo_dominio": item.get("codigo_dominio", ""),
                "nome_empresa": nome_empresa,
                "account_id": item.get("account_id", ""),
                "account_alias": account_alias,
                "flow_mode": flow_mode,
                "flow_label": flow_label,
                "status": "interrompida",
                "erro": "Execução interrompida após solicitação de parada.",
                "erro_code": "RUN_STOPPED",
                "erro_action": "Executar retry se este fluxo ainda for necessário.",
                "retryable": True,
                "finished_at": now_ms(),
                "usar_codigo_dominio": bool(item.get("usar_codigo_dominio", True)),
            }

            write_run_log(
                run_log_file,
                (
                    f"[ITEM_INTERRUPTED] flow={flow_mode} cnpj={normalize_cnpj(cnpj)} conta={account_alias} "
                    "code=RUN_STOPPED retryable=True msg=Execução interrompida após solicitação de parada."
                ),
            )

            await upsert_run_result(ctx, run_key, final_result, save=True)
            return final_result

        logger.info(f"[OK] scope={scope_id(ctx)} run={run_key} flow={flow_mode} cnpj={cnpj}")

        write_run_log(
            run_log_file,
            f"[ITEM_OK] flow={flow_mode} cnpj={normalize_cnpj(cnpj)} conta={account_alias}",
        )

        final_result = {
            "cnpj": cnpj,
            "codigo_dominio": item.get("codigo_dominio", ""),
            "nome_empresa": nome_empresa,
            "account_id": item.get("account_id", ""),
            "account_alias": account_alias,
            "flow_mode": flow_mode,
            "flow_label": flow_label,
            "status": "ok",
            "retryable": False,
            "finished_at": now_ms(),
            "usar_codigo_dominio": bool(item.get("usar_codigo_dominio", True)),
            **result,
        }

        await upsert_run_result(ctx, run_key, final_result, save=True)
        return final_result

    except Exception as e:
        if item_stop_requested(run_key, item):
            final_result = {
                "cnpj": cnpj,
                "codigo_dominio": item.get("codigo_dominio", ""),
                "nome_empresa": nome_empresa,
                "account_id": item.get("account_id", ""),
                "account_alias": account_alias,
                "flow_mode": flow_mode,
                "flow_label": flow_label,
                "status": "interrompida",
                "erro": "Execução interrompida após solicitação de parada.",
                "erro_code": "RUN_STOPPED",
                "erro_action": "Executar retry se este fluxo ainda for necessário.",
                "retryable": True,
                "finished_at": now_ms(),
                "usar_codigo_dominio": bool(item.get("usar_codigo_dominio", True)),
            }

            write_run_log(
                run_log_file,
                (
                    f"[ITEM_INTERRUPTED] flow={flow_mode} cnpj={normalize_cnpj(cnpj)} conta={account_alias} "
                    "code=RUN_STOPPED retryable=True msg=Execução interrompida após solicitação de parada."
                ),
            )

            await upsert_run_result(ctx, run_key, final_result, save=True)
            return final_result

        err = classify_exception(e)
        controlled_closed = err.code == "ESCRITURACAO_FECHADA_REABERTURA_DESATIVADA"

        log_error = logger.warning if controlled_closed else logger.error
        log_error(
            f"[ERRO] scope={scope_id(ctx)} run={run_key} flow={flow_mode} cnpj={cnpj} conta={account_alias} "
            f"code={err.code} msg={err.short_message}"
        )

        msg = err.short_message

        if (
            "Arquivo gerado vazio ou corrompido" in str(e)
            or "Nenhum arquivo válido" in str(e)
            or "Saída não encontrada" in str(e)
        ):
            msg = str(e)

        write_run_log(
            run_log_file,
            (
                f"[{'ITEM_OK_WITH_WARNING' if controlled_closed else 'ITEM_ERROR'}] flow={flow_mode} cnpj={normalize_cnpj(cnpj)} conta={account_alias} "
                f"code={err.code} retryable={bool(err.retryable)} msg={msg}"
            ),
        )

        final_result = {
            "cnpj": cnpj,
            "codigo_dominio": item.get("codigo_dominio", ""),
            "nome_empresa": nome_empresa,
            "account_id": item.get("account_id", ""),
            "account_alias": account_alias,
            "flow_mode": flow_mode,
            "flow_label": flow_label,
            "status": "ok" if controlled_closed else "erro",
            "erro": "" if controlled_closed else msg,
            "erro_code": "" if controlled_closed else err.code,
            "erro_action": "" if controlled_closed else err.action,
            "aviso": msg if controlled_closed else "",
            "aviso_code": err.code if controlled_closed else "",
            "aviso_action": err.action if controlled_closed else "",
            "retryable": False if controlled_closed else bool(err.retryable),
            "finished_at": now_ms(),
            "usar_codigo_dominio": bool(item.get("usar_codigo_dominio", True)),
        }

        await upsert_run_result(ctx, run_key, final_result, save=True)
        return final_result


async def run_cnpj_group_serial(
    ctx: WorkerContext,
    *,
    items: List[Dict[str, Any]],
    mes: str,
    run_key: str,
    attempt_run_dir: str,
    run_log_file: str,
) -> List[Dict[str, Any]]:
    """
    Um worker da fila executa um CNPJ por vez.
    Dentro do CNPJ, os fluxos são serializados para respeitar:
    certidão -> escrituração -> DAM -> notas.

    Isso protege o browserless: cada worker consome no máximo um navegador por vez.
    """

    ordered_items = sorted(
        items,
        key=lambda item: FLOW_EXECUTION_ORDER.index(item.get("flow_mode", "notas"))
        if item.get("flow_mode") in FLOW_EXECUTION_ORDER
        else 999,
    )

    results: List[Dict[str, Any]] = []
    escrituracao_result: Optional[Dict[str, Any]] = None

    for item in ordered_items:
        flow = item.get("flow_mode")

        if item_stop_requested(run_key, item):
            result = await cancel_item_before_start(
                ctx,
                run_key,
                item,
                run_log_file,
                reason="Fluxo não iniciado porque uma parada foi solicitada.",
                code="FLOW_STOPPED" if not RUNS.get(run_key, {}).get("stop_requested") else "RUN_STOPPED",
            )
            results.append(result)
            continue

        if flow == "dam":
            has_escrituracao = any(x.get("flow_mode") == "escrituracao" for x in ordered_items)
            escrit_closed_without_reopen = (
                escrituracao_result
                and escrituracao_result.get("erro_code") == "ESCRITURACAO_FECHADA_REABERTURA_DESATIVADA"
            )

            if has_escrituracao and (not escrituracao_result or (escrituracao_result.get("status") != "ok" and not escrit_closed_without_reopen)):
                skipped = {
                    "cnpj": item.get("cnpj", ""),
                    "codigo_dominio": item.get("codigo_dominio", ""),
                    "nome_empresa": item.get("nome_empresa", ""),
                    "account_id": item.get("account_id", ""),
                    "account_alias": item.get("account_alias", ""),
                    "flow_mode": "dam",
                    "flow_label": FLOW_LABELS.get("dam", "DAM"),
                    "status": "erro",
                    "erro": "DAM não executado porque a Escrituração deste CNPJ falhou.",
                    "erro_code": "DAM_BLOCKED_BY_ESCRITURACAO",
                    "erro_action": "Corrigir a Escrituração e executar retry antes de gerar DAM.",
                    "retryable": True,
                    "finished_at": now_ms(),
                    "usar_codigo_dominio": bool(item.get("usar_codigo_dominio", True)),
                }

                write_run_log(
                    run_log_file,
                    (
                        f"[ITEM_ERROR] flow=dam cnpj={normalize_cnpj(item.get('cnpj', ''))} "
                        f"code=DAM_BLOCKED_BY_ESCRITURACAO retryable=True "
                        f"msg=DAM não executado porque Escrituração falhou."
                    ),
                )

                await upsert_run_result(ctx, run_key, skipped, save=True)
                results.append(skipped)
                continue

        result = await run_one_item_unlocked(
            ctx,
            item=item,
            mes=mes,
            run_key=run_key,
            attempt_run_dir=attempt_run_dir,
            run_log_file=run_log_file,
        )

        if flow == "escrituracao":
            escrituracao_result = result

        results.append(result)

    return results


async def mark_run_group_started(ctx: WorkerContext, run_key: str, group_key: str) -> None:
    async with RUN_LOCK:
        run = RUNS.get(run_key)

        if not run:
            return

        if run.get("status") == "queued":
            run["status"] = "running"
            run["started_at"] = run.get("started_at") or now_ms()

        active_groups = set(run.get("active_groups", []) or [])
        active_groups.add(group_key)
        run["active_groups"] = sorted(active_groups)

        save_runs_state(ctx)


async def mark_run_group_finished(ctx: WorkerContext, run_key: str, group_key: str) -> None:
    should_auto_retry = False

    async with RUN_LOCK:
        run = RUNS.get(run_key)

        if not run:
            return

        active_groups = set(run.get("active_groups", []) or [])
        active_groups.discard(group_key)
        run["active_groups"] = sorted(active_groups)

        finished_groups = set(run.get("finished_groups", []) or [])
        finished_groups.add(group_key)
        run["finished_groups"] = sorted(finished_groups)
        run["groups_done"] = len(finished_groups)

        run["files"] = list_run_files(run.get("run_dir", ""), run.get("run_id", ""))
        recompute_run_counters(run_key)

        groups_total = int(run.get("groups_total") or 0)

        if groups_total > 0 and len(finished_groups) >= groups_total:
            final_status = "cancelled" if run.get("stop_requested") else "finished"
            run["status"] = final_status
            run["finished_at"] = now_ms()
            run["done"] = len([r for r in run.get("results", []) if r.get("status") in ITEM_FINAL_STATUSES])

            run_log_file = run.get("run_log_file") or os.path.join(run.get("run_dir", ""), "logs.txt")
            write_run_log(
                run_log_file,
                (
                    f"[RUN_END] run={run.get('run_id')} total={run.get('total')} "
                    f"status={run['status']} ok={run.get('ok', 0)} erros={run.get('erros', 0)}"
                ),
            )
            should_auto_retry = final_status == "finished"

        save_runs_state(ctx)

    if should_auto_retry:
        await maybe_schedule_auto_retry(ctx, run_key)


async def queue_worker(worker_index: int) -> None:
    logger.info(f"[queue] worker {worker_index} iniciado")

    while True:
        job = await GLOBAL_QUEUE.get()
        ACTIVE_QUEUE_JOBS[worker_index] = job

        try:
            run_key = job.run_id
            run = RUNS.get(run_key)

            if not run:
                continue

            run_dir = run.get("run_dir", "")
            run_log_file = run.get("run_log_file") or os.path.join(run_dir, "logs.txt")
            Path(run_dir).mkdir(parents=True, exist_ok=True)

            await mark_run_group_started(job.ctx, run_key, job.group_key)

            write_run_log(
                run_log_file,
                f"[QUEUE_WORKER_START] worker={worker_index} group={job.group_key} owner={job.owner_key}",
            )

            if RUNS.get(run_key, {}).get("stop_requested"):
                for item in job.items:
                    await cancel_item_before_start(
                        job.ctx,
                        run_key,
                        item,
                        run_log_file,
                        reason="Fluxo não iniciado porque a run foi parada enquanto estava na fila.",
                        code="RUN_STOPPED_IN_QUEUE",
                    )
            else:
                await run_cnpj_group_serial(
                    job.ctx,
                    items=job.items,
                    mes=job.mes,
                    run_key=run_key,
                    attempt_run_dir=run_dir,
                    run_log_file=run_log_file,
                )

            write_run_log(
                run_log_file,
                f"[QUEUE_WORKER_END] worker={worker_index} group={job.group_key}",
            )

        except asyncio.CancelledError:
            raise

        except Exception as e:
            logger.exception(f"[queue] worker={worker_index} falhou: {e}")

            try:
                run = RUNS.get(job.run_id)
                if run:
                    run_log_file = run.get("run_log_file") or os.path.join(run.get("run_dir", ""), "logs.txt")
                    write_run_log(
                        run_log_file,
                        f"[QUEUE_WORKER_FAILED] worker={worker_index} group={job.group_key} error={type(e).__name__}: {e}",
                    )
            except Exception:
                pass

        finally:
            try:
                await mark_run_group_finished(job.ctx, job.run_id, job.group_key)
            except Exception as e:
                logger.exception(f"[queue] falha ao finalizar grupo: {e}")
            ACTIVE_QUEUE_JOBS.pop(worker_index, None)


async def startup_queue_workers() -> None:
    global QUEUE_WORKERS_STARTED

    if QUEUE_WORKERS_STARTED:
        return

    QUEUE_WORKERS_STARTED = True

    for i in range(MAX_BROWSERS):
        task = asyncio.create_task(queue_worker(i + 1))
        QUEUE_WORKER_TASKS.append(task)

    logger.info(f"[queue] {MAX_BROWSERS} workers globais iniciados")


def next_attempt_number(ctx: WorkerContext, root_id: str) -> int:
    attempts = [r for r in runs_for_member(ctx) if root_id_of(r) == root_id]
    return len(attempts) + 1


async def create_retry_attempt_for_root(
    ctx: WorkerContext,
    root_id: str,
    *,
    parent_run_id: str,
    only_retryable: bool = True,
    include_cancelled: bool = False,
    include_interrupted: bool = False,
    auto: bool = False,
) -> Dict[str, Any]:
    root_run = next((r for r in runs_for_member(ctx) if r.get("run_id") == root_id), None)

    if not root_run:
        raise HTTPException(status_code=404, detail="Run raiz não encontrada.")

    retry_statuses = {"erro"}

    if include_cancelled:
        retry_statuses.add("cancelled")

    if include_interrupted:
        retry_statuses.add("interrompida")

    error_results = []

    for result in aggregate_results_for_root(ctx, root_id):
        if result.get("status") not in retry_statuses:
            continue
        if only_retryable and not is_safe_retryable_result(result):
            continue
        error_results.append(result)

    if not error_results:
        extras = []
        if include_cancelled:
            extras.append("cancelamentos")
        if include_interrupted:
            extras.append("interrompidas")
        qualifier = " seguros/retryable" if only_retryable else ""
        msg = "Não há erros" + qualifier + (", " + " ou ".join(extras) if extras else "") + " elegíveis para retry."
        raise HTTPException(status_code=400, detail=msg)

    error_keys = {task_key(result) for result in error_results}
    retry_state_tasks = [item for item in root_run.get("input_tasks", []) if task_key(item) in error_keys]

    if not retry_state_tasks:
        raise HTTPException(status_code=400, detail="Não foi possível montar os itens de retry.")

    runtime_tasks = hydrate_tasks_with_retry_snapshot(ctx, root_run, retry_state_tasks)

    return await create_attempt_record(
        ctx,
        mes=root_run.get("mes", ""),
        dataset_id=root_run.get("dataset_id"),
        dataset_alias=root_run.get("dataset_alias", ""),
        runtime_tasks=runtime_tasks,
        root_id=root_id,
        parent_run_id=parent_run_id,
        attempt_type="auto_retry" if auto else "retry",
        visible=False,
        inherit_credential_snapshots_from_root=root_run.get("credential_snapshots", {}),
        usar_codigo_dominio=bool(root_run.get("usar_codigo_dominio", True)),
        reabrir_escrituracao_fechada=bool(root_run.get("reabrir_escrituracao_fechada", True)),
        auto_retry_enabled=bool(root_run.get("auto_retry_enabled", True)),
        auto_retry_max_attempts=clamp_auto_retry_max_attempts(root_run.get("auto_retry_max_attempts")),
    )


async def maybe_schedule_auto_retry(ctx: WorkerContext, run_key: str) -> None:
    run = RUNS.get(run_key)

    if not run or run.get("status") != "finished":
        return

    root_id = root_id_of(run)
    root_run = next((r for r in runs_for_member(ctx) if r.get("run_id") == root_id), None)

    if not root_run or not bool(root_run.get("auto_retry_enabled", True)):
        return

    max_attempts = clamp_auto_retry_max_attempts(root_run.get("auto_retry_max_attempts"))
    attempts = _attempts_for_root(ctx, root_id)
    run_log_file = run.get("run_log_file") or os.path.join(run.get("run_dir", ""), "logs.txt")

    if len(attempts) >= max_attempts:
        write_run_log(
            run_log_file,
            f"[AUTO_RETRY_SKIP] run={root_id} motivo=max_attempts attempts={len(attempts)} max={max_attempts}",
        )
        return

    try:
        payload = await create_retry_attempt_for_root(
            ctx,
            root_id,
            parent_run_id=run.get("run_id"),
            only_retryable=True,
            include_cancelled=False,
            include_interrupted=False,
            auto=True,
        )
        write_run_log(
            run_log_file,
            (
                f"[AUTO_RETRY_START] run={root_id} "
                f"attempt_run_id={payload.get('attempt_run_id')} max_attempts={max_attempts}"
            ),
        )
    except HTTPException as exc:
        write_run_log(run_log_file, f"[AUTO_RETRY_SKIP] run={root_id} motivo={exc.detail}")
    except Exception as exc:
        logger.exception("[AUTO_RETRY_ERROR] run=%s", root_id)
        write_run_log(run_log_file, f"[AUTO_RETRY_ERROR] run={root_id} erro={type(exc).__name__}: {exc}")


async def create_attempt_record(
    ctx: WorkerContext,
    *,
    mes: str,
    dataset_id: Optional[str],
    dataset_alias: str,
    runtime_tasks: List[Dict[str, Any]],
    root_id: Optional[str],
    parent_run_id: Optional[str],
    attempt_type: str,
    visible: bool,
    inherit_credential_snapshots_from_root: Optional[Dict[str, Any]] = None,
    usar_codigo_dominio: bool = True,
    reabrir_escrituracao_fechada: bool = True,
    auto_retry_enabled: bool = True,
    auto_retry_max_attempts: int = AUTO_RETRY_MAX_ATTEMPTS,
) -> Dict[str, Any]:
    groups = group_tasks_by_cnpj(runtime_tasks)

    if not groups:
        raise HTTPException(status_code=400, detail="Nenhum grupo de CNPJ para executar.")

    async with RUN_LOCK:
        assert_no_active_run(ctx)

        run_id = new_run_id()
        root_id = root_id or run_id
        attempt_number = next_attempt_number(ctx, root_id)
        run_dir = attempt_dir(ctx, root_id, attempt_number)
        run_log_file = os.path.join(run_dir, "logs.txt")

        Path(run_dir).mkdir(parents=True, exist_ok=True)

        if inherit_credential_snapshots_from_root is not None:
            credential_snapshots = inherit_credential_snapshots_from_root
        else:
            credential_snapshots = credential_snapshot_from_tasks(runtime_tasks)

        run_key = local_run_key(ctx, run_id)

        RUNS[run_key] = {
            "scope_id": scope_id(ctx),
            "company_id": ctx.company_id,
            "company_name": ctx.company_name,
            "user_id": ctx.user_id,
            "user_email": ctx.user_email,
            "run_id": run_id,
            "root_id": root_id,
            "parent_run_id": parent_run_id,
            "attempt_number": attempt_number,
            "attempt_type": attempt_type,
            "visible": visible,
            "status": "queued",
            "mes": mes,
            "dataset_id": dataset_id,
            "dataset_alias": dataset_alias,
            "run_dir": run_dir,
            "run_log_file": run_log_file,
            "created_at": now_ms(),
            "started_at": None,
            "finished_at": None,
            "total": len(runtime_tasks),
            "done": 0,
            "ok": 0,
            "erros": 0,
            "running": 0,
            "results": [],
            "files": [],
            "input_tasks": strip_runtime_tasks(runtime_tasks),
            "credential_snapshots": credential_snapshots,
            "stop_requested": False,
            "stop_requested_keys": [],
            "usar_codigo_dominio": bool(usar_codigo_dominio),
            "reabrir_escrituracao_fechada": bool(reabrir_escrituracao_fechada),
            "auto_retry_enabled": bool(auto_retry_enabled),
            "auto_retry_max_attempts": clamp_auto_retry_max_attempts(auto_retry_max_attempts),
            "created_by_user_id": ctx.user_id,
            "created_by_user_email": ctx.user_email,
            "groups_total": len(groups),
            "groups_done": 0,
            "active_groups": [],
            "finished_groups": [],
        }

        write_run_log(
            run_log_file,
            (
                f"[RUN_QUEUED] scope={scope_id(ctx)} run={run_id} root={root_id} "
                f"attempt={attempt_number} type={attempt_type} mes={mes} "
                f"total_items={len(runtime_tasks)} total_groups={len(groups)}"
            ),
        )

        save_runs_state(ctx)

    for group_key, group_items in groups.items():
        await GLOBAL_QUEUE.put(
            QueueJob(
                scope_id=scope_id(ctx),
                owner_key=owner_key(ctx),
                run_id=local_run_key(ctx, run_id),
                group_key=group_key,
                mes=mes,
                items=group_items,
                ctx=ctx,
            )
        )

    position = await GLOBAL_QUEUE.position_for_owner(owner_key(ctx))
    root_run = RUNS[local_run_key(ctx, root_id)]

    return {
        "started": True,
        "queued": True,
        "run_id": root_id,
        "attempt_run_id": run_id,
        "queue_position": position,
        "groups_total": len(groups),
        "workers": MAX_BROWSERS,
        "run": compact_root_run(
            ctx,
            root_run,
            include_logs=False,
            include_attempts=True,
            include_logs_by_attempt=False,
        ),
    }
