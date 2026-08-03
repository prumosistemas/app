from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_portal_operational_credentials_reject_autofill() -> None:
    source = (ROOT / "portal-nacional.html").read_text(encoding="utf-8")
    assert 'id="certForm" class="stack" autocomplete="off"' in source
    assert 'id="certAlias"' in source and 'id="certPassword"' in source
    assert source.count('readonly data-lpignore="true" data-1p-ignore data-form-type="other"') >= 2
    assert source.count('guardOperationalCredentials("certAlias", "certPassword")') >= 2


def test_portal_never_renders_pfx_filename() -> None:
    source = (ROOT / "portal-nacional.html").read_text(encoding="utf-8")
    assert 'cert.filename || cert.subject' not in source
    assert 'textContent = file.name' not in source
    assert 'files[0]?.name' not in source
    assert source.count('"Arquivo selecionado"') >= 2


def test_advanced_editor_state_is_saved_and_restored_in_both_iss_views() -> None:
    for name in ("iss-fortaleza.html", "admin.html"):
        source = (ROOT / name).read_text(encoding="utf-8")
        assert "\n+function " not in source
        assert 'const ADVANCED_EDITOR_STATE_PREFIX="prumo:iss-editor:v1:"' in source
        assert "function captureAdvancedEditorState()" in source
        assert "sheet_name:" in source
        assert "map_cnpj:" in source
        assert "account_all_id:" in source
        assert "persistAdvancedEditorState(payload.dataset.id,editorState)" in source
        assert "restoreAdvancedEditorControls(savedState)" in source
        assert '$("mapContaTodos").addEventListener("change"' in source
        assert 'persistAdvancedEditorState(); $("xlsxAdvancedModal").classList.remove("open")' in source


def test_clearing_iss_editor_removes_the_saved_state() -> None:
    source = (ROOT / "iss-fortaleza.html").read_text(encoding="utf-8")
    clear_editor = source.split("function clearAdvancedXlsx(){", 1)[1].split("\n}", 1)[0]
    assert 'removeAdvancedEditorState($("datasetId").value||selectedDatasetId||"")' in clear_editor
