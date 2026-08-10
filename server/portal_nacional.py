import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from domain import (
    SECRET_ENC_PREFIX,
    WorkerContext,
    get_worker_context,
    member_output_root,
    protect_secret,
    safe_path_inside,
    safe_slug,
    unprotect_secret,
)
from db import OUTPUT_ROOT
from portal_nacional_session import list_certificates, load_pfx_identity
from portal_nacional_competencia import (
    UNKNOWN_COMPETENCIA,
    competencia_from_item,
    normalize_competencia,
    summarize_competencias,
)


BASE_DIR = Path(__file__).resolve().parent
AUTOMATION_SCRIPT = BASE_DIR / "portal_nacional_automation.py"
DEFAULT_SOLVER_URL = os.getenv(
    "PORTAL_NACIONAL_SOLVER_URL",
    "https://ryangurgell20--prumo-portal-nacional-google-solver-solve-d8ccea.modal.run/solve",
).strip()
PORTAL_DOWNLOAD_CONCURRENCY = max(
    1,
    min(8, int(os.getenv("PORTAL_NACIONAL_DOWNLOAD_CONCURRENCY", "4"))),
)
PORTAL_AUTOMATIC_RETENTION_DAYS = 123
PORTAL_AUTOMATIC_INITIAL_LOOKBACK_DAYS = 123
PORTAL_AUTOMATIC_OVERLAP_DAYS = 2
PORTAL_AUTOMATIC_POLL_SECONDS = max(
    10,
    min(300, int(os.getenv("PORTAL_AUTOMATIC_POLL_SECONDS", "30"))),
)
PORTAL_TIMEZONE = ZoneInfo("America/Sao_Paulo")

router = APIRouter(prefix="/api/portal-nacional", tags=["portal-nacional"])

_LOCK = threading.RLock()
_RUNTIME: Dict[str, Dict[str, Any]] = {}
_AUTOMATIC_LOCK = threading.RLock()
_AUTOMATIC_STOP = threading.Event()
_AUTOMATIC_THREAD: threading.Thread | None = None


class PortalRunPayload(BaseModel):
    modo: str = Field(default="recebidas")
    tipo_download: str = Field(default="ambos")
    data_inicial: str
    data_final: str
    cert_id: str = ""
    cert_index: int = 0
    renovar_sessao: bool = True
    max_items: int = 0
    retries: int = 6


class PortalRetryPayload(BaseModel):
    # Campos ausentes preservam a configuracao original da run. Defaults aqui
    # faziam um retry interno de XML voltar silenciosamente para XML+PDF.
    tipo_download: str | None = None
    max_items: int | None = None
    retries: int | None = None


class PortalContinuePayload(PortalRetryPayload):
    run_ids: List[str] = Field(default_factory=list)


class PortalSessionImportPayload(BaseModel):
    session: Dict[str, Any]


class PortalAutomaticPayload(BaseModel):
    cert_id: str
    modo: str = "ambos"
    data_inicial: str


def _retry_config(cfg: Dict[str, Any], payload: PortalRetryPayload) -> Dict[str, Any]:
    values = payload.model_dump(exclude={"run_ids"}, exclude_none=True)
    return _normalize_cfg(
        {
            **cfg,
            **values,
            "modo": cfg.get("modo") or "recebidas",
            "data_inicial": cfg.get("data_inicial"),
            "data_final": cfg.get("data_final"),
            "renovar_sessao": False,
        }
    )


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _portal_root(ctx: WorkerContext) -> Path:
    root = Path(member_output_root(ctx)) / "portal_nacional"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _certificates_root(ctx: WorkerContext) -> Path:
    root = _portal_root(ctx) / "certificates"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _runs_root(ctx: WorkerContext) -> Path:
    root = _portal_root(ctx) / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _session_path(ctx: WorkerContext) -> Path:
    sessions = _portal_root(ctx) / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    return sessions / "sessao_nfse.txt"


def _automatic_path(ctx: WorkerContext) -> Path:
    return _portal_root(ctx) / "automatic.json"


def _load_automatic_state(ctx: WorkerContext) -> Dict[str, Any]:
    state = _load_json(_automatic_path(ctx), {})
    jobs = state.get("jobs") if isinstance(state, dict) else None
    return {
        "version": 1,
        "updated_at": state.get("updated_at") if isinstance(state, dict) else None,
        "jobs": [dict(job) for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else [],
    }


def _save_automatic_state(ctx: WorkerContext, state: Dict[str, Any]) -> None:
    payload = {
        "version": 1,
        "updated_at": _now_iso(),
        "jobs": list(state.get("jobs") or []),
    }
    _save_json(_automatic_path(ctx), payload)


def _automatic_context_from_path(path: Path) -> WorkerContext | None:
    try:
        relative = path.resolve().relative_to(Path(OUTPUT_ROOT).resolve())
        parts = relative.parts
        if len(parts) != 6 or parts[0] != "empresas" or parts[2] != "colaboradores":
            return None
        if parts[4] != "portal_nacional" or parts[5] != "automatic.json":
            return None
        return WorkerContext(
            company_id=safe_slug(parts[1]),
            company_name=parts[1],
            user_id=safe_slug(parts[3]),
            user_email="",
            user_role="member",
            via_worker=True,
        )
    except Exception:
        return None


def _automatic_records() -> List[tuple[WorkerContext, Dict[str, Any], Dict[str, Any]]]:
    records: List[tuple[WorkerContext, Dict[str, Any], Dict[str, Any]]] = []
    root = Path(OUTPUT_ROOT) / "empresas"
    if not root.exists():
        return records
    for path in root.glob("*/colaboradores/*/portal_nacional/automatic.json"):
        ctx = _automatic_context_from_path(path)
        if not ctx:
            continue
        state = _load_automatic_state(ctx)
        for job in state["jobs"]:
            records.append((ctx, state, job))
    return records


def _parse_portal_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=PORTAL_TIMEZONE) if parsed.tzinfo is None else parsed.astimezone(PORTAL_TIMEZONE)
    except ValueError:
        return None


