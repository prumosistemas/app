"""Deploy reproduzivel do resolvedor Google Modo IA do Portal Nacional.

O codigo validado no projeto organizado fica versionado em ``solver/``. Apenas
cookies anonimos e estado efemero ficam no Volume privado do Modal.
"""

from __future__ import annotations

import os
import hashlib
import json
import socket
import shutil
import subprocess
import time
from pathlib import Path

import modal
import requests


BUNDLED_ROOT = Path(__file__).resolve().parents[1] / "solver" / "google_ai_mode"
SOURCE_ROOT = Path(
    os.environ.get(
        "PORTAL_GOOGLE_SOLVER_SOURCE",
        str(BUNDLED_ROOT),
    )
)
DETECTOR_ROOT = Path(
    os.environ.get(
        "PORTAL_GOOGLE_DETECTOR_SOURCE",
        str(BUNDLED_ROOT),
    )
)

LEGACY_SOLVER = SOURCE_ROOT / "api_resolvedora_resolver.py"
GOOGLE_SOLVER = SOURCE_ROOT / "api_resolvedora_resolver_google_ia.py"
DETECTOR = DETECTOR_ROOT / "detector_visual.py"
GOOGLE_CLIENT = SOURCE_ROOT / "google_ia_requests.py"
CHROME_WRAPPER = Path(__file__).with_name("chrome_modal_no_sandbox.sh")

if modal.is_local():
    for required in (LEGACY_SOLVER, GOOGLE_SOLVER, GOOGLE_CLIENT, DETECTOR, CHROME_WRAPPER):
        if not required.is_file():
            raise RuntimeError(f"Arquivo obrigatorio ausente: {required}")

BROWSERLESS_IMAGE = (
    "browserless/chrome@sha256:"
    "57d19e414d9fe4ae9d2ab12ba768c97f38d51246c5b31af55a009205c136012f"
)
PORT = int(os.environ.get("PORTAL_GOOGLE_SOLVER_PORT", "8765"))
INTERNAL_PORT = PORT + 1
PROXY_HOSTNAME = os.environ.get(
    "PRUMO_MODAL_PROXY_HOSTNAME", ""
).strip()
PROXY_LISTENER = os.environ.get(
    "PRUMO_MODAL_PROXY_LISTENER", "127.0.0.1:31480"
).strip()
PROXY_ENABLED = os.environ.get("PRUMO_MODAL_PROXY_ENABLED", "0").strip() == "1"
GOOGLE_STATE_SEED = Path("/google-ai-seed")
GOOGLE_STATE_ACTIVE = Path("/tmp/google-ai-state")

google_state = modal.Volume.from_name(
    "prumo-portal-google-ai-state", create_if_missing=False
)
proxy_access_secrets = (
    [modal.Secret.from_name("prumo-modal-proxy-access")]
    if PROXY_ENABLED and PROXY_HOSTNAME
    else []
)

image = (
    modal.Image.from_registry(BROWSERLESS_IMAGE, add_python="3.11")
    .apt_install("ca-certificates", "curl", "socat", "xvfb")
    .run_commands(
        "curl -fsSL "
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 "
        "-o /usr/local/bin/cloudflared",
        "chmod +x /usr/local/bin/cloudflared",
    )
    .pip_install(
        "beautifulsoup4",
        "browser-cookie3",
        "opencv-python-headless",
        "pillow",
        "requests",
        "websocket-client",
    )
    .add_local_file(
        CHROME_WRAPPER,
        "/usr/local/bin/google-chrome-prumo",
        copy=True,
    )
    .run_commands("chmod +x /usr/local/bin/google-chrome-prumo")
    .add_local_file(LEGACY_SOLVER, "/app/api_resolvedora_resolver.py")
    .add_local_file(GOOGLE_SOLVER, "/app/api_resolvedora_resolver_google_ia.py")
    .add_local_file(GOOGLE_CLIENT, "/app/google-ai-client/google_ia_requests.py")
    .add_local_file(DETECTOR, "/app/detector/detector_visual.py")
)

app = modal.App("prumo-portal-nacional-google-solver", image=image)


def _wait_for_listener(listener: str, timeout: float = 20.0) -> None:
    host, raw_port = listener.rsplit(":", 1)
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, int(raw_port)), timeout=1.0):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"Listener da proxy nao abriu: {last_error}")


