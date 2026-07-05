import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from db import OUTPUT_ROOT
from domain import WorkerContext, get_worker_context, member_output_root, safe_path_inside, safe_slug
from portal_nacional_session import list_certificates


BASE_DIR = Path(__file__).resolve().parent
AUTOMATION_SCRIPT = BASE_DIR / "portal_nacional_automation.py"
DEFAULT_SOLVER_URL = os.getenv(
    "PORTAL_NACIONAL_SOLVER_URL",
    "https://jorhinhogames--prumo-portal-nacional-solver-solver-server.modal.run/solve",
).strip()

router = APIRouter(prefix="/api/portal-nacional", tags=["portal-nacional"])

_LOCK = threading.RLock()
_RUNTIME: Dict[str, Dict[str, Any]] = {}


class PortalRunPayload(BaseModel):
    modo: str = Field(default="recebidas")
    tipo_download: str = Field(default="ambos")
    data_inicial: str
    data_final: str
    cert_index: int = 0
    renovar_sessao: bool = True
    max_items: int = 0
    concorrencia: int = 4
    retries: int = 6


class PortalRetryPayload(BaseModel):
    tipo_download: str = Field(default="ambos")
    max_items: int = 0
    concorrencia: int = 4
    retries: int = 6


class PortalSessionImportPayload(BaseModel):
    session: Dict[str, Any]


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


def _runs_root(ctx: WorkerContext) -> Path:
    root = _portal_root(ctx) / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _session_path(ctx: WorkerContext) -> Path:
    sessions = _portal_root(ctx) / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    return sessions / "sessao_nfse.txt"


def _safe_run_dir(ctx: WorkerContext, run_id: str) -> Path:
    run_id = safe_slug(run_id, "run")
    run_dir = _runs_root(ctx) / run_id
    if not (run_dir / "run.json").exists():
        raise HTTPException(status_code=404, detail="Run não encontrada.")
    return Path(safe_path_inside(str(_runs_root(ctx)), str(run_dir)))


def _run_paths(run_dir: Path) -> Dict[str, Path]:
    return {
        "run": run_dir / "run.json",
        "index": run_dir / "indice.json",
        "session": run_dir / "sessao_nfse.txt",
        "downloads": run_dir / "downloads",
        "logs": run_dir / "logs",
        "zip": run_dir / "_zip",
    }


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

    return {
        "modo": modo,
        "tipo_download": tipo,
        "data_inicial": _normalize_date(str(raw.get("data_inicial") or "")),
        "data_final": _normalize_date(str(raw.get("data_final") or "")),
        "cert_index": _safe_int(raw.get("cert_index"), 0, 0, 999),
        "renovar_sessao": bool(raw.get("renovar_sessao", True)),
        "max_items": _safe_int(raw.get("max_items"), 0, 0, 5000),
        "concorrencia": _safe_int(raw.get("concorrencia"), 4, 1, 16),
        "retries": _safe_int(raw.get("retries"), 6, 1, 20),
    }


