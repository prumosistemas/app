#!/usr/bin/env python3

import html
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from flow_errors import (
    CnpjInexistenteError,
    CnpjMismatchError,
    FlowError,
    LoginError,
    MensagemTelaError,
    PortalAccessBlockedError,
)


BASE = "https://iss.fortaleza.ce.gov.br"
ROOT = f"{BASE}/grpfor"
HOME = f"{ROOT}/home.seam"
OAUTH_LOGIN = f"{ROOT}/oauth2/login"
IDP_ORIGIN = "https://idp2.sefin.fortaleza.ce.gov.br"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class PortalBootstrapResult:
    cnpj: str
    empresa: str
    cid: str
    home_url: str
    cookies: List[Dict[str, object]]


def only_digits(value: object) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def clean_text(value: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True),
    ).strip()


def mask_cnpj(digits: str) -> str:
    d = only_digits(digits).zfill(14)
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:14]}"


def extract_view_state(text: str) -> str:
    m = re.search(
        r"<update[^>]+id=[\"']javax\.faces\.ViewState[\"'][^>]*>(.*?)</update>",
        text or "",
        re.S | re.I,
    )
    if m:
        raw = re.sub(r"^<!\[CDATA\[|\]\]>$", "", m.group(1).strip(), flags=re.S)
        val = re.search(r"value=[\"']([^\"']+)[\"']", raw, re.I)
        return html.unescape(val.group(1) if val else clean_text(raw))

    m = re.search(
        r"name=[\"']javax\.faces\.ViewState[\"'][^>]+value=[\"']([^\"']+)[\"']",
        text or "",
        re.I,
    )
    if m:
        return html.unescape(m.group(1))
    return ""


def parse_attrs(source: str) -> Dict[str, str]:
    return {
        m.group(1): html.unescape(m.group(2))
        for m in re.finditer(r"([:\w-]+)\s*=\s*[\"']([^\"']*)[\"']", source or "")
    }


def find_login_form(text: str, base_url: str) -> Tuple[str, Dict[str, str]]:
    m = re.search(
        r"<form\b[^>]*action=[\"']([^\"']*login-actions/authenticate[^\"']*)[\"'][^>]*>(.*?)</form>",
        text or "",
        re.S | re.I,
    )
    if not m:
        raise FlowError(
            "LOGIN_FORM_NOT_FOUND",
            "Form de login do Keycloak nao encontrado.",
            short_message="Form de login do portal nao foi encontrado.",
            action="Verificar disponibilidade do portal de login.",
            retryable=True,
        )

    inputs: Dict[str, str] = {}
    for input_match in re.finditer(r"<input\b([^>]*)>", m.group(2), re.I):
        attrs = parse_attrs(input_match.group(1))
        if attrs.get("name"):
            inputs[attrs["name"]] = attrs.get("value", "")
    return urljoin(base_url, html.unescape(m.group(1))), inputs


def detect_access_block(text: str) -> None:
    compact = re.sub(r"\s+", " ", text or "").strip()
    low = compact.lower()
    if (
        "geo-ip filter alert" in low
        or "this site has been blocked by the network administrator" in low
        or ("forbidden" in low and "sefin.fortaleza" in low)
    ):
        ip_match = re.search(r"IP address:\s*([0-9.]+)", compact, re.I)
        reason_match = re.search(r"Block reason:\s*([^\.]+(?:\.))", compact, re.I)
        parts = []
        if reason_match:
            parts.append(reason_match.group(1).strip())
        if ip_match:
            parts.append(f"IP={ip_match.group(1)}")
        raise PortalAccessBlockedError(" | ".join(parts) or compact[:300])


def detect_message_modal(text: str) -> None:
    compact = clean_text(text)
    low = compact.lower()
    if "mensagensmodalcontentdiv" in (text or "").lower() and (
        "mensagem" in low or "dar ciencia" in low or "dar ciência" in low
    ):
        raise MensagemTelaError("Mensagem na tela")


