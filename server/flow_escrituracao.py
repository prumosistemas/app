#!/usr/bin/env python3
"""
flow_escrituracao.py

Fluxo:
- Login
- Pesquisa empresa pelo CNPJ
- Acessa Escrituração
- Preenche competência
- Consulta / Escriturar
- Serviços pendentes (opcional)
- Simples Nacional (opcional)
- Encerrar -> Certificado (PDF)
- Exportação (XLS)

Estrutura:
output/runXXXXXXXXXX/logs.txt
output/runXXXXXXXXXX/<cnpj> - <empresa>/

Atualização:
- Aceita should_stop opcional.
- Se parada for solicitada depois que a escrituração tiver sido aberta, registra WARN no log.
- Não interrompe a execução em andamento; conclui o fluxo atual para evitar deixar o portal em estado inconsistente.
"""

import asyncio
import logging
import os
import re
from datetime import datetime
from typing import Any, Callable, Dict, Tuple

from playwright.async_api import TimeoutError as PWTimeoutError  # type: ignore

from flow_core import (
    FlowConfig,
    FlowContext,
    create_browser_context,
    ensure_dir,
    log_flow,
    portal_timeout_ms,
    rename_cnpj_dir_with_company,
    resilient_goto,
    requests_bootstrap_enabled,
    run_step,
    settle_portal_page,
    somente_digitos,
    submit_portal_login,
    try_requests_bootstrap_company,
)
from flow_errors import (
    CnpjInexistenteError,
    CnpjMismatchError,
    FlowError,
    LoginError,
    MensagemTelaError,
    handle_job_error,
)

logger = logging.getLogger("iss.escrituracao")


def _safe_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _norm_cnpj(s: str) -> str:
    return somente_digitos(s or "").zfill(14)


def _build_cnpj_dir(run_dir: str, cnpj: str) -> str:
    return os.path.join(run_dir, _norm_cnpj(cnpj))


async def _verificar_mensagem_na_tela(page) -> None:
    """
    Detecta de forma ampla qualquer mensagem/modal de mensagens do portal.
    Não precisa capturar o assunto; basta classificar como mensagem na tela.
    """
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


