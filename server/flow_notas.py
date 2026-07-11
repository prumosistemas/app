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
import json
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


def adaptive_timeout_ms(
    name: str,
    default: int,
    *,
    retry_level: int = 0,
    configured_max_ms: int,
    hard_max_ms: int,
) -> int:
    """Grow portal timeouts on later retries without allowing unbounded hangs."""
    base = portal_timeout_ms(name, default, max_ms=configured_max_ms)
    level = max(0, min(int(retry_level or 0), 5))
    factor = 1.0 + (0.25 * level)
    return min(hard_max_ms, max(base, int(round(base * factor))))


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
# EXPORTAR XML COM CHECKPOINT E PAGINAÇÃO VALIDADA
# ──────────────────────────────────────────────────────────────────────────────

_NFSE_ROWS_SELECTOR = "#consultarnfseForm\\:dataTable\\:tb tr"
_NFSE_ROW_CHECKBOX_SELECTOR = f"{_NFSE_ROWS_SELECTOR} input[type='checkbox']"
_NFSE_SELECT_ALL_SELECTOR = "input#consultarnfseForm\\:j_id324"
_NFSE_EXPORT_SELECTOR = "input[name='consultarnfseForm:j_id325']"
_NFSE_NEXT_SELECTOR = (
    "td.rich-datascr-button:not(.rich-datascr-button-dsbld)[onclick*=\"'next'\"]"
)
_NFSE_CHECKPOINT_VERSION = 1
_NFSE_PAGE_SIZE_EXPECTED = 10

