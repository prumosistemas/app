from __future__ import annotations

import argparse
from contextlib import contextmanager
import html
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import requests
import websocket
from bs4 import BeautifulSoup, Tag


PROJECT_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("GOOGLE_AI_STATE_DIR", str(PROJECT_DIR))).expanduser().resolve()
STATE_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)
COOKIE_FILE = STATE_DIR / "cookies_google_limpo.json"
COOKIE_BACKUP_FILE = STATE_DIR / "cookies_google_limpo.backup.json"
FIREFOX_SEED_DIR = STATE_DIR / "seed_firefox_profile"
FIREFOX_SEED_COOKIE_FILE = FIREFOX_SEED_DIR / "cookies.sqlite"
SESSION_RECOVERY_LOCK = STATE_DIR / ".session_recovery.lock"
SESSION_RECOVERY_STATE = STATE_DIR / "session_recovery_state.json"
RECOVERY_BROWSER_WAIT_SECONDS = tuple(
    float(value)
    for value in os.environ.get("GOOGLE_AI_RECOVERY_WAIT_SECONDS", "12,30,60").split(",")
    if value.strip()
)
SEARCH_URL = "https://www.google.com/search"
TEXT_ASYNC_URL = "https://www.google.com/async/folwr"
IMAGE_ASYNC_URL = "https://www.google.com/async/folif"
LENS_UPLOAD_URL = "https://lens.google.com/v3/upload"

ANONYMOUS_COOKIE_NAMES = {
    "AEC",
    "CONSENT",
    "DV",
    "NID",
    "SEARCH_SAMESITE",
    "SNID",
    "SOCS",
    "__Secure-STRP",
}

ACCOUNT_COOKIE_PREFIXES = (
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
)

# Esses cookies carregam a habilitação anônima do Modo IA. O Google pode
# devolver versões genéricas durante uma resposta; elas não devem substituir
# a sessão que já foi comprovada como funcional.
STICKY_COOKIE_NAMES = {"NID", "AEC"}

INTERNAL_HOST_SUFFIXES = (
    "google.com",
    "google.com.br",
    "gstatic.com",
    "googleusercontent.com",
    "googleapis.com",
)


class GoogleAIModeError(RuntimeError):
    pass


class SessionRecoveryError(GoogleAIModeError):
    def __init__(self, message: str, attempt: "CountingSession") -> None:
        super().__init__(message)
        self.attempt = attempt


@dataclass(frozen=True)
class SourceLink:
    title: str
    url: str
    domain: str


@dataclass(frozen=True)
class QueryResult:
    answer: str
    http_requests: int
    ai_queries: int
    sources: list[SourceLink]
    image: str | None = None


class CountingSession(requests.Session):
    def __init__(self) -> None:
        super().__init__()
        self.http_requests = 0
        self.request_log: list[dict[str, Any]] = []
        self.sticky_cookie_snapshot: list[dict[str, Any]] = []

    def capture_sticky_cookies(self) -> None:
        self.sticky_cookie_snapshot = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
                "expires": cookie.expires,
            }
            for cookie in self.cookies
            if cookie.name in STICKY_COOKIE_NAMES
        ]

    def restore_sticky_cookies(self) -> None:
        if not self.sticky_cookie_snapshot:
            return
        for cookie in list(self.cookies):
            if cookie.name not in STICKY_COOKIE_NAMES:
                continue
            try:
                self.cookies.clear(
                    domain=cookie.domain,
                    path=cookie.path,
                    name=cookie.name,
                )
            except KeyError:
                pass
        for cookie in self.sticky_cookie_snapshot:
            self.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie["domain"],
                path=cookie["path"],
                secure=cookie["secure"],
                expires=cookie["expires"],
            )

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        self.http_requests += 1
        response = super().send(request, **kwargs)
        self.restore_sticky_cookies()
        parsed = urlsplit(request.url or "")
        self.request_log.append(
            {
                "method": request.method,
                "host": parsed.netloc,
                "path": parsed.path,
                "status": response.status_code,
            }
        )
        return response


def _read_session_payload() -> dict[str, Any]:
    errors: list[str] = []
    for path in (COOKIE_FILE, COOKIE_BACKUP_FILE):
        if not path.exists():
            errors.append(f"{path.name}: ausente")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}")
            continue
        user_agent = str(data.get("user_agent", "")).strip()
        cookies = data.get("cookies", [])
        if user_agent and isinstance(cookies, list) and cookies:
            return data
        errors.append(f"{path.name}: sessão vazia ou inválida")
    raise GoogleAIModeError(
        "Nenhuma sessão anônima válida foi encontrada. " + "; ".join(errors)
    )


def load_session() -> tuple[CountingSession, str]:
    data = _read_session_payload()
    user_agent = str(data["user_agent"]).strip()
    session = CountingSession()
    for cookie in data.get("cookies", []):
        name = str(cookie.get("name", ""))
        if not name or name not in ANONYMOUS_COOKIE_NAMES:
            continue
        if name.startswith(ACCOUNT_COOKIE_PREFIXES):
            continue
        session.cookies.set(
            name,
            str(cookie.get("value", "")),
            domain=str(cookie.get("domain") or ".google.com"),
            path=str(cookie.get("path") or "/"),
        )
    if not session.cookies:
        raise GoogleAIModeError("O arquivo de sessão não contém cookies anônimos utilizáveis.")
    session.capture_sticky_cookies()
    if not session.sticky_cookie_snapshot:
        raise GoogleAIModeError(
            "A sessão não contém os cookies NID/AEC necessários ao Modo IA."
        )
    return session, user_agent


