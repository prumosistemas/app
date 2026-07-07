#!/usr/bin/env python3

import asyncio
import logging
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Tuple
from urllib.parse import unquote, urlsplit

from playwright.async_api import TimeoutError as PWTimeoutError  # type: ignore
from playwright.async_api import Page, async_playwright  # type: ignore

try:
    from playwright.async_api import Error as PlaywrightError  # type: ignore
except Exception:  # pragma: no cover
    PlaywrightError = Exception  # type: ignore

try:
    from playwright._impl._errors import TargetClosedError  # type: ignore
except Exception:  # pragma: no cover
    class TargetClosedError(PlaywrightError):  # type: ignore
        pass

from flow_errors import FlowError, MensagemTelaError, PortalAccessBlockedError
from portal_bootstrap import bootstrap_portal_requests

logger = logging.getLogger("iss")

# Injetado pelo main.py
BASE_DIR = ""
_BROWSER_POOL_ENV_KEY: tuple[str, str] | None = None
_BROWSER_POOL: list[tuple[str, str]] = []
_BROWSER_POOL_CURSOR = 0
_BROWSER_PROXY_ENV_KEY: str | None = None
_BROWSER_PROXY_MAP: dict[str, dict[str, str]] = {}
_BROWSER_LABEL_ACTIVE: dict[str, int] = {}
_BROWSER_LABEL_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class FlowConfig:
    run_id: str
    run_dir: str
    run_log_file: str
    cnpj_dir: str
    step_timeout_sec: int
    nav_timeout_ms: int
    selector_timeout_ms: int
    close_timeout_sec: int
    goto_retries: int
    headless: bool


@dataclass
class FlowContext:
    flow: str
    cnpj: str
    mes: str
    config: FlowConfig
    step: str = ""
    empresa: str = ""


@dataclass(frozen=True)
class BootstrapCompany:
    empresa: str
    cid: str
    pasta: str


def somente_digitos(s: str) -> str:
    return re.sub(r"\D+", "", s or "")


def sanitize_folder_name(name: str, max_len: int = 120) -> str:
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ")
    if not name:
        name = "SEM_NOME"

    reserved = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
    if name.upper() in reserved:
        name = f"_{name}"

    return name[:max_len].strip()


def ensure_dir(path: str) -> None:
    if path:
        Path(path).mkdir(parents=True, exist_ok=True)


async def save_download_checked(download: Any, path: str, ctx: FlowContext, *, expect_pdf: bool = False) -> None:
    """
    Salva download Playwright.
    A validação de conteúdo foi mantida desativada por regra operacional.
    """
    ensure_dir(os.path.dirname(path))

    try:
        failure = await download.failure()
    except Exception:
        failure = None

    if failure:
        raise FlowError(
            "DOWNLOAD_FAILED",
            f"Falha no download: {failure}",
            short_message="O portal iniciou o download, mas o arquivo falhou.",
            action="Tentar novamente e verificar sessão/portal.",
            retryable=True,
        )

    await download.save_as(path)
    await asyncio.sleep(0.25)

    if not os.path.exists(path):
        raise FlowError(
            "DOWNLOAD_NOT_SAVED",
            f"Arquivo não foi salvo: {path}",
            short_message="Download não foi salvo no disco.",
            action="Verificar permissões e comportamento do navegador remoto.",
            retryable=True,
        )


async def append_log(line: str, log_file: str) -> None:
    def _write() -> None:
        ensure_dir(os.path.dirname(log_file))
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    await asyncio.to_thread(_write)


def _format(ctx: FlowContext, event: str, msg: str, code: str = "") -> str:
    parts = [
        f"[{event}]",
        f"run={ctx.config.run_id}",
        f"flow={ctx.flow}",
        f"cnpj={ctx.cnpj}",
        f"mes={ctx.mes}",
    ]
    if ctx.empresa:
        parts.append(f"empresa={ctx.empresa}")
    if ctx.step:
        parts.append(f"step={ctx.step}")
    if code:
        parts.append(f"code={code}")
    return " ".join(parts) + " :: " + msg


