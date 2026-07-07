from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from flow_notas import _chaves_nfse_xml, _contar_nfse_xml


def _write_nfse_xml(path: Path, codes: list[str]) -> None:
    root = ET.Element("NFSES")
    for index, code in enumerate(codes, start=1):
        wrapper = ET.SubElement(root, "Nfse")
        nested = ET.SubElement(wrapper, "Nfse")
        ET.SubElement(nested, "Numero").text = str(index)
        ET.SubElement(nested, "CodigoVerificacao").text = code
        ET.SubElement(nested, "DataEmissao").text = "2026-06-01T10:00:00-03:00"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def test_count_uses_top_level_nfse_nodes_only(tmp_path: Path) -> None:
    xml_path = tmp_path / "notas.xml"
    _write_nfse_xml(xml_path, ["A", "B", "C"])

    assert _contar_nfse_xml(str(xml_path)) == 3
    assert len(_chaves_nfse_xml(str(xml_path))) == 3


def test_duplicate_verification_codes_are_detected(tmp_path: Path) -> None:
    xml_path = tmp_path / "duplicadas.xml"
    _write_nfse_xml(xml_path, ["A", "A"])

    assert _contar_nfse_xml(str(xml_path)) == 2
    assert len(_chaves_nfse_xml(str(xml_path))) == 1
