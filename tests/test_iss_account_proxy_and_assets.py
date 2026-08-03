from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_new_iss_account_uses_canonical_fastapi_route() -> None:
    for name in ("iss-fortaleza.html", "admin.html"):
        source = (ROOT / name).read_text(encoding="utf-8")
        assert ':"/py/api/accounts";' in source
        assert ':"/py/api/accounts/";' not in source


def test_python_proxy_normalizes_trailing_slashes_before_fetch() -> None:
    source = (ROOT / "cloudflare" / "worker.js").read_text(encoding="utf-8")
    assert 'rawPath.replace(/\\/+$/, "")' in source


def test_shadowed_images_are_forwarded_only_by_exact_path() -> None:
    source = (ROOT / "cloudflare" / "worker.js").read_text(encoding="utf-8")
    assert '"/login-farol.png"' in source
    assert '"/iss-fortaleza-logo.png"' in source
    assert "ORIGIN_STATIC_ASSET_PATHS.has(url.pathname)" in source
    assert "return await fetch(request);" in source


def test_password_change_screen_keeps_only_its_single_static_notice() -> None:
    source = (ROOT / "login.html").read_text(encoding="utf-8")
    assert source.count("Sua conta exige troca de senha antes de continuar.") == 1
    assert "Login aceito. Troque a senha para continuar." not in source
    assert "Sessão válida, mas a troca de senha é obrigatória." not in source
    show_change = source.split("function showChangePassword()", 1)[1].split("function showLogin()", 1)[0]
    assert "clearMsg();" in show_change
