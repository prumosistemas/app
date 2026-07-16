from urllib.parse import parse_qs, urlparse

from server.portal_nacional_automation import requests_page_url


def test_portal_index_url_does_not_send_date_filter() -> None:
    url = requests_page_url(
        "https://www.nfse.gov.br/EmissorNacional/Notas/Emitidas",
        1,
        "01/06/2026",
        "30/06/2026",
    )

    query = parse_qs(urlparse(url).query)
    assert "executar" not in query
    assert "datainicio" not in query
    assert "datafim" not in query
    assert "pg" not in query


def test_portal_index_url_removes_legacy_filter_and_keeps_pagination() -> None:
    url = requests_page_url(
        "https://www.nfse.gov.br/EmissorNacional/Notas/Recebidas?executar=1&datainicio=01%2F06%2F2026&datafim=30%2F06%2F2026",
        3,
        "01/06/2026",
        "30/06/2026",
    )

    query = parse_qs(urlparse(url).query)
    assert query == {"pg": ["3"]}
