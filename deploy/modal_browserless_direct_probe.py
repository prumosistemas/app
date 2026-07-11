"""Browserless efêmero para provar o ISS saindo direto do Modal.

Este app não substitui o ``prumo-browserless`` e usa somente uma sessão. Ele
existe para um teste A/B controlado; nenhuma configuração da produção aponta
para esta URL.
"""

import os
import subprocess

import modal


BROWSERLESS_IMAGE = (
    "browserless/chrome@sha256:"
    "57d19e414d9fe4ae9d2ab12ba768c97f38d51246c5b31af55a009205c136012f"
)

image = modal.Image.from_registry(BROWSERLESS_IMAGE, add_python="3.11")
app = modal.App("prumo-browserless-direct-probe", image=image)


@app.function(
    secrets=[modal.Secret.from_name("prumo-browserless")],
    min_containers=0,
    max_containers=1,
    startup_timeout=180,
    timeout=1800,
    scaledown_window=60,
    cpu=(1.0, 2.0),
    memory=(1024, 4096),
    env={
        "HOST": "0.0.0.0",
        "PORT": "3000",
        "CONCURRENT": "1",
        "MAX_CONCURRENT_SESSIONS": "1",
        "QUEUED": "2",
        "QUEUE_LENGTH": "2",
        "TIMEOUT": "900000",
        "CONNECTION_TIMEOUT": "900000",
        "DEFAULT_LAUNCH_ARGS": '["--no-sandbox"]',
    },
)
@modal.concurrent(max_inputs=1, target_inputs=1)
@modal.web_server(3000, startup_timeout=180)
def browserless_server():
    subprocess.Popen(["/bin/bash", "./start.sh"], cwd="/usr/src/app", env=os.environ.copy())
