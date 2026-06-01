#!/usr/bin/env python3
"""
flow_certidao.py

Fluxo:
- Login
- Pesquisa empresa pelo CNPJ
- Acessa menu Consultar Situação Fiscal
- Abre popup da certidão
- Seleciona tipo da certidão
- Baixa PDFs com validação de arquivo não vazio/PDF válido

Estrutura:
output/runXXXXXXXXXX/tentativa_N/certidao/<cnpj> - <empresa>/certidao/
"""

import asyncio
import base64
import html
import logging
import os
import re
import traceback
from typing import Any, Dict, Optional, Tuple

from flow_core import (
    FlowConfig,
    FlowContext,
    create_browser_context,
    ensure_dir,
    log_flow,
    rename_cnpj_dir_with_company,
    resilient_goto,
    run_step,
    somente_digitos,
    save_download_checked,
)
from flow_errors import (
    CnpjInexistenteError,
    CnpjMismatchError,
    FlowError,
    LoginError,
    MensagemTelaError,
    handle_job_error,
)

logger = logging.getLogger("iss.certidao")


# ──────────────────────────────────────────────────────────────────────────────
# DOWNLOAD VIA BROWSER FETCH (CDP REMOTO + JSF)
# ──────────────────────────────────────────────────────────────────────────────

_JS_FETCH_FORM_PDF = """
async (selector) => {
    try {
        const btn = document.querySelector(selector);
        if (!btn) return { error: 'Botão não encontrado: ' + selector };

        const form = btn.closest('form');
        if (!form) return { error: 'Form pai não encontrado para: ' + selector };

        const fd = new FormData(form);
        if (btn.name) {
            fd.append(btn.name, btn.value || '');
        }

        const url = form.action || window.location.href;

        const resp = await fetch(url, {
            method: 'POST',
            body: new URLSearchParams(fd),
        });

        const ct  = resp.headers.get('content-type') || '';
        const cd  = resp.headers.get('content-disposition') || '';
        const buf = await resp.arrayBuffer();
        const bytes = new Uint8Array(buf);

        let binary = '';
        const CHUNK = 8192;
        for (let i = 0; i < bytes.length; i += CHUNK) {
            const slice = bytes.subarray(i, Math.min(i + CHUNK, bytes.length));
            binary += String.fromCharCode.apply(null, slice);
        }

        return {
            base64: btoa(binary),
            contentType: ct,
            contentDisposition: cd,
            status: resp.status,
            size: bytes.length,
        };
    } catch (e) {
        return { error: e.message || String(e) };
    }
}
"""


def _validar_pdf_bytes(data: bytes) -> Tuple[bool, str]:
    if not data or len(data) < 100:
        return False, f"Muito pequeno ({len(data) if data else 0} bytes)"
    if data[:5] != b"%PDF-":
        return False, f"Header inválido (esperado %PDF-, obteve {data[:10]!r})"
    return True, f"OK ({len(data)} bytes)"


def _validar_pdf_salvo(caminho: str) -> Tuple[bool, str]:
    if not os.path.exists(caminho):
        return False, "Arquivo não existe no disco"
    size = os.path.getsize(caminho)
    if size < 100:
        return False, f"Muito pequeno ({size} bytes)"
    with open(caminho, "rb") as f:
        header = f.read(5)
    if header != b"%PDF-":
        return False, f"Header inválido (esperado %PDF-, obteve {header!r})"
    return True, f"OK ({size} bytes)"


