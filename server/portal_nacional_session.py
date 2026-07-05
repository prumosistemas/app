import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


DEFAULT_URL = "https://www.nfse.gov.br/EmissorNacional/Certificado"
DEFAULT_START_URL = "https://www.nfse.gov.br/EmissorNacional/Notas/Recebidas"
SESSION_FILE = Path(__file__).with_name("sessao_nfse.txt")


def list_certificates():
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
    raw = proc.stdout.strip()
    if not raw or raw == "null":
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        return [data]
    return data


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


def run_powershell_login(thumbprint: str, url: str, start_url: str) -> dict:
    ps = rf"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$thumbprint = '{thumbprint}'
$url = '{url}'
$startUrl = '{start_url}'
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
    $response = Invoke-WebRequest -Uri $url -Certificate $cert -WebSession $session -MaximumRedirection 5 -UseBasicParsing
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

$targetResponse = Invoke-WebRequest -Uri $startUrl -Certificate $cert -WebSession $session -MaximumRedirection 5 -UseBasicParsing
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
    parser.add_argument("--url", default=DEFAULT_URL, help="Endpoint de login por certificado.")
    parser.add_argument("--start-url", default=DEFAULT_START_URL, help="URL autenticada para validar e abrir depois.")
    parser.add_argument("--out", default=str(SESSION_FILE), help="Arquivo TXT de saida.")
    args = parser.parse_args()

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

    data = run_powershell_login(thumbprint, args.url, args.start_url)
    data["saved_at_local"] = datetime.now().isoformat(timespec="seconds")

    out = Path(args.out)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Sessao salva em: {out}")
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