def _start_proxy_tunnel() -> None:
    if not (PROXY_ENABLED and PROXY_HOSTNAME):
        return
    tunnel_env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        tunnel_env.pop(key, None)
    command = [
        "cloudflared",
        "access",
        "tcp",
        "--hostname",
        PROXY_HOSTNAME,
        "--url",
        PROXY_LISTENER,
        "--loglevel",
        "warn",
    ]
    service_token_id = tunnel_env.get("TUNNEL_SERVICE_TOKEN_ID", "").strip()
    service_token_secret = tunnel_env.get("TUNNEL_SERVICE_TOKEN_SECRET", "").strip()
    if bool(service_token_id) != bool(service_token_secret):
        raise RuntimeError("Service token da proxy esta incompleto.")
    if service_token_id:
        command.extend(
            [
                "--service-token-id",
                service_token_id,
                "--service-token-secret",
                service_token_secret,
            ]
        )
    subprocess.Popen(
        command,
        env=tunnel_env,
    )
    _wait_for_listener(PROXY_LISTENER)


def _prepare_instance_state() -> None:
    """Cria estado privado por container para permitir paralelismo real.

    O Volume e somente uma semente. Cada container trabalha em /tmp, evitando
    corrida entre quatro sessoes anonimas do Google Modo IA.
    """
    GOOGLE_STATE_ACTIVE.mkdir(parents=True, exist_ok=True)
    for name in (
        "cookies_google_limpo.json",
        "cookies_google_limpo_backup.json",
        "ask_session.json",
        "ask_session_backup.json",
    ):
        source = GOOGLE_STATE_SEED / name
        target = GOOGLE_STATE_ACTIVE / name
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)


def _prewarm_google_ai_session() -> None:
    """Forma a sessão de imagem do Modo IA antes de liberar concorrência."""
    if os.environ.get("GOOGLE_AI_PREWARM", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    lock_path = GOOGLE_STATE_ACTIVE / ".session_recovery.lock"
    try:
        lock_path.unlink()
        print("[prewarm] lock antigo de recuperacao removido.", flush=True)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[prewarm] nao consegui remover lock antigo: {type(exc).__name__}", flush=True)

    image_path = Path("/tmp/google-ai-prewarm.png")
    try:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (180, 120), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((30, 30, 150, 90), outline="black", width=3)
        draw.ellipse((70, 45, 110, 85), fill="royalblue")
        image.save(image_path)
    except Exception as exc:
        print(f"[prewarm] imagem minima nao criada: {type(exc).__name__}", flush=True)
        return

    command = [
        "python",
        "-u",
        "/app/google-ai-client/google_ia_requests.py",
        "Responda apenas: ok.",
        "--imagem",
        str(image_path),
        "--timeout",
        "90",
        "--tentativas",
        "1",
        "--sem-metricas",
    ]
    for attempt in range(1, 3):
        started = time.monotonic()
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=170,
            check=False,
        )
        elapsed = time.monotonic() - started
        if completed.returncode == 0:
            print(f"[prewarm] sessao Google Modo IA pronta em {elapsed:.1f}s.", flush=True)
            return
        tail = (completed.stderr or "").strip().splitlines()[-1:] or [""]
        print(
            f"[prewarm] tentativa {attempt}/2 falhou em {elapsed:.1f}s: {tail[0][:220]}",
            flush=True,
        )
        time.sleep(2 * attempt)


@app.function(
    secrets=proxy_access_secrets,
    cpu=0.25,
    memory=512,
    timeout=120,
    env={
        "PRUMO_MODAL_PROXY_HOSTNAME": PROXY_HOSTNAME,
        "PRUMO_MODAL_PROXY_LISTENER": PROXY_LISTENER,
        "PRUMO_MODAL_PROXY_ENABLED": "1" if PROXY_ENABLED else "0",
    },
)
def proxy_probe() -> str:
    """Comprova o egress da proxy sem expor o IP nem subir navegador."""
    _start_proxy_tunnel()

    direct = requests.Session()
    direct.trust_env = False
    proxied = requests.Session()
    proxied.trust_env = False
    if PROXY_ENABLED and PROXY_HOSTNAME:
        proxied.proxies.update(
            {"http": f"http://{PROXY_LISTENER}", "https": f"http://{PROXY_LISTENER}"}
        )

    def check_ip(session: requests.Session) -> str:
        response = session.get("https://api64.ipify.org", timeout=30)
        response.raise_for_status()
        return hashlib.sha256(response.text.strip().encode()).hexdigest()

    started = time.monotonic()
    direct_hash = check_ip(direct)
    direct_seconds = time.monotonic() - started
    started = time.monotonic()
    proxy_hash = check_ip(proxied)
    proxy_seconds = time.monotonic() - started
    started = time.monotonic()
    google = proxied.get("https://www.google.com/generate_204", timeout=30)
    google_seconds = time.monotonic() - started
    return json.dumps({
        "direct_hash": direct_hash,
        "proxy_hash": proxy_hash,
        "route": "residential" if PROXY_HOSTNAME else "direct",
        "same_egress": direct_hash == proxy_hash,
        "direct_seconds": round(direct_seconds, 3),
        "proxy_seconds": round(proxy_seconds, 3),
        "google_status": google.status_code,
        "google_seconds": round(google_seconds, 3),
    }, sort_keys=True)