async def log_flow(
    ctx: FlowContext,
    msg: str,
    *,
    event: str = "EVENT",
    level: int = logging.INFO,
    code: str = "",
) -> None:
    line = _format(ctx, event, msg, code=code)
    logger.log(level, line)
    try:
        await append_log(line, ctx.config.run_log_file)
    except Exception:
        pass


def mark_step(ctx: FlowContext, step: str) -> None:
    ctx.step = step


def rename_cnpj_dir_with_company(run_dir: str, cnpj: str, empresa: str) -> str:
    cnpj_norm = somente_digitos(cnpj).zfill(14)
    nome_limpo = sanitize_folder_name(empresa)
    destino = os.path.join(run_dir, f"{cnpj_norm} - {nome_limpo}")
    origem = os.path.join(run_dir, cnpj_norm)

    if os.path.abspath(origem) == os.path.abspath(destino):
        ensure_dir(destino)
        return destino

    ensure_dir(run_dir)

    if os.path.exists(origem):
        if not os.path.exists(destino):
            os.rename(origem, destino)
            return destino

        idx = 2
        while True:
            alt = os.path.join(run_dir, f"{cnpj_norm} - {nome_limpo} ({idx})")
            if not os.path.exists(alt):
                os.rename(origem, alt)
                return alt
            idx += 1

    ensure_dir(destino)
    return destino


def _parse_browser_cdp_pool() -> list[tuple[str, str]]:
    raw_pool = os.getenv("BROWSER_CDP_POOL", "").strip()
    fallback_url = os.getenv("BROWSER_CDP_URL", "").strip()
    env_key = (raw_pool, fallback_url)

    global _BROWSER_POOL_ENV_KEY, _BROWSER_POOL
    if _BROWSER_POOL_ENV_KEY == env_key:
        return _BROWSER_POOL

    pool: list[tuple[str, str]] = []
    if raw_pool:
        for idx, raw_entry in enumerate(raw_pool.split(";;"), start=1):
            entry = raw_entry.strip()
            if not entry:
                continue

            parts = [part.strip() for part in entry.split("|", 2)]
            if len(parts) == 3:
                label, capacity_raw, url = parts
            elif len(parts) == 2:
                label, capacity_raw, url = f"browser-{idx}", parts[0], parts[1]
            else:
                label, capacity_raw, url = f"browser-{idx}", "1", parts[0]

            if not url:
                continue

            try:
                capacity = max(1, int(capacity_raw))
            except Exception:
                capacity = 1

            pool.extend((label or f"browser-{idx}", url) for _ in range(capacity))
    elif fallback_url:
        pool.append(("browserless", fallback_url))

    _BROWSER_POOL_ENV_KEY = env_key
    _BROWSER_POOL = pool
    return _BROWSER_POOL


def _next_browser_cdp_target() -> tuple[str, str]:
    pool = _parse_browser_cdp_pool()
    if not pool:
        raise RuntimeError("BROWSER_CDP_URL ou BROWSER_CDP_POOL nao configurado.")

    global _BROWSER_POOL_CURSOR
    target = pool[_BROWSER_POOL_CURSOR % len(pool)]
    _BROWSER_POOL_CURSOR += 1
    return target


async def _register_browser_label(label: str) -> int:
    async with _BROWSER_LABEL_LOCK:
        active = _BROWSER_LABEL_ACTIVE.get(label, 0) + 1
        _BROWSER_LABEL_ACTIVE[label] = active
        return active


async def _unregister_browser_label(label: str) -> None:
    async with _BROWSER_LABEL_LOCK:
        active = max(0, _BROWSER_LABEL_ACTIVE.get(label, 0) - 1)
        if active:
            _BROWSER_LABEL_ACTIVE[label] = active
        else:
            _BROWSER_LABEL_ACTIVE.pop(label, None)


