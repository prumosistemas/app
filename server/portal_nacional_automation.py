import argparse
import concurrent.futures
import html as html_lib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import requests
import websocket
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "sessao_nfse.txt"
PROFILE_BASE_DIR = BASE_DIR / "chrome-profiles"
MODE_URLS = {
    "recebidas": "https://www.nfse.gov.br/EmissorNacional/Notas/Recebidas",
    "emitidas": "https://www.nfse.gov.br/EmissorNacional/Notas/Emitidas",
}
TARGET_URL = MODE_URLS["recebidas"]
STATE_FILE = BASE_DIR / "navegador_nfse.json"
DOWNLOAD_DIR = BASE_DIR / "downloads_nfse"
SESSION_GENERATOR = BASE_DIR / "portal_nacional_session.py"
DEFAULT_INDEX_FILE = BASE_DIR / "indice_nfse.json"
PORTAL_MAX_PERIOD_DAYS = 30
SOLVER_API_URL = "http://127.0.0.1:8765/solve"
SOLVER_REQUEST_TIMEOUT_SECONDS = int(os.environ.get("PORTAL_NACIONAL_SOLVER_TIMEOUT_SECONDS", "420"))
# O Modal continua sendo o endpoint primario. O resolvedor local usa o IP
# residencial do ThinkPad somente quando a tentativa primaria falha.
DEFAULT_SOLVER_FALLBACK_URL = "http://127.0.0.1:8876/solve"


def configured_solver_fallback_url(value: str | None) -> str:
    return (value or "").strip() or DEFAULT_SOLVER_FALLBACK_URL


def configured_solver_fallback_urls(
    value: str | None,
    legacy_value: str | None = None,
) -> list[str]:
    """Retorna failovers ordenados sem duplicatas.

    ``PORTAL_NACIONAL_SOLVER_FALLBACK_URLS`` aceita virgula, ponto e virgula
    ou quebra de linha. A variavel singular continua valida para upgrades sem
    interrupcao. O resolvedor residencial permanece como ultimo recurso.
    """
    raw = (value or "").strip()
    if raw:
        candidates = re.split(r"[,;\r\n]+", raw)
    else:
        candidates = [configured_solver_fallback_url(legacy_value)]
    urls = [candidate.strip() for candidate in candidates if candidate.strip()]
    if DEFAULT_SOLVER_FALLBACK_URL not in urls:
        urls.append(DEFAULT_SOLVER_FALLBACK_URL)
    return list(dict.fromkeys(urls))


SOLVER_FALLBACK_URLS = configured_solver_fallback_urls(
    os.environ.get("PORTAL_NACIONAL_SOLVER_FALLBACK_URLS"),
    os.environ.get("PORTAL_NACIONAL_SOLVER_FALLBACK_URL"),
)
# Alias preservado para integracoes e testes antigos que alteram um unico URL.
SOLVER_FALLBACK_URL = SOLVER_FALLBACK_URLS[0]
SOLVER_ENDPOINT_COOLDOWNS: dict[str, float] = {}
SOLVER_ENDPOINT_COOLDOWN_LOCK = threading.Lock()
SOLVER_STATUS_LOCK = threading.Lock()
SOLVER_STATUS_FILE = Path(
    os.environ.get(
        "PORTAL_NACIONAL_SOLVER_STATUS_FILE",
        str(BASE_DIR / "portal_solver_status.json"),
    )
)


def record_solver_endpoint_event(
    url: str,
    event: str,
    request_id: str,
    exc: Exception | None = None,
) -> None:
    """Persiste telemetria minima para o master, sem payload do captcha."""
    parsed = urlparse(url)
    payload = {
        "endpoint_host": (parsed.hostname or "").lower(),
        "event": event,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "request_id": str(request_id)[-80:],
    }
    if exc is not None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        payload["error_kind"] = f"http_{status}" if status else type(exc).__name__
    try:
        with SOLVER_STATUS_LOCK:
            SOLVER_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = SOLVER_STATUS_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(SOLVER_STATUS_FILE)
    except OSError:
        pass


def find_browser(explicit: str | None = None) -> str:
    candidates = []
    if explicit:
        candidates.append(explicit)

    local = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")

    candidates.extend([
        rf"{program_files}\Google\Chrome\Application\chrome.exe",
        rf"{program_files_x86}\Google\Chrome\Application\chrome.exe",
        rf"{local}\Google\Chrome\Application\chrome.exe",
        rf"{program_files}\Microsoft\Edge\Application\msedge.exe",
        rf"{program_files_x86}\Microsoft\Edge\Application\msedge.exe",
    ])

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate

    raise FileNotFoundError("Não achei Chrome/Edge.")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_browser_preferences(profile: Path, download_dir: Path) -> None:
    """
    Escreve preferências no perfil antes de abrir o Chrome/Edge.
    Isso ajuda a impedir o alerta de baixar em lote e força a pasta de download.
    """
    default_dir = profile / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)

    prefs_file = default_dir / "Preferences"

    prefs = {}
    if prefs_file.exists():
        try:
            prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        except Exception:
            prefs = {}

    prefs.setdefault("download", {})
    prefs["download"]["default_directory"] = str(download_dir)
    prefs["download"]["directory_upgrade"] = True
    prefs["download"]["prompt_for_download"] = False

    prefs.setdefault("profile", {})
    prefs["profile"].setdefault("default_content_setting_values", {})
    prefs["profile"]["default_content_setting_values"]["automatic_downloads"] = 1

    prefs.setdefault("safebrowsing", {})
    prefs["safebrowsing"]["enabled"] = True

    prefs_file.write_text(
        json.dumps(prefs, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


class CdpClient:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=15)
        self.next_id = 1

    def call(self, method: str, params: dict | None = None) -> dict:
        msg_id = self.next_id
        self.next_id += 1

        self.ws.send(json.dumps({
            "id": msg_id,
            "method": method,
            "params": params or {}
        }))

        while True:
            msg = json.loads(self.ws.recv())

            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expression: str, await_promise: bool = False):
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise
        })
        value = result.get("result", {})
        return value.get("value", value)

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def wait_for_cdp(port: int) -> str:
    base = f"http://127.0.0.1:{port}"

    for _ in range(120):
        try:
            pages = requests.get(f"{base}/json/list", timeout=2).json()

            for page in pages:
                if page.get("type") == "page" and page.get("webSocketDebuggerUrl"):
                    return page["webSocketDebuggerUrl"]

        except Exception:
            pass

        time.sleep(0.3)

    raise RuntimeError(f"DevTools não respondeu na porta {port}")


def configure_downloads(client: CdpClient, download_dir: Path) -> None:
    """
    Configura download via CDP.
    Usa Browser.setDownloadBehavior e, se falhar, tenta Page.setDownloadBehavior.
    """
    download_dir.mkdir(parents=True, exist_ok=True)

    params = {
        "behavior": "allow",
        "downloadPath": str(download_dir)
    }

    try:
        client.call("Browser.setDownloadBehavior", params)
        print("CDP: Browser.setDownloadBehavior aplicado.")
    except Exception as e:
        print(f"CDP: Browser.setDownloadBehavior falhou: {e}")
        try:
            client.call("Page.setDownloadBehavior", params)
            print("CDP: Page.setDownloadBehavior aplicado.")
        except Exception as e2:
            print(f"CDP: Page.setDownloadBehavior falhou: {e2}")

    try:
        client.call("Browser.grantPermissions", {
            "origin": "https://www.nfse.gov.br",
            "permissions": ["automaticDownloads"]
        })
        print("CDP: permissão automaticDownloads liberada.")
    except Exception as e:
        print(f"CDP: não foi possível liberar automaticDownloads via CDP: {e}")

    print(f"Downloads serão salvos em: {download_dir}")


def list_download_files(download_dir: Path) -> list[Path]:
    if not download_dir.exists():
        return []

    ignored_suffixes = {
        ".crdownload",
        ".tmp",
        ".download"
    }

    files = []
    for item in download_dir.iterdir():
        if item.is_file() and item.suffix.lower() not in ignored_suffixes:
            files.append(item)

    return files


def wait_for_new_download(download_dir: Path, before: set[str], timeout: int = 40) -> list[Path]:
    """
    Aguarda aparecer arquivo novo na pasta.
    Não depende disso para validar 100%, mas ajuda no log.
    """
    end = time.time() + timeout

    while time.time() < end:
        current = list_download_files(download_dir)
        new_files = [f for f in current if f.name not in before]

        active = list(download_dir.glob("*.crdownload")) if download_dir.exists() else []

        if new_files and not active:
            return new_files

        time.sleep(1)

    return []


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    last_error = None
    for attempt in range(30):
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{int(time.time() * 1000000)}.{attempt}.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last_error = exc
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            time.sleep(min(0.05 * (attempt + 1), 0.5))
    if last_error is not None:
        raise last_error


def load_index(path: Path, modo: str) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("modo") == modo:
                return data
        except Exception:
            pass
    return {
        "schema": 1,
        "modo": modo,
        "status": "novo",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "session": {},
        "certificates": [],
        "totals": {
            "portal_registros": None,
            "paginas": None,
            "capturados": 0,
            "pendentes": 0,
            "baixados": 0,
            "erros": 0,
        },
        "pages": {},
        "items": {},
        "events": [],
    }


def save_index(path: Path, index: dict, status: str | None = None, event: str | None = None, **extra) -> None:
    if status:
        index["status"] = status
    index["updated_at"] = now_iso()
    if event:
        entry = {"at": now_iso(), "event": event}
        entry.update(extra)
        index.setdefault("events", []).append(entry)
        index["events"] = index["events"][-300:]
    items = list(index.get("items", {}).values())
    index.setdefault("totals", {})
    index["totals"]["capturados"] = len(items)
    index["totals"]["pendentes"] = sum(1 for item in items if item.get("status") in (None, "pendente"))
    index["totals"]["baixados"] = sum(1 for item in items if item.get("status") == "baixado")
    index["totals"]["erros"] = sum(1 for item in items if item.get("status") == "erro")
    atomic_write_json(path, index)


def final_download_status(index: dict, max_items: int = 0) -> str:
    totals = index.get("totals", {}) or {}
    if totals.get("erros"):
        return "finalizado_com_erros"
    if totals.get("pendentes") and max_items:
        return "finalizado_parcial"
    if totals.get("pendentes"):
        return "finalizado_com_erros"
    return "finalizado"


def note_id_from_href(href: str) -> str:
    return href.rstrip("/").split("/")[-1].split("?")[0]


def update_certificate_in_index(index: dict, session_data: dict) -> None:
    cert = session_data.get("certificate") or {}
    thumbprint = cert.get("thumbprint")
    if not thumbprint:
        return
    index.setdefault("session", {})
    index["session"]["certificate_thumbprint"] = thumbprint
    index["session"]["certificate_subject"] = cert.get("subject")
    index["session"]["last_session_created_at"] = session_data.get("created_at")
    index["session"]["last_session_saved_at"] = session_data.get("saved_at_local")
    cert_entry = {
        "thumbprint": thumbprint,
        "subject": cert.get("subject"),
        "not_after": cert.get("not_after"),
        "seen_at": now_iso(),
    }
    certificates = [c for c in index.setdefault("certificates", []) if c.get("thumbprint") != thumbprint]
    certificates.append(cert_entry)
    index["certificates"] = certificates


