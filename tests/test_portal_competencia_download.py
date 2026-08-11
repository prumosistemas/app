import sys
import zipfile
from pathlib import Path

import pytest


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import portal_nacional  # noqa: E402
from portal_nacional_competencia import (  # noqa: E402
    competencia_from_item,
    competencia_from_xml_bytes,
    summarize_competencias,
)


def xml_for_competencia(value: str) -> bytes:
    return f'<NFSe xmlns="urn:teste"><infNFSe><DPS><infDPS><dCompet>{value}</dCompet></infDPS></DPS></infNFSe></NFSe>'.encode()


def test_competencia_is_read_from_namespaced_nfse_xml() -> None:
    assert competencia_from_xml_bytes(xml_for_competencia("2026-06-30")) == "2026-06"


def test_competencia_summary_counts_retroactive_notes(tmp_path: Path) -> None:
    june = tmp_path / "june.xml"
    july = tmp_path / "july.xml"
    june.write_bytes(xml_for_competencia("2026-06-30"))
    july.write_bytes(xml_for_competencia("2026-07-01"))
    items = [
        {"status": "baixado", "files_by_tipo": {"xml": str(june)}},
        {"status": "baixado", "files_by_tipo": {"xml": str(july)}},
        {"status": "erro", "competencia": "2026-07"},
    ]

    summary = summarize_competencias(items)

    assert competencia_from_item(items[0]) == "2026-06"
    assert summary["2026-06"] == {
        "competencia": "2026-06",
        "total": 1,
        "baixados": 1,
        "pendentes": 0,
        "erros": 0,
    }
    assert summary["2026-07"]["total"] == 2
    assert summary["2026-07"]["erros"] == 1


def test_portal_zip_entries_filter_and_separate_competencias(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    downloads = run_dir / "downloads"
    pdf_dir = downloads / "pdf"
    pdf_dir.mkdir(parents=True)
    june_xml = downloads / "june.xml"
    june_pdf = pdf_dir / "june.pdf"
    july_xml = downloads / "july.xml"
    june_xml.write_bytes(xml_for_competencia("2026-06-10"))
    june_pdf.write_bytes(b"%PDF-1.7 june")
    july_xml.write_bytes(xml_for_competencia("2026-07-10"))
    (run_dir / "logs").mkdir()
    (run_dir / "logs" / "private.log").write_text("nao incluir", encoding="utf-8")
    index = {
        "items": {
            "june": {"files_by_tipo": {"xml": str(june_xml), "pdf": str(june_pdf)}},
            "july": {"files_by_tipo": {"xml": str(july_xml)}},
        }
    }

    all_entries = portal_nacional._portal_download_entries(run_dir, index, set())
    june_entries = portal_nacional._portal_download_entries(run_dir, index, {"2026-06"})

    assert len(all_entries) == 3
    assert {entry["competencia"] for entry in june_entries} == {"2026-06"}
    assert {portal_nacional._zip_arcname(entry, True) for entry in all_entries} == {
        "06-2026/XML/june.xml",
        "06-2026/PDF/june.pdf",
        "07-2026/XML/july.xml",
    }
    assert all(entry["path"].name != "private.log" for entry in all_entries)


def test_combined_portal_zip_separates_mode_and_competencia(tmp_path: Path) -> None:
    received = tmp_path / "recebida.xml"
    issued = tmp_path / "emitida.xml"
    received.write_bytes(xml_for_competencia("2026-06-10"))
    issued.write_bytes(xml_for_competencia("2026-07-10"))
    output = tmp_path / "combined.zip"

    portal_nacional._write_portal_zip(
        output,
        [
            {"path": received, "competencia": "2026-06", "kind": "XML", "modo": "recebidas"},
            {"path": issued, "competencia": "2026-07", "kind": "XML", "modo": "emitidas"},
        ],
        separate_competencias=True,
        separate_modos=True,
    )

    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "Recebidas/06-2026/XML/recebida.xml",
            "Emitidas/07-2026/XML/emitida.xml",
        }


def test_invalid_competencia_filter_is_rejected() -> None:
    with pytest.raises(Exception):
        portal_nacional._selected_competencias(["../../segredo"])


def test_portal_503_is_reported_as_operational_outage(tmp_path: Path) -> None:
    index_path = tmp_path / "indice.json"
    index_path.write_text(
        '{"items":{},"totals":{},"events":[{"event":"requests_index_retry_wait","status_code":503,"delay_seconds":60,"attempt":2}]}',
        encoding="utf-8",
    )

    summary = portal_nacional._summarize_index(index_path)

    assert summary["problema_operacional"] == {
        "code": "portal_indisponivel_temporario",
        "message": "Portal Nacional temporariamente indisponível.",
        "status_code": 503,
        "retry_in_seconds": 60,
        "attempt": 2,
    }


def test_portal_frontend_uses_competence_selector_without_files_box() -> None:
    source = (Path(__file__).resolve().parents[1] / "portal-nacional.html").read_text(encoding="utf-8")
    assert 'id="filesBox"' not in source
    assert 'id="competenceBox"' in source
    assert 'data-competence="todas"' in source
    assert 'combinedQuery.append("run_id", child.run_id)' in source
    assert 'combinedQuery.append("competencia", value)' in source
    assert "responseErrorMessage" in source
    assert "looksLikeHtml" in source
