from datetime import date, datetime
from pathlib import Path
import sys
from types import SimpleNamespace


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import portal_nacional  # noqa: E402


def _configure_storage(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(portal_nacional, "OUTPUT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        portal_nacional,
        "member_output_root",
        lambda ctx: str(tmp_path / "empresas" / ctx.company_id / "colaboradores" / ctx.user_id),
    )


def _ctx(company: str, user: str):
    return SimpleNamespace(company_id=company, user_id=user)


def test_automatic_schedules_are_evenly_spread_across_day(monkeypatch, tmp_path: Path) -> None:
    _configure_storage(monkeypatch, tmp_path)
    for index in range(3):
        ctx = _ctx(f"empresa-{index}", f"usuario-{index}")
        portal_nacional._save_automatic_state(
            ctx,
            {"jobs": [{"id": f"cert-{index}", "cert_id": f"cert-{index}", "enabled": True}]},
        )

    now = datetime(2026, 8, 10, 8, 0, tzinfo=portal_nacional.PORTAL_TIMEZONE)
    portal_nacional._rebalance_automatic_schedules(now)

    minutes = []
    for index in range(3):
        state = portal_nacional._load_automatic_state(_ctx(f"empresa-{index}", f"usuario-{index}"))
        minutes.append(state["jobs"][0]["schedule_minute"])
        assert state["jobs"][0]["next_run_at"]
    assert minutes == [0, 480, 960]


def test_automatic_capture_starts_with_four_month_window_then_overlaps() -> None:
    assert portal_nacional._automatic_capture_range({}, date(2026, 8, 10)) == ("09/04/2026", "10/08/2026")
    assert portal_nacional._automatic_capture_range(
        {"data_inicial": "2026-06-01"},
        date(2026, 8, 10),
    ) == ("01/06/2026", "10/08/2026")
    assert portal_nacional._automatic_capture_range(
        {"last_success_date": "2026-08-01"},
        date(2026, 8, 10),
    ) == ("30/07/2026", "10/08/2026")


def test_retention_deletes_only_old_automatic_runs(monkeypatch, tmp_path: Path) -> None:
    _configure_storage(monkeypatch, tmp_path)
    ctx = _ctx("empresa", "usuario")
    runs = portal_nacional._runs_root(ctx)
    old_auto = runs / "old-auto"
    old_manual = runs / "old-manual"
    recent_auto = runs / "recent-auto"
    for folder, automatic, created in (
        (old_auto, True, "2026-01-01T10:00:00-03:00"),
        (old_manual, False, "2026-01-01T10:00:00-03:00"),
        (recent_auto, True, "2026-08-01T10:00:00-03:00"),
    ):
        folder.mkdir(parents=True)
        portal_nacional._save_json(
            folder / "run.json",
            {"created_at": created, "config": {"automatic": automatic}},
        )

    removed = portal_nacional._cleanup_automatic_runs(
        ctx,
        now=datetime(2026, 8, 10, 12, 0, tzinfo=portal_nacional.PORTAL_TIMEZONE),
    )

    assert removed == 1
    assert not old_auto.exists()
    assert old_manual.exists()
    assert recent_auto.exists()


def test_portal_ui_has_automatic_tab_back_button_and_contextual_stop() -> None:
    source = (Path(__file__).resolve().parents[1] / "portal-nacional.html").read_text(encoding="utf-8")
    assert 'id="btnBack" href="/"' in source
    assert 'id="btnLogout"' not in source
    assert 'id="btnRefresh"' not in source
    assert 'data-section="automaticSection"' in source
    assert 'id="btnStop"' in source
    assert source.index('id="btnStop"') < source.index('id="btnDeleteRun"')
    assert 'id="automaticDataInicial"' in source
    assert 'id="automaticEnabled"' not in source
    assert 'id="automaticTipo"' not in source
    assert "Capturar agora" in source
    assert 'data-edit-cert="' in source
    assert "function manualPortalRuns(runs)" in source
    assert "function automaticPortalRuns(runs)" in source
    assert 'id="automaticRunsTable"' in source
    assert 'id="automaticRunsCount"' in source
    assert "function renderAutomaticRuns(runs)" in source
    assert 'data-download-auto-run="${esc(run.run_id)}"' in source
    assert "renderAutomaticRuns(state.runs)" in source
    assert 'switchSection("automaticSection"); setMsg("ok", "Captura automática iniciada.' in source
    assert source.count("groupRuns(manualPortalRuns(state.runs))") == 2
    assert "!run?.config?.automatic" in source

    worker = (Path(__file__).resolve().parents[1] / "cloudflare" / "worker.js").read_text(encoding="utf-8")
    assert 'htmlResponse(portalNacionalHtml, 200, "no-store")' in worker


def test_run_list_can_skip_recursive_file_scan(monkeypatch, tmp_path: Path) -> None:
    _configure_storage(monkeypatch, tmp_path)
    ctx = _ctx("empresa", "usuario")
    run_dir = portal_nacional._runs_root(ctx) / "run-1"
    (run_dir / "downloads").mkdir(parents=True)
    portal_nacional._save_json(run_dir / "run.json", {"status": "finalizado"})
    (run_dir / "downloads" / "nota.xml").write_text("<NFSe/>", encoding="utf-8")

    summary = portal_nacional._compact_run(ctx, run_dir, include_files=False)
    detail = portal_nacional._compact_run(ctx, run_dir)

    assert "files" not in summary
    assert any(item["name"] == "nota.xml" for item in detail["files"])


def test_scheduler_waits_while_any_portal_run_is_active(monkeypatch, tmp_path: Path) -> None:
    _configure_storage(monkeypatch, tmp_path)
    ctx = _ctx("empresa", "usuario")
    portal_nacional._save_automatic_state(
        ctx,
        {
            "jobs": [
                {
                    "id": "cert-1",
                    "cert_id": "cert-1",
                    "enabled": True,
                    "next_run_at": "2026-08-10T07:00:00-03:00",
                }
            ]
        },
    )
    monkeypatch.setattr(portal_nacional, "_any_portal_runtime_active", lambda: True)

    result = portal_nacional._run_automatic_scheduler_cycle(
        datetime(2026, 8, 10, 8, 0, tzinfo=portal_nacional.PORTAL_TIMEZONE)
    )

    assert result == {"started": False, "reason": "portal_busy"}
