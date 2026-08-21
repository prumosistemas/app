"""CLI operacional da Prumo sem credenciais literais nos comandos."""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .secret_store import SecretStore, SecretStoreError, redact


ROOT = Path(__file__).resolve().parents[1]
WORKER_NAME = "morning-credit-8a59"
NETLIFY_SITE = "appprumo"
APP_URL = "https://app.prumosistemas.com.br"
CF_API = "https://api.cloudflare.com/client/v4"
NETLIFY_API = "https://api.netlify.com/api/v1"
PUBLIC_FILE_GLOBS = ("*.html", "*.png", "*.ico")
LOGIN_ALIAS_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
HTML_IMPORT_RE = re.compile(
    r'^import\s+([A-Za-z_$][\w$]*)\s+from\s+["\']([^"\']+\.html)["\'];\s*$', re.MULTILINE
)


class OpsError(RuntimeError):
    pass


def emit(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def require_ok(response: requests.Response, service: str) -> dict[str, Any] | list[Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not response.ok:
        detail = ""
        if isinstance(payload, dict):
            errors = payload.get("errors") or payload.get("error") or payload.get("message")
            detail = str(errors or "")[:500]
        raise OpsError(f"{service} respondeu HTTP {response.status_code}. {detail}".strip())
    if payload is None:
        raise OpsError(f"{service} respondeu sem JSON valido.")
    return payload


def cf_headers(store: SecretStore) -> tuple[dict[str, str], list[str]]:
    token = store.require("CLOUDFLARE_API_TOKEN")
    return {"Authorization": f"Bearer {token}"}, [token]


def cf_request(store: SecretStore, method: str, path: str, **kwargs: Any) -> Any:
    headers, _ = cf_headers(store)
    headers.update(kwargs.pop("headers", {}))
    response = requests.request(method, f"{CF_API}{path}", headers=headers, timeout=60, **kwargs)
    payload = require_ok(response, "Cloudflare")
    if isinstance(payload, dict) and payload.get("success") is False:
        raise OpsError("A API Cloudflare recusou a operacao.")
    return payload.get("result") if isinstance(payload, dict) and "result" in payload else payload


def cloudflare_status(store: SecretStore) -> None:
    account_id = store.require("CLOUDFLARE_ACCOUNT_ID")
    verify = cf_request(store, "GET", f"/accounts/{quote(account_id)}/tokens/verify")
    settings = cf_request(store, "GET", f"/accounts/{quote(account_id)}/workers/scripts/{WORKER_NAME}/settings")
    bindings = [
        {"name": item.get("name"), "type": item.get("type")}
        for item in (settings.get("bindings") or [])
        if isinstance(item, dict)
    ]
    emit(
        {
            "service": "cloudflare",
            "token_status": verify.get("status") if isinstance(verify, dict) else "unknown",
            "worker": WORKER_NAME,
            "compatibility_date": settings.get("compatibility_date"),
            "bindings": bindings,
            "secrets_exposed": False,
        }
    )


def build_worker_bundle() -> str:
    worker_path = ROOT / "cloudflare" / "worker.js"
    source = worker_path.read_text(encoding="utf-8")
    found = 0

    def replace_import(match: re.Match[str]) -> str:
        nonlocal found
        found += 1
        variable, relative = match.groups()
        target = (worker_path.parent / relative).resolve()
        try:
            target.relative_to(ROOT)
        except ValueError as exc:
            raise OpsError("Import HTML fora da raiz do projeto.") from exc
        if not target.is_file():
            raise OpsError(f"Arquivo HTML importado ausente: {target.name}")
        return f"const {variable} = {json.dumps(target.read_text(encoding='utf-8'), ensure_ascii=False)};"

    bundle = HTML_IMPORT_RE.sub(replace_import, source)
    if found == 0 or HTML_IMPORT_RE.search(bundle):
        raise OpsError("Nao foi possivel empacotar os imports HTML do Worker.")
    return bundle


def validate_worker_bundle(bundle: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8", delete=False) as handle:
        handle.write(bundle)
        temp_path = Path(handle.name)
    try:
        check = subprocess.run(["node", "--check", str(temp_path)], capture_output=True, text=True, timeout=30)
        if check.returncode:
            raise OpsError(f"Bundle Worker invalido: {(check.stderr or check.stdout)[:500]}")
    finally:
        temp_path.unlink(missing_ok=True)


def worker_metadata(existing: dict[str, Any]) -> dict[str, Any]:
    config = tomllib.loads((ROOT / "cloudflare" / "wrangler.toml").read_text(encoding="utf-8"))
    bindings: list[dict[str, Any]] = []
    managed: set[str] = set()
    for item in config.get("d1_databases", []):
        name = item["binding"]
        managed.add(name)
        bindings.append({"type": "d1", "name": name, "id": item["database_id"]})
    for name, value in config.get("vars", {}).items():
        managed.add(name)
        bindings.append({"type": "plain_text", "name": name, "text": str(value)})
    existing_bindings = existing.get("bindings") or []
    existing_names = {str(item.get("name")) for item in existing_bindings if isinstance(item, dict) and item.get("name")}
    if "ISS_INTERNAL_SECRET" not in existing_names:
        raise OpsError("Deploy bloqueado: o binding secreto ISS_INTERNAL_SECRET nao existe na versao atual.")
    for item in existing_bindings:
        name = str(item.get("name") or "") if isinstance(item, dict) else ""
        if name and name not in managed:
            bindings.append({"type": "inherit", "name": name})
    metadata: dict[str, Any] = {
        "main_module": "worker.bundle.mjs",
        "bindings": bindings,
        "compatibility_date": config["compatibility_date"],
        "compatibility_flags": config.get("compatibility_flags", []),
        "annotations": {"workers/message": "Deploy direto pela CLI segura Prumo"},
    }
    if config.get("observability"):
        metadata["observability"] = config["observability"]
    return metadata


def cloudflare_deploy(store: SecretStore, apply: bool) -> None:
    account_id = store.require("CLOUDFLARE_ACCOUNT_ID")
    bundle = build_worker_bundle()
    validate_worker_bundle(bundle)
    existing = cf_request(store, "GET", f"/accounts/{quote(account_id)}/workers/scripts/{WORKER_NAME}/settings")
    metadata = worker_metadata(existing)
    summary = {
        "worker": WORKER_NAME,
        "bundle_bytes": len(bundle.encode("utf-8")),
        "binding_names": sorted(item["name"] for item in metadata["bindings"]),
        "secrets_inherited": True,
        "routes_and_crons_changed": False,
    }
    if not apply:
        emit({"dry_run": True, **summary, "next": "repita com --apply para publicar"})
        return
    headers, _ = cf_headers(store)
    response = requests.put(
        f"{CF_API}/accounts/{quote(account_id)}/workers/scripts/{WORKER_NAME}",
        params={"bindings_inherit": "strict"},
        headers=headers,
        files={
            "metadata": (None, json.dumps(metadata, separators=(",", ":")), "application/json"),
            "worker.bundle.mjs": ("worker.bundle.mjs", bundle.encode("utf-8"), "application/javascript+module"),
        },
        timeout=180,
    )
    payload = require_ok(response, "Cloudflare")
    result = payload.get("result", {}) if isinstance(payload, dict) else {}
    emit({"deployed": True, **summary, "version_id": result.get("version_id"), "startup_time_ms": result.get("startup_time_ms")})


def netlify_headers(store: SecretStore) -> tuple[dict[str, str], list[str]]:
    token = store.require("NETLIFY_API_TOKEN")
    return {"Authorization": f"Bearer {token}"}, [token]


def netlify_status(store: SecretStore) -> None:
    headers, _ = netlify_headers(store)
    site = require_ok(requests.get(f"{NETLIFY_API}/sites/{NETLIFY_SITE}", headers=headers, timeout=45), "Netlify")
    deploys = require_ok(
        requests.get(f"{NETLIFY_API}/sites/{NETLIFY_SITE}/deploys", headers=headers, params={"per_page": 3}, timeout=45),
        "Netlify",
    )
    latest = deploys[0] if isinstance(deploys, list) and deploys else {}
    emit(
        {
            "service": "netlify",
            "site": site.get("name"),
            "custom_domain": site.get("custom_domain"),
            "repo": (site.get("build_settings") or {}).get("repo_url"),
            "latest_deploy": {k: latest.get(k) for k in ("id", "state", "created_at", "published_at", "error_message")},
        }
    )


def build_netlify_zip() -> bytes:
    config = tomllib.loads((ROOT / "netlify.toml").read_text(encoding="utf-8"))
    redirects = []
    for rule in config.get("redirects", []):
        force = "!" if rule.get("force") else ""
        redirects.append(f"{rule['from']} {rule['to']} {rule['status']}{force}")
    header_lines: list[str] = []
    for rule in config.get("headers", []):
        header_lines.append(rule["for"])
        for name, value in rule.get("values", {}).items():
            header_lines.append(f"  {name}: {value}")
        header_lines.append("")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        included: set[Path] = set()
        for pattern in PUBLIC_FILE_GLOBS:
            included.update(path for path in ROOT.glob(pattern) if path.is_file())
        for path in sorted(included):
            archive.write(path, path.name)
        archive.writestr("_redirects", "\n".join(redirects) + "\n")
        archive.writestr("_headers", "\n".join(header_lines).rstrip() + "\n")
    return output.getvalue()


def netlify_deploy(store: SecretStore, apply: bool) -> None:
    package = build_netlify_zip()
    if not apply:
        emit({"dry_run": True, "site": NETLIFY_SITE, "zip_bytes": len(package), "source_code_included": False, "next": "repita com --apply"})
        return
    headers, _ = netlify_headers(store)
    headers["Content-Type"] = "application/zip"
    deploy = require_ok(
        requests.post(f"{NETLIFY_API}/sites/{NETLIFY_SITE}/deploys", headers=headers, data=package, timeout=180), "Netlify"
    )
    emit({"deployed": True, "id": deploy.get("id"), "state": deploy.get("state"), "deploy_url": deploy.get("deploy_url")})


MODAL_ACCOUNTS = {
    "primary": ("MODAL_PRIMARY_TOKEN_ID", "MODAL_PRIMARY_TOKEN_SECRET"),
    "fallback": ("MODAL_FALLBACK_TOKEN_ID", "MODAL_FALLBACK_TOKEN_SECRET"),
    "tertiary": ("MODAL_TERTIARY_TOKEN_ID", "MODAL_TERTIARY_TOKEN_SECRET"),
}
MODAL_WORKSPACES = {
    "primary": "ryangurgell20",
    "fallback": "fabriciofarofa5",
    "tertiary": "prumo-sistema",
}

HF_ACCOUNTS = {
    "primary": ("HUGGINGFACE_PRIMARY_TOKEN", "HUGGINGFACE_TOKEN"),
    "secondary": ("HUGGINGFACE_SECONDARY_TOKEN",),
}
HF_SPACE_SOURCE = ROOT / "deploy" / "huggingface" / "navegador-headless"
HF_SPACE_CANONICAL_GOOGLE_AI = ROOT / "solver" / "google_ai_mode" / "google_ia_requests.py"
HF_SPACE_SOURCE_FILES = ("README.md", "app.py", "requirements.txt", "packages.txt")
HF_SPACE_UPLOAD_FILES = (*HF_SPACE_SOURCE_FILES, "google_ia_requests.py")


def hf_token(store: SecretStore, account: str) -> str:
    for name in HF_ACCOUNTS[account]:
        if store.has(name):
            return store.require(name)
    raise OpsError(f"Token Hugging Face ausente para a conta {account}.")


def hf_identity(store: SecretStore, account: str) -> dict[str, Any]:
    token = hf_token(store, account)
    response = requests.get(
        "https://huggingface.co/api/whoami-v2",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    data = require_ok(response, "Hugging Face")
    if not isinstance(data, dict) or not data.get("name"):
        raise OpsError("Hugging Face nao informou a identidade da conta.")
    return data


def hf_command(
    store: SecretStore,
    action: str,
    account: str,
    source_dir: str | None,
    space_names: list[str] | None,
) -> None:
    identity = hf_identity(store, account)
    username = str(identity["name"])
    if action == "status":
        from huggingface_hub import HfApi

        api = HfApi(token=hf_token(store, account))
        spaces = []
        for space in api.list_spaces(author=username, limit=100):
            runtime = None
            try:
                runtime = api.get_space_runtime(space.id)
            except Exception:
                pass
            spaces.append({
                "id": space.id,
                "private": bool(getattr(space, "private", False)),
                "stage": getattr(runtime, "stage", None),
                "hardware": getattr(runtime, "hardware", None),
                "requested_hardware": getattr(runtime, "requested_hardware", None),
            })
        emit({
            "account": account,
            "username": username,
            "plan": (identity.get("plan") or {}).get("name") if isinstance(identity.get("plan"), dict) else identity.get("plan"),
            "spaces": spaces,
            "token_exposed": False,
        })
        return
    if action != "deploy":
        raise OpsError("Acao Hugging Face invalida.")
    if not space_names:
        raise OpsError("Informe pelo menos um --space-name.")
    source = Path(source_dir).expanduser().resolve() if source_dir else HF_SPACE_SOURCE
    if not source.is_dir() or any(not (source / name).is_file() for name in HF_SPACE_SOURCE_FILES):
        raise OpsError("A fonte do Space esta incompleta.")
    if not HF_SPACE_CANONICAL_GOOGLE_AI.is_file():
        raise OpsError("O resolvedor Google Modo IA canonico esta ausente.")

    from huggingface_hub import HfApi
    from huggingface_hub.errors import HfHubHTTPError

    token = hf_token(store, account)
    api = HfApi(token=token)
    deployed = []
    with tempfile.TemporaryDirectory(prefix="prumo-hf-space-") as temporary:
        bundle = Path(temporary)
        for filename in HF_SPACE_SOURCE_FILES:
            shutil.copy2(source / filename, bundle / filename)
        # O Space recebe sempre o resolvedor versionado do projeto. Assim a
        # casca Gradio/HF nao mantem uma segunda copia divergente do motor.
        shutil.copy2(HF_SPACE_CANONICAL_GOOGLE_AI, bundle / "google_ia_requests.py")

        for raw_name in space_names:
            name = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(raw_name or "").strip()).strip("-.")
            if not name:
                raise OpsError("Nome de Space invalido.")
            repo_id = f"{username}/{name}"
            try:
                try:
                    api.repo_info(repo_id=repo_id, repo_type="space", token=token)
                except HfHubHTTPError as lookup_error:
                    if getattr(lookup_error.response, "status_code", None) != 404:
                        raise
                    api.create_repo(
                        repo_id=repo_id,
                        repo_type="space",
                        space_sdk="gradio",
                        # O navegador e o Modo IA usam CPU. CPU Basic evita a
                        # exigencia de PRO/30 dias do ZeroGPU em contas novas.
                        space_hardware="cpu-basic",
                        private=True,
                        token=token,
                    )
                api.upload_folder(
                    repo_id=repo_id,
                    repo_type="space",
                    folder_path=bundle,
                    allow_patterns=[*HF_SPACE_UPLOAD_FILES],
                    commit_message="Deploy Prumo Google Modo IA",
                    token=token,
                )
                runtime = api.get_space_runtime(repo_id, token=token)
            except HfHubHTTPError as exc:
                status = getattr(exc.response, "status_code", None)
                raise OpsError(f"Hugging Face recusou {repo_id} (HTTP {status or 'desconhecido'}).") from exc
            deployed.append({
                "id": repo_id,
                "stage": getattr(runtime, "stage", None),
                "hardware": getattr(runtime, "hardware", None),
                "requested_hardware": getattr(runtime, "requested_hardware", None),
            })
    emit({"account": account, "deployed": deployed, "token_exposed": False})


def modal_run(
    store: SecretStore,
    account: str,
    arguments: list[str],
    extra_env: dict[str, str] | None = None,
    sensitive_values: list[str] | None = None,
) -> None:
    id_name, secret_name = MODAL_ACCOUNTS[account]
    token_id = store.require(id_name)
    token_secret = store.require(secret_name)
    env = os.environ.copy()
    env.update(
        {
            "MODAL_TOKEN_ID": token_id,
            "MODAL_TOKEN_SECRET": token_secret,
            # A CLI Modal usa simbolos Unicode no progresso. Sem UTF-8, o
            # Python do Windows pode abortar antes do deploy com codec cp1252.
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    env.update(extra_env or {})
    command = ["modal", *arguments]
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    safe = redact(
        (result.stdout or "") + (result.stderr or ""),
        [token_id, token_secret, *(sensitive_values or [])],
    ).strip()
    if safe:
        console_encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(safe.encode(console_encoding, errors="replace").decode(console_encoding))
    if result.returncode:
        raise OpsError(f"Modal terminou com codigo {result.returncode}.")


def modal_command(
    store: SecretStore,
    action: str,
    account: str,
    target: str | None,
    hf_mode: str | None = None,
) -> None:
    if action == "status":
        modal_run(store, account, ["app", "list", "--json"])
    elif action == "logs":
        app_name = (
            "prumo-browserless"
            if target == "iss"
            else "prumo-portal-nacional-google-solver"
            if target == "portal"
            else None
        )
        if not app_name:
            raise OpsError("Informe --target para consultar logs Modal.")
        modal_run(store, account, ["app", "logs", app_name, "--tail", "250", "--timestamps"])
    elif action == "billing":
        modal_run(store, account, ["billing", "report", "--for", "this month", "--json"])
    elif action == "deploy":
        if target == "iss":
            sizing = (
                {"PRUMO_MODAL_MAX_CONTAINERS": "8"}
                if account == "primary"
                else {"PRUMO_MODAL_MAX_CONTAINERS": "6"}
            )
            modal_run(store, account, ["deploy", "deploy/modal_browserless.py"], sizing)
        elif target == "portal":
            sizing = (
                {"PORTAL_MODAL_MIN_CONTAINERS": "1", "PORTAL_MODAL_BUFFER_CONTAINERS": "1"}
                if account == "primary"
                else {"PORTAL_MODAL_MIN_CONTAINERS": "0", "PORTAL_MODAL_BUFFER_CONTAINERS": "0"}
            )
            sizing["PRUMO_SOLVER_LOCATION"] = f"modal_{account}"
            modal_run(store, account, ["deploy", "deploy/modal_portal_nacional_google_solver.py"], sizing)
        else:
            raise OpsError("Target Modal invalido.")
    elif action == "rollover":
        app_name = (
            "prumo-browserless"
            if target == "iss"
            else "prumo-portal-nacional-google-solver"
            if target == "portal"
            else None
        )
        if not app_name:
            raise OpsError("Target Modal invalido.")
        modal_run(store, account, ["app", "rollover", app_name, "--strategy", "recreate"])
    elif action == "sync-iss-secret":
        secret_name = f"ISS_BROWSERLESS_TOKEN_{account.upper()}"
        if not store.has(secret_name):
            if account == "primary":
                raise OpsError(
                    "O token primario existente nao deve ser rotacionado automaticamente. "
                    "Cadastre ISS_BROWSERLESS_TOKEN_PRIMARY antes desta operacao."
                )
            store.set(secret_name, secrets.token_urlsafe(32))
        token = store.require(secret_name)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", encoding="utf-8", delete=False
            ) as handle:
                json.dump({"TOKEN": token}, handle, separators=(",", ":"))
                temporary_path = Path(handle.name)
            modal_run(
                store,
                account,
                [
                    "secret", "create", "prumo-browserless",
                    "--from-json", str(temporary_path), "--force",
                ],
                sensitive_values=[token],
            )
            emit({
                "account": account,
                "secret": "prumo-browserless",
                "local_alias": secret_name,
                "value_printed": False,
            })
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
    elif action == "smoke-iss":
        secret_name = f"ISS_BROWSERLESS_TOKEN_{account.upper()}"
        token = store.require(secret_name)
        workspace = MODAL_WORKSPACES[account]
        endpoint = f"https://{workspace}--prumo-browserless-browserless-server.modal.run/json/version"
        started = time.perf_counter()
        response = requests.get(endpoint, params={"token": token}, timeout=120)
        if response.status_code != 200:
            raise OpsError(f"Browserless {account} respondeu HTTP {response.status_code}.")
        emit({
            "account": account,
            "browserless": "ok",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "value_printed": False,
        })
    elif action == "sync-hf-secret":
        token = hf_token(store, "primary")
        mode = hf_mode or "prefer"
        payload = {
            "HF_TOKEN": token,
            "PRUMO_HF_GOOGLE_AI_SPACES": (
                "ryanzinprot/navegador-headless,"
                "ryanzinprot/navegador-headless-2"
            ),
            "PRUMO_HF_GOOGLE_AI_MODE": mode,
            # Dois Spaces gratuitos atendem uma requisicao cada. Trinta
            # segundos preservam os sucessos observados (maximo ~24 s) e
            # desviam cedo a fila excedente para o egress Modal ja aquecido.
            "PRUMO_HF_GOOGLE_AI_TIMEOUT_SECONDS": "30",
            "PRUMO_HF_GOOGLE_AI_COOLDOWN_SECONDS": "180",
        }
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", encoding="utf-8", delete=False
            ) as handle:
                json.dump(payload, handle, separators=(",", ":"))
                temporary_path = Path(handle.name)
            modal_run(
                store,
                account,
                [
                    "secret", "create", "prumo-huggingface",
                    "--from-json", str(temporary_path), "--force",
                ],
                sensitive_values=[token],
            )
            emit({
                "account": account,
                "secret": "prumo-huggingface",
                "mode": mode,
                "value_printed": False,
            })
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


SSH_COMMAND = [
    "ssh",
    "-o",
    "ProxyCommand=cloudflared access ssh --hostname ssh.prumosistemas.com.br",
    "server@localhost",
    "bash -s",
]


def console_safe_text(value: str, encoding: str | None) -> str:
    """Evita que Unicode de ferramentas remotas derrube consoles legados."""
    target_encoding = encoding or "utf-8"
    return value.encode(target_encoding, errors="replace").decode(target_encoding)


def server_script(script: str, timeout: int = 300) -> None:
    # Enviar bytes evita que o modo texto do Windows converta LF para CRLF;
    # o bash remoto interpreta o CR como parte do comando.
    result = subprocess.run(SSH_COMMAND, input=script.encode("utf-8"), capture_output=True, timeout=timeout)
    stdout = result.stdout.decode("utf-8", errors="replace").strip()
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    if stdout:
        print(console_safe_text(stdout, getattr(sys.stdout, "encoding", None)))
    if stderr:
        print(console_safe_text(stderr, getattr(sys.stderr, "encoding", None)), file=sys.stderr)
    if result.returncode:
        raise OpsError(f"Comando remoto terminou com codigo {result.returncode}.")


def server_recover_from_spec(spec_raw: str) -> None:
    try:
        spec = json.loads(spec_raw)
    except (TypeError, ValueError) as exc:
        raise OpsError("PRUMO_SERVER_RECOVERY_SPEC precisa conter JSON valido.") from exc
    if not isinstance(spec, dict):
        raise OpsError("PRUMO_SERVER_RECOVERY_SPEC precisa ser um objeto JSON.")
    encoded = base64.b64encode(json.dumps(spec, ensure_ascii=False).encode("utf-8")).decode("ascii")
    server_script(
        f"""set -eu
docker exec -i -e PRUMO_RECOVERY_SPEC_B64='{encoded}' prumo-api python - <<'PY'
import base64
import json
import os
import requests

from db import ISS_INTERNAL_SECRET, now_ms
from domain import WorkerContext, load_accounts_raw, new_account_id, save_accounts

spec = json.loads(base64.b64decode(os.environ["PRUMO_RECOVERY_SPEC_B64"]).decode("utf-8"))
result = {{"account_copy": None, "portal_resumes": [], "closure_retries": []}}
copy_spec = spec.get("account_copy") or None
if copy_spec:
    company_id = str(copy_spec["company_id"])
    source_ctx = WorkerContext(company_id, company_id, str(copy_spec["source_user_id"]), "operacao@interno", "member", True)
    target_ctx = WorkerContext(company_id, company_id, str(copy_spec["target_user_id"]), str(copy_spec["target_user_email"]), "member", True)
    alias = str(copy_spec["alias"]).strip()
    source = next((item for item in load_accounts_raw(source_ctx) if str(item.get("alias") or "").strip() == alias), None)
    if source is None:
        raise RuntimeError("Conta de origem nao encontrada pelo alias informado.")
    target_accounts = load_accounts_raw(target_ctx)
    existing = next((item for item in target_accounts if str(item.get("alias") or "").strip() == alias), None)
    if existing is None:
        clone = dict(source)
        clone["id"] = new_account_id()
        clone["created_at"] = now_ms()
        clone["updated_at"] = now_ms()
        clone["created_by_user_email"] = target_ctx.user_email
        target_accounts.append(clone)
        save_accounts(target_ctx, target_accounts)
        result["account_copy"] = {{"created": True, "alias": alias, "target_user_email": target_ctx.user_email}}
    else:
        result["account_copy"] = {{"created": False, "alias": alias, "target_user_email": target_ctx.user_email}}

for item in spec.get("portal_resumes") or []:
    headers = {{
        "X-Internal-Secret": ISS_INTERNAL_SECRET,
        "X-Company-Id": str(item["company_id"]),
        "X-Company-Name": str(item.get("company_name") or item["company_id"]),
        "X-User-Id": str(item["user_id"]),
        "X-User-Email": str(item["user_email"]),
        "X-User-Role": str(item.get("user_role") or "member"),
    }}
    response = requests.post(
        "http://127.0.0.1:8000/api/portal-nacional/runs/continue",
        headers=headers,
        json={{"run_ids": list(item["run_ids"])}},
        timeout=30,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {{"detail": "Resposta sem JSON"}}
    if not response.ok:
        raise RuntimeError(f"Falha ao continuar Portal: HTTP {{response.status_code}} {{payload.get('detail', '')}}")
    result["portal_resumes"].append(
        {{"user_email": item["user_email"], "run_ids": payload.get("run_ids") or [payload.get("run_id")]}}
    )

for item in spec.get("closure_retry_errors") or []:
    headers = {{
        "X-Internal-Secret": ISS_INTERNAL_SECRET,
        "X-Company-Id": str(item["company_id"]),
        "X-Company-Name": str(item.get("company_name") or item["company_id"]),
        "X-User-Id": str(item["user_id"]),
        "X-User-Email": str(item["user_email"]),
        "X-User-Role": str(item.get("user_role") or "member"),
    }}
    run_id = str(item["run_id"])
    response = requests.post(
        f"http://127.0.0.1:8000/api/closure-scans/{{run_id}}/retry-errors",
        headers=headers,
        timeout=30,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {{"detail": "Resposta sem JSON"}}
    if not response.ok:
        raise RuntimeError(f"Falha ao retomar encerramento: HTTP {{response.status_code}} {{payload.get('detail', '')}}")
    result["closure_retries"].append({{"run_id": run_id, "retried": payload.get("retried", 0)}})

print(json.dumps(result, ensure_ascii=False))
PY
""",
        timeout=120,
    )


def configure_iss_browser_failover(store: SecretStore, *, apply: bool) -> None:
    if not apply:
        emit({
            "would_configure": "BROWSER_CDP_POOL",
            "primary_weight": 18,
            "fallback_weight": 4,
            "tertiary_weight": 8,
            "restart": "prumo-api",
            "apply": False,
        })
        return
    tokens = {
        "fallback": store.require("ISS_BROWSERLESS_TOKEN_FALLBACK"),
    }
    if store.has("ISS_BROWSERLESS_TOKEN_TERTIARY"):
        tokens["tertiary"] = store.require("ISS_BROWSERLESS_TOKEN_TERTIARY")
    payload_b64 = base64.b64encode(json.dumps(tokens).encode("utf-8")).decode("ascii")
    server_script(
        f"""set -eu
cd /opt/prumo/app/deploy
python3 - '{payload_b64}' <<'PY'
import base64
import json
import os
import sys
from pathlib import Path

path = Path('/opt/prumo/app/deploy/.env')
tokens = json.loads(base64.b64decode(sys.argv[1]).decode('utf-8'))
lines = path.read_text(encoding='utf-8').splitlines()
key = 'BROWSER_CDP_POOL'
current = next((line.split('=', 1)[1] for line in lines if line.startswith(key + '=')), '')
entries = []
primary_found = False
for raw in current.split(';;'):
    entry = raw.strip()
    if not entry:
        continue
    parts = entry.split('|', 2)
    url = parts[-1]
    if (
        'fabriciofarofa5--prumo-browserless' in url
        or 'prumo-sistema--prumo-browserless' in url
    ):
        continue
    if 'ryangurgell20--prumo-browserless' in url:
        primary_weight = 18 if 'tertiary' in tokens else 24
        entries.append(f'modal-primary|{{primary_weight}}|' + url)
        primary_found = True
    else:
        entries.append(entry)
if not primary_found:
    raise SystemExit('Pool primario atual nao encontrado; nenhuma alteracao aplicada.')
fallback = (
    'modal-fallback|4|wss://fabriciofarofa5--prumo-browserless-'
    'browserless-server.modal.run?token=' + tokens['fallback']
)
entries.append(fallback)
if 'tertiary' in tokens:
    entries.append(
        'modal-tertiary|8|wss://prumo-sistema--prumo-browserless-'
        'browserless-server.modal.run?token=' + tokens['tertiary']
    )
replacement = key + '=' + ';;'.join(entries)
updated = []
replaced = False
for line in lines:
    if line.startswith(key + '='):
        updated.append(replacement)
        replaced = True
    else:
        updated.append(line)
if not replaced:
    updated.append(replacement)
temp = path.with_suffix('.env.tmp')
temp.write_text('\\n'.join(updated) + '\\n', encoding='utf-8')
os.replace(temp, path)
print('Pool ISS configurado: ' + ('primario=18 fallback=4 terceiro=8' if 'tertiary' in tokens else 'primario=24 fallback=4') + '; valores nao exibidos.')
PY
docker compose up -d --force-recreate prumo-api
docker ps --filter name=prumo-api --format '{{{{.Names}}}} {{{{.Status}}}} {{{{.Image}}}}'
""",
        timeout=300,
    )


def server_command(store: SecretStore, action: str, apply: bool, lines: int) -> None:
    if action == "status":
        server_script(
            """set -eu
echo SERVER_GIT
git -C /home/server/prumo-src status --short --branch
echo CONTAINERS
docker ps --filter name=prumo --format '{{.Names}} {{.Status}} {{.Image}}'
echo API_HEALTH
curl -fsS http://127.0.0.1:8000/
"""
        )
        server_command(store, "runs", apply=False, lines=lines)
        recovery_spec = os.getenv("PRUMO_SERVER_RECOVERY_SPEC", "").strip()
        recovery_path = ROOT / ".ops-server-recovery.json"
        recovery_from_file = False
        if not recovery_spec and recovery_path.is_file():
            recovery_spec = recovery_path.read_text(encoding="utf-8")
            try:
                recovery_from_file = bool(json.loads(recovery_spec).get("apply"))
            except (AttributeError, ValueError):
                recovery_from_file = False
        if apply or recovery_from_file:
            if not recovery_spec:
                raise OpsError("Defina PRUMO_SERVER_RECOVERY_SPEC para usar server status --apply.")
            server_recover_from_spec(recovery_spec)
            if recovery_from_file:
                recovery_path.unlink(missing_ok=True)
    elif action == "configure-iss-pool":
        configure_iss_browser_failover(store, apply=apply)
    elif action == "smoke-iss":
        server_script(
            r"""set -eu
docker exec -i prumo-api python - <<'PY'
import asyncio
import json
import tempfile

from flow_core import FlowConfig, create_browser_context

async def main():
    with tempfile.TemporaryDirectory() as root:
        cfg = FlowConfig(
            run_id="ops-smoke-iss",
            run_dir=root,
            run_log_file=f"{root}/run.log",
            cnpj_dir=f"{root}/smoke",
            step_timeout_sec=30,
            nav_timeout_ms=30000,
            selector_timeout_ms=10000,
            close_timeout_sec=10,
            goto_retries=1,
            headless=True,
        )
        context, close = await create_browser_context(cfg)
        page = await context.new_page()
        await page.goto("data:text/html,<title>Prumo smoke</title>")
        title = await page.title()
        await close()
        print(json.dumps({"browserless": "ok", "title": title}))

asyncio.run(main())
PY
""",
            timeout=180,
        )
    elif action == "metrics":
        server_script(
            r"""set -eu
docker exec -i prumo-api python - <<'PY'
import json
import os
import requests

response = requests.get(
    "http://127.0.0.1:8000/api/internal/runtime-metrics",
    headers={"X-Internal-Secret": os.environ["ISS_INTERNAL_SECRET"]},
    timeout=10,
)
response.raise_for_status()
payload = response.json()
print(json.dumps({
    "iss": payload.get("iss"),
    "portal": payload.get("portal"),
    "queue": payload.get("queue"),
}, ensure_ascii=False))
PY
"""
        )
    elif action == "logs":
        server_script(f"docker logs --tail {max(1, min(lines, 2000))} prumo-api\n")
    elif action == "runs":
        server_script(
            r"""set -eu
docker exec -i prumo-api python - <<'PY'
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

output_root = Path(os.getenv("ISS_OUTPUT_ROOT", "/app/output"))
db_file = Path(os.getenv("ISS_DATA_ROOT", str(output_root / "_api_data"))) / "iss_automacao.db"
email_by_scope = {}
closure_runs = []

with sqlite3.connect(db_file) as conn:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT key, value FROM kv WHERE key LIKE '%:runs_state' OR key LIKE '%:closure_scans'"
    ).fetchall()

for row in rows:
    try:
        payload = json.loads(row["value"])
    except Exception:
        continue
    runs = payload.get("runs", {}) if isinstance(payload, dict) else {}
    values = runs.values() if isinstance(runs, dict) else runs if isinstance(runs, list) else []
    for run in values:
        if not isinstance(run, dict):
            continue
        company_id = str(run.get("company_id") or "")
        user_id = str(run.get("user_id") or "")
        user_email = str(run.get("user_email") or "")
        if company_id and user_id and user_email:
            email_by_scope[(company_id, user_id)] = user_email
        if not str(row["key"]).endswith(":closure_scans"):
            continue
        error_summary = Counter(
            str(item.get("erro") or "Sem detalhe")[:240]
            for item in run.get("results") or []
            if isinstance(item, dict) and item.get("status") == "ERRO"
        )
        accounts = []
        for account in run.get("accounts") or []:
            if isinstance(account, dict):
                accounts.append(
                    {
                        "alias": account.get("account_alias"),
                        "status": account.get("status"),
                        "processed": account.get("processed"),
                        "total": account.get("total"),
                        "open": account.get("open"),
                        "closed": account.get("closed"),
                        "errors": account.get("errors"),
                    }
                )
        closure_runs.append(
            {
                "company_id": company_id,
                "user_id": user_id,
                "user_email": user_email,
                "run_id": run.get("run_id"),
                "status": run.get("status"),
                "progress": run.get("progress"),
                "processed": run.get("processed"),
                "total": run.get("total"),
                "open": run.get("open"),
                "closed": run.get("closed"),
                "errors": run.get("errors"),
                "created_at": run.get("created_at"),
                "updated_at": run.get("updated_at"),
                "accounts": accounts,
                "error_summary": [
                    {"message": message, "count": count}
                    for message, count in error_summary.most_common(10)
                ],
            }
        )

portal_runs = []
portal_root = output_root / "empresas"
for path in portal_root.glob("*/colaboradores/*/portal_nacional/runs/*/run.json"):
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
        relative = path.relative_to(portal_root).parts
    except Exception:
        continue
    company_id, user_id = relative[0], relative[2]
    config = run.get("config") or {}
    summary = run.get("summary") or {}
    portal_runs.append(
        {
            "company_id": company_id,
            "user_id": user_id,
            "user_email": email_by_scope.get((company_id, user_id), ""),
            "run_id": run.get("run_id") or path.parent.name,
            "status": run.get("status"),
            "automatic": bool(config.get("automatic")),
            "modo": config.get("modo"),
            "tipo_download": config.get("tipo_download"),
            "created_at": run.get("created_at"),
            "updated_at": run.get("updated_at"),
            "baixados": summary.get("baixados"),
            "erros": summary.get("erros"),
            "pendentes": summary.get("pendentes"),
        }
    )

portal_runs.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
closure_runs.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)
print(json.dumps({"portal_runs": portal_runs[:60], "closure_runs": closure_runs[:20]}, ensure_ascii=False))
PY
"""
        )
    elif action == "deploy":
        if not apply:
            emit({"dry_run": True, "action": "server deploy", "steps": ["git pull --ff-only", "docker build", "compose recreate", "health", "keep current + 2 rollback images"]})
            return
        server_script(
            r"""set -eu
cd /home/server/prumo-src
git pull --ff-only
image=$(sed -n 's/.*PRUMO_API_IMAGE:-\([^}]*\).*/\1/p' deploy/docker-compose.yml | head -n 1)
test -n "$image"
docker build -f server/Dockerfile -t "$image" .
cp deploy/docker-compose.yml /opt/prumo/app/deploy/docker-compose.yml
cd /opt/prumo/app/deploy
if grep -q '^PRUMO_API_IMAGE=' .env; then
  sed -i "s|^PRUMO_API_IMAGE=.*$|PRUMO_API_IMAGE=$image|" .env
else
  printf 'PRUMO_API_IMAGE=%s\n' "$image" >> .env
fi
PRUMO_API_IMAGE="$image" docker compose up -d --force-recreate --remove-orphans
healthy=0
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/; then
    healthy=1
    break
  fi
  sleep 2
done
if [ "$healthy" -ne 1 ]; then
  echo 'API nao ficou saudavel dentro de 60 segundos.' >&2
  exit 1
fi

# A imagem corrente e duas anteriores bastam para rollback local. O codigo
# completo de todas as versoes continua no Git, portanto acumular dezenas de
# tags de 2+ GiB no ThinkPad apenas consome disco.
mapfile -t prumo_images < <(
  docker image ls ryang20/prumo-api \
    --format '{{.Repository}}:{{.Tag}}|{{.CreatedAt}}' \
    | sort -t '|' -k2,2r \
    | cut -d '|' -f1
)
for old_image in "${prumo_images[@]:3}"; do
  docker image rm "$old_image" >/dev/null 2>&1 || true
done
docker image prune -f >/dev/null 2>&1 || true
echo "Docker Prumo: ${#prumo_images[@]} tag(s) encontradas; mantidas as 3 mais recentes."
""",
            timeout=1800,
        )


def login_secret_names(alias: str) -> tuple[str, str]:
    clean = alias.lower().strip()
    if not LOGIN_ALIAS_RE.fullmatch(clean):
        raise OpsError("Alias de login invalido.")
    return f"LOGIN.{clean}.EMAIL", f"LOGIN.{clean}.PASSWORD"


def app_login_smoke(store: SecretStore, alias: str) -> None:
    email_name, password_name = login_secret_names(alias)
    email = store.require(email_name)
    password = store.require(password_name)
    session = requests.Session()
    login = session.post(f"{APP_URL}/api/login", json={"email": email, "password": password}, timeout=60)
    data = require_ok(login, "Login Prumo")
    token = data.get("session_token") if isinstance(data, dict) else None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    me = require_ok(session.get(f"{APP_URL}/api/me", headers=headers, timeout=45), "Sessao Prumo")
    csrf = data.get("csrf") if isinstance(data, dict) else None
    if csrf:
        headers["X-CSRF-Token"] = csrf
    session.post(f"{APP_URL}/api/logout", headers=headers, timeout=30)
    emit(
        {
            "alias": alias,
            "authenticated": bool(me.get("authenticated")),
            "role": (me.get("user") or {}).get("role"),
            "must_change_password": bool((me.get("user") or {}).get("must_change_password")),
            "credential_values_exposed": False,
        }
    )


def migrate_local(store: SecretStore) -> None:
    imported: list[str] = []
    values: dict[str, str] = {}
    account_file = ROOT / "AccountID.txt"
    token_file = ROOT / "token.txt"
    if account_file.is_file() and token_file.is_file():
        account_id = account_file.read_text(encoding="utf-8-sig").strip()
        candidates = [("cloudflare:file", token_file.read_text(encoding="utf-8-sig").strip())]
        # Fallback de migracao: copia o OAuth ja autenticado pelo usuario, mas
        # a CLI operacional continua falando diretamente com a API e nao chama
        # Wrangler. Tokens OAuth expiram; prefira cadastrar um API Token longo.
        wrangler_config = Path.home() / ".wrangler" / "config" / "default.toml"
        if wrangler_config.is_file():
            wrangler = tomllib.loads(wrangler_config.read_text(encoding="utf-8"))
            if wrangler.get("oauth_token"):
                candidates.append(("cloudflare:wrangler-oauth-temporary", str(wrangler["oauth_token"])))
        for source, candidate in candidates:
            try:
                probe = requests.get(
                    f"{CF_API}/accounts/{quote(account_id)}/workers/scripts/{WORKER_NAME}/settings",
                    headers={"Authorization": f"Bearer {candidate}"},
                    timeout=30,
                )
            except requests.RequestException:
                continue
            if probe.ok:
                values["CLOUDFLARE_ACCOUNT_ID"] = account_id
                values["CLOUDFLARE_API_TOKEN"] = candidate
                imported.append(source)
                break
    netlify_config = Path(os.getenv("APPDATA", "")) / "netlify" / "Config" / "config.json"
    if netlify_config.is_file():
        data = json.loads(netlify_config.read_text(encoding="utf-8"))
        users = data.get("users") or {}
        ordered_ids = [data.get("userId"), *users.keys()]
        seen: set[str] = set()
        for user_id in ordered_ids:
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            candidate = str((users.get(user_id) or {}).get("auth") or "")
            if not candidate:
                continue
            try:
                probe = requests.get(
                    f"{NETLIFY_API}/sites/{NETLIFY_SITE}",
                    headers={"Authorization": f"Bearer {candidate}"},
                    timeout=30,
                )
            except requests.RequestException:
                continue
            if probe.ok:
                values["NETLIFY_API_TOKEN"] = candidate
                imported.append("netlify:validated-profile")
                break
    modal_config = Path.home() / ".modal.toml"
    if modal_config.is_file():
        data = tomllib.loads(modal_config.read_text(encoding="utf-8"))
        for profile, prefix in (
            ("ryanzin", "MODAL_PRIMARY"),
            ("fabriciofarofa5", "MODAL_FALLBACK"),
            ("prumo-sistema", "MODAL_TERTIARY"),
        ):
            section = data.get(profile) or {}
            if section.get("token_id") and section.get("token_secret"):
                values[f"{prefix}_TOKEN_ID"] = str(section["token_id"])
                values[f"{prefix}_TOKEN_SECRET"] = str(section["token_secret"])
                imported.append(f"modal:{profile}")
    hf_token_files = (
        (Path.home() / "Downloads" / "hf_write_token.txt", "primary"),
        (Path.home() / "Downloads" / "hf_write_token - 2.txt", "secondary"),
    )
    for hf_token_file, account in hf_token_files:
        if not hf_token_file.is_file():
            continue
        candidate = hf_token_file.read_text(encoding="utf-8-sig").strip()
        if not candidate:
            continue
        try:
            probe = requests.get(
                "https://huggingface.co/api/whoami-v2",
                headers={"Authorization": f"Bearer {candidate}"},
                timeout=30,
            )
        except requests.RequestException:
            probe = None
        if probe is not None and probe.ok:
            if account == "primary":
                values["HUGGINGFACE_TOKEN"] = candidate
                values["HUGGINGFACE_PRIMARY_TOKEN"] = candidate
            else:
                values["HUGGINGFACE_SECONDARY_TOKEN"] = candidate
            imported.append(f"huggingface:{account}:validated-download-token")
    if not values:
        raise OpsError("Nenhuma credencial local conhecida foi encontrada.")
    store.set_many(values)
    emit({"imported_sources": imported, "values_printed": False, "store": str(store.path)})


def secret_command(store: SecretStore, action: str, name: str | None, alias: str | None) -> None:
    if action == "status":
        emit({"store": str(store.path), "configured_names": store.names(), "values_printed": False})
    elif action == "migrate-local":
        migrate_local(store)
    elif action == "set":
        if not name:
            raise OpsError("Informe o nome do segredo.")
        store.prompt_set(name)
        emit({"saved": name, "value_printed": False})
    elif action == "set-login":
        if not alias:
            raise OpsError("Informe o alias do login.")
        email_name, password_name = login_secret_names(alias)
        email = input(f"Email para o alias {alias}: ").strip()
        if not email or "@" not in email:
            raise OpsError("Email invalido.")
        store.set(email_name, email)
        store.prompt_set(password_name, label=f"Senha para {alias}")
        emit({"saved_login_alias": alias, "values_printed": False})
    elif action == "delete":
        if not name:
            raise OpsError("Informe o nome do segredo.")
        emit({"deleted": store.delete(name), "name": name})


def overall_status(store: SecretStore) -> None:
    public: dict[str, Any] = {}
    for name, url in {
        "app": APP_URL,
        "api": "https://api.prumosistemas.com.br/",
        "portal_solver_primary": "https://ryangurgell20--prumo-portal-nacional-google-solver-solve-d8ccea.modal.run/health",
    }.items():
        started = time.perf_counter()
        try:
            response = requests.get(url, timeout=30)
            public[name] = {"status": response.status_code, "latency_ms": round((time.perf_counter() - started) * 1000)}
        except requests.RequestException as exc:
            public[name] = {"error": type(exc).__name__}
    git = subprocess.run(["git", "status", "--short", "--branch"], cwd=ROOT, capture_output=True, text=True, timeout=20)
    emit({"public": public, "git": git.stdout.strip().splitlines(), "configured_secret_names": store.names()})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="area", required=True)
    sub.add_parser("status", help="Saude publica, Git e nomes configurados")

    secrets = sub.add_parser("secrets", help="Gerencia o cofre DPAPI sem imprimir valores")
    secrets.add_argument("action", choices=["status", "migrate-local", "set", "set-login", "delete"])
    secrets.add_argument("name", nargs="?")
    secrets.add_argument("--alias")

    cloudflare = sub.add_parser("cloudflare", help="Cloudflare via API REST, sem Wrangler")
    cloudflare.add_argument("action", choices=["status", "deploy"])
    cloudflare.add_argument("--apply", action="store_true")

    netlify = sub.add_parser("netlify", help="Netlify via API REST")
    netlify.add_argument("action", choices=["status", "deploy"])
    netlify.add_argument("--apply", action="store_true")

    modal = sub.add_parser("modal", help="Modal com token injetado no processo filho")
    modal.add_argument(
        "action",
        choices=[
            "status", "logs", "billing", "deploy", "rollover",
            "sync-hf-secret", "sync-iss-secret", "smoke-iss",
        ],
    )
    modal.add_argument("--account", choices=sorted(MODAL_ACCOUNTS), default="primary")
    modal.add_argument("--target", choices=["iss", "portal"])
    modal.add_argument("--hf-mode", choices=["off", "prefer", "fallback"])

    hf = sub.add_parser("hf", help="Hugging Face sem credenciais na linha de comando")
    hf.add_argument("action", choices=["status", "deploy"])
    hf.add_argument("--account", choices=sorted(HF_ACCOUNTS), default="primary")
    hf.add_argument("--source-dir")
    hf.add_argument("--space-name", action="append", dest="space_names")

    server = sub.add_parser("server", help="ThinkPad via Cloudflare Access SSH")
    server.add_argument(
        "action",
        choices=["status", "logs", "runs", "deploy", "configure-iss-pool", "smoke-iss", "metrics"],
    )
    server.add_argument("--apply", action="store_true")
    server.add_argument("--lines", type=int, default=200)

    app = sub.add_parser("app", help="Teste autenticado por alias")
    app.add_argument("action", choices=["login-smoke"])
    app.add_argument("--alias", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = SecretStore()
    try:
        if args.area == "status":
            overall_status(store)
        elif args.area == "secrets":
            secret_command(store, args.action, args.name, args.alias)
        elif args.area == "cloudflare":
            cloudflare_status(store) if args.action == "status" else cloudflare_deploy(store, args.apply)
        elif args.area == "netlify":
            netlify_status(store) if args.action == "status" else netlify_deploy(store, args.apply)
        elif args.area == "modal":
            modal_command(store, args.action, args.account, args.target, args.hf_mode)
        elif args.area == "hf":
            hf_command(store, args.action, args.account, args.source_dir, args.space_names)
        elif args.area == "server":
            server_command(store, args.action, args.apply, args.lines)
        elif args.area == "app":
            app_login_smoke(store, args.alias)
    except (OpsError, SecretStoreError, requests.RequestException, subprocess.TimeoutExpired) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
