import argparse
import base64
import html
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import websocket
from PIL import Image

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --- CONFIGURAÇÕES DO COHERE ---
COHERE_API_KEY = os.environ.get("COHERE_API_KEY", "").strip()
COHERE_MODEL = "command-a-vision-07-2025"

BASE_DIR = Path(__file__).resolve().parent
API_DIR = BASE_DIR / "api"
SOLVER_API_VERSION = "2026-07-05-modal-xvfb-proxy-hybrid-non9"
SOLVER_PAGE_HOST = os.environ.get("SOLVER_PAGE_HOST", "www.nfse.gov.br").strip() or "www.nfse.gov.br"
NON_9_SETTLE_SECONDS = float(os.environ.get("SOLVER_NON_9_SETTLE_SECONDS", "5"))
NON_9_RELOADS_BEFORE_AI = int(os.environ.get("SOLVER_NON_9_RELOADS_BEFORE_AI", "3"))
PROXY_HOSTNAME = os.environ.get("PRUMO_MODAL_PROXY_HOSTNAME", "").strip()
PROXY_LISTENER = os.environ.get("PRUMO_MODAL_PROXY_LISTENER", "127.0.0.1:31480").strip()
SOLVER_PROFILES = API_DIR / "chrome-profiles-hcaptcha"
CAPTCHA_DIR = API_DIR / "hcaptcha-imagens"
CHALLENGES_DIR = CAPTCHA_DIR / "desafios"
CHALLENGES_9_DIR = CHALLENGES_DIR / "9-tiles"
CHALLENGES_NON_9_DIR = CHALLENGES_DIR / "nao-9-tiles"
BROWSER_OVERRIDE = None
SOLVER_SEMAPHORE = None
ACTIVE_SOLVERS = 0
ACTIVE_LOCK = threading.Lock()
MAX_BROWSERS = 1
FAST_RELOAD_NON_9 = False
LAST_SOLVER_ERROR = threading.local()


def set_solver_error(reason: str, detail: str | None = None) -> None:
    LAST_SOLVER_ERROR.value = {"reason": reason, "detail": detail or reason}


def get_solver_error(default_reason: str = "solver_failed") -> dict:
    value = getattr(LAST_SOLVER_ERROR, "value", None)
    if isinstance(value, dict) and value.get("reason"):
        return value
    return {"reason": default_reason, "detail": default_reason}


def has_solver_error() -> bool:
    value = getattr(LAST_SOLVER_ERROR, "value", None)
    return isinstance(value, dict) and bool(value.get("reason"))


def should_reload_non_9_before_ai(non_9_reloads: int) -> bool:
    return FAST_RELOAD_NON_9 and non_9_reloads < max(0, NON_9_RELOADS_BEFORE_AI)


class TokenState:
    def __init__(self):
        self.token = None
        self.lock = threading.Lock()

    def set(self, token: str):
        with self.lock:
            self.token = token

    def get(self) -> str | None:
        with self.lock:
            return self.token


class CdpClient:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=10)
        self.next_id = 1

    def call(self, method: str, params: dict | None = None) -> dict:
        msg_id = self.next_id
        self.next_id += 1
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expression: str, await_promise: bool = False):
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
        )
        value = result.get("result", {})
        if "value" in value:
            return value["value"]
        return value

    def close(self):
        self.ws.close()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_browser(explicit: str | None = None) -> str:
    candidates = []
    if explicit:
        candidates.append(explicit)
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("PROGRAMFILES", r"C:\\Program Files")
    pfx86 = os.environ.get("PROGRAMFILES(X86)", r"C:\\Program Files (x86)")
    candidates += [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        rf"{pf}\\Google\\Chrome\\Application\\chrome.exe",
        rf"{pfx86}\\Google\\Chrome\\Application\\chrome.exe",
        rf"{local}\\Google\\Chrome\\Application\\chrome.exe",
        rf"{pf}\\Microsoft\\Edge\\Application\\msedge.exe",
        rf"{pfx86}\\Microsoft\\Edge\\Application\\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise FileNotFoundError("Nao achei Chrome/Edge. Passe --browser.")


def list_pages(port: int) -> list[dict]:
    return requests.get(f"http://127.0.0.1:{port}/json/list", timeout=1).json()


def is_solver_page_url(url: str) -> bool:
    value = url or ""
    return "127.0.0.1" in value or "localhost" in value or SOLVER_PAGE_HOST in value


def open_solver_browser(browser: str, url: str) -> tuple[int, Path, subprocess.Popen]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    tid = threading.get_ident()
    profile = SOLVER_PROFILES / f"solver-{stamp}"
    profile = SOLVER_PROFILES / f"solver-{stamp}-{tid}"
    profile.mkdir(parents=True, exist_ok=True)
    port = free_port()
    headless = os.environ.get("SOLVER_HEADLESS", "1").strip().lower() not in {"0", "false", "no", "off"}
    args = [
            browser,
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--host-resolver-rules=MAP {SOLVER_PAGE_HOST} 127.0.0.1",
            f"--unsafely-treat-insecure-origin-as-secure={url.rstrip('/')}",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1365,900",
            "--no-first-run",
            "--disable-default-apps",
            "--disable-background-mode",
            url,
    ]
    if PROXY_HOSTNAME and PROXY_LISTENER:
        args.insert(-1, f"--proxy-server=http://{PROXY_LISTENER}")
        args.insert(-1, f"--proxy-bypass-list=<-loopback>;localhost;127.0.0.1;{SOLVER_PAGE_HOST}")
    if headless:
        args[4:4] = ["--headless=new", "--disable-gpu"]
    else:
        args.insert(-1, "--new-window")
    proc = subprocess.Popen(args)
    return port, profile, proc


def start_solver_page(sitekey: str) -> tuple[ThreadingHTTPServer, int, TokenState]:
    token_state = TokenState()
    port = free_port()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/token":
                token = parse_qs(parsed.query).get("t", [""])[0]
                if token:
                    token_state.set(token)
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            escaped_sitekey = html.escape(sitekey, quote=True)
            page = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Desafio hCaptcha</title>
  <script src="https://js.hcaptcha.com/1/api.js?hl=pt" async defer></script>
</head>
<body>
  <div class="h-captcha" data-sitekey="{escaped_sitekey}" data-callback="captchaOk" data-error-callback="captchaErro" data-expired-callback="captchaExpirou"></div>
  <script>
    function captchaOk(token) {{
      fetch('/token?t=' + encodeURIComponent(token)).catch(() => {{}});
    }}
    function captchaErro() {{}}
    function captchaExpirou() {{}}
  </script>
</body>
</html>"""
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port, token_state


def close_solver_browser(port: int, proc: subprocess.Popen | None) -> None:
    try:
        version = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1).json()
        ws_url = version.get("webSocketDebuggerUrl")
        if ws_url:
            client = CdpClient(ws_url)
            try:
                client.call("Browser.close")
            finally:
                client.close()
    except Exception:
        pass

    if proc and proc.poll() is None:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def wait_solver_page(port: int) -> dict:
    end = time.time() + 30
    while time.time() < end:
        try:
            pages = list_pages(port)
            for page in pages:
                if page.get("type") == "page" and is_solver_page_url(page.get("url", "")):
                    return page
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("Nao achei pagina do navegador hCaptcha.")


def hcaptcha_targets(port: int) -> list[dict]:
    try:
        return [
            page
            for page in list_pages(port)
            if "hcaptcha.html" in page.get("url", "") and page.get("webSocketDebuggerUrl")
        ]
    except Exception:
        return []


def click_hcaptcha_checkbox(port: int, timeout: int = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            pages = list_pages(port)
            parent = next(
                (
                    page
                    for page in pages
                    if page.get("type") == "page"
                    and is_solver_page_url(page.get("url", ""))
                    and page.get("webSocketDebuggerUrl")
                ),
                None,
            )
            if parent:
                client = CdpClient(parent["webSocketDebuggerUrl"])
                try:
                    rect = client.eval(
                        """
(() => {
  const frame = [...document.querySelectorAll('iframe')]
    .find((f) => (f.src || '').includes('frame=checkbox'));
  if (!frame) return null;
  const r = frame.getBoundingClientRect();
  return { x: r.left + 30, y: r.top + 38, w: r.width, h: r.height };
})()
"""
                    )
                    if rect:
                        client.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": rect["x"], "y": rect["y"]})
                        client.call(
                            "Input.dispatchMouseEvent",
                            {"type": "mousePressed", "x": rect["x"], "y": rect["y"], "button": "left", "clickCount": 1},
                        )
                        client.call(
                            "Input.dispatchMouseEvent",
                            {"type": "mouseReleased", "x": rect["x"], "y": rect["y"], "button": "left", "clickCount": 1},
                        )
                        return True
                finally:
                    client.close()
        except Exception:
            pass
        time.sleep(0.5)

    for page in hcaptcha_targets(port):
        url = page.get("url", "")
        if "frame=checkbox" not in url:
            continue
        try:
            client = CdpClient(page["webSocketDebuggerUrl"])
            try:
                rect = client.eval(
                    """
(() => {
  const el = document.querySelector('#checkbox') || document.querySelector('[role="checkbox"]');
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height };
})()
"""
                )
                if rect:
                    client.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": rect["x"], "y": rect["y"]})
                    client.call(
                        "Input.dispatchMouseEvent",
                        {"type": "mousePressed", "x": rect["x"], "y": rect["y"], "button": "left", "clickCount": 1},
                    )
                    client.call(
                        "Input.dispatchMouseEvent",
                        {"type": "mouseReleased", "x": rect["x"], "y": rect["y"], "button": "left", "clickCount": 1},
                    )
                    return True
                return bool(client.eval("document.querySelector('#checkbox')?.click(); true"))
            finally:
                client.close()
            return True
        except Exception:
            pass
    return False


def challenge_grid_visible(port: int) -> bool:
    try:
        pages = list_pages(port)
        parent = next(
            (
                page
                for page in pages
                if page.get("type") == "page" and is_solver_page_url(page.get("url", "")) and page.get("webSocketDebuggerUrl")
            ),
            None,
        )
        if not parent:
            return False
        client = CdpClient(parent["webSocketDebuggerUrl"])
        try:
            return bool(
                client.eval(
                    """
