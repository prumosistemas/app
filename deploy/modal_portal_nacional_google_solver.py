"""Deploy do resolvedor Google Modo IA validado no projeto organizado.

O codigo-fonte do resolvedor continua no projeto de referencia pedido para o
Portal Nacional. Os cookies anonimos do Google ficam em um Volume privado do
Modal e nunca entram no Git nem na imagem.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import modal


SOURCE_ROOT = Path(
    os.environ.get(
        "PORTAL_GOOGLE_SOLVER_SOURCE",
        r"C:\Users\ryang\Desktop\projetosv2\avancar\portal-nacional\projeto organizado definitido",
    )
)
DETECTOR_ROOT = Path(
    os.environ.get(
        "PORTAL_GOOGLE_DETECTOR_SOURCE",
        r"C:\Users\ryang\Desktop\projetosv2\modo_ia_detector_visual",
    )
)

LEGACY_SOLVER = SOURCE_ROOT / "api_resolvedora_resolver.py"
GOOGLE_SOLVER = SOURCE_ROOT / "api_resolvedora_resolver_google_ia.py"
DETECTOR = DETECTOR_ROOT / "detector_visual.py"
CHROME_WRAPPER = Path(__file__).with_name("chrome_modal_no_sandbox.sh")

if modal.is_local():
    for required in (LEGACY_SOLVER, GOOGLE_SOLVER, DETECTOR, CHROME_WRAPPER):
        if not required.is_file():
            raise RuntimeError(f"Arquivo obrigatorio ausente: {required}")

BROWSERLESS_IMAGE = (
    "browserless/chrome@sha256:"
    "57d19e414d9fe4ae9d2ab12ba768c97f38d51246c5b31af55a009205c136012f"
)
PORT = int(os.environ.get("PORTAL_GOOGLE_SOLVER_PORT", "8765"))
INTERNAL_PORT = PORT + 1
PROXY_HOSTNAME = os.environ.get(
    "PRUMO_MODAL_PROXY_HOSTNAME", "modal-proxy.prumosistemas.com.br"
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
        "curl -fsSL 'https://download.mozilla.org/?product=firefox-latest&os=linux64&lang=en-US' "
        "-o /tmp/firefox.tar.xz",
        "tar -xf /tmp/firefox.tar.xz -C /opt",
        "ln -sf /opt/firefox/firefox /usr/local/bin/firefox",
        "rm -f /tmp/firefox.tar.xz",
        "firefox --version",
    )
    .pip_install(
        "beautifulsoup4",
        "browser-cookie3",
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
    volumes={"/google-ai": google_state},
    min_containers=1,
    max_containers=1,
    startup_timeout=240,
    timeout=86400,
    scaledown_window=600,
    cpu=(2.0, 4.0),
    memory=(3072, 8192),
    env={
        "GOOGLE_AI_PROJECT": "/google-ai",
        "MODO_IA_DETECTOR_PROJECT": "/app/detector",
        "HOST": "0.0.0.0",
        "SOLVER_HEADLESS": "0",
        "PRUMO_MODAL_PROXY_HOSTNAME": PROXY_HOSTNAME,
        "PRUMO_MODAL_PROXY_LISTENER": PROXY_LISTENER,
        # Mantem Google, hCaptcha e Portal na mesma saida brasileira. O
        # cliente requests respeita estas variaveis; localhost fica fora.
        "HTTP_PROXY": f"http://{PROXY_LISTENER}" if PROXY_HOSTNAME else "",
        "HTTPS_PROXY": f"http://{PROXY_LISTENER}" if PROXY_HOSTNAME else "",
        "NO_PROXY": "127.0.0.1,localhost",
    },
)
@modal.concurrent(max_inputs=1, target_inputs=1)
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
            "6",
            "--max-solver-failures",
            "8",
            "--max-solve-seconds",
            "240",
        ]
    )
