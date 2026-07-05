import json
import os
import socket
import subprocess
import time

import modal


BROWSERLESS_IMAGE = (
    "browserless/chrome@sha256:"
    "57d19e414d9fe4ae9d2ab12ba768c97f38d51246c5b31af55a009205c136012f"
)

CAPACITY_PER_CONTAINER = int(os.environ.get("PRUMO_MODAL_CAPACITY_PER_CONTAINER", "4"))
MAX_CONTAINERS = int(os.environ.get("PRUMO_MODAL_MAX_CONTAINERS", "10"))
TARGET_INPUTS = int(os.environ.get("PRUMO_MODAL_TARGET_INPUTS", str(max(1, CAPACITY_PER_CONTAINER - 2))))
QUEUE_LENGTH = int(os.environ.get("PRUMO_MODAL_QUEUE_LENGTH", str(CAPACITY_PER_CONTAINER * MAX_CONTAINERS * 2)))
PROXY_HOSTNAME = os.environ.get("PRUMO_MODAL_PROXY_HOSTNAME", "modal-proxy.prumosistemas.com.br").strip()
PROXY_LISTENER = os.environ.get("PRUMO_MODAL_PROXY_LISTENER", "127.0.0.1:31480").strip()
PROXY_LOG_LEVEL = os.environ.get("PRUMO_MODAL_PROXY_LOG_LEVEL", "warn").strip() or "warn"

image = (
    modal.Image.from_registry(BROWSERLESS_IMAGE, add_python="3.11")
    .apt_install("ca-certificates", "curl")
    .run_commands(
        "curl -fsSL "
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 "
        "-o /usr/local/bin/cloudflared",
        "chmod +x /usr/local/bin/cloudflared",
    )
)

app = modal.App("prumo-browserless", image=image)


def _default_launch_args() -> str:
    args = ["--no-sandbox"]
    if PROXY_HOSTNAME:
        args.append(f"--proxy-server=http://{PROXY_LISTENER}")
    return json.dumps(args)


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
    secrets=[modal.Secret.from_name("prumo-browserless")],
    min_containers=0,
    max_containers=MAX_CONTAINERS,
    startup_timeout=180,
    timeout=86400,
    scaledown_window=300,
    cpu=(2.0, 4.0),
    memory=(2048, 8192),
    env={
        "HOST": "0.0.0.0",
        "PORT": "3000",
        "CONCURRENT": str(CAPACITY_PER_CONTAINER),
        "MAX_CONCURRENT_SESSIONS": str(CAPACITY_PER_CONTAINER),
        "QUEUED": str(QUEUE_LENGTH),
        "QUEUE_LENGTH": str(QUEUE_LENGTH),
        "TIMEOUT": "1200000",
        "CONNECTION_TIMEOUT": "1200000",
        "DEFAULT_LAUNCH_ARGS": _default_launch_args(),
    },
)
@modal.concurrent(max_inputs=CAPACITY_PER_CONTAINER, target_inputs=TARGET_INPUTS)
@modal.web_server(3000, startup_timeout=180)
def browserless_server():
    _start_proxy_tunnel()
    subprocess.Popen(
        ["/bin/bash", "./start.sh"],
        cwd="/usr/src/app",
    )
