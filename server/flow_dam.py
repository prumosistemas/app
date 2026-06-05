#!/usr/bin/env python3
"""
flow_dam.py

Fluxo:
- Login
- Pesquisa empresa pelo CNPJ
- Acessa menu Recolhimento / Emitir DAM
- Seleciona competência
- Consulta tipos 0/1/2
- Emite e baixa PDFs

Estrutura:
output/runXXXXXXXXXX/logs.txt
output/runXXXXXXXXXX/<cnpj> - <empresa>/dam/
"""

import asyncio
import base64
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, Tuple

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
    submit_portal_login,
)
from flow_errors import (
    CnpjInexistenteError,
    CnpjMismatchError,
    FlowError,
    LoginError,
    MensagemTelaError,
    handle_job_error,
)

logger = logging.getLogger("iss.dam")


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


_JS_FETCH_DAM_PRINT_LINK = """
async () => {
    try {
        const form =
            document.querySelector('form#formEmitirDam') ||
            document.querySelector('form[name="formEmitirDam"]');
        if (!form) return { error: 'Form formEmitirDam não encontrado' };

        const fd = new FormData(form);
        fd.set('formEmitirDam', 'formEmitirDam');
        fd.set('formEmitirDam:j_idcl', 'link-imprimir-dam');

        if (!fd.has('comboTipoDam')) {
            fd.set('comboTipoDam', '1');
        }
        if (!fd.has('comboOutroFiltroSelecionado')) {
            fd.set('comboOutroFiltroSelecionado', '');
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
            url,
        };
    } catch (e) {
        return { error: e.message || String(e) };
    }
}
"""


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


def _validar_pdf_bytes(data: bytes) -> Tuple[bool, str]:
    if not data or len(data) < 100:
        return False, f"Muito pequeno ({len(data) if data else 0} bytes)"
    if data[:5] != b"%PDF-":
        return False, f"Header inválido (esperado %PDF-, obteve {data[:10]!r})"
    return True, f"OK ({len(data)} bytes)"


def _remover_arquivo_invalido(caminho: str) -> None:
    try:
        if os.path.exists(caminho):
            os.remove(caminho)
    except OSError:
        pass