(() => {
  const frame = [...document.querySelectorAll('iframe')]
    .find((f) => (f.src || '').includes('frame=challenge'));
  if (!frame) return false;
  const r = frame.getBoundingClientRect();
  const style = getComputedStyle(frame);
  return style.visibility !== 'hidden' && style.display !== 'none' && r.top > -100 && r.width > 250 && r.height > 250;
})()
"""
                )
            )
        finally:
            client.close()
    except Exception:
        return False
    return False


def challenge_page(port: int) -> dict | None:
    for page in hcaptcha_targets(port):
        url = page.get("url", "")
        title = page.get("title", "")
        if "frame=challenge" in url or "Desafio hCaptcha" in title:
            return page
    return None


def _challenge_dom_snapshot(client: CdpClient) -> dict | None:
    return client.eval(
        """
(() => {
  const promptEl =
    document.querySelector('#prompt-question span') ||
    document.querySelector('#prompt-question') ||
    document.querySelector('h2, h3, .challenge-text');
  const prompt = promptEl ? promptEl.innerText.trim().replace(/\\s+/g, ' ') : '';
  const checkmark = [...document.querySelectorAll('img[alt]')]
    .some((img) => (img.getAttribute('alt') || '').toLowerCase().includes('marca de verificação'));
  const imageCanvasEl = [...document.querySelectorAll('canvas[role="img"]')]
    .find((canvas) => {
      const label = canvas.getAttribute('aria-label') || '';
      return label.includes('Desafio de CAPTCHA baseado em imagem') &&
        canvas.width >= 900 &&
        canvas.height >= 900;
    });
  let imageCanvasClip = null;
  if (imageCanvasEl) {
    const r = imageCanvasEl.getBoundingClientRect();
    imageCanvasClip = {x: Math.max(0, r.left), y: Math.max(0, r.top), width: Math.max(1, r.width), height: Math.max(1, r.height), scale: 1};
  }
  const tasks = [...document.querySelectorAll('.task[role="button"], .task')]
    .map((el, i) => {
      const r = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      const img = el.querySelector('img');
      const child = el.querySelector('[style*="background-image"], .image, .task-image');
      const childStyle = child ? getComputedStyle(child) : null;
      const bg = style.backgroundImage && style.backgroundImage !== 'none'
        ? style.backgroundImage
        : (childStyle ? childStyle.backgroundImage : '');
      const visible =
        style.visibility !== 'hidden' &&
        style.display !== 'none' &&
        Number(style.opacity || '1') > 0.95 &&
        r.width >= 70 &&
        r.height >= 70 &&
        r.left > -20 &&
        r.top > -20;
      const loaded =
        (img && img.complete && img.naturalWidth > 20 && img.naturalHeight > 20) ||
        (bg && bg !== 'none') ||
        !!el.querySelector('canvas, svg');
      return {
        i: i + 1,
        left: Math.round(r.left),
        top: Math.round(r.top),
        width: Math.round(r.width),
        height: Math.round(r.height),
        visible,
        loaded,
        bg,
        img: img ? img.currentSrc || img.src || '' : '',
        text: el.innerText ? el.innerText.trim().replace(/\\s+/g, ' ') : ''
      };
    });
  const visibleTasks = tasks.filter((t) => t.visible);
  let grid = null;
  if (visibleTasks.length) {
    const left = Math.min(...visibleTasks.map((t) => t.left));
    const top = Math.min(...visibleTasks.map((t) => t.top));
    const right = Math.max(...visibleTasks.map((t) => t.left + t.width));
    const bottom = Math.max(...visibleTasks.map((t) => t.top + t.height));
    grid = {x: left, y: top, width: right - left, height: bottom - top, scale: 1};
  }
  const allLoaded = tasks.length === 9 && tasks.every((t) => t.visible && t.loaded);
  const signature = JSON.stringify(tasks.map((t) => ({
    i: t.i, left: t.left, top: t.top, width: t.width, height: t.height, bg: t.bg, img: t.img
  })));
  return {prompt, tasks, grid, allLoaded, signature, checkmark, imageCanvas: !!imageCanvasEl, imageCanvasClip};
})()
"""
    )


def wait_for_stable_9_tile_challenge(port: int, timeout: int = 8) -> tuple[dict | None, dict | None]:
    end = time.time() + timeout
    stable_signature = None
    stable_count = 0
    non_9_count = 0
    last_state = None
    while time.time() < end:
        page = challenge_page(port)
        if not page:
            time.sleep(0.25)
            continue
        client = CdpClient(page["webSocketDebuggerUrl"])
        try:
            state = _challenge_dom_snapshot(client)
        finally:
            client.close()
        last_state = state
        if state and (state.get("checkmark") or state.get("imageCanvas")):
            if state.get("imageCanvas") and NON_9_SETTLE_SECONDS > 0:
                print(f"[Debug] Canvas de imagem detectado; aguardando {NON_9_SETTLE_SECONDS:.1f}s para estabilizar.")
                time.sleep(NON_9_SETTLE_SECONDS)
                try:
                    client = CdpClient(page["webSocketDebuggerUrl"])
                    try:
                        state = _challenge_dom_snapshot(client) or state
                    finally:
                        client.close()
                except Exception:
                    pass
            marker = "marca de verificacao" if state.get("checkmark") else "canvas de imagem"
            print(f"[Debug] Desafio nao e grade de 9 tiles: {marker}.")
            return None, state
        task_count = len(state.get("tasks") or []) if state else 0
        if task_count and task_count != 9:
            non_9_count += 1
            if non_9_count >= 2:
                print(f"[Debug] Desafio nao e grade de 9 tiles: tasks={task_count}.")
                return None, state
        else:
            non_9_count = 0
        if state and state.get("allLoaded") and state.get("prompt") and state.get("grid"):
            signature = state.get("signature")
            if signature == stable_signature:
                stable_count += 1
            else:
                stable_signature = signature
                stable_count = 1
            if stable_count >= 3:
                time.sleep(0.6)
                return page, state
        else:
            stable_signature = None
            stable_count = 0
        time.sleep(0.4)
    if last_state:
        task_count = len(last_state.get("tasks") or [])
        loaded = sum(1 for task in last_state.get("tasks") or [] if task.get("loaded") and task.get("visible"))
        print(f"[Debug] Grade nao estabilizou: tasks={task_count}, carregados={loaded}, pergunta='{last_state.get('prompt') or ''}'")
    return None, last_state


def ensure_challenge_open(port: int, max_clicks: int = 8) -> bool:
    for _ in range(max_clicks):
        if challenge_grid_visible(port):
            return True
        if not click_hcaptcha_checkbox(port, timeout=3):
            continue
        for _ in range(10):
            if challenge_grid_visible(port):
                return True
            time.sleep(0.2)
    return challenge_grid_visible(port)


def screenshot_solver(port: int, note_index: int, attempt: int, label: str = "grade") -> Path | None:
    try:
        pages = list_pages(port)
        target = None
        clip = None
        for page in pages:
            if page.get("type") == "page" and is_solver_page_url(page.get("url", "")):
                target = page
                try:
                    client = CdpClient(page["webSocketDebuggerUrl"])
                    try:
                        rect = client.eval(
                            """