async def login(page, usuario: str, senha: str, config: FlowConfig) -> None:
    await submit_portal_login(page, usuario, senha, config)

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

    await page.wait_for_selector("input[id$='cpfPesquisa']", timeout=ctx.config.selector_timeout_ms)
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
        await page.wait_for_selector("#mpProgressoContainer", state="visible", timeout=5_000)
    except Exception:
        pass

    try:
        await page.wait_for_selector("#mpProgressoContainer", state="hidden", timeout=40_000)
    except Exception:
        pass

    await asyncio.sleep(1.0)
    await _verificar_mensagem_na_tela(page)

    try:
        msg_no_result = await page.query_selector("span[id$='j_id371'] span")
        if msg_no_result:
            txt = (await msg_no_result.inner_text()).strip()
            if "Nenhum registro encontrado" in txt:
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
        body_text = ""
        try:
            body_text = await page.inner_text("body")
        except Exception:
            pass

        if "Nenhum registro encontrado" in body_text:
            raise CnpjInexistenteError(cnpj)

        await _verificar_mensagem_na_tela(page)

        raise FlowError(
            "EMPRESA_NAO_LOCALIZADA",
            f"Empresa não localizada para o CNPJ {cnpj}",
            short_message="A pesquisa não retornou empresa utilizável.",
            action="Repetir a pesquisa; se persistir, verificar o CNPJ e o HTML retornado da grade.",
            retryable=True,
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
    await settle_portal_page(page, ctx, reason="selecionar empresa escrituração")

    await _verificar_mensagem_na_tela(page)

    try:
        modal = await page.query_selector("#mensagensModalContentDiv")
        if modal and await modal.is_visible():
            raise MensagemTelaError("Mensagem na tela")
    except MensagemTelaError:
        raise
    except Exception:
        pass

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


async def acessar_escrituracao(page, ctx: FlowContext) -> None:
    await log_flow(ctx, "Acessando menu Escrituração", event="STEP_DETAIL")
    await settle_portal_page(page, ctx, reason="antes menu Escrituração")
    menu_timeout = portal_timeout_ms("PORTAL_MENU_TIMEOUT_MS", 60_000, max_ms=120_000)

    try:
        await page.hover("text=Escrituração", timeout=menu_timeout)
    except Exception as e:
        raise FlowError(
            "MENU_ESCRIT_HOVER_FAIL",
            f"Falha hover menu Escrituração: {e}",
            short_message="Não foi possível abrir o menu de Escrituração.",
            action="Repetir o fluxo; se persistir, revisar seletor do menu e possíveis mudanças no HTML.",
            retryable=True,
        )

    await asyncio.sleep(0.8)

    btn_id = await page.evaluate(
        """() => {
            const el = document.querySelector("[id^='formMenuTopo:menuEscrituracao:j_id']");
            return el ? el.id : null;
        }"""
    )
    if not btn_id:
        raise FlowError(
            "MENU_ESCRIT_NOT_FOUND",
            "Item do menu Escrituração não localizado.",
            short_message="Item do menu Escrituração não encontrado.",
            action="Repetir o fluxo; se persistir, revisar seletor do item do menu no portal.",
            retryable=True,
        )

    await page.evaluate(
        f"""
        A4J.AJAX.Submit('formMenuTopo', event, {{
            'similarityGroupingId':'{btn_id}',
            'parameters':{{'{btn_id}':'{btn_id}'}}
        }});
        """
    )
    try:
        await page.wait_for_load_state("networkidle", timeout=12_000)
    except PWTimeoutError:
        pass
    await page.wait_for_selector(
        "#manterEscrituracaoForm\\:dataInicialHeader .rich-calendar-tool-btn",
        timeout=ctx.config.selector_timeout_ms,
    )
    await asyncio.sleep(1.0)


async def _selecionar_mes_calendar(page, prefixo: str, mes_num: int, ano: int) -> None:
    await page.click(f"#{prefixo}Header .rich-calendar-tool-btn".replace(":", "\\:"))
    await asyncio.sleep(0.6)

    base_y = f"#{prefixo}DateEditorLayoutY".replace(":", "\\:")
    for i in range(0, 12):
        div = await page.query_selector(f"{base_y}{i}")
        if div and (await div.inner_text()).strip() == str(ano):
            await div.click()
            await asyncio.sleep(0.4)
            break

    sel_m = f"#{prefixo}DateEditorLayoutM{mes_num - 1}".replace(":", "\\:")
    sel_ok = f"#{prefixo}DateEditorButtonOk".replace(":", "\\:")
    await page.click(sel_m)
    await page.click(sel_ok)
    await asyncio.sleep(0.8)


async def preencher_calendarios(page, mes: str, ctx: FlowContext) -> None:
    mes_num, ano = map(int, mes.split("/"))
    await log_flow(ctx, f"Preenchendo competência {mes}", event="STEP_DETAIL")

    await page.wait_for_selector(
        "#manterEscrituracaoForm\\:dataInicialHeader .rich-calendar-tool-btn",
        timeout=ctx.config.selector_timeout_ms,
    )
    await _selecionar_mes_calendar(page, "manterEscrituracaoForm:dataInicial", mes_num, ano)

    await page.wait_for_selector(
        "#manterEscrituracaoForm\\:dataFinalHeader .rich-calendar-tool-btn",
        timeout=ctx.config.selector_timeout_ms,
    )
    await _selecionar_mes_calendar(page, "manterEscrituracaoForm:dataFinal", mes_num, ano)


async def clicar_consultar(page, ctx: FlowContext) -> None:
    await log_flow(ctx, "Consultando escrituração", event="STEP_DETAIL")
    await page.click("#manterEscrituracaoForm\\:btnConsultar")
    await _esperar_resultado_consulta_escrituracao(page)


async def _esperar_resultado_consulta_escrituracao(page, timeout_ms: int = 70_000) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5_000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=12_000)
    except Exception:
        pass

    await _wait_for_stable_escrituracao_state(page, timeout_ms=timeout_ms)