def is_login_page(client: CdpClient) -> dict:
    data = client.eval(
        r"""
(() => {
  const text = document.body ? document.body.innerText : '';
  return {
    url: location.href,
    isLogin:
      location.href.includes('/Login') ||
      !!document.querySelector('section.login-page') ||
      text.includes('Acesso com Certificado Digital') ||
      text.includes('Acesso com Usuário/Senha')
  };
})()
"""
    )
    return data if isinstance(data, dict) else {"isLogin": False, "url": ""}


def normalize_date_for_portal(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%d/%m/%Y")
        except ValueError:
            pass
    raise ValueError(f"Data invalida: {value}. Use DD/MM/AAAA ou AAAA-MM-DD.")


def build_portal_date_windows(data_inicial: str | None, data_final: str | None) -> list[dict]:
    """Divide o periodo em janelas mensais de no maximo 30 dias inclusivos.

    O Portal rejeita intervalos com mais de 30 dias. Respeitar a virada do mes
    deixa a auditoria alinhada ao que o usuario informou (por exemplo, junho
    completo e depois julho parcial), sem filtrar a competencia da NFS-e.
    """
    start_value = normalize_date_for_portal(data_inicial)
    end_value = normalize_date_for_portal(data_final)
    if not start_value and not end_value:
        return [{"index": 1, "data_inicial": None, "data_final": None, "dias": None}]
    if not start_value or not end_value:
        raise ValueError("Data inicial e data final devem ser informadas juntas.")

    start = datetime.strptime(start_value, "%d/%m/%Y").date()
    end = datetime.strptime(end_value, "%d/%m/%Y").date()
    if start > end:
        raise ValueError("Data inicial nao pode ser posterior a data final.")

    windows = []
    current = start
    while current <= end:
        next_month = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
        month_end = next_month - timedelta(days=1)
        window_end = min(end, month_end, current + timedelta(days=PORTAL_MAX_PERIOD_DAYS - 1))
        windows.append({
            "index": len(windows) + 1,
            "data_inicial": current.strftime("%d/%m/%Y"),
            "data_final": window_end.strftime("%d/%m/%Y"),
            "dias": (window_end - current).days + 1,
        })
        current = window_end + timedelta(days=1)
    return windows


def update_url_query(url: str, updates: dict) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in updates.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = str(value)
    return urlunparse(parsed._replace(query=urlencode(query)))


def build_page_url(base_url: str, page: int) -> str:
    if page <= 1:
        return update_url_query(base_url, {"pg": None})
    return update_url_query(base_url, {"pg": page})


def page_snapshot(client: CdpClient) -> dict:
    data = client.eval(
        r"""
(() => {
  const desc = document.querySelector('.descricao')?.innerText || '';
  const match = desc.match(/Total\s+de\s+(\d+)\s+registros/i);
  const total = match ? parseInt(match[1], 10) : null;
  const anchors = [...document.querySelectorAll('.pagination a')].map((a) => ({
    text: (a.innerText || '').trim(),
    title: a.getAttribute('data-original-title') || a.getAttribute('title') || '',
    href: a.getAttribute('href') || '',
    absHref: a.href || ''
  }));
  const numbers = anchors.map((a) => {
    const m = (a.title + ' ' + a.text + ' ' + a.href).match(/(?:Página\s*)?(\d+)/i);
    return m ? parseInt(m[1], 10) : null;
  }).filter((n) => Number.isFinite(n));
  const last = anchors.find((a) => /Última|Ultima/i.test(a.title));
  let lastPage = numbers.length ? Math.max(...numbers) : 1;
  if (last && last.href) {
    const m = last.href.match(/[?&]pg=(\d+)/i);
    if (m) lastPage = parseInt(m[1], 10);
  }
  return { url: location.href, descricao: desc, totalRegistros: total, lastPage, pagination: anchors };
})()
"""
    )
    return data if isinstance(data, dict) else {}


def navigate_and_wait(client: CdpClient, url: str, seconds: float = 3.0) -> None:
    client.call("Page.navigate", {"url": url})
    time.sleep(seconds)


def apply_date_filters(client: CdpClient, data_inicial: str | None, data_final: str | None) -> dict:
    """Nao filtra o Portal por data.

    O filtro nativo usa a data conhecida pelo Portal e exclui notas emitidas
    retroativamente. As datas da run sao apenas referencia operacional; a
    indexacao precisa preservar tudo que o Portal apresenta.
    """
    return {
        "applied": False,
        "reason": "disabled_to_include_retroactive_notes",
        "data_inicial_referencia": normalize_date_for_portal(data_inicial),
        "data_final_referencia": normalize_date_for_portal(data_final),
    }


def apply_cookies_to_client(client: CdpClient, session_data: dict) -> None:
    client.call("Network.enable")
    try:
        client.call("Network.clearBrowserCookies")
    except Exception:
        pass
    for cookie in session_data.get("cookies", []):
        params = {
            "url": "https://www.nfse.gov.br/",
            "name": cookie["name"],
            "value": cookie["value"],
            "path": cookie.get("path") or "/",
            "secure": bool(cookie.get("secure")),
            "httpOnly": bool(cookie.get("httpOnly")),
            "sameSite": "Lax",
        }
        domain = cookie.get("domain")
        if domain and domain.startswith("."):
            params["domain"] = domain
        if cookie.get("expires"):
            try:
                dt = datetime.fromisoformat(cookie["expires"].replace("Z", "+00:00"))
                params["expires"] = int(dt.timestamp())
            except Exception:
                pass
        result = client.call("Network.setCookie", params)
        if not result.get("success"):
            raise RuntimeError(f"Falhou ao gravar cookie {cookie['name']}")


def regenerate_session(
    index: dict,
    index_path: Path,
    session_path: Path,
    start_url: str,
    cert_index: int | None = None,
    pfx_file: str | None = None,
    pfx_password_file: str | None = None,
) -> dict:
    thumbprint = index.get("session", {}).get("certificate_thumbprint")
    cmd = [sys.executable, str(SESSION_GENERATOR), "--out", str(session_path), "--start-url", start_url]
    if pfx_file:
        cmd.extend(["--pfx-file", str(pfx_file)])
        if pfx_password_file:
            cmd.extend(["--pfx-password-file", str(pfx_password_file)])
    elif cert_index is not None:
        cmd.extend(["--cert-index", str(cert_index)])
    elif thumbprint:
        cmd.extend(["--thumbprint", thumbprint])
    else:
        raise RuntimeError("Sessao caiu, mas nao ha PFX, certificate_thumbprint nem --cert-index para renovar sem interacao.")
    save_index(index_path, index, "renovando_sessao", "session_expired_regenerating", command=" ".join(cmd))
    configured_delays = os.environ.get(
        "PORTAL_SESSION_RETRY_DELAYS_SECONDS", "0,60,180,600,1800,3600,7200"
    )
    delays = []
    for value in configured_delays.split(","):
        try:
            delays.append(max(0, int(value.strip())))
        except ValueError:
            pass
    if not delays:
        delays = [0, 60, 180, 600, 1800, 3600, 7200]
    max_attempts = max(1, int(os.environ.get("PORTAL_SESSION_MAX_ATTEMPTS", "12")))
    last_output = ""
    session_data = None
    for attempt in range(1, max_attempts + 1):
        delay = delays[min(attempt - 1, len(delays) - 1)]
        if delay:
            print(
                f"[Sessao] Portal recusou o certificado; tentativa {attempt}/{max_attempts} "
                f"em {delay}s.",
                flush=True,
            )
            save_index(
                index_path,
                index,
                "aguardando_portal",
                "session_retry_backoff_wait",
                attempt=attempt,
                seconds=delay,
            )
            time.sleep(delay)
        proc = subprocess.run(cmd, cwd=str(BASE_DIR), text=True, capture_output=True, timeout=180)
        last_output = (proc.stderr or proc.stdout).strip()
        try:
            candidate = json.loads(session_path.read_text(encoding="utf-8"))
        except Exception:
            candidate = {}
        cookie_names = {cookie.get("name") for cookie in candidate.get("cookies") or []}
        valid = bool(
            proc.returncode == 0
            and candidate.get("target_looks_logged_in")
            and "Emissor" in cookie_names
        )
        if valid:
            session_data = candidate
            print(f"[Sessao] Renovada na tentativa {attempt}.", flush=True)
            break
        save_index(
            index_path,
            index,
            "aguardando_portal",
            "session_regenerate_attempt_failed",
            attempt=attempt,
            status_code=candidate.get("status_code"),
            target_looks_logged_in=bool(candidate.get("target_looks_logged_in")),
        )
        print(
            f"[Sessao] Tentativa {attempt}/{max_attempts} recusada "
            f"(HTTP {candidate.get('status_code') or 'sem resposta'}).",
            flush=True,
        )
    if session_data is None:
        raise RuntimeError(
            "Portal Nacional nao aceitou o certificado apos "
            f"{max_attempts} tentativas com backoff. Ultimo retorno: {last_output[-500:]}"
        )
    update_certificate_in_index(index, session_data)
    save_index(index_path, index, "sessao_renovada", "session_regenerated", stdout=proc.stdout[-1000:])
    return session_data


def ensure_logged_in(
    client: CdpClient,
    index: dict,
    index_path: Path,
    session_path: Path,
    start_url: str,
    cert_index: int | None = None,
    pfx_file: str | None = None,
    pfx_password_file: str | None = None,
) -> dict | None:
    info = is_login_page(client)
    if not info.get("isLogin"):
        return None
    session_data = regenerate_session(index, index_path, session_path, start_url, cert_index, pfx_file, pfx_password_file)
    apply_cookies_to_client(client, session_data)
    navigate_and_wait(client, start_url, 5)
    info = is_login_page(client)
    if info.get("isLogin"):
        raise RuntimeError(f"Sessao renovada, mas portal continuou no login: {info.get('url')}")
    return session_data


def scan_current_page_links(client: CdpClient, modo: str, page: int) -> list[dict]:
    links = first_page_xml_links(client)
    for item in links:
        item["modo"] = modo
        item["page"] = page
        item["id"] = note_id_from_href(item.get("href", ""))
    return links


def consolidate_page_totals(index: dict) -> None:
    date_windows = index.get("date_windows") or []
    if date_windows:
        totals = index.setdefault("totals", {})
        totals["portal_registros"] = len(index.get("items") or {})
        totals["portal_registros_soma_janelas"] = sum(
            int(window.get("portal_total_registros") or 0) for window in date_windows
        )
        totals["paginas"] = sum(int(window.get("paginas") or 0) for window in date_windows)
        totals["janelas"] = len(date_windows)
        totals["duplicados_entre_janelas"] = max(
            0,
            sum(int(window.get("capturados") or 0) for window in date_windows)
            - len(index.get("items") or {}),
        )
        return

    pages = index.get("pages", {})
    page_numbers = []
    page_totals = []
    for key, page in pages.items():
        try:
            page_numbers.append(int(key))
        except Exception:
            pass
        total = page.get("portal_total_registros")
        if total is not None:
            try:
                page_totals.append(int(total))
            except Exception:
                pass
    if page_totals:
        index.setdefault("totals", {})["portal_registros"] = max(page_totals)
    elif index.get("totals", {}).get("portal_registros") is None:
        index.setdefault("totals", {})["portal_registros"] = None
    if page_numbers:
        index.setdefault("totals", {})["paginas"] = max(page_numbers)


def requests_session_from_data(session_data: dict) -> requests.Session:
    session = requests.Session()
    network_retry = Retry(
        total=4,
        connect=4,
        read=2,
        status=2,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=network_retry, pool_connections=8, pool_maxsize=16)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    })
    for cookie in session_data.get("cookies", []):
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain") or "www.nfse.gov.br",
            path=cookie.get("path") or "/",
        )
    return session