(() => {
  const frame = [...document.querySelectorAll('iframe')]
    .find((f) => (f.src || '').includes('frame=challenge'));
  if (!frame) return null;
  const r = frame.getBoundingClientRect();
  const style = getComputedStyle(frame);
  if (style.visibility === 'hidden' || style.display === 'none' || r.top < -100) return null;
  return { x: Math.max(0, r.left), y: Math.max(0, r.top), width: Math.max(1, r.width), height: Math.max(1, r.height), scale: 1 };
})()
"""
                        )
                        if rect:
                            clip = rect
                    finally:
                        client.close()
                except Exception:
                    clip = None
                break
        for page in pages:
            if target:
                break
            url = page.get("url", "")
            title = page.get("title", "")
            if "hcaptcha.html" in url and ("frame=challenge" in url or "Desafio hCaptcha" in title):
                target = page
                break
        if not target:
            for page in pages:
                if page.get("type") == "page" and is_solver_page_url(page.get("url", "")):
                    target = page
                    break
        if not target:
            for page in pages:
                if page.get("type") == "page" and page.get("webSocketDebuggerUrl"):
                    target = page
                    break
        if not target:
            return None
        client = CdpClient(target["webSocketDebuggerUrl"])
        try:
            params = {"format": "png", "captureBeyondViewport": True}
            if clip:
                params["clip"] = clip
            png = client.call("Page.captureScreenshot", params)["data"]
        finally:
            client.close()
        CAPTCHA_DIR.mkdir(parents=True, exist_ok=True)
        path = CAPTCHA_DIR / f"nota-{note_index + 1:02d}-tentativa-{attempt:02d}-{label}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        path.write_bytes(base64.b64decode(png))
        return path
    except Exception:
        return None


def safe_name(value: str, max_len: int = 80) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in value.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return (cleaned or "desafio")[:max_len]


def top_level_solver_page(port: int) -> dict | None:
    try:
        return next(
            (
                page
                for page in list_pages(port)
                if page.get("type") == "page"
                and is_solver_page_url(page.get("url", ""))
                and page.get("webSocketDebuggerUrl")
            ),
            None,
        )
    except Exception:
        return None


def challenge_frame_clip(port: int) -> dict | None:
    page = top_level_solver_page(port)
    if not page:
        return None
    try:
        client = CdpClient(page["webSocketDebuggerUrl"])
        try:
            return client.eval(
                """
(() => {
  const frame = [...document.querySelectorAll('iframe')]
    .find((f) => (f.src || '').includes('frame=challenge'));
  if (!frame) return null;
  const r = frame.getBoundingClientRect();
  return {x: Math.max(0, r.left), y: Math.max(0, r.top), width: Math.max(1, r.width), height: Math.max(1, r.height), scale: 1};
})()
"""
            )
        finally:
            client.close()
    except Exception:
        return None


def capture_challenge_png(port: int, path: Path, clip: dict | None = None) -> bool:
    try:
        page = top_level_solver_page(port)
        if not page:
            return False
        frame_clip = challenge_frame_clip(port)
        final_clip = None
        if clip:
            final_clip = {
                "x": (float(frame_clip["x"]) if frame_clip else 0) + max(0, float(clip["x"])),
                "y": (float(frame_clip["y"]) if frame_clip else 0) + max(0, float(clip["y"])),
                "width": max(1, float(clip["width"])),
                "height": max(1, float(clip["height"])),
                "scale": 1,
            }
        elif frame_clip:
            final_clip = frame_clip

        client = CdpClient(page["webSocketDebuggerUrl"])
        try:
            params = {"format": "png", "captureBeyondViewport": True}
            if final_clip:
                params["clip"] = final_clip
            png = client.call("Page.captureScreenshot", params)["data"]
        finally:
            client.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(png))
        return True
    except Exception as e:
        print(f"[Debug] Falha ao capturar {path.name}: {e}")
        return False


def write_data_url_png(data_url: str, path: Path) -> bool:
    try:
        if not data_url or "," not in data_url:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
        return True
    except Exception as e:
        print(f"[Debug] Falha ao salvar {path.name} via canvas: {e}")
        return False


def crop_png_top(source: Path, dest: Path, top_css_px: float, total_css_height: float) -> bool:
    try:
        with Image.open(source) as img:
            top_px = int(round((float(top_css_px) / max(1.0, float(total_css_height))) * img.height))
            top_px = min(max(0, top_px), img.height - 1)
            cropped = img.crop((0, top_px, img.width, img.height))
            dest.parent.mkdir(parents=True, exist_ok=True)
            cropped.save(dest)
        return True
    except Exception as e:
        print(f"[Debug] Falha ao cortar {source.name}: {e}")
        return False


def split_png_grid(source: Path, out_dir: Path, cols: int = 5, rows: int = 3) -> bool:
    try:
        with Image.open(source) as img:
            out_dir.mkdir(parents=True, exist_ok=True)
            part_w = img.width / cols
            part_h = img.height / rows
            for row in range(rows):
                for col in range(cols):
                    part_num = row * cols + col + 1
                    box = (
                        int(round(col * part_w)),
                        int(round(row * part_h)),
                        int(round((col + 1) * part_w)),
                        int(round((row + 1) * part_h)),
                    )
                    img.crop(box).save(out_dir / f"parte-{part_num:02d}-l{row + 1}c{col + 1}.png")
        return True
    except Exception as e:
        print(f"[Debug] Falha ao dividir {source.name}: {e}")
        return False


def png_white_ratio(path: Path) -> float:
    try:
        with Image.open(path) as img:
            small = img.convert("RGB").resize((80, 80))
            pixels = list(small.getdata())
            white = sum(1 for r, g, b in pixels if r > 245 and g > 245 and b > 245)
            return white / max(1, len(pixels))
    except Exception:
        return 0.0


def png_seems_blank(path: Path, threshold: float = 0.92) -> bool:
    return png_white_ratio(path) >= threshold


def draw_non_9_grid_overlay(port: int, top_cut: int = 150) -> bool:
    page = challenge_page(port)
    if not page:
        return False
    try:
        client = CdpClient(page["webSocketDebuggerUrl"])
        try:
            return bool(client.eval(
                """