def _session_from_cookie_jar(cookie_jar: Any) -> CountingSession:
    session = CountingSession()
    for cookie in cookie_jar:
        if "google" not in cookie.domain:
            continue
        if cookie.name not in ANONYMOUS_COOKIE_NAMES:
            continue
        if cookie.name.startswith(ACCOUNT_COOKIE_PREFIXES):
            continue
        session.cookies.set(
            cookie.name,
            cookie.value,
            domain=cookie.domain,
            path=cookie.path or "/",
            secure=bool(cookie.secure),
            expires=cookie.expires,
        )
    session.capture_sticky_cookies()
    if not session.sticky_cookie_snapshot:
        raise GoogleAIModeError(
            "A origem de recuperação não contém NID/AEC anônimos utilizáveis."
        )
    return session


def _load_firefox_cookie_session(cookie_file: Path) -> CountingSession:
    if not cookie_file.exists():
        raise GoogleAIModeError(f"Banco de cookies ausente: {cookie_file}")
    try:
        import browser_cookie3
    except ImportError as exc:
        raise GoogleAIModeError(
            "A recuperação da sessão requer o pacote browser-cookie3."
        ) from exc

    try:
        cookie_jar = browser_cookie3.firefox(
            cookie_file=str(cookie_file),
            domain_name="google.com",
        )
    except Exception as exc:
        raise GoogleAIModeError(
            f"Não foi possível carregar o banco anônimo: {type(exc).__name__}"
        ) from exc
    return _session_from_cookie_jar(cookie_jar)


def load_firefox_seed_session(user_agent: str) -> CountingSession:
    del user_agent  # Mantido no contrato para compatibilidade.
    if not FIREFOX_SEED_COOKIE_FILE.exists():
        raise GoogleAIModeError(
            f"Banco anônimo de recuperação ausente: {FIREFOX_SEED_COOKIE_FILE}"
        )
    return _load_firefox_cookie_session(FIREFOX_SEED_COOKIE_FILE)


def _recovery_log(message: str) -> None:
    if os.environ.get("GOOGLE_AI_RECOVERY_VERBOSE", "").strip() in {"1", "true", "yes"}:
        print(f"[recuperação] {message}", file=sys.stderr, flush=True)


def _write_recovery_state(
    method: str,
    status: str,
    duration_seconds: float,
    profile_removed: bool | None = None,
) -> None:
    payload: dict[str, Any] = {
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": method,
        "status": status,
        "duration_seconds": round(duration_seconds, 3),
    }
    if profile_removed is not None:
        payload["temporary_profile_removed"] = profile_removed
    temporary = SESSION_RECOVERY_STATE.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(SESSION_RECOVERY_STATE)


def _has_required_mode_tokens(html_text: str, image_required: bool) -> bool:
    soup = BeautifulSoup(html_text, "html.parser")
    if image_required:
        return bool(
            soup.select_one("[data-xsrf-folif-token]")
            and soup.select_one("[data-stkp]")
            and soup.select("[data-elrc]")
        )
    return bool(
        soup.select_one("[data-xsrf-folwr-token]")
        and soup.select_one("[data-stkp]")
        and soup.find(id="aim-chrome-initial-inline-async-container")
    )


def _recovery_search_params(image_required: bool) -> dict[str, str]:
    return {
        "udm": "50",
        "q": (
            "Prepare-se para analisar uma imagem."
            if image_required
            else "Qual é a capital da Colômbia?"
        ),
        "hl": "pt-BR",
        "gl": "br",
        "pws": "0",
    }


def _validate_mode_session(
    session: CountingSession,
    user_agent: str,
    image_required: bool,
    timeout: float,
) -> bool:
    html_text, _ = fetch_initial_html(
        session,
        user_agent,
        _recovery_search_params(image_required),
        min(timeout, 35.0),
        allow_cookie_reset=False,
    )
    if not _has_required_mode_tokens(html_text, image_required):
        return False
    session.capture_sticky_cookies()
    return bool(session.sticky_cookie_snapshot)


def try_http_only_recovery(
    user_agent: str,
    image_required: bool,
    timeout: float,
) -> CountingSession:
    """Tenta formar uma sessão nova sem navegador antes do último recurso."""
    started = time.monotonic()
    session = CountingSession()
    warmups = (
        {"q": "Google", "hl": "pt-BR", "gl": "br", "pws": "0"},
        _recovery_search_params(image_required),
        {
            **_recovery_search_params(image_required),
            "safe": "off",
            "filter": "0",
        },
    )
    last_error: Exception | None = None
    for index, params in enumerate(warmups, start=1):
        try:
            html_text, _ = fetch_initial_html(
                session,
                user_agent,
                params,
                min(timeout, 30.0),
                allow_cookie_reset=False,
            )
            if _has_required_mode_tokens(html_text, image_required):
                session.capture_sticky_cookies()
                if session.sticky_cookie_snapshot:
                    save_anonymous_session(session, user_agent)
                    _write_recovery_state(
                        "http_only",
                        "success",
                        time.monotonic() - started,
                    )
                    _recovery_log(f"sessão renovada somente por HTTP no passo {index}")
                    return session
        except (requests.RequestException, GoogleAIModeError, OSError) as exc:
            last_error = exc
        time.sleep(0.25)
    _write_recovery_state(
        "http_only",
        "unavailable",
        time.monotonic() - started,
    )
    raise SessionRecoveryError(
        "A recuperação somente por HTTP não obteve tokens válidos do Modo IA."
        + (f" Último erro: {last_error}" if last_error else ""),
        session,
    )


