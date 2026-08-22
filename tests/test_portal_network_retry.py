import sys
import json
from pathlib import Path

import requests
import pytest


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import portal_nacional_automation as automation  # noqa: E402


@pytest.fixture(autouse=True)
def clear_solver_endpoint_cooldowns(monkeypatch, tmp_path):
    automation.SOLVER_ENDPOINT_COOLDOWNS.clear()
    automation.SOLVER_ENDPOINT_SCORES.clear()
    automation.SOLVER_MODAL_REQUEST_LOCKS.clear()
    automation.SOLVER_MODAL_ROTATION_COUNTER = 0
    monkeypatch.setattr(automation, "SOLVER_STATUS_FILE", tmp_path / "solver-status.json")
    yield
    automation.SOLVER_ENDPOINT_COOLDOWNS.clear()
    automation.SOLVER_ENDPOINT_SCORES.clear()
    automation.SOLVER_MODAL_REQUEST_LOCKS.clear()
    automation.SOLVER_MODAL_ROTATION_COUNTER = 0


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


def test_portal_keepalive_uses_existing_session_without_download(monkeypatch) -> None:
    calls = []

    class FakeSession:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            response = requests.Response()
            response.status_code = 200
            response.url = url
            response._content = b"Total de 0 registros"
            return response

        def close(self):
            calls.append(("close", {}))

    monkeypatch.setattr(
        automation,
        "requests_session_from_data",
        lambda _data: FakeSession(),
    )

    result = automation.portal_session_keepalive_once(
        {"cookies": [{"name": "Emissor", "value": "indireto"}]},
        automation.MODE_URLS["recebidas"],
    )

    assert result == "ok"
    assert calls[0][0] == automation.MODE_URLS["recebidas"]
    assert calls[-1][0] == "close"


