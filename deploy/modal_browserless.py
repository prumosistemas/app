import subprocess

import modal


BROWSERLESS_IMAGE = (
    "browserless/chrome@sha256:"
    "57d19e414d9fe4ae9d2ab12ba768c97f38d51246c5b31af55a009205c136012f"
)

image = modal.Image.from_registry(BROWSERLESS_IMAGE, add_python="3.11")

app = modal.App("prumo-browserless", image=image)


@app.function(
    secrets=[modal.Secret.from_name("prumo-browserless")],
    min_containers=0,
    max_containers=1,
    startup_timeout=180,
    timeout=86400,
    scaledown_window=300,
    cpu=(2.0, 4.0),
    memory=(2048, 8192),
    env={
        "HOST": "0.0.0.0",
        "PORT": "3000",
        "CONCURRENT": "4",
        "MAX_CONCURRENT_SESSIONS": "4",
        "QUEUED": "12",
        "QUEUE_LENGTH": "12",
        "TIMEOUT": "600000",
        "CONNECTION_TIMEOUT": "600000",
        "DEFAULT_LAUNCH_ARGS": '["--no-sandbox"]',
    },
)
@modal.web_server(3000, startup_timeout=180)
def browserless_server():
    subprocess.Popen(
        ["/bin/bash", "./start.sh"],
        cwd="/usr/src/app",
    )