def _next_daily_schedule(schedule_minute: int, *, after: datetime | None = None, force_next_day: bool = False) -> datetime:
    current = (after or datetime.now(PORTAL_TIMEZONE)).astimezone(PORTAL_TIMEZONE)
    minute = max(0, min(1439, int(schedule_minute)))
    target_date = current.date() + timedelta(days=1 if force_next_day else 0)
    target = datetime.combine(
        target_date,
        datetime_time(hour=minute // 60, minute=minute % 60),
        tzinfo=PORTAL_TIMEZONE,
    )
    if target <= current:
        target += timedelta(days=1)
    return target


def _rebalance_automatic_schedules(now: datetime | None = None) -> None:
    current = now or datetime.now(PORTAL_TIMEZONE)
    with _AUTOMATIC_LOCK:
        grouped: Dict[str, tuple[WorkerContext, Dict[str, Any]]] = {}
        enabled: List[tuple[WorkerContext, Dict[str, Any], Dict[str, Any]]] = []
        for ctx, state, job in _automatic_records():
            grouped[_runtime_key(ctx)] = (ctx, state)
            if bool(job.get("enabled", True)):
                enabled.append((ctx, state, job))

        enabled.sort(key=lambda item: (_runtime_key(item[0]), str(item[2].get("id") or "")))
        count = len(enabled)
        changed_scopes: set[str] = set()
        for index, (ctx, _state, job) in enumerate(enabled):
            minute = int(index * 1440 / count) if count else 0
            previous_minute = job.get("schedule_minute")
            job["schedule_minute"] = minute
            # Uma configuração nova fica vencida para iniciar assim que o Portal
            # estiver livre. Rebalanceamentos posteriores preservam essa primeira captura.
            if not job.get("next_run_at"):
                job["next_run_at"] = current.isoformat(timespec="seconds")
            elif previous_minute is not None and int(previous_minute) != minute and job.get("last_started_at"):
                job["next_run_at"] = _next_daily_schedule(minute, after=current).isoformat(timespec="seconds")
            changed_scopes.add(_runtime_key(ctx))

        for ctx, state in grouped.values():
            if _runtime_key(ctx) in changed_scopes:
                _save_automatic_state(ctx, state)


def _automatic_capture_range(job: Dict[str, Any], today: date | None = None) -> tuple[str, str]:
    end = today or datetime.now(PORTAL_TIMEZONE).date()
    last_success = str(job.get("last_success_date") or "").strip()
    try:
        start = datetime.strptime(last_success, "%Y-%m-%d").date() - timedelta(days=PORTAL_AUTOMATIC_OVERLAP_DAYS)
    except ValueError:
        configured_start = str(job.get("data_inicial") or "").strip()
        try:
            start = datetime.strptime(configured_start, "%Y-%m-%d").date()
        except ValueError:
            start = end - timedelta(days=PORTAL_AUTOMATIC_INITIAL_LOOKBACK_DAYS)
    if start > end:
        start = end
    return start.strftime("%d/%m/%Y"), end.strftime("%d/%m/%Y")


def _automatic_run_state(ctx: WorkerContext, job: Dict[str, Any]) -> tuple[str, bool]:
    run_ids = [safe_slug(value, "run") for value in list(job.get("last_run_ids") or []) if str(value or "").strip()]
    if not run_ids:
        return str(job.get("last_status") or "aguardando"), False
    statuses: List[str] = []
    for run_id in run_ids:
        run_path = _runs_root(ctx) / run_id / "run.json"
        run = _load_json(run_path, {})
        if run:
            statuses.append(str(run.get("status") or "criada"))
    if not statuses:
        return "run_removida", False
    active = any(value in {"criada", "rodando"} for value in statuses)
    if active:
        return "rodando", True
    if all(value == "finalizado" for value in statuses):
        return "finalizado", False
    if any("erro" in value or value in {"interrompida", "parado"} for value in statuses):
        return "finalizado_com_erros", False
    return statuses[0], False


def _reconcile_automatic_state(ctx: WorkerContext, state: Dict[str, Any]) -> bool:
    changed = False
    for job in state.get("jobs") or []:
        status, active = _automatic_run_state(ctx, job)
        if job.get("last_status") != status:
            job["last_status"] = status
            changed = True
        if status == "finalizado" and job.get("last_capture_end") and job.get("last_success_date") != job.get("last_capture_end"):
            job["last_success_date"] = job["last_capture_end"]
            job["last_completed_at"] = _now_iso()
            job["last_error"] = None
            changed = True
        elif not active and status == "finalizado_com_erros" and not job.get("last_error"):
            job["last_error"] = "A captura terminou com pendências. O próximo ciclo retomará o período não confirmado."
            changed = True
    if changed:
        _save_automatic_state(ctx, state)
    return changed


def _safe_run_dir(ctx: WorkerContext, run_id: str) -> Path:
    run_id = safe_slug(run_id, "run")
    run_dir = _runs_root(ctx) / run_id
    if not (run_dir / "run.json").exists():
        raise HTTPException(status_code=404, detail="Run não encontrada.")
    return Path(safe_path_inside(str(_runs_root(ctx)), str(run_dir)))


def _delete_run_dir(ctx: WorkerContext, run_id: str) -> None:
    run_dir = _safe_run_dir(ctx, run_id)
    root = _runs_root(ctx).resolve()
    target = run_dir.resolve()
    if target == root or root not in target.parents:
        raise HTTPException(status_code=400, detail="Caminho de run inválido.")
    runtime = _active_runtime(ctx)
    if runtime and runtime.get("run_id") == run_id:
        raise HTTPException(status_code=409, detail="Não é permitido excluir run ativa.")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="Run não encontrada.")
    shutil.rmtree(target)


def _run_paths(run_dir: Path) -> Dict[str, Path]:
    return {
        "run": run_dir / "run.json",
        "index": run_dir / "indice.json",
        "session": run_dir / "sessao_nfse.txt",
        "downloads": run_dir / "downloads",
        "logs": run_dir / "logs",
        "zip": run_dir / "_zip",
    }


def _certificate_dir(ctx: WorkerContext, cert_id: str) -> Path:
    cert_id = safe_slug(cert_id, "cert")
    cert_dir = _certificates_root(ctx) / cert_id
    return Path(safe_path_inside(str(_certificates_root(ctx)), str(cert_dir)))


def _certificate_meta_path(ctx: WorkerContext, cert_id: str) -> Path:
    return _certificate_dir(ctx, cert_id) / "meta.json"


