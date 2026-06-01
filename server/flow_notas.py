#!/usr/bin/env python3
"""
flow_notas.py

Fluxo:
- Login
- Pesquisa empresa pelo CNPJ
- NFS-e -> Consultar NFS-e
- Prestadas: selecionar competência + consultar + exportar XML em lotes
- Tomadas: selecionar competência + consultar + exportar XML em lotes

Layouts de saída:

1) usar_codigo_dominio=True:
output/runXXXXXXXXXX/tentativa_N/notas/
├── prestadas/
│   └── <codigo_dominio> - <empresa>/
└── tomadas/
    └── <codigo_dominio> - <empresa>/

2) usar_codigo_dominio=False:
output/runXXXXXXXXXX/tentativa_N/notas/
└── <cnpj> - <empresa>/
    ├── prestadas/
    └── tomadas/
"""

import asyncio
import base64
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, Tuple

from flow_core import (
    FlowConfig,
    FlowContext,
    create_browser_context,
    ensure_dir,
    log_flow,
    resilient_goto,
    run_step,
    sanitize_folder_name,
    somente_digitos,
)
from flow_errors import (
    CnpjInexistenteError,
    CnpjMismatchError,
    FlowError,
    LoginError,
    handle_job_error,
)

logger = logging.getLogger("iss.notas")


# ──────────────────────────────────────────────────────────────────────────────
# DOWNLOAD XML VIA BROWSER FETCH + FALLBACK
# ──────────────────────────────────────────────────────────────────────────────