(() => {
  const old = document.getElementById('codex-non9-grid-overlay');
  if (old) old.remove();
  const canvas = [...document.querySelectorAll('canvas[role="img"]')]
    .find((el) => {
      const label = el.getAttribute('aria-label') || '';
      return label.includes('Desafio de CAPTCHA baseado em imagem') &&
        el.width >= 900 &&
        el.height >= 900;
    });
  if (!canvas) return false;
  const parent = canvas.parentElement || document.body;
  const cr = canvas.getBoundingClientRect();
  const pr = parent.getBoundingClientRect();
  const cut = Math.min(Math.max(0, Number(arguments[0] || 150)), cr.height - 20);
  const overlay = document.createElement('div');
  overlay.id = 'codex-non9-grid-overlay';
  overlay.style.position = 'absolute';
  overlay.style.left = `${cr.left - pr.left}px`;
  overlay.style.top = `${cr.top - pr.top + cut}px`;
  overlay.style.width = `${cr.width}px`;
  overlay.style.height = `${cr.height - cut}px`;
  overlay.style.pointerEvents = 'none';
  overlay.style.zIndex = '2147483647';
  overlay.style.boxSizing = 'border-box';
  overlay.style.background = [
    'linear-gradient(to right, transparent calc(20% - 1px), #ffea00 calc(20% - 1px), #ffea00 calc(20% + 1px), transparent calc(20% + 1px))',
    'linear-gradient(to right, transparent calc(40% - 1px), #ffea00 calc(40% - 1px), #ffea00 calc(40% + 1px), transparent calc(40% + 1px))',
    'linear-gradient(to right, transparent calc(60% - 1px), #ffea00 calc(60% - 1px), #ffea00 calc(60% + 1px), transparent calc(60% + 1px))',
    'linear-gradient(to right, transparent calc(80% - 1px), #ffea00 calc(80% - 1px), #ffea00 calc(80% + 1px), transparent calc(80% + 1px))',
    'linear-gradient(to bottom, transparent calc(33.333% - 1px), #ffea00 calc(33.333% - 1px), #ffea00 calc(33.333% + 1px), transparent calc(33.333% + 1px))',
    'linear-gradient(to bottom, transparent calc(66.666% - 1px), #ffea00 calc(66.666% - 1px), #ffea00 calc(66.666% + 1px), transparent calc(66.666% + 1px))'
  ].join(',');
  for (let i = 0; i < 15; i++) {
    const label = document.createElement('div');
    label.textContent = String(i + 1);
    label.style.position = 'absolute';
    label.style.left = `${(i % 5) * 20 + 2}%`;
    label.style.top = `${Math.floor(i / 5) * 33.333 + 2}%`;
    label.style.padding = '1px 5px';
    label.style.borderRadius = '3px';
    label.style.background = 'rgba(0, 0, 0, 0.7)';
    label.style.color = '#ffea00';
    label.style.font = 'bold 18px Arial';
    overlay.appendChild(label);
  }
  parent.appendChild(overlay);
  return true;
})()
""".replace("arguments[0] || 150", str(top_cut))
            ))
        finally:
            client.close()
    except Exception:
        return False


def remove_non_9_grid_overlay(port: int) -> None:
    page = challenge_page(port)
    if not page:
        return
    try:
        client = CdpClient(page["webSocketDebuggerUrl"])
        try:
            client.eval("document.getElementById('codex-non9-grid-overlay')?.remove(); true")
        finally:
            client.close()
    except Exception:
        pass


def capture_non_9_canvas_artifacts(port: int, folder: Path) -> bool:
    page = challenge_page(port)
    if not page:
        return False
    try:
        client = CdpClient(page["webSocketDebuggerUrl"])
        try:
            data = client.eval(
                """
(() => {
  const src = [...document.querySelectorAll('canvas[role="img"]')]
    .find((canvas) => {
      const label = canvas.getAttribute('aria-label') || '';
      return label.includes('Desafio de CAPTCHA baseado em imagem') &&
        canvas.width >= 900 &&
        canvas.height >= 900;
    });
  if (!src) return null;
  const cols = 5;
  const rows = 3;
  const r = src.getBoundingClientRect();
  return {
    width: src.width,
    height: src.height,
    clip: {x: Math.max(0, r.left), y: Math.max(0, r.top), width: Math.max(1, r.width), height: Math.max(1, r.height), scale: 1},
    cols,
    rows
  };
})()
"""
            )
        finally:
            client.close()
    except Exception as e:
        print(f"[Debug] Falha ao extrair canvas nao-9: {e}")
        return False

    if not data:
        return False
    clip = data.get("clip")
    if not clip:
        return False
    top_cut = min(150.0, max(0.0, float(clip["height"]) - 20.0))
    full_path = folder / "desafio-completo.png"
    full_grid_path = folder / "desafio-completo-grade-15.png"
    data["top_cut_px"] = top_cut
    capture_challenge_png(port, full_path, clip)
    crop_png_top(full_path, folder / "desafio.png", top_cut, float(clip["height"]))
    draw_non_9_grid_overlay(port, int(top_cut))
    try:
        capture_challenge_png(port, full_grid_path, clip)
        crop_png_top(full_grid_path, folder / "desafio-grade-15.png", top_cut, float(clip["height"]))
    finally:
        remove_non_9_grid_overlay(port)
    grade_ratio = png_white_ratio(folder / "desafio-grade-15.png")
    if grade_ratio >= 0.92:
        print(f"[Debug] desafio-grade-15.png branco demais ({grade_ratio:.1%}); vou tentar capturar de novo.")
        (folder / "captura-invalida.txt").write_text(
            f"desafio-grade-15.png branco demais: {grade_ratio:.4f}\n",
            encoding="utf-8",
        )
        return False
    if png_seems_blank(folder / "desafio.png"):
        print("[Debug] desafio.png veio branco; usando imagem com grade como base visual.")
        try:
            Image.open(folder / "desafio-grade-15.png").save(folder / "desafio.png")
        except Exception:
            pass
    parts_dir = folder / "particoes"
    cols = int(data.get("cols") or 5)
    rows = int(data.get("rows") or 3)
    split_png_grid(folder / "desafio.png", parts_dir, cols, rows)
    (folder / "canvas-info.json").write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return True


def create_challenge_debug_folder(request_id: str, attempt: int, prompt_text: str, root: Path = CHALLENGES_9_DIR) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder = root / f"{stamp}-{safe_name(request_id)}-tentativa-{attempt:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pergunta.txt").write_text(prompt_text + "\n", encoding="utf-8")
    return folder


def save_9_tile_challenge_debug(port: int, state: dict, request_id: str, attempt: int) -> Path:
    prompt_text = str(state.get("prompt") or "")
    folder = create_challenge_debug_folder(request_id, attempt, prompt_text)
    (folder / "estado-dom.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    time.sleep(1)
    capture_challenge_png(port, folder / "desafio.png")
    if state.get("grid"):
        capture_challenge_png(port, folder / "grade.png", state["grid"])

    tiles_dir = folder / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    for task in state.get("tasks") or []:
        clip = {
            "x": task["left"],
            "y": task["top"],
            "width": task["width"],
            "height": task["height"],
            "scale": 1,
        }
        capture_challenge_png(port, tiles_dir / f"tile-{int(task['i']):02d}.png", clip)

    print(f"[Debug] Desafio salvo em: {folder}")
    return folder


def save_non_9_challenge_debug(port: int, state: dict, request_id: str, attempt: int) -> Path | None:
    prompt_text = str(state.get("prompt") or "")
    folder = create_challenge_debug_folder(request_id, attempt, prompt_text, CHALLENGES_NON_9_DIR)
    (folder / "estado-dom.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    time.sleep(1)
    if not capture_non_9_canvas_artifacts(port, folder):
        (folder / "canvas-erro.txt").write_text(
            "Nao consegui capturar o canvas selecionavel de forma valida; vou tentar novamente.\n",
            encoding="utf-8",
        )
        print(f"[Debug] Desafio nao-9 invalido salvo em: {folder}")
        return None

    print(f"[Debug] Desafio nao-9 salvo em: {folder}")
    return folder


def click_hcaptcha_refresh(port: int) -> bool:
    for page in hcaptcha_targets(port):
        url = page.get("url", "")
        title = page.get("title", "")
        if "frame=challenge" not in url and "Desafio hCaptcha" not in title:
            continue
        try:
            client = CdpClient(page["webSocketDebuggerUrl"])
            try:
                point = client.eval(
                    """
(() => {
  const el =
    document.querySelector('.refresh.button[aria-label="Atualizar teste de segurança."]') ||
    document.querySelector('.refresh.button') ||
    document.querySelector('[aria-label*="Atualizar"]') ||
    document.querySelector('[title*="Atualizar"]');
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
})()
"""
                )
                if not point:
                    return False
                client.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": point["x"], "y": point["y"]})
                client.call(
                    "Input.dispatchMouseEvent",
                    {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1},
                )
                client.call(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1},
                )
                return True
            finally:
                client.close()
        except Exception:
            pass
    return False


def refresh_hcaptcha_and_wait(port: int, wait_seconds: float = 2.5) -> bool:
    refreshed = click_hcaptcha_refresh(port)
    if refreshed:
        time.sleep(wait_seconds)
    return refreshed


def reload_non_9_challenge(port: int, current: int | None = None, limit: int | None = None) -> None:
    if current is not None and limit is not None:
        print(f"[Auto] Nao achei 9 tiles; reload {current}/{limit} pelo botao refresh.")
    else:
        print("[Auto] Recarregando desafio nao-9.")
    if refresh_hcaptcha_and_wait(port):
        return
    try:
        page = wait_solver_page(port)
        client = CdpClient(page["webSocketDebuggerUrl"])
        client.call("Page.reload", {"ignoreCache": True})
        client.close()
    except Exception:
        pass
    time.sleep(2.5)


def click_hcaptcha_submit(port: int) -> bool:
    for page in hcaptcha_targets(port):
        url = page.get("url", "")
        title = page.get("title", "")
        if "frame=challenge" not in url and "Desafio hCaptcha" not in title:
            continue
        try:
            client = CdpClient(page["webSocketDebuggerUrl"])
            try:
                point = client.eval(
                    """
