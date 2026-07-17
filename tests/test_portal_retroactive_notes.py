from urllib.parse import parse_qs, urlparse

import pytest

from server.portal_nacional_automation import build_portal_date_windows, requests_page_url


def test_portal_index_url_sends_date_filter() -> None:
    url = requests_page_url(
        "https://www.nfse.gov.br/EmissorNacional/Notas/Emitidas",
        1,
        "01/06/2026",
        "30/06/2026",
    )

    query = parse_qs(urlparse(url).query)
    assert query["executar"] == ["1"]
    assert query["datainicio"] == ["01/06/2026"]
    assert query["datafim"] == ["30/06/2026"]
    assert "pg" not in query


def test_portal_index_url_replaces_legacy_filter_and_keeps_pagination() -> None:
    url = requests_page_url(
        "https://www.nfse.gov.br/EmissorNacional/Notas/Recebidas?executar=1&datainicio=01%2F06%2F2026&datafim=30%2F06%2F2026",
        3,
        "01/07/2026",
        "17/07/2026",
    )

    query = parse_qs(urlparse(url).query)
    assert query == {
        "executar": ["1"],
        "datainicio": ["01/07/2026"],
        "datafim": ["17/07/2026"],
        "pg": ["3"],
    }


def test_portal_period_is_split_at_month_boundary() -> None:
    assert build_portal_date_windows("01/06/2026", "17/07/2026") == [
        {"index": 1, "data_inicial": "01/06/2026", "data_final": "30/06/2026", "dias": 30},
        {"index": 2, "data_inicial": "01/07/2026", "data_final": "17/07/2026", "dias": 17},
    ]


def test_portal_31_day_month_never_exceeds_30_days() -> None:
    assert build_portal_date_windows("01/07/2026", "31/07/2026") == [
        {"index": 1, "data_inicial": "01/07/2026", "data_final": "30/07/2026", "dias": 30},
        {"index": 2, "data_inicial": "31/07/2026", "data_final": "31/07/2026", "dias": 1},
    ]


def test_portal_period_requires_both_dates() -> None:
    with pytest.raises(ValueError, match="devem ser informadas juntas"):
        build_portal_date_windows("01/06/2026", None)
