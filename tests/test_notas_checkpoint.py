from pathlib import Path
from types import SimpleNamespace
import json
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from flow_errors import FlowError
from flow_notas import (
    _append_checkpoint_page,
    _chaves_nfse_xml,
    _classify_pagination_end,
    _cleanup_stale_partial_files,
    _load_checkpoint,
    _new_checkpoint,
    _save_checkpoint,
    _validate_checkpoint_files,
)


def _ctx():
    return SimpleNamespace(cnpj="11415798000159", mes="06/2026")


def _write_xml(path: Path, codes: list[str]) -> None:
    root = ET.Element("NFSES")
    for index, code in enumerate(codes, start=1):
        wrapper = ET.SubElement(root, "Nfse")
        nested = ET.SubElement(wrapper, "Nfse")
        ET.SubElement(nested, "Numero").text = str(index)
        ET.SubElement(nested, "CodigoVerificacao").text = code
        ET.SubElement(nested, "DataEmissao").text = "2026-06-01T10:00:00-03:00"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def test_checkpoint_is_atomic_and_validates_saved_xml(tmp_path: Path) -> None:
    ctx = _ctx()
    xml_path = tmp_path / "prestadas_lote001.xml"
    _write_xml(xml_path, ["A", "B", "C"])
    keys = _chaves_nfse_xml(str(xml_path))

    checkpoint = _new_checkpoint("prestadas", ctx)
    _append_checkpoint_page(
        checkpoint,
        page=1,
        rows=3,
        fingerprint="fingerprint-1",
        file_path=str(xml_path),
        records=3,
        keys=keys,
        pagination={
            "has_paginator": False,
            "active_page": None,
            "visible_pages": [],
            "next_enabled": False,
            "forward_disabled": False,
        },
    )

    checkpoint_path = tmp_path / "prestadas.json"
    _save_checkpoint(str(checkpoint_path), checkpoint)

    loaded = _load_checkpoint(str(checkpoint_path), "prestadas", ctx)
    seen, exports = _validate_checkpoint_files(loaded)

    assert loaded["last_completed_page"] == 1
    assert loaded["next_page"] == 2
    assert loaded["records_total"] == 3
    assert len(seen) == 3
    assert len(exports) == 1
    assert not list(tmp_path.glob("prestadas.json.tmp-*"))


def test_checkpoint_rejects_non_contiguous_pages(tmp_path: Path) -> None:
    ctx = _ctx()
    checkpoint_path = tmp_path / "prestadas.json"
    checkpoint_path.write_text(
        json.dumps(
            {
                "version": 1,
                "flow": "notas",
                "tipo": "prestadas",
                "cnpj": ctx.cnpj,
                "mes": ctx.mes,
                "pages": [{"page": 2}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(FlowError) as exc:
        _load_checkpoint(str(checkpoint_path), "prestadas", ctx)

    assert exc.value.code == "NFSE_CHECKPOINT_NON_CONTIGUOUS"


def test_pagination_end_requires_positive_evidence() -> None:
    assert (
        _classify_pagination_end(
            {
                "next_enabled": True,
                "forward_disabled": False,
                "has_paginator": True,
                "active_page": 3,
                "visible_pages": [1, 2, 3, 4],
            },
            current_page=3,
            rows=10,
        )
        == ""
    )

    assert (
        _classify_pagination_end(
            {
                "next_enabled": False,
                "forward_disabled": True,
                "has_paginator": True,
                "active_page": 25,
                "visible_pages": [21, 22, 23, 24, 25],
            },
            current_page=25,
            rows=10,
        )
        == "forward_control_disabled"
    )

    assert (
        _classify_pagination_end(
            {
                "next_enabled": False,
                "forward_disabled": False,
                "has_paginator": False,
                "active_page": None,
                "visible_pages": [],
            },
            current_page=1,
            rows=10,
        )
        == "single_page_without_paginator"
    )

    assert (
        _classify_pagination_end(
            {
                "next_enabled": False,
                "forward_disabled": False,
                "has_paginator": True,
                "active_page": 25,
                "visible_pages": [21, 22, 23, 24, 25],
            },
            current_page=25,
            rows=2,
        )
        == "short_final_page"
    )


def test_stale_partials_are_removed_but_valid_xml_is_preserved(tmp_path: Path) -> None:
    partial = tmp_path / "prestadas_lote001.xml.part"
    valid = tmp_path / "prestadas_lote001.xml"
    partial.write_text("partial", encoding="utf-8")
    valid.write_text("valid", encoding="utf-8")

    removed = _cleanup_stale_partial_files(str(tmp_path), "prestadas")

    assert removed == 1
    assert not partial.exists()
    assert valid.exists()


def test_stale_partials_from_prior_attempt_are_removed(tmp_path: Path) -> None:
    current = tmp_path / "tentativa_2" / "notas" / "prestadas" / "47 - EMPRESA"
    prior = tmp_path / "tentativa_1" / "notas" / "prestadas" / "47 - EMPRESA"
    checkpoint_dir = tmp_path / "_checkpoints" / "notas" / "11415798000159" / "06-2026"
    current.mkdir(parents=True)
    prior.mkdir(parents=True)
    checkpoint_dir.mkdir(parents=True)

    prior_partial = prior / "prestadas_lote005.xml.part"
    current_partial = current / "prestadas_lote006.xml.part"
    prior_partial.write_text("partial", encoding="utf-8")
    current_partial.write_text("partial", encoding="utf-8")

    removed = _cleanup_stale_partial_files(
        str(current),
        "prestadas",
        str(checkpoint_dir),
    )

    assert removed == 2
    assert not prior_partial.exists()
    assert not current_partial.exists()
