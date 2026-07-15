import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12


DEFAULT_URL = "https://certificado.nfse.gov.br/EmissorNacional/Certificado"
DEFAULT_START_URL = "https://www.nfse.gov.br/EmissorNacional/Notas/Recebidas"
SESSION_FILE = Path(__file__).with_name("sessao_nfse.txt")


def list_certificates():
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    if os.name != "nt" or not Path(powershell).exists():
        return []

    ps = r"""
$ErrorActionPreference = 'Stop'
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store('My', 'CurrentUser')
$store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadOnly)
$certs = @($store.Certificates | Where-Object { $_.HasPrivateKey } | ForEach-Object {
    [PSCustomObject]@{
        thumbprint = $_.Thumbprint
        subject = $_.Subject
        not_after = $_.NotAfter.ToUniversalTime().ToString('o')
    }
})
$store.Close()
if ($certs.Count -eq 0) { "null" } else { $certs | ConvertTo-Json -Depth 3 -Compress }
"""
    proc = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps,
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip())
    raw = proc.stdout.strip()
    if not raw or raw == "null":
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        return [data]
    return data


def _cert_datetime(value):
    if hasattr(value, "not_valid_after_utc"):
        return value.not_valid_after_utc.isoformat().replace("+00:00", "Z")
    return value.not_valid_after.isoformat()


def load_pfx_identity(pfx_file: str | Path, password: str = "") -> dict:
    pfx_path = Path(pfx_file)
    raw = pfx_path.read_bytes()
    password_bytes = password.encode("utf-8") if password else None
    try:
        private_key, certificate, additional = pkcs12.load_key_and_certificates(raw, password_bytes)
    except Exception as exc:
        raise RuntimeError("Não foi possível abrir o PFX. Confira arquivo e senha.") from exc
    if private_key is None or certificate is None:
        raise RuntimeError("PFX inválido: certificado ou chave privada ausente.")
    return {
        "private_key": private_key,
        "certificate": certificate,
        "additional": additional or [],
        "subject": certificate.subject.rfc4514_string(),
        "issuer": certificate.issuer.rfc4514_string(),
        "thumbprint": certificate.fingerprint(hashes.SHA1()).hex().upper(),
        "not_after": _cert_datetime(certificate),
    }


def _write_temp_client_cert(identity: dict, folder: Path) -> tuple[Path, Path]:
    cert_path = folder / "client.crt.pem"
    key_path = folder / "client.key.pem"
    certs = [identity["certificate"], *identity.get("additional", [])]
    cert_path.write_bytes(b"".join(cert.public_bytes(serialization.Encoding.PEM) for cert in certs if isinstance(cert, x509.Certificate)))
    key_path.write_bytes(
        identity["private_key"].private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _session_cookies(session: requests.Session) -> list[dict]:
    cookies = []
    seen = set()
    for cookie in session.cookies:
        domain = cookie.domain or "www.nfse.gov.br"
        path = cookie.path or "/"
        key = (cookie.name, domain, path)
        if key in seen:
            continue
        seen.add(key)
        item = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": domain,
            "path": path,
            "secure": bool(cookie.secure),
            "httpOnly": any(str(k).lower() == "httponly" for k in (getattr(cookie, "_rest", {}) or {})),
            "expires": datetime.utcfromtimestamp(cookie.expires).isoformat() + "Z" if cookie.expires else None,
        }
        cookies.append(item)
    return cookies


def run_pfx_login(pfx_file: str | Path, password: str, url: str, start_url: str, proxy: str | None = None) -> dict:
    identity = load_pfx_identity(pfx_file, password)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/139.0 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    with tempfile.TemporaryDirectory(prefix="prumo_nfse_pfx_") as tmp:
        cert_path, key_path = _write_temp_client_cert(identity, Path(tmp))
        client_cert = (str(cert_path), str(key_path))
        session = requests.Session()
        session.headers.update(headers)
        response = session.get(url, cert=client_cert, proxies=proxies, timeout=90, allow_redirects=True)
        target = session.get(start_url, cert=client_cert, proxies=proxies, timeout=90, allow_redirects=True)

    target_text = target.text or ""
    result = {
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "start_url": start_url,
        "login_url": url,
        "status_code": int(response.status_code),
        "redirect_location": response.headers.get("Location"),
        "target_final_url": target.url,
        "target_looks_logged_in": "Acesso com Certificado Digital" not in target_text,
        "proxy": proxy or "",
        "certificate": {
            "subject": identity["subject"],
            "issuer": identity["issuer"],
            "thumbprint": identity["thumbprint"],
            "not_after": identity["not_after"],
            "source": "pfx",
        },
        "cookies": _session_cookies(session),
    }
    return result


def choose_certificate(certificates):
    if not certificates:
        print("Nenhum certificado com chave privada encontrado em Cert:\\CurrentUser\\My.", file=sys.stderr)
        raise SystemExit(1)

    print("\nCertificados disponiveis:\n")
    for i, cert in enumerate(certificates, start=1):
        subject = cert.get("subject", "N/A")
        not_after = cert.get("not_after", "N/A")
        thumbprint = cert.get("thumbprint", "N/A")
        print(f"  [{i}] {subject}")
        print(f"      Validade: {not_after}")
        print(f"      Thumbprint: {thumbprint}")
        print()

    while True:
        try:
            choice = input("Escolha o numero do certificado: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(certificates):
                return certificates[idx]["thumbprint"]
        except ValueError:
            pass
        print("Escolha invalida. Tente novamente.")


def run_powershell_login(thumbprint: str, url: str, start_url: str, proxy: str | None = None) -> dict:
    proxy_value = (proxy or "").strip()
    ps = rf"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$thumbprint = '{thumbprint}'
$url = '{url}'
$startUrl = '{start_url}'
$proxy = '{proxy_value}'
$proxyArgs = @{{}}
if ($proxy) {{
    $proxyArgs['Proxy'] = $proxy
}}
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store('My', 'CurrentUser')
$store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadOnly)
$cert = $store.Certificates | Where-Object {{ $_.Thumbprint -eq $thumbprint }} | Select-Object -First 1
if (-not $cert) {{
    $store.Close()
    throw "Certificado nao encontrado no CurrentUser\\My: $thumbprint"
}}
if (-not $cert.HasPrivateKey) {{
    $store.Close()
    throw "Certificado encontrado, mas sem chave privada: $thumbprint"
}}

