from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
LEGACY_SOLVER_PATH = BASE_DIR / "api_resolvedora_resolver.py"
DEFAULT_GOOGLE_AI_PROJECT = Path(
    r"C:\Users\ryang\Desktop\projetosv2\google_modo_ia_perfil_limpo"
)
GOOGLE_AI_PROJECT = Path(
    os.environ.get("GOOGLE_AI_PROJECT", str(DEFAULT_GOOGLE_AI_PROJECT))
).expanduser().resolve()
GOOGLE_AI_STATE_DIR = Path(
    os.environ.get("GOOGLE_AI_STATE_DIR", str(GOOGLE_AI_PROJECT))
).expanduser().resolve()
GOOGLE_AI_CLIENT_PATH = GOOGLE_AI_PROJECT / "google_ia_requests.py"
DEFAULT_DETECTOR_PROJECT = Path(
    r"C:\Users\ryang\Desktop\projetosv2\modo_ia_detector_visual"
)
DETECTOR_PROJECT = Path(
    os.environ.get("MODO_IA_DETECTOR_PROJECT", str(DEFAULT_DETECTOR_PROJECT))
).expanduser().resolve()
DETECTOR_CLIENT_PATH = DETECTOR_PROJECT / "detector_visual.py"
API_DIR = BASE_DIR / "api"
PROVIDER_DIR = API_DIR / "google-ai-resolvedora"
PROVIDER_DIR.mkdir(parents=True, exist_ok=True)

SOLVER_API_VERSION = "2026-07-13-google-ai-mode-v11-bundled-source"
PROVIDER_MODEL = "google-ai-mode-multimodal"
PROVIDER_LOCK = threading.Lock()
PROVIDER_STATS_LOCK = threading.Lock()
PROVIDER_STATS: dict[str, Any] = {
    "requests": 0,
    "successes": 0,
    "failures": 0,
    "last_error": None,
    "last_success_at": None,
    "last_http_requests": None,
    "last_ai_queries": None,
    "last_source_count": None,
}


