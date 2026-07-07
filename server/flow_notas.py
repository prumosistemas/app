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
import hashlib
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

from flow_core import (
    FlowConfig,
    FlowContext,
    create_browser_context,
    ensure_dir,
    log_flow,
    portal_timeout_ms,
    resilient_goto,
    requests_bootstrap_enabled,
    run_step,
    sanitize_folder_name,
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

    if usar_codigo_dominio and (codigo_dominio or "").strip():
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
    pasta = f"{cnpj_norm} - {nome_limpo}"
    pasta_base = run_dir
    pasta_prestadas = os.path.join(run_dir, "prestadas", pasta)
    pasta_tomadas = os.path.join(run_dir, "tomadas", pasta)

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
            action="Repetir a pesquisa; se persistir, verificar o CNPJ pesquisado e o HTML retornado da grade.",
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
    await settle_portal_page(page, ctx, reason="selecionar empresa notas")

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
    await settle_portal_page(page, ctx, reason="antes menu NFS-e")
    menu_timeout = portal_timeout_ms("PORTAL_MENU_TIMEOUT_MS", 60_000, max_ms=120_000)

    try:
        await page.click("a.dropdown-toggle:has-text('NFS-e')", timeout=menu_timeout)
        await page.click("a:has-text('Consultar NFS-e')", timeout=menu_timeout)
    except Exception:
        try:
            await page.click("text=NFS-e", timeout=menu_timeout)
            await page.click("text=Consultar NFS-e", timeout=menu_timeout)
        except Exception as e:
            raise FlowError(
                "NFSE_MENU_FAIL",
                f"Falha ao abrir menu NFS-e/Consultar: {e}",
                short_message="Não foi possível acessar o menu de consulta de NFS-e.",
                action="Repetir o fluxo; se persistir, revisar os seletores do menu NFS-e no portal.",
                retryable=True,
            )

    await page.wait_for_selector("form[id^='consultarnfseForm']", timeout=ctx.config.selector_timeout_ms)
    await asyncio.sleep(0.8)


async def clicar_aba_competencia(page, tipo: str, ctx: FlowContext) -> None:
    await log_flow(ctx, f"Aba competência ({tipo})", event="STEP_DETAIL")
    await esperar_overlay_sumir(page)

    aba_id = (
        "#consultarnfseForm\\:competencia_prestador_tab_lbl"
        if tipo == "prestadas"
        else "#consultarnfseForm\\:abaPorCompetenciaTomador_tab_lbl"
    )

    aba = await page.wait_for_selector(aba_id, timeout=ctx.config.selector_timeout_ms)
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
    await page.wait_for_selector(f"label:has-text('{texto}')", timeout=ctx.config.selector_timeout_ms)
    await page.locator(f"label:has-text('{texto}')").click()
    await asyncio.sleep(0.8)


# ──────────────────────────────────────────────────────────────────────────────
# EXPORTAR XML
# ──────────────────────────────────────────────────────────────────────────────

_NFSE_ROWS_SELECTOR = "#consultarnfseForm\\:dataTable\\:tb tr"
_NFSE_ROW_CHECKBOX_SELECTOR = f"{_NFSE_ROWS_SELECTOR} input[type='checkbox']"
_NFSE_SELECT_ALL_SELECTOR = "input#consultarnfseForm\\:j_id324"
_NFSE_EXPORT_SELECTOR = "input[name='consultarnfseForm:j_id325']"
_NFSE_NEXT_SELECTOR = (
    "td.rich-datascr-button:not(.rich-datascr-button-dsbld)[onclick*=\"'next'\"]"
)


def _local_tag(tag: str) -> str:
    return (tag or "").split("}")[-1]


def _nfse_top_level_nodes(root: ET.Element) -> list[ET.Element]:
    direct = [child for child in list(root) if _local_tag(child.tag).lower() == "nfse"]
    if direct:
        return direct
    if _local_tag(root.tag).lower() == "nfse":
        return [root]
    return []


def _contar_nfse_xml(caminho: str) -> int:
    tree = ET.parse(caminho)
    return len(_nfse_top_level_nodes(tree.getroot()))


def _chaves_nfse_xml(caminho: str) -> set[str]:
    tree = ET.parse(caminho)
    nodes = _nfse_top_level_nodes(tree.getroot())
    keys: set[str] = set()

    for node in nodes:
        verification = ""
        number = ""
        emission = ""
        for elem in node.iter():
            tag = _local_tag(elem.tag).lower()
            value = (elem.text or "").strip()
            if not value:
                continue
            if not verification and tag == "codigoverificacao":
                verification = value
            elif not number and tag in {"numeronfse", "numero"}:
                number = value
            elif not emission and tag == "dataemissao":
                emission = value

        if verification:
            key = f"verificacao:{verification}"
        elif number and emission:
            key = f"numero-data:{number}:{emission}"
        else:
            raw = ET.tostring(node, encoding="utf-8")
            key = "sha256:" + hashlib.sha256(raw).hexdigest()
        keys.add(key)

    return keys


async def _portal_informa_sem_registros(page) -> bool:
    try:
        messages = await page.locator(
            "span.rich-messages-label, .rich-messages, .alert, .mensagem"
        ).all_inner_texts()
    except Exception:
        messages = []

    normalized = " ".join(messages).lower()
    return (
        "nenhum registro foi encontrado" in normalized
        or "nenhum registro encontrado" in normalized
    )


async def _esperar_grade_nfse(page, ctx: FlowContext, timeout_sec: float = 45.0) -> int:
    deadline = asyncio.get_running_loop().time() + timeout_sec

    while asyncio.get_running_loop().time() < deadline:
        await esperar_overlay_sumir(page, timeout_ms=5_000)
        try:
            qtd = await page.locator(_NFSE_ROWS_SELECTOR).count()
        except Exception:
            qtd = 0

        if qtd > 0:
            return qtd
        if await _portal_informa_sem_registros(page):
            return 0
        await asyncio.sleep(0.6)

    raise FlowError(
        "NFSE_RESULT_GRID_EMPTY",
        "A consulta terminou sem linhas e sem a mensagem oficial de ausência de registros.",
        short_message="A grade de NFS-e não carregou corretamente.",
        action="Repetir a consulta; o portal pode ter devolvido uma resposta AJAX incompleta.",
        retryable=True,
    )


async def _estado_checkboxes_linhas(page) -> tuple[int, int]:
    boxes = page.locator(_NFSE_ROW_CHECKBOX_SELECTOR)
    try:
        total = await boxes.count()
    except Exception:
        return 0, 0

    checked = 0
    for idx in range(total):
        try:
            if await boxes.nth(idx).is_checked():
                checked += 1
        except Exception:
            pass
    return total, checked


async def _selecionar_pagina_nfse(page, qtd_linhas: int, ctx: FlowContext) -> int:
    total_boxes, checked = await _estado_checkboxes_linhas(page)
    if total_boxes >= qtd_linhas and checked >= qtd_linhas:
        return qtd_linhas

    clicked_master = False
    master = page.locator(_NFSE_SELECT_ALL_SELECTOR)
    try:
        if await master.count() > 0:
            await master.first.click(timeout=ctx.config.selector_timeout_ms)
            clicked_master = True
            await esperar_overlay_sumir(page)
            await asyncio.sleep(0.5)
    except Exception as exc:
        await log_flow(
            ctx,
            f"Seleção geral da página falhou; tentando checkboxes individuais: {type(exc).__name__}: {exc}",
            event="WARN",
            code="NFSE_SELECT_ALL_FALLBACK",
        )

    total_boxes, checked = await _estado_checkboxes_linhas(page)
    if total_boxes >= qtd_linhas and checked < qtd_linhas:
        boxes = page.locator(_NFSE_ROW_CHECKBOX_SELECTOR)
        for idx in range(min(total_boxes, qtd_linhas)):
            box = boxes.nth(idx)
            try:
                if not await box.is_checked():
                    await box.click(timeout=ctx.config.selector_timeout_ms)
                    await esperar_overlay_sumir(page, timeout_ms=8_000)
            except Exception as exc:
                raise FlowError(
                    "NFSE_ROW_SELECTION_FAILED",
                    f"Falha ao selecionar a linha {idx + 1}/{qtd_linhas}: {type(exc).__name__}: {exc}",
                    short_message="Não foi possível selecionar todas as notas da página.",
                    action="Repetir a consulta e revisar os controles de seleção do portal.",
                    retryable=True,
                )

        total_boxes, checked = await _estado_checkboxes_linhas(page)

    if total_boxes >= qtd_linhas:
        if checked < qtd_linhas:
            raise FlowError(
                "NFSE_PAGE_SELECTION_INCOMPLETE",
                f"Página com {qtd_linhas} linhas, mas somente {checked} checkbox(es) ficaram marcados.",
                short_message="A seleção das notas ficou incompleta.",
                action="Repetir a página; nenhuma exportação parcial será aceita.",
                retryable=True,
            )
        return qtd_linhas

    if clicked_master:
        await log_flow(
            ctx,
            "O portal não expôs checkboxes verificáveis; a quantidade será validada pelo XML.",
            event="WARN",
            code="NFSE_SELECTION_XML_VALIDATION_ONLY",
        )
        return qtd_linhas

    raise FlowError(
        "NFSE_SELECTION_CONTROL_MISSING",
        "Não foram encontrados checkboxes de linha nem o controle de selecionar todos.",
        short_message="Os controles de seleção das notas não foram localizados.",
        action="Revisar a estrutura da tabela de NFS-e.",
        retryable=False,
    )


async def _limpar_selecao_pagina(page, ctx: FlowContext) -> None:
    total_boxes, checked = await _estado_checkboxes_linhas(page)
    if total_boxes > 0:
        boxes = page.locator(_NFSE_ROW_CHECKBOX_SELECTOR)
        for idx in range(total_boxes):
            box = boxes.nth(idx)
            try:
                if await box.is_checked():
                    await box.click(timeout=ctx.config.selector_timeout_ms)
                    await esperar_overlay_sumir(page, timeout_ms=8_000)
            except Exception as exc:
                raise FlowError(
                    "NFSE_SELECTION_CLEAR_FAILED",
                    f"Falha ao desmarcar a linha {idx + 1}: {type(exc).__name__}: {exc}",
                    short_message="Não foi possível limpar a seleção após exportar.",
                    action="Repetir o fluxo para evitar que notas de páginas anteriores contaminem o próximo XML.",
                    retryable=True,
                )

        _, remaining = await _estado_checkboxes_linhas(page)
        if remaining:
            raise FlowError(
                "NFSE_SELECTION_NOT_CLEARED",
                f"Ainda restaram {remaining} checkbox(es) marcados após a limpeza.",
                short_message="A seleção anterior permaneceu ativa.",
                action="Repetir o fluxo antes de exportar a próxima página.",
                retryable=True,
            )
        return

    # Alguns layouts não expõem checkbox por linha. Neles, o mesmo botão geral
    # costuma alternar a seleção. A validação de duplicidade do próximo XML
    # protege contra uma eventual seleção antiga que permaneça no servidor.
    master = page.locator(_NFSE_SELECT_ALL_SELECTOR)
    try:
        if await master.count() > 0:
            await master.first.click(timeout=ctx.config.selector_timeout_ms)
            await esperar_overlay_sumir(page)
            await asyncio.sleep(0.4)
    except Exception as exc:
        await log_flow(
            ctx,
            f"Não foi possível confirmar a limpeza pelo controle geral: {type(exc).__name__}: {exc}",
            event="WARN",
            code="NFSE_SELECTION_CLEAR_UNVERIFIED",
        )


async def _fingerprint_grade(page) -> str:
    rows = page.locator(_NFSE_ROWS_SELECTOR)
    try:
        count = await rows.count()
    except Exception:
        return ""
    if count <= 0:
        return ""

    samples = []
    for idx in sorted({0, count - 1}):
        try:
            samples.append((await rows.nth(idx).inner_text()).strip())
        except Exception:
            samples.append("")
    return hashlib.sha256("\n".join(samples).encode("utf-8", errors="ignore")).hexdigest()


async def _proximo_botao_nfse(page):
    locator = page.locator(_NFSE_NEXT_SELECTOR)
    try:
        count = await locator.count()
    except Exception:
        return None

    for idx in range(count):
        button = locator.nth(idx)
        try:
            if await button.is_visible():
                return button
        except Exception:
            continue
    return None


async def _avancar_pagina_nfse(page, button, previous_fingerprint: str, ctx: FlowContext) -> None:
    try:
        await button.click(timeout=ctx.config.selector_timeout_ms)
    except Exception as exc:
        raise FlowError(
            "NFSE_NEXT_PAGE_CLICK_FAILED",
            f"Falha ao avançar a paginação: {type(exc).__name__}: {exc}",
            short_message="Não foi possível avançar para a próxima página de notas.",
            action="Repetir o fluxo e revisar o paginador do portal.",
            retryable=True,
        )

    await esperar_overlay_sumir(page, timeout_ms=30_000)
    deadline = asyncio.get_running_loop().time() + 30.0
    while asyncio.get_running_loop().time() < deadline:
        current = await _fingerprint_grade(page)
        if current and current != previous_fingerprint:
            return
        await asyncio.sleep(0.5)

    raise FlowError(
        "NFSE_PAGINATION_STALLED",
        "O botão de próxima página foi acionado, mas a grade não mudou.",
        short_message="A paginação de NFS-e ficou travada.",
        action="Repetir a consulta; o portal pode ter ignorado a requisição AJAX.",
        retryable=True,
    )


async def baixar_lotes_xml(page, tipo: str, pasta_tipo_empresa: str, ctx: FlowContext) -> Dict[str, Any]:
    await log_flow(ctx, f"Exportando XML ({tipo})", event="STEP_DETAIL")
    ensure_dir(pasta_tipo_empresa)

    qtd_inicial = await _esperar_grade_nfse(page, ctx)
    if qtd_inicial == 0:
        await log_flow(ctx, f"Sem registros para {tipo}, confirmado pelo portal.", event="INFO")
        return {
            "baixou": False,
            "sem_registros": True,
            "arquivos": 0,
            "registros": 0,
            "paginas": 0,
        }

    seen_keys: set[str] = set()
    existing_files = sorted(
        path
        for path in Path(pasta_tipo_empresa).glob(f"{tipo}_lote*.xml")
        if path.is_file()
    )
    for existing in existing_files:
        try:
            seen_keys.update(_chaves_nfse_xml(str(existing)))
        except Exception:
            continue

    pagina = 1
    lote = len(existing_files) + 1
    arquivos_novos = 0
    registros_novos = 0
    fingerprints: set[str] = set()

    while True:
        qtd = await _esperar_grade_nfse(page, ctx)
        if qtd <= 0:
            raise FlowError(
                "NFSE_PAGE_UNEXPECTEDLY_EMPTY",
                f"A página {pagina} ficou vazia depois de a consulta já ter retornado registros.",
                short_message="A grade de notas desapareceu durante a paginação.",
                action="Repetir a consulta; não considerar a página vazia como conclusão.",
                retryable=True,
            )

        fingerprint = await _fingerprint_grade(page)
        if not fingerprint:
            raise FlowError(
                "NFSE_PAGE_FINGERPRINT_EMPTY",
                f"Não foi possível identificar o conteúdo da página {pagina}.",
                short_message="Não foi possível validar a página atual de notas.",
                action="Repetir a consulta.",
                retryable=True,
            )
        if fingerprint in fingerprints:
            raise FlowError(
                "NFSE_PAGINATION_LOOP",
                f"A página {pagina} repetiu um conteúdo já processado.",
                short_message="O paginador entrou em repetição.",
                action="Interromper para não gerar XML duplicado e repetir a consulta.",
                retryable=True,
            )
        fingerprints.add(fingerprint)

        await log_flow(
            ctx,
            f"Página {pagina}: {qtd} linha(s); selecionando e exportando esta página.",
            event="STEP_DETAIL",
        )
        selecionadas = await _selecionar_pagina_nfse(page, qtd, ctx)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        final_name = f"{tipo}_lote{lote:03}_{stamp}.xml"
        final_path = os.path.join(pasta_tipo_empresa, final_name)
        temp_path = final_path + ".part"

        export_btn = await page.query_selector(_NFSE_EXPORT_SELECTOR)
        if not export_btn:
            raise FlowError(
                "EXPORT_BUTTON_MISSING",
                "Botão 'Exportar XML' não encontrado.",
                short_message="Botão de exportação XML não foi localizado.",
                action="Revisar os seletores da tela de consulta NFS-e.",
                retryable=False,
            )

        await log_flow(
            ctx,
            f"Exportando página {pagina} como lote {lote} ({selecionadas} selecionadas)...",
            event="STEP_DETAIL",
        )
        await baixar_xml_com_fallback(
            page,
            _NFSE_EXPORT_SELECTOR,
            temp_path,
            ctx,
            timeout_sec=180,
        )

        try:
            quantidade_xml = _contar_nfse_xml(temp_path)
            keys = _chaves_nfse_xml(temp_path)
        except Exception as exc:
            raise FlowError(
                "NFSE_XML_COUNT_FAILED",
                f"Não foi possível contar as NFS-e exportadas: {type(exc).__name__}: {exc}",
                short_message="O XML foi salvo, mas sua quantidade não pôde ser validada.",
                action="Repetir a exportação; arquivos sem validação não serão aceitos.",
                retryable=True,
            )

        if quantidade_xml != selecionadas or len(keys) != quantidade_xml:
            invalid_path = final_path + f".quantidade-{quantidade_xml}-esperado-{selecionadas}.invalido"
            try:
                os.replace(temp_path, invalid_path)
            except OSError:
                pass
            raise FlowError(
                "NFSE_XML_QUANTIDADE_DIVERGENTE",
                f"Foram selecionadas {selecionadas} notas, mas o XML contém "
                f"{quantidade_xml} registro(s) e {len(keys)} chave(s) única(s).",
                short_message="O portal exportou uma quantidade diferente da seleção.",
                action="Repetir a página; o lote parcial foi separado e não será considerado concluído.",
                retryable=True,
            )

        overlap = keys.intersection(seen_keys)
        if overlap:
            if overlap == keys:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                await log_flow(
                    ctx,
                    f"Página {pagina} já estava integralmente salva nesta tentativa; pulando duplicata.",
                    event="INFO",
                    code="NFSE_PAGE_ALREADY_EXPORTED",
                )
            else:
                invalid_path = final_path + ".sobreposicao-parcial.invalido"
                try:
                    os.replace(temp_path, invalid_path)
                except OSError:
                    pass
                raise FlowError(
                    "NFSE_XML_PARTIAL_OVERLAP",
                    f"O XML da página {pagina} mistura {len(overlap)} nota(s) já exportada(s) com notas novas.",
                    short_message="A seleção acumulou notas de páginas diferentes.",
                    action="Limpar a seleção e repetir a consulta para evitar duplicidade.",
                    retryable=True,
                )
        else:
            os.replace(temp_path, final_path)
            seen_keys.update(keys)
            arquivos_novos += 1
            registros_novos += quantidade_xml

            if tipo == "tomadas":
                remover_canceladas_xml(final_path)
                valido, msg_validacao = _validar_xml_salvo(final_path)
                if not valido:
                    raise FlowError(
                        "XML_TOMADAS_INVALIDO_APOS_CANCELADAS",
                        f"XML de tomadas ficou inválido após remover canceladas: {msg_validacao}",
                        short_message="XML de tomadas ficou inválido após tratamento.",
                        action="Verificar se o arquivo tinha somente notas canceladas ou se veio vazio.",
                        retryable=True,
                    )

            await log_flow(
                ctx,
                f"Página {pagina} validada: {quantidade_xml} NFS-e em {final_path}",
                event="INFO",
            )
            lote += 1

        next_button = await _proximo_botao_nfse(page)
        if next_button is None:
            break

        await _avancar_pagina_nfse(page, next_button, fingerprint, ctx)
        pagina += 1

    await log_flow(
        ctx,
        f"Exportação {tipo} concluída: páginas={pagina}, arquivos_novos={arquivos_novos}, "
        f"registros_novos={registros_novos}, registros_unicos_total={len(seen_keys)}.",
        event="INFO",
    )
    return {
        "baixou": bool(seen_keys),
        "sem_registros": False,
        "arquivos": arquivos_novos,
        "registros": len(seen_keys),
        "paginas": pagina,
    }


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
        step_timeout_sec=portal_timeout_ms("PORTAL_NOTAS_STEP_TIMEOUT_MS", 240_000, max_ms=360_000) // 1000,
        nav_timeout_ms=portal_timeout_ms("PORTAL_NAV_TIMEOUT_MS", 90_000, max_ms=180_000),
        selector_timeout_ms=portal_timeout_ms("PORTAL_SELECTOR_TIMEOUT_MS", 60_000, max_ms=180_000),
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
                ),
            )

        if bootstrap:
            nome_emp = bootstrap.empresa
        else:
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
        export_timeout_sec = portal_timeout_ms(
            "PORTAL_NOTAS_EXPORT_TIMEOUT_MS",
            1_800_000,
            max_ms=3_600_000,
        ) // 1000

        resultado_prestadas = await run_step(
            ctx,
            "Prestadas: exportar",
            baixar_lotes_xml(page, "prestadas", pasta_prestadas, ctx),
            timeout_sec=export_timeout_sec,
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
        resultado_tomadas = await run_step(
            ctx,
            "Tomadas: exportar",
            baixar_lotes_xml(page, "tomadas", pasta_tomadas, ctx),
            timeout_sec=export_timeout_sec,
        )

        await log_flow(
            ctx,
            f"Resumo validado: prestadas={resultado_prestadas}; tomadas={resultado_tomadas}",
            event="INFO",
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
            "resultado_prestadas": resultado_prestadas,
            "resultado_tomadas": resultado_tomadas,
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