def _browser_stagger_ms(label: str) -> int:
    env_label = _proxy_env_label(label)
    raw = os.getenv(f"BROWSER_CONNECT_STAGGER_MS_{env_label}", "").strip()
    if not raw:
        raw = os.getenv("BROWSER_CONNECT_STAGGER_MS", "").strip()
    if not raw and "modal" in (label or "").lower():
        raw = "500"
    try:
        return max(0, min(int(raw or "0"), 5_000))
    except Exception:
        return 0


async def _stagger_browser_connect(label: str, active_for_label: int) -> None:
    step_ms = _browser_stagger_ms(label)
    if step_ms <= 0 or active_for_label <= 1:
        return
    max_ms = max(0, min(_env_int("BROWSER_CONNECT_STAGGER_MAX_MS", 10_000), 20_000))
    delay_ms = min(max_ms, (active_for_label - 1) * step_ms)
    if delay_ms <= 0:
        return
    await asyncio.sleep((delay_ms / 1000.0) + random.uniform(0.05, 0.25))


def _proxy_env_label(label: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", label or "").strip("_").upper()
    return normalized or "DEFAULT"


def _parse_proxy_url(proxy_url: str) -> dict[str, str]:
    parsed = urlsplit(proxy_url.strip())
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("Proxy precisa estar no formato http://usuario:senha@host:porta")

    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"

    proxy: dict[str, str] = {"server": server}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def _parse_browser_proxy_map() -> dict[str, dict[str, str]]:
    raw = os.getenv("BROWSER_PROXY_MAP", "").strip()

    global _BROWSER_PROXY_ENV_KEY, _BROWSER_PROXY_MAP
    if _BROWSER_PROXY_ENV_KEY == raw:
        return _BROWSER_PROXY_MAP

    proxy_map: dict[str, dict[str, str]] = {}
    for raw_entry in raw.split(";;"):
        entry = raw_entry.strip()
        if not entry:
            continue

        parts = [part.strip() for part in entry.split("|", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            logger.warning("[BROWSER_PROXY] entrada ignorada em BROWSER_PROXY_MAP")
            continue

        label, proxy_url = parts
        try:
            proxy_map[label] = _parse_proxy_url(proxy_url)
        except Exception as exc:
            logger.warning("[BROWSER_PROXY] proxy ignorado label=%s erro=%s", label, exc)

    _BROWSER_PROXY_ENV_KEY = raw
    _BROWSER_PROXY_MAP = proxy_map
    return _BROWSER_PROXY_MAP


def _browser_proxy_for_label(label: str) -> Optional[dict[str, str]]:
    env_key = f"BROWSER_PROXY_URL_{_proxy_env_label(label)}"
    raw = os.getenv(env_key, "").strip()
    if raw:
        try:
            return _parse_proxy_url(raw)
        except Exception as exc:
            logger.warning("[BROWSER_PROXY] proxy ignorado label=%s env=%s erro=%s", label, env_key, exc)
            return None

    return _parse_browser_proxy_map().get(label)


async def create_browser_context(config: FlowConfig) -> Tuple[Any, Callable[[], Awaitable[None]]]:
    last_err: Optional[BaseException] = None

    max_attempts = int(os.getenv("BROWSER_CONNECT_ATTEMPTS", "6"))
    max_attempts = max(1, min(max_attempts, 12))

    for attempt in range(1, max_attempts + 1):
        pw = None
        browser = None
        context = None
        browser_label = ""
        label_registered = False

        try:
            pw = await async_playwright().start()
            browser_label, browser_cdp_url = _next_browser_cdp_target()
            active_for_label = await _register_browser_label(browser_label)
            label_registered = True
            await _stagger_browser_connect(browser_label, active_for_label)
            logger.info(
                "[BROWSER_CONNECT] run=%s cnpj=%s target=%s attempt=%s/%s",
                config.run_id,
                os.path.basename(config.cnpj_dir or ""),
                browser_label,
                attempt,
                max_attempts,
            )

            browser = await pw.chromium.connect_over_cdp(browser_cdp_url)

            context_options: dict[str, Any] = {
                "accept_downloads": True,
                "viewport": {"width": 1366, "height": 900},
            }
            proxy = _browser_proxy_for_label(browser_label)
            if proxy:
                context_options["proxy"] = proxy
                logger.info(
                    "[BROWSER_PROXY] run=%s cnpj=%s target=%s proxy=on server=%s",
                    config.run_id,
                    os.path.basename(config.cnpj_dir or ""),
                    browser_label,
                    proxy.get("server", ""),
                )

            context = await browser.new_context(**context_options)
            context.set_default_timeout(config.selector_timeout_ms)
            context.set_default_navigation_timeout(config.nav_timeout_ms)

            async def closer() -> None:
                try:
                    if context is not None:
                        await asyncio.wait_for(context.close(), timeout=config.close_timeout_sec)
                except Exception:
                    pass

                try:
                    if browser is not None:
                        await asyncio.wait_for(browser.close(), timeout=config.close_timeout_sec)
                except Exception:
                    pass

                try:
                    if pw is not None:
                        await asyncio.wait_for(pw.stop(), timeout=config.close_timeout_sec)
                except Exception:
                    pass

                if browser_label:
                    await _unregister_browser_label(browser_label)

            return context, closer

        except Exception as e:
            last_err = e
            logger.warning(f"[browser] falha ao criar browser/context tentativa={attempt}: {e}")

            try:
                if context is not None:
                    await context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    await browser.close()
            except Exception:
                pass
            try:
                if pw is not None:
                    await pw.stop()
            except Exception:
                pass
            if label_registered and browser_label:
                await _unregister_browser_label(browser_label)

            text = str(e).lower()
            if "429" in text or "too many requests" in text:
                await asyncio.sleep(min(12.0, 1.5 * attempt))
            elif attempt < max_attempts:
                await asyncio.sleep(min(4.0, 0.5 * attempt))

    raise last_err or RuntimeError("Falha ao criar browser context")


async def resilient_goto(page: Page, url: str, *, config: FlowConfig) -> Any:
    last: Optional[BaseException] = None
    for i in range(1, config.goto_retries + 1):
        try:
            return await page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            last = e
            logger.warning(f"[goto] falhou {i}/{config.goto_retries}: {e}")
            await asyncio.sleep(0.8)
    raise last or RuntimeError("resilient_goto falhou")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def portal_timeout_ms(
    name: str,
    default: int,
    *,
    min_ms: int = 1_000,
    max_ms: int = 180_000,
) -> int:
    value = _env_int(name, default)
    return max(min_ms, min(value, max_ms))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off", "nao", "não"}


def requests_bootstrap_enabled() -> bool:
    return _env_bool("PORTAL_REQUESTS_BOOTSTRAP", True)


async def detect_portal_access_block(page: Page) -> None:
    try:
        title = await page.title()
    except Exception:
        title = ""

    try:
        body = await page.locator("body").inner_text(timeout=2_000)
    except Exception:
        body = ""

    text = f"{title}\n{body}".strip()
    low = text.lower()
    blocked = (
        "geo-ip filter alert" in low
        or "this site has been blocked by the network administrator" in low
        or ("forbidden" in low and "sefin.fortaleza" in low)
    )
    if not blocked:
        return

    compact = re.sub(r"\s+", " ", text).strip()
    ip_match = re.search(r"IP address:\s*([0-9.]+)", compact, re.IGNORECASE)
    reason_match = re.search(r"Block reason:\s*([^\.]+(?:\.))", compact, re.IGNORECASE)
    detail_parts = []
    if reason_match:
        detail_parts.append(reason_match.group(1).strip())
    if ip_match:
        detail_parts.append(f"IP={ip_match.group(1)}")
    detail = " | ".join(detail_parts) or compact[:300]
    raise PortalAccessBlockedError(detail)


async def _wait_portal_processing(page: Page, timeout_ms: int = 12_000) -> None:
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        try:
            busy = await page.evaluate(
                """() => {
                    const visible = (el) => {
                        if (!el) return false;
                        const st = window.getComputedStyle(el);
                        const box = el.getBoundingClientRect();
                        return st.display !== 'none' && st.visibility !== 'hidden' &&
                               Number(st.opacity || '1') > 0 &&
                               (box.width > 0 || box.height > 0);
                    };
                    const selectors = [
                        '#mpProgressoContainer',
                        '#mpProgressoDiv',
                        '[id$="mpProgressoContainer"]',
                        '[id$="mpProgressoDiv"]'
                    ];
                    return selectors.some((sel) =>
                        Array.from(document.querySelectorAll(sel)).some(visible)
                    );
                }"""
            )
            if not busy:
                return
        except Exception:
            return
        await asyncio.sleep(0.35)


async def _portal_modal_state(page: Page) -> dict[str, Any]:
    try:
        state = await page.evaluate(
            """() => {
                const norm = (s) => (s || '')
                    .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                    .toLowerCase().replace(/\\s+/g, ' ').trim();
                const visible = (el) => {
                    if (!el) return false;
                    const st = window.getComputedStyle(el);
                    const box = el.getBoundingClientRect();
                    return st.display !== 'none' && st.visibility !== 'hidden' &&
                           Number(st.opacity || '1') > 0 &&
                           (box.width > 0 || box.height > 0);
                };
                const bodyRaw = document.body ? (document.body.innerText || document.body.textContent || '') : '';
                const body = norm(bodyRaw);
                const panels = Array.from(document.querySelectorAll([
                    '#mensagensModalContainer',
                    '#mensagensModalContentDiv',
                    '#mensagensModalCDiv',
                    '.rich-modalpanel',
                    '.rich-mpnl-panel'
                ].join(','))).filter(visible);
                const panelRaw = panels.map((el) => el.innerText || el.textContent || '').join(' ');
                const panel = norm(panelRaw);
                const text = `${body} ${panel}`;
                const blockers = Array.from(document.querySelectorAll([
                    '#mensagensModalDiv',
                    '#mensagensModalContainer',
                    '#mensagensModalCDiv',
                    '#mensagensModalContentDiv',
                    '.rich-mpnl-mask-div',
                    '.rich-mpnl-mask-div-opaque',
                    '.rich-modalpanel',
                    '.rich-mpnl-panel',
                    '.modal-backdrop'
                ].join(','))).filter(visible);
                const ajaxResidue = /insert_command|partial-response|<update|<eval|a4j\\.ajax|richfaces/i.test(bodyRaw);
                return {
                    hasPesquisaSefin: text.includes('pesquisa sefin'),
                    hasManualMessage: (
                        text.includes('visualizar mensagens') ||
                        text.includes('dar ciencia') ||
                        text.includes('tomar ciencia')
                    ),
                    hasMenu: !!document.querySelector("[id='formMenuTopo'], [id^='formMenuTopo:']"),
                    ajaxResidue,
                    blockerCount: blockers.length,
                    panelText: panelRaw.replace(/\\s+/g, ' ').trim().slice(0, 180),
                    bodyText: bodyRaw.replace(/\\s+/g, ' ').trim().slice(0, 180)
                };
            }"""
        )
        return dict(state or {})
    except Exception:
        return {}


async def _click_pesquisa_sefin_nao(page: Page) -> bool:
    try:
        clicked = await page.evaluate(
            """() => {
                const norm = (s) => (s || '')
                    .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                    .toLowerCase().replace(/\\s+/g, ' ').trim();
                const body = norm(document.body ? (document.body.innerText || document.body.textContent || '') : '');
                if (!body.includes('pesquisa sefin')) return false;
                const controls = Array.from(document.querySelectorAll('input, button, a'));
                const isNo = (el) => {
                    const text = norm(
                        el.value || el.innerText || el.textContent || el.title ||
                        el.getAttribute('aria-label') || el.getAttribute('alt') || ''
                    );
                    return text === 'nao' || text === 'n' || text.startsWith('nao ') ||
                           text.includes(' nao') || text.includes('nao,') || text.includes('nao.');
                };
                const btn = controls.find(isNo);
                if (!btn) return false;
                btn.click();
                return true;
            }"""
        )
        return bool(clicked)
    except Exception:
        return False


async def _remove_broken_portal_blockers(page: Page) -> int:
    try:
        removed = await page.evaluate(
            """() => {
                const norm = (s) => (s || '')
                    .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                    .toLowerCase().replace(/\\s+/g, ' ').trim();
                const visible = (el) => {
                    if (!el) return false;
                    const st = window.getComputedStyle(el);
                    const box = el.getBoundingClientRect();
                    return st.display !== 'none' && st.visibility !== 'hidden' &&
                           Number(st.opacity || '1') > 0 &&
                           (box.width > 0 || box.height > 0);
                };
                const bodyRaw = document.body ? (document.body.innerText || document.body.textContent || '') : '';
                const panels = Array.from(document.querySelectorAll([
                    '#mensagensModalContainer',
                    '#mensagensModalContentDiv',
                    '#mensagensModalCDiv',
                    '.rich-modalpanel',
                    '.rich-mpnl-panel'
                ].join(','))).filter(visible);
                const panelText = norm(panels.map((el) => el.innerText || el.textContent || '').join(' '));
                const fullText = `${norm(bodyRaw)} ${panelText}`;
                const manualMessage = (
                    fullText.includes('visualizar mensagens') ||
                    fullText.includes('dar ciencia') ||
                    fullText.includes('tomar ciencia')
                );
                if (manualMessage) return 0;
                const ajaxResidue = /insert_command|partial-response|<update|<eval|a4j\\.ajax|richfaces/i.test(bodyRaw);
                const sefin = fullText.includes('pesquisa sefin');
                const blockers = Array.from(document.querySelectorAll([
                    '#mensagensModalDiv',
                    '#mensagensModalContainer',
                    '#mensagensModalCDiv',
                    '#mensagensModalContentDiv',
                    '#mensagensModalShadowDiv',
                    '#mensagensModalCursorDiv',
                    '.rich-mpnl-mask-div',
                    '.rich-mpnl-mask-div-opaque',
                    '.rich-modalpanel',
                    '.rich-mpnl-panel',
                    '.modal-backdrop'
                ].join(','))).filter(visible);
                if (!blockers.length) return 0;
                if (!ajaxResidue && !sefin && panelText.length > 20) return 0;
                blockers.forEach((el) => el.remove());
                document.body && (document.body.style.overflow = '');
                return blockers.length;
            }"""
        )
        return int(removed or 0)
    except Exception:
        return 0


async def settle_portal_page(page: Page, ctx: FlowContext, *, reason: str = "") -> None:
    await _wait_portal_processing(page)

    for _ in range(2):
        clicked = await _click_pesquisa_sefin_nao(page)
        if not clicked:
            break
        await log_flow(
            ctx,
            f"Modal Pesquisa Sefin fechado com Nao ({reason or 'portal'}).",
            event="INFO",
        )
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except PWTimeoutError:
            pass
        await _wait_portal_processing(page)
        await asyncio.sleep(0.5)

    state = await _portal_modal_state(page)
    if state.get("hasManualMessage"):
        raise MensagemTelaError("Mensagem na tela")

    removed = await _remove_broken_portal_blockers(page)
    if removed:
        await log_flow(
            ctx,
            f"Removidos {removed} bloqueador(es) RichFaces/AJAX travados ({reason or 'portal'}).",
            event="WARN",
            level=logging.WARNING,
            code="PORTAL_BLOCKER_CLEANUP",
        )
        await asyncio.sleep(0.4)

    state = await _portal_modal_state(page)
    if state.get("hasManualMessage"):
        raise MensagemTelaError("Mensagem na tela")

    needs_reload = bool(state.get("ajaxResidue")) and not bool(state.get("hasMenu"))
    if needs_reload:
        await log_flow(
            ctx,
            f"Resposta AJAX parcial detectada; recarregando home.seam ({reason or 'portal'}).",
            event="WARN",
            level=logging.WARNING,
            code="PORTAL_AJAX_RECOVERY",
        )
        await resilient_goto(page, "https://iss.fortaleza.ce.gov.br/grpfor/home.seam", config=ctx.config)
        await detect_portal_access_block(page)
        await _wait_portal_processing(page)
        if await _click_pesquisa_sefin_nao(page):
            await _wait_portal_processing(page)
        state = await _portal_modal_state(page)
        if state.get("hasManualMessage"):
            raise MensagemTelaError("Mensagem na tela")
        removed = await _remove_broken_portal_blockers(page)
        if removed:
            await log_flow(
                ctx,
                f"Removidos {removed} bloqueador(es) apos recarregar home.seam.",
                event="WARN",
                level=logging.WARNING,
                code="PORTAL_BLOCKER_CLEANUP",
            )


async def submit_portal_login(page: Page, usuario: str, senha: str, config: FlowConfig) -> None:
    url = "https://iss.fortaleza.ce.gov.br/grpfor/oauth2/login"
    attempts = max(1, min(_env_int("PORTAL_LOGIN_ATTEMPTS", 2), 4))
    nav_timeout = max(10_000, min(_env_int("PORTAL_LOGIN_NAV_TIMEOUT_MS", 45_000), config.nav_timeout_ms))
    idle_timeout = max(1_000, min(_env_int("PORTAL_LOGIN_IDLE_TIMEOUT_MS", 8_000), 20_000))
    selector_timeout = max(config.selector_timeout_ms, _env_int("PORTAL_LOGIN_SELECTOR_TIMEOUT_MS", 60_000))
    last: Optional[BaseException] = None

    for attempt in range(1, attempts + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)
            await detect_portal_access_block(page)
            await page.wait_for_selector("#username", state="visible", timeout=selector_timeout)
            await page.fill("#username", usuario)
            await page.fill("#password", senha)
            await page.wait_for_selector("#botao-entrar", state="visible", timeout=selector_timeout)
            await page.click("#botao-entrar", timeout=selector_timeout)

            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except PWTimeoutError:
                pass
            try:
                await page.wait_for_load_state("networkidle", timeout=idle_timeout)
            except PWTimeoutError:
                pass
            return
        except Exception as e:
            last = e
            logger.warning(f"[login] falhou {attempt}/{attempts}: {e}")
            if attempt < attempts:
                await asyncio.sleep(min(3.0, 0.8 * attempt))

    raise last or RuntimeError("Falha no login do portal")


def _update_ctx_company(ctx: FlowContext, cnpj: str, empresa: str, *, rename_dir: bool) -> str:
    ctx.empresa = empresa
    if not rename_dir:
        return ""

    pasta_final = rename_cnpj_dir_with_company(ctx.config.run_dir, cnpj, empresa)
    ctx.config = FlowConfig(
        run_id=ctx.config.run_id,
        run_dir=ctx.config.run_dir,
        run_log_file=ctx.config.run_log_file,
        cnpj_dir=pasta_final,
        step_timeout_sec=ctx.config.step_timeout_sec,
        nav_timeout_ms=ctx.config.nav_timeout_ms,
        selector_timeout_ms=ctx.config.selector_timeout_ms,
        close_timeout_sec=ctx.config.close_timeout_sec,
        goto_retries=ctx.config.goto_retries,
        headless=ctx.config.headless,
    )
    return pasta_final


async def try_requests_bootstrap_company(
    context: Any,
    page: Page,
    usuario: str,
    senha: str,
    cnpj: str,
    ctx: FlowContext,
    *,
    rename_dir: bool = False,
) -> Optional[BootstrapCompany]:
    if not requests_bootstrap_enabled():
        return None

    timeout = max(10, min(_env_int("PORTAL_REQUESTS_BOOTSTRAP_TIMEOUT_SEC", 60), 180))
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                bootstrap_portal_requests,
                usuario,
                senha,
                cnpj,
                timeout=timeout,
            ),
            timeout=timeout + 10,
        )
        if not result.cookies:
            raise FlowError(
                "REQUESTS_BOOTSTRAP_NO_COOKIES",
                "Bootstrap requests nao retornou cookies.",
                short_message="Login requests nao retornou cookies aproveitaveis.",
                action="Usar fallback pelo navegador.",
                retryable=True,
            )

        await context.add_cookies(result.cookies)
        await resilient_goto(page, result.home_url, config=ctx.config)
        await detect_portal_access_block(page)
        await settle_portal_page(page, ctx, reason="bootstrap requests")

        content = await page.content()
        if re.search(r"kc-form-login|login-actions/authenticate|Por favor,\s*identifique-se", content, re.I):
            raise FlowError(
                "REQUESTS_BOOTSTRAP_COOKIE_REJECTED",
                "Portal redirecionou para login apos injetar cookies.",
                short_message="Sessao requests nao foi aceita pelo navegador.",
                action="Usar fallback pelo navegador ou alinhar proxy/IP.",
                retryable=True,
            )
        if "homeForm" not in content:
            raise FlowError(
                "REQUESTS_BOOTSTRAP_HOME_INVALID",
                f"Home da empresa nao validou apos bootstrap. URL={page.url}",
                short_message="Home da empresa nao ficou pronta apos bootstrap.",
                action="Usar fallback pelo navegador.",
                retryable=True,
            )

        pasta = _update_ctx_company(ctx, cnpj, result.empresa, rename_dir=rename_dir)
        await log_flow(
            ctx,
            f"Bootstrap requests OK: empresa={result.empresa} cid={result.cid}",
            event="INFO",
        )
        return BootstrapCompany(empresa=result.empresa, cid=result.cid, pasta=pasta)

    except FlowError as exc:
        if not exc.retryable:
            raise
        await log_flow(
            ctx,
            f"Bootstrap requests falhou; usando login normal. {type(exc).__name__}: {exc}",
            event="WARN",
            level=logging.WARNING,
            code="REQUESTS_BOOTSTRAP_FALLBACK",
        )
        return None

    except Exception as exc:
        await log_flow(
            ctx,
            f"Bootstrap requests falhou; usando login normal. {type(exc).__name__}: {exc}",
            event="WARN",
            level=logging.WARNING,
            code="REQUESTS_BOOTSTRAP_FALLBACK",
        )
        return None


async def run_step(
    ctx: FlowContext,
    name: str,
    coro: Awaitable[Any],
    *,
    timeout_sec: Optional[float] = None,
) -> Any:
    mark_step(ctx, name)
    await log_flow(ctx, f"Step: {name}", event="STEP")

    effective_timeout = float(
        ctx.config.step_timeout_sec if timeout_sec is None else timeout_sec
    )

    try:
        return await asyncio.wait_for(coro, timeout=effective_timeout)

    except asyncio.TimeoutError as e:
        await log_flow(
            ctx,
            f"Timeout na etapa '{name}': {e}",
            event="ERROR",
            level=logging.ERROR,
            code="STEP_TIMEOUT",
        )
        raise

    except PWTimeoutError as e:
        await log_flow(
            ctx,
            f"Timeout Playwright em '{name}': {e}",
            event="ERROR",
            level=logging.ERROR,
            code="PW_TIMEOUT",
        )
        raise

    except TargetClosedError as e:
        await log_flow(
            ctx,
            f"Browser fechado em '{name}': {e}",
            event="ERROR",
            level=logging.ERROR,
            code="BROWSER_TARGET_CLOSED",
        )
        raise

    except FlowError:
        raise

    except Exception as e:
        await log_flow(
            ctx,
            f"Erro em '{name}': {type(e).__name__}: {e}",
            event="ERROR",
            level=logging.ERROR,
            code="STEP_ERROR",
        )
        raise
