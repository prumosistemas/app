import os
import subprocess

import modal


BROWSERLESS_IMAGE = (
    "browserless/chrome@sha256:"
    "57d19e414d9fe4ae9d2ab12ba768c97f38d51246c5b31af55a009205c136012f"
)
PORT = int(os.environ.get("PORTAL_NACIONAL_SOLVER_PORT", "8765"))
BROWSERS_PER_CONTAINER = int(os.environ.get("PORTAL_NACIONAL_SOLVER_BROWSERS_PER_CONTAINER", "4"))
MAX_CONTAINERS = int(os.environ.get("PORTAL_NACIONAL_SOLVER_MAX_CONTAINERS", "4"))
REQUESTS_PER_CONTAINER = int(os.environ.get("PORTAL_NACIONAL_SOLVER_REQUESTS_PER_CONTAINER", str(BROWSERS_PER_CONTAINER * 2)))

image = (
    modal.Image.from_registry(BROWSERLESS_IMAGE, add_python="3.11")
    .apt_install("xvfb")
    .pip_install("requests", "websocket-client", "pillow")
    .add_local_file("deploy/portal_nacional_solver.py", remote_path="/app/portal_nacional_solver.py")
)

app = modal.App("prumo-portal-nacional-solver", image=image)


@app.function(
    secrets=[modal.Secret.from_name("prumo-portal-nacional-solver")],
    min_containers=0,
    max_containers=MAX_CONTAINERS,
    startup_timeout=180,
    timeout=86400,
    scaledown_window=300,
    cpu=(2.0, 4.0),
    memory=(2048, 8192),
    env={"HOST": "0.0.0.0", "SOLVER_HEADLESS": "0"},
)
@modal.concurrent(max_inputs=REQUESTS_PER_CONTAINER, target_inputs=max(1, BROWSERS_PER_CONTAINER))
@modal.web_server(PORT, startup_timeout=180)
def solver_server():
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
        ],
    )
