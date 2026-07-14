import sys
from pathlib import Path

import requests


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import portal_nacional_automation as automation  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def test_nfse_session_retries_only_safe_methods() -> None:
    session = automation.requests_session_from_data({"cookies": []})
    retry = session.get_adapter("https://").max_retries
    assert retry.connect == 4
    assert "GET" in retry.allowed_methods
    assert "POST" not in retry.allowed_methods


def test_async_solver_poll_survives_transient_timeout(monkeypatch) -> None:
    responses = iter(
        [
            requests.ReadTimeout("oscilacao"),
            FakeResponse(202, {"accepted": True, "status": "pending"}),
            FakeResponse(200, {"success": True, "token": "token-ok"}),
        ]
    )

    monkeypatch.setattr(
        automation.requests,
        "post",
        lambda *args, **kwargs: FakeResponse(202, {"accepted": True, "job_id": "job-1"}),
    )

    def fake_get(*args, **kwargs):
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(automation.requests, "get", fake_get)
    monkeypatch.setattr(automation.time, "sleep", lambda seconds: None)

    assert automation.solve_captcha_with_url("https://solver.example/solve", "key", "run") == "token-ok"


def test_solver_uses_fallback_after_primary_failure(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(automation, "SOLVER_FALLBACK_URL", "https://fallback.example/solve")

    def fake_solve(url, sitekey, request_id):
        calls.append(url)
        if "primary" in url:
            raise requests.ConnectionError("offline")
        return "token-fallback"

    monkeypatch.setattr(automation, "solve_captcha_once", fake_solve)
    token = automation.solve_captcha_with_url("https://primary.example/solve", "key", "run")

    assert token == "token-fallback"
    assert calls == ["https://primary.example/solve", "https://fallback.example/solve"]


def test_blank_fallback_configuration_uses_residential_solver() -> None:
    assert automation.configured_solver_fallback_url("") == (
        "http://127.0.0.1:8876/solve"
    )


def test_solver_async_urls_preserve_access_token() -> None:
    solver = "https://solver.example/internal/solve?token=segredo"
    assert automation.solver_api_health_url(solver) == (
        "https://solver.example/internal/health?token=segredo"
    )
    assert automation.solver_api_job_url(solver, "job com espaço") == (
        "https://solver.example/internal/jobs/job%20com%20espa%C3%A7o?token=segredo"
    )


def test_solver_outage_backoff_grows_across_different_items() -> None:
    assert automation.is_transient_solver_outage(
        {"reason": "solver:all_endpoints_failed: 503 Service Unavailable"}
    )
    assert not automation.is_transient_solver_outage({"reason": "arquivo_invalido"})
    assert [automation.retry_backoff_seconds(2, streak) for streak in range(1, 7)] == [
        4,
        8,
        16,
        32,
        64,
        120,
    ]
