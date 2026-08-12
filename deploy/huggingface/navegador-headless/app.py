from __future__ import annotations

import asyncio
import atexit
import base64
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import gradio as gr
import spaces
from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from gradio.routes import App as GradioApp
from fastapi.staticfiles import StaticFiles

DISPLAY = ":99"
SCREEN = "1440x900x24"
VNC_PORT = 5900
HTTP_PORT = 7860
PROFILE_DIR = Path("/tmp/google-chrome-profile")
DOWNLOAD_DIR = Path("/tmp/google-chrome-downloads")
LOG_DIR = Path("/tmp/google-chrome-container-logs")
PROCESSES: list[subprocess.Popen] = []
CHROME_ROOT = Path("/tmp/google-chrome-stable")
CHROME_DEB = Path("/tmp/google-chrome-stable_current_amd64.deb")
CHROME_URL = "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
DESKTOP_ENABLED = os.environ.get("PRUMO_HF_DESKTOP_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on",
}


def log(message: str) -> None:
    print(f"[google-chrome-container] {message}", flush=True)


def find_binary(*names: str) -> str:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError(f"Executável não encontrado: {', '.join(names)}")


def ensure_google_chrome() -> str:
    """Obtém e extrai o pacote oficial sem instalar como root."""
    for name in ("google-chrome-stable", "google-chrome"):
        path = shutil.which(name)
        if path:
            os.environ["GOOGLE_CHROME_BIN"] = path
            return path

    binary = CHROME_ROOT / "opt/google/chrome/google-chrome"
    if binary.is_file():
        os.environ["GOOGLE_CHROME_BIN"] = str(binary)
        return str(binary)

    CHROME_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = CHROME_DEB.with_suffix(".download")
    log("Baixando o pacote oficial Google Chrome Stable.")
    try:
        urllib.request.urlretrieve(CHROME_URL, temporary)
        if temporary.stat().st_size < 50_000_000:
            raise RuntimeError("Pacote Google Chrome recebido com tamanho inválido.")
        temporary.replace(CHROME_DEB)
        subprocess.run(
            [find_binary("dpkg-deb"), "--extract", str(CHROME_DEB), str(CHROME_ROOT)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
    finally:
        temporary.unlink(missing_ok=True)

    if not binary.is_file():
        raise RuntimeError("Google Chrome não foi encontrado após extrair o pacote oficial.")
    binary.chmod(binary.stat().st_mode | 0o111)
    os.environ["GOOGLE_CHROME_BIN"] = str(binary)
    return str(binary)


def test_google_ai(image_path: str | None, question: str) -> dict[str, object]:
    """Executa uma prova visual real do Modo IA usando o egress deste Space."""
    started = time.perf_counter()
    if not image_path:
        return {"ok": False, "error": "Selecione uma imagem."}
    prompt = (question or "O que há nesta imagem? Descreva objetivamente.").strip()
    chrome = ensure_google_chrome()
    os.environ.setdefault("GOOGLE_AI_STATE_DIR", "/tmp/google-ai-mode-state")
    os.environ.setdefault("GOOGLE_AI_RECOVERY_POLICY", "chrome")
    os.environ.setdefault("GOOGLE_AI_FIREFOX_FALLBACK", "0")
    os.environ.setdefault("GOOGLE_AI_CHROME_RECOVERY_ATTEMPTS", "1")
    os.environ.setdefault("GOOGLE_AI_RECOVERY_WAIT_SECONDS", "4,8")
    os.environ["GOOGLE_CHROME_BIN"] = chrome

    try:
        import google_ia_requests as google_ai

        result = google_ai.query_google_ai(
            prompt,
            timeout=60,
            image_path=image_path,
            # Existem dois Spaces, duas contas Modal e o ThinkPad. Uma
            # segunda tentativa no mesmo egress aumenta a latencia e o risco
            # de bloqueio sem adicionar uma rota nova.
            attempts=1,
            allow_browser_recovery=True,
        )
        return {
            "ok": True,
            "answer": result.answer,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "http_requests": result.http_requests,
            "ai_queries": result.ai_queries,
            "source_count": len(result.sources),
            "chrome": Path(chrome).name,
        }
    except Exception as exc:
        detail = str(exc)[:700]
        lowered = detail.lower()
        return {
            "ok": False,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "error_type": type(exc).__name__,
            "error": detail,
            "unusual_traffic": "unusual traffic" in lowered or "/sorry/" in lowered,
        }


@spaces.GPU(duration=1)
def zero_gpu_compatibility_probe() -> str:
    """Mantem o runtime ZeroGPU valido sem reservar GPU para o fluxo CPU."""
    return "ok"


def start_process(name: str, command: list[str], env: dict[str, str]) -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handle = (LOG_DIR / f"{name}.log").open("ab", buffering=0)
    log(f"Iniciando {name}: {' '.join(command)}")
    process = subprocess.Popen(
        command,
        stdout=handle,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    process._log_handle = handle  # type: ignore[attr-defined]
    PROCESSES.append(process)
    return process


def wait_for_path(path: Path, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.2)
    raise RuntimeError(f"Tempo excedido aguardando {path}")


def wait_for_port(port: int, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.4)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.25)
    raise RuntimeError(f"Tempo excedido aguardando a porta {port}")


def novnc_directory() -> Path:
    for path in (Path("/usr/share/novnc"), Path("/usr/share/noVNC")):
        if (path / "vnc.html").exists():
            return path
    raise RuntimeError("Arquivos do noVNC não foram encontrados.")


def cleanup() -> None:
    for process in reversed(PROCESSES):
        if process.poll() is not None:
            continue
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except Exception:
            pass
    time.sleep(0.5)
    for process in reversed(PROCESSES):
        if process.poll() is not None:
            continue
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            pass
    for process in PROCESSES:
        handle = getattr(process, "_log_handle", None)
        if handle:
            try:
                handle.close()
            except Exception:
                pass


def start_desktop() -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY
    env["HOME"] = "/tmp"
    env["XDG_RUNTIME_DIR"] = "/tmp/xdg-runtime"
    Path(env["XDG_RUNTIME_DIR"]).mkdir(parents=True, exist_ok=True)
    os.chmod(env["XDG_RUNTIME_DIR"], 0o700)

    start_process(
        "xvfb",
        [find_binary("Xvfb"), DISPLAY, "-screen", "0", SCREEN, "-ac", "+extension", "GLX", "+render", "-noreset"],
        env,
    )
    wait_for_path(Path("/tmp/.X11-unix/X99"))
    start_process("openbox", [find_binary("openbox-session", "openbox")], env)
    start_process(
        "x11vnc",
        [
            find_binary("x11vnc"),
            "-display", DISPLAY,
            "-forever",
            "-shared",
            "-nopw",
            "-rfbport", str(VNC_PORT),
            "-noxdamage",
            "-repeat",
            "-xkb",
        ],
        env,
    )
    wait_for_port(VNC_PORT)

    chrome = ensure_google_chrome()
    start_process(
        "google-chrome",
        [
            chrome,
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-setuid-sandbox",
            "--no-first-run",
            "--password-store=basic",
            "--start-maximized",
            f"--user-data-dir={PROFILE_DIR}",
            "--window-size=1440,900",
            "https://www.google.com/",
        ],
        env,
    )
    log("Desktop gráfico iniciado: Xvfb + Openbox + Google Chrome Stable + x11vnc.")


NOVNC_URL = "/desktop"
CSS = """
.gradio-container { max-width: 100% !important; padding: 0 !important; }
#desktop-frame { width: 100%; height: calc(100vh - 84px); min-height: 720px; border: 0; background: #111; }
#top-info { padding: 8px 14px; margin: 0; }
footer { display: none !important; }
"""

with gr.Blocks(title="Google Chrome Container", css=CSS) as demo:
    gr.Markdown(
        "**Google Chrome Stable oficial no container** — Xvfb + Openbox + x11vnc + noVNC. "
        "O perfil permanece enquanto o container estiver ativo.",
        elem_id="top-info",
    )
    gr.HTML(f'<iframe id="desktop-frame" src="{NOVNC_URL}" allow="clipboard-read; clipboard-write"></iframe>')
    with gr.Accordion("Diagnóstico Google Modo IA", open=False):
        diagnostic_image = gr.Image(type="filepath", label="Imagem")
        diagnostic_question = gr.Textbox(
            label="Pergunta",
            value="O que há nesta imagem? Descreva objetivamente.",
        )
        diagnostic_button = gr.Button("Testar Modo IA", variant="primary")
        diagnostic_result = gr.JSON(label="Resultado")
        diagnostic_button.click(
            test_google_ai,
            inputs=[diagnostic_image, diagnostic_question],
            outputs=diagnostic_result,
            api_name="test_google_ai",
        )
    with gr.Row(visible=False):
        zero_gpu_button = gr.Button("ZeroGPU compatibility")
        zero_gpu_output = gr.Textbox()
        zero_gpu_button.click(
            zero_gpu_compatibility_probe,
            inputs=[],
            outputs=zero_gpu_output,
            api_name=False,
        )

async def desktop_page(request: Request) -> HTMLResponse:
    # O token __sign aparece na URL do Gradio privado. document.referrer preserva
    # essa URL quando o iframe /desktop é carregado.
    html = """<!doctype html>
<html lang="pt-BR">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Google Chrome</title></head>
<body style="margin:0;background:#111">
<script>
(() => {
  let sign = new URL(location.href).searchParams.get('__sign') || '';
  try {
    if (!sign && document.referrer) sign = new URL(document.referrer).searchParams.get('__sign') || '';
  } catch (_) {}
  let wsPath = 'websockify';
  if (sign) wsPath += '?__sign=' + encodeURIComponent(sign);
  const q = new URLSearchParams({
    autoconnect: 'true', reconnect: 'true', resize: 'remote',
    quality: '7', compression: '6', path: wsPath
  });
  if (sign) q.set('__sign', sign);
  location.replace('/novnc/vnc.html?' + q.toString());
})();
</script>
</body></html>"""
    return HTMLResponse(html)


async def websocket_to_vnc(websocket: WebSocket) -> None:
    protocols = websocket.headers.get("sec-websocket-protocol", "")
    use_base64 = "base64" in protocols and "binary" not in protocols
    log(f"WebSocket VNC recebido; protocolos={protocols or '(nenhum)'}")
    # O proxy de Spaces privados pode remover Sec-WebSocket-Protocol. Aceitar
    # sem ecoar subprotocolo continua permitindo frames binários do noVNC.
    await websocket.accept()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", VNC_PORT)
    except Exception as exc:
        log(f"Falha ao conectar no VNC local: {type(exc).__name__}: {exc}")
        await websocket.close(code=1011)
        return

    async def browser_to_vnc() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                data = message.get("bytes")
                if data is None and message.get("text") is not None:
                    text = message["text"]
                    data = base64.b64decode(text) if use_base64 else text.encode("latin1")
                if data:
                    writer.write(data)
                    await writer.drain()
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            return

    async def vnc_to_browser() -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                if use_base64:
                    await websocket.send_text(base64.b64encode(data).decode("ascii"))
                else:
                    await websocket.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            return

    tasks = [asyncio.create_task(browser_to_vnc()), asyncio.create_task(vnc_to_browser())]
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    writer.close()
    await writer.wait_closed()
    try:
        await websocket.close()
    except Exception:
        pass
    log("WebSocket VNC encerrado")


# O Gradio 5 recria seu FastAPI durante launch(). Interceptamos essa criação
# para anexar as rotas noVNC ao app final sem quebrar o hook do ZeroGPU.
_original_create_app = GradioApp.create_app


def create_app_with_desktop(*args, **kwargs):
    app = _original_create_app(*args, **kwargs)
    if not getattr(app, "_google_chrome_desktop_routes", False):
        app.add_api_route("/desktop", desktop_page, methods=["GET"], response_class=HTMLResponse)
        app.mount("/novnc", StaticFiles(directory=str(novnc_directory()), html=True), name="novnc")
        app.add_api_websocket_route("/websockify", websocket_to_vnc)
        app._google_chrome_desktop_routes = True
    return app


GradioApp.create_app = staticmethod(create_app_with_desktop)


if __name__ == "__main__":
    atexit.register(cleanup)
    if DESKTOP_ENABLED:
        start_desktop()
    else:
        # O desktop permanente (Chrome + Xvfb + Openbox + x11vnc) consumia o
        # limite de PIDs/threads do Space. A API entao falhava antes mesmo da
        # analise com `can't start new thread`. Baixe o Chrome no startup, mas
        # deixe cada recuperacao abrir seu perfil isolado sob demanda.
        chrome = ensure_google_chrome()
        os.environ["GOOGLE_CHROME_BIN"] = chrome
        state_dir = Path("/tmp/google-ai-mode-state")
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / ".session_recovery.lock").unlink(missing_ok=True)
        log("Modo provedor ativo: Chrome real sob demanda; desktop permanente desativado.")
    demo.queue(default_concurrency_limit=1).launch(
        server_name="0.0.0.0",
        server_port=HTTP_PORT,
        ssr_mode=False,
        show_error=True,
    )