def response_is_login(response: requests.Response) -> bool:
    text = response.text if response.content else ""
    return (
        "/Login" in response.url or
        "Acesso com Certificado Digital" in text or
        "Acesso com Usuário/Senha" in text or
        "Acesso com Usuario/Senha" in text or
        "login-page" in text
    )


def response_is_xml(response: requests.Response) -> bool:
    if response.status_code < 200 or response.status_code >= 300:
        return False

    content = response.content or b""
    if not content.strip():
        return False

    ctype = (response.headers.get("content-type") or "").lower()
    body_start = content[:512].lstrip()
    body_probe = content[:2048].decode("utf-8", errors="ignore").lower()
    stripped_probe = body_probe.strip()

    if stripped_probe.startswith("bad request") or "bad request" in stripped_probe:
        return False
    if "<html" in stripped_probe or "<!doctype html" in stripped_probe:
        return False

    looks_like_xml = (
        "xml" in ctype or
        body_start.startswith(b"<?xml") or
        body_start.startswith(b"<CompNfse") or
        body_start.startswith(b"<NFSe") or
        body_start.startswith(b"<Nfse")
    )
    if not looks_like_xml:
        return False

    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return False

    root_tag = str(root.tag).lower()
    return "nfse" in root_tag or "compnfse" in root_tag


def response_is_pdf(response: requests.Response) -> bool:
    if response.status_code < 200 or response.status_code >= 300:
        return False
    content = response.content or b""
    ctype = (response.headers.get("content-type") or "").lower()
    return content.lstrip().startswith(b"%PDF") or "application/pdf" in ctype


def normalize_download_tipos(tipo_download: str | None) -> list[str]:
    value = (tipo_download or "xml").strip().lower()
    aliases = {
        "nfse": "xml",
        "danfse": "pdf",
        "pdf_danfse": "pdf",
        "todos": "ambos",
        "both": "ambos",
    }
    value = aliases.get(value, value)
    if value == "xml":
        return ["xml"]
    if value == "pdf":
        return ["pdf"]
    if value == "ambos":
        return ["xml", "pdf"]
    raise ValueError(f"Tipo de download invalido: {tipo_download}. Use xml, pdf ou ambos.")


def expected_response_ok(response: requests.Response, tipo: str) -> bool:
    return response_is_xml(response) if tipo == "xml" else response_is_pdf(response)


def default_extension_for_tipo(tipo: str) -> str:
    return ".xml" if tipo == "xml" else ".pdf"


def download_path_segment_for_tipo(tipo: str) -> str:
    return "NFSe" if tipo == "xml" else "DANFSe"


def filename_from_response(response: requests.Response, item: dict, tipo: str = "xml") -> str:
    disp = response.headers.get("content-disposition") or ""
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disp, flags=re.I)
    if match:
        filename = unquote(match.group(1).strip('"'))
        suffix = Path(filename).suffix.lower()
        wanted_suffix = default_extension_for_tipo(tipo)
        if suffix == wanted_suffix:
            return filename
    return f"{item['id']}{default_extension_for_tipo(tipo)}"


def nfse_download_url(item: dict, tipo: str = "xml") -> str:
    segment = download_path_segment_for_tipo(tipo)
    return f"https://www.nfse.gov.br/EmissorNacional/Notas/Download/{segment}/{item['id']}"


def nfse_modal_download_url(item: dict, tipo: str = "xml") -> str:
    segment = download_path_segment_for_tipo(tipo)
    return f"https://www.nfse.gov.br/emissornacional/DPS/ModalCaptcha/{segment}/{item['id']}"


def download_dir_for_tipo(download_dir: Path, tipo: str) -> Path:
    # Mantem compatibilidade: XML fica na pasta atual; PDF fica em subpasta propria.
    return download_dir if tipo == "xml" else (download_dir / "pdf")


def nfse_navigation_headers(referer: str | None = None) -> dict:
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": referer or "https://www.nfse.gov.br/EmissorNacional/Notas/Recebidas",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Priority": "u=0, i",
        "TE": "trailers",
    }


def requests_page_url(base_url: str, page: int, data_inicial: str | None = None, data_final: str | None = None) -> str:
    """Monta uma pagina usando um intervalo aceito pelo Portal Nacional."""
    start_value = normalize_date_for_portal(data_inicial)
    end_value = normalize_date_for_portal(data_final)
    if bool(start_value) != bool(end_value):
        raise ValueError("Data inicial e data final devem ser informadas juntas.")
    updates = {
        "executar": 1 if start_value else None,
        "datainicio": start_value,
        "datafim": end_value,
        "pg": page if page > 1 else None,
    }
    return update_url_query(base_url, updates)


def html_to_text(fragment: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", fragment, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def page_snapshot_html(html_text: str, url: str) -> dict:
    total = None
    total_match = re.search(r"Total\s+de\s+(\d+)\s+registros", html_text, flags=re.I)
    if total_match:
        total = int(total_match.group(1))

    numbers = []
    pagination_match = re.search(r'<ul\b[^>]*class=["\'][^"\']*\bpagination\b[^"\']*["\'][^>]*>.*?</ul>', html_text, flags=re.I | re.S)
    pagination_html = pagination_match.group(0) if pagination_match else ""
    for match in re.finditer(r"Página\s*(\d+)|[?&]pg=(\d+)", pagination_html, flags=re.I):
        value = match.group(1) or match.group(2)
        if value:
            numbers.append(int(value))
    last_page = max(numbers) if numbers else 1
    last_match = re.search(r'title=["\'](?:Última|Ultima)["\'][^>]+href=["\'][^"\']*[?&]pg=(\d+)', pagination_html, flags=re.I)
    if not last_match:
        last_match = re.search(r'href=["\'][^"\']*[?&]pg=(\d+)[^"\']*["\'][^>]+title=["\'](?:Última|Ultima)["\']', pagination_html, flags=re.I)
    if last_match:
        last_page = int(last_match.group(1))

    return {
        "url": url,
        "descricao": total_match.group(0) if total_match else "",
        "totalRegistros": total,
        "lastPage": last_page,
    }


def scan_html_links(html_text: str, modo: str, page: int, base_url: str) -> list[dict]:
    items = []
    seen = set()
    row_matches = list(re.finditer(r"<tr\b[^>]*>.*?</tr>", html_text, flags=re.I | re.S))
    fragments = [match.group(0) for match in row_matches] or [html_text]
    for index, fragment in enumerate(fragments):
        for href_match in re.finditer(r'href=["\']([^"\']*/Notas/Download/NFSe/[^"\']+)["\']', fragment, flags=re.I):
            href = urljoin(base_url, html_lib.unescape(href_match.group(1)))
            key = note_id_from_href(href)
            if not key or key in seen:
                continue
            seen.add(key)
            items.append({
                "index": index,
                "href": href,
                "text": html_to_text(fragment),
                "modo": modo,
                "page": page,
                "id": key,
            })
    return items


def run_requests_index(
    index: dict,
    index_path: Path,
    session_path: Path,
    modo: str,
    target_url: str,
    data_inicial: str | None,
    data_final: str | None,
    cert_index: int | None,
    pfx_file: str | None = None,
    pfx_password_file: str | None = None,
) -> None:
    session_data = json.loads(session_path.read_text(encoding="utf-8"))
    update_certificate_in_index(index, session_data)
    session = requests_session_from_data(session_data)

    def get_page(page: int, window_start: str | None, window_end: str | None) -> requests.Response:
        nonlocal session, session_data
        page_url = requests_page_url(target_url, page, window_start, window_end)
        response = session.get(page_url, timeout=60, allow_redirects=True, headers=nfse_navigation_headers(target_url))
        if response_is_login(response):
            save_index(index_path, index, "renovando_sessao", "requests_index_login_failed", page=page, url=response.url)
            session_data = regenerate_session(index, index_path, session_path, page_url, cert_index, pfx_file, pfx_password_file)
            session = requests_session_from_data(session_data)
            response = session.get(page_url, timeout=60, allow_redirects=True, headers=nfse_navigation_headers(target_url))
        if response_is_login(response):
            raise RuntimeError(f"Sessao nao entrou no portal por requests; URL atual: {response.url}")
        response.raise_for_status()
        return response

    date_windows = build_portal_date_windows(data_inicial, data_final)
    save_index(
        index_path,
        index,
        "indexando_requests",
        "requests_scan_started",
        data_inicial=data_inicial,
        data_final=data_final,
        portal_date_filter_applied=bool(data_inicial and data_final),
        include_retroactive_notes=True,
        period_strategy="monthly_windows_max_30_days",
        janelas=len(date_windows),
    )
    previous_items = dict(index.get("items") or {})
    index["pages"] = {}
    index["items"] = {}
    index["date_windows"] = []
    index["target_url"] = target_url

    for window in date_windows:
        window_index = int(window["index"])
        window_start = window.get("data_inicial")
        window_end = window.get("data_final")
        first_response = get_page(1, window_start, window_end)
        first = page_snapshot_html(first_response.text, first_response.url)
        portal_total = first.get("totalRegistros")
        if portal_total is None:
            raise RuntimeError(
                f"Portal nao informou o total da janela {window_start or 'sem filtro'} a {window_end or 'sem filtro'}."
            )
        portal_total = int(portal_total)
        last_page = int(first.get("lastPage") or 1)
        window_state = {
            **window,
            "portal_total_registros": portal_total,
            "paginas": last_page,
            "capturados": 0,
            "status": "indexando",
        }
        index["date_windows"].append(window_state)
        save_index(
            index_path,
            index,
            "indexando_requests",
            "requests_window_detected",
            window=window_index,
            data_inicial=window_start,
            data_final=window_end,
            total=portal_total,
            paginas=last_page,
        )

        if last_page > 1:
            last_response = get_page(last_page, window_start, window_end)
            last_snapshot = page_snapshot_html(last_response.text, last_response.url)
            last_page = max(last_page, int(last_snapshot.get("lastPage") or last_page))
            window_state["paginas"] = last_page

        print(
            f"Janela {window_index}/{len(date_windows)} {window_start or 'sem filtro'} a "
            f"{window_end or 'sem filtro'}: {portal_total} registros em {last_page} paginas."
        )
        window_ids: set[str] = set()
        for scan_attempt in range(1, 4):
            for page in range(1, last_page + 1):
                page_key = f"w{window_index:02d}-p{page:03d}"
                page_url = requests_page_url(target_url, page, window_start, window_end)
                save_index(
                    index_path,
                    index,
                    "indexando_requests",
                    "requests_page_scan_started",
                    window=window_index,
                    page=page,
                    attempt=scan_attempt,
                )
                response = first_response if page == 1 and scan_attempt == 1 else get_page(page, window_start, window_end)
                snapshot = page_snapshot_html(response.text, response.url)
                links = scan_html_links(response.text, modo, page, response.url)
                index["pages"][page_key] = {
                    "page": page,
                    "window": window_index,
                    "data_inicial": window_start,
                    "data_final": window_end,
                    "url": page_url,
                    "status": "capturada",
                    "captured_at": now_iso(),
                    "portal_total_text": snapshot.get("descricao"),
                    "portal_total_registros": snapshot.get("totalRegistros"),
                    "links_count": len(links),
                    "method": "requests",
                    "scan_attempt": scan_attempt,
                }
                for item in links:
                    key = item["id"] or note_id_from_href(item["href"])
                    window_ids.add(key)
                    current = index["items"].get(key, {})
                    existing = previous_items.get(key, current)
                    status = "baixado" if existing.get("status") == "baixado" else existing.get("status") or "pendente"
                    seen_windows = set(current.get("windows") or existing.get("windows") or [])
                    seen_windows.add(window_index)
                    index["items"][key] = {
                        **existing,
                        **current,
                        "id": key,
                        "modo": modo,
                        "page": page,
                        "window": window_index,
                        "windows": sorted(seen_windows),
                        "data_inicial_janela": window_start,
                        "data_final_janela": window_end,
                        "href": item.get("href"),
                        "text": item.get("text"),
                        "status": status,
                        "captured_at": existing.get("captured_at") or now_iso(),
                        "updated_at": now_iso(),
                    }
                window_state["capturados"] = len(window_ids)
                save_index(
                    index_path,
                    index,
                    "indexando_requests",
                    "requests_page_scan_finished",
                    window=window_index,
                    page=page,
                    attempt=scan_attempt,
                    links=len(links),
                )
            if len(window_ids) == portal_total:
                break
            if scan_attempt < 3:
                save_index(
                    index_path,
                    index,
                    "indexando_requests",
                    "requests_window_retry",
                    window=window_index,
                    portal_total=portal_total,
                    captured=len(window_ids),
                    next_attempt=scan_attempt + 1,
                )
                time.sleep(scan_attempt * 2)

        window_state["capturados"] = len(window_ids)
        if len(window_ids) != portal_total:
            window_state["status"] = "incompleta"
            consolidate_page_totals(index)
            save_index(
                index_path,
                index,
                "indice_incompleto",
                "requests_window_count_mismatch",
                window=window_index,
                data_inicial=window_start,
                data_final=window_end,
                portal_total=portal_total,
                captured=len(window_ids),
            )
            raise RuntimeError(
                f"Indice incompleto na janela {window_start} a {window_end}: "
                f"Portal informou {portal_total}, indice capturou {len(window_ids)}."
            )
        window_state["status"] = "completa"
        window_state["completed_at"] = now_iso()
        consolidate_page_totals(index)
        save_index(
            index_path,
            index,
            "indexando_requests",
            "requests_window_finished",
            window=window_index,
            portal_total=portal_total,
            captured=len(window_ids),
        )

    consolidate_page_totals(index)
    captured = len(index.get("items", {}))
    save_index(
        index_path,
        index,
        "indice_pronto",
        "requests_scan_finished",
        captured=captured,
        soma_janelas=index["totals"].get("portal_registros_soma_janelas"),
        duplicados_entre_janelas=index["totals"].get("duplicados_entre_janelas"),
    )
    print(f"Indice pronto por requests: {captured} notas unicas em {len(date_windows)} janela(s).")


def extract_modal_data(html_text: str, base_url: str) -> dict:
    def first(patterns: list[str]) -> str | None:
        for pattern in patterns:
            match = re.search(pattern, html_text, flags=re.I | re.S)
            if match:
                return match.group(1)
        return None

    sitekey = first([
        r'id=["\']HCaptchaPublicKey["\'][^>]*value=["\']([^"\']+)',
        r'data-sitekey=["\']([^"\']+)',
        r'sitekey["\']?\s*[:=]\s*["\']([^"\']+)',
    ])
    redirect = first([
        r'id=["\']RedirectUrl["\'][^>]*value=["\']([^"\']+)',
        r'name=["\']RedirectUrl["\'][^>]*value=["\']([^"\']+)',
        r'redirectUrl=([^"\'&<>]+)',
    ])
    form_action = first([
        r'<form[^>]+id=["\']formModalCaptcha["\'][^>]*action=["\']([^"\']+)',
        r'<form[^>]+action=["\']([^"\']+)["\'][^>]+id=["\']formModalCaptcha["\']',
        r'<form[^>]+action=["\']([^"\']*ModalCaptcha[^"\']*)',
    ])
    token = first([
        r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)',
    ])
    return {
        "sitekey": sitekey,
        "redirect": unquote(redirect) if redirect else None,
        "form_action": urljoin(base_url, form_action) if form_action else None,
        "request_verification_token": token,
        "modal_url": base_url,
    }


