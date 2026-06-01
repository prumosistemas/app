#!/usr/bin/env python3

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Tuple

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

from flow_errors import FlowError

logger = logging.getLogger("iss")

# Injetado pelo main.py
BASE_DIR = ""


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


async def create_browser_context(config: FlowConfig) -> Tuple[Any, Callable[[], Awaitable[None]]]:
    last_err: Optional[BaseException] = None

    max_attempts = int(os.getenv("BROWSER_CONNECT_ATTEMPTS", "6"))
    max_attempts = max(1, min(max_attempts, 12))

    for attempt in range(1, max_attempts + 1):
        pw = None
        browser = None
        context = None

        try:
            pw = await async_playwright().start()
            browser_cdp_url = os.getenv("BROWSER_CDP_URL", "").strip()
            if not browser_cdp_url:
                raise RuntimeError("BROWSER_CDP_URL não configurado.")

            browser = await pw.chromium.connect_over_cdp(browser_cdp_url)

            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1366, "height": 900},
            )
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


async def run_step(ctx: FlowContext, name: str, coro: Awaitable[Any]) -> Any:
    mark_step(ctx, name)
    await log_flow(ctx, f"Step: {name}", event="STEP")

    try:
        return await asyncio.wait_for(coro, timeout=ctx.config.step_timeout_sec)

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