def _public_certificate_meta(cert_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    alias = str(meta.get("alias") or meta.get("subject") or "Certificado").strip()
    not_after = str(meta.get("not_after") or "")
    label_tail = f" | vence {not_after[:10]}" if not_after else ""
    return {
        "id": cert_id,
        "source": "upload",
        "label": f"{alias}{label_tail}",
        "alias": alias,
        "subject": meta.get("subject"),
        "issuer": meta.get("issuer"),
        "thumbprint": meta.get("thumbprint"),
        "not_after": meta.get("not_after"),
        "uploaded_at": meta.get("uploaded_at"),
        "updated_at": meta.get("updated_at"),
        "size": meta.get("size"),
    }


def _list_uploaded_certificates(ctx: WorkerContext) -> List[Dict[str, Any]]:
    certs: List[Dict[str, Any]] = []
    for meta_path in sorted(_certificates_root(ctx).glob("*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = _load_json(meta_path, {})
        cert_file = meta_path.parent / "cert.pfx"
        if not cert_file.exists():
            continue
        certs.append(_public_certificate_meta(meta_path.parent.name, meta))
    return certs


def _get_uploaded_certificate(ctx: WorkerContext, cert_id: str) -> Dict[str, Any]:
    cert_id = safe_slug(cert_id, "cert")
    cert_dir = _certificate_dir(ctx, cert_id)
    meta = _load_json(cert_dir / "meta.json", {})
    cert_file = cert_dir / "cert.pfx"
    if not meta or not cert_file.exists():
        raise HTTPException(status_code=404, detail="Certificado não encontrado.")
    return {"id": cert_id, "dir": cert_dir, "file": cert_file, "meta": meta}


def _runtime_certificates() -> tuple[List[Dict[str, Any]], str | None]:
    certs: List[Dict[str, Any]] = []
    try:
        for index, cert in enumerate(list_certificates()):
            certs.append(
                {
                    "id": f"runtime:{index}",
                    "source": "runtime",
                    "index": index,
                    "label": f"{index + 1}. {cert.get('subject') or 'Certificado'} | vence {(cert.get('not_after') or '')[:10]}",
                    "subject": cert.get("subject"),
                    "thumbprint": cert.get("thumbprint"),
                    "not_after": cert.get("not_after"),
                }
            )
    except Exception as exc:
        return [], str(exc)
    return certs, None


def _validate_pfx_bytes(raw: bytes, password: str, tmp_path: Path) -> Dict[str, Any]:
    tmp_path.write_bytes(raw)
    try:
        identity = load_pfx_identity(tmp_path, password)
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass
    return {
        "subject": identity.get("subject"),
        "issuer": identity.get("issuer"),
        "thumbprint": identity.get("thumbprint"),
        "not_after": identity.get("not_after"),
    }


def _uploaded_certificate_password(cert: Dict[str, Any]) -> str:
    """Falha cedo quando uma troca de segredo tornou a senha indecifrável."""
    stored = str(cert["meta"].get("password") or "")
    password = unprotect_secret(stored)
    if stored.startswith(SECRET_ENC_PREFIX) and not password:
        raise HTTPException(
            status_code=409,
            detail=(
                "A senha deste certificado não pode mais ser aberta com o segredo atual. "
                "Envie novamente o PFX para atualizar a credencial."
            ),
        )
    try:
        load_pfx_identity(cert["file"], password)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail="A senha salva não abre o PFX. Envie novamente o certificado.",
        ) from exc
    return password


def _normalize_date(value: str) -> str:
    value = str(value or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%d/%m/%Y")
        except ValueError:
            pass
    raise HTTPException(status_code=400, detail=f"Data inválida: {value}. Use DD/MM/AAAA.")


def _date_slug(value: str) -> str:
    return datetime.strptime(_normalize_date(value), "%d/%m/%Y").strftime("%Y%m%d")


def _safe_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _normalize_cfg(payload: PortalRunPayload | PortalRetryPayload | Dict[str, Any]) -> Dict[str, Any]:
    raw = payload.model_dump() if hasattr(payload, "model_dump") else dict(payload)
    modo = str(raw.get("modo") or "recebidas").strip().lower()
    if modo in {"todos", "ambas"}:
        modo = "ambos"
    if modo not in {"recebidas", "emitidas", "ambos"}:
        raise HTTPException(status_code=400, detail="Modo deve ser recebidas, emitidas ou ambos.")

    tipo = str(raw.get("tipo_download") or "ambos").strip().lower()
    if tipo in {"todos", "both"}:
        tipo = "ambos"
    if tipo not in {"xml", "pdf", "ambos"}:
        raise HTTPException(status_code=400, detail="Arquivo deve ser xml, pdf ou ambos.")

    cert_ref = str(raw.get("cert_id") or "").strip()
    cert_id = ""
    cert_index = _safe_int(raw.get("cert_index"), 0, 0, 999)
    if cert_ref.startswith("runtime:"):
        cert_index = _safe_int(cert_ref.split(":", 1)[1], 0, 0, 999)
    elif cert_ref:
        cert_id = safe_slug(cert_ref, "cert")

    return {
        "modo": modo,
        "tipo_download": tipo,
        "data_inicial": _normalize_date(str(raw.get("data_inicial") or "")),
        "data_final": _normalize_date(str(raw.get("data_final") or "")),
        "cert_id": cert_id,
        "cert_index": cert_index,
        "renovar_sessao": bool(raw.get("renovar_sessao", True)),
        "max_items": _safe_int(raw.get("max_items"), 0, 0, 5000),
        # A capacidade e definida pelo servidor. Aceitar um numero arbitrario
        # do navegador desequilibra duas pessoas simultaneas e aumenta timeout
        # sem criar capacidade real no Modal/ThinkPad.
        "concorrencia": PORTAL_DOWNLOAD_CONCURRENCY,
        "retries": _safe_int(raw.get("retries"), 6, 1, 20),
    }


def _run_id_for(cfg: Dict[str, Any], modo: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "" if cfg["tipo_download"] == "xml" else f"-{cfg['tipo_download']}"
    cert_token = f"cert-{safe_slug(cfg.get('cert_id', ''), 'pfx')[:12]}" if cfg.get("cert_id") else f"cert{int(cfg['cert_index']):02d}"
    return (
        f"{stamp}-{modo}-"
        f"{_date_slug(cfg['data_inicial'])}-{_date_slug(cfg['data_final'])}-"
        f"{cert_token}{suffix}"
    )


def _summarize_index(index_path: Path) -> Dict[str, Any]:
    data = _load_json(index_path, {})
    items = list((data.get("items") or {}).values())
    totals = data.get("totals") or {}
    last_event = (data.get("events") or [{}])[-1]
    event_name = str(last_event.get("event") or "")
    operational_issue = None
    if event_name == "requests_index_retry_wait":
        operational_issue = {
            "code": "portal_indisponivel_temporario",
            "message": "Portal Nacional temporariamente indisponível.",
            "status_code": last_event.get("status_code"),
            "retry_in_seconds": last_event.get("delay_seconds"),
            "attempt": last_event.get("attempt"),
        }
    elif event_name == "requests_index_unavailable":
        operational_issue = {
            "code": "portal_indisponivel",
            "message": "Portal Nacional permaneceu indisponível após as tentativas automáticas.",
            "status_code": last_event.get("status_code"),
            "attempt": last_event.get("attempts"),
        }
    return {
        "status": data.get("status"),
        "portal_registros": totals.get("portal_registros") or len(items),
        "paginas": totals.get("paginas"),
        "capturados": totals.get("capturados", len(items)),
        "pendentes": totals.get("pendentes", sum(1 for item in items if item.get("status") in (None, "pendente"))),
        "executando": sum(1 for item in items if item.get("status") == "executando"),
        "baixados": totals.get("baixados", sum(1 for item in items if item.get("status") == "baixado")),
        "erros": totals.get("erros", sum(1 for item in items if item.get("status") == "erro")),
        "ultimo_evento": last_event,
        "problema_operacional": operational_issue,
        "competencias": summarize_competencias(items),
    }


def _final_run_status(summary: Dict[str, Any], code: int) -> str:
    if code != 0 and not summary:
        return f"erro_codigo_{code}"
    if code != 0:
        return "finalizado_com_erros"
    status = str(summary.get("status") or "").strip()
    if status in {"finalizado", "finalizado_parcial", "finalizado_com_erros"}:
        return status
    if summary.get("erros"):
        return "finalizado_com_erros"
    if summary.get("pendentes"):
        return "finalizado_com_erros"
    return "finalizado"


def _attach_certificate_to_run(ctx: WorkerContext, cfg: Dict[str, Any], run_dir: Path) -> Dict[str, Any]:
    cfg = dict(cfg)
    cert_id = str(cfg.get("cert_id") or "").strip()
    if cert_id:
        cert = _get_uploaded_certificate(ctx, cert_id)
        password = _uploaded_certificate_password(cert)
        cert_run_dir = run_dir / "certificado"
        cert_run_dir.mkdir(parents=True, exist_ok=True)
        pfx_path = cert_run_dir / "cert.pfx"
        password_path = cert_run_dir / "password.txt"
        shutil.copy2(cert["file"], pfx_path)
        password_path.write_text(password, encoding="utf-8")
        try:
            pfx_path.chmod(0o600)
            password_path.chmod(0o600)
        except Exception:
            pass
        cfg.update(
            {
                "cert_source": "upload",
                "cert_alias": cert["meta"].get("alias") or cert["meta"].get("subject") or cert_id,
                "cert_subject": cert["meta"].get("subject"),
                "pfx_file": str(pfx_path),
                "pfx_password_file": str(password_path),
            }
        )
        return cfg

    if not cfg.get("renovar_sessao"):
        return cfg

    if os.name != "nt":
        raise HTTPException(
            status_code=400,
            detail="Envie um certificado .pfx em Certificados antes de iniciar a run neste servidor.",
        )
    cfg["cert_source"] = "runtime"
    return cfg


def _list_files(run_dir: Path) -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    allowed = [run_dir / "downloads", run_dir / "logs"]
    for base in allowed:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.stat().st_size <= 0:
                continue
            rel = path.relative_to(run_dir).as_posix()
            files.append({"name": path.name, "relative_path": rel, "size": path.stat().st_size})
    for extra in [run_dir / "indice.json", run_dir / "run.json"]:
        if extra.exists() and extra.stat().st_size > 0:
            files.append({"name": extra.name, "relative_path": extra.relative_to(run_dir).as_posix(), "size": extra.stat().st_size})
    files.sort(key=lambda item: item["relative_path"].lower())
    return files


def _selected_competencias(values: List[str]) -> set[str]:
    selected: set[str] = set()
    for raw in values:
        value = str(raw or "").strip().lower()
        if not value or value in {"todas", "todos", "all"}:
            continue
        if value == UNKNOWN_COMPETENCIA:
            selected.add(value)
            continue
        normalized = normalize_competencia(value)
        if not normalized:
            raise HTTPException(status_code=400, detail="Competência inválida.")
        selected.add(normalized)
    return selected


def _competencia_folder(value: str) -> str:
    if value == UNKNOWN_COMPETENCIA:
        return "nao-identificada"
    year, month = value.split("-", 1)
    return f"{month}-{year}"


def _portal_download_entries(
    run_dir: Path,
    index: Dict[str, Any],
    selected: set[str],
) -> List[Dict[str, Any]]:
    downloads_root = (run_dir / "downloads").resolve()
    entries: List[Dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in (index.get("items") or {}).values():
        competence = competencia_from_item(item) or UNKNOWN_COMPETENCIA
        if selected and competence not in selected:
            continue
        file_paths: List[str] = []
        by_type = item.get("files_by_tipo") or {}
        if isinstance(by_type, dict):
            file_paths.extend(str(path) for path in by_type.values() if path)
        file_paths.extend(str(path) for path in (item.get("files") or []) if path)
        for raw_path in file_paths:
            try:
                full = Path(raw_path).resolve()
                if downloads_root != full and downloads_root not in full.parents:
                    continue
                if not full.is_file() or full.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            path_key = str(full)
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            kind = "XML" if full.suffix.lower() == ".xml" else "PDF" if full.suffix.lower() == ".pdf" else "OUTROS"
            entries.append({"path": full, "competencia": competence, "kind": kind})
    return entries


def _automatic_accumulated_entries(
    ctx: WorkerContext,
    job_id: str,
    until: date | None = None,
) -> tuple[List[Dict[str, Any]], List[Path]]:
    safe_job_id = safe_slug(job_id, "job")
    entries_by_key: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    run_dirs: List[Path] = []
    for run_path in sorted(_runs_root(ctx).glob("*/run.json"), key=lambda path: path.stat().st_mtime):
        run = _load_json(run_path, {})
        cfg = dict(run.get("config") or {})
        current_job = safe_slug(str(cfg.get("automatic_job_id") or cfg.get("cert_id") or ""), "job")
        if not cfg.get("automatic") or current_job != safe_job_id:
            continue
        created = _parse_portal_datetime(run.get("created_at"))
        if until and created and created.astimezone(PORTAL_TIMEZONE).date() > until:
            continue
        run_dir = run_path.parent
        index = _load_json(_run_paths(run_dir)["index"], {})
        modo = str(cfg.get("modo") or "").strip().lower()
        found = _portal_download_entries(run_dir, index, set())
        if found:
            run_dirs.append(run_dir)
        for entry in found:
            key = (modo, str(entry.get("kind") or ""), entry["path"].name.casefold())
            entries_by_key[key] = {**entry, "modo": modo}
    return list(entries_by_key.values()), run_dirs


def _modo_folder(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "recebidas":
        return "Recebidas"
    if normalized == "emitidas":
        return "Emitidas"
    raise HTTPException(status_code=400, detail="Tipo de nota inválido para o ZIP.")


def _zip_arcname(
    entry: Dict[str, Any],
    separate_competencias: bool,
    separate_modos: bool = False,
) -> str:
    parts = []
    if separate_modos:
        parts.append(_modo_folder(entry.get("modo")))
    if separate_competencias:
        parts.append(_competencia_folder(entry["competencia"]))
    parts.extend([entry["kind"], entry["path"].name])
    return "/".join(parts)


def _write_portal_zip(
    zip_path: Path,
    entries: List[Dict[str, Any]],
    *,
    separate_competencias: bool,
    separate_modos: bool = False,
) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        used_names: set[str] = set()
        for entry in entries:
            arcname = _zip_arcname(entry, separate_competencias, separate_modos)
            if arcname in used_names:
                parent = Path(arcname).parent.as_posix()
                stem = Path(arcname).stem
                suffix = Path(arcname).suffix
                counter = 2
                candidate = f"{parent}/{stem}-{counter}{suffix}"
                while candidate in used_names:
                    counter += 1
                    candidate = f"{parent}/{stem}-{counter}{suffix}"
                arcname = candidate
            used_names.add(arcname)
            zf.write(entry["path"], arcname=arcname)


def _runtime_key(ctx: WorkerContext) -> str:
    return f"{safe_slug(ctx.company_id)}:{safe_slug(ctx.user_id)}"


def _active_runtime(ctx: WorkerContext) -> Dict[str, Any] | None:
    key = _runtime_key(ctx)
    with _LOCK:
        runtime = _RUNTIME.get(key)
        if runtime and runtime.get("thread") and runtime["thread"].is_alive():
            return runtime
        if runtime:
            _RUNTIME.pop(key, None)
    return None


def _update_run(run_dir: Path, **updates: Any) -> Dict[str, Any]:
    paths = _run_paths(run_dir)
    data = _load_json(paths["run"], {})
    data.update(updates)
    data["updated_at"] = _now_iso()
    if paths["index"].exists():
        data["summary"] = _summarize_index(paths["index"])
    _save_json(paths["run"], data)
    return data


def _compact_run(ctx: WorkerContext, run_dir: Path, *, include_files: bool = True) -> Dict[str, Any]:
    paths = _run_paths(run_dir)
    data = _load_json(paths["run"], {})
    if data.get("status") == "rodando" and not _active_runtime(ctx):
        data = _update_run(run_dir, status="interrompida", last_error=data.get("last_error") or "Processo não está mais ativo.")
    if paths["index"].exists():
        data["summary"] = _summarize_index(paths["index"])
    if include_files:
        data["files"] = _list_files(run_dir)
    return data


def _create_run(ctx: WorkerContext, cfg: Dict[str, Any], modo: str) -> Path:
    cfg = dict(cfg)
    run_id = _run_id_for(cfg, modo)
    run_dir = _runs_root(ctx) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    paths = _run_paths(run_dir)
    for path in paths.values():
        if path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)

    if not cfg.get("renovar_sessao"):
        saved_session = _session_path(ctx)
        if not saved_session.exists():
            raise HTTPException(status_code=400, detail="Nenhuma sessão salva para este colaborador. Gere ou importe uma sessão primeiro.")
        shutil.copy2(saved_session, paths["session"])
    else:
        cfg = _attach_certificate_to_run(ctx, cfg, run_dir)

    _save_json(
        paths["run"],
        {
            "run_id": run_id,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "status": "criada",
            "app": "portal_nacional",
            "config": {**cfg, "modo": modo},
            "paths": {key: str(value) for key, value in paths.items() if key != "zip"},
            "summary": {},
            "files": [],
        },
    )
    return run_dir


def _build_command(cfg: Dict[str, Any], run_dir: Path, retry_only: bool) -> List[str]:
    paths = _run_paths(run_dir)
    cmd = [
        sys.executable,
        "-u",
        str(AUTOMATION_SCRIPT),
        "--modo",
        cfg["modo"],
        "--session",
        str(paths["session"]),
        "--download-dir",
        str(paths["downloads"]),
        "--tipo-download",
        cfg["tipo_download"],
        "--index",
        str(paths["index"]),
        "--cert-index",
        str(cfg["cert_index"]),
        "--solver-url",
        DEFAULT_SOLVER_URL,
        "--concorrencia",
        str(cfg["concorrencia"]),
        "--retries",
        str(cfg["retries"]),
        "--data-inicial",
        cfg["data_inicial"],
        "--data-final",
        cfg["data_final"],
    ]
    if cfg.get("pfx_file"):
        cmd.extend(["--pfx-file", str(cfg["pfx_file"])])
        if cfg.get("pfx_password_file"):
            cmd.extend(["--pfx-password-file", str(cfg["pfx_password_file"])])
    if cfg.get("max_items"):
        cmd.extend(["--max", str(cfg["max_items"])])
    if not retry_only:
        cmd.append("--recriar-index")
        if cfg.get("renovar_sessao"):
            cmd.append("--renovar-inicio")
    return cmd


def _run_process(scope: str, run_dir: Path, cfg: Dict[str, Any], retry_only: bool) -> int:
    paths = _run_paths(run_dir)
    log_path = paths["logs"] / f"automacao_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    cmd = _build_command(cfg, run_dir, retry_only)
    _update_run(
        run_dir,
        status="rodando",
        last_error=None,
        last_command=cmd,
        last_log=str(log_path),
    )
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PORTAL_NACIONAL_SOLVER_URL", DEFAULT_SOLVER_URL)
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        with _LOCK:
            runtime = _RUNTIME.get(scope)
            if runtime is not None:
                runtime["process"] = proc
                runtime["run_id"] = run_dir.name
        assert proc.stdout is not None
        for line in proc.stdout:
            log_file.write(line)
            log_file.flush()
        return proc.wait()


def _sequence_worker(scope: str, jobs: List[Path], retry_only: bool = False) -> None:
    try:
        for run_dir in jobs:
            run = _load_json(run_dir / "run.json", {})
            cfg = dict(run.get("config") or {})
            with _LOCK:
                runtime = _RUNTIME.get(scope)
                if runtime and runtime.get("stop_requested"):
                    _update_run(run_dir, status="parado", last_error="Parado antes de iniciar.")
                    break
            try:
                code = _run_process(scope, run_dir, cfg, retry_only)
                summary = _summarize_index(run_dir / "indice.json") if (run_dir / "indice.json").exists() else {}
                status = _final_run_status(summary, code)
                with _LOCK:
                    runtime = _RUNTIME.get(scope)
                    if runtime and runtime.get("stop_requested"):
                        status = "parado"
                _update_run(run_dir, status=status)
                if status.startswith("erro_codigo"):
                    break
            except Exception as exc:
                _update_run(run_dir, status="erro", last_error=str(exc))
                break
    finally:
        with _LOCK:
            _RUNTIME.pop(scope, None)


def _start_jobs(ctx: WorkerContext, run_dirs: List[Path], retry_only: bool = False) -> None:
    if _active_runtime(ctx):
        raise HTTPException(status_code=409, detail="Já existe uma execução do Portal Nacional rodando para este colaborador.")
    scope = _runtime_key(ctx)
    thread = threading.Thread(target=_sequence_worker, args=(scope, run_dirs, retry_only), daemon=True)
    with _LOCK:
        _RUNTIME[scope] = {"thread": thread, "process": None, "run_id": run_dirs[0].name, "stop_requested": False}
    thread.start()


def _start_automatic_job(ctx: WorkerContext, job: Dict[str, Any], *, reason: str) -> List[Path]:
    cert_id = safe_slug(str(job.get("cert_id") or ""), "cert")
    cert = _get_uploaded_certificate(ctx, cert_id)
    data_inicial, data_final = _automatic_capture_range(job)
    cfg = _normalize_cfg(
        {
            "modo": job.get("modo") or "ambos",
            "tipo_download": job.get("tipo_download") or "ambos",
            "data_inicial": data_inicial,
            "data_final": data_final,
            "cert_id": cert_id,
            "renovar_sessao": True,
            "max_items": 0,
            "retries": 8,
        }
    )
    cfg.update(
        {
            "automatic": True,
            "automatic_job_id": str(job.get("id") or cert_id),
            "automatic_reason": reason,
            "automatic_retention_days": PORTAL_AUTOMATIC_RETENTION_DAYS,
            "certificate_alias": str(cert["meta"].get("alias") or "Certificado"),
        }
    )
    modos = ["recebidas", "emitidas"] if cfg["modo"] == "ambos" else [cfg["modo"]]
    run_dirs = [_create_run(ctx, cfg, modo) for modo in modos]
    _start_jobs(ctx, run_dirs, retry_only=False)
    return run_dirs


def _cleanup_automatic_runs(ctx: WorkerContext, *, now: datetime | None = None) -> int:
    current = now or datetime.now(PORTAL_TIMEZONE)
    cutoff = current - timedelta(days=PORTAL_AUTOMATIC_RETENTION_DAYS)
    active = _active_runtime(ctx)
    removed = 0
    for run_path in list(_runs_root(ctx).glob("*/run.json")):
        run = _load_json(run_path, {})
        cfg = dict(run.get("config") or {})
        if not cfg.get("automatic"):
            continue
        if active and active.get("run_id") == run_path.parent.name:
            continue
        created = _parse_portal_datetime(run.get("created_at"))
        if created and created < cutoff:
            target = run_path.parent.resolve()
            root = _runs_root(ctx).resolve()
            if root in target.parents:
                shutil.rmtree(target)
                removed += 1
    return removed


def _any_portal_runtime_active() -> bool:
    with _LOCK:
        stale = [key for key, runtime in _RUNTIME.items() if not runtime.get("thread") or not runtime["thread"].is_alive()]
        for key in stale:
            _RUNTIME.pop(key, None)
        return bool(_RUNTIME)


def _record_automatic_started(
    ctx: WorkerContext,
    state: Dict[str, Any],
    job: Dict[str, Any],
    run_dirs: List[Path],
    *,
    current: datetime,
) -> None:
    run_cfg = dict((_load_json(run_dirs[0] / "run.json", {}).get("config") or {}))
    job["last_started_at"] = current.isoformat(timespec="seconds")
    job["last_capture_start"] = datetime.strptime(run_cfg["data_inicial"], "%d/%m/%Y").date().isoformat()
    job["last_capture_end"] = datetime.strptime(run_cfg["data_final"], "%d/%m/%Y").date().isoformat()
    job["last_run_ids"] = [path.name for path in run_dirs]
    job["last_status"] = "rodando"
    job["last_error"] = None
    job["next_run_at"] = _next_daily_schedule(
        int(job.get("schedule_minute") or 0),
        after=current,
        force_next_day=True,
    ).isoformat(timespec="seconds")
    _save_automatic_state(ctx, state)


def _run_automatic_scheduler_cycle(now: datetime | None = None) -> Dict[str, Any]:
    current = now or datetime.now(PORTAL_TIMEZONE)
    _rebalance_automatic_schedules(current)
    records = _automatic_records()
    scopes_cleaned: set[str] = set()
    for ctx, state, _job in records:
        scope = _runtime_key(ctx)
        if scope not in scopes_cleaned:
            _reconcile_automatic_state(ctx, state)
            _cleanup_automatic_runs(ctx, now=current)
            scopes_cleaned.add(scope)

    if _any_portal_runtime_active():
        return {"started": False, "reason": "portal_busy"}

    due = []
    for ctx, state, job in _automatic_records():
        if not bool(job.get("enabled", True)):
            continue
        next_run = _parse_portal_datetime(job.get("next_run_at"))
        if next_run and next_run <= current:
            due.append((next_run, _runtime_key(ctx), str(job.get("id") or ""), ctx, state, job))
    if not due:
        return {"started": False, "reason": "nothing_due"}

    _next_run, _scope, _job_id, ctx, state, job = min(due, key=lambda item: item[:3])
    try:
        run_dirs = _start_automatic_job(ctx, job, reason="schedule")
        _record_automatic_started(ctx, state, job, run_dirs, current=current)
        return {"started": True, "scope": _runtime_key(ctx), "job_id": job.get("id"), "run_ids": job["last_run_ids"]}
    except Exception as exc:
        job["last_status"] = "erro_ao_iniciar"
        job["last_error"] = str(getattr(exc, "detail", None) or exc)
        job["next_run_at"] = (current + timedelta(minutes=30)).isoformat(timespec="seconds")
        _save_automatic_state(ctx, state)
        return {"started": False, "reason": "start_failed", "scope": _runtime_key(ctx), "job_id": job.get("id")}


def _automatic_scheduler_loop() -> None:
    while not _AUTOMATIC_STOP.is_set():
        try:
            result = _run_automatic_scheduler_cycle()
            if result.get("started"):
                print(f"[portal-automatic] captura iniciada scope={result.get('scope')} job={result.get('job_id')}", flush=True)
        except Exception as exc:
            print(f"[portal-automatic] ciclo falhou: {type(exc).__name__}: {exc}", flush=True)
        _AUTOMATIC_STOP.wait(PORTAL_AUTOMATIC_POLL_SECONDS)


def start_portal_automatic_scheduler() -> None:
    global _AUTOMATIC_THREAD
    with _AUTOMATIC_LOCK:
        if _AUTOMATIC_THREAD and _AUTOMATIC_THREAD.is_alive():
            return
        _AUTOMATIC_STOP.clear()
        _AUTOMATIC_THREAD = threading.Thread(
            target=_automatic_scheduler_loop,
            name="portal-automatic-scheduler",
            daemon=True,
        )
        _AUTOMATIC_THREAD.start()


def stop_portal_automatic_scheduler() -> None:
    global _AUTOMATIC_THREAD
    _AUTOMATIC_STOP.set()
    thread = _AUTOMATIC_THREAD
    if thread and thread.is_alive():
        thread.join(timeout=3)
    _AUTOMATIC_THREAD = None


def _session_status(ctx: WorkerContext) -> Dict[str, Any]:
    path = _session_path(ctx)
    data = _load_json(path, {}) if path.exists() else {}
    cert = data.get("certificate") or {}
    cookies = data.get("cookies") or []
    return {
        "exists": path.exists(),
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds") if path.exists() else None,
        "certificate_subject": cert.get("subject"),
        "certificate_thumbprint": cert.get("thumbprint"),
        "cookies_count": len(cookies) if isinstance(cookies, list) else 0,
        "target_looks_logged_in": data.get("target_looks_logged_in"),
    }


def _public_automatic_state(ctx: WorkerContext) -> Dict[str, Any]:
    with _AUTOMATIC_LOCK:
        state = _load_automatic_state(ctx)
        _reconcile_automatic_state(ctx, state)
        certs = {cert["id"]: cert for cert in _list_uploaded_certificates(ctx)}
        jobs = []
        for job in state.get("jobs") or []:
            cert = certs.get(str(job.get("cert_id") or ""), {})
            minute = max(0, min(1439, int(job.get("schedule_minute") or 0)))
            jobs.append(
                {
                    "id": str(job.get("id") or ""),
                    "cert_id": str(job.get("cert_id") or ""),
                    "certificate_alias": cert.get("alias") or job.get("certificate_alias") or "Certificado",
                    "certificate_available": bool(cert),
                    "enabled": bool(job.get("enabled", True)),
                    "modo": str(job.get("modo") or "ambos"),
                    "tipo_download": str(job.get("tipo_download") or "ambos"),
                    "data_inicial": job.get("data_inicial"),
                    "schedule_minute": minute,
                    "schedule_label": f"{minute // 60:02d}:{minute % 60:02d}",
                    "next_run_at": job.get("next_run_at"),
                    "last_started_at": job.get("last_started_at"),
                    "last_completed_at": job.get("last_completed_at"),
                    "last_status": job.get("last_status") or "aguardando",
                    "last_error": job.get("last_error"),
                    "last_run_ids": list(job.get("last_run_ids") or []),
                    "last_capture_start": job.get("last_capture_start"),
                    "last_capture_end": job.get("last_capture_end"),
                }
            )
        return {
            "enabled_jobs": sum(1 for job in jobs if job["enabled"]),
            "jobs": jobs,
            "retention_days": PORTAL_AUTOMATIC_RETENTION_DAYS,
            "initial_lookback_days": PORTAL_AUTOMATIC_INITIAL_LOOKBACK_DAYS,
            "overlap_days": PORTAL_AUTOMATIC_OVERLAP_DAYS,
            "timezone": str(PORTAL_TIMEZONE),
        }


@router.get("/state")
async def portal_state(ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    runtime_certificates, certificate_error = await asyncio.to_thread(_runtime_certificates)
    certificates = [*_list_uploaded_certificates(ctx), *runtime_certificates]

    return {
        "ok": True,
        "solver_url": DEFAULT_SOLVER_URL,
        "storage_root": str(_portal_root(ctx)),
        "session": _session_status(ctx),
        "certificates": certificates,
        "certificate_error": certificate_error,
        "automatic": _public_automatic_state(ctx),
        "active_run_id": (_active_runtime(ctx) or {}).get("run_id"),
        "runs": [
            _compact_run(ctx, path.parent, include_files=False)
            for path in sorted(_runs_root(ctx).glob("*/run.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        ],
        "limits": {
            "concorrencia": PORTAL_DOWNLOAD_CONCURRENCY,
            "concorrencia_automatica": True,
            "max_items_max": 5000,
        },
    }


@router.post("/certificates")
async def upload_certificate(
    file: UploadFile = File(...),
    password: str = Form(default=""),
    alias: str = Form(default=""),
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    filename = Path(file.filename or "certificado.pfx").name
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pfx", ".p12"}:
        raise HTTPException(status_code=400, detail="Envie um arquivo .pfx ou .p12.")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Arquivo vazio.")
    if len(raw) > 12 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Certificado muito grande.")

    temp_path = _certificates_root(ctx) / f"upload_{os.getpid()}_{threading.get_ident()}.tmp"
    try:
        cert_info = await asyncio.to_thread(_validate_pfx_bytes, raw, password or "", temp_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cert_id_base = safe_slug(f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{(cert_info.get('thumbprint') or '')[-10:]}", "cert")
    cert_id = cert_id_base
    for i in range(2, 100):
        if not _certificate_dir(ctx, cert_id).exists():
            break
        cert_id = safe_slug(f"{cert_id_base}_{i}", "cert")
    cert_dir = _certificate_dir(ctx, cert_id)
    cert_dir.mkdir(parents=True, exist_ok=False)
    cert_file = cert_dir / "cert.pfx"
    cert_file.write_bytes(raw)
    meta = {
        "id": cert_id,
        "alias": str(alias or "").strip() or Path(filename).stem,
        "filename": filename,
        "size": len(raw),
        "password": protect_secret(password or ""),
        "uploaded_at": _now_iso(),
        "updated_at": _now_iso(),
        **cert_info,
    }
    _save_json(cert_dir / "meta.json", meta)
    return {"ok": True, "certificate": _public_certificate_meta(cert_id, meta)}


@router.delete("/certificates/{cert_id}")
async def delete_certificate(cert_id: str, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    cert = _get_uploaded_certificate(ctx, cert_id)
    shutil.rmtree(cert["dir"], ignore_errors=True)
    with _AUTOMATIC_LOCK:
        state = _load_automatic_state(ctx)
        changed = False
        for job in state["jobs"]:
            if str(job.get("cert_id") or "") == cert["id"]:
                job["enabled"] = False
                job["next_run_at"] = None
                job["last_status"] = "certificado_removido"
                job["last_error"] = "Certificado removido. Cadastre-o novamente para reativar a captura."
                changed = True
        if changed:
            _save_automatic_state(ctx, state)
    return {"ok": True}


@router.put("/certificates/{cert_id}")
async def update_certificate(
    cert_id: str,
    file: UploadFile | None = File(default=None),
    password: str = Form(default=""),
    password_changed: bool = Form(default=False),
    alias: str = Form(default=""),
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    cert = _get_uploaded_certificate(ctx, cert_id)
    meta = dict(cert["meta"])
    raw = await file.read() if file is not None else b""
    if file is not None:
        filename = Path(file.filename or "certificado.pfx").name
        if Path(filename).suffix.lower() not in {".pfx", ".p12"}:
            raise HTTPException(status_code=400, detail="Envie um arquivo .pfx ou .p12.")
        if not raw or len(raw) > 12 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Certificado vazio ou maior que 12 MB.")
        temp_path = cert["dir"] / f"replace_{os.getpid()}_{threading.get_ident()}.tmp"
        try:
            cert_info = await asyncio.to_thread(_validate_pfx_bytes, raw, password or "", temp_path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        replacement = cert["dir"] / "cert.pfx.new"
        replacement.write_bytes(raw)
        os.replace(replacement, cert["file"])
        meta.update(
            {
                "filename": filename,
                "size": len(raw),
                "password": protect_secret(password or ""),
                **cert_info,
            }
        )
    elif password_changed:
        try:
            cert_info = await asyncio.to_thread(load_pfx_identity, cert["file"], password or "")
        except Exception as exc:
            raise HTTPException(status_code=400, detail="A nova senha não abre este certificado.") from exc
        meta["password"] = protect_secret(password or "")
        meta.update({key: cert_info.get(key) for key in ("subject", "issuer", "thumbprint", "not_after")})

    clean_alias = str(alias or "").strip()
    if clean_alias:
        meta["alias"] = clean_alias[:120]
    meta["updated_at"] = _now_iso()
    _save_json(cert["dir"] / "meta.json", meta)
    return {"ok": True, "certificate": _public_certificate_meta(cert["id"], meta)}


@router.post("/sessions/import")
async def import_session(payload: PortalSessionImportPayload, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    session = dict(payload.session or {})
    cookies = session.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise HTTPException(status_code=400, detail="Sessão inválida: cookies ausentes.")
    _save_json(_session_path(ctx), session)
    return {"ok": True, "session": _session_status(ctx)}


@router.get("/automatic")
async def get_portal_automatic(ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    return {"ok": True, "automatic": _public_automatic_state(ctx)}


@router.get("/automatic/download")
async def download_portal_automatic_accumulated(
    job_id: str = Query(...),
    ate: str = Query(default=""),
    ctx: WorkerContext = Depends(get_worker_context),
):
    cutoff = None
    if str(ate or "").strip():
        try:
            cutoff = datetime.strptime(str(ate).strip(), "%Y-%m-%d").date()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Data limite inválida.") from exc
    entries, run_dirs = _automatic_accumulated_entries(ctx, job_id, cutoff)
    if not entries or not run_dirs:
        raise HTTPException(status_code=404, detail="Nenhum arquivo automático disponível até esta data.")
    zip_dir = _run_paths(run_dirs[-1])["zip"]
    zip_dir.mkdir(parents=True, exist_ok=True)
    cutoff_slug = cutoff.isoformat() if cutoff else "tudo"
    zip_path = zip_dir / f"portal-automatico-{safe_slug(job_id, 'job')}-ate-{cutoff_slug}.zip"
    _write_portal_zip(zip_path, entries, separate_competencias=True, separate_modos=True)
    return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")


@router.post("/automatic")
async def save_portal_automatic(
    payload: PortalAutomaticPayload,
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    cert_id = safe_slug(payload.cert_id, "cert")
    cert = _get_uploaded_certificate(ctx, cert_id)
    current = datetime.now(PORTAL_TIMEZONE)
    try:
        configured_start = datetime.strptime(str(payload.data_inicial or "").strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Data inicial inválida.") from exc
    if configured_start > current.date():
        raise HTTPException(status_code=400, detail="Data inicial não pode estar no futuro.")
    today = current.strftime("%d/%m/%Y")
    normalized = _normalize_cfg(
        {
            "cert_id": cert_id,
            "modo": payload.modo,
            "tipo_download": "ambos",
            "data_inicial": today,
            "data_final": today,
        }
    )
    with _AUTOMATIC_LOCK:
        state = _load_automatic_state(ctx)
        job = next((item for item in state["jobs"] if str(item.get("id") or "") == cert_id), None)
        if job is None:
            job = {
                "id": cert_id,
                "cert_id": cert_id,
                "created_at": _now_iso(),
                "next_run_at": datetime.now(PORTAL_TIMEZONE).isoformat(timespec="seconds"),
                "last_status": "aguardando",
                "last_run_ids": [],
            }
            state["jobs"].append(job)
        previous_start = str(job.get("data_inicial") or "")
        job.update(
            {
                "cert_id": cert_id,
                "certificate_alias": str(cert["meta"].get("alias") or "Certificado"),
                "modo": normalized["modo"],
                "tipo_download": "ambos",
                "data_inicial": configured_start.isoformat(),
                "enabled": True,
                "retention_days": PORTAL_AUTOMATIC_RETENTION_DAYS,
                "updated_at": _now_iso(),
            }
        )
        if previous_start != job["data_inicial"]:
            job.pop("last_success_date", None)
            job["last_status"] = "aguardando"
            job["last_error"] = None
            job["next_run_at"] = current.isoformat(timespec="seconds")
        elif not job.get("next_run_at"):
            job["next_run_at"] = current.isoformat(timespec="seconds")
        _save_automatic_state(ctx, state)
        _rebalance_automatic_schedules()
    return {"ok": True, "automatic": _public_automatic_state(ctx)}


@router.delete("/automatic/{job_id}")
async def delete_portal_automatic(job_id: str, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    safe_id = safe_slug(job_id, "job")
    with _AUTOMATIC_LOCK:
        state = _load_automatic_state(ctx)
        kept = [job for job in state["jobs"] if str(job.get("id") or "") != safe_id]
        if len(kept) == len(state["jobs"]):
            raise HTTPException(status_code=404, detail="Automação não encontrada.")
        state["jobs"] = kept
        _save_automatic_state(ctx, state)
        _rebalance_automatic_schedules()
    return {"ok": True, "automatic": _public_automatic_state(ctx)}


@router.post("/automatic/{job_id}/capture-now")
async def capture_portal_automatic_now(
    job_id: str,
    ctx: WorkerContext = Depends(get_worker_context),
) -> Dict[str, Any]:
    if _active_runtime(ctx):
        raise HTTPException(status_code=409, detail="Já existe uma captura do Portal rodando para este colaborador.")
    safe_id = safe_slug(job_id, "job")
    with _AUTOMATIC_LOCK:
        state = _load_automatic_state(ctx)
        job = next((item for item in state["jobs"] if str(item.get("id") or "") == safe_id), None)
        if job is None:
            raise HTTPException(status_code=404, detail="Automação não encontrada.")
        current = datetime.now(PORTAL_TIMEZONE)
        run_dirs = _start_automatic_job(ctx, job, reason="capture_now")
        _record_automatic_started(ctx, state, job, run_dirs, current=current)
    return {"ok": True, "run_id": run_dirs[0].name, "run_ids": [path.name for path in run_dirs]}


@router.post("/runs")
async def start_portal_run(payload: PortalRunPayload, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    cfg = _normalize_cfg(payload)
    start = datetime.strptime(cfg["data_inicial"], "%d/%m/%Y")
    end = datetime.strptime(cfg["data_final"], "%d/%m/%Y")
    if start > end:
        raise HTTPException(status_code=400, detail="Data inicial não pode ser maior que data final.")
    modos = ["recebidas", "emitidas"] if cfg["modo"] == "ambos" else [cfg["modo"]]
    run_dirs = [_create_run(ctx, cfg, modo) for modo in modos]
    _start_jobs(ctx, run_dirs, retry_only=False)
    return {"ok": True, "run_id": run_dirs[0].name, "run_ids": [path.name for path in run_dirs]}


@router.get("/runs")
async def list_portal_runs(ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    return {
        "ok": True,
        "active_run_id": (_active_runtime(ctx) or {}).get("run_id"),
        "runs": [
            _compact_run(ctx, path.parent, include_files=False)
            for path in sorted(_runs_root(ctx).glob("*/run.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        ],
    }


@router.get("/runs/{run_id}")
async def get_portal_run(run_id: str, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    run_dir = _safe_run_dir(ctx, run_id)
    return {"ok": True, "run": _compact_run(ctx, run_dir), "index": _load_json(run_dir / "indice.json", {})}


@router.post("/runs/{run_id}/retry")
async def retry_portal_run(run_id: str, payload: PortalRetryPayload, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    run_dir = _safe_run_dir(ctx, run_id)
    run = _load_json(run_dir / "run.json", {})
    cfg = dict(run.get("config") or {})
    override = _retry_config(cfg, payload)
    override["modo"] = cfg.get("modo") or override["modo"]
    override["renovar_sessao"] = False
    override = _attach_certificate_to_run(ctx, override, run_dir)
    _update_run(run_dir, config=override)
    _start_jobs(ctx, [run_dir], retry_only=True)
    return {"ok": True, "run_id": run_dir.name}


@router.post("/runs/continue")
async def continue_portal_runs(payload: PortalContinuePayload, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    run_ids = list(dict.fromkeys(str(value or "").strip() for value in payload.run_ids))
    run_ids = [value for value in run_ids if value]
    if not run_ids:
        raise HTTPException(status_code=400, detail="Selecione ao menos uma run para continuar.")
    if len(run_ids) > 4:
        raise HTTPException(status_code=400, detail="No máximo quatro partes podem ser continuadas juntas.")

    run_dirs: List[Path] = []
    for run_id in run_ids:
        run_dir = _safe_run_dir(ctx, run_id)
        run = _load_json(run_dir / "run.json", {})
        cfg = dict(run.get("config") or {})
        override = _retry_config(cfg, payload)
        override["modo"] = cfg.get("modo") or override["modo"]
        override["renovar_sessao"] = False
        override = _attach_certificate_to_run(ctx, override, run_dir)
        _update_run(run_dir, config=override, last_error=None)
        run_dirs.append(run_dir)

    _start_jobs(ctx, run_dirs, retry_only=True)
    return {"ok": True, "run_id": run_dirs[0].name, "run_ids": [path.name for path in run_dirs]}


@router.post("/runs/{run_id}/stop")
async def stop_portal_run(run_id: str, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    runtime = _active_runtime(ctx)
    if not runtime or runtime.get("run_id") != run_id:
        return {"ok": True, "stopped": False, "message": "Run não está ativa."}
    with _LOCK:
        runtime["stop_requested"] = True
        proc = runtime.get("process")
    if proc and proc.poll() is None:
        proc.terminate()
        for _ in range(20):
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        if proc.poll() is None:
            proc.kill()
    try:
        _update_run(_safe_run_dir(ctx, run_id), status="parado", last_error="Parado pelo usuário.")
    except Exception:
        pass
    return {"ok": True, "stopped": True}

@router.delete("/runs/{run_id}")
async def delete_portal_run(run_id: str, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    _delete_run_dir(ctx, run_id)
    return {"ok": True, "run_id": run_id}


@router.get("/download")
async def download_portal_run_group(
    run_id: List[str] = Query(default=[]),
    competencia: List[str] = Query(default=[]),
    ctx: WorkerContext = Depends(get_worker_context),
):
    run_ids = list(dict.fromkeys(str(value or "").strip() for value in run_id))
    run_ids = [value for value in run_ids if value]
    if not run_ids:
        raise HTTPException(status_code=400, detail="Selecione ao menos uma run.")
    if len(run_ids) > 4:
        raise HTTPException(status_code=400, detail="No máximo quatro partes podem ser baixadas juntas.")

    selected = _selected_competencias(competencia)
    entries: List[Dict[str, Any]] = []
    run_dirs: List[Path] = []
    modos: set[str] = set()
    for current_id in run_ids:
        run_dir = _safe_run_dir(ctx, current_id)
        run_dirs.append(run_dir)
        paths = _run_paths(run_dir)
        run = _load_json(paths["run"], {})
        modo = str((run.get("config") or {}).get("modo") or "").strip().lower()
        _modo_folder(modo)
        modos.add(modo)
        index = _load_json(paths["index"], {})
        for entry in _portal_download_entries(run_dir, index, selected):
            entries.append({**entry, "modo": modo})

    if not entries:
        raise HTTPException(status_code=404, detail="Nenhum arquivo disponível para a competência selecionada.")

    paths = _run_paths(run_dirs[0])
    paths["zip"].mkdir(parents=True, exist_ok=True)
    selection_slug = "-".join(sorted(selected)) if selected else "todas"
    mode_slug = "recebidas-emitidas" if modos == {"recebidas", "emitidas"} else "-".join(sorted(modos))
    zip_path = paths["zip"] / f"portal-{safe_slug(mode_slug)}-{safe_slug(selection_slug, 'todas')}.zip"
    _write_portal_zip(
        zip_path,
        entries,
        separate_competencias=True,
        separate_modos=True,
    )
    return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")


@router.get("/runs/{run_id}/download")
async def download_portal_run(
    run_id: str,
    competencia: List[str] = Query(default=[]),
    ctx: WorkerContext = Depends(get_worker_context),
):
    run_dir = _safe_run_dir(ctx, run_id)
    paths = _run_paths(run_dir)
    selected = _selected_competencias(competencia)
    index = _load_json(paths["index"], {})
    entries = _portal_download_entries(run_dir, index, selected)
    if not entries:
        raise HTTPException(status_code=404, detail="Nenhum arquivo disponível para a competência selecionada.")
    present_competencias = {entry["competencia"] for entry in entries}
    separate_competencias = len(present_competencias) > 1
    paths["zip"].mkdir(parents=True, exist_ok=True)
    selection_slug = "-".join(sorted(selected)) if selected else "todas"
    zip_path = paths["zip"] / f"{run_dir.name}-{safe_slug(selection_slug, 'todas')}.zip"
    _write_portal_zip(zip_path, entries, separate_competencias=separate_competencias)
    return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")


@router.get("/runs/{run_id}/file")
async def download_portal_file(
    run_id: str,
    path: str = Query(...),
    ctx: WorkerContext = Depends(get_worker_context),
):
    run_dir = _safe_run_dir(ctx, run_id)
    full = Path(safe_path_inside(str(run_dir), str(run_dir / path)))
    if not full.is_file() or full.stat().st_size <= 0:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    allowed_roots = [run_dir / "downloads", run_dir / "logs"]
    allowed_exact = {str((run_dir / "indice.json").resolve()), str((run_dir / "run.json").resolve())}
    is_allowed_tree = any(str(full).startswith(str(root.resolve())) for root in allowed_roots)
    if not is_allowed_tree and str(full.resolve()) not in allowed_exact:
        raise HTTPException(status_code=400, detail="Caminho inválido.")
    return FileResponse(full, filename=full.name)