def _find_firefox_executable() -> Path:
    candidates = [
        shutil.which("firefox.exe"),
        shutil.which("firefox"),
        str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Mozilla Firefox" / "firefox.exe"),
        str(Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Mozilla Firefox" / "firefox.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise GoogleAIModeError(
        "Firefox não foi encontrado para a recuperação automática de último recurso."
    )


def _find_chrome_executable() -> Path:
    candidates = [
        os.environ.get("GOOGLE_CHROME_BIN"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chrome.exe"),
        str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise GoogleAIModeError("Chrome não foi encontrado para renovar a sessão do Modo IA.")


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _cdp_command(ws: websocket.WebSocket, request_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        message = json.loads(ws.recv())
        if message.get("id") == request_id:
            if message.get("error"):
                raise GoogleAIModeError(f"CDP falhou em {method}: {message['error'].get('message', 'erro')}")
            return message.get("result") or {}
    raise GoogleAIModeError(f"CDP não respondeu a {method}.")


def _session_from_chrome_cookies(cookies: list[dict[str, Any]]) -> CountingSession:
    session = CountingSession()
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "")
        if "google" not in domain or name not in ANONYMOUS_COOKIE_NAMES:
            continue
        if name.startswith(ACCOUNT_COOKIE_PREFIXES):
            continue
        expires = cookie.get("expires")
        session.cookies.set(
            name,
            str(cookie.get("value") or ""),
            domain=domain,
            path=str(cookie.get("path") or "/"),
            secure=bool(cookie.get("secure")),
            expires=int(expires) if isinstance(expires, (int, float)) and expires > 0 else None,
        )
    session.capture_sticky_cookies()
    if not session.sticky_cookie_snapshot:
        raise GoogleAIModeError("Chrome abriu o Modo IA sem cookies NID/AEC utilizáveis.")
    return session


def recover_session_with_chrome(
    user_agent: str,
    image_required: bool,
    timeout: float,
) -> CountingSession:
    """Renova a sessão anônima via Chrome/CDP sem persistir o perfil do navegador."""
    started = time.monotonic()
    chrome = _find_chrome_executable()
    with _session_recovery_lock():
        try:
            concurrent_session, _ = load_session()
            if _validate_mode_session(concurrent_session, user_agent, image_required, timeout):
                return concurrent_session
        except (requests.RequestException, GoogleAIModeError, OSError):
            pass

        profile = Path(tempfile.mkdtemp(prefix="google_ai_chrome_recovery_"))
        process: subprocess.Popen[Any] | None = None
        removed = False
        try:
            port = _available_loopback_port()
            url = f"{SEARCH_URL}?{urlencode(_recovery_search_params(image_required))}"
            command = [
                str(chrome),
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--disable-default-apps",
                "--remote-allow-origins=*",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "--window-size=1365,900",
                f"--user-agent={user_agent}",
            ]
            proxy = urlsplit(os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "")
            if proxy.hostname and proxy.port:
                command.append(f"--proxy-server={proxy.scheme or 'http'}://{proxy.hostname}:{proxy.port}")
            command.append(url)
            if sys.platform != "win32" and not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
                command = ["xvfb-run", "-a", *command]

            _recovery_log("iniciando Chrome isolado para renovar a sessão")
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=os.environ.copy(),
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
            )

            page: dict[str, Any] | None = None
            deadline = time.monotonic() + min(max(timeout, 15.0), 45.0)
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise GoogleAIModeError(f"Chrome encerrou durante a renovação (code={process.returncode}).")
                try:
                    pages = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=1).json()
                    page = next((item for item in pages if item.get("type") == "page"), None)
                    if page:
                        break
                except (requests.RequestException, ValueError):
                    pass
                time.sleep(0.25)
            if not page:
                raise GoogleAIModeError("Chrome não abriu a página do Modo IA dentro do prazo.")

            ws = websocket.create_connection(str(page["webSocketDebuggerUrl"]), timeout=12)
            try:
                session: CountingSession | None = None
                for wait_seconds in (3.0, 6.0, 12.0):
                    time.sleep(wait_seconds)
                    state_result = _cdp_command(
                        ws,
                        100 + int(wait_seconds),
                        "Runtime.evaluate",
                        {
                            "expression": (
                                "JSON.stringify({folif:!!document.querySelector('[data-xsrf-folif-token]'),"
                                "folwr:!!document.querySelector('[data-xsrf-folwr-token]')})"
                            ),
                            "returnByValue": True,
                        },
                    )
                    state_raw = state_result.get("result", {}).get("value") or "{}"
                    state = json.loads(state_raw)
                    required_ready = bool(state.get("folif") if image_required else state.get("folwr"))
                    if not required_ready:
                        continue
                    cookie_result = _cdp_command(ws, 200 + int(wait_seconds), "Network.getAllCookies")
                    session = _session_from_chrome_cookies(cookie_result.get("cookies") or [])
                    if _validate_mode_session(session, user_agent, image_required, timeout):
                        break
                    session = None
                if session is None:
                    raise GoogleAIModeError("Chrome abriu o Modo IA, mas não formou uma sessão HTTP válida.")
            finally:
                ws.close()

            save_anonymous_session(session, user_agent)
            _write_recovery_state("chrome_cdp", "success", time.monotonic() - started, profile_removed=None)
            _recovery_log("sessão renovada pelo Chrome/CDP")
            return session
        finally:
            if process is not None:
                _stop_firefox_profile(process, profile)
            shutil.rmtree(profile, ignore_errors=True)
            removed = not profile.exists()
            if process is not None and process.returncode not in (None, 0, -15):
                _write_recovery_state("chrome_cdp", "failed", time.monotonic() - started, profile_removed=removed)


def recover_session_with_browser(
    user_agent: str,
    image_required: bool,
    timeout: float,
) -> CountingSession:
    try:
        chrome_attempts = max(1, min(3, int(os.environ.get("GOOGLE_AI_CHROME_RECOVERY_ATTEMPTS", "1"))))
    except ValueError:
        chrome_attempts = 1
    chrome_error: BaseException | None = None
    for attempt in range(1, chrome_attempts + 1):
        try:
            return recover_session_with_chrome(user_agent, image_required, timeout)
        except (requests.RequestException, GoogleAIModeError, OSError, subprocess.SubprocessError) as exc:
            chrome_error = exc
            _recovery_log(f"Chrome não renovou a sessão ({attempt}/{chrome_attempts}): {exc}")
            if attempt < chrome_attempts:
                time.sleep(float(attempt * 2))

    firefox_enabled = os.environ.get("GOOGLE_AI_FIREFOX_FALLBACK", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if not firefox_enabled:
        raise GoogleAIModeError(
            f"Chrome não renovou a sessão após {chrome_attempts} tentativa(s): {chrome_error}"
        ) from chrome_error

    try:
        return recover_session_with_headless_firefox(user_agent, image_required, timeout)
    except (requests.RequestException, GoogleAIModeError, OSError, subprocess.SubprocessError) as firefox_error:
        raise GoogleAIModeError(
            f"Chrome e Firefox não renovaram a sessão. Chrome: {chrome_error}; Firefox: {firefox_error}"
        ) from firefox_error


def _write_firefox_recovery_preferences(profile: Path) -> None:
    preferences = """
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("datareporting.healthreport.uploadEnabled", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.enabled", false);
user_pref("toolkit.telemetry.unified", false);
user_pref("app.update.auto", false);
user_pref("app.update.enabled", false);
""".strip()
    proxy_url = urlsplit(
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or ""
    )
    if proxy_url.hostname and proxy_url.port:
        preferences += f'''
user_pref("network.proxy.type", 1);
user_pref("network.proxy.http", "{proxy_url.hostname}");
user_pref("network.proxy.http_port", {proxy_url.port});
user_pref("network.proxy.ssl", "{proxy_url.hostname}");
user_pref("network.proxy.ssl_port", {proxy_url.port});
user_pref("network.proxy.no_proxies_on", "localhost, 127.0.0.1");
'''.rstrip()
    (profile / "user.js").write_text(preferences + "\n", encoding="utf-8")


def _copy_cookie_database(source_dir: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    copied: set[str] = set()
    for name in ("cookies.sqlite", "cookies.sqlite-wal", "cookies.sqlite-shm"):
        source = source_dir / name
        target = target_dir / name
        if not source.is_file():
            continue
        temporary = target.with_name(target.name + ".new")
        for attempt in range(4):
            try:
                shutil.copy2(source, temporary)
                temporary.replace(target)
                copied.add(name)
                break
            except OSError:
                if attempt >= 3:
                    raise
                time.sleep(0.35 * (attempt + 1))
    if "cookies.sqlite" not in copied:
        raise GoogleAIModeError(
            f"O perfil temporário não produziu banco de cookies em {source_dir}."
        )
    for sidecar in ("cookies.sqlite-wal", "cookies.sqlite-shm"):
        if sidecar not in copied:
            try:
                (target_dir / sidecar).unlink()
            except FileNotFoundError:
                pass


def _seed_temporary_firefox_profile(profile: Path) -> None:
    if FIREFOX_SEED_COOKIE_FILE.is_file():
        try:
            _copy_cookie_database(FIREFOX_SEED_DIR, profile)
        except (OSError, GoogleAIModeError):
            # Um seed corrompido não deve impedir uma tentativa realmente limpa.
            for name in ("cookies.sqlite", "cookies.sqlite-wal", "cookies.sqlite-shm"):
                try:
                    (profile / name).unlink()
                except FileNotFoundError:
                    pass


def _stop_firefox_profile(process: subprocess.Popen[Any], profile: Path) -> None:
    try:
        process.terminate()
        process.wait(timeout=2.0)
    except Exception:
        pass
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
        environment = os.environ.copy()
        environment["GOOGLE_AI_TEMP_PROFILE"] = str(profile)
        script = (
            "$needle=$env:GOOGLE_AI_TEMP_PROFILE; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($needle) } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    time.sleep(0.5)


def _wait_for_firefox_cookies(profile: Path, maximum_seconds: float) -> None:
    deadline = time.monotonic() + maximum_seconds
    first_ready: float | None = None
    cookie_file = profile / "cookies.sqlite"
    while time.monotonic() < deadline:
        if cookie_file.is_file() and cookie_file.stat().st_size > 0:
            try:
                _load_firefox_cookie_session(cookie_file)
                if first_ready is None:
                    first_ready = time.monotonic()
                elif time.monotonic() - first_ready >= 1.25:
                    return
            except (GoogleAIModeError, OSError):
                first_ready = None
        time.sleep(0.35)
    raise GoogleAIModeError(
        f"Firefox headless nao produziu cookies validos em {maximum_seconds:.0f}s."
    )


@contextmanager
def _session_recovery_lock(timeout: float = 45.0):
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(
                SESSION_RECOVERY_LOCK,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(
                descriptor,
                f"pid={os.getpid()} utc={time.time()}".encode("ascii", errors="ignore"),
            )
        except FileExistsError:
            try:
                age = time.time() - SESSION_RECOVERY_LOCK.stat().st_mtime
                if age > 120:
                    SESSION_RECOVERY_LOCK.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise GoogleAIModeError(
                    "Outra recuperação de sessão permaneceu bloqueada por tempo excessivo."
                )
            time.sleep(0.3)
    try:
        yield
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            SESSION_RECOVERY_LOCK.unlink()
        except FileNotFoundError:
            pass


def recover_session_with_headless_firefox(
    user_agent: str,
    image_required: bool,
    timeout: float,
) -> CountingSession:
    """Último recurso: Firefox headless, isolado, sem Selenium e sem janela."""
    started = time.monotonic()
    firefox = _find_firefox_executable()
    with _session_recovery_lock():
        # Se outro processo renovou enquanto aguardávamos o lock, não abra navegador.
        for loader in (
            lambda: load_session()[0],
            lambda: load_firefox_seed_session(user_agent),
        ):
            try:
                concurrent_session = loader()
                if _validate_mode_session(
                    concurrent_session,
                    user_agent,
                    image_required,
                    timeout,
                ):
                    save_anonymous_session(concurrent_session, user_agent)
                    return concurrent_session
            except (requests.RequestException, GoogleAIModeError, OSError):
                pass

        last_error: Exception | None = None
        for attempt_index, wait_seconds in enumerate(
            RECOVERY_BROWSER_WAIT_SECONDS,
            start=1,
        ):
            profile = Path(
                tempfile.mkdtemp(prefix="google_ai_firefox_recovery_")
            )
            removed = False
            process: subprocess.Popen[Any] | None = None
            try:
                if attempt_index == 1:
                    _seed_temporary_firefox_profile(profile)
                _write_firefox_recovery_preferences(profile)
                url = f"{SEARCH_URL}?{urlencode(_recovery_search_params(image_required))}"
                environment = os.environ.copy()
                environment["MOZ_HEADLESS"] = "1"
                command = [
                    str(firefox),
                    "-headless",
                    "-no-remote",
                    "-new-instance",
                    "-profile",
                    str(profile),
                    url,
                ]
                _recovery_log(
                    f"iniciando Firefox headless isolado, tentativa {attempt_index}"
                )
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        if sys.platform == "win32"
                        else 0
                    ),
                )
                _wait_for_firefox_cookies(profile, wait_seconds)
                _stop_firefox_profile(process, profile)
                process = None

                session = _load_firefox_cookie_session(profile / "cookies.sqlite")
                if not _validate_mode_session(
                    session,
                    user_agent,
                    image_required,
                    timeout,
                ):
                    raise GoogleAIModeError(
                        "O Firefox headless abriu o Modo IA, mas a sessão não recebeu tokens válidos."
                    )
                _copy_cookie_database(profile, FIREFOX_SEED_DIR)
                save_anonymous_session(session, user_agent)
                shutil.rmtree(profile, ignore_errors=True)
                removed = not profile.exists()
                _write_recovery_state(
                    "headless_firefox",
                    "success",
                    time.monotonic() - started,
                    profile_removed=removed,
                )
                _recovery_log("sessão renovada e perfil temporário removido")
                return session
            except (
                requests.RequestException,
                GoogleAIModeError,
                OSError,
                subprocess.SubprocessError,
            ) as exc:
                last_error = exc
            finally:
                if process is not None:
                    _stop_firefox_profile(process, profile)
                shutil.rmtree(profile, ignore_errors=True)
                removed = not profile.exists()

        _write_recovery_state(
            "headless_firefox",
            "failed",
            time.monotonic() - started,
            profile_removed=removed,
        )
        raise GoogleAIModeError(
            "A recuperação automática pelo Firefox headless falhou."
            + (f" Último erro: {last_error}" if last_error else "")
        )


def _merge_session_metrics(
    previous: CountingSession,
    replacement: CountingSession,
) -> CountingSession:
    replacement.http_requests += previous.http_requests
    replacement.request_log = previous.request_log + replacement.request_log
    return replacement

def save_anonymous_session(session: requests.Session, user_agent: str) -> None:
    cookies: list[dict[str, Any]] = []
    for cookie in session.cookies:
        if "google" not in cookie.domain:
            continue
        if cookie.name not in ANONYMOUS_COOKIE_NAMES:
            continue
        if cookie.name.startswith(ACCOUNT_COOKIE_PREFIXES):
            continue
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
            }
        )
    if not cookies:
        raise GoogleAIModeError("A consulta terminou sem cookies anônimos válidos para salvar.")

    payload = {
        "user_agent": user_agent,
        "cookies": cookies,
        "last_success_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    for path in (COOKIE_FILE, COOKIE_BACKUP_FILE):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(path)


def initial_headers(user_agent: str) -> dict[str, str]:
    major_match = re.search(r"Chrome/(\d+)", user_agent)
    major = major_match.group(1) if major_match else "150"
    return {
        "User-Agent": user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-CH-UA": (
            f'"Chromium";v="{major}", "Google Chrome";v="{major}", '
            '"Not_A Brand";v="99"'
        ),
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def async_headers(user_agent: str, initial: dict[str, str], referer: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": initial["Accept-Language"],
        "Referer": referer,
        "Sec-CH-UA": initial["Sec-CH-UA"],
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "XMLHttpRequest",
    }


def _write_netscape_cookie_jar(
    path: Path,
    session: requests.Session,
    include_existing: bool,
) -> None:
    lines = ["# Netscape HTTP Cookie File", ""]
    if include_existing:
        for cookie in session.cookies:
            if "google" not in cookie.domain:
                continue
            if cookie.name not in ANONYMOUS_COOKIE_NAMES:
                continue
            if cookie.name.startswith(ACCOUNT_COOKIE_PREFIXES):
                continue
            domain = cookie.domain or ".google.com"
            include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
            secure = "TRUE" if cookie.secure else "FALSE"
            expires = int(cookie.expires or 0)
            lines.append(
                "\t".join(
                    (
                        domain,
                        include_subdomains,
                        cookie.path or "/",
                        secure,
                        str(expires),
                        cookie.name,
                        cookie.value,
                    )
                )
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_netscape_cookie_jar(path: Path, session: requests.Session) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        domain, _, cookie_path, _, _, name, value = fields
        if "google" not in domain:
            continue
        if name not in ANONYMOUS_COOKIE_NAMES:
            continue
        if name.startswith(ACCOUNT_COOKIE_PREFIXES):
            continue
        session.cookies.set(
            name,
            value,
            domain=domain,
            path=cookie_path or "/",
        )
    if isinstance(session, CountingSession):
        session.restore_sticky_cookies()


def fetch_initial_html(
    session: CountingSession,
    user_agent: str,
    search_params: dict[str, str],
    timeout: float,
    allow_cookie_reset: bool = True,
) -> tuple[str, str]:
    curl_path = shutil.which("curl.exe") or shutil.which("curl")
    url = f"{SEARCH_URL}?{urlencode(search_params)}"
    if not curl_path:
        response = session.get(
            SEARCH_URL,
            params=search_params,
            headers=initial_headers(user_agent),
            timeout=timeout,
        )
        response.raise_for_status()
        return response.text, response.url

    headers = initial_headers(user_agent)
    last_html = ""
    last_error = ""
    include_modes = (True, False) if allow_cookie_reset else (True,)
    for include_existing in include_modes:
        with tempfile.TemporaryDirectory(prefix="google_ia_http_") as temp_dir:
            temp = Path(temp_dir)
            cookie_jar = temp / "cookies.txt"
            html_file = temp / "initial.html"
            _write_netscape_cookie_jar(cookie_jar, session, include_existing)
            command = [
                curl_path,
                "-sS",
                "-L",
                "--compressed",
                "--max-time",
                str(max(1, int(timeout))),
                "-A",
                user_agent,
                "-H",
                f"Accept-Language: {headers['Accept-Language']}",
                "-H",
                f"sec-ch-ua: {headers['Sec-CH-UA']}",
                "-H",
                "sec-ch-ua-mobile: ?0",
                "-H",
                'sec-ch-ua-platform: "Windows"',
                "-b",
                str(cookie_jar),
                "-c",
                str(cookie_jar),
                "-o",
                str(html_file),
                "-w",
                "%{num_redirects}",
                url,
            ]
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout + 15,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if sys.platform == "win32"
                    else 0
                ),
                check=False,
            )
            if completed.returncode != 0:
                last_error = completed.stderr.strip() or (
                    f"curl terminou com código {completed.returncode}"
                )
                continue
            last_html = html_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
            _load_netscape_cookie_jar(cookie_jar, session)
            try:
                redirects = int(completed.stdout.strip() or "0")
            except ValueError:
                redirects = 0
            session.http_requests += 1 + max(0, redirects)
            session.request_log.append(
                {
                    "method": "GET",
                    "host": urlsplit(url).netloc,
                    "path": urlsplit(url).path,
                    "status": 200,
                    "transport": "curl-schannel",
                }
            )
            soup = BeautifulSoup(last_html, "html.parser")
            if soup.select_one("[data-xsrf-folwr-token]"):
                return last_html, url
            if soup.select_one("[data-xsrf-folif-token]"):
                return last_html, url

    if last_html:
        return last_html, url
    raise GoogleAIModeError(
        f"Não foi possível abrir o Modo IA pelo bootstrap HTTP: {last_error}"
    )


def required_attr(tag: Tag | None, name: str) -> str:
    if tag is None:
        raise GoogleAIModeError(f"Elemento necessário não encontrado: {name}")
    value = tag.get(name)
    if not value:
        raise GoogleAIModeError(f"Token necessário não encontrado: {name}")
    return html.unescape(str(value))


def check_mode_available(soup: BeautifulSoup) -> None:
    body_text = soup.get_text(" ", strip=True).lower()
    if "não está disponível" in body_text:
        raise GoogleAIModeError(
            "O Google informou que o Modo IA não está disponível para esta sessão."
        )


def build_text_request(html_text: str, question: str) -> dict[str, str]:
    soup = BeautifulSoup(html_text, "html.parser")
    token_tag = soup.find(
        attrs={
            "data-ei": True,
            "data-garc": True,
            "data-lro-signature": True,
            "data-lro-token": True,
            "data-srtst": True,
            "data-xsrf-folwr-token": True,
        }
    )
    state_tag = soup.find(attrs={"data-stkp": True})
    container = soup.find(id="aim-chrome-initial-inline-async-container")

    if token_tag is None or state_tag is None or container is None:
        check_mode_available(soup)
        raise GoogleAIModeError(
            "A sessão anônima foi recusada ou expirou; os tokens do Modo IA não vieram no HTML."
        )

    ved = required_attr(container, "data-ved")
    xsrf = required_attr(token_tag, "data-xsrf-folwr-token")
    return {
        "srtst": required_attr(token_tag, "data-srtst"),
        "garc": required_attr(token_tag, "data-garc"),
        "mlro": required_attr(token_tag, "data-lro-token"),
        "mlros": required_attr(token_tag, "data-lro-signature"),
        "ei": required_attr(token_tag, "data-ei"),
        "q": question,
        "yv": "3",
        "vet": f"1{ved}..i",
        "ved": ved,
        "udm": "50",
        "stkp": required_attr(state_tag, "data-stkp"),
        "cs": "1",
        "async": f"_fmt:adl,_xsrf:{xsrf}",
    }


def build_image_request(
    html_text: str,
    question: str,
    lens_params: dict[str, str],
) -> dict[str, str]:
    soup = BeautifulSoup(html_text, "html.parser")
    token_tag = soup.find(
        attrs={
            "data-ei": True,
            "data-srtst": True,
            "data-xsrf-folif-token": True,
        }
    )
    state_tag = soup.find(attrs={"data-stkp": True})
    elrc_tags = soup.select("[data-elrc]")

    if token_tag is None or state_tag is None or not elrc_tags:
        check_mode_available(soup)
        raise GoogleAIModeError(
            "A página inicial não trouxe os tokens necessários para analisar imagens."
        )

    vsrid = lens_params.get("vsrid")
    gsessionid = lens_params.get("gsessionid")
    if not vsrid or not gsessionid:
        raise GoogleAIModeError(
            "O upload da imagem terminou sem os identificadores do Google Lens."
        )

    return {
        "srtst": required_attr(token_tag, "data-srtst"),
        "ei": required_attr(token_tag, "data-ei"),
        "yv": "3",
        "lns_mode": "cvst",
        "udm": "50",
        "stkp": required_attr(state_tag, "data-stkp"),
        "cs": "1",
        "csuir": "0",
        "elrc": required_attr(elrc_tags[-1], "data-elrc"),
        "csui": "3",
        "q": question,
        "vsrid": vsrid,
        "gsessionid": gsessionid,
        "vit": "img",
        "async": (
            "_fmt:adl,_xsrf:"
            + required_attr(token_tag, "data-xsrf-folif-token")
        ),
    }


def clean_fragment_text(element: Tag) -> str:
    clone = BeautifulSoup(str(element), "html.parser")
    for badge in clone.select(
        ".WBgIic, .S9OuHf, .a14YJe, .DHPVt, [data-sfc-c='source']"
    ):
        badge.decompose()
    text = clone.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip()


def extract_answer(response_html: str) -> str:
    soup = BeautifulSoup(response_html, "html.parser")
    blocks = soup.select(
        "div.n6owBd.awi2gc, div.otQkpb, ul.KsbFXc.U6u95"
    )
    output: list[str] = []
    seen: set[str] = set()

    for block in blocks:
        if block.name == "ul":
            items: list[str] = []
            for item in block.find_all("li", recursive=False):
                value = clean_fragment_text(item)
                if value and value not in seen:
                    seen.add(value)
                    items.append(f"- {value}")
            if items:
                output.append("\n".join(items))
            continue

        value = clean_fragment_text(block)
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)

    answer = "\n\n".join(output).strip()
    if answer:
        return answer

    plain = soup.get_text("\n", strip=True)
    start_markers = (
        "A resposta do Modo IA está pronta\n",
        "Copiar\nEditar\n",
    )
    for marker in start_markers:
        pos = plain.find(marker)
        if pos >= 0:
            plain = plain[pos + len(marker):]

    cut_markers = (
        "\nCopiar\n",
        "\nCompartilhar link público\n",
        "\nBoa resposta\n",
    )
    end = len(plain)
    for marker in cut_markers:
        pos = plain.find(marker)
        if pos >= 0:
            end = min(end, pos)
    fallback = plain[:end].strip()
    if fallback:
        return fallback

    raise GoogleAIModeError("A resposta chegou, mas não foi possível extrair o texto.")


def normalize_source_url(href: str) -> str | None:
    href = html.unescape(href.strip())
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = "https://www.google.com" + href

    parsed = urlsplit(href)
    if parsed.scheme not in {"http", "https"}:
        return None

    if parsed.netloc.endswith(("google.com", "google.com.br")) and parsed.path == "/url":
        query = parse_qs(parsed.query)
        href = (query.get("q") or query.get("url") or [""])[0]
        parsed = urlsplit(href)

    host = parsed.netloc.lower().split(":", 1)[0]
    if not host or any(host == suffix or host.endswith("." + suffix) for suffix in INTERNAL_HOST_SUFFIXES):
        return None

    return href


def extract_sources(response_html: str) -> list[SourceLink]:
    soup = BeautifulSoup(response_html, "html.parser")
    sources: dict[str, SourceLink] = {}
    generic_titles = {
        "abrir",
        "copiar",
        "mostrar tudo",
        "saiba mais",
        "visitar",
    }

    for anchor in soup.find_all("a", href=True):
        url = normalize_source_url(str(anchor.get("href", "")))
        if not url or url in sources:
            continue
        parsed = urlsplit(url)
        title = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        if not title or title.lower() in generic_titles:
            title = parsed.netloc
        sources[url] = SourceLink(
            title=title[:180],
            url=url,
            domain=parsed.netloc.lower(),
        )

    return list(sources.values())


def upload_image(
    session: CountingSession,
    user_agent: str,
    image_path: Path,
    timeout: float,
) -> dict[str, str]:
    if not image_path.is_file():
        raise GoogleAIModeError(f"Imagem não encontrada: {image_path}")
    if image_path.stat().st_size == 0:
        raise GoogleAIModeError("A imagem está vazia.")

    mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    headers = {
        "User-Agent": user_agent,
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    with image_path.open("rb") as image_file:
        response = session.post(
            LENS_UPLOAD_URL,
            params={"stcs": str(int(time.time() * 1000))},
            headers=headers,
            files={
                "encoded_image": (
                    image_path.name,
                    image_file,
                    mime_type,
                )
            },
            timeout=timeout,
            allow_redirects=True,
        )
    response.raise_for_status()
    return dict(parse_qsl(urlsplit(response.url).query, keep_blank_values=True))


def query_google_ai(
    question: str,
    timeout: float = 60.0,
    image_path: str | Path | None = None,
    attempts: int = 3,
    allow_browser_recovery: bool = True,
) -> QueryResult:
    if attempts < 1:
        raise ValueError("attempts deve ser pelo menos 1")

    user_agent = DEFAULT_USER_AGENT
    used_recovery_sources: set[str] = set()
    try:
        session, user_agent = load_session()
        session_source = "json"
    except GoogleAIModeError:
        try:
            session = load_firefox_seed_session(user_agent)
            session_source = "seed"
            used_recovery_sources.add("seed")
        except GoogleAIModeError:
            try:
                session = try_http_only_recovery(
                    user_agent,
                    image_path is not None,
                    timeout,
                )
                session_source = "http"
                used_recovery_sources.add("http")
            except SessionRecoveryError as recovery_exc:
                if not allow_browser_recovery:
                    raise
                session = recover_session_with_browser(
                    user_agent,
                    image_path is not None,
                    timeout,
                )
                session = _merge_session_metrics(recovery_exc.attempt, session)
                session_source = "browser"
                used_recovery_sources.add("browser")
            except GoogleAIModeError:
                if not allow_browser_recovery:
                    raise
                session = recover_session_with_browser(
                    user_agent,
                    image_path is not None,
                    timeout,
                )
                session_source = "browser"
                used_recovery_sources.add("browser")

    headers = initial_headers(user_agent)
    image = Path(image_path).expanduser().resolve() if image_path else None

    if image is not None:
        if not image.is_file():
            raise GoogleAIModeError(f"Imagem não encontrada: {image}")
        if image.stat().st_size == 0:
            raise GoogleAIModeError("A imagem está vazia.")

    ai_queries = 0
    last_error: Exception | None = None
    invalid_phrases = (
        "parece que não há uma resposta disponível",
        "tente pedir outra coisa",
        "there doesn't seem to be an answer available",
        "try asking something else",
        "something went wrong and an ai response wasn't generated",
        "something went wrong and the content wasn't generated",
    )

    failures = 0
    while failures < attempts:
        try:
            initial_question = (
                "Prepare-se para analisar uma imagem."
                if image is not None
                else question
            )
            search_params = {"udm": "50", "q": initial_question}
            initial_html, referer = fetch_initial_html(
                session,
                user_agent,
                search_params,
                timeout,
            )

            if image is None:
                async_params = build_text_request(initial_html, question)
                endpoint = TEXT_ASYNC_URL
            else:
                lens_params = upload_image(session, user_agent, image, timeout)
                async_params = build_image_request(initial_html, question, lens_params)
                endpoint = IMAGE_ASYNC_URL

            ai_queries += 1
            response = session.get(
                endpoint,
                params=async_params,
                headers=async_headers(user_agent, headers, referer),
                timeout=timeout,
            )
            response.raise_for_status()

            plain = BeautifulSoup(response.text, "html.parser").get_text(
                " ", strip=True
            ).lower()
            if any(phrase in plain for phrase in invalid_phrases):
                raise GoogleAIModeError(
                    "O Modo IA respondeu sem gerar conteúdo; a consulta será repetida."
                )

            answer = extract_answer(response.text)
            save_anonymous_session(session, user_agent)
            return QueryResult(
                answer=answer,
                http_requests=session.http_requests,
                ai_queries=ai_queries,
                sources=extract_sources(response.text),
                image=str(image) if image is not None else None,
            )
        except (requests.RequestException, GoogleAIModeError) as exc:
            last_error = exc
            error_text = str(exc).lower()
            token_failure = any(
                marker in error_text
                for marker in (
                    "tokens do modo ia",
                    "tokens necessários",
                    "sessão anônima foi recusada",
                    "modo ia não está disponível",
                    "não trouxe os tokens",
                )
            )

            if token_failure:
                replacement: CountingSession | None = None
                replacement_source = ""

                if "seed" not in used_recovery_sources and session_source != "seed":
                    used_recovery_sources.add("seed")
                    try:
                        replacement = load_firefox_seed_session(user_agent)
                        replacement_source = "seed"
                    except GoogleAIModeError:
                        replacement = None

                if replacement is None and "http" not in used_recovery_sources:
                    used_recovery_sources.add("http")
                    try:
                        replacement = try_http_only_recovery(
                            user_agent,
                            image is not None,
                            timeout,
                        )
                        replacement_source = "http"
                    except SessionRecoveryError as recovery_exc:
                        session.http_requests += recovery_exc.attempt.http_requests
                        session.request_log.extend(recovery_exc.attempt.request_log)
                        replacement = None
                    except GoogleAIModeError:
                        replacement = None

                if (
                    replacement is None
                    and allow_browser_recovery
                    and "browser" not in used_recovery_sources
                ):
                    used_recovery_sources.add("browser")
                    replacement = recover_session_with_browser(
                        user_agent,
                        image is not None,
                        timeout,
                    )
                    replacement_source = "browser"

                if replacement is not None:
                    session = _merge_session_metrics(session, replacement)
                    session_source = replacement_source
                    headers = initial_headers(user_agent)
                    continue

            failures += 1
            if failures >= attempts:
                raise
            time.sleep(min(2.0, 0.6 * failures))

    raise GoogleAIModeError(str(last_error or "Falha desconhecida no Modo IA."))

def ask_google_ai(question: str, timeout: float = 60.0) -> str:
    return query_google_ai(question, timeout=timeout).answer


def print_result(result: QueryResult, show_metrics: bool = True) -> None:
    print(result.answer)
    if not show_metrics:
        return

    print("\n---")
    print(f"Requisições HTTP: {result.http_requests}")
    print(f"Consultas ao Modo IA: {result.ai_queries}")
    print(f"Links/fontes retornados: {len(result.sources)}")
    for index, source in enumerate(result.sources, start=1):
        print(f"{index}. {source.title} — {source.url}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Consulta o Modo IA do Google com requests, inclusive com imagem."
    )
    parser.add_argument("pergunta", nargs="?", default=None)
    parser.add_argument("--imagem", type=Path)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--tentativas", type=int, default=3)
    parser.add_argument("--sem-metricas", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--sem-recuperacao-navegador",
        action="store_true",
        help="Não usar navegador se todas as sessões anônimas expirarem.",
    )
    args = parser.parse_args()

    question = args.pergunta
    if not question:
        question = (
            "O que há nesta imagem? Descreva de forma objetiva."
            if args.imagem
            else "Qual é a capital da Colômbia?"
        )

    try:
        result = query_google_ai(
            question,
            timeout=args.timeout,
            image_path=args.imagem,
            attempts=args.tentativas,
            allow_browser_recovery=not args.sem_recuperacao_navegador,
        )
    except (requests.RequestException, GoogleAIModeError, ValueError, OSError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    else:
        print_result(result, show_metrics=not args.sem_metricas)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
