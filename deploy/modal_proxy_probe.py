import socket
import subprocess
import time
from urllib.parse import urlparse

import modal


PROXY_HOSTNAME = "modal-proxy.prumosistemas.com.br"
PROXY_LISTENER = "127.0.0.1:31480"

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates", "curl")
    .run_commands(
        "curl -fsSL "
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 "
        "-o /usr/local/bin/cloudflared",
        "chmod +x /usr/local/bin/cloudflared",
    )
    .pip_install("requests")
)

app = modal.App("prumo-proxy-tunnel-probe", image=image)


def _wait_for_port(host: str, port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"listener did not open: {last_error}")


@app.function(timeout=120)
def probe() -> dict:
    import requests

    def inspect_route(*, proxies=None) -> dict:
        started = time.monotonic()
        response = requests.get(
            "https://iss.fortaleza.ce.gov.br/grpfor/login.seam",
            proxies=proxies,
            timeout=30,
        )
        body = response.text[:100_000].lower()
        blocked = (
            "geo-ip filter alert" in body
            or "this site has been blocked by the network administrator" in body
        )
        return {
            "status": response.status_code,
            "host": urlparse(response.url).hostname or "",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "geo_blocked": blocked,
            "looks_like_login": any(
                marker in body
                for marker in ("kc-form-login", "login-actions/authenticate", "identifique-se")
            ),
        }

    direct = inspect_route()

    proc = subprocess.Popen(
        [
            "cloudflared",
            "access",
            "tcp",
            "--hostname",
            PROXY_HOSTNAME,
            "--url",
            PROXY_LISTENER,
            "--loglevel",
            "debug",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_port("127.0.0.1", 31480)
        proxy_url = f"http://{PROXY_LISTENER}"
        proxies = {"http": proxy_url, "https": proxy_url}
        proxied = inspect_route(proxies=proxies)
        return {
            "ok": proxied["status"] < 500,
            "direct": direct,
            "proxied": proxied,
        }
    finally:
        proc.terminate()
        try:
            _, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate(timeout=5)
        if proc.returncode not in (0, -15, -9, 143):
            print(stderr[-2000:])


@app.local_entrypoint()
def main():
    result = probe.remote()
    print(result)