def find_company_link_and_name(text: str, cnpj_digits: str) -> Tuple[str, str, str]:
    soup = BeautifulSoup(text or "", "html.parser")
    for row in soup.select("tbody[id='alteraInscricaoForm:empresaDataTable:tb'] tr"):
        row_digits = only_digits(row.get_text(" ", strip=True))
        if cnpj_digits not in row_digits:
            continue
        link = row.find("a", id=re.compile(r"alteraInscricaoForm:empresaDataTable:\d+:linkNome"))
        if not link or not link.get("id"):
            continue
        cells = row.find_all("td")
        cnpj_text = clean_text(str(cells[1])) if len(cells) > 1 else cnpj_digits
        nome = clean_text(str(cells[3])) if len(cells) > 3 else clean_text(str(link))
        return link["id"], nome or cnpj_digits, only_digits(cnpj_text).zfill(14)

    body = clean_text(text)
    if "Nenhum registro encontrado" in body:
        raise CnpjInexistenteError(cnpj_digits)

    m = re.search(r"(alteraInscricaoForm:empresaDataTable:\d+:linkNome)", text or "")
    if m:
        return m.group(1), cnpj_digits, cnpj_digits

    raise FlowError(
        "EMPRESA_NAO_LOCALIZADA",
        f"Empresa nao localizada para o CNPJ {cnpj_digits}",
        short_message="A pesquisa nao retornou empresa utilizavel.",
        action="Verificar o CNPJ pesquisado e o HTML retornado da grade.",
        retryable=False,
    )


def extract_cid(text: str, url: str = "") -> str:
    m = re.search(r"cid=(\d+)", url or "") or re.search(r"cid=(\d+)", text or "")
    return m.group(1) if m else ""