async def baixar_dam_pdf_via_browser_fetch(
    page,
    click_selector: str,
    caminho: str,
    ctx: FlowContext,
    *,
    tipo: str,
    timeout_sec: float = 60,
) -> None:
    try:
        result = await asyncio.wait_for(
            page.evaluate(_JS_FETCH_FORM_PDF, click_selector),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        raise FlowError(
            "DAM_FETCH_TIMEOUT",
            f"Timeout ({timeout_sec}s) ao fazer fetch interno do DAM tipo={tipo}",
            short_message="Timeout no download do DAM via fetch interno.",
            action="Verificar se o portal ISS está respondendo e executar retry.",
            retryable=True,
        )
    except Exception as e:
        raise FlowError(
            "DAM_FETCH_EVALUATE_ERROR",
            f"page.evaluate falhou no DAM tipo={tipo}: {type(e).__name__}: {e}",
            short_message="Erro ao executar fetch interno do DAM.",
            action="Verificar estado da página e conexão CDP.",
            retryable=True,
        )

    if not result:
        raise FlowError(
            "DAM_FETCH_EMPTY",
            f"Fetch interno do DAM tipo={tipo} retornou vazio",
            short_message="Fetch interno do DAM retornou vazio.",
            action="Verificar se o botão de confirmação e o form existem na página.",
            retryable=True,
        )

    if "error" in result:
        raise FlowError(
            "DAM_FETCH_JS_ERROR",
            f"Fetch JS error no DAM tipo={tipo}: {result['error']}",
            short_message=f"Erro no fetch interno do DAM: {result['error']}",
            action="Verificar seletor do botão Confirmar e estrutura do form.",
            retryable=True,
        )

    try:
        pdf_bytes = base64.b64decode(result["base64"])
    except Exception as e:
        raise FlowError(
            "DAM_FETCH_DECODE_ERROR",
            f"Erro ao decodificar base64 do DAM tipo={tipo}: {e}",
            short_message="Erro na decodificação do PDF do DAM.",
            action="Verificar integridade da resposta do portal.",
            retryable=True,
        )

    valido, msg_validacao = _validar_pdf_bytes(pdf_bytes)
    if not valido:
        ct = result.get("contentType", "")
        status = result.get("status", 0)
        size = result.get("size", 0)
        raise FlowError(
            "DAM_FETCH_NOT_PDF",
            f"Fetch do DAM tipo={tipo} retornou conteúdo inválido: {msg_validacao} "
            f"(content-type={ct}, HTTP {status}, size={size})",
            short_message="O portal retornou DAM vazio ou conteúdo inválido.",
            action="Sessão pode ter expirado ou o form do DAM mudou. Executar retry.",
            retryable=True,
        )

    ensure_dir(os.path.dirname(caminho))
    with open(caminho, "wb") as f:
        f.write(pdf_bytes)

    await log_flow(ctx, f"DAM salvo via fetch: {os.path.basename(caminho)} — {msg_validacao}", event="INFO")


async def baixar_dam_pdf_via_link_imprimir(
    page,
    caminho: str,
    ctx: FlowContext,
    *,
    tipo: str,
    timeout_sec: float = 60,
) -> None:
    try:
        result = await asyncio.wait_for(
            page.evaluate(_JS_FETCH_DAM_PRINT_LINK),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        raise FlowError(
            "DAM_PRINT_LINK_TIMEOUT",
            f"Timeout ({timeout_sec}s) ao baixar PDF real do DAM tipo={tipo} via link-imprimir-dam",
            short_message="Timeout no download do PDF real do DAM.",
            action="Verificar se o portal ISS está respondendo e executar retry.",
            retryable=True,
        )
    except Exception as e:
        raise FlowError(
            "DAM_PRINT_LINK_EVALUATE_ERROR",
            f"page.evaluate falhou no link-imprimir-dam tipo={tipo}: {type(e).__name__}: {e}",
            short_message="Erro ao executar download real do DAM.",
            action="Verificar estado da página e conexão CDP.",
            retryable=True,
        )

    if not result:
        raise FlowError(
            "DAM_PRINT_LINK_EMPTY",
            f"link-imprimir-dam do DAM tipo={tipo} retornou vazio",
            short_message="Download real do DAM retornou vazio.",
            action="Verificar formEmitirDam e executar retry.",
            retryable=True,
        )

    if "error" in result:
        raise FlowError(
            "DAM_PRINT_LINK_JS_ERROR",
            f"Fetch JS error no link-imprimir-dam tipo={tipo}: {result['error']}",
            short_message=f"Erro no download real do DAM: {result['error']}",
            action="Verificar formEmitirDam e link-imprimir-dam.",
            retryable=True,
        )

    try:
        pdf_bytes = base64.b64decode(result["base64"])
    except Exception as e:
        raise FlowError(
            "DAM_PRINT_LINK_DECODE_ERROR",
            f"Erro ao decodificar base64 do link-imprimir-dam tipo={tipo}: {e}",
            short_message="Erro na decodificação do PDF real do DAM.",
            action="Verificar integridade da resposta do portal.",
            retryable=True,
        )

    valido, msg_validacao = _validar_pdf_bytes(pdf_bytes)
    if not valido:
        ct = result.get("contentType", "")
        status = result.get("status", 0)
        size = result.get("size", 0)
        raise FlowError(
            "DAM_PRINT_LINK_NOT_PDF",
            f"link-imprimir-dam tipo={tipo} retornou conteúdo inválido: {msg_validacao} "
            f"(content-type={ct}, HTTP {status}, size={size})",
            short_message="O portal não retornou o PDF real do DAM.",
            action="Verificar se a seleção do DAM foi aplicada e executar retry.",
            retryable=True,
        )

    ensure_dir(os.path.dirname(caminho))
    with open(caminho, "wb") as f:
        f.write(pdf_bytes)

    await log_flow(ctx, f"DAM PDF real salvo via link-imprimir-dam: {os.path.basename(caminho)} — {msg_validacao}", event="INFO")


async def baixar_dam_pdf_via_link_click(
    page,
    caminho: str,
    ctx: FlowContext,
    *,
    tipo: str,
    timeout_sec: float = 60,
) -> None:
    click_selector = "a#link-imprimir-dam, input#link-imprimir-dam, a[id$='link-imprimir-dam'], input[id$='link-imprimir-dam']"
    async with page.expect_download(timeout=int(timeout_sec * 1000)) as dl:
        await page.click(click_selector, timeout=10_000)
    download = await dl.value

    dl_failure = await download.failure()
    if dl_failure:
        raise FlowError(
            "DAM_PRINT_LINK_CLICK_FAILED",
            f"Browser reportou falha no clique do link-imprimir-dam tipo={tipo}: {dl_failure}",
            short_message="O download real do DAM falhou no navegador.",
            action="Executar retry; se persistir, verificar o portal ISS.",
            retryable=True,
        )

    ensure_dir(os.path.dirname(caminho))
    await download.save_as(caminho)

    valido, msg_validacao = _validar_pdf_salvo(caminho)
    if not valido:
        _remover_arquivo_invalido(caminho)
        raise FlowError(
            "DAM_PRINT_LINK_CLICK_INVALID",
            f"link-imprimir-dam via clique gerou arquivo inválido tipo={tipo}: {msg_validacao}",
            short_message="O download real do DAM veio vazio ou inválido.",
            action="Executar retry; se persistir, verificar mudança no portal ISS.",
            retryable=True,
        )

    await log_flow(ctx, f"DAM PDF real salvo via clique: {os.path.basename(caminho)} — {msg_validacao}", event="INFO")


async def baixar_dam_pdf_com_fallback(
    page,
    caminho: str,
    ctx: FlowContext,
    *,
    tipo: str,
    timeout_sec: float = 60,
) -> None:
    try:
        await baixar_dam_pdf_via_link_imprimir(
            page,
            caminho,
            ctx,
            tipo=tipo,
            timeout_sec=timeout_sec,
        )
        return
    except Exception as e1:
        await log_flow(
            ctx,
            f"link-imprimir-dam via fetch falhou ({type(e1).__name__}: {e1}), tentando clique direto...",
            event="WARN",
        )

    try:
        await baixar_dam_pdf_via_link_click(
            page,
            caminho,
            ctx,
            tipo=tipo,
            timeout_sec=timeout_sec,
        )
        return

    except Exception as e2:
        await log_flow(
            ctx,
            f"link-imprimir-dam via clique também falhou: {type(e2).__name__}: {e2}",
            event="ERROR",
        )

    raise FlowError(
        "DAM_DOWNLOAD_FAILED",
        f"Nenhuma estratégia baixou o PDF real do DAM tipo={tipo}",
        short_message="Não foi possível salvar o PDF real do DAM.",
        action="Executar retry; se persistir, verificar formEmitirDam/link-imprimir-dam.",
        retryable=True,
    )


async def fechar_confirmacao_dam(page) -> None:
    try:
        closed = await page.evaluate(
            """() => {
                try {
                    if (window.Richfaces && typeof window.Richfaces.hideModalPanel === 'function') {
                        window.Richfaces.hideModalPanel('panelQrdCode');
                    }
                } catch (e) {}

                const ids = [
                    'panelQrdCodeContainer',
                    'panelQrdCodeDiv',
                    'panelQrdCodeCursorDiv',
                    'panelQrdCodeShadowDiv',
                    'panelQrdCodeCDiv'
                ];
                let touched = false;
                for (const id of ids) {
                    const el = document.getElementById(id);
                    if (el) {
                        el.style.display = 'none';
                        el.style.visibility = 'hidden';
                        el.style.pointerEvents = 'none';
                        touched = true;
                    }
                }
                document.querySelectorAll('.rich-mpnl-mask-div, .rich-mpnl-panel').forEach((el) => {
                    if ((el.id || '').includes('panelQrdCode')) {
                        el.style.display = 'none';
                        el.style.visibility = 'hidden';
                        el.style.pointerEvents = 'none';
                        touched = true;
                    }
                });
                return touched;
            }"""
        )
        if closed:
            await asyncio.sleep(0.4)
            return
    except Exception:
        pass

    seletores = [
        "a#j_id401",
        "input#btnVoltar",
        "input[id$='btnVoltar']",
        "input[value='Voltar']",
        "input[value='Cancelar']",
        "input[value='Fechar']",
        "a[id$='hideLink']",
    ]

    for seletor in seletores:
        try:
            el = await page.query_selector(seletor)
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(0.4)
                return
        except Exception:
            pass

    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.3)
    except Exception:
        pass


def _safe_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _norm_cnpj(s: str) -> str:
    return somente_digitos(s or "").zfill(14)


def _build_cnpj_dir(run_dir: str, cnpj: str) -> str:
    return os.path.join(run_dir, _norm_cnpj(cnpj))


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


async def acessar_menu_dam(page, ctx: FlowContext) -> None:
    await log_flow(ctx, "Acessando menu Recolhimento / Emitir DAM", event="STEP_DETAIL")

    try:
        await page.hover("text=Recolhimento", timeout=10_000)
    except Exception as e:
        raise FlowError(
            "DAM_MENU_HOVER_FAIL",
            f"Falha ao abrir menu Recolhimento: {e}",
            short_message="Não foi possível abrir o menu de Recolhimento.",
            action="Revisar seletor do menu e possíveis mudanças no HTML.",
            retryable=False,
        )

    await asyncio.sleep(0.8)

    btn_id = await page.evaluate(
        """() => {
            const el = document.querySelector("[id^='formMenuTopo:menuRecolhimento:j_id']");
            return el ? el.id : null;
        }"""
    )

    if not btn_id:
        raise FlowError(
            "DAM_MENU_NOT_FOUND",
            "Não foi possível localizar item do menu Recolhimento.",
            short_message="Item do menu Recolhimento não encontrado.",
            action="Revisar seletor do item do menu no portal.",
            retryable=False,
        )

    await page.evaluate(
        f"""
        A4J.AJAX.Submit('formMenuTopo', event, {{
            'similarityGroupingId':'{btn_id}',
            'parameters':{{'{btn_id}':'{btn_id}'}}
        }});
        """
    )

    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(1.2)


async def selecionar_competencia_dam(page, mes: str, ctx: FlowContext) -> None:
    mes_num, ano = map(int, mes.split("/"))
    await log_flow(ctx, f"Selecionando competência {mes}", event="STEP_DETAIL")

    await page.wait_for_selector("div.rich-calendar-tool-btn", timeout=15_000)
    await page.click("div.rich-calendar-tool-btn")
    await asyncio.sleep(0.6)

    await page.wait_for_selector("#competenciaDateEditorLayout", timeout=10_000)

    base_y = "#competenciaDateEditorLayoutY"
    ano_ok = False
    for i in range(0, 12):
        div = await page.query_selector(f"{base_y}{i}")
        if div and (await div.inner_text()).strip() == str(ano):
            await div.click()
            ano_ok = True
            await asyncio.sleep(0.4)
            break

    if not ano_ok:
        await log_flow(
            ctx,
            f"Ano {ano} não encontrado no calendário de DAM.",
            event="WARN",
            code="DAM_YEAR_NOT_FOUND",
        )

    await page.click(f"#competenciaDateEditorLayoutM{mes_num - 1}")
    await page.click("#competenciaDateEditorButtonOk")
    await asyncio.sleep(0.8)


async def _emitir_dam_tipo(page, tipo: str, pasta_destino: str, ctx: FlowContext) -> bool:
    await log_flow(ctx, f"Emitindo DAM tipo={tipo}", event="STEP_DETAIL")

    combo = page.locator("select#comboImposto")
    await combo.wait_for(state="visible", timeout=30_000)
    await combo.select_option(value=tipo)
    await asyncio.sleep(0.8)

    await page.click("input#btnConsultar")
    await asyncio.sleep(1.0)

    try:
        if await page.is_visible("dt.alert"):
            return False
    except Exception:
        pass

    try:
        await page.wait_for_selector("table#datatable_emissao_dam", timeout=5_000)
    except Exception:
        return False

    try:
        marcar_btn = await page.query_selector("input#btnMarcarTodos")
        if marcar_btn:
            valor = (await marcar_btn.get_attribute("value")) or ""
            if "Marcar" in valor:
                await marcar_btn.click()
                await asyncio.sleep(0.5)
    except Exception:
        pass

    await page.click("input#btnEmitir")
    await asyncio.sleep(0.5)

    await page.wait_for_selector("input#btnConfirma", timeout=20_000)

    ensure_dir(pasta_destino)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nome = f"DAM_tipo_{tipo}_{stamp}.pdf"
    caminho = os.path.join(pasta_destino, nome)

    await baixar_dam_pdf_com_fallback(page, caminho, ctx, tipo=tipo)

    await fechar_confirmacao_dam(page)

    return True


async def emitir_dams(page, pasta_destino: str, ctx: FlowContext) -> Dict[str, bool]:
    await page.wait_for_selector("select#comboImposto", timeout=20_000)

    resultados: Dict[str, bool] = {}
    falhas: Dict[str, str] = {}
    for tipo in ("0", "1", "2"):
        try:
            resultados[tipo] = await _emitir_dam_tipo(page, tipo, pasta_destino, ctx)
        except Exception as e:
            resultados[tipo] = False
            falhas[tipo] = f"{type(e).__name__}: {e}"
            await log_flow(
                ctx,
                f"Falha no DAM tipo={tipo}: {type(e).__name__}: {e}",
                event="WARN",
                level=logging.WARNING,
                code="DAM_TIPO_FAIL",
            )

    if not any(resultados.values()) and falhas:
        detalhes = "; ".join(f"tipo {tipo}: {msg}" for tipo, msg in falhas.items())
        raise FlowError(
            "DAM_EMITIR_FAILED",
            f"Nenhum DAM válido foi baixado. Falhas: {detalhes}",
            short_message="Nenhum DAM válido foi baixado.",
            action="Executar retry; se repetir, verificar indisponibilidade ou mudança no portal ISS.",
            retryable=True,
        )

    return resultados


async def job_dam(
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
        flow="dam",
        cnpj=cnpj_norm,
        mes=mes,
        config=config,
    )

    await log_flow(ctx, "=== INÍCIO (DAM) ===", event="FLOW_START")

    context = None
    closer = None
    page = None

    try:
        context, closer = await create_browser_context(config)
        page = await context.new_page()

        await run_step(ctx, "Login", login(page, usuario, senha, config))
        nome_emp, pasta_saida = await run_step(ctx, "Pesquisar Empresa", pesquisar_empresa(page, cnpj_norm, ctx))

        dam_dir = os.path.join(pasta_saida, "dam")
        ensure_dir(dam_dir)

        await log_flow(ctx, f"Empresa selecionada: {nome_emp}", event="INFO")

        await run_step(ctx, "DAM: Acessar Menu", acessar_menu_dam(page, ctx))
        await run_step(ctx, "DAM: Selecionar Competência", selecionar_competencia_dam(page, mes, ctx))
        resultados = await run_step(ctx, "DAM: Emitir", emitir_dams(page, dam_dir, ctx))

        tipos_ok = [k for k, v in resultados.items() if v]

        await log_flow(
            ctx,
            f"DAM(s) baixado(s): {', '.join(tipos_ok) if tipos_ok else 'nenhum'}",
            event="INFO",
        )

        await log_flow(ctx, "=== FIM (DAM OK) ===", event="FLOW_END")
        return {
            "status": "ok",
            "cnpj": cnpj_norm,
            "empresa": nome_emp,
            "pasta": dam_dir,
            "tipos_ok": tipos_ok,
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