@app.function(
    secrets=proxy_access_secrets,
    volumes={"/google-ai-seed": google_state},
    min_containers=1,
    buffer_containers=3,
    max_containers=4,
    startup_timeout=240,
    timeout=86400,
    scaledown_window=180,
    cpu=(1.5, 2.0),
    memory=(2048, 3072),
    env={
        "GOOGLE_AI_PROJECT": "/app/google-ai-client",
        "GOOGLE_AI_STATE_DIR": "/tmp/google-ai-state",
        "GOOGLE_AI_ARTIFACT_ROOT": "/tmp/solver-artifacts",
        "GOOGLE_CHROME_BIN": "/usr/local/bin/google-chrome-prumo",
        "MODO_IA_DETECTOR_PROJECT": "/app/detector",
        "HOST": "0.0.0.0",
        "SOLVER_HEADLESS": "0",
        "GOOGLE_AI_RECOVERY_VERBOSE": "1",
        "GOOGLE_AI_PREWARM": "1",
        # Cliente v16 do projeto validado. No Modal a base Ubuntu nao traz
        # Firefox apt usavel; use Chrome/CDP em Xvfb, com timeout HTTP curto.
        "GOOGLE_AI_RECOVERY_POLICY": "chrome",
        "GOOGLE_AI_CHROME_RECOVERY_ATTEMPTS": "3",
        "GOOGLE_AI_RECOVERY_WAIT_SECONDS": "10",
        "GOOGLE_AI_FIREFOX_FALLBACK": "0",
        "PRUMO_MODAL_PROXY_HOSTNAME": PROXY_HOSTNAME,
        "PRUMO_MODAL_PROXY_LISTENER": PROXY_LISTENER,
        "PRUMO_MODAL_PROXY_ENABLED": "1" if PROXY_ENABLED else "0",
        # Direto por padrao. A proxy residencial so entra quando o Access tiver
        # service-token de maquina validado e PRUMO_MODAL_PROXY_ENABLED=1.
        "HTTP_PROXY": f"http://{PROXY_LISTENER}" if PROXY_ENABLED and PROXY_HOSTNAME else "",
        "HTTPS_PROXY": f"http://{PROXY_LISTENER}" if PROXY_ENABLED and PROXY_HOSTNAME else "",
        "NO_PROXY": "127.0.0.1,localhost",
    },
)
@modal.concurrent(max_inputs=2, target_inputs=1)
@modal.web_server(PORT, startup_timeout=240)
def solver_server() -> None:
    _start_proxy_tunnel()
    _prepare_instance_state()
    _prewarm_google_ai_session()
    # O projeto organizado mantem o listener da API em 127.0.0.1. O relay
    # expoe somente a porta esperada pelo web_server do Modal.
    relay_command = [
        "socat",
        f"TCP-LISTEN:{PORT},fork,reuseaddr,bind=0.0.0.0",
        f"TCP:127.0.0.1:{INTERNAL_PORT}",
    ]
    subprocess.Popen(
        [
            "xvfb-run",
            "-a",
            "-s",
            "-screen 0 1365x900x24",
            "python",
            "-u",
            "/app/api_resolvedora_resolver_google_ia.py",
            "--port",
            str(INTERNAL_PORT),
            "--browser",
            "/usr/local/bin/google-chrome-prumo",
            "--max-browsers",
            "1",
            "--max-provider-failures",
            "30",
            "--max-solver-failures",
            "20",
            "--max-solve-seconds",
            "150",
        ]
    )
    # Nao abra a porta publica antes da API interna estar pronta. Caso contrario,
    # containers frios aceitam a requisicao e devolvem 500 no relay do Modal.
    _wait_for_listener(f"127.0.0.1:{INTERNAL_PORT}", timeout=90)
    subprocess.Popen(relay_command)
