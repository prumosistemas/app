import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import iss_closure_scan as scan  # noqa: E402


def _ctx(user: str = "usuario"):
    return SimpleNamespace(
        company_id="empresa",
        company_name="Empresa",
        user_id=user,
        user_email=f"{user}@example.com",
        user_role="member",
        via_worker=True,
    )


def _memory_storage(monkeypatch):
    storage = {}
    monkeypatch.setattr(scan, "db_get_json", lambda key, default: storage.get(key, default))
    monkeypatch.setattr(scan, "db_set_json", lambda key, value: storage.__setitem__(key, value))
    return storage


def test_company_and_pending_tables_are_parsed_from_jsf_html() -> None:
    companies = scan._parse_companies(
        """
        <tbody id="alteraInscricaoForm:empresaDataTable:tb"><tr>
          <td><a id="alteraInscricaoForm:empresaDataTable:3:linkDocumento">12.345.678/0001-90</a></td>
          <td><a id="alteraInscricaoForm:empresaDataTable:3:linkInscricao">123456</a></td>
          <td><a id="alteraInscricaoForm:empresaDataTable:3:linkNome">Empresa Exemplo</a></td>
        </tr></tbody>
        """,
        2,
    )
    assert companies == [
        {
            "page": 2,
            "idx": 3,
            "cnpj": "12.345.678/0001-90",
            "cnpj_digits": "12345678000190",
            "inscricao": "123456",
            "nome": "Empresa Exemplo",
        }
    ]

    rows = scan._parse_table_rows(
        """
        <tbody id="manterEscrituracaoForm:dataTablePendentes:tb"><tr>
          <td>07/2026</td><td>Aberta</td><td></td><td>01/08/2026</td>
        </tr></tbody>
        """,
        "manterEscrituracaoForm:dataTablePendentes:tb",
    )
    assert rows == [["07/2026", "Aberta", "", "01/08/2026"]]


def test_listed_company_position_avoids_three_call_cnpj_search(monkeypatch) -> None:
    company = {"page": 1, "idx": 3, "cnpj_digits": "12345678000190", "nome": "Empresa Exemplo"}
    html = """
      <input name="javax.faces.ViewState" value="state-1" />
      <tbody id="alteraInscricaoForm:empresaDataTable:tb"><tr>
        <td><a id="alteraInscricaoForm:empresaDataTable:3:linkDocumento">12.345.678/0001-90</a></td>
        <td><a id="alteraInscricaoForm:empresaDataTable:3:linkInscricao">123456</a></td>
        <td><a id="alteraInscricaoForm:empresaDataTable:3:linkNome">Empresa Exemplo</a></td>
      </tr></tbody>
    """
    monkeypatch.setattr(scan, "_search_company", lambda *_args: pytest.fail("fallback não deveria ser usado"))

    resolved, state = scan._resolve_company(object(), html, "state-0", company)

    assert resolved["idx"] == 3
    assert resolved["cnpj_digits"] == "12345678000190"
    assert state == "state-1"


def test_company_discovery_paginates_in_parallel_and_reports_progress(monkeypatch) -> None:
    def page_html(page: int, active: int | None = None) -> str:
        active_html = f'<td class="rich-datascr-act">{active}</td>' if active else ""
        return f"""
          <input name="javax.faces.ViewState" value="state-{page}" />
          <table><tr>{active_html}</tr></table>
          <tbody id="alteraInscricaoForm:empresaDataTable:tb"><tr>
            <td><a id="alteraInscricaoForm:empresaDataTable:{page}:linkDocumento">12.345.678/000{page}-90</a></td>
            <td><a id="alteraInscricaoForm:empresaDataTable:{page}:linkInscricao">{page}</a></td>
            <td><a id="alteraInscricaoForm:empresaDataTable:{page}:linkNome">Empresa {page}</a></td>
          </tr></tbody>
        """

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def login(self, *_args):
            return "state"

    called_pages = []
    monkeypatch.setattr(scan, "PortalBootstrapClient", FakeClient)
    monkeypatch.setattr(scan, "_open_company_modal", lambda _client: (page_html(1), "state-1"))

    def fake_fetch(_client, _state, page, _scroller):
        if page == "last":
            return page_html(5, active=5)
        called_pages.append(int(page))
        return page_html(int(page))

    monkeypatch.setattr(scan, "_fetch_companies_page", fake_fetch)
    progress = []
    companies = scan._discover_companies(
        {"usuario": "u", "senha": "s"},
        scan.threading.Event(),
        lambda done, total: progress.append((done, total)),
    )

    assert [item["page"] for item in companies] == [1, 2, 3, 4, 5]
    assert sorted(called_pages) == [2, 3, 4]
    assert progress[0] == (2, 5)
    assert progress[-1] == (5, 5)