def test_solver_outage_gate_probes_one_item_before_reopening_pool(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(automation, "require_solver_api", lambda url: url)
    monkeypatch.setattr(
        automation,
        "portal_session_keepalive",
        lambda *_args, **_kwargs: automation.nullcontext(),
    )
    sleeps = []
    monkeypatch.setattr(automation.time, "sleep", sleeps.append)
    calls: dict[str, int] = {}

    def fake_download(_session, item, *_args, **_kwargs):
        key = item["id"]
        calls[key] = calls.get(key, 0) + 1
        if key in {"nota-1", "nota-2", "nota-3", "nota-4"} and calls[key] == 1:
            return {
                "ok": False,
                "reason": "solver:all_endpoints_failed: 503 Service Unavailable",
            }
        target = tmp_path / f"{key}.xml"
        target.write_text("<NFSe />", encoding="utf-8")
        return {
            "ok": True,
            "files": [str(target)],
            "files_by_tipo": {"xml": str(target)},
            "methods_by_tipo": {"xml": "captcha_xml"},
            "method": "captcha_xml",
        }

    monkeypatch.setattr(automation, "download_item_requests", fake_download)
    session_path = tmp_path / "session.json"
    index_path = tmp_path / "indice.json"
    session_path.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    index = automation.load_index(index_path, "recebidas")
    index["items"] = {
        f"nota-{number}": {
            "id": f"nota-{number}",
            "page": 1,
            "status": "pendente",
        }
        for number in range(1, 7)
    }

    automation.run_requests_downloads(
        index,
        index_path,
        session_path,
        "https://solver.example/solve",
        tmp_path / "downloads",
        0,
        4,
        max_attempts=3,
        tipo_download="xml",
    )

    event_names = [event["event"] for event in index["events"]]
    assert "solver_outage_gate_opened" in event_names
    assert "solver_outage_probe_started" in event_names
    assert "solver_outage_gate_closed" in event_names
    assert event_names.index("solver_outage_probe_started") < event_names.index(
        "solver_outage_gate_closed"
    )
    assert sleeps[0] == 10
    assert index["totals"]["baixados"] == 6


def test_portal_index_retries_503_with_growing_backoff(monkeypatch, tmp_path: Path) -> None:
    responses = []
    for status, body in ((503, "temporariamente indisponivel"), (200, "Total de 0 registros")):
        response = requests.Response()
        response.status_code = status
        response.url = "https://www.nfse.gov.br/EmissorNacional/Notas/Recebidas"
        response._content = body.encode()
        responses.append(response)

    class FakeSession:
        def get(self, *args, **kwargs):
            return responses.pop(0)

    sleeps = []
    monkeypatch.setattr(automation, "requests_session_from_data", lambda data: FakeSession())
    monkeypatch.setattr(automation.time, "sleep", sleeps.append)
    session_path = tmp_path / "session.json"
    index_path = tmp_path / "indice.json"
    session_path.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    index = automation.load_index(index_path, "recebidas")

    automation.run_requests_index(
        index,
        index_path,
        session_path,
        "recebidas",
        automation.MODE_URLS["recebidas"],
        "01/07/2026",
        "30/07/2026",
        None,
    )

    assert sleeps == [15]
    assert index["status"] == "indice_pronto"
    assert any(event["event"] == "requests_index_retry_wait" for event in index["events"])


def test_portal_index_keeps_retrying_transient_outage_past_soft_limit(monkeypatch, tmp_path: Path) -> None:
    responses = []
    for status, body in ((503, "indisponivel"), (503, "indisponivel"), (200, "Total de 0 registros")):
        response = requests.Response()
        response.status_code = status
        response.url = "https://www.nfse.gov.br/EmissorNacional/Notas/Recebidas"
        response._content = body.encode()
        responses.append(response)

    class FakeSession:
        def get(self, *args, **kwargs):
            return responses.pop(0)

    sleeps = []
    monkeypatch.setenv("PORTAL_INDEX_HTTP_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(automation, "requests_session_from_data", lambda data: FakeSession())
    monkeypatch.setattr(automation.time, "sleep", sleeps.append)
    session_path = tmp_path / "session.json"
    index_path = tmp_path / "indice.json"
    session_path.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    index = automation.load_index(index_path, "recebidas")

    automation.run_requests_index(
        index,
        index_path,
        session_path,
        "recebidas",
        automation.MODE_URLS["recebidas"],
        "01/07/2026",
        "30/07/2026",
        None,
    )

    assert sleeps == [15, 30]
    assert index["status"] == "indice_pronto"


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

    def fake_solve(url, sitekey, request_id, page_url=None):
        calls.append(url)
        if "primary" in url:
            raise requests.ConnectionError("offline")
        return "token-fallback"

    monkeypatch.setattr(automation, "solve_captcha_once", fake_solve)
    token = automation.solve_captcha_with_url("https://primary.example/solve", "key", "run")

    assert token == "token-fallback"
    assert calls == ["https://primary.example/solve", "https://fallback.example/solve"]


def test_cold_health_timeout_keeps_primary_for_real_post(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise requests.ReadTimeout("container em prewarm")

    monkeypatch.setattr(automation.requests, "get", timeout)

    primary = "https://primary.example/solve"
    assert automation.require_solver_api(primary) == primary
    assert automation.SOLVER_ENDPOINT_COOLDOWNS == {}


def test_visual_failure_tries_second_modal_before_local_solver(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        automation,
        "SOLVER_FALLBACK_URLS",
        ["https://modal-2.example/solve", "http://127.0.0.1:8876/solve"],
    )
    monkeypatch.setattr(automation, "SOLVER_FALLBACK_URL", "https://modal-2.example/solve")

    def fake_solve(url, sitekey, request_id, page_url=None):
        calls.append(url)
        if "primary" in url:
            raise RuntimeError("solver:visual_challenge_not_ready: grade movel")
        if "modal-2" in url:
            return "token-modal-2"
        return "token-local"

    monkeypatch.setattr(automation, "solve_captcha_once", fake_solve)

    assert automation.solve_captcha_with_url(
        "https://primary.example/solve", "key", "run"
    ) == "token-modal-2"
    assert calls == ["https://primary.example/solve", "https://modal-2.example/solve"]


def test_solver_failure_message_redacts_url_queries(monkeypatch) -> None:
    monkeypatch.setattr(automation, "SOLVER_FALLBACK_URLS", [])
    monkeypatch.setattr(automation, "SOLVER_FALLBACK_URL", "")

    def fake_solve(url, sitekey, request_id, page_url=None):
        raise RuntimeError(
            "solver:failed: https://solver.example/solve?token=segredo&attempt=abc"
        )

    monkeypatch.setattr(automation, "solve_captcha_once", fake_solve)

    with pytest.raises(RuntimeError) as raised:
        automation.solve_captcha_with_url(
            "https://solver.example/solve?access=nao-pode-vazar", "key", "run"
        )

    detail = str(raised.value)
    assert "segredo" not in detail
    assert "nao-pode-vazar" not in detail
    assert "https://solver.example/solve" in detail


def test_blank_fallback_configuration_uses_residential_solver() -> None:
    assert automation.configured_solver_fallback_url("") == (
        "http://127.0.0.1:8876/solve"
    )


def test_multiple_modal_fallbacks_keep_local_solver_last() -> None:
    assert automation.configured_solver_fallback_urls(
        "https://modal-2.example/solve; https://modal-3.example/solve"
    ) == [
        "https://modal-2.example/solve",
        "https://modal-3.example/solve",
        "http://127.0.0.1:8876/solve",
    ]


def test_solver_candidates_are_ordered_and_unique(monkeypatch) -> None:
    monkeypatch.setattr(
        automation,
        "SOLVER_FALLBACK_URLS",
        ["https://modal-2.example/solve", "http://127.0.0.1:8876/solve"],
    )
    monkeypatch.setattr(
        automation,
        "SOLVER_FALLBACK_URL",
        "https://modal-2.example/solve",
    )
    assert automation.solver_url_candidates("https://modal-1.example/solve") == [
        "https://modal-1.example/solve",
        "https://modal-2.example/solve",
        "http://127.0.0.1:8876/solve",
    ]


def test_primary_modal_is_preferred_and_local_remains_last() -> None:
    candidates = [
        "https://primary--solver.modal.run/solve",
        "https://fallback--solver.modal.run/solve",
        "http://127.0.0.1:8876/solve",
    ]
    first = automation.balance_modal_solver_candidates(candidates, "nota-a")
    second = automation.balance_modal_solver_candidates(candidates, "nota-b")

    assert first[:2] == second[:2] == candidates[:2]
    assert first[-1] == second[-1] == "http://127.0.0.1:8876/solve"


def test_recovered_primary_is_preferred_over_fallback_history(monkeypatch, tmp_path) -> None:
    primary = "https://primary--solver.modal.run/solve"
    fallback = "https://fallback--solver.modal.run/solve"
    residential = "http://127.0.0.1:8876/solve"
    monkeypatch.setattr(automation, "SOLVER_STATUS_FILE", tmp_path / "status.json")

    automation.record_solver_endpoint_event(primary, "failure", "nota-1", RuntimeError("offline"))
    automation.record_solver_endpoint_event(fallback, "success", "nota-1")

    ordered = automation.balance_modal_solver_candidates([primary, fallback, residential], "nota-2")
    assert ordered == [primary, fallback, residential]


def test_visual_failure_tries_second_modal_before_residential(monkeypatch) -> None:
    primary = "https://modal-1.example/solve"
    fallback = "https://modal-2.example/solve"
    residential = "http://127.0.0.1:8876/solve"
    calls: list[str] = []

    monkeypatch.setattr(
        automation,
        "wait_for_solver_candidates",
        lambda _primary: [primary, fallback, residential],
    )
    monkeypatch.setattr(automation, "record_solver_endpoint_event", lambda *args: None)
    monkeypatch.setattr(automation, "mark_solver_endpoint_unavailable", lambda *args: 0)
    monkeypatch.setattr(automation, "clear_solver_endpoint_cooldown", lambda *args: None)

    def fake_solve(url, _sitekey, _request_id, _page_url=None):
        calls.append(url)
        if url == primary:
            raise RuntimeError("solver:visual_challenge_not_opened")
        if url == fallback:
            return "token-modal-2"
        raise AssertionError("ThinkPad nao deveria ser usado apos sucesso no segundo Modal")

    monkeypatch.setattr(automation, "solve_captcha_once", fake_solve)

    assert automation.solve_captcha_with_url(primary, "sitekey", "nota") == "token-modal-2"
    assert calls == [primary, fallback]


def test_solver_telemetry_contains_no_url_query_or_exception_text(monkeypatch, tmp_path) -> None:
    target = tmp_path / "solver-status.json"
    monkeypatch.setattr(automation, "SOLVER_STATUS_FILE", target)
    automation.record_solver_endpoint_event(
        "https://modal.example/solve?token=nao-pode-vazar",
        "failure",
        "empresa-nota-123",
        RuntimeError("segredo no erro"),
    )
    payload = target.read_text(encoding="utf-8")
    assert "modal.example" in payload
    assert "nao-pode-vazar" not in payload
    assert "segredo no erro" not in payload


def test_solver_receives_real_nfse_page_context(monkeypatch) -> None:
    captured = {}

    def fake_post(*args, **kwargs):
        captured.update(kwargs["json"])
        return FakeResponse(200, {"success": True, "token": "token-ok"})

    monkeypatch.setattr(automation.requests, "post", fake_post)
    page_url = "https://www.nfse.gov.br/EmissorNacional/DPS/ModalCaptcha/Abrir/"

    assert automation.solve_captcha_once(
        "https://solver.example/solve", "key", "nota", page_url
    ) == "token-ok"
    assert captured["url"] == page_url


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
        10,
        20,
        30,
        60,
        90,
        120,
    ]


def test_visual_failure_does_not_cool_down_entire_modal_pool() -> None:
    assert automation.solver_endpoint_cooldown_seconds(
        RuntimeError("solver:grade_9_nao_estabilizou: desafio dificil")
    ) == 0


def test_not_ready_is_scoped_to_the_current_captcha() -> None:
    assert automation.solver_endpoint_cooldown_seconds(
        RuntimeError("solver:visual_challenge_not_ready: sessao visual indisponivel")
    ) == 0


def test_google_session_failure_cools_only_that_endpoint() -> None:
    assert automation.solver_endpoint_cooldown_seconds(
        RuntimeError("solver:google_ai_request_failed: sessao anonima indisponivel")
    ) == 300


def test_local_google_session_failure_does_not_hide_residential_fallback() -> None:
    assert automation.mark_solver_endpoint_unavailable(
        "http://127.0.0.1:8876/solve",
        RuntimeError("solver:google_ai_request_failed: sessao anonima indisponivel"),
    ) == 0


def test_modal_container_outage_has_short_pool_cooldown() -> None:
    response = requests.Response()
    response.status_code = 503
    error = requests.HTTPError("temporariamente indisponivel", response=response)
    assert automation.mark_solver_endpoint_unavailable(
        "https://conta--solver.modal.run/solve", error
    ) == 15


def test_disabled_modal_workspace_has_long_shared_cooldown() -> None:
    response = requests.Response()
    response.status_code = 404
    error = requests.HTTPError("workspace is disabled", response=response)
    url = "https://conta--solver.modal.run/solve"

    automation.record_solver_endpoint_event(url, "failure", "health", error)

    assert automation.mark_solver_endpoint_unavailable(url, error) == (
        automation.SOLVER_MODAL_DISABLED_RECHECK_SECONDS
    )
    assert automation.persisted_solver_endpoint_cooldown_remaining(url) > (
        automation.SOLVER_MODAL_DISABLED_RECHECK_SECONDS - 5
    )


def test_disabled_primary_automatically_rejoins_after_shared_cooldown(
    monkeypatch, tmp_path
) -> None:
    primary = "https://primary--solver.modal.run/solve"
    fallback = "https://fallback--solver.modal.run/solve"
    residential = "http://127.0.0.1:8876/solve"
    monkeypatch.setattr(automation, "SOLVER_STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(automation, "SOLVER_FALLBACK_URL", fallback)
    monkeypatch.setattr(automation, "SOLVER_FALLBACK_URLS", [fallback, residential])
    monkeypatch.setattr(automation, "SOLVER_MODAL_DISABLED_RECHECK_SECONDS", 300)

    endpoint_file = automation.solver_endpoint_state_file(primary)
    endpoint_file.parent.mkdir(parents=True, exist_ok=True)
    endpoint_file.write_text(
        json.dumps(
            {
                "event": "failure",
                "error_kind": "http_404",
                "at": "2020-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    available, wait_seconds = automation.available_solver_url_candidates(primary)
    assert wait_seconds is None
    assert automation.balance_modal_solver_candidates(available, "nota") == [
        primary,
        fallback,
        residential,
    ]


def test_explicit_google_block_keeps_modal_pool_available() -> None:
    assert automation.mark_solver_endpoint_unavailable(
        "https://conta--solver.modal.run/solve",
        RuntimeError("solver:google_ai_request_failed: unusual traffic /sorry/index"),
    ) == 10


def test_inflight_modal_attempt_rechecks_shared_cooldown(monkeypatch) -> None:
    modal = "https://conta--solver.modal.run/solve"
    residential = "http://127.0.0.1:8876/solve"
    calls: list[str] = []
    monkeypatch.setattr(
        automation,
        "wait_for_solver_candidates",
        lambda _primary: [modal, residential],
    )
    monkeypatch.setattr(automation, "record_solver_endpoint_event", lambda *args: None)
    with automation.SOLVER_ENDPOINT_COOLDOWN_LOCK:
        automation.SOLVER_ENDPOINT_COOLDOWNS[modal] = automation.time.monotonic() + 300

    def fake_solve(url, *_args, **_kwargs):
        calls.append(url)
        return "token-local"

    monkeypatch.setattr(automation, "solve_captcha_once", fake_solve)

    assert automation.solve_captcha_with_url(modal, "sitekey", "nota") == "token-local"
    assert calls == [residential]


def test_solver_preserves_json_reason_from_503(monkeypatch) -> None:
    response = requests.Response()
    response.status_code = 503
    response.url = "https://solver.example/solve"
    response._content = json.dumps(
        {
            "success": False,
            "reason": "google_ai_request_failed",
            "error": "unusual traffic",
        }
    ).encode()
    monkeypatch.setattr(automation.requests, "post", lambda *args, **kwargs: response)

    with pytest.raises(RuntimeError, match="google_ai_request_failed.*unusual traffic"):
        automation.solve_captcha_once("https://solver.example/solve", "key", "nota")


def test_generic_json_503_keeps_short_modal_pool_cooldown(monkeypatch) -> None:
    response = requests.Response()
    response.status_code = 503
    response.url = "https://conta--solver.modal.run/solve"
    response._content = json.dumps(
        {
            "success": False,
            "reason": "container_unavailable",
            "error": "temporary backend outage",
        }
    ).encode()
    monkeypatch.setattr(automation.requests, "post", lambda *args, **kwargs: response)

    with pytest.raises(RuntimeError) as captured:
        automation.solve_captcha_once(response.url, "key", "nota")

    assert automation.mark_solver_endpoint_unavailable(response.url, captured.value) == 15


def test_endpoint_outages_still_open_cooldown() -> None:
    response = requests.Response()
    response.status_code = 503
    error = requests.HTTPError("temporariamente indisponivel", response=response)
    assert automation.solver_endpoint_cooldown_seconds(error) == 90
