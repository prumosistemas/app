import sys
from pathlib import Path

import pytest
from fastapi import HTTPException


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import portal_nacional  # noqa: E402
import portal_nacional_session  # noqa: E402


def test_certificate_login_uses_dedicated_mtls_host() -> None:
    assert portal_nacional_session.DEFAULT_URL == (
        "https://certificado.nfse.gov.br/EmissorNacional/Certificado"
    )


def test_rejects_password_that_cannot_be_decrypted(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(portal_nacional, "unprotect_secret", lambda value: "")
    cert = {
        "file": tmp_path / "cert.pfx",
        "meta": {"password": "enc:v1:token-antigo"},
    }

    with pytest.raises(HTTPException) as caught:
        portal_nacional._uploaded_certificate_password(cert)

    assert caught.value.status_code == 409
    assert "segredo atual" in caught.value.detail


def test_validates_decrypted_password_before_writing_run(monkeypatch, tmp_path) -> None:
    seen = {}
    monkeypatch.setattr(portal_nacional, "unprotect_secret", lambda value: "senha-valida")
    monkeypatch.setattr(
        portal_nacional,
        "load_pfx_identity",
        lambda path, password: seen.update(path=path, password=password) or {},
    )
    cert = {
        "file": tmp_path / "cert.pfx",
        "meta": {"password": "enc:v1:token-atual"},
    }

    assert portal_nacional._uploaded_certificate_password(cert) == "senha-valida"
    assert seen == {"path": cert["file"], "password": "senha-valida"}