_JS_FETCH_FORM_XML = """
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


def _validar_xml_bytes(data: bytes) -> Tuple[bool, str]:
    if not data or len(data) < 20:
        return False, f"Muito pequeno ({len(data) if data else 0} bytes)"

    inicio = data[:300].lstrip().lower()

    if inicio.startswith(b"<!doctype html") or inicio.startswith(b"<html") or b"<html" in inicio:
        return False, "Retornou HTML em vez de XML"

    if not inicio.startswith(b"<?xml") and not inicio.startswith(b"<"):
        return False, f"Header inválido para XML: {data[:30]!r}"

    try:
        ET.fromstring(data)
    except Exception as e:
        return False, f"XML inválido: {e}"

    return True, f"OK ({len(data)} bytes)"


def _validar_xml_salvo(caminho: str) -> Tuple[bool, str]:
    if not os.path.exists(caminho):
        return False, "Arquivo não existe no disco"

    size = os.path.getsize(caminho)
    if size < 20:
        return False, f"Muito pequeno ({size} bytes)"

    try:
        with open(caminho, "rb") as f:
            data = f.read()
    except Exception as e:
        return False, f"Erro ao ler arquivo: {e}"

    return _validar_xml_bytes(data)


async def baixar_xml_via_browser_fetch(
    page,
    click_selector: str,
    caminho: str,
    ctx,
    timeout_sec: float = 180,
) -> None:
    try:
        result = await asyncio.wait_for(
            page.evaluate(_JS_FETCH_FORM_XML, click_selector),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        raise FlowError(
            "XML_FETCH_TIMEOUT",
            f"Timeout ({timeout_sec}s) ao fazer fetch interno para {caminho}",
            short_message="Timeout no download do XML via fetch interno.",
            action="Verificar se o portal está respondendo.",
            retryable=True,
        )
    except Exception as e:
        raise FlowError(
            "XML_FETCH_EVALUATE_ERROR",
            f"page.evaluate falhou: {type(e).__name__}: {e}",
            short_message="Erro ao executar fetch interno do XML.",
            action="Verificar estado da página e conexão CDP.",
            retryable=True,
        )

    if not result:
        raise FlowError(
            "XML_FETCH_EMPTY",
            "page.evaluate retornou resultado vazio",
            short_message="Fetch interno retornou vazio.",
            action="Verificar se o botão e o form existem na página.",
            retryable=True,
        )

    if "error" in result:
        raise FlowError(
            "XML_FETCH_JS_ERROR",
            f"Fetch JS error: {result['error']}",
            short_message=f"Erro no fetch interno: {result['error']}",
            action="Verificar seletor do botão Exportar XML e estrutura do form.",
            retryable=True,
        )

    try:
        xml_bytes = base64.b64decode(result["base64"])
    except Exception as e:
        raise FlowError(
            "XML_FETCH_DECODE_ERROR",
            f"Erro ao decodificar base64: {e}",
            short_message="Erro na decodificação do XML.",
            action="Verificar integridade da resposta.",
            retryable=True,
        )

    valido, msg_validacao = _validar_xml_bytes(xml_bytes)

    if not valido:
        ct = result.get("contentType", "")
        status = result.get("status", 0)
        size = result.get("size", 0)

        raise FlowError(
            "XML_FETCH_INVALIDO",
            f"Fetch retornou conteúdo inválido: {msg_validacao} "
            f"(content-type={ct}, HTTP {status}, size={size})",
            short_message="O portal retornou XML vazio ou conteúdo inválido.",
            action="Sessão pode ter expirado, seleção pode não ter aplicado ou o form mudou.",
            retryable=True,
        )

    ensure_dir(os.path.dirname(caminho))
    with open(caminho, "wb") as f:
        f.write(xml_bytes)

    await log_flow(ctx, f"XML salvo via fetch: {caminho} — {msg_validacao}", event="INFO")


async def baixar_xml_com_fallback(
    page,
    click_selector: str,
    caminho: str,
    ctx,
    timeout_sec: float = 180,
) -> None:
    try:
        await baixar_xml_via_browser_fetch(
            page,
            click_selector,
            caminho,
            ctx,
            timeout_sec,
        )
        return
    except Exception as e1:
        await log_flow(
            ctx,
            f"Browser fetch XML falhou ({type(e1).__name__}), tentando save_as...",
            event="WARN",
        )

    try:
        async with page.expect_download(timeout=int(timeout_sec * 1000)) as dl_info:
            await page.click(click_selector)

        download = await dl_info.value

        dl_failure = await download.failure()
        if dl_failure:
            raise Exception(f"Browser reportou falha: {dl_failure}")

        ensure_dir(os.path.dirname(caminho))
        await download.save_as(caminho)

        valido, msg = _validar_xml_salvo(caminho)
        if valido:
            await log_flow(ctx, f"XML salvo via save_as: {caminho} — {msg}", event="INFO")
            return

        try:
            os.remove(caminho)
        except OSError:
            pass

        raise Exception(f"save_as() gerou XML inválido: {msg}")

    except Exception as e2:
        await log_flow(
            ctx,
            f"save_as XML também falhou: {type(e2).__name__}: {e2}",
            event="ERROR",
        )

    raise FlowError(
        "XML_TODAS_ESTRATEGIAS_FALHARAM",
        f"Nenhuma estratégia de download XML funcionou para {caminho}",
        short_message="Não foi possível salvar o XML das notas.",
        action="Verificar sessão, seleção das notas, estado do form e conectividade CDP.",
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


def _build_notas_company_dir_codigo_dominio(
    run_dir: str,
    tipo: str,
    codigo_dominio: str,
    empresa: str,
) -> str:
    codigo = (codigo_dominio or "").strip()
    nome_limpo = sanitize_folder_name(empresa)

    if codigo:
        pasta = f"{codigo} - {nome_limpo}"
    else:
        pasta = nome_limpo

    destino = os.path.join(run_dir, tipo, pasta)
    ensure_dir(destino)
    return destino


def _build_notas_company_dirs(
    run_dir: str,
    cnpj: str,
    codigo_dominio: str,
    empresa: str,
    *,
    usar_codigo_dominio: bool,
) -> Tuple[str, str, str]:
    """
    Retorna:
    - pasta_base
    - pasta_prestadas
    - pasta_tomadas
    """

    if usar_codigo_dominio:
        pasta_prestadas = _build_notas_company_dir_codigo_dominio(
            run_dir,
            "prestadas",
            codigo_dominio,
            empresa,
        )
        pasta_tomadas = _build_notas_company_dir_codigo_dominio(
            run_dir,
            "tomadas",
            codigo_dominio,
            empresa,
        )

        return run_dir, pasta_prestadas, pasta_tomadas

    cnpj_norm = _norm_cnpj(cnpj)
    nome_limpo = sanitize_folder_name(empresa)
    pasta_base = os.path.join(run_dir, f"{cnpj_norm} - {nome_limpo}")

    pasta_prestadas = os.path.join(pasta_base, "prestadas")
    pasta_tomadas = os.path.join(pasta_base, "tomadas")

    ensure_dir(pasta_base)
    ensure_dir(pasta_prestadas)
    ensure_dir(pasta_tomadas)

    return pasta_base, pasta_prestadas, pasta_tomadas


def remover_canceladas_xml(path_xml: str) -> None:
    try:
        tree = ET.parse(path_xml)
        root = tree.getroot()
    except Exception:
        return

    removidas = 0
    for bloco in list(root):
        tag = bloco.tag.split("}")[-1].lower()
        if tag != "nfse":
            continue

        cancel = bloco.find(".//Cancelamento")
        if cancel is not None:
            root.remove(bloco)
            removidas += 1

    if removidas:
        try:
            tree.write(path_xml, encoding="utf-8", xml_declaration=True)
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

async def pesquisar_empresa(page, cnpj: str, ctx: FlowContext) -> str:
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

    ctx.empresa = nome_emp
    return nome_emp


# ──────────────────────────────────────────────────────────────────────────────
# TELA / MENU / FILTROS
# ──────────────────────────────────────────────────────────────────────────────

async def esperar_overlay_sumir(page, timeout_ms: int = 20_000) -> None:
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        try:
            if not await page.is_visible("#mpProgressoDiv"):
                return
        except Exception:
            return
        await asyncio.sleep(0.4)


async def acessar_menu_nfse_consulta(page, ctx: FlowContext) -> None:
    await log_flow(ctx, "Acessando NFS-e → Consultar NFS-e", event="STEP_DETAIL")

    try:
        await page.click("a.dropdown-toggle:has-text('NFS-e')", timeout=10_000)
        await page.click("a:has-text('Consultar NFS-e')", timeout=10_000)
    except Exception:
        try:
            await page.click("text=NFS-e", timeout=10_000)
            await page.click("text=Consultar NFS-e", timeout=10_000)
        except Exception as e:
            raise FlowError(
                "NFSE_MENU_FAIL",
                f"Falha ao abrir menu NFS-e/Consultar: {e}",
                short_message="Não foi possível acessar o menu de consulta de NFS-e.",
                action="Revisar os seletores do menu NFS-e no portal.",
                retryable=False,
            )

    await page.wait_for_selector("form[id^='consultarnfseForm']", timeout=20_000)
    await asyncio.sleep(0.8)


async def clicar_aba_competencia(page, tipo: str, ctx: FlowContext) -> None:
    await log_flow(ctx, f"Aba competência ({tipo})", event="STEP_DETAIL")
    await esperar_overlay_sumir(page)

    aba_id = (
        "#consultarnfseForm\\:competencia_prestador_tab_lbl"
        if tipo == "prestadas"
        else "#consultarnfseForm\\:abaPorCompetenciaTomador_tab_lbl"
    )

    aba = await page.wait_for_selector(aba_id, timeout=20_000)
    cls = (await aba.get_attribute("class")) or ""
    if "rich-tab-active" not in cls:
        await aba.click()
        await esperar_overlay_sumir(page)
        await asyncio.sleep(0.8)


async def selecionar_competencia(page, tipo: str, mes_num: int, ano: int, ctx: FlowContext) -> None:
    await log_flow(ctx, f"Selecionando competência {mes_num:02}/{ano} ({tipo})", event="STEP_DETAIL")

    prefixo = "competencia" if tipo == "prestadas" else "competenciaTomador"

    btn = (
        "#consultarnfseForm\\:competenciaHeader .rich-calendar-tool-btn"
        if tipo == "prestadas"
        else "#consultarnfseForm\\:competenciaTomadorHeader .rich-calendar-tool-btn"
    )

    await page.click(btn)
    await asyncio.sleep(0.8)

    for i in range(0, 12):
        sel_year = f"#consultarnfseForm\\:{prefixo}DateEditorLayoutY{i}"
        div = await page.query_selector(sel_year)
        if div and (await div.inner_text()).strip() == str(ano):
            await div.click()
            await asyncio.sleep(0.4)
            break

    await page.click(f"#consultarnfseForm\\:{prefixo}DateEditorLayoutM{mes_num - 1}")
    await page.click(f"#consultarnfseForm\\:{prefixo}DateEditorButtonOk")
    await asyncio.sleep(0.8)


async def clicar_consultar(page, ctx: FlowContext) -> None:
    await log_flow(ctx, "Clicando Consultar", event="STEP_DETAIL")
    await esperar_overlay_sumir(page)
    await page.click("input[value='Consultar']")
    await esperar_overlay_sumir(page)
    await asyncio.sleep(0.8)


async def selecionar_tipo(page, tipo: str, ctx: FlowContext) -> None:
    texto = "Serviços Prestados" if tipo == "prestadas" else "Serviços Tomados"
    await log_flow(ctx, f"Selecionando tipo: {texto}", event="STEP_DETAIL")
    await page.wait_for_selector(f"label:has-text('{texto}')", timeout=20_000)
    await page.locator(f"label:has-text('{texto}')").click()
    await asyncio.sleep(0.8)


# ──────────────────────────────────────────────────────────────────────────────
# EXPORTAR XML
# ──────────────────────────────────────────────────────────────────────────────

async def baixar_lotes_xml(page, tipo: str, pasta_tipo_empresa: str, ctx: FlowContext) -> bool:
    await log_flow(ctx, f"Exportando XML ({tipo})", event="STEP_DETAIL")

    msg = await page.query_selector("span.rich-messages-label")
    if msg:
        t = (await msg.inner_text()).strip()
        if "Nenhum registro foi encontrado" in t:
            await log_flow(ctx, f"Sem registros para {tipo}.", event="INFO")
            return False

    ensure_dir(pasta_tipo_empresa)

    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(0.5)

    tds = await page.query_selector_all(
        "#consultarnfseForm\\:dataTable\\:j_id374_table td.rich-datascr-inact, td.rich-datascr-act"
    )
    pages = []
    for td in tds:
        txt = (await td.inner_text()).strip()
        if txt.isdigit():
            pages.append(int(txt))

    total_paginas = max(pages) if pages else 1

    pagina = 1
    lote = 1
    acumulado = 0
    baixou = False

    while True:
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(0.4)

        rows = await page.query_selector_all("#consultarnfseForm\\:dataTable\\:tb tr")
        qtd = len(rows)

        if qtd > 0:
            await esperar_overlay_sumir(page)

            btn_all = await page.query_selector("input#consultarnfseForm\\:j_id324")
            if btn_all:
                try:
                    await btn_all.click()
                except Exception:
                    pass

                await esperar_overlay_sumir(page)
                await asyncio.sleep(0.8)

            acumulado += qtd
            baixou = True

            await log_flow(
                ctx,
                f"Página {pagina}/{total_paginas}: {qtd} linhas (acumulado={acumulado})",
                event="STEP_DETAIL",
            )
        else:
            await log_flow(
                ctx,
                f"Página {pagina}/{total_paginas}: sem linhas",
                event="WARN",
                code="NFSE_EMPTY_PAGE",
            )

        next_btn = await page.query_selector(
            f"td.rich-datascr-button:not(.rich-datascr-button-dsbld):has-text('{pagina + 1}')"
        ) or await page.query_selector("td.rich-datascr-button[onclick*=\"'next'\"]")

        ultima = next_btn is None

        if acumulado >= 100 or (ultima and acumulado > 0):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            nome_arquivo = f"{tipo}_lote{lote:02}_{stamp}.xml"
            destino = os.path.join(pasta_tipo_empresa, nome_arquivo)

            export_selector = "input[name='consultarnfseForm:j_id325']"

            export_btn = await page.query_selector(export_selector)
            if not export_btn:
                raise FlowError(
                    "EXPORT_BUTTON_MISSING",
                    "Botão 'Exportar XML' não encontrado.",
                    short_message="Botão de exportação XML não foi localizado.",
                    action="Revisar os seletores da tela de consulta NFS-e.",
                    retryable=False,
                )

            await esperar_overlay_sumir(page)
            await asyncio.sleep(1.2)

            await log_flow(ctx, f"Exportando lote {lote} ({acumulado})...", event="STEP_DETAIL")

            await baixar_xml_com_fallback(
                page,
                export_selector,
                destino,
                ctx,
                timeout_sec=180,
            )

            if tipo == "tomadas":
                remover_canceladas_xml(destino)

                valido, msg_validacao = _validar_xml_salvo(destino)
                if not valido:
                    raise FlowError(
                        "XML_TOMADAS_INVALIDO_APOS_CANCELADAS",
                        f"XML de tomadas ficou inválido após remover canceladas: {msg_validacao}",
                        short_message="XML de tomadas ficou inválido após tratamento.",
                        action="Verificar se o arquivo tinha somente notas canceladas ou se veio vazio.",
                        retryable=True,
                    )

            acumulado = 0
            lote += 1

        if ultima:
            break

        pagina += 1

        try:
            await next_btn.click()
        except Exception:
            await asyncio.sleep(0.8)
            await next_btn.click()

        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1.0)

    return baixou


# ──────────────────────────────────────────────────────────────────────────────
# JOB PRINCIPAL
# ──────────────────────────────────────────────────────────────────────────────

async def job_notas(
    cnpj: str,
    mes: str,
    usuario: str,
    senha: str,
    run_id: str,
    run_dir: str,
    run_log_file: str,
    *,
    codigo_dominio: str = "",
    usar_codigo_dominio: bool = True,
    headless: bool = True,
) -> Dict[str, Any]:
    cnpj_norm = _norm_cnpj(cnpj)

    ensure_dir(run_dir)

    if usar_codigo_dominio:
        ensure_dir(os.path.join(run_dir, "prestadas"))
        ensure_dir(os.path.join(run_dir, "tomadas"))

    cnpj_dir = run_dir

    config = FlowConfig(
        run_id=run_id,
        run_dir=run_dir,
        run_log_file=run_log_file,
        cnpj_dir=cnpj_dir,
        step_timeout_sec=200,
        nav_timeout_ms=60_000,
        selector_timeout_ms=30_000,
        close_timeout_sec=15,
        goto_retries=3,
        headless=headless,
    )

    ctx = FlowContext(
        flow="notas",
        cnpj=cnpj_norm,
        mes=mes,
        config=config,
    )

    await log_flow(
        ctx,
        (
            f"=== INÍCIO (NOTAS) "
            f"codigo_dominio={codigo_dominio} "
            f"usar_codigo_dominio={usar_codigo_dominio} ==="
        ),
        event="FLOW_START",
    )

    context = None
    closer = None
    page = None

    try:
        context, closer = await create_browser_context(config)
        page = await context.new_page()

        await run_step(ctx, "Login", login(page, usuario, senha, config))

        nome_emp = await run_step(
            ctx,
            "Pesquisar Empresa",
            pesquisar_empresa(page, cnpj_norm, ctx),
        )

        pasta_base, pasta_prestadas, pasta_tomadas = _build_notas_company_dirs(
            run_dir,
            cnpj_norm,
            codigo_dominio,
            nome_emp,
            usar_codigo_dominio=usar_codigo_dominio,
        )

        await log_flow(ctx, f"Empresa selecionada: {nome_emp}", event="INFO")
        await log_flow(ctx, f"Pasta base: {pasta_base}", event="INFO")
        await log_flow(ctx, f"Pasta prestadas: {pasta_prestadas}", event="INFO")
        await log_flow(ctx, f"Pasta tomadas: {pasta_tomadas}", event="INFO")

        await run_step(ctx, "NFS-e: Consultar", acessar_menu_nfse_consulta(page, ctx))

        mes_num, ano = map(int, mes.split("/"))

        await run_step(
            ctx,
            "Prestadas: selecionar tipo",
            selecionar_tipo(page, "prestadas", ctx),
        )
        await run_step(
            ctx,
            "Prestadas: aba competência",
            clicar_aba_competencia(page, "prestadas", ctx),
        )
        await run_step(
            ctx,
            "Prestadas: competência",
            selecionar_competencia(page, "prestadas", mes_num, ano, ctx),
        )
        await run_step(
            ctx,
            "Prestadas: consultar",
            clicar_consultar(page, ctx),
        )
        await run_step(
            ctx,
            "Prestadas: exportar",
            baixar_lotes_xml(page, "prestadas", pasta_prestadas, ctx),
        )

        await run_step(
            ctx,
            "Tomadas: selecionar tipo",
            selecionar_tipo(page, "tomadas", ctx),
        )
        await run_step(
            ctx,
            "Tomadas: aba competência",
            clicar_aba_competencia(page, "tomadas", ctx),
        )
        await run_step(
            ctx,
            "Tomadas: competência",
            selecionar_competencia(page, "tomadas", mes_num, ano, ctx),
        )
        await run_step(
            ctx,
            "Tomadas: consultar",
            clicar_consultar(page, ctx),
        )
        await run_step(
            ctx,
            "Tomadas: exportar",
            baixar_lotes_xml(page, "tomadas", pasta_tomadas, ctx),
        )

        await log_flow(ctx, "=== FIM (OK) ===", event="FLOW_END")

        return {
            "status": "ok",
            "cnpj": cnpj_norm,
            "empresa": nome_emp,
            "pasta": pasta_base,
            "pasta_prestadas": pasta_prestadas,
            "pasta_tomadas": pasta_tomadas,
            "codigo_dominio": codigo_dominio,
            "usar_codigo_dominio": usar_codigo_dominio,
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