(() => {
  const el =
    document.querySelector('.button-submit.button[aria-label="Verificar respostas"]') ||
    document.querySelector('.button-submit.button') ||
    document.querySelector('[aria-label="Verificar respostas"]') ||
    document.querySelector('[title="Verificar respostas"]');
  if (!el || el.getAttribute('aria-disabled') === 'true') return null;
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
})()
"""
                )
                if not point:
                    return False
                client.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": point["x"], "y": point["y"]})
                client.call(
                    "Input.dispatchMouseEvent",
                    {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1},
                )
                client.call(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1},
                )
                return True
            finally:
                client.close()
        except Exception:
            pass
    return False


def captcha_retry_error_visible(port: int) -> bool:
    for page in hcaptcha_targets(port):
        if "frame=challenge" not in page.get("url", "") and "Desafio hCaptcha" not in page.get("title", ""):
            continue
        try:
            client = CdpClient(page["webSocketDebuggerUrl"])
            try:
                return bool(client.eval(
                    """
(() => {
  const el = document.querySelector('.error-text');
  if (!el) return false;
  const r = el.getBoundingClientRect();
  const style = getComputedStyle(el);
  const visible = style.display !== 'none' &&
    style.visibility !== 'hidden' &&
    Number(style.opacity || '1') > 0.5 &&
    r.width > 1 &&
    r.height > 1;
  const text = el ? el.innerText.trim().toLowerCase() : '';
  return visible && text.includes('tentar novamente');
})()
"""
                ))
            finally:
                client.close()
        except Exception:
            pass
    return False


def captcha_checkmark_visible(port: int) -> bool:
    for page in hcaptcha_targets(port):
        if "frame=challenge" not in page.get("url", "") and "Desafio hCaptcha" not in page.get("title", ""):
            continue
        try:
            client = CdpClient(page["webSocketDebuggerUrl"])
            try:
                return bool(client.eval(
                    """
(() => [...document.querySelectorAll('img[alt]')]
  .some((img) => (img.getAttribute('alt') || '').toLowerCase().includes('marca de verificação')))()
"""
                ))
            finally:
                client.close()
        except Exception:
            pass
    return False


def wait_token_or_retry_after_submit(port: int, timeout: float = 15.0) -> str | None:
    print("[Auto] Aguardando token...")
    end = time.time() + timeout
    saw_checkmark = False
    while time.time() < end:
        token = extract_token_from_page(port)
        if token:
            print(f"[Auto] Token obtido com sucesso! ({len(token)} chars)")
            return token
        if captcha_checkmark_visible(port):
            if not saw_checkmark:
                print("[Auto] Marca de verificacao apareceu; aguardando token/callback.")
            saw_checkmark = True
            end = max(end, time.time() + 8)
        if captcha_retry_error_visible(port):
            print("[Auto] hCaptcha mostrou 'tentar novamente'; aguardando troca/token.")
            time.sleep(1.2)
        time.sleep(0.4)
    return None


def captcha_task_count(port: int) -> int:
    """Conta quantas tarefas (imagens) aparecem no desafio."""
    for page in hcaptcha_targets(port):
        if "frame=challenge" not in page.get("url", "") and "Desafio hCaptcha" not in page.get("title", ""):
            continue
        try:
            client = CdpClient(page["webSocketDebuggerUrl"])
            try:
                # Correção aqui:
                count = client.eval(
                    r"""document.querySelectorAll('.task[role="button"], .task').length"""
                )
                return int(count or 0)
            finally:
                client.close()
        except Exception:
            pass
    return 0


def captcha_prompt_text(port: int) -> str:
    """
    Extrai automaticamente o texto da pergunta do desafio hCaptcha.
    """
    for page in hcaptcha_targets(port):
        if "frame=challenge" not in page.get("url", "") and "Desafio hCaptcha" not in page.get("title", ""):
            continue
        try:
            client = CdpClient(page["webSocketDebuggerUrl"])
            try:
                text = client.eval(
                    """
(() => {
  let el = document.querySelector('#prompt-question span');
  
  if (!el) el = document.querySelector('#prompt-question');
  
  if (!el) {
      const headers = document.querySelectorAll('h2, h3, .challenge-text');
      for (let h of headers) {
          if (h.innerText.length > 5 && h.innerText.length < 100) {
              el = h;
              break;
          }
      }
  }

  if (el) {
      return el.innerText.trim().replace(/\\s+/g, ' ');
  }
  return "";
})()
"""
                )
                return str(text or "").strip()
            finally:
                client.close()
        except Exception:
            pass
    return ""


def click_hcaptcha_tasks(port: int, indexes: list[int]) -> bool:
    for page in hcaptcha_targets(port):
        if "frame=challenge" not in page.get("url", "") and "Desafio hCaptcha" not in page.get("title", ""):
            continue
        try:
            client = CdpClient(page["webSocketDebuggerUrl"])
            try:
                tasks = client.eval(
                    """
(() => [...document.querySelectorAll('.task[role="button"], .task')].map((el, i) => {
  const r = el.getBoundingClientRect();
  return { i: i + 1, x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height };
}))()
"""
                )
                if not tasks:
                    return False
                by_index = {int(task["i"]): task for task in tasks}
                for index in indexes:
                    task = by_index.get(index)
                    if not task:
                        continue
                    client.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": task["x"], "y": task["y"]})
                    client.call(
                        "Input.dispatchMouseEvent",
                        {"type": "mousePressed", "x": task["x"], "y": task["y"], "button": "left", "clickCount": 1},
                    )
                    client.call(
                        "Input.dispatchMouseEvent",
                        {"type": "mouseReleased", "x": task["x"], "y": task["y"], "button": "left", "clickCount": 1},
                    )
                    time.sleep(0.1)
                return True
            finally:
                client.close()
        except Exception:
            pass
    return False


def click_non_9_choice(port: int, escolha: dict) -> bool:
    if not escolha:
        return False
    try:
        x_percent = float(escolha.get("x_percent_na_imagem"))
        y_percent = float(escolha.get("y_percent_na_imagem"))
    except (TypeError, ValueError):
        return False
    x_percent = min(100.0, max(0.0, x_percent))
    y_percent = min(100.0, max(0.0, y_percent))
    page = challenge_page(port)
    if not page:
        return False
    try:
        client = CdpClient(page["webSocketDebuggerUrl"])
        try:
            point = client.eval(
                f"""
(() => {{
  const canvas = [...document.querySelectorAll('canvas[role="img"]')]
    .find((el) => {{
      const label = el.getAttribute('aria-label') || '';
      return label.includes('Desafio de CAPTCHA baseado em imagem') &&
        el.width >= 900 &&
        el.height >= 900;
    }});
  if (!canvas) return null;
  const r = canvas.getBoundingClientRect();
  const topCut = Math.min(150, Math.max(0, r.height - 20));
  const selectableHeight = Math.max(1, r.height - topCut);
  return {{
    x: r.left + r.width * ({x_percent} / 100),
    y: r.top + topCut + selectableHeight * ({y_percent} / 100)
  }};
}})()
"""
            )
            if not point:
                return False
            client.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": point["x"], "y": point["y"]})
            client.call(
                "Input.dispatchMouseEvent",
                {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1},
            )
            client.call(
                "Input.dispatchMouseEvent",
                {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1},
            )
            return True
        finally:
            client.close()
    except Exception:
        return False


def parse_task_numbers(raw: str) -> list[int]:
    nums = []
    for piece in raw.replace(";", ",").replace(" ", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            n = int(piece)
        except ValueError:
            continue
        if 1 <= n <= 9:
            nums.append(n)
    return sorted(set(nums))


def should_skip_prompt(prompt_text: str) -> bool:
    return "mudou" in (prompt_text or "").casefold()


def wait_for_canvas_image(port: int, note_index: int, attempt: int, timeout: int = 30) -> Path | None:
    end = time.time() + timeout
    while time.time() < end:
        if challenge_grid_visible(port):
            time.sleep(1)
            shot = screenshot_solver(port, note_index, attempt, "desafio")
            if shot:
                return shot
        time.sleep(1)
    return None


def image_as_data_url(image_path: Path) -> str:
    with open(image_path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
    return f"data:image/png;base64,{encoded_string}"


def solve_with_cohere(image_path: Path, captcha_question: str, challenge_dir: Path | None = None) -> list[int] | None:
    """
    Envia a imagem para o Cohere e retorna a lista de índices [1-9] ou None se falhar.
    """
    if not image_path or not image_path.exists():
        print("[Cohere] Imagem não encontrada.")
        return None
    if not COHERE_API_KEY:
        print("[Cohere] COHERE_API_KEY nao configurada no ambiente.")
        set_solver_error("cohere_key_missing", "COHERE_API_KEY nao configurada no Secret do Modal.")
        return None

    bot_response = ""
    try:
        prompt = f"""