def solver_api_health_url(solver_url: str) -> str:
    parsed = urlparse(solver_url)
    base_path = parsed.path.rsplit("/", 1)[0]
    return parsed._replace(path=base_path + "/health", fragment="").geturl()


def solver_api_job_url(solver_url: str, job_id: str) -> str:
    parsed = urlparse(solver_url)
    base_path = parsed.path.rsplit("/", 1)[0]
    return parsed._replace(
        path=base_path + "/jobs/" + quote(str(job_id), safe=""),
        fragment="",
    ).geturl()


def solver_url_candidates(primary: str) -> list[str]:
    return list(
        dict.fromkeys(
            url
            for url in (primary, SOLVER_FALLBACK_URL, *SOLVER_FALLBACK_URLS)
            if url
        )
    )


def solver_endpoint_label(url: str) -> str:
    """Identifica o endpoint sem persistir query strings ou fragmentos."""
    parsed = urlparse(str(url or ""))
    return parsed._replace(query="", fragment="").geturl()


def sanitized_solver_error(exc: Exception) -> str:
    """Mantem a causa operacional, removendo URLs e parametros sensiveis."""
    detail = str(exc or "solver_error")

    def clean_url(match: re.Match[str]) -> str:
        return solver_endpoint_label(match.group(0).rstrip(".,;)]}"))

    detail = re.sub(r"https?://[^\s]+", clean_url, detail)
    detail = re.sub(
        r"(?i)\b(token|secret|password|api[_-]?key)=([^\s&]+)",
        r"\1=<redacted>",
        detail,
    )
    return detail[:1000]


def is_local_solver_url(url: str) -> bool:
    return (urlparse(str(url or "")).hostname or "").lower() in {
        "127.0.0.1",
        "localhost",
        "::1",
    }


def is_visual_solver_failure(exc: Exception) -> bool:
    detail = str(exc or "").lower()
    return detail.startswith("solver:") and any(
        marker in detail
        for marker in (
            "visual_",
            "grade_9",
            "nao_achou_9",
            "captcha_prompt",
            "token_nao_voltou",
        )
    )


def solver_endpoint_cooldown_seconds(exc: Exception) -> int:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    detail = str(exc).lower()
    if status in {401, 403, 404} or "workspace" in detail and "disabled" in detail:
        return 3600
    if status == 429 or "too many requests" in detail:
        return 300
    if status in {500, 502, 503, 504} or "circuit_open" in detail:
        return 90
    # O endpoint respondeu e apenas esta tentativa visual nao venceu o
    # hCaptcha. Nao derrube o pool inteiro nem desvie as outras threads para o
    # ThinkPad: somente a requisicao atual deve tentar o fallback residencial.
    if detail.startswith("solver:"):
        return 0
    return 30


def mark_solver_endpoint_unavailable(url: str, exc: Exception) -> int:
    cooldown = solver_endpoint_cooldown_seconds(exc)
    if cooldown <= 0:
        return 0
    with SOLVER_ENDPOINT_COOLDOWN_LOCK:
        SOLVER_ENDPOINT_COOLDOWNS[url] = max(
            SOLVER_ENDPOINT_COOLDOWNS.get(url, 0.0), time.monotonic() + cooldown
        )
    return cooldown


def clear_solver_endpoint_cooldown(url: str) -> None:
    with SOLVER_ENDPOINT_COOLDOWN_LOCK:
        SOLVER_ENDPOINT_COOLDOWNS.pop(url, None)


def available_solver_url_candidates(primary: str) -> tuple[list[str], float | None]:
    now = time.monotonic()
    with SOLVER_ENDPOINT_COOLDOWN_LOCK:
        candidates = solver_url_candidates(primary)
        available = [url for url in candidates if SOLVER_ENDPOINT_COOLDOWNS.get(url, 0.0) <= now]
        waits = [SOLVER_ENDPOINT_COOLDOWNS[url] - now for url in candidates if SOLVER_ENDPOINT_COOLDOWNS.get(url, 0.0) > now]
    return available, min(waits) if waits else None


def wait_for_solver_candidates(primary: str) -> list[str]:
    deadline = time.monotonic() + SOLVER_REQUEST_TIMEOUT_SECONDS
    while True:
        candidates, wait_seconds = available_solver_url_candidates(primary)
        if candidates:
            return candidates
        if time.monotonic() >= deadline:
            raise RuntimeError("solver:endpoints_cooling_down_timeout")
        delay = min(max(wait_seconds or 1.0, 1.0), 30.0, deadline - time.monotonic())
        print(f"[Solver] Todos os endpoints em cooldown; nova verificacao em {delay:.0f}s")
        time.sleep(delay)


def require_solver_api(solver_url: str) -> str:
    last_error: Exception | None = None
    candidates = solver_url_candidates(solver_url)
    for candidate_index, candidate in enumerate(candidates):
        attempts = 2 if candidate_index == 0 and len(candidates) > 1 else 6
        for attempt in range(1, attempts + 1):
            try:
                response = requests.get(solver_api_health_url(candidate), timeout=12)
                response.raise_for_status()
                health = response.json()
                fatal = health.get("fatal_circuit") or {}
                if fatal.get("open"):
                    # No Modal o circuito e local ao container. Rejeitar todo o
                    # endpoint aqui impede que a requisicao alcance outra
                    # instancia saudavel do pool. O POST decide e faz fallback.
                    print(
                        "[Solver] Health degradado em um container; "
                        f"tentando o pool: {fatal.get('reason') or 'solver'}"
                    )
                if candidate != solver_url:
                    print(f"[Solver] Usando fallback saudavel: {candidate}")
                return candidate
            except Exception as exc:
                last_error = exc
                cooldown = mark_solver_endpoint_unavailable(candidate, exc)
                print(
                    f"[Solver] Health indisponivel ({attempt}/{attempts}) em {candidate}: "
                    f"{exc}; cooldown={cooldown}s"
                )
                if attempt < attempts:
                    time.sleep(min(3 * attempt, 12))
    raise RuntimeError(f"Nenhum solver ficou disponivel: {last_error}")


def solver_response_json(response: requests.Response) -> dict:
    try:
        data = response.json()
    except requests.exceptions.JSONDecodeError:
        # Gateways com keepalive podem deixar framing residual depois do JSON.
        # Aceite somente o primeiro objeto completo e continue validando os
        # campos success/token normalmente.
        raw = response.text.lstrip()
        data, _ = json.JSONDecoder().raw_decode(raw)
    if not isinstance(data, dict):
        raise RuntimeError("solver:invalid_json_object")
    return data


