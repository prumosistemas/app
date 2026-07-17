from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_worker_routes_await_async_handlers_inside_error_boundary() -> None:
    source = (ROOT / "cloudflare" / "worker.js").read_text(encoding="utf-8")

    assert not re.search(r"return\s+handle[A-Za-z0-9_]+\(", source)
    assert len(re.findall(r"return\s+await\s+handle[A-Za-z0-9_]+\(", source)) >= 20


def test_login_does_not_expose_infrastructure_html_as_error_text() -> None:
    source = (ROOT / "login.html").read_text(encoding="utf-8")

    assert "looksLikeHtml" in source
    assert "Serviço temporariamente indisponível" in source
    assert 'res.headers.get("cf-ray")' in source