def _load_module(path: Path, name: str) -> ModuleType:
    if not path.is_file():
        raise RuntimeError(f"Modulo obrigatorio nao encontrado: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Nao foi possivel carregar modulo: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


legacy = _load_module(LEGACY_SOLVER_PATH, "_portal_hcaptcha_solver_core")
google_ai = _load_module(GOOGLE_AI_CLIENT_PATH, "_portal_google_ai_client")
detector = _load_module(DETECTOR_CLIENT_PATH, "_portal_modo_ia_detector_visual")

# Estado e artefatos exclusivos desta API do Google Modo IA.
legacy.SOLVER_API_VERSION = SOLVER_API_VERSION
legacy.SOLVER_PROFILES = API_DIR / "chrome-profiles-hcaptcha-google-ia"
legacy.CAPTCHA_DIR = Path(
    os.environ.get(
        "GOOGLE_AI_ARTIFACT_ROOT",
        str(API_DIR / "hcaptcha-imagens-google-ia"),
    )
)
legacy.CHALLENGES_DIR = legacy.CAPTCHA_DIR / "desafios"
legacy.CHALLENGES_9_DIR = legacy.CHALLENGES_DIR / "9-tiles"
legacy.CHALLENGES_NON_9_DIR = legacy.CHALLENGES_DIR / "nao-9-tiles"
legacy.PROVIDER_ABORT_FILE = PROVIDER_DIR / "google-ai-circuit-open.json"
legacy.SOLVER_ABORT_FILE = PROVIDER_DIR / "solver-circuit-open.json"
legacy.LEGACY_PROVIDER_MODEL = PROVIDER_MODEL  # campo legado usado apenas em arquivos de diagnostico

# Perfil rapido, limitado a esta API.
legacy.BROWSER_EXTRA_ARGS = [
    "--window-size=1180,900",
    "--window-position=30,30",
    "--disable-notifications",
    "--disable-features=Translate,MediaRouter",
]
_modal_proxy = os.environ.get("HTTPS_PROXY", "").strip()
if _modal_proxy:
    legacy.BROWSER_EXTRA_ARGS.append(f"--proxy-server={_modal_proxy}")
legacy.CHALLENGE_STABLE_REQUIRED_POLLS = 2
legacy.CHALLENGE_STABLE_POLL_SECONDS = 0.15
legacy.CHALLENGE_STABLE_SETTLE_SECONDS = 0.10
legacy.NON9_CAPTURE_SETTLE_SECONDS = 0.12
legacy.OPEN_CHALLENGE_RETRY_DELAY_SECONDS = 0.35
legacy.NON9_POST_CLICK_DELAY_SECONDS = 0.12
legacy.NON9_NEXT_CHALLENGE_DELAY_SECONDS = 0.12
legacy.TOKEN_POLL_SECONDS = 0.12
legacy.TOKEN_RETRY_ABORT_SECONDS = 0.55
legacy.CHECKMARK_TOKEN_EXTENSION_SECONDS = 4.0
legacy.TILE_CLICK_INTERVAL_SECONDS = 0.04
legacy.TILE_POST_SELECTION_DELAY_SECONDS = 0.18


# Esta variante nunca descarta um desafio por palavras da pergunta. A regra antiga
# pulava prompts contendo "mudou"; aqui todo desafio visual segue para analise.
def _never_skip_prompt(prompt_text: str) -> bool:
    return False


legacy.should_skip_prompt = _never_skip_prompt


# hCaptcha pode trocar imagens enquanto uma grade de 9 tiles ainda está
# carregando. O núcleo compartilhado devolve o último estado completo como
# "não estabilizado"; nesta API isso precisa continuar sendo tratado como 9
# tiles, para a imagem passar pela análise do Google Modo IA.
_legacy_wait_for_stable = legacy.wait_for_stable_9_tile_challenge


def _wait_for_stable_google_ai(port: int, timeout: int = 8):
    token = legacy.extract_token_from_page(port)
    if token:
        return None, {
            "prompt": "",
            "tasks": [],
            "grid": None,
            "allLoaded": False,
            "signature": "token-ready",
            "checkmark": True,
            "imageCanvas": False,
            "tokenReady": True,
        }
    challenge, state = _legacy_wait_for_stable(port, timeout=timeout)
    if challenge is None and state:
        tasks = state.get("tasks") or []
        if (
            len(tasks) == 9
            and state.get("allLoaded")
            and state.get("prompt")
            and state.get("grid")
        ):
            page = legacy.challenge_page(port)
            if page:
                print(
                    "[Google AI] Grade 9 movel; usando o ultimo snapshot completo "
                    "em vez de classificar como nao-9.",
                    flush=True,
                )
                return page, state
    return challenge, state


legacy.wait_for_stable_9_tile_challenge = _wait_for_stable_google_ai


_legacy_ensure_challenge_open = legacy.ensure_challenge_open


def _ensure_challenge_or_token(port: int, max_clicks: int = 8) -> bool:
    if legacy.extract_token_from_page(port):
        print("[Google AI] Token ja presente; nao vou clicar novamente no checkbox.")
        return True
    return _legacy_ensure_challenge_open(port, max_clicks=max_clicks)


legacy.ensure_challenge_open = _ensure_challenge_or_token


def _capture_non_9_canvas_clean(port: int, folder: Path, stem: str = "desafio") -> bool:
    """Captura apenas a area selecionavel, sem grade artificial nem particoes."""
    page = legacy.challenge_page(port)
    if not page:
        return False
    try:
        client = legacy.CdpClient(page["webSocketDebuggerUrl"])
        try:
            # O overlay serve apenas para auditoria visual. Remova-o antes da proxima
            # captura para que caixas/nomes antigos nunca contaminem a imagem enviada a IA.
            client.eval(
                """
new Promise((resolve) => {
  document.getElementById('google-ai-vision-overlay')?.remove();
  document.getElementById('google-ai-debug-panel')?.remove();
  document.getElementById('codex-non9-grid-overlay')?.remove();
  requestAnimationFrame(() => requestAnimationFrame(() => resolve(true)));
})
""",
                await_promise=True,
            )
            data = client.eval(
                """
(() => {
  const src = [...document.querySelectorAll('canvas[role="img"]')]
    .find((canvas) => {
      const label = canvas.getAttribute('aria-label') || '';
      return label.includes('Desafio de CAPTCHA baseado em imagem') &&
        canvas.width >= 900 && canvas.height >= 900;
    });
  if (!src) return null;
  const r = src.getBoundingClientRect();
  return {
    source_width: src.width,
    source_height: src.height,
    clip: {
      x: Math.max(0, r.left),
      y: Math.max(0, r.top),
      width: Math.max(1, r.width),
      height: Math.max(1, r.height),
      scale: 1
    }
  };
})()
"""
            )
        finally:
            client.close()
    except Exception as exc:
        print(f"[Debug] Falha ao extrair canvas nao-9 limpo: {exc}")
        return False

    if not data or not data.get("clip"):
        return False
    clip = data["clip"]
    top_cut = min(150.0, max(0.0, float(clip["height"]) - 20.0))
    full_path = folder / ("desafio-completo.png" if stem == "desafio" else f"{stem}-completo.png")
    clean_path = folder / f"{stem}.png"
    data["top_cut_css_px"] = top_cut
    if not legacy.capture_challenge_png(port, full_path, clip):
        return False
    if not legacy.crop_png_top(full_path, clean_path, top_cut, float(clip["height"])):
        return False
    if legacy.png_seems_blank(clean_path):
        print("[Debug] Captura limpa nao-9 veio branca.")
        return False
    try:
        with legacy.Image.open(clean_path) as image:
            data["clean_image_width"] = image.width
            data["clean_image_height"] = image.height
    except Exception:
        pass
    if stem == "desafio":
        (folder / "canvas-info.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return True


legacy.capture_non_9_canvas_artifacts = _capture_non_9_canvas_clean


def _image_entropy(path: Path) -> float:
    try:
        with legacy.Image.open(path) as image:
            return float(image.convert("RGB").entropy())
    except Exception:
        return 0.0


def _build_motion_sequence(folder: Path, frames: list[Path]) -> Path | None:
    """Monta quatro instantes em ordem temporal sem alterar a escala de clique."""
    usable = [path for path in frames if path.is_file() and not legacy.png_seems_blank(path)]
    if len(usable) < 2:
        return None
    opened = []
    try:
        for path in usable[:4]:
            opened.append(legacy.Image.open(path).convert("RGB"))
        width, height = opened[0].size
        opened = [image if image.size == (width, height) else image.resize((width, height)) for image in opened]
        gap = 8
        canvas = legacy.Image.new("RGB", (width * 2 + gap, height * 2 + gap), "white")
        positions = ((0, 0), (width + gap, 0), (0, height + gap), (width + gap, height + gap))
        for image, position in zip(opened, positions):
            canvas.paste(image, position)
        sequence_path = folder / "sequencia-temporal.jpg"
        canvas.save(sequence_path, format="JPEG", quality=86, optimize=False)
        return sequence_path
    finally:
        for image in opened:
            image.close()


def _capture_non_9_canvas_sequence(
    port: int,
    folder: Path,
    frame_count: int = 4,
    interval_ms: int = 180,
) -> list[Path]:
    """Le quadros contiguos do proprio canvas, evitando latencia de screenshots CDP."""
    page = legacy.challenge_page(port)
    if not page:
        return []
    try:
        client = legacy.CdpClient(page["webSocketDebuggerUrl"])
        try:
            data = client.eval(
                f"""
new Promise(async (resolve) => {{
  document.getElementById('google-ai-vision-overlay')?.remove();
  document.getElementById('google-ai-debug-panel')?.remove();
  document.getElementById('codex-non9-grid-overlay')?.remove();
  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  const src = [...document.querySelectorAll('canvas[role="img"]')].find((canvas) => {{
    const label = canvas.getAttribute('aria-label') || '';
    return label.includes('Desafio de CAPTCHA baseado em imagem') &&
      canvas.width >= 900 && canvas.height >= 900;
  }});
  if (!src) return resolve(null);
  const rect = src.getBoundingClientRect();
  const topCutCss = Math.min(150, Math.max(0, rect.height - 20));
  const topCutNative = Math.round((topCutCss / Math.max(1, rect.height)) * src.height);
  const probe = document.createElement('canvas');
  probe.width = 40;
  probe.height = 28;
  const probeContext = probe.getContext('2d', {{willReadFrequently: true}});
  const visualScore = () => {{
    probeContext.clearRect(0, 0, probe.width, probe.height);
    probeContext.drawImage(
      src, 0, topCutNative, src.width, Math.max(1, src.height - topCutNative),
      0, 0, probe.width, probe.height
    );
    const pixels = probeContext.getImageData(0, 0, probe.width, probe.height).data;
    let sum = 0;
    let sumSquares = 0;
    for (let i = 0; i < pixels.length; i += 4) {{
      const value = pixels[i] * 0.299 + pixels[i + 1] * 0.587 + pixels[i + 2] * 0.114;
      sum += value;
      sumSquares += value * value;
    }}
    const count = pixels.length / 4;
    const mean = sum / count;
    return Math.sqrt(Math.max(0, sumSquares / count - mean * mean));
  }};
  const readyStarted = Date.now();
  let score = 0;
  let stableReady = 0;
  while (Date.now() - readyStarted < 4000) {{
    score = visualScore();
    stableReady = score >= 12 ? stableReady + 1 : 0;
    if (stableReady >= 2) break;
    await new Promise((done) => setTimeout(done, 90));
  }}
  if (stableReady < 2) return resolve({{loading: true, visual_score: score}});
  const frames = [];
  try {{
    for (let i = 0; i < {max(2, frame_count)}; i++) {{
      frames.push(src.toDataURL('image/jpeg', 0.86));
      if (i + 1 < {max(2, frame_count)}) {{
        await new Promise((done) => setTimeout(done, {max(40, interval_ms)}));
      }}
    }}
  }} catch (error) {{
    return resolve({{error: String(error)}});
  }}
  resolve({{
    width: src.width,
    height: src.height,
    top_cut_native: topCutNative,
    interval_ms: {max(40, interval_ms)},
    ready_wait_ms: Date.now() - readyStarted,
    visual_score: score,
    frames
  }});
}})
""",
                await_promise=True,
            )
        finally:
            client.close()
    except Exception as exc:
        print(f"[Debug] Falha ao capturar sequencia direta do canvas: {exc}")
        return []
    if not isinstance(data, dict) or not isinstance(data.get("frames"), list):
        return []

    top_cut = max(0, int(data.get("top_cut_native") or 0))
    paths: list[Path] = []
    for index, data_url in enumerate(data["frames"][:frame_count], 1):
        full_path = folder / f"quadro-{index:02d}-completo.jpg"
        clean_path = folder / f"quadro-{index:02d}.jpg"
        try:
            full_path.write_bytes(base64.b64decode(str(data_url).split(",", 1)[1]))
        except Exception:
            continue
        try:
            with legacy.Image.open(full_path) as image:
                cut = min(top_cut, image.height - 1)
                image.crop((0, cut, image.width, image.height)).save(
                    clean_path,
                    format="JPEG",
                    quality=86,
                    optimize=False,
                )
        except Exception:
            continue
        if legacy.png_seems_blank(clean_path):
            continue
        paths.append(clean_path)
    if paths:
        with legacy.Image.open(paths[-1]) as image:
            image.save(folder / "desafio.png")
        full_last = paths[-1].with_name(paths[-1].stem + "-completo.jpg")
        if full_last.is_file():
            shutil.copy2(full_last, folder / "desafio-completo.jpg")
        (folder / "canvas-info.json").write_text(
            json.dumps(
                {
                    "source_width": data.get("width"),
                    "source_height": data.get("height"),
                    "top_cut_native_px": top_cut,
                    "temporal_frames": len(paths),
                    "interval_ms": data.get("interval_ms"),
                    "ready_wait_ms": data.get("ready_wait_ms"),
                    "visual_score": data.get("visual_score"),
                    "capture_method": "canvas_to_data_url_sequence",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return paths


def _capture_live_canvas_for_click(port: int, output_path: Path) -> dict[str, Any] | None:
    """Captura um unico quadro atual e a pergunta para impedir clique atrasado."""
    page = legacy.challenge_page(port)
    if not page:
        return None
    try:
        client = legacy.CdpClient(page["webSocketDebuggerUrl"])
        try:
            data = client.eval(
                """
(() => {
  document.getElementById('google-ai-vision-overlay')?.remove();
  document.getElementById('google-ai-debug-panel')?.remove();
  const src = [...document.querySelectorAll('canvas[role="img"]')].find((canvas) => {
    const label = canvas.getAttribute('aria-label') || '';
    return label.includes('Desafio de CAPTCHA baseado em imagem') &&
      canvas.width >= 900 && canvas.height >= 900;
  });
  if (!src) return null;
  const rect = src.getBoundingClientRect();
  const topCutCss = Math.min(150, Math.max(0, rect.height - 20));
  const topCutNative = Math.round((topCutCss / Math.max(1, rect.height)) * src.height);
  const prompt = (document.querySelector('#prompt-question')?.innerText || '')
    .trim().replace(/\\s+/g, ' ');
  return {
    prompt,
    width: src.width,
    height: src.height,
    top_cut_native: topCutNative,
    image: src.toDataURL('image/jpeg', 0.86)
  };
})()
"""
            )
        finally:
            client.close()
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("image"):
        return None
    try:
        raw = base64.b64decode(str(data["image"]).split(",", 1)[1])
        temporary = output_path.with_name(output_path.stem + "-completo.jpg")
        temporary.write_bytes(raw)
        with legacy.Image.open(temporary) as image:
            cut = min(max(0, int(data.get("top_cut_native") or 0)), image.height - 1)
            image.crop((0, cut, image.width, image.height)).save(
                output_path,
                format="JPEG",
                quality=86,
                optimize=False,
            )
        data["path"] = str(output_path)
        return data
    except Exception:
        return None


def _relocate_choice_on_live_canvas(
    port: int,
    challenge_dir: Path,
    question: str,
    selected: Any,
    click_choice: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {"safe": False, "reason": "live_capture_failed"}
    live_path = challenge_dir / "quadro-atual-antes-clique.jpg"
    live = _capture_live_canvas_for_click(port, live_path)
    if not live:
        return result
    current_question = " ".join(str(live.get("prompt") or "").split()).casefold()
    expected_question = " ".join(str(question or "").split()).casefold()
    if current_question != expected_question:
        return {
            "safe": False,
            "reason": "challenge_changed_while_ai_was_answering",
            "expected_prompt": expected_question,
            "current_prompt": current_question,
        }
    try:
        import cv2
    except Exception:
        return {"safe": True, "reason": "opencv_unavailable_keep_original"}
    base_path = challenge_dir / "desafio.png"
    base = cv2.imread(str(base_path))
    current = cv2.imread(str(live_path))
    if base is None or current is None or selected is None:
        return result
    height, width = base.shape[:2]
    x1 = max(0, min(width - 1, int(round(selected.x1 / 1000.0 * width))))
    y1 = max(0, min(height - 1, int(round(selected.y1 / 1000.0 * height))))
    x2 = max(x1 + 1, min(width, int(round(selected.x2 / 1000.0 * width))))
    y2 = max(y1 + 1, min(height, int(round(selected.y2 / 1000.0 * height))))
    template = base[y1:y2, x1:x2]
    if template.size == 0:
        return result
    best = None
    for scale in (0.85, 0.95, 1.0, 1.05, 1.15):
        scaled_width = max(12, int(round(template.shape[1] * scale)))
        scaled_height = max(12, int(round(template.shape[0] * scale)))
        if scaled_width >= current.shape[1] or scaled_height >= current.shape[0]:
            continue
        scaled = cv2.resize(template, (scaled_width, scaled_height))
        scores = cv2.matchTemplate(current, scaled, cv2.TM_CCOEFF_NORMED)
        _, confidence, _, location = cv2.minMaxLoc(scores)
        candidate = (float(confidence), location, scaled_width, scaled_height, scale)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None or best[0] < 0.52:
        return {
            "safe": False,
            "reason": "target_not_found_in_current_frame",
            "confidence": best[0] if best else None,
        }
    confidence, (left, top), match_width, match_height, scale = best
    click_choice["x_normalizado"] = (left + match_width / 2.0) / current.shape[1] * 1000.0
    click_choice["y_normalizado"] = (top + match_height / 2.0) / current.shape[0] * 1000.0
    click_choice["x_percent_na_imagem"] = click_choice["x_normalizado"] / 10.0
    click_choice["y_percent_na_imagem"] = click_choice["y_normalizado"] / 10.0
    return {
        "safe": True,
        "reason": "target_relocated_on_current_frame",
        "confidence": confidence,
        "scale": scale,
        "x_normalizado": click_choice["x_normalizado"],
        "y_normalizado": click_choice["y_normalizado"],
    }


# A captura geral costuma acontecer um quadro antes da grade real terminar de pintar.
# O arquivo recortado da grade e objetivamente mais informativo nesse caso.
_legacy_create_debug_folder = legacy.create_challenge_debug_folder


def _create_google_ai_debug_folder(
    request_id: str,
    attempt: int,
    prompt_text: str,
    root: Path | None = None,
) -> Path:
    return _legacy_create_debug_folder(
        request_id,
        attempt,
        prompt_text,
        root=legacy.CHALLENGES_9_DIR if root is None else root,
    )


legacy.create_challenge_debug_folder = _create_google_ai_debug_folder
_legacy_save_9_tile = legacy.save_9_tile_challenge_debug


def _save_9_tile_google_ai(port: int, state: dict, request_id: str, attempt: int) -> Path:
    folder = _legacy_save_9_tile(port, state, request_id, attempt)
    full_path = folder / "desafio.png"
    grid_path = folder / "grade.png"
    if grid_path.is_file() and _image_entropy(grid_path) > _image_entropy(full_path) + 0.75:
        shutil.copy2(grid_path, full_path)
        (folder / "captura-escolhida.txt").write_text(
            "grade.png substituiu a captura geral ainda incompleta.\n",
            encoding="utf-8",
        )
        print("[Google AI] Grade recortada pronta; ignorando a captura geral ainda cinza.")
    return folder


legacy.save_9_tile_challenge_debug = _save_9_tile_google_ai


def _safe_stage_name(stage: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", stage).strip("-") or "estado"


def _save_browser_dom_debug(port: int, folder: Path, stage: str) -> dict[str, Any] | None:
    """Salva resumo e HTML do frame do desafio sem depender de OCR."""
    page = legacy.challenge_page(port)
    if not page:
        return None
    client = legacy.CdpClient(page["webSocketDebuggerUrl"])
    try:
        summary = client.eval(
            r"""
(() => {
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' &&
      Number(s.opacity || '1') > 0.05 && r.width > 1 && r.height > 1;
  };
  const prompt = document.querySelector('#prompt-question')?.innerText?.trim() || '';
  const tasks = [...document.querySelectorAll('.task[role="button"], .task')].map((el, i) => {
    const r = el.getBoundingClientRect();
    return {index: i + 1, left: r.left, top: r.top, width: r.width, height: r.height, visible: visible(el)};
  });
  const canvas = [...document.querySelectorAll('canvas[role="img"]')].find((el) =>
    (el.getAttribute('aria-label') || '').includes('Desafio de CAPTCHA baseado em imagem'));
  const canvasRect = canvas ? canvas.getBoundingClientRect() : null;
  const submit = document.querySelector('.button-submit, [data-testid="submit-button"], button[type="submit"]');
  const error = document.querySelector('.error-text');
  const overlay = document.getElementById('google-ai-vision-overlay');
  return {
    url: location.href,
    title: document.title,
    ready_state: document.readyState,
    prompt,
    task_count: tasks.length,
    tasks,
    canvas: canvas ? {
      width: canvas.width,
      height: canvas.height,
      aria_label: canvas.getAttribute('aria-label') || '',
      rect: canvasRect ? {left: canvasRect.left, top: canvasRect.top, width: canvasRect.width, height: canvasRect.height} : null
    } : null,
    submit: submit ? {text: submit.innerText || '', disabled: !!submit.disabled, visible: visible(submit)} : null,
    error: error ? {text: error.innerText || '', visible: visible(error)} : null,
    overlay_present: !!overlay,
    body_text_preview: (document.body?.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 1200),
    timestamp: new Date().toISOString()
  };
})()
"""
        )
        html_text = client.eval("document.documentElement ? document.documentElement.outerHTML : ''")
    finally:
        client.close()
    safe = _safe_stage_name(stage)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"dom-{safe}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if isinstance(html_text, str) and html_text:
        (folder / f"dom-{safe}.html").write_text(html_text, encoding="utf-8", errors="replace")
    return summary if isinstance(summary, dict) else None


def _inject_green_browser_overlay(
    port: int,
    by_key: dict[str, Any],
    click_choice: dict[str, Any],
    challenge_dir: Path,
    captcha_question: str,
) -> dict[str, Any] | None:
    payload = {
        "question": captcha_question,
        "selected": click_choice.get("objeto"),
        "x": float(click_choice.get("x_normalizado") or 0),
        "y": float(click_choice.get("y_normalizado") or 0),
        "confidence": click_choice.get("confianca"),
        "objects": [
            {
                "key": key,
                "label": detection.label,
                "x1": detection.x1,
                "y1": detection.y1,
                "x2": detection.x2,
                "y2": detection.y2,
                "confidence": detection.confidence,
            }
            for key, detection in by_key.items()
        ],
    }
    page = legacy.challenge_page(port)
    if not page:
        return None
    client = legacy.CdpClient(page["webSocketDebuggerUrl"])
    try:
        result = client.eval(
            f"""
(() => {{
  const data = {json.dumps(payload, ensure_ascii=False)};
  document.getElementById('google-ai-vision-overlay')?.remove();
  document.getElementById('google-ai-debug-panel')?.remove();
  const canvas = [...document.querySelectorAll('canvas[role="img"]')].find((el) =>
    (el.getAttribute('aria-label') || '').includes('Desafio de CAPTCHA baseado em imagem'));
  if (!canvas) return {{ok: false, reason: 'canvas_not_found'}};
  const r = canvas.getBoundingClientRect();
  const topCut = Math.min(150, Math.max(0, r.height - 20));
  const selectableHeight = Math.max(1, r.height - topCut);
  const overlay = document.createElement('div');
  overlay.id = 'google-ai-vision-overlay';
  Object.assign(overlay.style, {{
    position: 'fixed', left: `${{r.left}}px`, top: `${{r.top + topCut}}px`,
    width: `${{r.width}}px`, height: `${{selectableHeight}}px`,
    zIndex: '2147483645', pointerEvents: 'none', boxSizing: 'border-box'
  }});
  for (const item of data.objects) {{
    const selected = item.key === data.selected;
    const box = document.createElement('div');
    Object.assign(box.style, {{
      position: 'absolute',
      left: `${{item.x1 / 10}}%`, top: `${{item.y1 / 10}}%`,
      width: `${{Math.max(0.5, (item.x2 - item.x1) / 10)}}%`,
      height: `${{Math.max(0.5, (item.y2 - item.y1) / 10)}}%`,
      border: selected ? '4px solid #00ff66' : '2px solid #00e85a',
      background: selected ? 'rgba(0,255,102,.22)' : 'rgba(0,232,90,.10)',
      boxShadow: selected ? '0 0 0 2px rgba(0,0,0,.75), 0 0 14px #00ff66' : '0 0 0 1px rgba(0,0,0,.65)',
      boxSizing: 'border-box'
    }});
    const label = document.createElement('div');
    const conf = item.confidence == null ? '' : ` ${{Math.round(item.confidence * 100)}}%`;
    label.textContent = `${{selected ? '✓ ' : ''}}${{item.label}}${{conf}}`;
    Object.assign(label.style, {{
      position: 'absolute', left: '-2px', top: '-27px', padding: '3px 7px',
      background: selected ? '#008c3b' : '#006b30', color: '#fff',
      border: '1px solid #00ff66', borderRadius: '4px', whiteSpace: 'nowrap',
      font: 'bold 14px Arial, sans-serif', textShadow: '0 1px 2px #000'
    }});
    box.appendChild(label);
    overlay.appendChild(box);
  }}
  const point = document.createElement('div');
  Object.assign(point.style, {{
    position: 'absolute', left: `calc(${{data.x / 10}}% - 9px)`, top: `calc(${{data.y / 10}}% - 9px)`,
    width: '18px', height: '18px', borderRadius: '50%',
    background: '#00ff66', border: '3px solid #fff', boxShadow: '0 0 0 2px #003d1c, 0 0 14px #00ff66'
  }});
  overlay.appendChild(point);
  document.body.appendChild(overlay);

  const panel = document.createElement('div');
  panel.id = 'google-ai-debug-panel';
  const chosen = data.objects.find((x) => x.key === data.selected);
  panel.innerHTML = `<div style="font-size:15px;font-weight:bold;color:#00ff66">Google AI · visão</div>` +
    `<div><b>Pergunta:</b> ${{data.question}}</div>` +
    `<div><b>Escolha:</b> ${{chosen?.label || data.selected || 'sem chave'}}</div>` +
    `<div><b>Ponto:</b> x=${{Math.round(data.x)}} y=${{Math.round(data.y)}} / 1000</div>` +
    `<div><b>Objetos:</b> ${{data.objects.length}} · <b>conf.:</b> ${{data.confidence == null ? '-' : Math.round(data.confidence * 100) + '%'}}</div>`;
  Object.assign(panel.style, {{
    position: 'fixed', right: '10px', top: '10px', zIndex: '2147483646',
    width: '330px', padding: '10px 12px', background: 'rgba(5,18,10,.94)',
    color: '#fff', border: '2px solid #00e85a', borderRadius: '8px',
    font: '13px/1.4 Arial, sans-serif', boxShadow: '0 4px 22px rgba(0,0,0,.55)',
    pointerEvents: 'none'
  }});
  document.body.appendChild(panel);
  return {{
    ok: true,
    canvas_rect: {{left: r.left, top: r.top, width: r.width, height: r.height}},
    top_cut: topCut,
    selectable_height: selectableHeight,
    clip: {{x: Math.max(0, r.left), y: Math.max(0, r.top), width: Math.max(1, r.width), height: Math.max(1, r.height), scale: 1}}
  }};
}})()
"""
        )
    finally:
        client.close()
    challenge_dir.mkdir(parents=True, exist_ok=True)
    (challenge_dir / "overlay-browser.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if isinstance(result, dict) and result.get("ok") and result.get("clip"):
        legacy.capture_challenge_png(
            port,
            challenge_dir / "navegador-caixas-verdes.png",
            result["clip"],
        )
    _save_browser_dom_debug(port, challenge_dir, "apos-overlay")
    return result if isinstance(result, dict) else None


def _remove_browser_debug_overlay(port: int) -> None:
    """Remove a instrumentacao antes do clique real no hCaptcha."""
    page = legacy.challenge_page(port)
    if not page:
        return
    client = legacy.CdpClient(page["webSocketDebuggerUrl"])
    try:
        client.eval(
            """
(() => {
  document.getElementById('google-ai-vision-overlay')?.remove();
  document.getElementById('google-ai-debug-panel')?.remove();
  return true;
})()
"""
        )
    finally:
        client.close()


def _provider_stats_snapshot() -> dict[str, Any]:
    with PROVIDER_STATS_LOCK:
        return dict(PROVIDER_STATS)


def _record_google_request_start() -> None:
    with PROVIDER_STATS_LOCK:
        PROVIDER_STATS["requests"] += 1


def _record_google_success(result: Any) -> None:
    with PROVIDER_STATS_LOCK:
        PROVIDER_STATS["successes"] += 1
        PROVIDER_STATS["last_error"] = None
        PROVIDER_STATS["last_success_at"] = datetime.now().isoformat(timespec="seconds")
        PROVIDER_STATS["last_http_requests"] = int(result.http_requests)
        PROVIDER_STATS["last_ai_queries"] = int(result.ai_queries)
        PROVIDER_STATS["last_source_count"] = len(result.sources)


def _record_google_failure(exc: Exception) -> None:
    with PROVIDER_STATS_LOCK:
        PROVIDER_STATS["failures"] += 1
        PROVIDER_STATS["last_error"] = f"{type(exc).__name__}: {exc}"[:1000]


def provider_circuit_state() -> dict:
    return legacy.provider_circuit_state()


def reset_provider_circuit() -> None:
    with legacy.PROVIDER_LOCK:
        legacy.PROVIDER_FAILURE_COUNT = 0
        legacy.PROVIDER_FAILURE_TOTAL = 0
        legacy.PROVIDER_CIRCUIT_OPEN = False
        legacy.PROVIDER_LAST_ERROR = None
    try:
        legacy.PROVIDER_ABORT_FILE.unlink()
    except FileNotFoundError:
        pass


def record_provider_failure(detail: str) -> dict:
    opened_now = False
    with legacy.PROVIDER_LOCK:
        legacy.PROVIDER_FAILURE_TOTAL += 1
        legacy.PROVIDER_FAILURE_COUNT += 1
        legacy.PROVIDER_LAST_ERROR = str(detail)[:1000]
        if (
            not legacy.PROVIDER_CIRCUIT_OPEN
            and legacy.PROVIDER_FAILURE_COUNT >= legacy.PROVIDER_FAILURE_LIMIT
        ):
            legacy.PROVIDER_CIRCUIT_OPEN = True
            opened_now = True
        state = {
            "open": legacy.PROVIDER_CIRCUIT_OPEN,
            "consecutive_failures": legacy.PROVIDER_FAILURE_COUNT,
            "total_failures": legacy.PROVIDER_FAILURE_TOTAL,
            "failure_limit": legacy.PROVIDER_FAILURE_LIMIT,
            "last_error": legacy.PROVIDER_LAST_ERROR,
        }
    if opened_now:
        payload = {
            **state,
            "opened_at": datetime.now().isoformat(timespec="seconds"),
            "provider": "google_ai_mode",
        }
        legacy.PROVIDER_ABORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        legacy.PROVIDER_ABORT_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f"[Google AI] Circuito aberto apos {state['consecutive_failures']} falhas consecutivas."
        )
    else:
        print(
            f"[Google AI] Falha {state['consecutive_failures']}/{state['failure_limit']}: {detail}"
        )
    return state


def record_provider_success() -> None:
    with legacy.PROVIDER_LOCK:
        if legacy.PROVIDER_CIRCUIT_OPEN:
            return
        legacy.PROVIDER_FAILURE_COUNT = 0
        legacy.PROVIDER_LAST_ERROR = None


legacy.reset_provider_circuit = reset_provider_circuit
legacy.record_provider_failure = record_provider_failure
legacy.record_provider_success = record_provider_success


def _json_candidates(text: str):
    stripped = text.strip().lstrip("\ufeff")
    if stripped:
        yield stripped
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.I | re.S):
        candidate = match.group(1).strip()
        if candidate:
            yield candidate
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        yield text[start : end + 1]


def _parse_json_answer(text: str) -> dict[str, Any]:
    errors: list[str] = []
    seen: set[str] = set()
    for candidate in _json_candidates(text):
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(f"linha {exc.lineno}, coluna {exc.colno}: {exc.msg}")
            continue
        if isinstance(value, dict):
            return value
    # O Modo IA ocasionalmente ecoa todo o prompt e so depois escreve o JSON.
    # Procure objetos balanceados e prefira o ultimo com o contrato esperado.
    decoder = json.JSONDecoder()
    decoded: list[dict[str, Any]] = []
    for position, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[position:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and (
            ("objetos" in value and "escolha" in value)
            or any(f"tile_{number}" in value for number in range(1, 10))
        ):
            decoded.append(value)
    if decoded:
        return decoded[-1]
    detail = "; ".join(errors[-3:]) if errors else "nenhum objeto JSON encontrado"
    raise ValueError(f"Resposta sem JSON valido: {detail}")


def _query_image(image_path: Path, prompt: str) -> Any:
    _record_google_request_start()
    # O cliente persiste uma sessao anonima compartilhada; serializar evita corrida
    # na gravacao atomica dos dois JSONs e reduz recusas por rajada.
    with PROVIDER_LOCK:
        try:
            result = google_ai.query_google_ai(
                prompt,
                timeout=90,
                image_path=image_path,
                attempts=3,
                allow_browser_recovery=True,
            )
        except Exception as exc:
            _record_google_failure(exc)
            raise
    _record_google_success(result)
    return result


def _nine_tile_prompt(captcha_question: str) -> str:
    return f"""
Analise SOMENTE os pixels da imagem anexada. Nao pesquise na web, nao consulte paginas,
nao use fontes externas e nao forneca links ou citacoes.

Esta imagem e um desafio visual com uma grade 3 x 3. A pergunta original e:
"{captcha_question}"

Numere os quadrados assim, da esquerda para a direita e de cima para baixo:
1 2 3
4 5 6
7 8 9

Decida literalmente quais quadrados atendem a pergunta original.
Regras:
- examine cada quadrado separadamente;
- objetos feitos por pessoas incluem construcoes, torres, postes, veiculos, placas,
  ferramentas, maquinas e outros objetos artificiais;
- natureza, animais, plantas, areia, agua, ceu e montanhas nao sao feitos por pessoas;
- um objeto parcial so conta quando ainda e claramente identificavel;
- nao marque por sombra, reflexo, texto de marca-dagua ou associacao vaga;
- em duvida real, use selecionar=false;
- nao use listas JSON nem colchetes, pois o produto pode remove-los;
- retorne somente JSON valido, sem Markdown e sem texto adicional.

Formato obrigatorio, mantendo exatamente as chaves tile_1 ate tile_9:
{{
  "tile_1": {{"descricao": "curta", "selecionar": false, "confianca": 0.0, "motivo": "curto"}},
  "tile_2": {{"descricao": "curta", "selecionar": false, "confianca": 0.0, "motivo": "curto"}},
  "tile_3": {{"descricao": "curta", "selecionar": false, "confianca": 0.0, "motivo": "curto"}},
  "tile_4": {{"descricao": "curta", "selecionar": false, "confianca": 0.0, "motivo": "curto"}},
  "tile_5": {{"descricao": "curta", "selecionar": false, "confianca": 0.0, "motivo": "curto"}},
  "tile_6": {{"descricao": "curta", "selecionar": false, "confianca": 0.0, "motivo": "curto"}},
  "tile_7": {{"descricao": "curta", "selecionar": false, "confianca": 0.0, "motivo": "curto"}},
  "tile_8": {{"descricao": "curta", "selecionar": false, "confianca": 0.0, "motivo": "curto"}},
  "tile_9": {{"descricao": "curta", "selecionar": false, "confianca": 0.0, "motivo": "curto"}},
  "resposta_direta": "2,5"
}}

Em resposta_direta, coloque somente os numeros selecionados em ordem crescente,
separados por virgula. Se nenhum corresponder, use string vazia.
""".strip()


def solve_with_google_ai(
    image_path: Path,
    captcha_question: str,
    challenge_dir: Path | None = None,
) -> list[int] | None:
    if not legacy.provider_request_allowed():
        state = legacy.provider_circuit_state()
        legacy.set_solver_error(
            "provider_circuit_open",
            f"Google AI bloqueado apos {state['consecutive_failures']} falhas consecutivas.",
        )
        return None
    if not image_path or not image_path.is_file():
        legacy.set_solver_error("google_ai_image_missing", str(image_path))
        return None

    raw_answer = ""
    try:
        result = _query_image(image_path, _nine_tile_prompt(captcha_question))
        raw_answer = result.answer
        parsed = _parse_json_answer(raw_answer)

        selected: list[int] = []
        for number in range(1, 10):
            tile = parsed.get(f"tile_{number}")
            if isinstance(tile, dict) and bool(tile.get("selecionar")):
                selected.append(number)

        direct = str(parsed.get("resposta_direta") or "")
        direct_numbers = legacy.parse_task_numbers(direct)
        if direct_numbers != selected:
            # Os nove booleanos sao a fonte principal; a string serve como redundancia.
            print(
                f"[Google AI] Divergencia booleans={selected} resposta_direta={direct_numbers}; usando booleans."
            )

        legacy.record_provider_success()
        if challenge_dir:
            challenge_dir.mkdir(parents=True, exist_ok=True)
            (challenge_dir / "resposta-google-ia.json").write_text(
                json.dumps(
                    {
                        "tipo": "9_tiles",
                        "pergunta": captcha_question,
                        "provedor": "google_ai_mode",
                        "modelo": PROVIDER_MODEL,
                        "indices": selected,
                        "resposta_parseada": parsed,
                        "resposta_bruta": raw_answer,
                        "metricas": {
                            "http_requests": result.http_requests,
                            "ai_queries": result.ai_queries,
                            "sources": len(result.sources),
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        print(
            f"[Google AI] Pergunta='{captcha_question}' indices={selected} "
            f"http={result.http_requests} consultas={result.ai_queries} fontes={len(result.sources)}"
        )
        return selected if selected else None
    except Exception as exc:
        state = legacy.record_provider_failure(str(exc))
        legacy.set_solver_error(
            "provider_circuit_open" if state["open"] else "google_ai_request_failed",
            str(exc),
        )
        print(f"[Google AI] Erro ao analisar 9 tiles: {type(exc).__name__}: {exc}")
        if challenge_dir:
            challenge_dir.mkdir(parents=True, exist_ok=True)
            (challenge_dir / "resposta-google-ia.json").write_text(
                json.dumps(
                    {
                        "tipo": "9_tiles",
                        "pergunta": captcha_question,
                        "provedor": "google_ai_mode",
                        "modelo": PROVIDER_MODEL,
                        "erro": type(exc).__name__,
                        "detalhe": str(exc),
                        "resposta_bruta": raw_answer,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return None


def _non_nine_prompt(
    captcha_question: str,
    image_width: int,
    image_height: int,
    frame_count: int = 1,
    reference_present: bool = False,
) -> str:
    sequence_note = ""
    if frame_count > 1:
        sequence_note = f"""
A imagem anexada e uma sequencia temporal de {frame_count} quadros da MESMA cena,
em ordem: superior esquerdo, superior direito, inferior esquerdo, inferior direito.
Compare a mudanca de posicao entre os quadros para descobrir direcao e velocidade.
Depois de decidir o destino, localize e marque esse alvo no QUARTO quadro
(inferior direito). As caixas e coordenadas devem usar a montagem anexada inteira
na escala 0..1000; o programa convertera o ponto para um quadro individual.
Nao escolha pelo alinhamento de um unico quadro: projete a trajetoria observada na sequencia.
"""
    reference_note = (
        "A parte superior contem uma referencia visual nao clicavel; compare-a com as opcoes inferiores."
        if reference_present
        else "A imagem anexada ja contem somente a area clicavel. Nao invente referencia fora dela."
    )
    return f"""
Analise SOMENTE os pixels da imagem anexada. Nao pesquise na web, nao consulte paginas,
nao use fontes externas e nao forneca links ou citacoes.

A imagem e a area limpa e clicavel de um desafio visual, sem grade artificial.
Pergunta original: "{captcha_question}"
{reference_note}
Tamanho da area inferior clicavel: {image_width} x {image_height} pixels.
{sequence_note}

Objetivo:
1. Identifique os objetos e destinos visiveis que ajudam a responder literalmente a pergunta.
2. Para cada objeto, forneca uma caixa delimitadora na escala normalizada de 0 a 1000:
   x=0 esquerda, x=1000 direita, y=0 topo, y=1000 base.
3. Determine exatamente UM ponto de clique que responde a pergunta original.
4. Em perguntas de trajetoria, como "onde a bola vai entrar", calcule a direcao pela
   mudanca entre quadros e escolha o centro do destino correto; nao clique na bola.
5. O ponto escolhido deve estar dentro da caixa do objeto/destino selecionado.
6. Nao use grade, celula, linha ou coluna. Nao divida a imagem em quadrados.
7. Nao invente objetos. Se houver ambiguidade, descreva-a e escolha o candidato visual mais forte.
8. Nao use arrays/listas JSON nem colchetes. Use objetos nomeados objeto_1, objeto_2 etc.
9. Retorne somente JSON valido, sem Markdown e sem texto adicional.
10. Quando a pergunta pedir o animal diferente, compare TODOS antes de escolher:
    especie, orientacao/espelhamento e pose. Para direcao, localize focinho/rosto e tronco:
    focinho a esquerda do tronco = olhando para a esquerda; focinho a direita = olhando para a direita.
    Registre essa comparacao no motivo de cada objeto, conte o padrao majoritario e escolha apenas a excecao.
    Revise explicitamente o candidato e a alternativa mais parecida antes de responder.

Formato obrigatorio:
{{
  "descricao_geral": "curta",
  "alvo_da_pergunta": "o que deve ser clicado",
  "objetos": {{
    "objeto_1": {{
      "nome": "gol esquerdo",
      "caixa": {{"x1": 80, "y1": 430, "x2": 310, "y2": 920}},
      "corresponde_pergunta": false,
      "confianca": 0.90,
      "motivo": "curto"
    }},
    "objeto_2": {{
      "nome": "gol direito",
      "caixa": {{"x1": 690, "y1": 420, "x2": 940, "y2": 930}},
      "corresponde_pergunta": true,
      "confianca": 0.95,
      "motivo": "a trajetoria termina neste gol"
    }}
  }},
  "escolha": {{
    "objeto": "objeto_2",
    "x": 815,
    "y": 675,
    "descricao_do_alvo": "gol direito",
    "argumento": "curto",
    "confianca": 0.95
  }},
  "observacoes": "curta"
}}
""".strip()


def _top_reference_has_visual_content(full_path: Path, top_cut: int) -> bool:
    if top_cut <= 0:
        return False
    try:
        with legacy.Image.open(full_path) as source:
            cut = min(int(top_cut), source.height)
            if cut <= 4:
                return False
            # A borda inferior da faixa reservada pode conter alguns pixels da
            # area clicavel por arredondamento CSS/native. Ignore os 20% finais
            # para nao confundir esse vazamento com uma referencia.
            probe_cut = max(4, int(cut * 0.8))
            top = source.convert("L").crop((0, 0, source.width, probe_cut))
            histogram = top.histogram()
            total = max(1, sum(histogram))
            dark = sum(histogram[:18]) / total
            bright = sum(histogram[238:]) / total
            return top.entropy() >= 1.2 and dark < 0.985 and bright < 0.985
    except Exception:
        return False


def _float_0_1000(value: Any, field: str) -> float:
    number = float(value)
    if not (0.0 <= number <= 1000.0):
        raise ValueError(f"{field} fora de 0..1000: {number}")
    return number


def _parse_non9_objects(parsed: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    raw_objects = parsed.get("objetos")
    if not isinstance(raw_objects, dict):
        raise ValueError("Campo objetos ausente ou invalido")

    detections: list[Any] = []
    by_key: dict[str, Any] = {}
    for key, raw in raw_objects.items():
        if not isinstance(raw, dict):
            continue
        box = raw.get("caixa")
        if not isinstance(box, dict):
            continue
        name = str(raw.get("nome") or key).strip()[:80]
        try:
            detection = detector.Detection(
                label=name,
                x1=_float_0_1000(box.get("x1"), f"{key}.x1"),
                y1=_float_0_1000(box.get("y1"), f"{key}.y1"),
                x2=_float_0_1000(box.get("x2"), f"{key}.x2"),
                y2=_float_0_1000(box.get("y2"), f"{key}.y2"),
                confidence=detector._parse_confidence(raw.get("confianca")),
                note=str(raw.get("motivo") or "").strip()[:240] or None,
            ).normalized()
        except Exception:
            continue
        if detection.x2 - detection.x1 < 2 or detection.y2 - detection.y1 < 2:
            continue
        detections.append(detection)
        by_key[str(key)] = detection
    if not detections:
        raise ValueError("Nenhuma caixa de objeto valida retornada")
    return detections, by_key


def _sequence_coordinates_to_frame(
    parsed: dict[str, Any],
    frame_width: int,
    frame_height: int,
    montage_width: int,
    montage_height: int,
) -> dict[str, Any]:
    """Converte caixas 0..1000 da montagem 2x2 para um quadro individual."""
    converted = json.loads(json.dumps(parsed, ensure_ascii=False))
    objects = converted.get("objetos")
    if not isinstance(objects, dict):
        return converted
    gap_x = max(0, montage_width - frame_width * 2)
    gap_y = max(0, montage_height - frame_height * 2)

    def axis(values: list[Any], frame_size: int, montage_size: int, gap: int) -> list[float]:
        nums = [float(value) for value in values]
        center_px = (sum(nums) / len(nums)) / 1000.0 * montage_size
        second = center_px >= frame_size + gap / 2.0
        offset = frame_size + gap if second else 0.0
        return [min(1000.0, max(0.0, ((value / 1000.0 * montage_size) - offset) / frame_size * 1000.0)) for value in nums]

    selected_key = str((converted.get("escolha") or {}).get("objeto") or "")
    selected_box = None
    for key, raw in objects.items():
        if not isinstance(raw, dict) or not isinstance(raw.get("caixa"), dict):
            continue
        box = raw["caixa"]
        try:
            x1, x2 = axis([box.get("x1"), box.get("x2")], frame_width, montage_width, gap_x)
            y1, y2 = axis([box.get("y1"), box.get("y2")], frame_height, montage_height, gap_y)
        except (TypeError, ValueError):
            continue
        box.update({"x1": x1, "x2": x2, "y1": y1, "y2": y2})
        if str(key) == selected_key:
            selected_box = (x1, y1, x2, y2)
    choice = converted.get("escolha")
    if isinstance(choice, dict):
        if selected_box:
            choice["x"] = (selected_box[0] + selected_box[2]) / 2.0
            choice["y"] = (selected_box[1] + selected_box[3]) / 2.0
        else:
            try:
                choice["x"] = axis([choice.get("x")], frame_width, montage_width, gap_x)[0]
                choice["y"] = axis([choice.get("y")], frame_height, montage_height, gap_y)[0]
            except (TypeError, ValueError):
                pass
    return converted


def _full_coordinates_to_selectable(
    parsed: dict[str, Any],
    full_height: int,
    top_cut: int,
) -> dict[str, Any]:
    """Converte Y normalizado do canvas completo para a area inferior clicavel."""
    converted = json.loads(json.dumps(parsed, ensure_ascii=False))
    selectable_height = max(1, full_height - top_cut)

    def y_value(value: Any) -> float:
        pixel = float(value) / 1000.0 * full_height
        return min(1000.0, max(0.0, (pixel - top_cut) / selectable_height * 1000.0))

    objects = converted.get("objetos")
    if isinstance(objects, dict):
        for raw in objects.values():
            if not isinstance(raw, dict) or not isinstance(raw.get("caixa"), dict):
                continue
            box = raw["caixa"]
            try:
                box["y1"] = y_value(box.get("y1"))
                box["y2"] = y_value(box.get("y2"))
            except (TypeError, ValueError):
                continue
    choice = converted.get("escolha")
    if isinstance(choice, dict):
        try:
            choice["y"] = y_value(choice.get("y"))
        except (TypeError, ValueError):
            pass
    return converted


def _motion_centers(challenge_dir: Path) -> list[tuple[float, float]]:
    """Rastreia o maior objeto movel sem tentar classificar o tipo do desafio."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return []
    paths = sorted(challenge_dir.glob("quadro-[0-9][0-9].jpg"))
    if not paths:
        paths = sorted(challenge_dir.glob("quadro-[0-9][0-9].png"))
    images = [cv2.imread(str(path)) for path in paths]
    images = [image for image in images if image is not None]
    if len(images) < 3 or any(image.shape != images[0].shape for image in images):
        return []
    median = np.median(np.stack(images), axis=0).astype(np.uint8)
    kernel = np.ones((9, 9), np.uint8)
    centers: list[tuple[float, float]] = []
    height, width = images[0].shape[:2]
    for image in images:
        delta = cv2.absdiff(image, median)
        gray = cv2.cvtColor(delta, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, 25, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = [contour for contour in contours if cv2.contourArea(contour) >= 80]
        if not contours:
            return []
        x, y, box_width, box_height = cv2.boundingRect(max(contours, key=cv2.contourArea))
        if box_width * box_height > width * height * 0.45:
            return []
        centers.append(
            (
                (x + box_width / 2.0) / width * 1000.0,
                (y + box_height / 2.0) / height * 1000.0,
            )
        )
    return centers


def _override_choice_from_motion(
    parsed: dict[str, Any],
    challenge_dir: Path,
) -> dict[str, Any] | None:
    centers = _motion_centers(challenge_dir)
    if len(centers) < 3:
        return None
    step_sizes = [
        ((centers[index][0] - centers[index - 1][0]) ** 2 + (centers[index][1] - centers[index - 1][1]) ** 2) ** 0.5
        for index in range(1, len(centers))
    ]
    typical_step = sorted(step_sizes)[len(step_sizes) // 2]
    jump_limit = max(35.0, typical_step * 3.0)
    segments: list[list[tuple[float, float]]] = [[]]
    for index, center in enumerate(centers):
        if index and step_sizes[index - 1] > jump_limit:
            segments.append([])
        segments[-1].append(center)
    continuous = max(segments, key=len)
    if len(continuous) >= 3:
        centers_used = continuous
    else:
        centers_used = centers
    first_x, first_y = centers_used[0]
    last_x, last_y = centers_used[-1]
    velocity_x = last_x - first_x
    velocity_y = last_y - first_y
    velocity_squared = velocity_x * velocity_x + velocity_y * velocity_y
    if velocity_squared < 20.0:
        return {
            "applied": False,
            "reason": "movimento_insuficiente",
            "centers": centers,
            "centers_used": centers_used,
        }

    objects = parsed.get("objetos")
    if not isinstance(objects, dict):
        return None
    candidates = []
    moving_words = ("bola", "ball", "futebol", "objeto movel", "objeto móvel")
    for key, raw in objects.items():
        if not isinstance(raw, dict) or not isinstance(raw.get("caixa"), dict):
            continue
        name = str(raw.get("nome") or key).casefold()
        if any(word in name for word in moving_words):
            continue
        box = raw["caixa"]
        try:
            target_x = (float(box["x1"]) + float(box["x2"])) / 2.0
            target_y = (float(box["y1"]) + float(box["y2"])) / 2.0
        except (KeyError, TypeError, ValueError):
            continue
        delta_x = target_x - last_x
        delta_y = target_y - last_y
        forward = (delta_x * velocity_x + delta_y * velocity_y) / velocity_squared
        perpendicular = abs(delta_x * velocity_y - delta_y * velocity_x) / (velocity_squared ** 0.5)
        score = perpendicular + (1200.0 if forward <= 0 else 0.0)
        candidates.append(
            {
                "key": str(key),
                "name": str(raw.get("nome") or key),
                "x": target_x,
                "y": target_y,
                "forward": forward,
                "perpendicular": perpendicular,
                "score": score,
            }
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: item["score"])
    best = candidates[0]
    margin = candidates[1]["score"] - best["score"] if len(candidates) > 1 else 999.0
    if best["forward"] <= 0 or margin < 45.0:
        return {
            "applied": False,
            "reason": "trajetoria_ambigua",
            "centers": centers,
            "centers_used": centers_used,
            "gif_loop_jump_limit": jump_limit,
            "candidates": candidates,
            "margin": margin,
        }
    choice = parsed.get("escolha")
    if not isinstance(choice, dict):
        choice = {}
        parsed["escolha"] = choice
    previous = choice.get("objeto")
    choice.update(
        {
            "objeto": best["key"],
            "x": best["x"],
            "y": best["y"],
            "descricao_do_alvo": best["name"],
            "argumento": "trajetoria medida nos quadros contiguos do canvas",
            "confianca": max(0.75, min(0.99, 0.75 + margin / 1000.0)),
        }
    )
    return {
        "applied": True,
        "previous_choice": previous,
        "selected": best["key"],
        "centers": centers,
        "centers_used": centers_used,
        "gif_loop_jump_limit": jump_limit,
        "candidates": candidates,
        "margin": margin,
    }


def analyze_non_9_with_google_ai(
    challenge_dir: Path,
    captcha_question: str,
    port: int | None = None,
) -> dict | None:
    # A IA recebe o canvas completo para enxergar a referencia superior. As
    # coordenadas sao convertidas depois para a area inferior realmente clicavel.
    image_path = challenge_dir / "desafio.png"
    if not image_path.is_file():
        return None
    full_path = challenge_dir / "desafio-completo.jpg"
    if not full_path.is_file():
        full_path = challenge_dir / "desafio-completo.png"
    if not full_path.is_file():
        full_path = image_path
    sequence_path = challenge_dir / "sequencia-temporal.jpg"
    if not sequence_path.is_file():
        sequence_path = challenge_dir / "sequencia-temporal.png"
    tracked_centers = _motion_centers(challenge_dir) if sequence_path.is_file() else []
    displacement = 0.0
    if len(tracked_centers) >= 3:
        displacement = (
            (tracked_centers[-1][0] - tracked_centers[0][0]) ** 2
            + (tracked_centers[-1][1] - tracked_centers[0][1]) ** 2
        ) ** 0.5
    moving_sequence = sequence_path.is_file() and displacement >= 4.5
    query_path = sequence_path if moving_sequence else full_path
    if not legacy.provider_request_allowed():
        state = legacy.provider_circuit_state()
        legacy.set_solver_error(
            "provider_circuit_open",
            f"Google AI bloqueado apos {state['consecutive_failures']} falhas consecutivas.",
        )
        return None

    raw_answer = ""
    try:
        with legacy.Image.open(image_path) as image:
            width, height = image.size
        with legacy.Image.open(full_path) as full_image:
            full_width, full_height = full_image.size
        top_cut = max(0, full_height - height)
        try:
            canvas_info = json.loads((challenge_dir / "canvas-info.json").read_text(encoding="utf-8"))
            top_cut = int(canvas_info.get("top_cut_native_px") or top_cut)
        except Exception:
            pass
        reference_present = _top_reference_has_visual_content(full_path, top_cut)
        if not moving_sequence:
            query_path = full_path if reference_present else image_path
        frame_count = len(list(challenge_dir.glob("quadro-[0-9][0-9].jpg"))) if query_path == sequence_path else 1
        if frame_count == 0 and query_path == sequence_path:
            frame_count = len(list(challenge_dir.glob("quadro-[0-9][0-9].png")))
        with legacy.Image.open(query_path) as query_image:
            query_width, query_height = query_image.size
        result = _query_image(
            query_path,
            _non_nine_prompt(
                captcha_question,
                width,
                height,
                frame_count=frame_count,
                reference_present=reference_present,
            ),
        )
        raw_answer = result.answer
        parsed = _parse_json_answer(raw_answer)
        parsed_original = parsed
        if frame_count > 1:
            parsed = _sequence_coordinates_to_frame(
                parsed,
                full_width,
                full_height,
                query_width,
                query_height,
            )
        if query_path == full_path:
            parsed = _full_coordinates_to_selectable(parsed, full_height, top_cut)
        motion_override = _override_choice_from_motion(parsed, challenge_dir) if frame_count > 1 else None
        detections, by_key = _parse_non9_objects(parsed)

        choice = parsed.get("escolha")
        if not isinstance(choice, dict):
            raise ValueError("Campo escolha ausente ou invalido")
        selected_key = str(choice.get("objeto") or "").strip()
        selected = by_key.get(selected_key)

        x = _float_0_1000(choice.get("x"), "escolha.x")
        y = _float_0_1000(choice.get("y"), "escolha.y")
        corrected = False
        if selected is not None:
            inside = selected.x1 <= x <= selected.x2 and selected.y1 <= y <= selected.y2
            if not inside:
                x = (selected.x1 + selected.x2) / 2.0
                y = (selected.y1 + selected.y2) / 2.0
                corrected = True

        output_image = challenge_dir / "desafio-anotado-google-ia.png"
        detector.draw_detections(
            image_path,
            output_image,
            detections,
            show_confidence=True,
        )

        click_choice = {
            "x_percent_na_imagem": x / 10.0,
            "y_percent_na_imagem": y / 10.0,
            "x_normalizado": x,
            "y_normalizado": y,
            "objeto": selected_key or None,
            "descricao_do_alvo": str(choice.get("descricao_do_alvo") or "").strip(),
            "argumento": str(choice.get("argumento") or "").strip(),
            "confianca": detector._parse_confidence(choice.get("confianca")),
            "ponto_corrigido_para_centro_da_caixa": corrected,
        }
        live_relocation = None
        if port is not None:
            live_relocation = _relocate_choice_on_live_canvas(
                port,
                challenge_dir,
                captcha_question,
                selected,
                click_choice,
            )
            (challenge_dir / "validacao-antes-clique.json").write_text(
                json.dumps(live_relocation, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if not live_relocation.get("safe"):
                legacy.set_solver_error(
                    "visual_state_changed",
                    str(live_relocation.get("reason") or "estado visual mudou antes do clique"),
                )
                print(
                    "[Google AI] Estado mudou durante a resposta; descartando coordenada antiga "
                    f"({live_relocation.get('reason')})."
                )
                return None
        overlay_result = None
        if port is not None:
            overlay_result = _inject_green_browser_overlay(
                port,
                by_key,
                click_choice,
                challenge_dir,
                captcha_question,
            )

        legacy.record_provider_success()
        (challenge_dir / "resposta-google-ia.json").write_text(
            json.dumps(
                {
                    "tipo": "nao_9_tiles_objetos_xy",
                    "pergunta": captcha_question,
                    "provedor": "google_ai_mode",
                    "modelo": PROVIDER_MODEL,
                    "imagem_analisada": str(query_path),
                    "imagem_base_coordenadas": str(image_path),
                    "quadros_temporais": frame_count,
                    "evidencia_visual": "sequencia_temporal" if moving_sequence else "quadro_unico",
                    "deslocamento_medido": displacement,
                    "trajetoria_local": motion_override,
                    "validacao_antes_clique": live_relocation,
                    "imagem_anotada": str(output_image),
                    "resposta_parseada": parsed,
                    "resposta_parseada_antes_conversao_montagem": parsed_original,
                    "escolha_convertida": click_choice,
                    "overlay_navegador": overlay_result,
                    "objetos_parseados": [
                        {
                            "nome": item.label,
                            "x1": item.x1,
                            "y1": item.y1,
                            "x2": item.x2,
                            "y2": item.y2,
                            "confianca": item.confidence,
                        }
                        for item in detections
                    ],
                    "resposta_bruta": raw_answer,
                    "metricas": {
                        "http_requests": result.http_requests,
                        "ai_queries": result.ai_queries,
                        "sources": len(result.sources),
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if port is not None:
            _remove_browser_debug_overlay(port)
        print(
            f"[Google AI] Nao-9 objetos={len(detections)} escolha={click_choice} "
            f"http={result.http_requests} consultas={result.ai_queries} fontes={len(result.sources)}"
        )
        return click_choice
    except Exception as exc:
        state = legacy.record_provider_failure(str(exc))
        legacy.set_solver_error(
            "provider_circuit_open" if state["open"] else "google_ai_request_failed",
            str(exc),
        )
        (challenge_dir / "resposta-google-ia.json").write_text(
            json.dumps(
                {
                    "tipo": "nao_9_tiles_objetos_xy",
                    "pergunta": captcha_question,
                    "provedor": "google_ai_mode",
                    "modelo": PROVIDER_MODEL,
                    "erro": type(exc).__name__,
                    "detalhe": str(exc),
                    "resposta_bruta": raw_answer,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"[Google AI] Erro no nao-9 por objetos/XY: {type(exc).__name__}: {exc}")
        return None


def _save_and_analyze_non_9_fast(
    port: int,
    state: dict,
    request_id: str,
    attempt: int,
) -> dict | None:
    if not state:
        return None
    if state.get("checkmark") and not state.get("imageCanvas"):
        return None
    task_count = len(state.get("tasks") or [])
    if not (state.get("imageCanvas") or (task_count and task_count != 9)):
        return None

    prompt_text = str(state.get("prompt") or "")
    folder = legacy.create_challenge_debug_folder(
        request_id,
        attempt,
        prompt_text,
        legacy.CHALLENGES_NON_9_DIR,
    )
    (folder / "estado-dom.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _save_browser_dom_debug(port, folder, "antes-captura")

    captured = False
    frame_paths: list[Path] = []
    capture_started = time.perf_counter()
    for _ in range(2):
        frame_paths = _capture_non_9_canvas_sequence(port, folder)
        if frame_paths:
            captured = True
            break
        time.sleep(0.10)
    if captured:
        full_frame_paths = [
            path.with_name(path.stem + "-completo.jpg")
            for path in frame_paths
        ]
        _build_motion_sequence(folder, full_frame_paths)
    capture_seconds = time.perf_counter() - capture_started
    (folder / "timing.json").write_text(
        json.dumps(
            {
                "capture_seconds": round(capture_seconds, 4),
                "captured": captured,
                "temporal_frames": len(frame_paths),
                "sequence_created": any(
                    (folder / name).is_file()
                    for name in ("sequencia-temporal.jpg", "sequencia-temporal.png")
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if not captured:
        (folder / "canvas-erro.txt").write_text(
            "Canvas limpo nao ficou pronto no prazo curto; a tentativa sera refeita.\n",
            encoding="utf-8",
        )
        return None
    _save_browser_dom_debug(port, folder, "capturado")
    print(f"[Debug] Desafio nao-9 limpo salvo em {capture_seconds:.2f}s: {folder}")
    return analyze_non_9_with_google_ai(folder, prompt_text, port=port)


def _challenge_wait_state(port: int) -> dict[str, Any] | None:
    page = legacy.challenge_page(port)
    if not page:
        return None
    try:
        client = legacy.CdpClient(page["webSocketDebuggerUrl"])
        try:
            return client.eval(
                """
(() => {
  const button =
    document.querySelector('.button-submit.button') ||
    document.querySelector('[aria-label="Verificar respostas"]');
  const canvas = [...document.querySelectorAll('canvas[role="img"]')].find((el) =>
    (el.getAttribute('aria-label') || '').includes('Desafio de CAPTCHA baseado em imagem'));
  const tasks = [...document.querySelectorAll('.task[role="button"], .task')];
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || '1') > 0.05 && rect.width > 1 && rect.height > 1;
  };
  const loading = visible(document.querySelector('.button-submit-spinner')) ||
    visible(document.querySelector('.loading-indicator')) ||
    visible(document.querySelector('.spinner:not(.spinner-icon)'));
  const disabled = button ? (
    !!button.disabled ||
    button.getAttribute('aria-disabled') === 'true' ||
    button.classList.contains('button-disabled')
  ) : null;
  return {challenge_ready: !!canvas || tasks.length > 0, submit_disabled: disabled, loading};
})()
"""
            )
        finally:
            client.close()
    except Exception:
        return None


_legacy_click_submit = legacy.click_hcaptcha_submit


def _click_submit_when_ready_google_ai(port: int) -> bool:
    """Nunca confirma enquanto o frame ainda mostra spinner/transicao."""
    end = time.time() + 2.5
    ready_polls = 0
    while time.time() < end:
        state = _challenge_wait_state(port)
        if state and not state.get("loading") and state.get("submit_disabled") is False:
            ready_polls += 1
            if ready_polls >= 2:
                return _legacy_click_submit(port)
        else:
            ready_polls = 0
        time.sleep(0.10)
    print("[Auto] Verificar nao ficou pronto; nao vou clicar fora de hora.")
    return False


def _wait_token_or_next_stage_google_ai(
    port: int,
    timeout: float = 15.0,
    browser_proc: Any = None,
) -> str | None:
    """Sai cedo quando o hCaptcha ja abriu a proxima etapa visual."""
    print("[Auto] Aguardando token ou proxima etapa...")
    started = time.time()
    end = started + timeout
    saw_checkmark = False
    retry_since = None
    retry_logged = False
    next_stage_polls = 0
    while time.time() < end:
        if not legacy.solver_browser_alive(port, browser_proc):
            legacy.set_solver_error(
                "browser_closed",
                "Navegador do solve foi fechado ou deixou de responder.",
            )
            print("[Auto] Navegador deixou de responder; esta janela sera reaberta.")
            return None
        token = legacy.extract_token_from_page(port)
        if token:
            print(f"[Auto] Token obtido com sucesso! ({len(token)} chars)")
            return token
        if legacy.captcha_checkmark_visible(port):
            if not saw_checkmark:
                print("[Auto] Marca de verificacao apareceu; aguardando callback do token.")
                end = max(end, time.time() + legacy.CHECKMARK_TOKEN_EXTENSION_SECONDS)
            saw_checkmark = True
            retry_since = None
            next_stage_polls = 0
        elif legacy.captcha_retry_error_visible(port):
            next_stage_polls = 0
            if retry_since is None:
                retry_since = time.time()
            if not retry_logged:
                print("[Auto] Rejeicao visivel; confirmando pelo DOM.")
                retry_logged = True
            if time.time() - retry_since >= legacy.TOKEN_RETRY_ABORT_SECONDS:
                print("[Auto] Rejeicao confirmada; avancando sem espera ociosa.")
                return None
        else:
            retry_since = None
            retry_logged = False
            stage = _challenge_wait_state(port)
            if (
                time.time() - started >= 0.75
                and stage
                and stage.get("challenge_ready")
                and stage.get("submit_disabled") is True
                and not stage.get("loading")
            ):
                next_stage_polls += 1
                if next_stage_polls >= 2:
                    print("[Auto] Proxima etapa visual pronta; voltando imediatamente para analisar.")
                    return None
            else:
                next_stage_polls = 0
        time.sleep(legacy.TOKEN_POLL_SECONDS)
    return None


legacy.solve_with_legacy_provider = solve_with_google_ai
legacy.analyze_non_9_with_legacy_provider = analyze_non_9_with_google_ai
legacy.save_and_analyze_non_9_challenge = _save_and_analyze_non_9_fast
legacy.click_hcaptcha_submit = _click_submit_when_ready_google_ai
legacy.wait_token_or_retry_after_submit = _wait_token_or_next_stage_google_ai


def google_ai_health() -> dict[str, Any]:
    recovery_state = None
    recovery_file = GOOGLE_AI_STATE_DIR / "session_recovery_state.json"
    try:
        recovery_state = json.loads(recovery_file.read_text(encoding="utf-8"))
    except Exception:
        recovery_state = None
    return {
        "configured": GOOGLE_AI_CLIENT_PATH.is_file(),
        "project": str(GOOGLE_AI_PROJECT),
        "state_dir": str(GOOGLE_AI_STATE_DIR),
        "model": PROVIDER_MODEL,
        "route": "server_proxy" if os.environ.get("PRUMO_MODAL_PROXY_HOSTNAME", "").strip() else "direct",
        "serialized_provider_requests": True,
        "browser_recovery_last_state": recovery_state,
        "stats": _provider_stats_snapshot(),
    }


class GoogleSolverRequestHandler(legacy.SolverRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        with legacy.ACTIVE_LOCK:
            active = legacy.ACTIVE_SOLVERS
        payload = {
            "status": "ok",
            "service": "hcaptcha-solver",
            "version": legacy.SOLVER_API_VERSION,
            "max_browsers": legacy.MAX_BROWSERS,
            "active_browsers": active,
            "provider": "google_ai_mode",
            "provider_state": legacy.provider_circuit_state(),
            "google_ai": google_ai_health(),
            "solver_state": legacy.solver_circuit_state(),
            "max_solve_seconds": legacy.MAX_SOLVE_SECONDS,
            "fatal_circuit": legacy.fatal_circuit_state(),
        }
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


legacy.SolverRequestHandler = GoogleSolverRequestHandler


def main() -> None:
    # Mesmo que alguem reutilize um comando antigo, esta API nao aceita o modo que
    # recarrega automaticamente desafios fora de 9 tiles. Ela tenta resolve-los.
    sys.argv = [arg for arg in sys.argv if arg != "--recarregar-nao-9"]
    legacy.FAST_RELOAD_NON_9 = False
    print("=" * 60)
    print("API resolvedora alternativa: Google Modo IA")
    print(f"Nucleo reutilizado: {LEGACY_SOLVER_PATH.name}")
    print(f"Cliente multimodal: {GOOGLE_AI_CLIENT_PATH}")
    print("Politica: nao pular prompts; resolver 9 tiles e desafios nao-9.")
    print("Provedor visual ativo: somente Google Modo IA.")
    print("As chamadas visuais sao serializadas para proteger a sessao anonima.")
    print("=" * 60)
    legacy.main()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRO: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