def _is_navigation_race(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return "execution context was destroyed" in text or "most likely because of a navigation" in text


async def _escrituracao_aberta(page) -> bool:
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const body = document.body ? (document.body.innerText || '') : '';
                    return !!document.querySelector("form#abaEncerramentoForm") ||
                           !!document.querySelector("[id='abaEncerramentoForm:btnEncerrarEscrituracao']") ||
                           !!document.querySelector("#abaEncerramento_shifted") ||
                           body.includes('Escrituração Fiscal');
                }"""
            )
        )
    except Exception as exc:
        if _is_navigation_race(exc):
            return False
        raise


async def _escrituracao_resultado_pronto(page) -> bool:
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const visible = (el) => {
                        if (!el) return false;
                        const st = window.getComputedStyle(el);
                        const box = el.getBoundingClientRect();
                        return st.display !== 'none' && st.visibility !== 'hidden' &&
                               Number(st.opacity || '1') > 0 &&
                               (box.width > 0 || box.height > 0);
                    };
                    const busy = Array.from(document.querySelectorAll(
                        '#mpProgressoContainer, #mpProgressoDiv, [id$="mpProgressoContainer"], [id$="mpProgressoDiv"]'
                    )).some(visible);
                    if (busy) return false;
                    const body = document.body ? (document.body.innerText || '') : '';
                    return !!document.querySelector("a[id$=':linkEscriturar']") ||
                           !!document.querySelector("span[id$=':linkEscriturarDesabilitado']") ||
                           !!document.querySelector("a[id$=':linkReabrir']") ||
                           !!document.querySelector("form#abaEncerramentoForm") ||
                           body.includes('Escrituração Fiscal');
                }"""
            )
        )
    except Exception as exc:
        if _is_navigation_race(exc):
            return False
        raise


async def _wait_for_stable_escrituracao_state(page, *, timeout_ms: int) -> None:
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000.0)
    stable_hits = 0
    last_exc: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            if await _escrituracao_resultado_pronto(page):
                stable_hits += 1
                if stable_hits >= 3:
                    return
            else:
                stable_hits = 0
        except Exception as exc:
            last_exc = exc
            if not _is_navigation_race(exc):
                raise
            stable_hits = 0
        await asyncio.sleep(0.45)
    if last_exc:
        raise last_exc
    raise PWTimeoutError("Timeout aguardando resultado da consulta de escrituração")


async def _wait_escrituracao_aberta(page, *, timeout_ms: int = 70_000) -> None:
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000.0)
    stable_hits = 0
    last_exc: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            if await _escrituracao_aberta(page):
                stable_hits += 1
                if stable_hits >= 2:
                    return
            else:
                stable_hits = 0
        except Exception as exc:
            last_exc = exc
            if not _is_navigation_race(exc):
                raise
            stable_hits = 0
        await asyncio.sleep(0.45)
    if last_exc:
        raise last_exc
    raise PWTimeoutError("Timeout aguardando abertura da escrituração")


async def _query_selector_retrying_navigation(page, selector: str, *, timeout_ms: int = 10_000):
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000.0)
    last_exc: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            return await page.query_selector(selector)
        except Exception as exc:
            last_exc = exc
            if not _is_navigation_race(exc):
                raise
        await asyncio.sleep(0.25)
    if last_exc:
        raise last_exc
    return None