def _run_id_for(cfg: Dict[str, Any], modo: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "" if cfg["tipo_download"] == "xml" else f"-{cfg['tipo_download']}"
    return (
        f"{stamp}-{modo}-"
        f"{_date_slug(cfg['data_inicial'])}-{_date_slug(cfg['data_final'])}-"
        f"cert{int(cfg['cert_index']):02d}{suffix}"
    )


def _summarize_index(index_path: Path) -> Dict[str, Any]:
    data = _load_json(index_path, {})
    items = list((data.get("items") or {}).values())
    totals = data.get("totals") or {}
    return {
        "status": data.get("status"),
        "portal_registros": totals.get("portal_registros") or len(items),
        "paginas": totals.get("paginas"),
        "capturados": totals.get("capturados", len(items)),
        "pendentes": totals.get("pendentes", sum(1 for item in items if item.get("status") in (None, "pendente"))),
        "executando": sum(1 for item in items if item.get("status") == "executando"),
        "baixados": totals.get("baixados", sum(1 for item in items if item.get("status") == "baixado")),
        "erros": totals.get("erros", sum(1 for item in items if item.get("status") == "erro")),
        "ultimo_evento": (data.get("events") or [{}])[-1],
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


def _compact_run(ctx: WorkerContext, run_dir: Path) -> Dict[str, Any]:
    paths = _run_paths(run_dir)
    data = _load_json(paths["run"], {})
    if data.get("status") == "rodando" and not _active_runtime(ctx):
        data = _update_run(run_dir, status="interrompida", last_error=data.get("last_error") or "Processo não está mais ativo.")
    if paths["index"].exists():
        data["summary"] = _summarize_index(paths["index"])
    data["files"] = _list_files(run_dir)
    return data


def _create_run(ctx: WorkerContext, cfg: Dict[str, Any], modo: str) -> Path:
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
    if cfg.get("max_items"):
        cmd.extend(["--max", str(cfg["max_items"])])
    if retry_only:
        cmd.append("--forcar-indexar")
    else:
        cmd.append("--recriar-index")
        if cfg.get("renovar_sessao"):
            cmd.append("--renovar-inicio")
    return cmd


def _run_process(scope: str, run_dir: Path, cfg: Dict[str, Any], retry_only: bool) -> int:
    paths = _run_paths(run_dir)
    log_path = paths["logs"] / f"automacao_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    cmd = _build_command(cfg, run_dir, retry_only)
    _update_run(run_dir, status="rodando", last_command=cmd, last_log=str(log_path))
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


@router.get("/state")
async def portal_state(ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    certificate_error = None
    certificates: List[Dict[str, Any]] = []
    try:
        for index, cert in enumerate(await asyncio.to_thread(list_certificates)):
            certificates.append(
                {
                    "index": index,
                    "label": f"{index + 1}. {cert.get('subject') or 'Certificado'} | vence {(cert.get('not_after') or '')[:10]}",
                    "subject": cert.get("subject"),
                    "thumbprint": cert.get("thumbprint"),
                    "not_after": cert.get("not_after"),
                }
            )
    except Exception as exc:
        certificate_error = str(exc)

    return {
        "ok": True,
        "solver_url": DEFAULT_SOLVER_URL,
        "storage_root": str(_portal_root(ctx)),
        "session": _session_status(ctx),
        "certificates": certificates,
        "certificate_error": certificate_error,
        "active_run_id": (_active_runtime(ctx) or {}).get("run_id"),
        "runs": [_compact_run(ctx, path.parent) for path in sorted(_runs_root(ctx).glob("*/run.json"), key=lambda p: p.stat().st_mtime, reverse=True)],
        "limits": {"concorrencia_max": 16, "max_items_max": 5000},
    }


@router.post("/sessions/import")
async def import_session(payload: PortalSessionImportPayload, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    session = dict(payload.session or {})
    cookies = session.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise HTTPException(status_code=400, detail="Sessão inválida: cookies ausentes.")
    _save_json(_session_path(ctx), session)
    return {"ok": True, "session": _session_status(ctx)}


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
        "runs": [_compact_run(ctx, path.parent) for path in sorted(_runs_root(ctx).glob("*/run.json"), key=lambda p: p.stat().st_mtime, reverse=True)],
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
    override = _normalize_cfg({**cfg, **payload.model_dump(), "modo": cfg.get("modo") or "recebidas", "data_inicial": cfg.get("data_inicial"), "data_final": cfg.get("data_final"), "renovar_sessao": False})
    override["modo"] = cfg.get("modo") or override["modo"]
    override["renovar_sessao"] = False
    _update_run(run_dir, config=override)
    _start_jobs(ctx, [run_dir], retry_only=True)
    return {"ok": True, "run_id": run_dir.name}


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


@router.get("/runs/{run_id}/download")
async def download_portal_run(run_id: str, ctx: WorkerContext = Depends(get_worker_context)):
    run_dir = _safe_run_dir(ctx, run_id)
    paths = _run_paths(run_dir)
    paths["zip"].mkdir(parents=True, exist_ok=True)
    zip_path = paths["zip"] / f"{run_dir.name}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in _list_files(run_dir):
            full = safe_path_inside(str(run_dir), str(run_dir / file["relative_path"]))
            zf.write(full, arcname=file["relative_path"])
    return FileResponse(zip_path, filename=f"{run_dir.name}.zip", media_type="application/zip")


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