async def baixar_pdf_via_browser_fetch(
    page, click_selector: str, caminho: str, ctx, timeout_sec: float = 60
) -> None:
    try:
        result = await asyncio.wait_for(
            page.evaluate(_JS_FETCH_FORM_PDF, click_selector),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        raise FlowError(
            "DOWNLOAD_FETCH_TIMEOUT",
            f"Timeout ({timeout_sec}s) ao fazer fetch interno para {caminho}",
            short_message="Timeout no download do PDF via fetch interno.",
            action="Verificar se o portal está respondendo.",
            retryable=True,
        )
    except Exception as e:
        raise FlowError(
            "DOWNLOAD_FETCH_EVALUATE_ERROR",
            f"page.evaluate falhou: {type(e).__name__}: {e}",
            short_message="Erro ao executar fetch interno no browser.",
            action="Verificar estado da página e conexão CDP.",
            retryable=True,
        )

    if not result:
        raise FlowError(
            "DOWNLOAD_FETCH_EMPTY",
            "page.evaluate retornou resultado vazio",
            short_message="Fetch interno retornou vazio.",
            action="Verificar se o botão e o form existem na página.",
            retryable=False,
        )

    if "error" in result:
        raise FlowError(
            "DOWNLOAD_FETCH_JS_ERROR",
            f"Fetch JS error: {result['error']}",
            short_message=f"Erro no fetch interno: {result['error']}",
            action="Verificar seletores do botão e estrutura do form.",
            retryable=False,
        )

    try:
        pdf_bytes = base64.b64decode(result["base64"])
    except Exception as e:
        raise FlowError(
            "DOWNLOAD_FETCH_DECODE_ERROR",
            f"Erro ao decodificar base64: {e}",
            short_message="Erro na decodificação do PDF.",
            action="Verificar integridade da resposta.",
            retryable=True,
        )

    valido, msg_validacao = _validar_pdf_bytes(pdf_bytes)

    if not valido:
        ct = result.get("contentType", "")
        status = result.get("status", 0)
        raise FlowError(
            "DOWNLOAD_FETCH_NOT_PDF",
            f"Fetch retornou conteúdo inválido: {msg_validacao} "
            f"(content-type={ct}, HTTP {status})",
            short_message="O portal retornou HTML em vez de PDF.",
            action="Sessão pode ter expirado ou o form mudou. Tentar novamente.",
            retryable=True,
        )

    with open(caminho, "wb") as f:
        f.write(pdf_bytes)

    await log_flow(ctx, f"PDF salvo: {caminho} — {msg_validacao}", event="INFO")


async def baixar_pdf_com_fallback(
    page, click_selector: str, caminho: str, ctx, timeout_sec: float = 60
) -> None:
    # ── Estratégia 1: Browser Fetch (principal) ──
    try:
        await baixar_pdf_via_browser_fetch(
            page, click_selector, caminho, ctx, timeout_sec
        )
        return
    except Exception as e1:
        await log_flow(
            ctx,
            f"Browser fetch falhou ({type(e1).__name__}), tentando save_as...",
            event="WARN",
        )

    # ── Estratégia 2: expect_download + save_as (fallback) ──
    try:
        async with page.expect_download(
            timeout=int(timeout_sec * 1000)
        ) as dl_info:
            await page.click(click_selector)
        download = await dl_info.value

        dl_failure = await download.failure()
        if dl_failure:
            raise Exception(f"Browser reportou falha: {dl_failure}")

        await download.save_as(caminho)

        valido, msg = _validar_pdf_salvo(caminho)
        if valido:
            await log_flow(ctx, f"PDF salvo via save_as: {caminho} — {msg}", event="INFO")
            return
        else:
            try:
                os.remove(caminho)
            except OSError:
                pass
            raise Exception(f"save_as() gerou arquivo inválido: {msg}")

    except Exception as e2:
        await log_flow(
            ctx,
            f"save_as também falhou: {type(e2).__name__}: {e2}",
            event="ERROR",
        )

    raise FlowError(
        "DOWNLOAD_TODAS_ESTRATEGIAS_FALHARAM",
        f"Nenhuma estratégia de download funcionou para {caminho}",
        short_message="Não foi possível salvar o PDF da certidão.",
        action="Verificar sessão, estado do form e conectividade CDP.",
        retryable=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
# UTILITÁRIOS
# ──────────────────────────────────────────────────────────────────────────────

def _safe_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _norm_cnpj(s: str) -> str:
    return somente_digitos(s or "").zfill(14)


def _build_cnpj_dir(run_dir: str, cnpj: str) -> str:
    return os.path.join(run_dir, _norm_cnpj(cnpj))


# ──────────────────────────────────────────────────────────────────────────────
# VERIFICAÇÃO DE MENSAGENS NA TELA
# ──────────────────────────────────────────────────────────────────────────────

async def _verificar_mensagem_na_tela(page) -> None:
    seletores = [
        "#mensagensModalContentDiv",
        "#mensagensModalCDiv",
        "#mensagensForm\\:mensagemDataTable",
        "#mensagensForm\\:divMensagensTable",
    ]

    for seletor in seletores:
        try:
            el = await page.query_selector(seletor)
            if el and await el.is_visible():
                raise MensagemTelaError("Mensagem na tela")
        except MensagemTelaError:
            raise
        except Exception:
            pass

    try:
        body_text = await page.inner_text("body")
        body_norm = _safe_text(body_text).lower()
        if "visualizar mensagens" in body_norm and "dar ciência" in body_norm:
            raise MensagemTelaError("Mensagem na tela")
    except MensagemTelaError:
        raise
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# LOGIN
# ──────────────────────────────────────────────────────────────────────────────

async def login(page, usuario: str, senha: str, config: FlowConfig) -> None:
    await resilient_goto(
        page,
        "https://iss.fortaleza.ce.gov.br/grpfor/oauth2/login",
        config=config,
    )
    await page.fill("#username", usuario)
    await page.fill("#password", senha)
    await page.click("#botao-entrar")
    await page.wait_for_load_state("networkidle")

    erro_login = await page.query_selector(".login-error-pg .login-error-msg")
    if erro_login:
        msg = (await erro_login.inner_text()).strip()
        raise LoginError(msg)

    try:
        await page.wait_for_selector(
            "form[id='j_id461'] input[type='submit'][value='Não']",
            timeout=3000,
        )
        await page.click("form[id='j_id461'] input[type='submit'][value='Não']")
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# PESQUISAR EMPRESA
# ──────────────────────────────────────────────────────────────────────────────

async def pesquisar_empresa(page, cnpj: str, ctx: FlowContext) -> Tuple[str, str]:
    tentativas = 3

    for tentativa in range(1, tentativas + 1):
        try:
            response = await resilient_goto(
                page,
                "https://iss.fortaleza.ce.gov.br/grpfor/home.seam",
                config=ctx.config,
            )
            status = getattr(response, "status", None)
            if status is None or status >= 500:
                if tentativa == tentativas:
                    raise FlowError(
                        "HOME_LOAD_ERROR",
                        f"Erro ao carregar home.seam para {cnpj} (HTTP {status})",
                        short_message="Falha ao carregar a página inicial do portal.",
                        action="Verificar indisponibilidade do portal e tentar novamente.",
                        retryable=True,
                    )
                await asyncio.sleep(2)
                continue
            break
        except FlowError:
            raise
        except Exception as e:
            if tentativa == tentativas:
                raise FlowError(
                    "HOME_LOAD_ERROR",
                    f"Erro ao carregar home.seam para {cnpj}: {e}",
                    short_message="Falha ao carregar a página inicial do portal.",
                    action="Verificar indisponibilidade do portal e tentar novamente.",
                    retryable=True,
                )
            await asyncio.sleep(2)

    await asyncio.sleep(1.0)
    await _verificar_mensagem_na_tela(page)

    await page.wait_for_selector("input[id$='cpfPesquisa']", timeout=15_000)
    await page.check("input[value='CNPJ']")
    await asyncio.sleep(0.8)

    inp = "input[id$='cpfPesquisa']"
    await page.fill(inp, "")
    await asyncio.sleep(0.3)
    await page.fill(inp, cnpj)
    await asyncio.sleep(0.5)
    await page.evaluate("document.querySelector(\"input[id$='cpfPesquisa']\").blur()")
    await asyncio.sleep(0.5)

    await page.click("input[id$='btnPesquisar']")

    try:
        await page.wait_for_selector(
            "#mpProgressoContainer", state="visible", timeout=5_000
        )
    except Exception:
        pass

    try:
        await page.wait_for_selector(
            "#mpProgressoContainer", state="hidden", timeout=40_000
        )
    except Exception:
        pass

    await asyncio.sleep(1.0)
    await _verificar_mensagem_na_tela(page)

    try:
        msg_elem = await page.query_selector("span[id$='j_id371'] span")
        if msg_elem:
            msg_text = (await msg_elem.inner_text()).strip()
            if "Nenhum registro encontrado" in msg_text:
                raise CnpjInexistenteError(cnpj)
    except CnpjInexistenteError:
        raise
    except Exception:
        pass

    row_link_cnpj = "table[id$='empresaDataTable'] tbody tr td:nth-child(2) a"
    row_link_nome = "table[id$='empresaDataTable'] tbody tr td:nth-child(4) a"

    cnpj_link = await page.query_selector(row_link_cnpj)
    nome_link = await page.query_selector(row_link_nome)

    if not cnpj_link or not nome_link:
        await _verificar_mensagem_na_tela(page)
        raise FlowError(
            "EMPRESA_NAO_LOCALIZADA",
            f"Empresa não localizada para o CNPJ {cnpj}",
            short_message="A pesquisa não retornou empresa utilizável.",
            action="Verificar o CNPJ pesquisado e o HTML retornado da grade.",
            retryable=False,
        )

    cnpj_ret_raw = (await cnpj_link.inner_text()).strip()
    nome_emp = _safe_text(await nome_link.inner_text())

    if _norm_cnpj(cnpj) != _norm_cnpj(cnpj_ret_raw):
        raise CnpjMismatchError(_norm_cnpj(cnpj), _norm_cnpj(cnpj_ret_raw))

    await page.evaluate(
        """
        A4J.AJAX.Submit('alteraInscricaoForm', event, {
          'similarityGroupingId':'alteraInscricaoForm:empresaDataTable:0:linkNome',
          'parameters':{'alteraInscricaoForm:empresaDataTable:0:linkNome':'alteraInscricaoForm:empresaDataTable:0:linkNome'}
        });
        """
    )
    await asyncio.sleep(2)

    await _verificar_mensagem_na_tela(page)

    pasta_final = rename_cnpj_dir_with_company(ctx.config.run_dir, cnpj, nome_emp)
    ctx.empresa = nome_emp
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

    ensure_dir(ctx.config.cnpj_dir)
    return nome_emp, ctx.config.cnpj_dir


# ──────────────────────────────────────────────────────────────────────────────
# POPUP DA CERTIDÃO
# ──────────────────────────────────────────────────────────────────────────────

class PopupServerError(Exception):
    pass


async def esperar_tela_certidao_renderizada(
    page, total_timeout_sec: float = 30.0
) -> None:
    async def _detect_internal_server_error() -> bool:
        try:
            txt = (
                await page.evaluate(
                    "() => document.body ? (document.body.innerText || '') : ''"
                )
            ) or ""
            t = txt.lower()
            return ("internal server error" in t) or ("erro interno" in t)
        except Exception:
            return False

    try:
        await asyncio.wait_for(
            page.wait_for_function(
                """
                () => {
                  const loader = document.querySelector("#gifAguarde");
                  const form   = document.querySelector("form[id*='pesquisaForm']");
                  const hidden = (!loader)
                              || (loader.offsetParent === null)
                              || (loader.style && loader.style.display === "none");
                  return document.readyState === "complete" && hidden && !!form;
                }
                """
            ),
            timeout=total_timeout_sec,
        )
    except asyncio.TimeoutError:
        if await _detect_internal_server_error():
            raise PopupServerError("Internal Server Error detectado no popup")
        raise TimeoutError("Popup não estabilizou (timeout)")


async def acessar_menu_certidao_via_api(page, ctx: FlowContext):
    await log_flow(
        ctx, "Acessando menu Consultar Situação Fiscal", event="STEP_DETAIL"
    )

    base_url = "https://iss.fortaleza.ce.gov.br/grpfor/home.seam"
    pw_ctx = page.context

    vs_el = await page.query_selector("input[name='javax.faces.ViewState']")
    viewstate = await (vs_el.get_attribute("value") if vs_el else None)
    if not viewstate:
        await resilient_goto(page, base_url, config=ctx.config)
        vs_el = await page.query_selector("input[name='javax.faces.ViewState']")
        viewstate = await (vs_el.get_attribute("value") if vs_el else None)

    if not viewstate:
        raise FlowError(
            "CERTIDAO_VIEWSTATE_NOT_FOUND",
            "Não foi possível obter javax.faces.ViewState.",
            short_message="ViewState não encontrado para abrir a certidão.",
            action="Revisar HTML da home e o estado da sessão.",
            retryable=False,
        )

    link = await page.query_selector("a:has-text('Consultar Situação Fiscal')")
    if not link:
        link = await page.query_selector(
            "[id^='formMenuTopo:menuRelatorios:j_id']"
        )

    if not link:
        raise FlowError(
            "CERTIDAO_MENU_NOT_FOUND",
            "Link 'Consultar Situação Fiscal' não encontrado.",
            short_message="Menu da certidão não encontrado.",
            action="Revisar seletor do menu/relatório no portal.",
            retryable=False,
        )

    btn_id = await link.get_attribute("id")
    if not btn_id:
        raise FlowError(
            "CERTIDAO_MENU_ID_NOT_FOUND",
            "O link da certidão não possui ID válido.",
            short_message="ID do menu da certidão não encontrado.",
            action="Revisar o elemento retornado pelo seletor.",
            retryable=False,
        )

    data = {
        "AJAXREQUEST": "_viewRoot",
        "formMenuTopo": "formMenuTopo",
        "javax.faces.ViewState": viewstate,
        btn_id: btn_id,
        "AJAX:EVENTS_COUNT": "1",
    }

    try:
        for p in list(pw_ctx.pages):
            if p is not page:
                try:
                    await p.close()
                except Exception:
                    pass
    except Exception:
        pass

    resp = await page.request.post(base_url, form=data)
    if resp.status != 200:
        raise FlowError(
            "CERTIDAO_AJAX_HTTP_ERROR",
            f"Erro HTTP {resp.status} ao tentar abrir popup de certidão.",
            short_message="Falha HTTP ao abrir a certidão.",
            action="Verificar sessão autenticada e resposta AJAX do portal.",
            retryable=True,
        )

    text = await resp.text()
    m = re.search(
        r"abrirPopup\('([^']*extratoSituacaoFiscal\.seam[^']*)'\)", text
    )
    if not m:
        raise FlowError(
            "CERTIDAO_POPUP_URL_NOT_FOUND",
            "abrirPopup(...) não encontrado na resposta AJAX.",
            short_message="URL do popup da certidão não encontrada.",
            action="Revisar resposta AJAX e possíveis mudanças no portal.",
            retryable=False,
        )

    url_popup = html.unescape(m.group(1)).replace("&amp;", "&")
    if not url_popup.startswith("http"):
        if url_popup.startswith("/"):
            url_popup = "https://iss.fortaleza.ce.gov.br" + url_popup
        else:
            raise FlowError(
                "CERTIDAO_POPUP_URL_INVALID",
                f"URL inválida obtida para popup: {url_popup}",
                short_message="URL inválida do popup da certidão.",
                action="Revisar parsing da resposta AJAX.",
                retryable=False,
            )

    popup = await pw_ctx.new_page()
    await popup.goto(url_popup, wait_until="domcontentloaded")
    await esperar_tela_certidao_renderizada(popup, total_timeout_sec=30.0)
    return popup


# ──────────────────────────────────────────────────────────────────────────────
# PREENCHER CERTIDÃO
# ──────────────────────────────────────────────────────────────────────────────

async def preencher_certidao(
    page, ctx: FlowContext, tipo_value: str = "6"
) -> None:
    await log_flow(
        ctx,
        f"Selecionando tipo de certidão={tipo_value}",
        event="STEP_DETAIL",
    )
    sel = "select[id$=':tipoCertidao']"
    await page.wait_for_selector(sel, timeout=20_000)
    await page.select_option(sel, value=tipo_value)
    await asyncio.sleep(0.8)


# ──────────────────────────────────────────────────────────────────────────────
# BAIXAR CERTIDÕES
# ──────────────────────────────────────────────────────────────────────────────

async def baixar_certidoes(
    page, pasta_saida: str, cnpj: str, ctx: FlowContext
) -> None:
    await log_flow(ctx, "Baixando PDFs da certidão", event="STEP_DETAIL")
    ensure_dir(pasta_saida)

    # ── Recuperar ──
    btn_rec = "input[id$=':btnRecuperar']"
    await page.wait_for_selector(btn_rec, timeout=30_000)
    await page.click(btn_rec)
    await page.wait_for_load_state("networkidle")

    # ── Pesquisar ──
    btn_pesq = "input[id$=':btnPesquisar']"
    await page.wait_for_selector(btn_pesq, timeout=30_000)
    await page.click(btn_pesq)
    await page.wait_for_load_state("networkidle")

    # ── Emitir Certidão ──
    btn_emitir = "input[id$=':btnEmitirCertidao']"
    await page.wait_for_selector(btn_emitir, timeout=30_000)
    is_disabled = (await page.get_attribute(btn_emitir, "disabled")) is not None

    if not is_disabled:
        caminho_pdf1 = os.path.join(pasta_saida, f"{cnpj}_certidao_iss.pdf")
        await baixar_pdf_com_fallback(page, btn_emitir, caminho_pdf1, ctx)
        await log_flow(ctx, f"Certidão ISS salva: {caminho_pdf1}", event="INFO")
    else:
        await log_flow(
            ctx,
            "Botão Emitir Certidão desabilitado; PDF principal não gerado.",
            event="WARN",
            code="CERTIDAO_EMITIR_DESABILITADO",
        )

    # ── Exportar Resultado ──
    btn_export = "input[id$=':btnExportar']"
    await page.wait_for_selector(btn_export, timeout=30_000)
    caminho_pdf2 = os.path.join(pasta_saida, f"{cnpj}_certidao_resultado.pdf")
    await baixar_pdf_com_fallback(page, btn_export, caminho_pdf2, ctx)
    await log_flow(ctx, f"Resultado salvo: {caminho_pdf2}", event="INFO")


# ──────────────────────────────────────────────────────────────────────────────
# JOB PRINCIPAL
# ──────────────────────────────────────────────────────────────────────────────

async def job_certidao(
    cnpj: str,
    mes: str,
    usuario: str,
    senha: str,
    run_id: str,
    run_dir: str,
    run_log_file: str,
    *,
    headless: bool = True,
) -> Dict[str, Any]:
    cnpj_norm = _norm_cnpj(cnpj)
    cnpj_dir = _build_cnpj_dir(run_dir, cnpj_norm)

    ensure_dir(run_dir)
    ensure_dir(cnpj_dir)

    config = FlowConfig(
        run_id=run_id,
        run_dir=run_dir,
        run_log_file=run_log_file,
        cnpj_dir=cnpj_dir,
        step_timeout_sec=120,
        nav_timeout_ms=60_000,
        selector_timeout_ms=30_000,
        close_timeout_sec=15,
        goto_retries=3,
        headless=headless,
    )

    ctx = FlowContext(
        flow="certidao",
        cnpj=cnpj_norm,
        mes=mes,
        config=config,
    )

    await log_flow(ctx, "=== INÍCIO (CERTIDÃO) ===", event="FLOW_START")

    context = None
    closer = None
    page = None
    popup = None

    try:
        context, closer = await create_browser_context(config)
        page = await context.new_page()

        await run_step(ctx, "Login", login(page, usuario, senha, config))

        nome_emp, pasta_saida_base = await run_step(
            ctx, "Pesquisar Empresa", pesquisar_empresa(page, cnpj_norm, ctx)
        )

        certidao_dir = os.path.join(pasta_saida_base, "certidao")
        ensure_dir(certidao_dir)

        await log_flow(ctx, f"Empresa selecionada: {nome_emp}", event="INFO")

        popup = await run_step(
            ctx,
            "Certidão: Abrir Popup",
            acessar_menu_certidao_via_api(page, ctx),
        )

        try:
            await popup.bring_to_front()
        except Exception:
            pass

        await run_step(
            ctx,
            "Certidão: Preencher",
            preencher_certidao(popup, ctx, tipo_value="6"),
        )
        await run_step(
            ctx,
            "Certidão: Baixar PDFs",
            baixar_certidoes(popup, certidao_dir, cnpj_norm, ctx),
        )

        await log_flow(ctx, "=== FIM (CERTIDÃO OK) ===", event="FLOW_END")
        return {
            "status": "ok",
            "cnpj": cnpj_norm,
            "empresa": nome_emp,
            "pasta": certidao_dir,
        }

    except Exception as e:
        pagina_erro = popup if popup is not None else page
        await handle_job_error(ctx, pagina_erro, e)
        raise

    finally:
        if closer is not None:
            try:
                await closer()
            except Exception:
                pass