def test_history_is_isolated_and_retained_at_five(monkeypatch) -> None:
    _memory_storage(monkeypatch)
    alan, bia = _ctx("alan"), _ctx("bia")
    scan._save_runs(
        alan,
        [{"run_id": f"run-{index}", "created_at": index, "status": "finished"} for index in range(8)],
    )
    scan._save_runs(bia, [{"run_id": "bia-1", "created_at": 1, "status": "finished"}])

    assert [item["run_id"] for item in scan._load_runs(alan)] == ["run-7", "run-6", "run-5", "run-4", "run-3"]
    assert [item["run_id"] for item in scan._load_runs(bia)] == ["bia-1"]


def test_create_scan_persists_references_but_not_credentials(monkeypatch) -> None:
    storage = _memory_storage(monkeypatch)
    monkeypatch.setattr(
        scan,
        "load_accounts_raw",
        lambda _ctx: [{"id": "conta-1", "alias": "Matriz", "usuario": "login-secreto", "senha": "senha-secreta"}],
    )
    monkeypatch.setattr(scan, "_schedule", lambda *_args: None)

    created = asyncio.run(scan.create_closure_scan(scan.ClosureScanCreateRequest(account_ids=["conta-1"]), _ctx()))
    serialized = repr(storage)

    assert created["account_ids"] == ["conta-1"]
    assert "login-secreto" not in serialized
    assert "senha-secreta" not in serialized
    assert "results" not in created


def test_same_account_cannot_be_scanned_twice_concurrently(monkeypatch) -> None:
    _memory_storage(monkeypatch)
    monkeypatch.setattr(
        scan,
        "load_accounts_raw",
        lambda _ctx: [{"id": "conta-1", "alias": "Matriz", "usuario": "u", "senha": "s"}],
    )
    monkeypatch.setattr(scan, "_schedule", lambda *_args: None)
    payload = scan.ClosureScanCreateRequest(account_ids=["conta-1"])
    asyncio.run(scan.create_closure_scan(payload, _ctx()))

    with pytest.raises(Exception) as exc:
        asyncio.run(scan.create_closure_scan(payload, _ctx()))
    assert getattr(exc.value, "status_code", None) == 409


def test_closure_scan_ui_is_before_instructions_and_has_multi_account_controls() -> None:
    source = (ROOT / "iss-fortaleza.html").read_text(encoding="utf-8")
    assert source.index('data-section="closureScanSection"') < source.index('data-section="instructionsSection"')
    assert 'id="closureAccountChoices"' in source
    assert 'id="btnStartClosureScan"' in source
    assert 'id="btnStopClosureScan"' in source
    assert 'id="btnDownloadClosureScan"' in source
    assert 'id="closureResultsBody"' in source
    assert '"/py/api/closure-scans"' in source


def test_request_concurrency_is_server_controlled() -> None:
    assert scan.GLOBAL_REQUEST_WORKERS == 6
    assert scan.PER_ACCOUNT_WORKERS == 4
    assert scan.COMPANIES_PER_SESSION == 12