Você vai classificar uma grade hCaptcha com 9 tiles.

Pergunta original: "{captcha_question}"

As imagens anexadas vêm nesta ordem:
1. primeiro, uma imagem geral do desafio inteiro;
2. depois, os recortes individuais tile 1, tile 2, ... tile 9.

Regras de decisão:
- Responda à pergunta original literalmente.
- Se a pergunta mencionar "imagem de exemplo", primeiro identifique o objeto/alvo mostrado nessa imagem de exemplo e só depois compare cada tile com esse alvo.
- Em desafios com "imagem de exemplo", nunca selecione quase todos os tiles por precaução. Só selecione os tiles que correspondem claramente ao exemplo.
- Analise cada tile individualmente usando o recorte do próprio tile como fonte principal.
- Use a imagem geral só para contexto e para confirmar a numeração.
- Se o alvo aparecer parcialmente, selecione apenas quando ainda for claramente identificável.
- Não selecione por associação vaga, reflexo, sombra, fundo distante ou objeto muito ambíguo.
- Em perguntas de categoria/conceito, explique o raciocínio por tile. Exemplos:
  - feito por humanos/construído por pessoas: veículos, edifícios, placas, postes, máquinas e objetos artificiais contam; árvores, céu, montanhas e animais não contam.
  - absorve líquido: esponja, papel, pano e toalha contam; garrafa, copo, hidrante ou água não contam só por conter/liberar líquido.
  - metal: selecione só quando o objeto metálico for visível e relevante.
- Selecionar os 9 tiles é raro. Só faça isso se cada tile cumprir claramente a pergunta.
- Se estiver em dúvida real sobre um tile, marque `contem_alvo` como false e use confiança baixa.

Resposta obrigatória:
Retorne **exclusivamente** um JSON válido com esta estrutura exata:
```json
{{
  "itens": [
    {{
      "numero": 1,
      "descricao": "descrição objetiva do tile 1",
      "contem_alvo": true,
      "confianca": 0.0,
      "argumento": "por que este tile deve ou não deve ser selecionado"
    }},
    ... (para todos os 9 tiles)
  ],
  "resposta_direta": "1, 3, 4, 7"
}}
```
- Inclua exatamente 9 objetos em `"itens"`, numerados de 1 a 9.
- No campo `"resposta_direta"` liste apenas os números dos tiles com `contem_alvo=true`, separados por vírgula, em ordem crescente.
- Se nenhum tile corresponder → `"resposta_direta": ""`
"""
        content = [{"type": "text", "text": prompt}]
        content.append({"type": "text", "text": "Imagem geral do desafio:"})
        content.append({"type": "image_url", "image_url": {"url": image_as_data_url(image_path)}})
        if challenge_dir:
            for tile_num in range(1, 10):
                tile_path = challenge_dir / "tiles" / f"tile-{tile_num:02d}.png"
                if tile_path.exists():
                    content.append({"type": "text", "text": f"Tile {tile_num}:"})
                    content.append({"type": "image_url", "image_url": {"url": image_as_data_url(tile_path)}})

        payload = {
            "model": COHERE_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": content
                }
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"}
        }
        
        headers = {
            "Authorization": f"bearer {COHERE_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        response = requests.post("https://api.cohere.com/v2/chat", headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        
        data = response.json()
        bot_response = data["message"]["content"][0]["text"]
        
        # Parsing mais robusto
        result_json = json.loads(bot_response.strip())
        resposta_direta = result_json.get("resposta_direta", "")
        
        indices = parse_task_numbers(resposta_direta)
        if "imagem de exemplo" in (captcha_question or "").casefold() and len(indices) >= 7:
            print("[Cohere] Resposta rejeitada: desafio com imagem de exemplo selecionou quase todos os tiles.")
            set_solver_error(
                "resposta_ambigua_imagem_exemplo",
                "IA selecionou quase todos os tiles em desafio com imagem de exemplo; recarregando desafio.",
            )
            indices = []
        if challenge_dir:
            (challenge_dir / "resposta.json").write_text(
                json.dumps(
                    {
                        "pergunta": captcha_question,
                        "modelo": COHERE_MODEL,
                        "resposta_direta": resposta_direta,
                        "indices": indices,
                        "resposta_parseada": result_json,
                        "resposta_bruta": bot_response,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        
        print(f"[Cohere] Pergunta: '{captcha_question}'")
        print(f"[Cohere] Resposta direta: '{resposta_direta}' -> Indices: {indices}")
        
        return indices if indices else None

    except json.JSONDecodeError as e:
        print(f"[Cohere] Erro ao fazer parse do JSON: {e}")
        print(f"[Cohere] Resposta bruta: {bot_response[:500]}...")
        if challenge_dir:
            (challenge_dir / "resposta.json").write_text(
                json.dumps(
                    {
                        "pergunta": captcha_question,
                        "modelo": COHERE_MODEL,
                        "erro": "json_decode",
                        "detalhe": str(e),
                        "resposta_bruta": bot_response,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return None
    except Exception as e:
        print(f"[Cohere] Erro ao resolver captcha: {e}")
        if challenge_dir:
            (challenge_dir / "resposta.json").write_text(
                json.dumps(
                    {
                        "pergunta": captcha_question,
                        "modelo": COHERE_MODEL,
                        "erro": "cohere_exception",
                        "detalhe": str(e),
                        "resposta_bruta": bot_response,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return None


def analyze_non_9_with_cohere(challenge_dir: Path, captcha_question: str) -> dict | None:
    image_path = challenge_dir / "desafio-grade-15.png"
    if not image_path.exists():
        image_path = challenge_dir / "desafio.png"
    if not image_path.exists():
        return None
    if not COHERE_API_KEY:
        print("[Cohere] COHERE_API_KEY nao configurada no ambiente.")
        set_solver_error("cohere_key_missing", "COHERE_API_KEY nao configurada no Secret do Modal.")
        return None

    bot_response = ""
    try:
        prompt = f"""
Analise este desafio hCaptcha que NÃO está no formato de 9 tiles.

Pergunta original: "{captcha_question}"

A imagem anexada é o CANVAS do desafio, sem a área da pergunta, com uma grade desenhada por cima.
A grade tem 5 colunas x 3 linhas, numerada de 1 a 15, da esquerda para a direita e de cima para baixo.

Objetivo:
- Encontrar exatamente UM lugar para clicar.
- Escolha o ponto mais preciso que responde à pergunta original.
- Use a grade apenas como referência espacial. A resposta deve apontar uma única célula e um ponto dentro dela.
- Se houver vários candidatos, escolha o mais claro/central/inequívoco.
- Se nada corresponder, ainda indique o melhor ponto provável e marque confiança baixa.

