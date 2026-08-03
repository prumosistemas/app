from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_operational_iss_credentials_are_not_browser_autofill_targets() -> None:
    for name in ("iss-fortaleza.html", "admin.html"):
        source = _source(name)
        assert 'id="accountUsuario"' in source
        assert 'id="accountSenha"' in source
        assert 'autocomplete="off" readonly' in source
        assert 'data-lpignore="true"' in source
        assert "guardOperationalCredentials(\"accountUsuario\",\"accountSenha\")" in source
        assert "disableOperationalAutofill(tr);" in source


def test_xlsx_import_prefers_raw_numeric_cell_when_display_is_scientific() -> None:
    for name in ("iss-fortaleza.html", "admin.html"):
        source = _source(name)
        assert "function worksheetRowsForImport(ws)" in source
        assert "raw:false" in source
        assert "raw:true" in source
        assert "typeof rawValue===\"number\"" in source
        assert "advancedXlsxRows=worksheetRowsForImport(ws);" in source
        assert "const rows=worksheetRowsForImport(sheet);" in source
        assert "normalizeImportedCnpj(row[Number(idxCnpj)]" in source


def test_scientific_cnpj_text_expands_to_plain_safe_integer() -> None:
    source = _source("iss-fortaleza.html")
    match = re.search(
        r"function spreadsheetCellText\(value\)\{.*?\n\}",
        source,
        flags=re.DOTALL,
    )
    assert match is not None
    script = (
        match.group(0)
        + '\nprocess.stdout.write(JSON.stringify(['
        + 'spreadsheetCellText("1.45915E+13"),'
        + 'spreadsheetCellText(14591512345678),'
        + 'spreadsheetCellText("14.591.512/3456-78")'
        + ']));'
    )
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout == '["14591500000000","14591512345678","14.591.512/3456-78"]'


def test_worksheet_import_replaces_scientific_display_with_full_raw_value() -> None:
    source = _source("iss-fortaleza.html")
    match = re.search(
        r"function worksheetRowsForImport\(ws\)\{.*?\n\}",
        source,
        flags=re.DOTALL,
    )
    assert match is not None
    script = (
        "let calls=0;"
        "const XLSX={utils:{sheet_to_json:(_ws,options)=>{calls+=1;"
        "return options.raw?[[14591512345678,'0012']]:[['1.45915E+13','0012']];}}};"
        + match.group(0)
        + "\nprocess.stdout.write(JSON.stringify(worksheetRowsForImport({})));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout == '[[14591512345678,"0012"]]'
