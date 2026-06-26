import os
import subprocess

import modal


BROWSERLESS_IMAGE = (
    "browserless/chrome@sha256:"
    "57d19e414d9fe4ae9d2ab12ba768c97f38d51246c5b31af55a009205c136012f"
)

CAPACITY_PER_CONTAINER = int(os.environ.get("PRUMO_MODAL_CAPACITY_PER_CONTAINER", "8"))
MAX_CONTAINERS = int(os.environ.get("PRUMO_MODAL_MAX_CONTAINERS", "2"))
TARGET_INPUTS = int(os.environ.get("PRUMO_MODAL_TARGET_INPUTS", str(max(1, CAPACITY_PER_CONTAINER - 2))))
QUEUE_LENGTH = int(os.environ.get("PRUMO_MODAL_QUEUE_LENGTH", str(CAPACITY_PER_CONTAINER * MAX_CONTAINERS * 2)))

image = modal.Image.from_registry(BROWSERLESS_IMAGE, add_python="3.11")

app = modal.App("prumo-browserless", image=image)


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
        "TIMEOUT": "600000",
        "CONNECTION_TIMEOUT": "600000",
        "DEFAULT_LAUNCH_ARGS": '["--no-sandbox"]',
    },
)
@modal.concurrent(max_inputs=CAPACITY_PER_CONTAINER, target_inputs=TARGET_INPUTS)
@modal.web_server(3000, startup_timeout=180)
def browserless_server():
    subprocess.Popen(
        ["/bin/bash", "./start.sh"],
        cwd="/usr/src/app",
    )