Retorne exclusivamente um JSON válido:
```json
{{
  "descricao_geral": "descrição curta da imagem grande",
  "alvo_da_pergunta": "o alvo inferido da pergunta, se existir",
  "escolha": {{
    "celula": 8,
    "linha": 2,
    "coluna": 3,
    "x_percent_na_imagem": 52,
    "y_percent_na_imagem": 48,
    "descricao_do_alvo": "o objeto/local exato escolhido",
    "argumento": "por que este é o melhor lugar para clicar",
    "confianca": 0.0
  }},
  "observacoes": "qualquer alerta sobre ambiguidade"
}}
```
Retorne uma única escolha. Não liste múltiplos candidatos.
"""
        content = [
            {"type": "text", "text": prompt},
            {"type": "text", "text": "Canvas do desafio com grade 5x3 numerada:"},
            {"type": "image_url", "image_url": {"url": image_as_data_url(image_path)}},
        ]

        payload = {
            "model": COHERE_MODEL,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"bearer {COHERE_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        last_error = None
        response = None
        for _ in range(2):
            try:
                response = requests.post("https://api.cohere.com/v2/chat", headers=headers, json=payload, timeout=90)
                break
            except requests.Timeout as e:
                last_error = e
                time.sleep(2)
        if response is None:
            raise last_error or RuntimeError("Cohere sem resposta")
        response.raise_for_status()
        data = response.json()
        bot_response = data["message"]["content"][0]["text"]
        parsed = json.loads(bot_response.strip())
        alvo = parsed.get("alvo_da_pergunta") or ""
        escolha = parsed.get("escolha") or {}
        (challenge_dir / "resposta.json").write_text(
            json.dumps(
                {
                    "tipo": "nao_9_tiles",
                    "pergunta": captcha_question,
                    "modelo": COHERE_MODEL,
                    "resposta_parseada": parsed,
                    "resposta_bruta": bot_response,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"[Cohere] Analise nao-9 salva em: {challenge_dir / 'resposta.json'}")
        print(f"[Cohere] Nao-9 alvo='{alvo}' escolha='{escolha}'")
        return escolha if isinstance(escolha, dict) else None
    except json.JSONDecodeError as e:
        (challenge_dir / "resposta.json").write_text(
            json.dumps(
                {
                    "tipo": "nao_9_tiles",
                    "pergunta": captcha_question,
                    "modelo": COHERE_MODEL,
                    "erro": "json_decode",
                    "detalhe": str(e),
                    "resposta_bruta": bot_response,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return None
    except Exception as e:
        if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code == 429:
            set_solver_error(
                "cohere_rate_limited",
                "Cohere retornou 429 ao analisar desafio hCaptcha nao-9; tente novamente depois ou reduza paralelismo.",
            )
        else:
            set_solver_error("cohere_non9_error", str(e))
        (challenge_dir / "resposta.json").write_text(
            json.dumps(
                {
                    "tipo": "nao_9_tiles",
                    "pergunta": captcha_question,
                    "modelo": COHERE_MODEL,
                    "erro": "cohere_exception",
                    "detalhe": str(e),
                    "resposta_bruta": bot_response,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"[Cohere] Erro ao analisar desafio nao-9: {e}")
        return None


def save_and_analyze_non_9_challenge(port: int, state: dict, request_id: str, attempt: int) -> dict | None:
    if not state:
        return None
    if state.get("checkmark") and not state.get("imageCanvas"):
        return None
    task_count = len(state.get("tasks") or [])
    is_non_9 = state.get("imageCanvas") or (task_count and task_count != 9)
    if not is_non_9:
        return None
    folder = save_non_9_challenge_debug(port, state, request_id, attempt)
    if folder:
        return analyze_non_9_with_cohere(folder, str(state.get("prompt") or ""))
    return None



def auto_solve_grid(port: int, note_index: int, attempt: int, request_id: str = "request", max_refreshes: int = 10) -> str | None:
    """
    Fluxo automático completo:
    1. Abre o desafio.
    2. Detecta AUTOMATICAMENTE a pergunta do captcha.
    3. Tira print.
    4. Manda para IA.
    5. Clica e verifica.
    6. Aguarda o token aparecer no campo hidden.
    
    Retorna o token ou None se falhar.
    """
    print("[Auto] Iniciando resolução automática...")
    non_9_reloads = 0
    grid_seen = False
    cohere_failed = False
    
    for i in range(max_refreshes):
        # 1. Garantir que o desafio está aberto
        if not ensure_challenge_open(port):
            print("[Auto] Falha ao abrir o desafio. Tentando clicar no checkbox...")
            click_hcaptcha_checkbox(port)
            time.sleep(2)
            if not challenge_grid_visible(port):
                continue

        # 2. Esperar a grade de 9 tiles carregar e parar de se mexer antes de capturar.
        challenge, state = wait_for_stable_9_tile_challenge(port)
        if not challenge or not state:
            print("[Auto] Grade de 9 tiles nao estabilizou.")
            if state:
                if state.get("checkmark") and not state.get("imageCanvas"):
                    token = wait_token_or_retry_after_submit(port, timeout=12)
                    if token:
                        return token
                    print("[Auto] Marca apareceu mas token ainda nao voltou; vou continuar aguardando/procurando.")
                    time.sleep(1.5)
                    continue
                if should_skip_prompt(str(state.get("prompt") or "")):
                    print("[Auto] Pergunta contem 'mudou'; pulando desafio.")
                    set_solver_error("captcha_prompt_mudou", "Pergunta continha 'mudou'; desafio pulado.")
                    refresh_hcaptcha_and_wait(port)
                    continue
                if should_reload_non_9_before_ai(non_9_reloads):
                    non_9_reloads += 1
                    reload_non_9_challenge(port, non_9_reloads, max_refreshes)
                    continue
                escolha = save_and_analyze_non_9_challenge(port, state, request_id, attempt + i)
                if escolha:
                    print(f"[Auto] Clicando escolha nao-9: {escolha}")
                    if click_non_9_choice(port, escolha):
                        time.sleep(0.8)
                        if click_hcaptcha_submit(port):
                            token = wait_token_or_retry_after_submit(port, timeout=10)
                            if token:
                                return token
                            print("[Auto] Sem token ainda; vou continuar para o proximo desafio.")
                            time.sleep(1.5)
                            continue
                if not FAST_RELOAD_NON_9:
                    print("[Auto] Desafio nao-9 sem token; vou aguardar/procurar proximo desafio.")
                    time.sleep(1.5)
                    continue
            non_9_reloads += 1
            reload_non_9_challenge(port, non_9_reloads, max_refreshes)
            continue

        # 3. Salvar debug completo do desafio de 9 tiles e descobrir a pergunta.
        grid_seen = True
        task_count = len(state.get("tasks") or [])
        prompt_text = str(state.get("prompt") or "").strip()
        
        print(f"[Auto] Tasks encontradas: {task_count}, Pergunta: '{prompt_text}'")
        if should_skip_prompt(prompt_text):
            print("[Auto] Pergunta contem 'mudou'; pulando desafio.")
            set_solver_error("captcha_prompt_mudou", "Pergunta continha 'mudou'; desafio pulado.")
            refresh_hcaptcha_and_wait(port)
            continue
        
        if task_count != 9 or not prompt_text:
            print("[Auto] Formato inesperado.")
            if should_reload_non_9_before_ai(non_9_reloads):
                non_9_reloads += 1
                reload_non_9_challenge(port, non_9_reloads, max_refreshes)
                continue
            escolha = save_and_analyze_non_9_challenge(port, state, request_id, attempt + i)
            if escolha:
                print(f"[Auto] Clicando escolha nao-9: {escolha}")
                if click_non_9_choice(port, escolha):
                    time.sleep(0.8)
                    if click_hcaptcha_submit(port):
                        token = wait_token_or_retry_after_submit(port, timeout=10)
                        if token:
                            return token
                        print("[Auto] Sem token ainda; vou continuar para o proximo desafio.")
                        time.sleep(1.5)
                        continue
            if not FAST_RELOAD_NON_9:
                print("[Auto] Desafio nao-9 sem token; vou aguardar/procurar proximo desafio.")
                time.sleep(1.5)
                continue
            non_9_reloads += 1
            reload_non_9_challenge(port, non_9_reloads, max_refreshes)
            continue
        challenge_dir = save_9_tile_challenge_debug(port, state, request_id, attempt + i)
        image_path = challenge_dir / "desafio.png"
        if not image_path.exists():
            image_path = challenge_dir / "grade.png"
        if not image_path.exists():
            print("[Auto] Falha ao salvar imagem do desafio. Recarregando...")
            refresh_hcaptcha_and_wait(port)
            continue

        # 4. Resolver com Cohere
        indices = solve_with_cohere(image_path, prompt_text, challenge_dir)
        if not indices:
            cohere_failed = True
            set_solver_error("nao_consegui_resolver_9_tiles", "Achei o 9 tiles, mas a IA nao retornou resposta valida.")
            print("[Auto] Cohere nao retornou resposta valida. Recarregando...")
            try:
                page = wait_solver_page(port)
                client = CdpClient(page["webSocketDebuggerUrl"])
                client.call("Page.reload", {"ignoreCache": True})
                client.close()
            except: 
                pass
            time.sleep(2)
            continue

        # 5. Clicar nas tarefas
        print(f"[Auto] Clicando nos itens: {indices}")
        if not click_hcaptcha_tasks(port, indices):
            set_solver_error("falha_clicar_9_tiles", "Achei e analisei o 9 tiles, mas falhei ao clicar nas tarefas.")
            print("[Auto] Falha ao clicar nas tarefas.")
            time.sleep(1)

        time.sleep(1)

        # 6. Submeter
        if not click_hcaptcha_submit(port):
            set_solver_error("falha_submit_9_tiles", "Achei e cliquei o 9 tiles, mas falhei ao confirmar.")
            print("[Auto] Falha ao clicar em submit.")
            continue

        # 7. Aguardar o token aparecer
        token = wait_token_or_retry_after_submit(port, timeout=10)
        if token:
            return token
        
        print("[Auto] Token nao encontrado; vou aguardar o hCaptcha trocar sozinho.")
        set_solver_error("token_nao_voltou", "hCaptcha nao devolveu token depois do submit.")
        time.sleep(2)
    
    if not grid_seen and not has_solver_error():
        if non_9_reloads:
            set_solver_error("nao_achou_9_tiles", f"Nao conseguiu achar 9 tiles apos {non_9_reloads} recarregamentos.")
        else:
            set_solver_error("grade_9_nao_estabilizou", f"Grade de 9 tiles nao estabilizou apos {max_refreshes} tentativas.")
    elif cohere_failed and not has_solver_error():
        set_solver_error("nao_consegui_resolver_9_tiles", "Achei 9 tiles, mas nao consegui resolver com a IA dentro do limite.")
    print("[Auto] Falha apos todas as tentativas.")
    return None


def extract_token_from_page(port: int) -> str | None:
    """Extrai o token do campo hidden h-captcha-response"""
    try:
        pages = list_pages(port)
        parent = next(
            (
                page
                for page in pages
                if page.get("type") == "page" and is_solver_page_url(page.get("url", "")) and page.get("webSocketDebuggerUrl")
            ),
            None,
        )
        if not parent:
            return None
        
        client = CdpClient(parent["webSocketDebuggerUrl"])
        try:
            token = client.eval(
                """