async def _js_click_selector(page, selector: str) -> bool:
    try:
        return bool(
            await page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    el.click();
                    return true;
                }""",
                selector,
            )
        )
    except Exception as exc:
        if _is_navigation_race(exc):
            return True
        raise


async def clicar_escriturar_ou_reabrir(page, ctx: FlowContext, *, reabrir_fechada: bool = True) -> None:
    await log_flow(ctx, "Escriturar/Reabrir", event="STEP_DETAIL")

    if await _escrituracao_aberta(page):
        return

    desab = await _query_selector_retrying_navigation(
        page,
        "span[id$=':linkEscriturarDesabilitado']",
        timeout_ms=35_000,
    )
    if await _escrituracao_aberta(page):
        return

    if desab:
        if not reabrir_fechada:
            await log_flow(
                ctx,
                "Empresa tava com a escrituração fechada, fluxo parou.",
                event="WARN",
                code="ESCRITURACAO_FECHADA_REABERTURA_DESATIVADA",
            )
            raise FlowError(
                "ESCRITURACAO_FECHADA_REABERTURA_DESATIVADA",
                "Empresa tava com a escrituração fechada, fluxo parou.",
                short_message="Empresa tava com a escrituração fechada, fluxo parou.",
                action="Reabertura desativada; DAM pode seguir se estiver selecionado.",
                retryable=False,
            )
        try:
            clicked = await _js_click_selector(page, "a[id$=':linkReabrir']")
            if not clicked:
                await page.click("a[id$=':linkReabrir']", timeout=ctx.config.selector_timeout_ms)
            await _esperar_resultado_consulta_escrituracao(page, timeout_ms=70_000)
        except Exception:
            pass

    clicked = await _js_click_selector(page, "a[id$=':linkEscriturar']")
    if not clicked:
        await page.click("a[id$=':linkEscriturar']", timeout=ctx.config.selector_timeout_ms)
    await _wait_escrituracao_aberta(page, timeout_ms=70_000)
    await asyncio.sleep(0.5)


async def aba_servicos_existe(page) -> bool:
    return (await page.query_selector("a[id$=':abaServicosPendentes']")) is not None or (
        await page.query_selector("#aba_servicos_pendentes_shifted") is not None
    )


async def clicar_aba_servicos_pendentes(page, ctx: FlowContext) -> None:
    await log_flow(ctx, "Aba Serviços Pendentes", event="STEP_DETAIL")
    try:
        await page.click("#aba_servicos_pendentes_shifted")
    except Exception:
        await page.click("a[id$=':abaServicosPendentes']")
    await page.wait_for_selector("form#servicos_pendentes_form", timeout=25_000)
    await asyncio.sleep(0.8)


async def aceitar_todos_servicos_pendentes(page, ctx: FlowContext) -> None:
    await log_flow(ctx, "Aceitando serviços pendentes", event="STEP_DETAIL")
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(0.5)

    botoes = [
        (
            "Tomados",
            "#servicos_pendentes_form\\:idLinkaceitarDocTomados",
            "#aceite_todos_doc_tomados_modal_panel_form\\:btnSim",
        ),
        (
            "Prestados",
            "#servicos_pendentes_form\\:idLinkaceitarDocPrestados",
            "#aceite_todos_doc_prestados_modal_panel_form\\:btnSim",
        ),
    ]

    for nome, link, confirmar in botoes:
        try:
            await page.wait_for_selector(link, timeout=20_000)
            await page.click(link)
            await asyncio.sleep(0.8)
            await page.click(confirmar)
            await asyncio.sleep(0.8)
        except Exception:
            await log_flow(
                ctx,
                f"Não foi possível aceitar pendentes ({nome}) - seguindo.",
                event="WARN",
                code="SERV_PEND_FAIL",
            )


async def aba_simples_existe(page) -> bool:
    return (await page.query_selector("a[id$=':abaEspelhoSimplesNacional']")) is not None or (
        await page.query_selector("#abaEspelhoSimplesNacional_shifted") is not None
    )


async def clicar_aba_simples(page, ctx: FlowContext) -> None:
    await log_flow(ctx, "Aba Simples Nacional", event="STEP_DETAIL")
    try:
        await page.click("#abaEspelhoSimplesNacional_shifted")
    except Exception:
        await page.click("a[id$=':abaEspelhoSimplesNacional']")
    await asyncio.sleep(1.2)


async def gerar_pdf_simples(page, cnpj_dir: str, ctx: FlowContext) -> None:
    await log_flow(ctx, "Gerando Simples Nacional (screenshot)", event="STEP_DETAIL")
    await page.wait_for_selector("form#abaEspelhoSimplesNacionalForm", timeout=ctx.config.selector_timeout_ms)

    ensure_dir(cnpj_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    png_path = os.path.join(cnpj_dir, f"simples_{stamp}.png")
    pdf_path = os.path.join(cnpj_dir, f"simples_{stamp}.pdf")

    form = await page.query_selector("form#abaEspelhoSimplesNacionalForm")
    if form is None:
        raise FlowError(
            "SIMPLES_FORM_NOT_FOUND",
            "Formulário do Simples Nacional não localizado.",
            short_message="Formulário do Simples Nacional não encontrado.",
            action="Revisar seletor da aba do Simples Nacional.",
            retryable=False,
        )

    await form.screenshot(path=png_path)

    try:
        from PIL import Image  # type: ignore
        Image.open(png_path).convert("RGB").save(pdf_path)
        try:
            os.remove(png_path)
        except Exception:
            pass
    except Exception:
        pass


async def voltar_encerramento(page, ctx: FlowContext) -> None:
    await log_flow(ctx, "Voltando para aba Encerramento", event="STEP_DETAIL")
    try:
        await page.click("#abaEncerramento_shifted", timeout=ctx.config.selector_timeout_ms)
    except Exception:
        await page.click("a[id$=':abaEncerramento']", timeout=ctx.config.selector_timeout_ms)
    await asyncio.sleep(1.0)
    await page.wait_for_selector("#abaEncerramentoForm\\:btnEncerrarEscrituracao", timeout=ctx.config.selector_timeout_ms)


async def clicar_encerrar(page, ctx: FlowContext) -> None:
    await log_flow(ctx, "Encerrando escrituração", event="STEP_DETAIL")
    clicked = await _js_click_selector(page, "#abaEncerramentoForm\\:btnEncerrarEscrituracao")
    if not clicked:
        await page.click("#abaEncerramentoForm\\:btnEncerrarEscrituracao", timeout=ctx.config.selector_timeout_ms)
    await asyncio.sleep(1.0)


async def confirmar_sim(page, ctx: FlowContext) -> None:
    await log_flow(ctx, "Confirmando encerramento (Sim)", event="STEP_DETAIL")
    clicked = await _js_click_selector(page, "#formEncerramento\\:btnSim")
    if not clicked:
        await page.click("#formEncerramento\\:btnSim", timeout=ctx.config.selector_timeout_ms)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8_000)
    except PWTimeoutError:
        pass
    await asyncio.sleep(1.0)


async def _wait_certificado_button_id(page, timeout_ms: int) -> str:
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000.0)
    last_exc: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            btn_id = await page.evaluate(
                """
                () => {
                  const norm = (s) => (s || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
                  const els = Array.from(document.querySelectorAll('input[id][value], button[id], a[id]'));
                  const el = els.find(e => norm(e.value || e.innerText || e.textContent || '').includes('certificado de encerramento'));
                  return el ? el.id : null;
                }
                """
            )
            if btn_id:
                return str(btn_id)
        except Exception as exc:
            last_exc = exc
            if not _is_navigation_race(exc):
                raise
        await asyncio.sleep(0.75)
    if last_exc:
        raise last_exc
    raise PWTimeoutError("Timeout aguardando botão Certificado de Encerramento")


async def abrir_certificado(page, ctx: FlowContext) -> None:
    await log_flow(ctx, "Abrindo certificado de encerramento", event="STEP_DETAIL")
    try:
        btn_id = await _wait_certificado_button_id(page, ctx.config.selector_timeout_ms)
    except Exception as exc:
        raise FlowError(
            "CERT_BUTTON_NOT_FOUND",
            f"Botão Certificado de Encerramento não localizado: {exc}",
            short_message="Botão do certificado de encerramento não encontrado.",
            action="Repetir o fluxo; se persistir, revisar HTML da tela de encerramento.",
            retryable=True,
        )

    await page.evaluate("(id) => document.getElementById(id)?.click()", btn_id)
    await asyncio.sleep(1.2)
    try:
        await page.wait_for_load_state("networkidle", timeout=12_000)
    except PWTimeoutError:
        pass


async def gerar_pdf_certificado(page, cnpj_dir: str, ctx: FlowContext) -> None:
    await log_flow(ctx, "Gerando PDF do certificado", event="STEP_DETAIL")

    try:
        await page.evaluate(
            """
            let principal = document.getElementById('docPrincipal')?.outerHTML;
            if (principal) document.body.innerHTML = principal;
            """
        )
    except Exception:
        pass

    ensure_dir(cnpj_dir)
    out = os.path.join(cnpj_dir, "certificado.pdf")

    await page.pdf(
        path=out,
        format="A4",
        margin={"top": "20px", "bottom": "20px", "left": "20px", "right": "20px"},
        scale=0.9,
        print_background=True,
    )


async def acessar_exportacao(page, ctx: FlowContext) -> None:
    await log_flow(ctx, "Acessando exportação de escrituração", event="STEP_DETAIL")
    await resilient_goto(
        page,
        "https://iss.fortaleza.ce.gov.br/grpfor/pages/escrituracao/exportarEscrituracao.seam",
        config=ctx.config,
    )
    await asyncio.sleep(1.0)


async def selecionar_competencia_exportacao(page, mes: str, ctx: FlowContext) -> None:
    await log_flow(ctx, f"Selecionando competência exportação: {mes}", event="STEP_DETAIL")
    mes_num, ano = map(int, mes.split("/"))

    btn = "#exportarEscrituracaoForm\\:competenciaHeader .rich-calendar-tool-btn"
    await page.wait_for_selector(btn, timeout=20_000)
    await page.click(btn)
    await asyncio.sleep(0.6)

    base_y = "#exportarEscrituracaoForm\\:competenciaDateEditorLayoutY"
    for i in range(0, 12):
        div = await page.query_selector(f"{base_y}{i}")
        if div and (await div.inner_text()).strip() == str(ano):
            await div.click()
            await asyncio.sleep(0.4)
            break

    await page.click(f"#exportarEscrituracaoForm\\:competenciaDateEditorLayoutM{mes_num - 1}")
    await page.click("#exportarEscrituracaoForm\\:competenciaDateEditorButtonOk")
    await asyncio.sleep(0.8)


async def gerar_exportacao(page, ctx: FlowContext) -> None:
    await log_flow(ctx, "Gerando exportação", event="STEP_DETAIL")
    await page.click("#exportarEscrituracaoForm\\:btnGerar")
    await asyncio.sleep(1.0)


async def baixar_exportacao(page, cnpj: str, cnpj_dir: str, ctx: FlowContext) -> None:
    await log_flow(ctx, "Baixando exportação (XLS)", event="STEP_DETAIL")
    ensure_dir(cnpj_dir)

    btn = "#exportarEscrituracaoForm\\:fileButton"
    nenhum = "#nenhumText"

    try:
        await page.wait_for_selector(btn, state="visible", timeout=60_000)
        async with page.expect_download(timeout=60_000) as dl:
            await page.click(btn)
        download = await dl.value
        destino = os.path.join(cnpj_dir, f"exportacao_{cnpj}.xls")
        await download.save_as(destino)
    except Exception:
        if await page.query_selector(nenhum):
            await log_flow(ctx, "Nenhum arquivo de exportação disponível.", event="INFO")
            return
        raise


async def maybe_log_stop_after_open(
    ctx: FlowContext,
    should_stop: Callable[[], bool],
    *,
    escrituracao_aberta: bool,
    state: Dict[str, bool],
) -> None:
    if not escrituracao_aberta or state.get("logged_stop_after_open"):
        return

    try:
        stopped = bool(should_stop())
    except Exception:
        stopped = False

    if stopped:
        state["logged_stop_after_open"] = True
        await log_flow(
            ctx,
            (
                "Parada solicitada depois que a Escrituração já foi aberta. "
                "O fluxo em execução será concluído para evitar deixar a escrituração em estado inconsistente; "
                "nenhum fluxo ainda não iniciado será executado."
            ),
            event="WARN",
            code="ESCRITURACAO_ABERTA_STOP_REQUESTED",
        )


async def job_escrituracao(
    cnpj: str,
    mes: str,
    usuario: str,
    senha: str,
    run_id: str,
    run_dir: str,
    run_log_file: str,
    *,
    headless: bool = True,
    should_stop: Callable[[], bool] = lambda: False,
    reabrir_fechada: bool = True,
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
        step_timeout_sec=portal_timeout_ms("PORTAL_ESCRITURACAO_STEP_TIMEOUT_MS", 180_000, max_ms=300_000) // 1000,
        nav_timeout_ms=portal_timeout_ms("PORTAL_NAV_TIMEOUT_MS", 90_000, max_ms=180_000),
        selector_timeout_ms=portal_timeout_ms("PORTAL_SELECTOR_TIMEOUT_MS", 60_000, max_ms=180_000),
        close_timeout_sec=15,
        goto_retries=3,
        headless=headless,
    )

    ctx = FlowContext(
        flow="escrituracao",
        cnpj=cnpj_norm,
        mes=mes,
        config=config,
    )

    await log_flow(ctx, "=== INÍCIO (ESCRITURAÇÃO) ===", event="FLOW_START")

    context = None
    closer = None
    page = None
    escrituracao_aberta = False
    stop_state = {"logged_stop_after_open": False}

    try:
        context, closer = await create_browser_context(config)
        page = await context.new_page()

        bootstrap = None
        if requests_bootstrap_enabled():
            bootstrap = await run_step(
                ctx,
                "Login/Empresa requests",
                try_requests_bootstrap_company(
                    context,
                    page,
                    usuario,
                    senha,
                    cnpj_norm,
                    ctx,
                    rename_dir=True,
                ),
            )

        if bootstrap:
            nome_emp = bootstrap.empresa
            pasta_saida = bootstrap.pasta
        else:
            await run_step(ctx, "Login", login(page, usuario, senha, config))
            nome_emp, pasta_saida = await run_step(ctx, "Pesquisar Empresa", pesquisar_empresa(page, cnpj_norm, ctx))

        await log_flow(ctx, f"Empresa selecionada: {nome_emp}", event="INFO")

        await run_step(ctx, "Acessar Escrituração", acessar_escrituracao(page, ctx))
        await run_step(ctx, "Preencher Calendários", preencher_calendarios(page, mes, ctx))
        await run_step(ctx, "Consultar", clicar_consultar(page, ctx))
        await run_step(ctx, "Escriturar/Reabrir", clicar_escriturar_ou_reabrir(page, ctx, reabrir_fechada=reabrir_fechada))
        escrituracao_aberta = True
        await maybe_log_stop_after_open(ctx, should_stop, escrituracao_aberta=escrituracao_aberta, state=stop_state)

        if await aba_servicos_existe(page):
            await run_step(ctx, "Aba Serviços (opcional)", clicar_aba_servicos_pendentes(page, ctx))
            await maybe_log_stop_after_open(ctx, should_stop, escrituracao_aberta=escrituracao_aberta, state=stop_state)
            await run_step(ctx, "Aceitar Pendentes (opcional)", aceitar_todos_servicos_pendentes(page, ctx))
            await maybe_log_stop_after_open(ctx, should_stop, escrituracao_aberta=escrituracao_aberta, state=stop_state)

        if await aba_simples_existe(page):
            await run_step(ctx, "Aba Simples (opcional)", clicar_aba_simples(page, ctx))
            await maybe_log_stop_after_open(ctx, should_stop, escrituracao_aberta=escrituracao_aberta, state=stop_state)
            await run_step(ctx, "Gerar Simples (opcional)", gerar_pdf_simples(page, pasta_saida, ctx))
            await maybe_log_stop_after_open(ctx, should_stop, escrituracao_aberta=escrituracao_aberta, state=stop_state)

        await run_step(ctx, "Voltar Encerramento", voltar_encerramento(page, ctx))
        await maybe_log_stop_after_open(ctx, should_stop, escrituracao_aberta=escrituracao_aberta, state=stop_state)
        await run_step(ctx, "Encerrar", clicar_encerrar(page, ctx))
        await maybe_log_stop_after_open(ctx, should_stop, escrituracao_aberta=escrituracao_aberta, state=stop_state)
        await run_step(ctx, "Confirmar Sim", confirmar_sim(page, ctx))
        await maybe_log_stop_after_open(ctx, should_stop, escrituracao_aberta=escrituracao_aberta, state=stop_state)
        await run_step(ctx, "Abrir Certificado", abrir_certificado(page, ctx))
        await maybe_log_stop_after_open(ctx, should_stop, escrituracao_aberta=escrituracao_aberta, state=stop_state)
        await run_step(ctx, "PDF Certificado", gerar_pdf_certificado(page, pasta_saida, ctx))
        await maybe_log_stop_after_open(ctx, should_stop, escrituracao_aberta=escrituracao_aberta, state=stop_state)

        await run_step(ctx, "Exportação: Acessar", acessar_exportacao(page, ctx))
        await maybe_log_stop_after_open(ctx, should_stop, escrituracao_aberta=escrituracao_aberta, state=stop_state)
        await run_step(ctx, "Exportação: Competência", selecionar_competencia_exportacao(page, mes, ctx))
        await maybe_log_stop_after_open(ctx, should_stop, escrituracao_aberta=escrituracao_aberta, state=stop_state)
        await run_step(ctx, "Exportação: Gerar", gerar_exportacao(page, ctx))
        await maybe_log_stop_after_open(ctx, should_stop, escrituracao_aberta=escrituracao_aberta, state=stop_state)
        await run_step(ctx, "Exportação: Baixar", baixar_exportacao(page, cnpj_norm, pasta_saida, ctx))

        await log_flow(ctx, "=== FIM (OK) ===", event="FLOW_END")
        return {
            "status": "ok",
            "cnpj": cnpj_norm,
            "empresa": nome_emp,
            "pasta": pasta_saida,
        }

    except Exception as e:
        await handle_job_error(ctx, page, e)
        raise

    finally:
        if closer is not None:
            try:
                await closer()
            except Exception:
                pass