_JS_PAGINATION_SNAPSHOT = """
() => Array.from(document.querySelectorAll('td')).filter((el) => {
    const cls = String(el.className || '');
    return cls.includes('rich-datascr');
}).map((el, index) => ({
    index,
    className: String(el.className || ''),
    text: String(el.textContent || '').trim(),
    onclick: String(el.getAttribute('onclick') || ''),
    title: String(el.getAttribute('title') || ''),
    ariaLabel: String(el.getAttribute('aria-label') || ''),
}));
"""


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


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _keys_digest(keys: set[str]) -> str:
    payload = "\n".join(sorted(keys)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _checkpoint_path(checkpoint_dir: str, tipo: str) -> str:
    return os.path.join(checkpoint_dir, f"{tipo}.json")


def _new_checkpoint(tipo: str, ctx: FlowContext) -> Dict[str, Any]:
    return {
        "version": _NFSE_CHECKPOINT_VERSION,
        "flow": "notas",
        "tipo": tipo,
        "cnpj": ctx.cnpj,
        "mes": ctx.mes,
        "status": "in_progress",
        "last_completed_page": 0,
        "next_page": 1,
        "records_total": 0,
        "files_total": 0,
        "detected_total_pages": None,
        "terminal_reason": None,
        "pages": [],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _save_checkpoint(path: str, checkpoint: Dict[str, Any]) -> None:
    ensure_dir(os.path.dirname(path))
    checkpoint["updated_at"] = datetime.now().isoformat(timespec="seconds")
    temp_path = f"{path}.tmp-{os.getpid()}"
    with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(checkpoint, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _load_checkpoint(path: str, tipo: str, ctx: FlowContext) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return _new_checkpoint(tipo, ctx)

    try:
        with open(path, "r", encoding="utf-8") as handle:
            checkpoint = json.load(handle)
    except Exception as exc:
        raise FlowError(
            "NFSE_CHECKPOINT_READ_FAILED",
            f"Não foi possível ler {path}: {type(exc).__name__}: {exc}",
            short_message="O checkpoint das notas está corrompido.",
            action="Preservar os XMLs e revisar o arquivo de checkpoint antes de retomar.",
            retryable=False,
        )

    expected = {
        "version": _NFSE_CHECKPOINT_VERSION,
        "flow": "notas",
        "tipo": tipo,
        "cnpj": ctx.cnpj,
        "mes": ctx.mes,
    }
    mismatches = {
        key: (checkpoint.get(key), value)
        for key, value in expected.items()
        if checkpoint.get(key) != value
    }
    if mismatches:
        raise FlowError(
            "NFSE_CHECKPOINT_CONTEXT_MISMATCH",
            f"Checkpoint incompatível com a execução atual: {mismatches}",
            short_message="O checkpoint pertence a outro CNPJ, competência ou tipo de nota.",
            action="Não reutilizar esse checkpoint; iniciar uma nova run raiz.",
            retryable=False,
        )

    pages = checkpoint.get("pages")
    if not isinstance(pages, list):
        raise FlowError(
            "NFSE_CHECKPOINT_INVALID",
            "Campo pages ausente ou inválido.",
            short_message="O checkpoint das notas está estruturalmente inválido.",
            action="Revisar o checkpoint antes de retomar.",
            retryable=False,
        )

    ordered = sorted(pages, key=lambda item: int(item.get("page") or 0))
    expected_pages = list(range(1, len(ordered) + 1))
    actual_pages = [int(item.get("page") or 0) for item in ordered]
    if actual_pages != expected_pages:
        raise FlowError(
            "NFSE_CHECKPOINT_NON_CONTIGUOUS",
            f"Páginas do checkpoint não são contíguas: {actual_pages}",
            short_message="O checkpoint possui um salto de páginas.",
            action="Não retomar para evitar lacunas na exportação.",
            retryable=False,
        )

    checkpoint["pages"] = ordered
    checkpoint["last_completed_page"] = len(ordered)
    checkpoint["next_page"] = len(ordered) + 1
    return checkpoint


def _validate_checkpoint_files(
    checkpoint: Dict[str, Any],
) -> tuple[set[str], Dict[str, Dict[str, Any]]]:
    seen_keys: set[str] = set()
    exports_by_digest: Dict[str, Dict[str, Any]] = {}

    for page_entry in checkpoint.get("pages", []):
        path = str(page_entry.get("file") or "")
        if not path or not os.path.isfile(path):
            raise FlowError(
                "NFSE_CHECKPOINT_FILE_MISSING",
                f"Arquivo do checkpoint não existe: {path}",
                short_message="Um XML já confirmado pelo checkpoint desapareceu.",
                action="Restaurar o arquivo ou iniciar uma nova run raiz.",
                retryable=False,
            )

        try:
            count = _contar_nfse_xml(path)
            keys = _chaves_nfse_xml(path)
            file_hash = _sha256_file(path)
        except Exception as exc:
            raise FlowError(
                "NFSE_CHECKPOINT_FILE_INVALID",
                f"Falha ao validar {path}: {type(exc).__name__}: {exc}",
                short_message="Um XML referenciado pelo checkpoint está inválido.",
                action="Restaurar o XML ou iniciar uma nova run raiz.",
                retryable=False,
            )

        if count != int(page_entry.get("records") or 0):
            raise FlowError(
                "NFSE_CHECKPOINT_COUNT_MISMATCH",
                f"{path}: checkpoint={page_entry.get('records')} XML={count}",
                short_message="A quantidade do XML não coincide com o checkpoint.",
                action="Não retomar até verificar a integridade do arquivo.",
                retryable=False,
            )
        if len(keys) != count:
            raise FlowError(
                "NFSE_CHECKPOINT_DUPLICATE_KEYS",
                f"{path}: registros={count}, chaves únicas={len(keys)}",
                short_message="Um XML do checkpoint contém chaves duplicadas.",
                action="Não retomar com arquivo inconsistente.",
                retryable=False,
            )
        if page_entry.get("file_sha256") and page_entry.get("file_sha256") != file_hash:
            raise FlowError(
                "NFSE_CHECKPOINT_FILE_CHANGED",
                f"O hash de {path} mudou desde a confirmação.",
                short_message="Um XML confirmado foi alterado após o checkpoint.",
                action="Não retomar até verificar a alteração.",
                retryable=False,
            )

        overlap = seen_keys.intersection(keys)
        if overlap:
            raise FlowError(
                "NFSE_CHECKPOINT_CROSS_PAGE_DUPLICATE",
                f"O checkpoint repete {len(overlap)} chave(s) entre páginas.",
                short_message="O checkpoint possui notas duplicadas entre páginas.",
                action="Não retomar com checkpoint inconsistente.",
                retryable=False,
            )

        digest = _keys_digest(keys)
        if page_entry.get("keys_sha256") and page_entry.get("keys_sha256") != digest:
            raise FlowError(
                "NFSE_CHECKPOINT_KEYS_CHANGED",
                f"As chaves de {path} mudaram desde a confirmação.",
                short_message="As chaves de um XML não coincidem com o checkpoint.",
                action="Não retomar até verificar o arquivo.",
                retryable=False,
            )

        seen_keys.update(keys)
        exports_by_digest[digest] = {
            "file": path,
            "records": count,
            "keys": keys,
            "file_sha256": file_hash,
        }

    expected_total = sum(int(item.get("records") or 0) for item in checkpoint.get("pages", []))
    checkpoint["records_total"] = expected_total
    checkpoint["files_total"] = len(checkpoint.get("pages", []))
    return seen_keys, exports_by_digest


def _scan_uncheckpointed_exports(
    pasta_tipo_empresa: str,
    tipo: str,
    checkpoint_files: set[str],
    seen_keys: set[str],
) -> Dict[str, Dict[str, Any]]:
    exports: Dict[str, Dict[str, Any]] = {}
    for path in sorted(Path(pasta_tipo_empresa).glob(f"{tipo}_lote*.xml")):
        absolute = str(path.resolve())
        if absolute in checkpoint_files:
            continue
        count = _contar_nfse_xml(absolute)
        keys = _chaves_nfse_xml(absolute)
        if count <= 0 or len(keys) != count:
            raise FlowError(
                "NFSE_ORPHAN_XML_INVALID",
                f"Arquivo não checkpointado inválido: {absolute}",
                short_message="Foi encontrado um XML órfão inconsistente.",
                action="Revisar o arquivo antes de retomar.",
                retryable=False,
            )
        overlap = seen_keys.intersection(keys)
        if overlap:
            raise FlowError(
                "NFSE_ORPHAN_XML_OVERLAP",
                f"Arquivo órfão {absolute} repete {len(overlap)} chave(s) já confirmadas.",
                short_message="Foi encontrado um XML órfão duplicado.",
                action="Remover ou revisar o arquivo antes de retomar.",
                retryable=False,
            )
        digest = _keys_digest(keys)
        exports[digest] = {
            "file": absolute,
            "records": count,
            "keys": keys,
            "file_sha256": _sha256_file(absolute),
        }
        seen_keys.update(keys)
    return exports


def _append_checkpoint_page(
    checkpoint: Dict[str, Any],
    *,
    page: int,
    rows: int,
    fingerprint: str,
    file_path: str,
    records: int,
    keys: set[str],
    pagination: Dict[str, Any],
) -> None:
    expected_page = len(checkpoint.get("pages", [])) + 1
    if page != expected_page:
        raise FlowError(
            "NFSE_CHECKPOINT_APPEND_OUT_OF_ORDER",
            f"Tentativa de gravar página {page}; esperada {expected_page}.",
            short_message="O checkpoint seria gravado fora de ordem.",
            action="Interromper para não criar lacunas.",
            retryable=False,
        )

    checkpoint.setdefault("pages", []).append(
        {
            "page": page,
            "rows": rows,
            "records": records,
            "fingerprint": fingerprint,
            "file": str(Path(file_path).resolve()),
            "file_size": os.path.getsize(file_path),
            "file_sha256": _sha256_file(file_path),
            "keys_sha256": _keys_digest(keys),
            "pagination": pagination,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    checkpoint["status"] = "in_progress"
    checkpoint["last_completed_page"] = page
    checkpoint["next_page"] = page + 1
    checkpoint["records_total"] = sum(
        int(item.get("records") or 0) for item in checkpoint.get("pages", [])
    )
    checkpoint["files_total"] = len(checkpoint.get("pages", []))
    checkpoint["detected_total_pages"] = None
    checkpoint["terminal_reason"] = None


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


async def _fingerprint_grade(page) -> str:
    rows = page.locator(_NFSE_ROWS_SELECTOR)
    try:
        count = await rows.count()
    except Exception:
        return ""
    if count <= 0:
        return ""

    samples = []
    for idx in range(count):
        try:
            samples.append((await rows.nth(idx).inner_text()).strip())
        except Exception:
            samples.append("")
    return hashlib.sha256("\n".join(samples).encode("utf-8", errors="ignore")).hexdigest()


def _is_forward_control(cell: Dict[str, Any]) -> bool:
    marker = " ".join(
        str(cell.get(key) or "").lower()
        for key in ("onclick", "title", "ariaLabel")
    )
    text = str(cell.get("text") or "").strip().lower()
    return (
        "next" in marker
        or "fastforward" in marker
        or text in {">", ">>", "›", "»", "próxima", "proxima"}
    )


async def _pagination_snapshot(page) -> Dict[str, Any]:
    try:
        cells = await page.evaluate(_JS_PAGINATION_SNAPSHOT)
    except Exception:
        cells = []
    if not isinstance(cells, list):
        cells = []

    visible_pages = []
    active_page = None
    forward_disabled = False
    for cell in cells:
        text = str(cell.get("text") or "").strip()
        class_name = str(cell.get("className") or "").lower()
        if text.isdigit():
            number = int(text)
            visible_pages.append(number)
            if "rich-datascr-act" in class_name:
                active_page = number
        if "dsbld" in class_name and _is_forward_control(cell):
            forward_disabled = True

    next_button = await _proximo_botao_nfse(page)
    return {
        "has_paginator": bool(cells),
        "active_page": active_page,
        "visible_pages": sorted(set(visible_pages)),
        "next_enabled": next_button is not None,
        "forward_disabled": forward_disabled,
        "cells": cells,
    }


def _classify_pagination_end(
    snapshot: Dict[str, Any],
    *,
    current_page: int,
    rows: int,
) -> str:
    if snapshot.get("next_enabled"):
        return ""
    if snapshot.get("forward_disabled"):
        return "forward_control_disabled"

    active = snapshot.get("active_page")
    visible = snapshot.get("visible_pages") or []
    active_matches = active in {None, current_page}

    if not snapshot.get("has_paginator") and current_page == 1:
        return "single_page_without_paginator"
    if active_matches and rows < _NFSE_PAGE_SIZE_EXPECTED:
        return "short_final_page"
    if active == current_page and visible and current_page == max(visible) and rows < _NFSE_PAGE_SIZE_EXPECTED:
        return "active_short_last_visible_page"
    return ""


def _compact_pagination_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "has_paginator": bool(snapshot.get("has_paginator")),
        "active_page": snapshot.get("active_page"),
        "visible_pages": list(snapshot.get("visible_pages") or []),
        "next_enabled": bool(snapshot.get("next_enabled")),
        "forward_disabled": bool(snapshot.get("forward_disabled")),
    }


def _cleanup_stale_partial_files(
    pasta_tipo_empresa: str,
    tipo: str,
    checkpoint_dir: str = "",
) -> int:
    company_dir = Path(pasta_tipo_empresa)
    candidate_dirs = {company_dir}

    checkpoint_path = Path(checkpoint_dir) if checkpoint_dir else None
    if checkpoint_path and "_checkpoints" in checkpoint_path.parts:
        checkpoint_index = checkpoint_path.parts.index("_checkpoints")
        root_dir = Path(*checkpoint_path.parts[:checkpoint_index])
        company_folder = company_dir.name
        for attempt in root_dir.glob("tentativa_*"):
            candidate_dirs.add(attempt / "notas" / tipo / company_folder)

    removed = 0
    for directory in candidate_dirs:
        for partial in directory.glob(f"{tipo}_lote*.xml.part"):
            try:
                partial.unlink()
                removed += 1
            except OSError:
                pass
    return removed


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


async def _botao_pagina_nfse(page, target_page: int):
    locator = page.locator("td.rich-datascr-inact, td.rich-datascr-act")
    try:
        count = await locator.count()
    except Exception:
        return None

    wanted = str(target_page)
    for idx in range(count):
        button = locator.nth(idx)
        try:
            text = (await button.inner_text()).strip()
            class_name = ((await button.get_attribute("class")) or "").lower()
            if text == wanted and "rich-datascr-act" not in class_name and await button.is_visible():
                return button
        except Exception:
            continue
    return None


async def _wait_grade_change(page, previous_fingerprint: str, timeout_sec: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while asyncio.get_running_loop().time() < deadline:
        current = await _fingerprint_grade(page)
        if current and current != previous_fingerprint:
            return True
        await asyncio.sleep(0.5)
    return False


async def _pagination_outcome(
    page,
    *,
    current_page: int,
    rows: int,
    ctx: FlowContext,
    timeout_sec: float = 12.0,
) -> tuple[Any, Dict[str, Any], str]:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    last_snapshot: Dict[str, Any] = {}

    while asyncio.get_running_loop().time() < deadline:
        await esperar_overlay_sumir(page, timeout_ms=4_000)
        snapshot = await _pagination_snapshot(page)
        last_snapshot = snapshot

        numeric_button = await _botao_pagina_nfse(page, current_page + 1)
        if numeric_button is not None:
            snapshot["next_enabled"] = True
            snapshot["navigation_mode"] = "numeric"
            return numeric_button, snapshot, ""

        button = await _proximo_botao_nfse(page)
        if button is not None:
            snapshot["next_enabled"] = True
            snapshot["navigation_mode"] = "generic_next"
            return button, snapshot, ""

        reason = _classify_pagination_end(
            snapshot,
            current_page=current_page,
            rows=rows,
        )
        if reason:
            return None, snapshot, reason
        await asyncio.sleep(0.5)

    raise FlowError(
        "NFSE_PAGINATION_END_UNCONFIRMED",
        f"Não foi possível confirmar próxima página nem fim da paginação na página {current_page}: {last_snapshot}",
        short_message="O fim da paginação não pôde ser confirmado.",
        action="Repetir a consulta; não marcar a exportação como concluída sem evidência do paginador.",
        retryable=True,
    )


async def _avancar_pagina_nfse(
    page,
    button,
    previous_fingerprint: str,
    ctx: FlowContext,
    *,
    current_page: int,
) -> None:
    target_page = current_page + 1

    async def click_and_wait(candidate, label: str, timeout_sec: float) -> bool:
        if candidate is None:
            return False
        try:
            await candidate.click(timeout=ctx.config.selector_timeout_ms)
        except Exception as exc:
            await log_flow(
                ctx,
                f"Falha ao navegar para a página {target_page} via {label}: {type(exc).__name__}: {exc}",
                event="WARN",
                code="NFSE_PAGE_NAVIGATION_CLICK_FAILED",
            )
            return False

        await esperar_overlay_sumir(page, timeout_ms=30_000)
        return await _wait_grade_change(page, previous_fingerprint, timeout_sec)

    if await click_and_wait(button, "controle preferencial", 12.0):
        return

    numeric_button = await _botao_pagina_nfse(page, target_page)
    if numeric_button is not None:
        await log_flow(
            ctx,
            f"Controle inicial não alterou a grade; tentando página numérica {target_page}.",
            event="WARN",
            code="NFSE_NUMERIC_PAGE_FALLBACK",
        )
        if await click_and_wait(numeric_button, f"página numérica {target_page}", 20.0):
            return

    generic_next = await _proximo_botao_nfse(page)
    if generic_next is not None:
        await log_flow(
            ctx,
            f"Página numérica indisponível ou sem efeito; repetindo controle próxima para chegar à página {target_page}.",
            event="WARN",
            code="NFSE_GENERIC_NEXT_RETRY",
        )
        if await click_and_wait(generic_next, "controle próxima", 20.0):
            return

    snapshot = await _pagination_snapshot(page)
    raise FlowError(
        "NFSE_PAGINATION_STALLED",
        (
            f"Não foi possível avançar da página {current_page} para {target_page}. "
            f"Estado do paginador: active={snapshot.get('active_page')}, "
            f"visible={snapshot.get('visible_pages')}, "
            f"next_enabled={snapshot.get('next_enabled')}."
        ),
        short_message="A paginação de NFS-e ficou travada.",
        action="Repetir a consulta; o checkpoint preservará todas as páginas já concluídas.",
        retryable=True,
    )


async def baixar_lotes_xml(
    page,
    tipo: str,
    pasta_tipo_empresa: str,
    checkpoint_dir: str,
    ctx: FlowContext,
) -> Dict[str, Any]:
    await log_flow(ctx, f"Exportando XML ({tipo})", event="STEP_DETAIL")
    ensure_dir(pasta_tipo_empresa)
    ensure_dir(checkpoint_dir)

    stale_partials = _cleanup_stale_partial_files(pasta_tipo_empresa, tipo, checkpoint_dir)
    if stale_partials:
        await log_flow(
            ctx,
            f"Removidos {stale_partials} arquivo(s) parcial(is) de execução interrompida.",
            event="INFO",
            code="NFSE_STALE_PARTIALS_REMOVED",
        )

    checkpoint_path = _checkpoint_path(checkpoint_dir, tipo)
    checkpoint = _load_checkpoint(checkpoint_path, tipo, ctx)
    seen_keys, checkpoint_exports = _validate_checkpoint_files(checkpoint)
    checkpoint_files = {
        str(Path(item.get("file") or "").resolve())
        for item in checkpoint.get("pages", [])
        if item.get("file")
    }
    orphan_exports = _scan_uncheckpointed_exports(
        pasta_tipo_empresa,
        tipo,
        checkpoint_files,
        seen_keys,
    )

    if checkpoint.get("pages"):
        await log_flow(
            ctx,
            (
                f"Checkpoint {tipo} carregado: páginas={len(checkpoint['pages'])}, "
                f"registros={checkpoint.get('records_total', 0)}, "
                f"status={checkpoint.get('status')}."
            ),
            event="INFO",
            code="NFSE_CHECKPOINT_LOADED",
        )

    qtd_inicial = await _esperar_grade_nfse(page, ctx)
    if qtd_inicial == 0:
        if checkpoint.get("pages"):
            raise FlowError(
                "NFSE_CHECKPOINT_STALE_EMPTY_PORTAL",
                "O checkpoint possui páginas, mas o portal agora informa ausência de registros.",
                short_message="O conteúdo do portal divergiu do checkpoint.",
                action="Iniciar uma nova run raiz para não misturar conjuntos diferentes.",
                retryable=False,
            )
        checkpoint.update(
            {
                "status": "empty",
                "last_completed_page": 0,
                "next_page": 1,
                "records_total": 0,
                "files_total": 0,
                "detected_total_pages": 0,
                "terminal_reason": "portal_confirmed_no_records",
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        _save_checkpoint(checkpoint_path, checkpoint)
        await log_flow(ctx, f"Sem registros para {tipo}, confirmado pelo portal.", event="INFO")
        return {
            "baixou": False,
            "sem_registros": True,
            "arquivos": 0,
            "registros": 0,
            "paginas": 0,
            "checkpoint": checkpoint_path,
            "terminal_reason": "portal_confirmed_no_records",
        }

    if checkpoint.get("status") == "empty":
        checkpoint = _new_checkpoint(tipo, ctx)
        _save_checkpoint(checkpoint_path, checkpoint)
        await log_flow(
            ctx,
            "O checkpoint vazio foi reaberto porque o portal agora possui registros.",
            event="WARN",
            code="NFSE_CHECKPOINT_EMPTY_REOPENED",
        )

    pagina = 1
    arquivos_novos = 0
    registros_novos = 0
    fingerprints_seen: set[str] = set()

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
        if fingerprint in fingerprints_seen:
            raise FlowError(
                "NFSE_PAGINATION_LOOP",
                f"A página {pagina} repetiu um conteúdo já percorrido nesta sessão.",
                short_message="O paginador entrou em repetição.",
                action="Interromper para não gerar XML duplicado.",
                retryable=True,
            )
        fingerprints_seen.add(fingerprint)

        snapshot_before = await _pagination_snapshot(page)
        active_page = snapshot_before.get("active_page")
        if active_page is not None and active_page != pagina:
            raise FlowError(
                "NFSE_ACTIVE_PAGE_MISMATCH",
                f"Página lógica={pagina}, página ativa do portal={active_page}.",
                short_message="O número da página ativa não coincide com o progresso esperado.",
                action="Interromper para evitar pular ou repetir páginas.",
                retryable=True,
            )

        checkpoint_pages = checkpoint.get("pages", [])
        if pagina <= len(checkpoint_pages):
            expected = checkpoint_pages[pagina - 1]
            if int(expected.get("rows") or 0) != qtd or expected.get("fingerprint") != fingerprint:
                raise FlowError(
                    "NFSE_CHECKPOINT_PAGE_CHANGED",
                    (
                        f"Página {pagina} divergiu do checkpoint: "
                        f"rows atual={qtd}/salvo={expected.get('rows')}, "
                        f"fingerprint atual={fingerprint}/salvo={expected.get('fingerprint')}."
                    ),
                    short_message="Uma página já confirmada mudou no portal.",
                    action="Iniciar nova run raiz para não misturar versões da consulta.",
                    retryable=False,
                )
            await log_flow(
                ctx,
                (
                    f"Página {pagina} retomada do checkpoint: "
                    f"{expected.get('records')} NFS-e já validadas em {expected.get('file')}."
                ),
                event="INFO",
                code="NFSE_CHECKPOINT_PAGE_SKIPPED",
            )
        else:
            await log_flow(
                ctx,
                f"Página {pagina}: {qtd} linha(s); selecionando e exportando esta página.",
                event="STEP_DETAIL",
            )
            selecionadas = await _selecionar_pagina_nfse(page, qtd, ctx)

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            final_name = f"{tipo}_lote{pagina:03}_{stamp}.xml"
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
                f"Exportando página {pagina} como lote {pagina} ({selecionadas} selecionadas)...",
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
                    action="Repetir a página; o lote parcial foi separado e não será aceito.",
                    retryable=True,
                )

            digest = _keys_digest(keys)
            overlap = keys.intersection(seen_keys)
            recovered = orphan_exports.get(digest)
            if recovered and recovered.get("keys") == keys:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                final_path = str(recovered["file"])
                quantidade_xml = int(recovered["records"])
                await log_flow(
                    ctx,
                    f"Página {pagina} recuperada de XML válido gravado antes do checkpoint: {final_path}",
                    event="INFO",
                    code="NFSE_ORPHAN_XML_RECOVERED",
                )
            elif overlap:
                invalid_path = final_path + ".sobreposicao.invalido"
                try:
                    os.replace(temp_path, invalid_path)
                except OSError:
                    pass
                raise FlowError(
                    "NFSE_XML_OVERLAP",
                    f"O XML da página {pagina} repete {len(overlap)} nota(s) já confirmada(s).",
                    short_message="A exportação repetiu notas de páginas anteriores.",
                    action="Interromper para não aceitar duplicidade.",
                    retryable=True,
                )
            else:
                os.replace(temp_path, final_path)
                if tipo == "tomadas":
                    remover_canceladas_xml(final_path)
                    valido, msg_validacao = _validar_xml_salvo(final_path)
                    if not valido:
                        raise FlowError(
                            "XML_TOMADAS_INVALIDO_APOS_CANCELADAS",
                            f"XML de tomadas ficou inválido após remover canceladas: {msg_validacao}",
                            short_message="XML de tomadas ficou inválido após tratamento.",
                            action="Verificar o arquivo exportado.",
                            retryable=True,
                        )
                    quantidade_xml = _contar_nfse_xml(final_path)
                    keys = _chaves_nfse_xml(final_path)
                arquivos_novos += 1
                registros_novos += quantidade_xml

            if len(keys) != quantidade_xml:
                raise FlowError(
                    "NFSE_FINAL_XML_DUPLICATE_KEYS",
                    f"Página {pagina}: registros finais={quantidade_xml}, chaves únicas={len(keys)}.",
                    short_message="O XML final contém chaves duplicadas.",
                    action="Não registrar checkpoint para esse arquivo.",
                    retryable=True,
                )

            seen_keys.update(keys)
            _append_checkpoint_page(
                checkpoint,
                page=pagina,
                rows=qtd,
                fingerprint=fingerprint,
                file_path=final_path,
                records=quantidade_xml,
                keys=keys,
                pagination=_compact_pagination_snapshot(snapshot_before),
            )
            _save_checkpoint(checkpoint_path, checkpoint)
            await log_flow(
                ctx,
                (
                    f"Página {pagina} validada e checkpointada: "
                    f"{quantidade_xml} NFS-e em {final_path}"
                ),
                event="INFO",
                code="NFSE_PAGE_CHECKPOINTED",
            )

        next_button, pagination, terminal_reason = await _pagination_outcome(
            page,
            current_page=pagina,
            rows=qtd,
            ctx=ctx,
        )

        if terminal_reason:
            if len(checkpoint.get("pages", [])) > pagina:
                raise FlowError(
                    "NFSE_CHECKPOINT_HAS_EXTRA_PAGES",
                    f"O portal terminou na página {pagina}, mas o checkpoint possui {len(checkpoint['pages'])} páginas.",
                    short_message="O portal terminou antes do checkpoint.",
                    action="Iniciar uma nova run raiz para não reutilizar um conjunto antigo.",
                    retryable=False,
                )
            checkpoint["status"] = "completed"
            checkpoint["detected_total_pages"] = pagina
            checkpoint["terminal_reason"] = terminal_reason
            checkpoint["completed_at"] = datetime.now().isoformat(timespec="seconds")
            _save_checkpoint(checkpoint_path, checkpoint)
            await log_flow(
                ctx,
                (
                    f"Fim da paginação confirmado: página={pagina}, motivo={terminal_reason}, "
                    f"ativa={pagination.get('active_page')}, visíveis={pagination.get('visible_pages')}."
                ),
                event="INFO",
                code="NFSE_PAGINATION_END_CONFIRMED",
            )
            break

        await _avancar_pagina_nfse(
            page,
            next_button,
            fingerprint,
            ctx,
            current_page=pagina,
        )
        pagina += 1

    await log_flow(
        ctx,
        f"Exportação {tipo} concluída: páginas={pagina}, arquivos_novos={arquivos_novos}, "
        f"registros_novos={registros_novos}, registros_unicos_total={len(seen_keys)}, "
        f"checkpoint={checkpoint_path}.",
        event="INFO",
    )
    return {
        "baixou": bool(seen_keys),
        "sem_registros": False,
        "arquivos": int(checkpoint.get("files_total") or 0),
        "arquivos_novos": arquivos_novos,
        "registros": len(seen_keys),
        "registros_novos": registros_novos,
        "paginas": pagina,
        "checkpoint": checkpoint_path,
        "terminal_reason": checkpoint.get("terminal_reason"),
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
    checkpoint_dir: str = "",
    retry_level: int = 0,
    headless: bool = True,
) -> Dict[str, Any]:
    cnpj_norm = _norm_cnpj(cnpj)

    ensure_dir(run_dir)
    checkpoint_dir = checkpoint_dir or os.path.join(run_dir, ".checkpoints", cnpj_norm, mes.replace("/", "-"))
    ensure_dir(checkpoint_dir)

    if usar_codigo_dominio:
        ensure_dir(os.path.join(run_dir, "prestadas"))
        ensure_dir(os.path.join(run_dir, "tomadas"))

    cnpj_dir = run_dir

    step_timeout_ms = adaptive_timeout_ms(
        "PORTAL_NOTAS_STEP_TIMEOUT_MS",
        240_000,
        retry_level=retry_level,
        configured_max_ms=360_000,
        hard_max_ms=720_000,
    )
    nav_timeout_ms = adaptive_timeout_ms(
        "PORTAL_NAV_TIMEOUT_MS",
        90_000,
        retry_level=retry_level,
        configured_max_ms=180_000,
        hard_max_ms=300_000,
    )
    selector_timeout_ms = adaptive_timeout_ms(
        "PORTAL_SELECTOR_TIMEOUT_MS",
        60_000,
        retry_level=retry_level,
        configured_max_ms=180_000,
        hard_max_ms=240_000,
    )

    config = FlowConfig(
        run_id=run_id,
        run_dir=run_dir,
        run_log_file=run_log_file,
        cnpj_dir=cnpj_dir,
        step_timeout_sec=step_timeout_ms // 1000,
        nav_timeout_ms=nav_timeout_ms,
        selector_timeout_ms=selector_timeout_ms,
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
            f"usar_codigo_dominio={usar_codigo_dominio} "
            f"retry_level={retry_level} timeouts_ms="
            f"step:{step_timeout_ms},nav:{nav_timeout_ms},selector:{selector_timeout_ms} ==="
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
        export_timeout_sec = adaptive_timeout_ms(
            "PORTAL_NOTAS_EXPORT_TIMEOUT_MS",
            1_800_000,
            retry_level=retry_level,
            configured_max_ms=3_600_000,
            hard_max_ms=4_500_000,
        ) // 1000

        resultado_prestadas = await run_step(
            ctx,
            "Prestadas: exportar",
            baixar_lotes_xml(page, "prestadas", pasta_prestadas, checkpoint_dir, ctx),
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
            baixar_lotes_xml(page, "tomadas", pasta_tomadas, checkpoint_dir, ctx),
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
            "checkpoint_dir": checkpoint_dir,
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