(() => {
  const field = document.querySelector('[name="h-captcha-response"]') || 
                document.querySelector('#h-captcha-response');
  return field ? field.value : null;
})()
"""
            )
            return token if token and len(token) > 10 else None
        finally:
            client.close()
    except Exception:
        return None


class SolverRequestHandler(BaseHTTPRequestHandler):
    """Handler HTTP para receber requisições de solving"""
    
    def log_message(self, format, *args):
        # Silenciar logs padrão
        pass
    
    def do_POST(self):
        """Recebe requisição POST com sitekey e URL"""
        global ACTIVE_SOLVERS
        if self.path != "/solve":
            self.send_response(404)
            self.end_headers()
            return
        
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        
        try:
            data = json.loads(body)
            sitekey = data.get("sitekey")
            url = data.get("url", "https://www.nfse.gov.br/")
            
            if not sitekey:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "sitekey obrigatorio"}).encode())
                return
            
            request_id = data.get("request_id") or datetime.now().strftime("%Y%m%d%H%M%S%f")
            LAST_SOLVER_ERROR.value = None
            print(f"\\n[Solver API] Recebida requisicao {request_id}: sitekey={sitekey[:20]}...")

            if SOLVER_SEMAPHORE:
                print(f"[Solver API] {request_id}: aguardando vaga de navegador...")
                SOLVER_SEMAPHORE.acquire()
            with ACTIVE_LOCK:
                ACTIVE_SOLVERS += 1
            try:
                token = solve_captcha_for_request(sitekey, url, request_id)
            finally:
                with ACTIVE_LOCK:
                    ACTIVE_SOLVERS -= 1
                if SOLVER_SEMAPHORE:
                    SOLVER_SEMAPHORE.release()
            
            if token:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                response = {"token": token, "success": True, "request_id": request_id}
                self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))
                print(f"[Solver API] {request_id}: token enviado: {len(token)} chars")
            else:
                error_info = get_solver_error("solver_failed")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                response = {
                    "error": error_info.get("detail") or "Falha ao resolver captcha",
                    "reason": error_info.get("reason") or "solver_failed",
                    "success": False,
                    "request_id": request_id,
                }
                self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))
                print(f"[Solver API] {request_id}: falha ao resolver: {response['reason']} - {response['error']}")
                
        except Exception as e:
            print(f"[Solver API] Erro: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e), "reason": "api_exception", "success": False}, ensure_ascii=False).encode("utf-8"))
    
    def do_GET(self):
        """Endpoint de health check"""
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            with ACTIVE_LOCK:
                active = ACTIVE_SOLVERS
            self.wfile.write(json.dumps({
                "status": "ok",
                "service": "hcaptcha-solver",
                "version": SOLVER_API_VERSION,
                "max_browsers": MAX_BROWSERS,
                "active_browsers": active,
                "reload_non_9": FAST_RELOAD_NON_9,
                "non_9_reloads_before_ai": NON_9_RELOADS_BEFORE_AI,
            }).encode())
        else:
            self.send_response(404)
            self.end_headers()


def solve_captcha_for_request(sitekey: str, url: str, request_id: str = "request") -> str | None:
    """
    Abre navegador isolado, carrega página com hCaptcha, resolve e retorna token.
    """
    browser = find_browser(BROWSER_OVERRIDE)
    server = None
    
    solver_port = None
    profile = None
    proc = None
    
    try:
        server, server_port, token_state = start_solver_page(sitekey)
        solver_url = f"http://{SOLVER_PAGE_HOST}:{server_port}/"
        solver_port, profile, proc = open_solver_browser(browser, solver_url)
        print(f"[Solver API] Navegador aberto na porta {solver_port}")
        
        wait_solver_page(solver_port)
        
        token = auto_solve_grid(solver_port, 0, 1, request_id=request_id)
        if token:
            return token

        end = time.time() + 10
        while time.time() < end:
            token = token_state.get()
            if token:
                return token
            token = extract_token_from_page(solver_port)
            if token:
                return token
            time.sleep(0.5)
        
        if not has_solver_error():
            set_solver_error("token_nao_voltou", "Captcha pode ter resolvido, mas o token nao voltou para a pagina local.")
        return None
        
    except Exception as e:
        set_solver_error("solver_exception", str(e))
        print(f"[Solver API] Erro durante resolucao: {e}")
        return None
    finally:
        if server:
            server.shutdown()
        # Limpar
        if solver_port:
            close_solver_browser(solver_port, proc)


def main():
    global BROWSER_OVERRIDE, SOLVER_SEMAPHORE, MAX_BROWSERS, FAST_RELOAD_NON_9
    parser = argparse.ArgumentParser(description="API Server para resolver hCaptcha")
    parser.add_argument("--port", type=int, default=8765, help="Porta do servidor API")
    parser.add_argument("--browser", default=None, help="Caminho do navegador")
    parser.add_argument("--max-browsers", type=int, default=1, help="Quantidade maxima de navegadores resolvedores em paralelo.")
    parser.add_argument("--recarregar-nao-9", action="store_true", help="Recarrega automaticamente desafios que nao forem grade de 9 tiles.")
    args = parser.parse_args()
    BROWSER_OVERRIDE = args.browser
    MAX_BROWSERS = max(1, args.max_browsers)
    FAST_RELOAD_NON_9 = bool(args.recarregar_nao_9)
    SOLVER_SEMAPHORE = threading.BoundedSemaphore(MAX_BROWSERS)
    
    print(f"=" * 60)
    print(f"hCaptcha Solver API")
    print(f"Porta: {args.port}")
    print(f"Endpoint: http://127.0.0.1:{args.port}/solve")
    print(f"Health: http://127.0.0.1:{args.port}/health")
    print(f"Navegadores simultaneos: {MAX_BROWSERS}")
    print(f"Recarregar nao-9 automaticamente: {FAST_RELOAD_NON_9}")
    print(f"=" * 60)
    print(f"Aguardando requisicoes...\\n")
    
    class SolverHTTPServer(ThreadingHTTPServer):
        daemon_threads = True

    host = os.environ.get("HOST", "127.0.0.1")
    server = SolverHTTPServer((host, args.port), SolverRequestHandler)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\\nServidor encerrado.")
        server.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        sys.exit(1)
