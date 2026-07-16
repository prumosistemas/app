"""CLI operacional da Prumo sem credenciais literais nos comandos."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
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
}


def modal_run(store: SecretStore, account: str, arguments: list[str], extra_env: dict[str, str] | None = None) -> None:
    id_name, secret_name = MODAL_ACCOUNTS[account]
    token_id = store.require(id_name)
    token_secret = store.require(secret_name)
    env = os.environ.copy()
    env.update({"MODAL_TOKEN_ID": token_id, "MODAL_TOKEN_SECRET": token_secret})
    env.update(extra_env or {})
    command = ["modal", *arguments]
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    safe = redact((result.stdout or "") + (result.stderr or ""), [token_id, token_secret]).strip()
    if safe:
        print(safe)
    if result.returncode:
        raise OpsError(f"Modal terminou com codigo {result.returncode}.")


def modal_command(store: SecretStore, action: str, account: str, target: str | None) -> None:
    if action == "status":
        modal_run(store, account, ["app", "list", "--json"])
    elif action == "billing":
        modal_run(store, account, ["billing", "report", "--for", "this month", "--json"])
    elif action == "deploy":
        if target == "iss" and account != "primary":
            raise OpsError("O Browserless ISS pertence a conta primary.")
        if target == "iss":
            modal_run(store, account, ["deploy", "deploy/modal_browserless.py"])
        elif target == "portal":
            sizing = (
                {"PORTAL_MODAL_MIN_CONTAINERS": "1", "PORTAL_MODAL_BUFFER_CONTAINERS": "3"}
                if account == "primary"
                else {"PORTAL_MODAL_MIN_CONTAINERS": "0", "PORTAL_MODAL_BUFFER_CONTAINERS": "2"}
            )
            modal_run(store, account, ["deploy", "deploy/modal_portal_nacional_google_solver.py"], sizing)
        else:
            raise OpsError("Target Modal invalido.")


SSH_COMMAND = [
    "ssh",
    "-o",
    "ProxyCommand=cloudflared access ssh --hostname ssh.prumosistemas.com.br",
    "server@localhost",
    "bash -s",
]


def server_script(script: str, timeout: int = 300) -> None:
    # Enviar bytes evita que o modo texto do Windows converta LF para CRLF;
    # o bash remoto interpreta o CR como parte do comando.
    result = subprocess.run(SSH_COMMAND, input=script.encode("utf-8"), capture_output=True, timeout=timeout)
    stdout = result.stdout.decode("utf-8", errors="replace").strip()
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    if stdout:
        print(stdout)
    if stderr:
        print(stderr, file=sys.stderr)
    if result.returncode:
        raise OpsError(f"Comando remoto terminou com codigo {result.returncode}.")


def server_command(action: str, apply: bool, lines: int) -> None:
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
    elif action == "logs":
        server_script(f"docker logs --tail {max(1, min(lines, 2000))} prumo-api\n")
    elif action == "deploy":
        if not apply:
            emit({"dry_run": True, "action": "server deploy", "steps": ["git pull --ff-only", "docker build", "compose recreate", "health"]})
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
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/; then
    exit 0
  fi
  sleep 2
done
echo 'API nao ficou saudavel dentro de 60 segundos.' >&2
exit 1
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
        for profile, prefix in (("ryanzin", "MODAL_PRIMARY"), ("fabriciofarofa5", "MODAL_FALLBACK")):
            section = data.get(profile) or {}
            if section.get("token_id") and section.get("token_secret"):
                values[f"{prefix}_TOKEN_ID"] = str(section["token_id"])
                values[f"{prefix}_TOKEN_SECRET"] = str(section["token_secret"])
                imported.append(f"modal:{profile}")
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
    modal.add_argument("action", choices=["status", "billing", "deploy"])
    modal.add_argument("--account", choices=sorted(MODAL_ACCOUNTS), default="primary")
    modal.add_argument("--target", choices=["iss", "portal"])

    server = sub.add_parser("server", help="ThinkPad via Cloudflare Access SSH")
    server.add_argument("action", choices=["status", "logs", "deploy"])
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
            modal_command(store, args.action, args.account, args.target)
        elif args.area == "server":
            server_command(args.action, args.apply, args.lines)
        elif args.area == "app":
            app_login_smoke(store, args.alias)
    except (OpsError, SecretStoreError, requests.RequestException, subprocess.TimeoutExpired) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
