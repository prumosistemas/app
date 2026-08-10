#!/usr/bin/env python3
"""Varredura por requests do encerramento da Escrituração no ISS Fortaleza."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from db import db_connect, db_get_json, db_set_json, now_ms
from domain import WorkerContext, get_worker_context, kv_key, load_accounts_raw
from flow_errors import LoginError
from portal_bootstrap import (
    BASE,
    HOME,
    ROOT,
    PortalBootstrapClient,
    clean_text,
    extract_cid,
    extract_view_state,
    only_digits,
)


logger = logging.getLogger("iss.closure_scan")
router = APIRouter(prefix="/api/closure-scans", tags=["iss-closure-scans"])

HISTORY_LIMIT = 5
GLOBAL_REQUEST_WORKERS = max(1, min(int(os.getenv("ISS_CLOSURE_SCAN_GLOBAL_WORKERS", "6")), 12))
PER_ACCOUNT_WORKERS = max(1, min(int(os.getenv("ISS_CLOSURE_SCAN_ACCOUNT_WORKERS", "4")), 8))
COMPANIES_PER_SESSION = max(3, min(int(os.getenv("ISS_CLOSURE_SCAN_CHUNK_SIZE", "12")), 40))
PORTAL_TIMEOUT_SECONDS = max(15, min(int(os.getenv("ISS_CLOSURE_SCAN_TIMEOUT_SECONDS", "45")), 120))
ACTIVE_STATUSES = {"queued", "running", "stopping"}

_NETWORK_SLOTS = threading.BoundedSemaphore(GLOBAL_REQUEST_WORKERS)
_STATE_LOCK = threading.RLock()
_TASKS_LOCK = threading.RLock()
_TASKS: Dict[str, asyncio.Task] = {}
_STOP_FLAGS: Dict[str, threading.Event] = {}


class ClosureScanCreateRequest(BaseModel):
    account_ids: List[str] = Field(..., min_length=1, max_length=50)


def _task_id(ctx: WorkerContext, run_id: str) -> str:
    return f"{ctx.company_id}:{ctx.user_id}:{run_id}"


def _state_key(ctx: WorkerContext) -> str:
    return kv_key(ctx, "closure_scans")


def _load_runs(ctx: WorkerContext) -> List[Dict[str, Any]]:
    payload = db_get_json(_state_key(ctx), {"runs": []})
    runs = payload.get("runs", []) if isinstance(payload, dict) else []
    return [item for item in runs if isinstance(item, dict)]


def _save_runs(ctx: WorkerContext, runs: List[Dict[str, Any]]) -> None:
    ordered = sorted(runs, key=lambda item: int(item.get("created_at") or 0), reverse=True)
    active = [item for item in ordered if item.get("status") in ACTIVE_STATUSES]
    final = [item for item in ordered if item.get("status") not in ACTIVE_STATUSES]
    # Runs ativas nunca são apagadas no meio da execução; terminadas são podadas
    # primeiro e, ao finalizar, o conjunto volta exatamente ao limite de cinco.
    kept = (active + final[: max(0, HISTORY_LIMIT - len(active))])[: max(HISTORY_LIMIT, len(active))]
    kept.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)
    db_set_json(_state_key(ctx), {"updated_at": now_ms(), "limit": HISTORY_LIMIT, "runs": kept})


def _find_run(runs: List[Dict[str, Any]], run_id: str) -> Optional[Dict[str, Any]]:
    return next((item for item in runs if item.get("run_id") == run_id), None)


def _mutate_run(ctx: WorkerContext, run_id: str, mutator: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    with _STATE_LOCK:
        runs = _load_runs(ctx)
        run = _find_run(runs, run_id)
        if run is None:
            raise KeyError(run_id)
        mutator(run)
        run["updated_at"] = now_ms()
        _save_runs(ctx, runs)
        return dict(run)


def _public_summary(run: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in run.items() if key != "results"}


def _new_run_id() -> str:
    return f"scan_{secrets.token_urlsafe(12)}"


def _safe_error(exc: BaseException) -> str:
    text = re.sub(r"https?://\S+", "portal", str(exc or "Erro desconhecido"))
    text = re.sub(r"(?i)(password|senha)=[^\s&]+", r"\1=[redigido]", text)
    return re.sub(r"\s+", " ", text).strip()[:600]


def _competence_range() -> tuple[str, str]:
    now = datetime.now(ZoneInfo("America/Fortaleza"))
    current = f"{now.month:02d}/{now.year}"
    if now.month == 1:
        previous = f"12/{now.year - 1}"
    else:
        previous = f"{now.month - 1:02d}/{now.year}"
    return previous, current


def _is_login_page(text: str) -> bool:
    return bool(re.search(r"login-actions/authenticate|kc-form-login|Por favor,\s*identifique-se", text or "", re.I))


def _is_view_expired(text: str) -> bool:
    return bool(re.search(r"errorViewExpired\.seam|ViewExpiredException", text or "", re.I))


def _is_recoverable_error(exc: BaseException) -> bool:
    return bool(
        re.search(
            r"ViewExpired|sess[aã]o|login|ViewState|HTTP 5\d\d|timeout|temporar|connection|reset|portal",
            str(exc or ""),
            re.I,
        )
    ) and not isinstance(exc, LoginError)


def _has_company_table(text: str) -> bool:
    return "alteraInscricaoForm:empresaDataTable" in (text or "")


def _open_company_modal(client: PortalBootstrapClient) -> tuple[str, str]:
    home = client.get(HOME)
    text = home.text
    view_state = extract_view_state(text)
    if not view_state:
        raise RuntimeError("ViewState da HOME não encontrado.")
    if _has_company_table(text):
        return text, view_state

    retry = client.get(HOME)
    retry_text = retry.text
    retry_state = extract_view_state(retry_text) or view_state
    if _has_company_table(retry_text):
        return retry_text, retry_state

    response = client.post(
        HOME,
        data=OrderedDict(
            {
                "AJAXREQUEST": "_viewRoot",
                "j_id157": "j_id157",
                "javax.faces.ViewState": retry_state,
                "ajaxSingle": "j_id157:j_id159",
                "j_id157:j_id159": "j_id157:j_id159",
                "AJAX:EVENTS_COUNT": "1",
                "": "",
            }
        ),
        headers=client.ajax_headers(HOME),
    )
    if _is_login_page(response.text) or _is_view_expired(response.text):
        raise RuntimeError("Sessão expirou ao abrir a lista de empresas.")
    if not _has_company_table(response.text):
        raise RuntimeError("O ISS não devolveu a tabela de empresas.")
    return response.text, extract_view_state(response.text) or retry_state


def _find_scroller_id(text: str) -> str:
    candidates = re.findall(r"(alteraInscricaoForm:empresaDataTable:j_id\d+)", text or "")
    for candidate in reversed(candidates):
        if candidate in (text or "") and re.search(re.escape(candidate) + r"[^\n]{0,240}(?:last|next|page)", text or "", re.I):
            return candidate
    return "alteraInscricaoForm:empresaDataTable:j_id389"


def _fetch_companies_page(client: PortalBootstrapClient, view_state: str, page: object, scroller: str) -> str:
    response = client.post(
        HOME,
        data=OrderedDict(
            {
                "AJAXREQUEST": "_viewRoot",
                "alteraInscricaoForm": "alteraInscricaoForm",
                "alteraInscricaoForm:cpfPesquisa": "",
                "alteraInscricaoForm:sugestaoPesquisa_selection": "",
                "alteraInscricaoForm:tipoPesquisa": "CPF",
                "alteraInscricaoForm:confirmaAlteraInscricaoAtualModalOpenedState": "",
                "javax.faces.ViewState": view_state,
                "ajaxSingle": scroller,
                scroller: str(page),
                "AJAX:EVENTS_COUNT": "1",
                "": "",
            }
        ),
        headers=client.ajax_headers(HOME),
    )
    if _is_login_page(response.text) or _is_view_expired(response.text):
        raise RuntimeError(f"Sessão expirada ao paginar empresas ({page}).")
    return response.text


def _parse_companies(text: str, page: int) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(text or "", "html.parser")
    tbody = soup.find("tbody", id="alteraInscricaoForm:empresaDataTable:tb")
    if tbody is None:
        return []
    output: List[Dict[str, Any]] = []
    for row in tbody.find_all("tr"):
        name_link = row.find("a", id=re.compile(r"empresaDataTable:\d+:linkNome"))
        if name_link is None:
            continue
        match = re.search(r"empresaDataTable:(\d+):linkNome", name_link.get("id", ""))
        doc_link = row.find("a", id=re.compile(r":linkDocumento$"))
        inscription_link = row.find("a", id=re.compile(r":linkInscricao$"))
        cnpj = clean_text(str(doc_link or ""))
        digits = only_digits(cnpj)
        if not match or not digits:
            continue
        output.append(
            {
                "page": page,
                "idx": int(match.group(1)),
                "cnpj": cnpj,
                "cnpj_digits": digits,
                "inscricao": clean_text(str(inscription_link or "")),
                "nome": clean_text(str(name_link)),
            }
        )
    return output


def _active_page(text: str) -> int:
    soup = BeautifulSoup(text or "", "html.parser")
    active = soup.select_one("td.rich-datascr-act")
    try:
        return int(clean_text(str(active or "")))
    except (TypeError, ValueError):
        return 0


def _fetch_discovery_chunk(
    account: Dict[str, Any],
    pages: List[int],
    stop: threading.Event,
    page_done: Callable[[], None],
    existing: Optional[tuple[PortalBootstrapClient, str, str]] = None,
) -> List[Dict[str, Any]]:
    with _NETWORK_SLOTS:
        if stop.is_set():
            return []
        if existing is None:
            client = PortalBootstrapClient(timeout=PORTAL_TIMEOUT_SECONDS)
            client.login(account.get("usuario", ""), account.get("senha", ""))
            modal_html, view_state = _open_company_modal(client)
        else:
            client, modal_html, view_state = existing
        scroller = _find_scroller_id(modal_html)
        companies: List[Dict[str, Any]] = []
        for page in pages:
            if stop.is_set():
                break
            page_html = _fetch_companies_page(client, view_state, page, scroller)
            companies.extend(_parse_companies(page_html, page))
            page_done()
        return companies


def _discover_companies(
    account: Dict[str, Any],
    stop: threading.Event,
    progress: Optional[Callable[[int, int], None]] = None,
) -> List[Dict[str, Any]]:
    last_error: Optional[BaseException] = None
    for attempt in range(3):
        if stop.is_set():
            return []
        try:
            with _NETWORK_SLOTS:
                client = PortalBootstrapClient(timeout=PORTAL_TIMEOUT_SECONDS)
                client.login(account.get("usuario", ""), account.get("senha", ""))
                first_html, view_state = _open_company_modal(client)
                scroller = _find_scroller_id(first_html)
                last_html = _fetch_companies_page(client, view_state, "last", scroller)
            last_page = max(1, _active_page(last_html))
            seen: set[str] = set()
            companies = _parse_companies(first_html, 1)
            if last_page > 1:
                companies.extend(_parse_companies(last_html, last_page))

            middle_pages = list(range(2, last_page))
            completed_pages = 1 if last_page == 1 else 2
            progress_lock = threading.Lock()
            last_progress_at = 0.0

            def page_done() -> None:
                nonlocal completed_pages, last_progress_at
                with progress_lock:
                    completed_pages += 1
                    now = time.monotonic()
                    if progress and (now - last_progress_at >= 2.0 or completed_pages >= last_page):
                        last_progress_at = now
                        progress(completed_pages, last_page)

            if progress:
                progress(completed_pages, last_page)
            if middle_pages:
                worker_count = min(PER_ACCOUNT_WORKERS, len(middle_pages))
                page_groups = [middle_pages[index::worker_count] for index in range(worker_count)]
                with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="closure-discovery") as executor:
                    futures = []
                    for index, pages in enumerate(page_groups):
                        existing = (client, first_html, view_state) if index == 0 else None
                        futures.append(executor.submit(_fetch_discovery_chunk, account, pages, stop, page_done, existing))
                    for future in as_completed(futures):
                        companies.extend(future.result())

            unique: List[Dict[str, Any]] = []
            for company in sorted(companies, key=lambda item: (int(item.get("page") or 0), int(item.get("idx") or 0))):
                key = company["cnpj_digits"]
                if key not in seen:
                    seen.add(key)
                    unique.append(company)
            companies = unique
            if not companies:
                raise RuntimeError("Nenhuma empresa foi encontrada nesta conta do ISS.")
            return companies
        except LoginError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(_safe_error(last_error or RuntimeError("Falha ao listar empresas.")))


def _search_company(client: PortalBootstrapClient, view_state: str, cnpj: str) -> tuple[Dict[str, Any], str]:
    common = OrderedDict(
        {
            "AJAXREQUEST": "_viewRoot",
            "alteraInscricaoForm": "alteraInscricaoForm",
            "alteraInscricaoForm:cpfPesquisa": cnpj,
            "alteraInscricaoForm:sugestaoPesquisa_selection": "",
            "alteraInscricaoForm:tipoPesquisa": "CNPJ",
            "javax.faces.ViewState": view_state,
            "AJAX:EVENTS_COUNT": "1",
        }
    )

    suggestion_data = OrderedDict(common)
    suggestion_data.update(
        {
            "alteraInscricaoForm:sugestaoPesquisa": "alteraInscricaoForm:sugestaoPesquisa",
            "ajaxSingle": "alteraInscricaoForm:sugestaoPesquisa",
            "inputvalue": cnpj,
        }
    )
    suggestion = client.post(HOME, data=suggestion_data, headers=client.ajax_headers(HOME))
    if _is_login_page(suggestion.text) or _is_view_expired(suggestion.text):
        raise RuntimeError("Sessão expirada ao buscar CNPJ.")
    state = extract_view_state(suggestion.text) or view_state

    selected_data = OrderedDict(common)
    selected_data["javax.faces.ViewState"] = state
    selected_data["ajaxSingle"] = "alteraInscricaoForm:sugestaoPesquisa"
    selected_data["alteraInscricaoForm:sugestaoPesquisa:j_id353"] = "alteraInscricaoForm:sugestaoPesquisa:j_id353"
    selected = client.post(HOME, data=selected_data, headers=client.ajax_headers(HOME))
    if _is_login_page(selected.text) or _is_view_expired(selected.text):
        raise RuntimeError("Sessão expirada ao selecionar CNPJ.")
    state = extract_view_state(selected.text) or state

    searched_data = OrderedDict(common)
    searched_data["alteraInscricaoForm:confirmaAlteraInscricaoAtualModalOpenedState"] = ""
    searched_data["javax.faces.ViewState"] = state
    searched_data["alteraInscricaoForm:btnPesquisar"] = "alteraInscricaoForm:btnPesquisar"
    searched_data[""] = ""
    searched = client.post(HOME, data=searched_data, headers=client.ajax_headers(HOME))
    if _is_login_page(searched.text) or _is_view_expired(searched.text):
        raise RuntimeError("Sessão expirada ao pesquisar CNPJ.")
    state = extract_view_state(searched.text) or state
    digits = only_digits(cnpj)
    companies = [item for item in _parse_companies(searched.text, 1) if item["cnpj_digits"] == digits]
    if not companies:
        raise RuntimeError("CNPJ não encontrado nesta sessão do ISS.")
    return companies[0], state


def _parse_table_rows(text: str, tbody_id: str) -> List[List[str]]:
    soup = BeautifulSoup(text or "", "html.parser")
    tbody = soup.find("tbody", id=tbody_id)
    if tbody is None:
        return []
    rows: List[List[str]] = []
    for tr in tbody.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all("td")]
        if len(cells) >= 4:
            rows.append(cells)
    return rows


def _extract_messages(text: str) -> List[str]:
    messages: List[str] = []
    for cells in _parse_table_rows(text, "mensagensForm:mensagemDataTable:tb"):
        if len(cells) > 1 and cells[1].startswith("Mensagem de Alerta para"):
            message = f"{cells[1]} | {cells[2]}" if len(cells) > 2 and cells[2] else cells[1]
            if message not in messages:
                messages.append(message)
    return messages


def _has_message_modal(text: str) -> bool:
    return bool(re.search(r"mensagensModalContentDiv[\s\S]{0,1200}(Mensagem|sess[aã]o|opera[cç][aã]o)", text or "", re.I))


def _resolve_company(
    client: PortalBootstrapClient,
    modal_html: str,
    modal_state: str,
    original: Dict[str, Any],
) -> tuple[Dict[str, Any], str]:
    page = int(original.get("page") or 0)
    if page > 0 and original.get("idx") is not None:
        page_html = modal_html if page == 1 else _fetch_companies_page(
            client,
            modal_state,
            page,
            _find_scroller_id(modal_html),
        )
        state = extract_view_state(page_html) or modal_state
        listed = _parse_companies(page_html, page)
        resolved = next(
            (item for item in listed if item.get("cnpj_digits") == original.get("cnpj_digits")),
            None,
        )
        if resolved is None and not listed:
            resolved = dict(original)
        if resolved is not None:
            resolved["nome"] = resolved.get("nome") or original.get("nome", "")
            return resolved, state
    return _search_company(client, modal_state, original["cnpj_digits"])


def _consult_company(client: PortalBootstrapClient, state: str, company: Dict[str, Any]) -> Dict[str, Any]:
    link = f"alteraInscricaoForm:empresaDataTable:{company['idx']}:linkNome"
    selected = client.post(
        HOME,
        data=OrderedDict(
            {
                "AJAXREQUEST": "_viewRoot",
                "alteraInscricaoForm": "alteraInscricaoForm",
                "alteraInscricaoForm:cpfPesquisa": "",
                "alteraInscricaoForm:sugestaoPesquisa_selection": "",
                "alteraInscricaoForm:tipoPesquisa": "CPF",
                "alteraInscricaoForm:confirmaAlteraInscricaoAtualModalOpenedState": "",
                "javax.faces.ViewState": state,
                link: link,
                "conversationPropagation": "none",
                "AJAX:EVENTS_COUNT": "1",
                "": "",
            }
        ),
        headers=client.ajax_headers(HOME),
        allow_redirects=True,
    )
    if _is_login_page(selected.text):
        raise RuntimeError("Sessão expirada ao selecionar empresa.")
    if _is_view_expired(selected.text) or _has_message_modal(selected.text):
        return {"pendencias": [], "status": "FECHADO"}
    cid = extract_cid(selected.text, selected.url)
    if not cid:
        raise RuntimeError("CID da empresa não encontrado.")

    url = f"{ROOT}/pages/escrituracao/manterEscrituracao.seam?cid={cid}"
    page = client.get(url)
    if _is_login_page(page.text):
        raise RuntimeError("Sessão expirada ao abrir Escrituração.")
    if _is_view_expired(page.text):
        return {"pendencias": [], "status": "FECHADO"}
    messages = _extract_messages(page.text)
    if messages:
        return {
            "status": "ABERTO_MENSAGEM",
            "pendencias": [{"origem": "mensagem_tela", "situacao": message} for message in messages],
        }

    previous, current = _competence_range()
    page_state = extract_view_state(page.text)
    if not page_state:
        raise RuntimeError("ViewState da Escrituração não encontrado.")
    consulted = client.post(
        url,
        data=OrderedDict(
            {
                "manterEscrituracaoForm": "manterEscrituracaoForm",
                "manterEscrituracaoForm:dataInicialInputDate": previous,
                "manterEscrituracaoForm:dataInicialInputCurrentDate": previous,
                "manterEscrituracaoForm:dataFinalInputDate": current,
                "manterEscrituracaoForm:dataFinalInputCurrentDate": current,
                "manterEscrituracaoForm:btnConsultar": "Consultar",
                "manterEscrituracaoForm:exportar_escrituracao_modal_panelOpenedState": "",
                "manterEscrituracaoForm:comboEscolherTipoExportacao": "1",
                "manterEscrituracaoForm:certificado_encerramento_modal_panelOpenedState": "",
                "javax.faces.ViewState": page_state,
            }
        ),
    )
    if _is_login_page(consulted.text):
        raise RuntimeError("Sessão expirada ao consultar encerramentos.")
    if _is_view_expired(consulted.text):
        raise RuntimeError("ViewExpired ao consultar encerramentos.")

    pending = [
        {
            "competencia": cells[0],
            "situacao": cells[1],
            "data_encerramento": cells[2],
            "data_situacao": cells[3],
            "origem": "tabela_pendentes",
        }
        for cells in _parse_table_rows(consulted.text, "manterEscrituracaoForm:dataTablePendentes:tb")
    ]
    writings = [
        {
            "competencia": cells[0],
            "situacao": cells[1],
            "data_encerramento": cells[2],
            "data_situacao": cells[3],
        }
        for cells in _parse_table_rows(consulted.text, "manterEscrituracaoForm:dataTable:tb")
    ]
    if not pending:
        for record in writings:
            if record["competencia"] == previous and re.match(r"Aberta", record["situacao"], re.I) and not record["data_encerramento"]:
                pending.append({**record, "origem": "mes_anterior_aberto"})
                break
    return {"pendencias": pending, "status": "ABERTO" if pending else "FECHADO"}


def _company_result(
    client: PortalBootstrapClient,
    modal_html: str,
    modal_state: str,
    company: Dict[str, Any],
) -> Dict[str, Any]:
    resolved, state = _resolve_company(client, modal_html, modal_state, company)
    result = _consult_company(client, state, resolved)
    valid = [item for item in result["pendencias"] if item.get("origem") != "mensagem_tela"]
    messages = [item.get("situacao", "") for item in result["pendencias"] if item.get("origem") == "mensagem_tela"]
    return {
        "cnpj": company.get("cnpj", ""),
        "cnpj_digits": company.get("cnpj_digits", ""),
        "inscricao": company.get("inscricao", ""),
        "nome": company.get("nome", ""),
        "status": "ABERTO" if valid else result.get("status", "FECHADO"),
        "qtd_pendencias": len(valid),
        "competencias_pendentes": sorted({item.get("competencia", "") for item in valid if item.get("competencia")}),
        "mensagens": messages[:5],
    }


def _open_analysis_session(account: Dict[str, Any]) -> tuple[PortalBootstrapClient, str, str]:
    client = PortalBootstrapClient(timeout=PORTAL_TIMEOUT_SECONDS)
    client.login(account.get("usuario", ""), account.get("senha", ""))
    modal_html, modal_state = _open_company_modal(client)
    return client, modal_html, modal_state


def _analyze_chunk(account: Dict[str, Any], companies: List[Dict[str, Any]], stop: threading.Event) -> List[Dict[str, Any]]:
    with _NETWORK_SLOTS:
        if stop.is_set():
            return []
        try:
            client, modal_html, modal_state = _open_analysis_session(account)
        except Exception as exc:
            message = _safe_error(exc)
            return [{**company, "status": "ERRO", "qtd_pendencias": 0, "competencias_pendentes": [], "erro": message} for company in companies]

        results: List[Dict[str, Any]] = []
        for company in companies:
            if stop.is_set():
                break
            try:
                results.append(_company_result(client, modal_html, modal_state, company))
                continue
            except Exception as first_error:
                if not _is_recoverable_error(first_error):
                    results.append({**company, "status": "ERRO", "qtd_pendencias": 0, "competencias_pendentes": [], "erro": _safe_error(first_error)})
                    continue
            try:
                client, modal_html, modal_state = _open_analysis_session(account)
                results.append(_company_result(client, modal_html, modal_state, company))
            except Exception as second_error:
                results.append({**company, "status": "ERRO", "qtd_pendencias": 0, "competencias_pendentes": [], "erro": _safe_error(second_error)})
        return results


def _recalculate_run(run: Dict[str, Any]) -> None:
    results = run.get("results", [])
    run["processed"] = len(results)
    run["total"] = sum(int(account.get("total") or 0) for account in run.get("accounts", []))
    run["open"] = sum(1 for item in results if item.get("status") in {"ABERTO", "ABERTO_MENSAGEM"})
    run["closed"] = sum(1 for item in results if item.get("status") == "FECHADO")
    run["errors"] = sum(1 for item in results if item.get("status") == "ERRO")


def _update_account(ctx: WorkerContext, run_id: str, account_id: str, **values: Any) -> None:
    def mutate(run: Dict[str, Any]) -> None:
        account = next((item for item in run.get("accounts", []) if item.get("account_id") == account_id), None)
        if account is not None:
            account.update(values)
        _recalculate_run(run)

    _mutate_run(ctx, run_id, mutate)


def _append_results(ctx: WorkerContext, run_id: str, account: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
    def mutate(run: Dict[str, Any]) -> None:
        enriched = [
            {**item, "account_id": account.get("id", ""), "account_alias": account.get("alias", "")}
            for item in results
        ]
        run.setdefault("results", []).extend(enriched)
        summary = next((item for item in run.get("accounts", []) if item.get("account_id") == account.get("id")), None)
        if summary is not None:
            summary["processed"] = int(summary.get("processed") or 0) + len(enriched)
            summary["open"] = int(summary.get("open") or 0) + sum(1 for item in enriched if item.get("status") in {"ABERTO", "ABERTO_MENSAGEM"})
            summary["closed"] = int(summary.get("closed") or 0) + sum(1 for item in enriched if item.get("status") == "FECHADO")
            summary["errors"] = int(summary.get("errors") or 0) + sum(1 for item in enriched if item.get("status") == "ERRO")
            summary["progress"] = f"Analisadas {summary['processed']} de {summary.get('total', 0)} empresa(s)."
        _recalculate_run(run)

    _mutate_run(ctx, run_id, mutate)


def _scan_account(ctx: WorkerContext, run_id: str, account: Dict[str, Any], stop: threading.Event) -> None:
    account_id = str(account.get("id", ""))
    _update_account(ctx, run_id, account_id, status="discovering", progress="Listando empresas no ISS...")
    try:
        companies = _discover_companies(
            account,
            stop,
            lambda completed, total: _update_account(
                ctx,
                run_id,
                account_id,
                progress=f"Listando empresas: {completed}/{total} página(s)...",
            ),
        )
    except Exception as exc:
        _update_account(ctx, run_id, account_id, status="failed", errors=1, error=_safe_error(exc), progress="Falha ao listar empresas.")
        return
    if stop.is_set():
        _update_account(ctx, run_id, account_id, status="cancelled", progress="Interrompida pelo usuário.")
        return

    _update_account(
        ctx,
        run_id,
        account_id,
        status="running",
        total=len(companies),
        progress=f"{len(companies)} empresa(s) encontradas. Iniciando análise...",
    )
    chunks = [companies[index : index + COMPANIES_PER_SESSION] for index in range(0, len(companies), COMPANIES_PER_SESSION)]
    workers = min(PER_ACCOUNT_WORKERS, len(chunks))
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="closure-account") as executor:
        futures = [executor.submit(_analyze_chunk, account, chunk, stop) for chunk in chunks]
        for future in as_completed(futures):
            if stop.is_set():
                for pending in futures:
                    pending.cancel()
                break
            try:
                _append_results(ctx, run_id, account, future.result())
            except Exception as exc:
                logger.exception("Falha ao consolidar lote da varredura: %s", _safe_error(exc))

    latest = _find_run(_load_runs(ctx), run_id) or {}
    summary = next((item for item in latest.get("accounts", []) if item.get("account_id") == account_id), {})
    if stop.is_set():
        _update_account(ctx, run_id, account_id, status="cancelled", progress="Interrompida pelo usuário.")
    elif int(summary.get("errors") or 0):
        _update_account(ctx, run_id, account_id, status="finished_with_errors", progress="Concluída com alguns erros.")
    else:
        _update_account(ctx, run_id, account_id, status="finished", progress="Concluída.")


def _execute_run_sync(ctx: WorkerContext, run_id: str, stop: threading.Event) -> None:
    try:
        _mutate_run(ctx, run_id, lambda run: run.update(status="running", started_at=run.get("started_at") or now_ms(), progress="Preparando contas..."))
        run = _find_run(_load_runs(ctx), run_id) or {}
        selected_ids = [str(value) for value in run.get("account_ids", [])]
        account_map = {str(account.get("id")): account for account in load_accounts_raw(ctx)}
        selected = [account_map[account_id] for account_id in selected_ids if account_id in account_map]
        missing = [account_id for account_id in selected_ids if account_id not in account_map]
        for account_id in missing:
            _update_account(ctx, run_id, account_id, status="failed", errors=1, error="Conta removida ou indisponível.", progress="Conta não encontrada.")
        if not selected:
            raise RuntimeError("Nenhuma conta selecionada continua disponível.")

        # Duas contas podem descobrir/analisar em paralelo. O semáforo global de
        # rede continua sendo o limitador real entre todos os usuários e runs.
        with ThreadPoolExecutor(max_workers=min(2, len(selected)), thread_name_prefix="closure-run") as executor:
            futures = [executor.submit(_scan_account, ctx, run_id, account, stop) for account in selected]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    logger.exception("Conta falhou na varredura: %s", _safe_error(exc))

        def finish(run: Dict[str, Any]) -> None:
            _recalculate_run(run)
            run["results"] = sorted(
                run.get("results", []),
                key=lambda item: (str(item.get("account_alias", "")).lower(), str(item.get("nome", "")).lower(), str(item.get("cnpj_digits", ""))),
            )
            run["finished_at"] = now_ms()
            if stop.is_set() or run.get("stop_requested"):
                run["status"] = "cancelled"
                run["progress"] = "Verificação interrompida. Resultados concluídos foram preservados."
            elif run.get("errors"):
                run["status"] = "finished_with_errors"
                run["progress"] = "Verificação concluída com alguns erros."
            else:
                run["status"] = "finished"
                run["progress"] = "Verificação concluída."

        _mutate_run(ctx, run_id, finish)
    except Exception as exc:
        try:
            _mutate_run(
                ctx,
                run_id,
                lambda run: run.update(status="failed", finished_at=now_ms(), error=_safe_error(exc), progress="A verificação falhou."),
            )
        except KeyError:
            pass
    finally:
        with _TASKS_LOCK:
            _TASKS.pop(_task_id(ctx, run_id), None)
            _STOP_FLAGS.pop(_task_id(ctx, run_id), None)


async def _run_background(ctx: WorkerContext, run_id: str, stop: threading.Event) -> None:
    await asyncio.to_thread(_execute_run_sync, ctx, run_id, stop)


def _schedule(ctx: WorkerContext, run_id: str) -> None:
    key = _task_id(ctx, run_id)
    with _TASKS_LOCK:
        current = _TASKS.get(key)
        if current and not current.done():
            return
        stop = threading.Event()
        _STOP_FLAGS[key] = stop
        _TASKS[key] = asyncio.create_task(_run_background(ctx, run_id, stop), name=f"closure-scan-{run_id}")


def start_closure_scan_recovery() -> None:
    with db_connect() as conn:
        rows = conn.execute("SELECT value FROM kv WHERE key LIKE '%:closure_scans'").fetchall()
    for row in rows:
        try:
            payload = __import__("json").loads(row["value"])
        except Exception:
            continue
        for run in payload.get("runs", []) if isinstance(payload, dict) else []:
            if run.get("status") not in ACTIVE_STATUSES:
                continue
            ctx = WorkerContext(
                company_id=str(run.get("company_id", "")),
                company_name=str(run.get("company_name", "")),
                user_id=str(run.get("user_id", "")),
                user_email=str(run.get("user_email", "")),
                user_role=str(run.get("user_role", "member")),
                via_worker=True,
            )
            try:
                _mutate_run(ctx, run["run_id"], lambda item: item.update(status="queued", progress="Retomando após reinício do servidor..."))
                _schedule(ctx, run["run_id"])
            except Exception:
                logger.exception("Não foi possível retomar %s", run.get("run_id"))


def stop_closure_scan_tasks() -> None:
    with _TASKS_LOCK:
        for flag in _STOP_FLAGS.values():
            flag.set()
        for task in _TASKS.values():
            task.cancel()


@router.get("")
async def list_closure_scans(ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    runs = _load_runs(ctx)
    return {
        "limit": HISTORY_LIMIT,
        "global_workers": GLOBAL_REQUEST_WORKERS,
        "runs": [_public_summary(run) for run in runs],
        "has_active_run": any(run.get("status") in ACTIVE_STATUSES for run in runs),
    }


@router.post("")
async def create_closure_scan(payload: ClosureScanCreateRequest, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    requested = list(dict.fromkeys(str(value).strip() for value in payload.account_ids if str(value).strip()))
    accounts = {str(account.get("id")): account for account in load_accounts_raw(ctx)}
    missing = [account_id for account_id in requested if account_id not in accounts]
    if missing:
        raise HTTPException(status_code=400, detail="Uma ou mais contas selecionadas não existem mais.")
    invalid = [account.get("alias", account_id) for account_id, account in accounts.items() if account_id in requested and (not account.get("usuario") or not account.get("senha"))]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Conta(s) sem login e senha: {', '.join(invalid)}")

    with _STATE_LOCK:
        runs = _load_runs(ctx)
        active_accounts = {
            account_id
            for run in runs
            if run.get("status") in ACTIVE_STATUSES
            for account_id in run.get("account_ids", [])
        }
        overlap = [accounts[account_id].get("alias", account_id) for account_id in requested if account_id in active_accounts]
        if overlap:
            raise HTTPException(status_code=409, detail=f"Já existe verificação ativa para: {', '.join(overlap)}")
        if len([run for run in runs if run.get("status") in ACTIVE_STATUSES]) >= HISTORY_LIMIT:
            raise HTTPException(status_code=409, detail="Aguarde uma verificação ativa terminar.")

        previous, current = _competence_range()
        run_id = _new_run_id()
        run = {
            "run_id": run_id,
            "company_id": ctx.company_id,
            "company_name": ctx.company_name,
            "user_id": ctx.user_id,
            "user_email": ctx.user_email,
            "user_role": ctx.user_role,
            "created_at": now_ms(),
            "updated_at": now_ms(),
            "started_at": None,
            "finished_at": None,
            "status": "queued",
            "progress": "Aguardando início...",
            "competencia_inicial": previous,
            "competencia_final": current,
            "account_ids": requested,
            "accounts": [
                {
                    "account_id": account_id,
                    "account_alias": accounts[account_id].get("alias", account_id),
                    "status": "queued",
                    "progress": "Aguardando...",
                    "total": 0,
                    "processed": 0,
                    "open": 0,
                    "closed": 0,
                    "errors": 0,
                }
                for account_id in requested
            ],
            "results": [],
            "total": 0,
            "processed": 0,
            "open": 0,
            "closed": 0,
            "errors": 0,
            "stop_requested": False,
            "error": "",
        }
        runs.insert(0, run)
        _save_runs(ctx, runs)

    _schedule(ctx, run_id)
    return _public_summary(run)


@router.get("/{run_id}")
async def get_closure_scan(run_id: str, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    run = _find_run(_load_runs(ctx), run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Verificação não encontrada.")
    return run


@router.post("/{run_id}/stop")
async def stop_closure_scan(run_id: str, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    run = _find_run(_load_runs(ctx), run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Verificação não encontrada.")
    if run.get("status") not in ACTIVE_STATUSES:
        return {"stopped": False, "message": "A verificação já terminou."}
    _mutate_run(ctx, run_id, lambda item: item.update(status="stopping", stop_requested=True, progress="Parada solicitada..."))
    with _TASKS_LOCK:
        flag = _STOP_FLAGS.get(_task_id(ctx, run_id))
        if flag:
            flag.set()
    return {"stopped": True, "message": "Parada solicitada. Resultados já concluídos serão preservados."}


@router.delete("/{run_id}")
async def delete_closure_scan(run_id: str, ctx: WorkerContext = Depends(get_worker_context)) -> Dict[str, Any]:
    with _STATE_LOCK:
        runs = _load_runs(ctx)
        run = _find_run(runs, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Verificação não encontrada.")
        if run.get("status") in ACTIVE_STATUSES:
            raise HTTPException(status_code=409, detail="Pare a verificação antes de excluir.")
        _save_runs(ctx, [item for item in runs if item.get("run_id") != run_id])
    return {"deleted": True, "run_id": run_id}


@router.get("/{run_id}/download")
async def download_closure_scan(run_id: str, ctx: WorkerContext = Depends(get_worker_context)) -> Response:
    run = _find_run(_load_runs(ctx), run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Verificação não encontrada.")
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["CONTA", "CNPJ", "INSCRICAO", "EMPRESA", "STATUS", "QTD_PENDENCIAS", "COMPETENCIAS", "MENSAGENS", "ERRO"])
    for item in run.get("results", []):
        writer.writerow(
            [
                item.get("account_alias", ""),
                item.get("cnpj", ""),
                item.get("inscricao", ""),
                item.get("nome", ""),
                item.get("status", ""),
                item.get("qtd_pendencias", 0),
                ",".join(item.get("competencias_pendentes", []) or []),
                " | ".join(item.get("mensagens", []) or []),
                item.get("erro", ""),
            ]
        )
    content = "\ufeff" + output.getvalue()
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="encerramento_{run_id}.csv"', "Cache-Control": "no-store"},
    )
