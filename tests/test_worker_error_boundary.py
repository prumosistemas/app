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


def test_login_serializes_d1_writes_and_leaves_cleanup_to_cron() -> None:
    source = (ROOT / "cloudflare" / "worker.js").read_text(encoding="utf-8")
    login = source.split("async function handleLoginPost", 1)[1].split("async function handleLogout", 1)[0]

    assert "scheduleCleanup" not in login
    assert "Promise.all" not in login
    assert "checkLoginRateLimits" in login
    assert "persistSuccessfulLogin" in login
    assert '"login_rate_limit"' in source
    assert '"login_session_persist"' in source
    assert "D1_TRANSIENT_MAX_ATTEMPTS = 3" in source


def test_worker_errors_are_searchable_by_support_code_and_stage() -> None:
    source = (ROOT / "cloudflare" / "worker.js").read_text(encoding="utf-8")

    assert 'event: "worker_request_failed"' in source
    assert "request_id: requestId" in source
    assert 'stage: String(err?.prumoStage || "request_handler")' in source


def test_worker_uses_safe_d1_session_for_read_replication() -> None:
    source = (ROOT / "cloudflare" / "worker.js").read_text(encoding="utf-8")

    assert "env = withRequestD1Session(env);" in source
    assert 'env.db.withSession("first-primary")' in source
    assert "async scheduled(_event, env, ctx)" in source


def test_python_proxy_hides_cloudflare_tunnel_html() -> None:
    source = (ROOT / "cloudflare" / "worker.js").read_text(encoding="utf-8")
    proxy = source.split("async function handlePythonProxy", 1)[1].split("function pythonHeaders", 1)[0]

    assert "isPythonInfrastructureFailure(upstreamResponse)" in proxy
    assert 'status === 530' in proxy
    assert 'code: "UPSTREAM_TEMPORARILY_UNAVAILABLE"' in proxy
    assert "upstreamResponse.body?.cancel()" in proxy
    assert "new Response(upstreamResponse.body" in proxy
