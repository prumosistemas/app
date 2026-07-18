import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import portal_nacional  # noqa: E402
import portal_nacional_automation as automation  # noqa: E402


def test_portal_concurrency_is_server_controlled() -> None:
    cfg = portal_nacional._normalize_cfg(
        {
            "modo": "emitidas",
            "tipo_download": "ambos",
            "data_inicial": "01/06/2026",
            "data_final": "30/06/2026",
            "concorrencia": 16,
        }
    )
    assert cfg["concorrencia"] == portal_nacional.PORTAL_DOWNLOAD_CONCURRENCY == 4


def test_portal_member_roots_and_runtime_keys_are_isolated(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        portal_nacional,
        "member_output_root",
        lambda ctx: str(tmp_path / ctx.company_id / ctx.user_id),
    )
    alan = SimpleNamespace(company_id="empresa", user_id="alan")
    gabriel = SimpleNamespace(company_id="empresa", user_id="gabriel")

    alan_run = portal_nacional._runs_root(alan) / "run-alan"
    alan_run.mkdir(parents=True)
    (alan_run / "run.json").write_text("{}", encoding="utf-8")

    assert portal_nacional._runtime_key(alan) != portal_nacional._runtime_key(gabriel)
    assert portal_nacional._safe_run_dir(alan, "run-alan") == alan_run
    with pytest.raises(Exception):
        portal_nacional._safe_run_dir(gabriel, "run-alan")


def test_download_keeps_xml_checkpoint_when_pdf_solver_raises(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def fake_download(session, item, solver_url, download_dir, tipo):
        calls.append(tipo)
        if tipo == "xml":
            path = tmp_path / "nota.xml"
            path.write_text("<NFSe />", encoding="utf-8")
            return {"ok": True, "tipo": "xml", "file": str(path), "method": "captcha_xml"}
        raise RuntimeError("solver:visual_challenge_not_ready: indisponivel")

    monkeypatch.setattr(automation, "download_item_tipo_requests", fake_download)
    result = automation.download_item_requests(
        object(),
        {"id": "nota", "required_tipos": ["xml", "pdf"]},
        "https://solver.example/solve",
        tmp_path,
        "ambos",
    )

    assert not result["ok"]
    assert result["files_by_tipo"]["xml"].endswith("nota.xml")
    assert result["methods_by_tipo"]["xml"] == "captcha_xml"
    assert calls == ["xml", "pdf"]