$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
try {{
    $response = Invoke-WebRequest -Uri $url -Certificate $cert -WebSession $session -MaximumRedirection 5 -UseBasicParsing @proxyArgs
    $statusCode = [int]$response.StatusCode
    $location = $response.Headers.Location
}} catch {{
    if ($_.Exception.Response) {{
        $statusCode = [int]$_.Exception.Response.StatusCode
        $location = $_.Exception.Response.Headers.Location
    }} else {{
        throw
    }}
}}

$targetResponse = Invoke-WebRequest -Uri $startUrl -Certificate $cert -WebSession $session -MaximumRedirection 5 -UseBasicParsing @proxyArgs
$targetFinalUrl = $targetResponse.BaseResponse.ResponseUri.AbsoluteUri
$targetLooksLoggedIn = -not ($targetResponse.Content -like '*Acesso com Certificado Digital*')

$cookies = @()
$seen = @{{}}
foreach ($cookieUri in @([Uri]'https://www.nfse.gov.br/', [Uri]'https://nfse.gov.br/')) {{
    foreach ($cookie in $session.Cookies.GetCookies($cookieUri)) {{
        $domain = if ($cookie.Domain) {{ $cookie.Domain }} else {{ $cookieUri.Host }}
        $key = "$($cookie.Name)|$domain|$($cookie.Path)"
        if (-not $seen.ContainsKey($key)) {{
            $seen[$key] = $true
            $cookies += [PSCustomObject]@{{
                name = $cookie.Name
                value = $cookie.Value
                domain = $domain
                path = if ($cookie.Path) {{ $cookie.Path }} else {{ '/' }}
                secure = [bool]$cookie.Secure
                httpOnly = [bool]$cookie.HttpOnly
                expires = if ($cookie.Expires -and $cookie.Expires.Year -gt 1900) {{ $cookie.Expires.ToUniversalTime().ToString('o') }} else {{ $null }}
            }}
        }}
    }}
}}

[PSCustomObject]@{{
    created_at = (Get-Date).ToUniversalTime().ToString('o')
    start_url = $startUrl
    login_url = $url
    status_code = $statusCode
    redirect_location = $location
    target_final_url = $targetFinalUrl
    target_looks_logged_in = $targetLooksLoggedIn
    proxy = $proxy
    certificate = [PSCustomObject]@{{
        subject = $cert.Subject
        issuer = $cert.Issuer
        thumbprint = $cert.Thumbprint
        not_after = $cert.NotAfter.ToUniversalTime().ToString('o')
    }}
    cookies = $cookies
}} | ConvertTo-Json -Depth 6 -Compress
$store.Close()
"""
    proc = subprocess.run(
        [
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps,
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip())
    return json.loads(proc.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description="Gera sessao TXT do Portal Nacional NFS-e com escolha interativa de certificado.")
    parser.add_argument("--thumbprint", default=None, help="Thumbprint do certificado (pula a escolha interativa).")
    parser.add_argument("--cert-index", dest="cert_index", type=int, default=None, help="Numero do certificado na lista (1-based, pula a escolha interativa).")
    parser.add_argument("--pfx-file", default=None, help="Arquivo .pfx/.p12 para autenticar sem depender da store Windows.")
    parser.add_argument("--pfx-password", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--pfx-password-file", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--url", default=DEFAULT_URL, help="Endpoint de login por certificado.")
    parser.add_argument("--start-url", default=DEFAULT_START_URL, help="URL autenticada para validar e abrir depois.")
    parser.add_argument("--out", default=str(SESSION_FILE), help="Arquivo TXT de saida.")
    parser.add_argument("--proxy", default=None, help="Proxy HTTP opcional para gerar sessao com o IP do servidor.")
    args = parser.parse_args()

    if args.pfx_file:
        password = args.pfx_password or ""
        if args.pfx_password_file:
            password = Path(args.pfx_password_file).read_text(encoding="utf-8").strip()
        data = run_pfx_login(args.pfx_file, password, args.url, args.start_url, args.proxy)
    else:
        certificates = list_certificates()
        if args.thumbprint:
            thumbprint = args.thumbprint
        elif args.cert_index is not None:
            idx = args.cert_index
            if idx < 0 or idx >= len(certificates):
                print(f"--cert-index {args.cert_index} invalido. Ha {len(certificates)} certificado(s).", file=sys.stderr)
                raise SystemExit(1)
            thumbprint = certificates[idx]["thumbprint"]
        else:
            thumbprint = choose_certificate(certificates)
        data = run_powershell_login(thumbprint, args.url, args.start_url, args.proxy)
    data["saved_at_local"] = datetime.now().isoformat(timespec="seconds")

    out = Path(args.out)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Arquivo de sessao gravado em: {out}")
    print(f"Status: {data.get('status_code')} -> {data.get('redirect_location')}")
    print(f"Destino: {data.get('target_final_url')} logged_in={data.get('target_looks_logged_in')}")
    print(f"Cookies: {', '.join(c['name'] for c in data.get('cookies', []))}")
    print(f"Certificado: {data['certificate']['subject']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        raise SystemExit(1)