def solve_captcha_once(solver_url: str, sitekey: str, request_id: str) -> str | None:
    response = requests.post(
        solver_url,
        json={"sitekey": sitekey, "request_id": request_id},
        timeout=SOLVER_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = solver_response_json(response)
    if data.get("accepted") and data.get("job_id"):
        job_url = solver_api_job_url(solver_url, str(data["job_id"]))
        deadline = time.monotonic() + SOLVER_REQUEST_TIMEOUT_SECONDS
        poll_failures = 0
        while time.monotonic() < deadline:
            try:
                poll = requests.get(job_url, timeout=20)
            except requests.RequestException as exc:
                poll_failures += 1
                delay = min(2 * poll_failures, 12)
                print(f"[Solver] Poll transitorio ({poll_failures}): {exc}; nova tentativa em {delay}s")
                time.sleep(delay)
                continue
            if poll.status_code == 202:
                time.sleep(3)
                continue
            poll.raise_for_status()
            data = solver_response_json(poll)
            break
        else:
            raise RuntimeError("solver:async_job_timeout")
    if data.get("success") and data.get("token"):
        return data["token"]
    reason = data.get("reason") or data.get("error") or "solver_no_token"
    detail = data.get("error") or reason
    raise RuntimeError(f"solver:{reason}: {detail}")
    return None


def solve_captcha_with_url(solver_url: str, sitekey: str, request_id: str) -> str | None:
    errors = []
    candidates = wait_for_solver_candidates(solver_url)
    local_fallback_available = any(is_local_solver_url(url) for url in candidates)
    skip_remote_visual_fallbacks = False
    for candidate in candidates:
        if skip_remote_visual_fallbacks and not is_local_solver_url(candidate):
            continue
        try:
            token = solve_captcha_once(candidate, sitekey, request_id)
            clear_solver_endpoint_cooldown(candidate)
            record_solver_endpoint_event(candidate, "success", request_id)
            return token
        except Exception as exc:
            record_solver_endpoint_event(candidate, "failure", request_id, exc)
            cooldown = mark_solver_endpoint_unavailable(candidate, exc)
            safe_candidate = solver_endpoint_label(candidate)
            safe_error = sanitized_solver_error(exc)
            errors.append(f"{safe_candidate}: {safe_error}")
            if (
                candidate == solver_url
                and local_fallback_available
                and is_visual_solver_failure(exc)
            ):
                # A segunda conta Modal e reserva de quota/indisponibilidade.
                # Repetir nela o mesmo desafio visual antes do fallback
                # residencial duplica custo sem aumentar a diversidade de rota.
                skip_remote_visual_fallbacks = True
            print(
                f"[Solver] Falha em {safe_candidate}; tentando proximo endpoint: "
                f"{safe_error}; cooldown={cooldown}s"
            )
    raise RuntimeError("solver:all_endpoints_failed: " + " | ".join(errors))


def submit_captcha_requests(session: requests.Session, modal: dict, token: str, item: dict) -> requests.Response:
    redirect = modal.get("redirect") or item["href"]
    payload = {
        "h-captcha-response": token,
        "g-recaptcha-response": token,
        "HCaptchaToken": token,
        "CaptchaToken": token,
        "hcaptchaToken": token,
        "RedirectUrl": redirect,
    }
    if modal.get("sitekey"):
        payload["HCaptchaPublicKey"] = modal["sitekey"]
    if modal.get("request_verification_token"):
        payload["__RequestVerificationToken"] = modal["request_verification_token"]

    candidates = []
    if modal.get("form_action"):
        candidates.append(modal["form_action"])
    candidates.extend([
        "https://www.nfse.gov.br/EmissorNacional/DPS/ModalCaptcha/SolicitarCaptcha",
        "https://www.nfse.gov.br/EmissorNacional/DPS/ModalCaptcha/Validar",
        "https://www.nfse.gov.br/EmissorNacional/DPS/ModalCaptcha/Confirmar",
        "https://www.nfse.gov.br/EmissorNacional/DPS/ModalCaptcha",
    ])
    last_response = None
    for url in dict.fromkeys(candidates):
        try:
            last_response = session.post(
                url,
                data=payload,
                timeout=60,
                allow_redirects=True,
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Referer": modal.get("modal_url") or "https://www.nfse.gov.br/EmissorNacional/Notas/Recebidas",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            if response_is_xml(last_response):
                return last_response
            body = last_response.text.strip()
            if body.startswith("{"):
                try:
                    data = last_response.json()
                except ValueError:
                    data = {}
                redirect_url = data.get("RedirectUrl") or data.get("redirectUrl")
                if data.get("Sucesso") and redirect_url:
                    return session.get(
                        redirect_url,
                        timeout=60,
                        allow_redirects=True,
                        headers=nfse_navigation_headers(modal.get("modal_url")),
                    )
            if not response_is_login(last_response) and "SolicitarCaptcha" in url:
                return last_response
        except requests.RequestException:
            continue
    if last_response is not None:
        return last_response
    tipo = item.get("tipo_download") or ("pdf" if "/DANFSe/" in str(item.get("href") or "") else "xml")
    return session.get(
        nfse_modal_download_url(item, tipo),
        timeout=60,
        allow_redirects=True,
        headers=nfse_navigation_headers(redirect),
    )


def save_response_file(response: requests.Response, item: dict, download_dir: Path, tipo: str = "xml") -> Path:
    target_dir = download_dir_for_tipo(download_dir, tipo)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = filename_from_response(response, item, tipo)
    path = target_dir / filename
    if path.exists():
        stem = path.stem
        suffix = path.suffix or default_extension_for_tipo(tipo)
        path = target_dir / f"{stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}{suffix}"
    path.write_bytes(response.content)
    return path


def download_item_tipo_requests(session: requests.Session, item: dict, solver_url: str, download_dir: Path, tipo: str) -> dict:
    item_for_tipo = {**item, "href": nfse_download_url(item, tipo), "tipo_download": tipo}
    response = session.get(item_for_tipo["href"], timeout=60, allow_redirects=True, headers=nfse_navigation_headers())
    if response_is_login(response):
        return {"ok": False, "reason": "login", "tipo": tipo, "url": response.url}
    if expected_response_ok(response, tipo):
        path = save_response_file(response, item, download_dir, tipo)
        return {"ok": True, "tipo": tipo, "file": str(path), "method": f"direct_{tipo}"}

    modal = extract_modal_data(response.text if response.content else "", response.url)
    if not modal.get("sitekey"):
        redirect_path = "/" + item_for_tipo["href"].split("nfse.gov.br/", 1)[-1].lstrip("/")
        modal_url = "https://www.nfse.gov.br/EmissorNacional/DPS/ModalCaptcha/Abrir/?redirectUrl=" + quote(redirect_path, safe="")
        modal_response = session.get(
            modal_url,
            timeout=60,
            allow_redirects=True,
            headers={
                **nfse_navigation_headers(),
                "Referer": "https://www.nfse.gov.br/EmissorNacional/Notas/Recebidas",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if response_is_login(modal_response):
            return {"ok": False, "reason": "login_modal", "tipo": tipo, "url": modal_response.url}
        modal = extract_modal_data(modal_response.text if modal_response.content else "", modal_response.url)
        modal["modal_url"] = modal_response.url
    if not modal.get("sitekey"):
        target_dir = download_dir_for_tipo(download_dir, tipo)
        target_dir.mkdir(parents=True, exist_ok=True)
        snippet_path = target_dir / f"{item['id']}-{tipo}-sem-sitekey.html"
        snippet_path.write_bytes(response.content or b"")
        return {"ok": False, "reason": "sitekey_not_found", "tipo": tipo, "url": response.url, "html": str(snippet_path)}

    # Regra intencional: um captcha por arquivo. XML e PDF nao reaproveitam token.
    token = solve_captcha_with_url(solver_url, modal["sitekey"], f"{item['id']}-{tipo}")
    if not token:
        return {"ok": False, "reason": "solver_no_token", "tipo": tipo}

    submitted = submit_captcha_requests(session, modal, token, item_for_tipo)
    if response_is_login(submitted):
        return {"ok": False, "reason": "login_after_captcha", "tipo": tipo, "url": submitted.url}
    if not expected_response_ok(submitted, tipo):
        submitted = session.get(
            nfse_modal_download_url(item, tipo),
            timeout=60,
            allow_redirects=True,
            headers=nfse_navigation_headers(modal.get("modal_url")),
        )
    if expected_response_ok(submitted, tipo):
        path = save_response_file(submitted, item, download_dir, tipo)
        return {"ok": True, "tipo": tipo, "file": str(path), "method": f"requests_captcha_{tipo}"}

    target_dir = download_dir_for_tipo(download_dir, tipo)
    target_dir.mkdir(parents=True, exist_ok=True)
    debug_path = target_dir / f"{item['id']}-{tipo}-resposta-invalida.html"
    debug_path.write_bytes(submitted.content or b"")
    return {
        "ok": False,
        "reason": f"not_{tipo}_after_captcha",
        "tipo": tipo,
        "url": submitted.url,
        "status_code": submitted.status_code,
        "content_type": submitted.headers.get("content-type"),
        "html": str(debug_path),
    }


def item_has_tipos(item: dict, tipos: list[str]) -> bool:
    files_by_tipo = item.get("files_by_tipo") or {}
    if all(files_by_tipo.get(tipo) and Path(str(files_by_tipo[tipo])).exists() for tipo in tipos):
        return True
    files = item.get("files") or []
    for tipo in tipos:
        suffix = default_extension_for_tipo(tipo).lower()
        if not any(str(path).lower().endswith(suffix) and Path(str(path)).exists() for path in files):
            return False
    return True


def item_existing_file_for_tipo(item: dict, tipo: str) -> str | None:
    files_by_tipo = item.get("files_by_tipo") or {}
    existing = files_by_tipo.get(tipo)
    if existing:
        return str(existing)
    suffix = default_extension_for_tipo(tipo).lower()
    for file_path in item.get("files") or []:
        if str(file_path).lower().endswith(suffix):
            return str(file_path)
    return None


def item_required_tipos(item: dict, fallback: list[str]) -> list[str]:
    configured = item.get("required_tipos")
    if isinstance(configured, list):
        valid = [tipo for tipo in configured if tipo in {"xml", "pdf"}]
        if valid:
            return list(dict.fromkeys(valid))
    return list(fallback)


def reconcile_existing_downloads(index: dict, download_dir: Path, tipos: list[str]) -> int:
    """Reaproveita arquivos validos salvos antes de timeout, stop ou retry."""
    reconciled = 0
    for item in (index.get("items") or {}).values():
        note_id = str(item.get("id") or "").strip()
        if not note_id:
            continue
        files_by_tipo = dict(item.get("files_by_tipo") or {})
        files = list(item.get("files") or [])
        changed = False
        for tipo in tipos:
            if files_by_tipo.get(tipo) and Path(str(files_by_tipo[tipo])).is_file():
                continue
            target_dir = download_dir_for_tipo(download_dir, tipo)
            suffix = default_extension_for_tipo(tipo)
            matches = sorted(
                target_dir.glob(f"{note_id}*{suffix}"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            ) if target_dir.exists() else []
            valid = next((path for path in matches if path.is_file() and path.stat().st_size > 100), None)
            if valid is None:
                continue
            files_by_tipo[tipo] = str(valid)
            if str(valid) not in files:
                files.append(str(valid))
            changed = True
        if changed:
            item["files_by_tipo"] = files_by_tipo
            item["files"] = files
            reconciled += 1
        required = item_required_tipos(item, tipos)
        if all(files_by_tipo.get(tipo) and Path(str(files_by_tipo[tipo])).is_file() for tipo in required):
            item["status"] = "baixado"
    return reconciled


def download_item_requests(session: requests.Session, item: dict, solver_url: str, download_dir: Path, tipo_download: str = "xml") -> dict:
    tipos = item_required_tipos(item, normalize_download_tipos(tipo_download))
    files_by_tipo = dict(item.get("files_by_tipo") or {})
    methods_by_tipo = dict(item.get("methods_by_tipo") or {})
    errors = []

    for tipo in tipos:
        existing = item_existing_file_for_tipo({**item, "files_by_tipo": files_by_tipo}, tipo)
        if existing:
            files_by_tipo[tipo] = existing
            methods_by_tipo.setdefault(tipo, f"existing_{tipo}")
            continue
        result = download_item_tipo_requests(session, item, solver_url, download_dir, tipo)
        if result.get("ok"):
            files_by_tipo[tipo] = result.get("file")
            methods_by_tipo[tipo] = result.get("method")
        else:
            errors.append(result)
            break

    if errors:
        return {
            "ok": False,
            "reason": errors[-1].get("reason") or "download_tipo_failed",
            "errors": errors,
            "files_by_tipo": files_by_tipo,
            "methods_by_tipo": methods_by_tipo,
        }

    files = [files_by_tipo[tipo] for tipo in tipos if files_by_tipo.get(tipo)]
    return {
        "ok": True,
        "files": files,
        "files_by_tipo": files_by_tipo,
        "methods_by_tipo": methods_by_tipo,
        "method": "+".join(methods_by_tipo.get(tipo, tipo) for tipo in tipos),
        "tipo_download": tipo_download,
    }


def is_transient_solver_outage(result: dict) -> bool:
    detail = json.dumps(result or {}, ensure_ascii=False).lower()
    return any(
        marker in detail
        for marker in (
            "429",
            "503",
            "too many requests",
            "service unavailable",
            "all_endpoints_failed",
            "provider_circuit_open",
            "solver_circuit_open",
            "connection refused",
            "connection reset",
            "name or service not known",
        )
    )


def retry_backoff_seconds(retry_level: int, outage_streak: int) -> int:
    item_delay = min(2 ** max(1, retry_level), 30)
    if outage_streak <= 0:
        return item_delay
    outage_delay = min(2 ** min(outage_streak + 1, 7), 120)
    return max(item_delay, outage_delay)


def run_requests_downloads(
    index: dict,
    index_path: Path,
    session_path: Path,
    solver_url: str,
    download_dir: Path,
    max_items: int,
    concurrency: int,
    max_attempts: int = 3,
    cert_index: int | None = None,
    tipo_download: str = "xml",
    pfx_file: str | None = None,
    pfx_password_file: str | None = None,
) -> None:
    solver_url = require_solver_api(solver_url)
    tipos_download = normalize_download_tipos(tipo_download)
    index["tipo_download"] = tipo_download
    index["download_tipos"] = tipos_download
    reconciled = reconcile_existing_downloads(index, download_dir, tipos_download)
    if reconciled:
        save_index(
            index_path,
            index,
            "reconciliando_downloads",
            "existing_downloads_reconciled",
            items=reconciled,
            tipos=tipos_download,
        )
    session_data = json.loads(session_path.read_text(encoding="utf-8"))
    update_certificate_in_index(index, session_data)
    items = list(index.get("items", {}).values())
    items.sort(key=lambda item: (int(item.get("page") or 0), item.get("id") or ""))
    pending = [
        item
        for item in items
        if not (
            item.get("status") == "baixado"
            and item_has_tipos(item, item_required_tipos(item, tipos_download))
        )
    ]
    for item in pending:
        item["requests_attempts"] = 0
        if item.get("status") in {"erro", "executando"}:
            item["status"] = "pendente"
    if max_items:
        pending = pending[:max_items]
    save_index(index_path, index, "baixando_requests", "requests_download_started", pending=len(pending), concurrency=concurrency, tipo_download=tipo_download, tipos=tipos_download)

    def worker(item: dict) -> tuple[str, dict]:
        local_session = requests_session_from_data(session_data)
        return item["id"], download_item_requests(local_session, item, solver_url, download_dir, tipo_download=tipo_download)

    queue = list(pending)
    max_workers = max(1, concurrency)
    started_count = 0
    solver_outage_streak = 0

    def submit_next(executor: concurrent.futures.ThreadPoolExecutor, futures: dict) -> bool:
        nonlocal started_count
        if not queue:
            return False
        item = queue.pop(0)
        key = item["id"]
        meta = index["items"][key]
        attempts = int(meta.get("requests_attempts") or 0) + 1
        meta["requests_attempts"] = attempts
        meta["status"] = "executando"
        meta["updated_at"] = now_iso()
        meta.setdefault("attempts", []).append({"at": now_iso(), "attempt": attempts, "mode": "requests", "status": "started"})
        started_count += 1
        save_index(index_path, index, "baixando_requests", "item_started", id=key, attempt=attempts, active=len(futures) + 1, queue=len(queue))
        futures[executor.submit(worker, item)] = key
        return True

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures: dict[concurrent.futures.Future, str] = {}
        while queue and len(futures) < max_workers:
            submit_next(executor, futures)

        while futures:
            done, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
            retry_items = []
            login_errors = []
            saw_solver_outage = False
            saw_success = False

            for future in done:
                key = futures.pop(future)
                try:
                    _, result = future.result()
                except Exception as exc:
                    result = {"ok": False, "reason": str(exc)}

                if result.get("ok"):
                    saw_success = True
                    index["items"][key]["status"] = "baixado"
                    existing_files = list(index["items"][key].get("files") or [])
                    for file_path in result.get("files", []) or []:
                        if file_path and file_path not in existing_files:
                            existing_files.append(file_path)
                    index["items"][key]["files"] = existing_files
                    existing_files_by_tipo = dict(index["items"][key].get("files_by_tipo") or {})
                    existing_files_by_tipo.update(result.get("files_by_tipo", {}) or {})
                    index["items"][key]["files_by_tipo"] = existing_files_by_tipo
                    existing_methods_by_tipo = dict(index["items"][key].get("methods_by_tipo") or {})
                    existing_methods_by_tipo.update(result.get("methods_by_tipo", {}) or {})
                    index["items"][key]["methods_by_tipo"] = existing_methods_by_tipo
                    index["items"][key]["tipo_download"] = tipo_download
                    index["items"][key]["downloaded_at"] = now_iso()
                    index["items"][key]["download_method"] = result.get("method")
                    index["items"][key].pop("last_error", None)
                    save_index(index_path, index, "baixando_requests", "item_downloaded", id=key, method=result.get("method"), active=len(futures), queue=len(queue))
                    print(f"Baixado via requests: {key}")
                    continue

                reason = str(result.get("reason") or "")
                saw_solver_outage = saw_solver_outage or is_transient_solver_outage(result)
                attempts = int(index["items"][key].get("requests_attempts") or 1)
                index["items"][key]["last_error"] = result
                if result.get("files_by_tipo"):
                    index["items"][key]["files_by_tipo"] = result.get("files_by_tipo", {})
                    existing_files = list(index["items"][key].get("files") or [])
                    for file_path in (result.get("files_by_tipo", {}) or {}).values():
                        if file_path and file_path not in existing_files:
                            existing_files.append(file_path)
                    index["items"][key]["files"] = existing_files
                if result.get("methods_by_tipo"):
                    index["items"][key]["methods_by_tipo"] = result.get("methods_by_tipo", {})

                if reason in {"login", "login_modal", "login_after_captcha"}:
                    index["items"][key]["status"] = "pendente"
                    login_errors.append({"id": key, "error": result})
                    retry_items.append(index["items"][key])
                    print(f"Sessao caiu via requests: {key} :: {result}")
                    continue

                if attempts < max_attempts:
                    index["items"][key]["status"] = "pendente"
                    retry_items.append(index["items"][key])
                    save_index(index_path, index, "baixando_requests", "item_retry_scheduled", id=key, attempt=attempts, error=result, active=len(futures), queue=len(queue))
                    print(f"Falhou via requests, vou tentar de novo: {key} :: {result}")
                else:
                    index["items"][key]["status"] = "erro"
                    save_index(index_path, index, "baixando_requests", "item_failed_max_attempts", id=key, attempts=attempts, error=result, active=len(futures), queue=len(queue))
                    print(f"Falhou via requests apos {attempts} tentativas: {key} :: {result}")

            if login_errors:
                save_index(index_path, index, "renovando_sessao", "requests_login_failed", errors=login_errors, active=len(futures), queue=len(queue))
                print("Sessao caiu no requests. Renovando sessao pelo certificado do indice...")
                session_data = regenerate_session(
                    index,
                    index_path,
                    session_path,
                    index.get("target_url") or TARGET_URL,
                    cert_index,
                    pfx_file,
                    pfx_password_file,
                )

            retry_items = [
                item for item in retry_items
                if int(item.get("requests_attempts") or 0) < max_attempts or login_errors
            ]
            retry_items.sort(key=lambda item: (int(item.get("page") or 0), item.get("id") or ""))
            queue.extend(retry_items)

            if saw_success:
                solver_outage_streak = 0
            elif saw_solver_outage:
                solver_outage_streak += 1
            else:
                solver_outage_streak = 0

            if retry_items:
                retry_level = max(int(item.get("requests_attempts") or 1) for item in retry_items)
                retry_delay = retry_backoff_seconds(retry_level, solver_outage_streak)
                save_index(
                    index_path,
                    index,
                    "baixando_requests",
                    "retry_backoff_wait",
                    seconds=retry_delay,
                    retry_level=retry_level,
                    solver_outage_streak=solver_outage_streak,
                    items=len(retry_items),
                )
                time.sleep(retry_delay)

            while queue and len(futures) < max_workers:
                submit_next(executor, futures)

            save_index(index_path, index, "baixando_requests", "requests_pool_tick", started=started_count, active=len(futures), queue=len(queue))

    # Marca qualquer pendente que esgotou tentativas como erro, mantendo retomada clara.
    for item in items:
        if item.get("status") == "pendente" and int(item.get("requests_attempts") or 0) >= max_attempts:
            key = item["id"]
            index["items"][key]["status"] = "erro"
    save_index(index_path, index, "baixando_requests", "requests_download_finished")


def load_cookies_and_navigate(port: int, session_data: dict, download_dir: Path) -> None:
    client = CdpClient(wait_for_cdp(port))

    try:
        client.call("Network.enable")
        client.call("Page.enable")
        configure_downloads(client, download_dir)

        print("Aplicando cookies...")

        for cookie in session_data.get("cookies", []):
            params = {
                "url": "https://www.nfse.gov.br/",
                "name": cookie["name"],
                "value": cookie["value"],
                "path": cookie.get("path") or "/",
                "secure": bool(cookie.get("secure")),
                "httpOnly": bool(cookie.get("httpOnly")),
                "sameSite": "Lax",
            }

            domain = cookie.get("domain")
            if domain and domain.startswith("."):
                params["domain"] = domain

            if cookie.get("expires"):
                try:
                    dt = datetime.fromisoformat(cookie["expires"].replace("Z", "+00:00"))
                    params["expires"] = int(dt.timestamp())
                except Exception:
                    pass

            result = client.call("Network.setCookie", params)

            if not result.get("success"):
                raise RuntimeError(f"Falhou ao gravar cookie {cookie['name']}")

        current = client.call("Network.getCookies", {
            "urls": ["https://www.nfse.gov.br/"]
        })

        loaded = {cookie["name"] for cookie in current.get("cookies", [])}

        missing = [
            cookie["name"]
            for cookie in session_data.get("cookies", [])
            if cookie["name"] not in loaded
        ]

        if missing:
            raise RuntimeError(f"Cookies nao apareceram no Chrome: {', '.join(missing)}")

        target_url = session_data.get("start_url") or TARGET_URL

        print(f"Navegando diretamente para: {target_url}")
        client.call("Page.navigate", {"url": target_url})

        print("Aguardando carregamento completo...")
        time.sleep(8)

        logged = client.eval("""
            !location.href.includes('/Login') &&
            (
                document.body.innerText.includes('Sair') ||
                document.querySelector('a[href*="logout"], a[href*="LogOff"], button[onclick*="logout"]') !== null ||
                location.href.includes('/EmissorNacional/Notas') ||
                location.href.includes('/EmissorNacional/Dashboard') ||
                document.querySelector('.usuario-logado, .perfil-usuario') !== null
            )
        """)

        print(f"Status Login: {'LOGADO' if logged else 'NAO DETECTADO'}")

        if not logged:
            print("Recarregando página para garantir login...")
            client.call("Page.reload", {"ignoreCache": True})
            time.sleep(6)

            logged = client.eval("""
                !location.href.includes('/Login') &&
                (
                    document.body.innerText.includes('Sair') ||
                    location.href.includes('/EmissorNacional/Notas') ||
                    location.href.includes('/EmissorNacional/Dashboard')
                )
            """)

            if not logged:
                current_url = client.eval("location.href")
                raise RuntimeError(f"Sessao nao entrou no portal; URL atual: {current_url}")

    finally:
        client.close()


# ===================== FUNÇÕES DE NOTAS =====================

def first_page_xml_links(client: CdpClient) -> list[dict]:
    expression = """
(() => {
  window.scrollTo(0, document.body.scrollHeight);

  return [...document.querySelectorAll('table tbody tr, .table tbody tr, tr')]
    .map((tr, index) => {
      const xml = [...tr.querySelectorAll('a')].find(a =>
        (a.textContent || a.innerText || '').toLowerCase().includes('xml') &&
        a.href.includes('/Notas/Download')
      );

      if (!xml) return null;

      return {
        index,
        href: xml.href,
        text: tr.innerText.trim()
      };
    })
    .filter(Boolean);
})()
"""
    result = client.eval(expression)
    return result if isinstance(result, list) else []


def click_xml_link(client: CdpClient, href: str) -> dict:
    expression = f"""
(() => {{
  const href = {json.dumps(href)};

  const link = [...document.querySelectorAll('a')].find(a =>
    a.href === href ||
    a.getAttribute('href') === href.replace(location.origin, '')
  );

  if (!link) return {{ok: false, reason: 'link nao encontrado'}};

  link.scrollIntoView({{block: 'center'}});
  link.click();

  return {{ok: true}};
}})()
"""
    result = client.eval(expression)
    return result if isinstance(result, dict) else {"ok": False, "reason": "retorno invalido"}


# ===================== CAPTCHA =====================

def wait_modal_and_sitekey(client: CdpClient, timeout: int = 60) -> dict:
    expression = """
(() => {
  const modal = document.querySelector('#modalCaptcha');
  const visible = !!modal && getComputedStyle(modal).display !== 'none';

  const sitekey =
    document.querySelector('#HCaptchaPublicKey')?.value ||
    document.querySelector('#hcaptcha-container')?.getAttribute('data-sitekey');

  const redirect = document.querySelector('#RedirectUrl')?.value || null;

  return {
    visible,
    sitekey,
    redirect,
    url: location.href
  };
})()
"""

    end = time.time() + timeout

    while time.time() < end:
        data = client.eval(expression)

        if data.get("visible") and data.get("sitekey"):
            return data

        time.sleep(1)

    raise RuntimeError("Modal hCaptcha não apareceu.")


def inject_and_submit(client: CdpClient, token: str) -> dict:
    expression = f"""
(() => {{
  const token = {json.dumps(token)};

  const names = [
    'h-captcha-response',
    'g-recaptcha-response',
    'HCaptchaToken',
    'CaptchaToken',
    'hcaptchaToken'
  ];

  const touched = [];

  for (const name of names) {{
    let fields = [...document.querySelectorAll(`[name="${{name}}"], #${{CSS.escape(name)}}`)];

    if (!fields.length) {{
      const field = document.createElement(name.includes('response') ? 'textarea' : 'input');
      field.name = name;
      field.id = name;
      field.style.display = 'none';

      (
        document.querySelector('#formModalCaptcha') ||
        document.querySelector('form') ||
        document.body
      ).appendChild(field);

      fields = [field];
    }}

    fields.forEach((el) => {{
      el.value = token;
      el.setAttribute('value', token);
      el.dispatchEvent(new Event('input', {{ bubbles: true }}));
      el.dispatchEvent(new Event('change', {{ bubbles: true }}));
      touched.push(el.name || el.id || el.tagName);
    }});
  }}

  const button = document.querySelector('#btnSubmitHCaptcha');

  if (button) button.click();

  return {{
    touched,
    clicked: !!button,
    redirect: document.querySelector('#RedirectUrl')?.value || null,
    hLen: (document.querySelector('[name="h-captcha-response"]') || {{ value: '' }}).value.length
  }};
}})()
"""
    result = client.eval(expression)
    return result if isinstance(result, dict) else {}


def modal_still_visible(client: CdpClient) -> bool:
    return bool(client.eval("""
        !!document.querySelector('#modalCaptcha') &&
        getComputedStyle(document.querySelector('#modalCaptcha')).display !== 'none'
    """))


def reset_modal(client: CdpClient) -> None:
    try:
        client.eval("""
            document.querySelector('#btnLimpar, [data-dismiss="modal"], .btn-close, .close')?.click()
        """)
        time.sleep(0.5)
    except Exception:
        pass


def solve_captcha_via_api(sitekey: str, url: str) -> str | None:
    print("[Solver] Enviando captcha para API...")

    try:
        r = requests.post(
            SOLVER_API_URL,
            json={"sitekey": sitekey},
            timeout=180
        )

        r.raise_for_status()
        data = r.json()

        if data.get("success") and data.get("token"):
            print(f"[Solver] Token recebido ({len(data['token'])} caracteres)")
            return data["token"]

        print(f"[Solver] Resposta sem token: {data}")

    except Exception as e:
        print(f"[Solver] Erro: {e}")

    return None


# ===================== MAIN =====================

def main() -> int:
    global SOLVER_API_URL
    parser = argparse.ArgumentParser(description="NFS-e - indexa todas as paginas e baixa XML com retomada por JSON.")
    parser.add_argument("--modo", choices=sorted(MODE_URLS), default="recebidas")
    parser.add_argument("--session", default=str(SESSION_FILE))
    parser.add_argument("--profile", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--browser", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--max", type=int, default=0, help="Limite de notas para processar depois de indexar; 0 = todas.")
    parser.add_argument("--download-dir", default=str(DOWNLOAD_DIR))
    parser.add_argument("--tipo-download", choices=["xml", "pdf", "ambos"], default="xml", help="Baixa XML, PDF/DANFSe ou ambos. Em 'ambos', resolve um captcha para cada arquivo.")
    parser.add_argument("--index", default=None, help="Arquivo JSON de indice/retomada.")
    parser.add_argument("--data-inicial", default=None, help="Data inicial do filtro, em DD/MM/AAAA ou AAAA-MM-DD.")
    parser.add_argument("--data-final", default=None, help="Data final do filtro, em DD/MM/AAAA ou AAAA-MM-DD.")
    parser.add_argument("--somente-index", action="store_true", help="So pagina e monta/atualiza o JSON.")
    parser.add_argument("--recriar-index", action="store_true", help="Ignora itens antigos e reconstrói o indice.")
    parser.add_argument("--renovar-inicio", action="store_true", help="Roda 02_gera_sessao_txt.py antes de consultar o portal por requests.")
    parser.add_argument("--thumbprint", default=None, help="Certificado usado ao renovar sessao.")
    parser.add_argument("--cert-index", type=int, default=None, help="Indice do certificado usado ao renovar sessao.")
    parser.add_argument("--pfx-file", default=None, help="Arquivo .pfx/.p12 usado ao renovar sessao.")
    parser.add_argument("--pfx-password-file", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--manter-aberto", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--solver-url", default=SOLVER_API_URL, help="Endpoint da API resolvedora, ex: http://127.0.0.1:8765/solve.")
    parser.add_argument("--concorrencia", type=int, default=1, help="Quantidade de downloads simultaneos no modo requests.")
    parser.add_argument("--sem-navegador", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--forcar-indexar", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--download-browser", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    SOLVER_API_URL = args.solver_url

    target_url = MODE_URLS[args.modo]
    data_inicial = normalize_date_for_portal(args.data_inicial)
    data_final = normalize_date_for_portal(args.data_final)
    session_path = Path(args.session).resolve()
    download_dir = Path(args.download_dir).resolve() / args.modo
    index_path = Path(args.index).resolve() if args.index else (BASE_DIR / f"indice_nfse_{args.modo}.json").resolve()
    pfx_file = str(Path(args.pfx_file).resolve()) if args.pfx_file else None
    pfx_password_file = str(Path(args.pfx_password_file).resolve()) if args.pfx_password_file else None

    if args.recriar_index and index_path.exists():
        index_path.unlink()
    index = load_index(index_path, args.modo)
    index["target_url"] = target_url
    index["filtros"] = {
        "data_inicial": data_inicial,
        "data_final": data_final,
        "modo": args.modo,
        "tipo_download": args.tipo_download,
        "portal_date_filter_applied": bool(data_inicial and data_final),
        "include_retroactive_notes": True,
        "period_strategy": "monthly_windows_max_30_days",
        "date_window_max_days": PORTAL_MAX_PERIOD_DAYS,
    }
    index["tipo_download"] = args.tipo_download
    index["download_tipos"] = normalize_download_tipos(args.tipo_download)
    index["download_dir"] = str(download_dir)
    if args.thumbprint:
        index.setdefault("session", {})["certificate_thumbprint"] = args.thumbprint
    if pfx_file:
        index.setdefault("session", {})["certificate_source"] = "pfx"
        index["session"]["pfx_file"] = pfx_file
    elif session_path.exists():
        try:
            existing_session = json.loads(session_path.read_text(encoding="utf-8"))
            update_certificate_in_index(index, existing_session)
        except Exception:
            pass
    save_index(index_path, index, "iniciando", "start", modo=args.modo, data_inicial=data_inicial, data_final=data_final)

    if args.renovar_inicio or not session_path.exists():
        regenerate_session(index, index_path, session_path, target_url, args.cert_index, pfx_file, pfx_password_file)

    if not session_path.exists():
        raise FileNotFoundError(f"Sessao nao encontrada: {session_path}")

    session_data = json.loads(session_path.read_text(encoding="utf-8"))
    session_data["start_url"] = target_url
    update_certificate_in_index(index, session_data)
    save_index(index_path, index, "sessao_carregada", "session_loaded")

    has_index_items = bool(index.get("items"))
    if not has_index_items or (args.forcar_indexar and not index.get("skip_force_reindex")):
        print("Indexando por requests, sem navegador principal.")
        run_requests_index(
            index=index,
            index_path=index_path,
            session_path=session_path,
            modo=args.modo,
            target_url=target_url,
            data_inicial=data_inicial,
            data_final=data_final,
            cert_index=args.cert_index,
            pfx_file=pfx_file,
            pfx_password_file=pfx_password_file,
        )
    else:
        print("Usando indice existente; sem navegador principal.")

    if args.somente_index:
        save_index(index_path, index, "finalizado", "requests_index_only_finished")
        print("Indexacao por requests finalizada.")
        return 0

    print(f"Indice: {index_path}")
    print(f"API resolvedora externa: {SOLVER_API_URL}")
    print(f"Concorrencia: {args.concorrencia}")
    run_requests_downloads(
        index=index,
        index_path=index_path,
        session_path=session_path,
        solver_url=SOLVER_API_URL,
        download_dir=download_dir,
        max_items=args.max,
        concurrency=args.concorrencia,
        max_attempts=args.retries,
        cert_index=args.cert_index,
        tipo_download=args.tipo_download,
        pfx_file=pfx_file,
        pfx_password_file=pfx_password_file,
    )
    consolidate_page_totals(index)
    final_status = final_download_status(index, args.max)
    save_index(index_path, index, final_status, "requests_run_finished")
    print("Download por requests finalizado.")
    return 0

    browser = find_browser(args.browser)
    port = free_port()
    download_dir.mkdir(parents=True, exist_ok=True)

    if args.profile:
        profile = Path(args.profile).resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        profile = (PROFILE_BASE_DIR / f"nfse-{args.modo}-{stamp}").resolve()
    profile.mkdir(parents=True, exist_ok=True)
    write_browser_preferences(profile, download_dir)

    cmd = [
        browser,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--new-window",
        "--no-first-run",
        "--disable-default-apps",
        "--disable-popup-blocking",
        "--start-maximized",
        "--safebrowsing-disable-download-protection",
        "--disable-features=DownloadBubble,DownloadBubbleV2",
        "about:blank",
    ]

    print("Abrindo navegador principal...")
    subprocess.Popen(cmd)
    time.sleep(4)
    try:
        load_cookies_and_navigate(port, session_data, download_dir)
    except Exception as exc:
        if "Sessao nao entrou no portal" not in str(exc):
            raise
        print("Sessao inicial caiu no login. Renovando e tentando de novo...")
        save_index(index_path, index, "renovando_sessao", "initial_session_failed", error=str(exc))
        session_data = regenerate_session(index, index_path, session_path, target_url, args.cert_index, pfx_file, pfx_password_file)
        session_data["start_url"] = target_url
        load_cookies_and_navigate(port, session_data, download_dir)

    STATE_FILE.write_text(json.dumps({
        "port": port,
        "profile": str(profile),
        "browser": browser,
        "url": target_url,
        "modo": args.modo,
        "download_dir": str(download_dir),
        "index": str(index_path),
        "created_at": now_iso(),
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("Navegador aberto.")
    print(f"Modo: {args.modo}")
    print(f"Perfil: {profile}")
    print(f"Porta: {port}")
    print(f"Indice: {index_path}")
    print(f"Downloads: {download_dir}")
    print()

    client = CdpClient(wait_for_cdp(port))
    current_page = None

    try:
        client.call("Network.enable")
        client.call("Page.enable")
        configure_downloads(client, download_dir)
        client.close()
        client = CdpClient(wait_for_cdp(port))
        client.call("Network.enable")
        client.call("Page.enable")

        save_index(index_path, index, "indexando", "scan_started")
        navigate_and_wait(client, target_url, 5)
        ensure_logged_in(client, index, index_path, session_path, target_url, args.cert_index, pfx_file, pfx_password_file)
        if data_inicial or data_final:
            filter_result = apply_date_filters(client, data_inicial, data_final)
            save_index(
                index_path,
                index,
                "indexando",
                "date_filter_skipped_include_retroactive",
                data_inicial_referencia=data_inicial,
                data_final_referencia=data_final,
                result=filter_result,
                url=target_url,
            )

        first = page_snapshot(client)
        portal_total = first.get("totalRegistros")
        last_page = int(first.get("lastPage") or 1)
        index["totals"]["portal_registros"] = portal_total
        index["totals"]["paginas"] = last_page
        save_index(index_path, index, "indexando", "pagination_detected", total=portal_total, paginas=last_page)

        if last_page > 1:
            print(f"Paginador detectado: indo na ultima pagina ({last_page}) para confirmar.")
            navigate_and_wait(client, build_page_url(target_url, last_page), 4)
            ensure_logged_in(client, index, index_path, session_path, build_page_url(target_url, last_page), args.cert_index, pfx_file, pfx_password_file)
            last_snapshot = page_snapshot(client)
            if last_snapshot.get("lastPage"):
                last_page = max(last_page, int(last_snapshot.get("lastPage") or last_page))
                index["totals"]["paginas"] = last_page
                save_index(index_path, index, "indexando", "last_page_confirmed", paginas=last_page)

        print(f"Total informado no portal: {portal_total}")
        print(f"Paginas a capturar: {last_page}")

        for page in range(1, last_page + 1):
            page_url = build_page_url(target_url, page)
            print(f"Indexando pagina {page}/{last_page}: {page_url}")
            save_index(index_path, index, "indexando", "page_scan_started", page=page)
            navigate_and_wait(client, page_url, 4)
            ensure_logged_in(client, index, index_path, session_path, page_url, args.cert_index, pfx_file, pfx_password_file)
            snapshot = page_snapshot(client)
            links = scan_current_page_links(client, args.modo, page)
            index.setdefault("pages", {})[str(page)] = {
                "page": page,
                "url": page_url,
                "status": "capturada",
                "captured_at": now_iso(),
                "portal_total_text": snapshot.get("descricao"),
                "portal_total_registros": snapshot.get("totalRegistros"),
                "links_count": len(links),
            }
            for item in links:
                key = item["id"] or note_id_from_href(item["href"])
                existing = index.setdefault("items", {}).get(key, {})
                if existing.get("status") == "baixado":
                    status = "baixado"
                else:
                    status = existing.get("status") or "pendente"
                index["items"][key] = {
                    **existing,
                    "id": key,
                    "modo": args.modo,
                    "page": page,
                    "href": item.get("href"),
                    "text": item.get("text"),
                    "status": status,
                    "captured_at": existing.get("captured_at") or now_iso(),
                    "updated_at": now_iso(),
                }
            save_index(index_path, index, "indexando", "page_scan_finished", page=page, links=len(links))

        consolidate_page_totals(index)
        portal_total = index.get("totals", {}).get("portal_registros")
        captured = len(index.get("items", {}))
        if portal_total is not None and captured != int(portal_total):
            save_index(index_path, index, "indice_incompleto", "count_mismatch", portal_total=portal_total, captured=captured)
            print(f"AVISO: portal informou {portal_total}, indice capturou {captured}.")
        else:
            save_index(index_path, index, "indice_pronto", "scan_finished", captured=captured)
            print(f"Indice pronto: {captured} notas.")

        if args.somente_index:
            return 0

        if not args.download_browser:
            print("Fechando navegador principal; downloads serao feitos por requests.")
            try:
                client.call("Browser.close")
            except Exception:
                pass
            run_requests_downloads(
                index=index,
                index_path=index_path,
                session_path=session_path,
                solver_url=SOLVER_API_URL,
                download_dir=download_dir,
                max_items=args.max,
                concurrency=args.concorrencia,
                max_attempts=args.retries,
                cert_index=args.cert_index,
                tipo_download=args.tipo_download,
                pfx_file=pfx_file,
                pfx_password_file=pfx_password_file,
            )
            consolidate_page_totals(index)
            final_status = final_download_status(index, args.max)
            save_index(index_path, index, final_status, "requests_run_finished")
            print("Download por requests finalizado.")
            return 0

        require_solver_api(SOLVER_API_URL)

        items = list(index.get("items", {}).values())
        items.sort(key=lambda item: (int(item.get("page") or 0), item.get("id") or ""))
        pending = [item for item in items if not (item.get("status") == "baixado" and item_has_tipos(item, tipos_download))]
        if args.max:
            pending = pending[:args.max]
        save_index(index_path, index, "baixando", "download_started", pending=len(pending))

        for pos, item in enumerate(pending, 1):
            key = item["id"]
            page = int(item.get("page") or 1)
            page_url = build_page_url(target_url, page)
            print()
            print(f"[{pos}/{len(pending)}] Pagina {page} :: {item.get('text', '')[:100]}")
            if current_page != page:
                navigate_and_wait(client, page_url, 4)
                ensure_logged_in(client, index, index_path, session_path, page_url, args.cert_index, pfx_file, pfx_password_file)
                current_page = page

            index["items"][key]["status"] = "executando"
            index["items"][key]["updated_at"] = now_iso()
            save_index(index_path, index, "baixando", "item_started", id=key, page=page)
            ok = False
            last_error = None

            for attempt in range(1, args.retries + 1):
                before_files = {f.name for f in list_download_files(download_dir)}
                index["items"][key]["attempt"] = attempt
                index["items"][key].setdefault("attempts", []).append({"at": now_iso(), "attempt": attempt, "status": "started"})
                save_index(index_path, index, "baixando", "item_attempt", id=key, attempt=attempt)
                try:
                    reset_modal(client)
                    clicked = click_xml_link(client, item["href"])
                    if not clicked.get("ok"):
                        raise RuntimeError(f"Link XML nao clicado: {clicked.get('reason', 'sem motivo')}")

                    time.sleep(2)
                    renewed = ensure_logged_in(client, index, index_path, session_path, page_url, args.cert_index, pfx_file, pfx_password_file)
                    if renewed:
                        current_page = None
                        raise RuntimeError("Sessao renovada apos clique; repetir item.")

                    direct_files = wait_for_new_download(download_dir, before_files, timeout=6)
                    if direct_files and not modal_still_visible(client):
                        ok = True
                        index["items"][key]["files"] = [str(f) for f in direct_files]
                        break

                    modal = wait_modal_and_sitekey(client, timeout=35)
                    token = solve_captcha_via_api(modal["sitekey"], modal.get("redirect") or modal.get("url") or target_url)
                    if not token:
                        raise RuntimeError("API resolvedora nao retornou token.")
                    inject_result = inject_and_submit(client, token)
                    index["items"][key]["last_inject"] = inject_result
                    save_index(index_path, index, "baixando", "captcha_submitted", id=key, clicked=inject_result.get("clicked"))
                    time.sleep(5)

                    renewed = ensure_logged_in(client, index, index_path, session_path, page_url, args.cert_index, pfx_file, pfx_password_file)
                    if renewed:
                        current_page = None
                        raise RuntimeError("Sessao caiu depois do captcha; repetir item.")

                    new_files = wait_for_new_download(download_dir, before_files, timeout=30)
                    if new_files:
                        index["items"][key]["files"] = [str(f) for f in new_files]
                    if not modal_still_visible(client):
                        ok = True
                        break
                    raise RuntimeError("Modal continuou aberto depois do submit.")
                except Exception as exc:
                    last_error = str(exc)
                    print(f"Erro tentativa {attempt}: {last_error}")
                    index["items"][key]["last_error"] = last_error
                    index["items"][key]["updated_at"] = now_iso()
                    save_index(index_path, index, "baixando", "item_attempt_error", id=key, attempt=attempt, error=last_error)
                    time.sleep(2)

            if ok:
                print("Baixado.")
                index["items"][key]["status"] = "baixado"
                index["items"][key]["downloaded_at"] = now_iso()
                index["items"][key]["updated_at"] = now_iso()
                index["items"][key].pop("last_error", None)
                save_index(index_path, index, "baixando", "item_downloaded", id=key)
            else:
                print(f"Falhou: {last_error}")
                index["items"][key]["status"] = "erro"
                index["items"][key]["last_error"] = last_error
                index["items"][key]["updated_at"] = now_iso()
                save_index(index_path, index, "baixando", "item_failed", id=key, error=last_error)

        consolidate_page_totals(index)
        final_status = final_download_status(index, args.max)
        save_index(index_path, index, final_status, "run_finished")
        print()
        print("=== RESUMO ===")
        print(f"Indice: {index_path}")
        print(f"Total portal: {index['totals'].get('portal_registros')}")
        print(f"Capturados: {index['totals'].get('capturados')}")
        print(f"Baixados: {index['totals'].get('baixados')}")
        print(f"Erros: {index['totals'].get('erros')}")
        print(f"Downloads: {download_dir}")

        if args.manter_aberto:
            print("Navegador continua aberto. Pressione Ctrl+C para sair.")
            try:
                while True:
                    time.sleep(30)
            except KeyboardInterrupt:
                pass

    except KeyboardInterrupt:
        save_index(index_path, index, "interrompido", "keyboard_interrupt")
        print("Encerrado pelo usuario.")
    finally:
        client.close()
        print("Script finalizado.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        raise SystemExit(1)
