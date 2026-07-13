"""Deploy reproduzivel do resolvedor Google Modo IA do Portal Nacional.

O codigo validado no projeto organizado fica versionado em ``solver/``. Apenas
cookies anonimos e estado efemero ficam no Volume privado do Modal.
"""

from __future__ import annotations

import os
import hashlib
import json
import socket
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

google_state = modal.Volume.from_name(
    "prumo-portal-google-ai-state", create_if_missing=False
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
    if not PROXY_HOSTNAME:
        return
    tunnel_env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        tunnel_env.pop(key, None)
    subprocess.Popen(
        [
            "cloudflared",
            "access",
            "tcp",
            "--hostname",
            PROXY_HOSTNAME,
            "--url",
            PROXY_LISTENER,
            "--loglevel",
            "warn",
        ],
        env=tunnel_env,
    )
    _wait_for_listener(PROXY_LISTENER)


@app.function(
    cpu=0.25,
    memory=512,
    timeout=120,
    env={
        "PRUMO_MODAL_PROXY_HOSTNAME": PROXY_HOSTNAME,
        "PRUMO_MODAL_PROXY_LISTENER": PROXY_LISTENER,
    },
)
def proxy_probe() -> str:
    """Comprova o egress da proxy sem expor o IP nem subir navegador."""
    _start_proxy_tunnel()

    direct = requests.Session()
    direct.trust_env = False
    proxied = requests.Session()
    proxied.trust_env = False
    if PROXY_HOSTNAME:
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
    volumes={"/google-ai": google_state},
    min_containers=1,
    max_containers=1,
    startup_timeout=240,
    timeout=86400,
    scaledown_window=600,
    cpu=(1.5, 2.0),
    memory=(2048, 3072),
    env={
        "GOOGLE_AI_PROJECT": "/app/google-ai-client",
        "GOOGLE_AI_STATE_DIR": "/google-ai",
        "GOOGLE_AI_ARTIFACT_ROOT": "/google-ai/solver-artifacts",
        "MODO_IA_DETECTOR_PROJECT": "/app/detector",
        "HOST": "0.0.0.0",
        "SOLVER_HEADLESS": "0",
        "GOOGLE_AI_RECOVERY_VERBOSE": "1",
        # No Modal, o Chrome/CDP foi validado. O Firefox falha no runtime e
        # consumia ate 102 s antes de devolver o mesmo erro.
        "GOOGLE_AI_CHROME_RECOVERY_ATTEMPTS": "1",
        "GOOGLE_AI_FIREFOX_FALLBACK": "0",
        "PRUMO_MODAL_PROXY_HOSTNAME": PROXY_HOSTNAME,
        "PRUMO_MODAL_PROXY_LISTENER": PROXY_LISTENER,
        # Direto por padrao. O hostname residencial permanece como fallback
        # configuravel para incidentes de rota/origem.
        "HTTP_PROXY": f"http://{PROXY_LISTENER}" if PROXY_HOSTNAME else "",
        "HTTPS_PROXY": f"http://{PROXY_LISTENER}" if PROXY_HOSTNAME else "",
        "NO_PROXY": "127.0.0.1,localhost",
    },
)
@modal.concurrent(max_inputs=2, target_inputs=2)
@modal.web_server(PORT, startup_timeout=240)
def solver_server() -> None:
    _start_proxy_tunnel()
    # O projeto organizado mantem o listener da API em 127.0.0.1. O relay
    # expoe somente a porta esperada pelo web_server do Modal.
    subprocess.Popen(
        [
            "socat",
            f"TCP-LISTEN:{PORT},fork,reuseaddr,bind=0.0.0.0",
            f"TCP:127.0.0.1:{INTERNAL_PORT}",
        ]
    )
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
            "4",
            "--max-solver-failures",
            "6",
            "--max-solve-seconds",
            "180",
        ]
    )
