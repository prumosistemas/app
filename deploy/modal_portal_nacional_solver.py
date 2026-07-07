import os
import socket
import subprocess
import time

import modal


BROWSERLESS_IMAGE = (
    "browserless/chrome@sha256:"
    "57d19e414d9fe4ae9d2ab12ba768c97f38d51246c5b31af55a009205c136012f"
)
PORT = int(os.environ.get("PORTAL_NACIONAL_SOLVER_PORT", "8765"))
BROWSERS_PER_CONTAINER = int(os.environ.get("PORTAL_NACIONAL_SOLVER_BROWSERS_PER_CONTAINER", "6"))
MAX_CONTAINERS = int(os.environ.get("PORTAL_NACIONAL_SOLVER_MAX_CONTAINERS", "4"))
REQUESTS_PER_CONTAINER = int(os.environ.get("PORTAL_NACIONAL_SOLVER_REQUESTS_PER_CONTAINER", str(BROWSERS_PER_CONTAINER)))
PROXY_HOSTNAME = os.environ.get("PRUMO_MODAL_PROXY_HOSTNAME", "modal-proxy.prumosistemas.com.br").strip()
PROXY_LISTENER = os.environ.get("PRUMO_MODAL_PROXY_LISTENER", "127.0.0.1:31480").strip()
PROXY_LOG_LEVEL = os.environ.get("PRUMO_MODAL_PROXY_LOG_LEVEL", "warn").strip() or "warn"

image = (
    modal.Image.from_registry(BROWSERLESS_IMAGE, add_python="3.11")
    .apt_install("ca-certificates", "curl", "xvfb")
    .run_commands(
        "curl -fsSL "
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 "
        "-o /usr/local/bin/cloudflared",
        "chmod +x /usr/local/bin/cloudflared",
    )
    .pip_install("requests", "websocket-client", "pillow")
    .add_local_file("deploy/portal_nacional_solver.py", remote_path="/app/portal_nacional_solver.py")
)

app = modal.App("prumo-portal-nacional-solver", image=image)


def _wait_for_listener(listener: str, timeout: float = 20.0) -> None:
    host, raw_port = listener.rsplit(":", 1)
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, int(raw_port)), timeout=1.0):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"cloudflared listener did not open: {last_error}")


def _start_proxy_tunnel() -> None:
    if not PROXY_HOSTNAME:
        return
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
            PROXY_LOG_LEVEL,
        ]
    )
    _wait_for_listener(PROXY_LISTENER)


@app.function(
    secrets=[modal.Secret.from_name("prumo-portal-nacional-solver")],
    min_containers=1,
    max_containers=MAX_CONTAINERS,
    startup_timeout=180,
    timeout=86400,
    scaledown_window=300,
    cpu=(2.0, 4.0),
    memory=(2048, 8192),
    env={
        "HOST": "0.0.0.0",
        "SOLVER_HEADLESS": "0",
        "PRUMO_MODAL_PROXY_HOSTNAME": PROXY_HOSTNAME,
        "PRUMO_MODAL_PROXY_LISTENER": PROXY_LISTENER,
    },
)
@modal.concurrent(max_inputs=REQUESTS_PER_CONTAINER, target_inputs=max(1, BROWSERS_PER_CONTAINER))
@modal.web_server(PORT, startup_timeout=180)
def solver_server():
    _start_proxy_tunnel()
    subprocess.Popen(
        [
            "xvfb-run",
            "-a",
            "-s",
            "-screen 0 1365x900x24",
            "python",
            "-u",
            "/app/portal_nacional_solver.py",
            "--port",
            str(PORT),
            "--browser",
            "/usr/bin/google-chrome",
            "--max-browsers",
            str(BROWSERS_PER_CONTAINER),
            "--recarregar-nao-9",
        ],
    )