class PortalBootstrapClient:
    def __init__(self, *, timeout: int = 45):
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": UA,
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            }
        )

    def get(self, url: str, **kwargs) -> requests.Response:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            **kwargs.pop("headers", {}),
        }
        r = self.s.get(url, headers=headers, timeout=self.timeout, **kwargs)
        detect_access_block(r.text)
        return r

    def post(self, url: str, data, *, headers: Optional[Dict[str, str]] = None, **kwargs) -> requests.Response:
        merged = {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
        if headers:
            merged.update(headers)
        r = self.s.post(url, data=data, headers=merged, timeout=self.timeout, **kwargs)
        detect_access_block(r.text)
        return r

    def ajax_headers(self, referer: str) -> Dict[str, str]:
        return {
            "Accept": "*/*",
            "Faces-Request": "partial/ajax",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE,
            "Referer": referer,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def login(self, usuario: str, senha: str) -> str:
        r0 = self.get(OAUTH_LOGIN)
        action, inputs = find_login_form(r0.text, r0.url)
        payload = OrderedDict(inputs)
        payload["username"] = only_digits(usuario)
        payload["password"] = senha
        payload.setdefault("credentialId", "")
        self.post(
            action,
            payload,
            headers={
                "Origin": IDP_ORIGIN,
                "Referer": r0.url,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        home = self.get(HOME)
        if re.search(r"kc-form-login|Por favor,\s*identifique-se|senha inv[aá]lida", home.text, re.I):
            raise LoginError("Login falhou.")
        vs = extract_view_state(home.text)
        if not vs:
            raise FlowError(
                "VIEWSTATE_NOT_FOUND",
                "ViewState nao encontrado apos login.",
                short_message="Portal logou, mas nao retornou estado JSF valido.",
                action="Repetir login e verificar indisponibilidade do portal.",
                retryable=True,
            )
        return vs

    def select_company(self, cnpj: str, view_state: str) -> Tuple[str, str]:
        cnpj_digits = only_digits(cnpj).zfill(14)
        base = {
            "AJAXREQUEST": "_viewRoot",
            "alteraInscricaoForm": "alteraInscricaoForm",
            "alteraInscricaoForm:cpfPesquisa": cnpj_digits,
            "alteraInscricaoForm:sugestaoPesquisa_selection": "",
            "alteraInscricaoForm:tipoPesquisa": "CNPJ",
            "alteraInscricaoForm:confirmaAlteraInscricaoAtualModalOpenedState": "",
            "javax.faces.ViewState": view_state,
            "AJAX:EVENTS_COUNT": "1",
        }

        data = OrderedDict(base)
        data["alteraInscricaoForm:sugestaoPesquisa"] = "alteraInscricaoForm:sugestaoPesquisa"
        data["ajaxSingle"] = "alteraInscricaoForm:sugestaoPesquisa"
        data["inputvalue"] = cnpj_digits
        r = self.post(HOME, data=data, headers=self.ajax_headers(HOME))
        vs = extract_view_state(r.text) or view_state

        data = OrderedDict(base)
        data["alteraInscricaoForm:cpfPesquisa"] = mask_cnpj(cnpj_digits)
        data["javax.faces.ViewState"] = vs
        data["alteraInscricaoForm:btnPesquisar"] = "alteraInscricaoForm:btnPesquisar"
        r = self.post(HOME, data=data, headers=self.ajax_headers(HOME))
        vs = extract_view_state(r.text) or vs
        link, empresa, cnpj_ret = find_company_link_and_name(r.text, cnpj_digits)
        if cnpj_digits != cnpj_ret:
            raise CnpjMismatchError(cnpj_digits, cnpj_ret)

        data = OrderedDict(base)
        data["alteraInscricaoForm:cpfPesquisa"] = mask_cnpj(cnpj_digits)
        data["javax.faces.ViewState"] = vs
        data[link] = link
        r = self.post(HOME, data=data, headers=self.ajax_headers(HOME), allow_redirects=True)
        detect_message_modal(r.text)
        cid = extract_cid(r.text, r.url)
        if not cid:
            raise FlowError(
                "CID_NOT_FOUND",
                "Nao consegui extrair cid da empresa selecionada.",
                short_message="Empresa foi encontrada, mas o portal nao retornou cid.",
                action="Repetir a selecao da empresa ou usar fallback pelo navegador.",
                retryable=True,
            )

        page = self.get(f"{HOME}?cid={cid}")
        detect_message_modal(page.text)
        if "homeForm" not in page.text:
            raise FlowError(
                "COMPANY_HOME_NOT_READY",
                f"Home da empresa nao carregou. URL={page.url}",
                short_message="Sessao da empresa nao ficou pronta apos selecao.",
                action="Repetir a selecao da empresa ou usar fallback pelo navegador.",
                retryable=True,
            )
        return empresa, cid

    def playwright_cookies(self) -> List[Dict[str, object]]:
        cookies: List[Dict[str, object]] = []
        for cookie in self.s.cookies:
            item: Dict[str, object] = {
                "name": cookie.name,
                "value": cookie.value,
                "path": cookie.path or "/",
            }
            if cookie.domain:
                item["domain"] = cookie.domain
            else:
                item["url"] = BASE
            if cookie.expires:
                item["expires"] = float(cookie.expires)
            if cookie.secure:
                item["secure"] = True
            rest = getattr(cookie, "_rest", {}) or {}
            if any(str(k).lower() == "httponly" for k in rest):
                item["httpOnly"] = True
            same_site = rest.get("SameSite") or rest.get("samesite")
            if same_site:
                normalized = str(same_site).strip().capitalize()
                if normalized in {"Lax", "Strict", "None"}:
                    item["sameSite"] = normalized
            cookies.append(item)
        return cookies


def bootstrap_portal_requests(usuario: str, senha: str, cnpj: str, *, timeout: int = 45) -> PortalBootstrapResult:
    client = PortalBootstrapClient(timeout=timeout)
    view_state = client.login(usuario, senha)
    empresa, cid = client.select_company(cnpj, view_state)
    return PortalBootstrapResult(
        cnpj=only_digits(cnpj).zfill(14),
        empresa=empresa,
        cid=cid,
        home_url=f"{HOME}?cid={cid}",
        cookies=client.playwright_cookies(),
    )
