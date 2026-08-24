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
        "PORTAL_SOLVER_GLOBAL_STATE_FILE",
        tmp_path / "solver-global.json",
    )
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


def test_rebalance_never_skips_an_automatic_job_that_has_not_started_today(monkeypatch, tmp_path: Path) -> None:
    _configure_storage(monkeypatch, tmp_path)
    ctx = _ctx("empresa", "usuario")
    portal_nacional._save_automatic_state(
        ctx,
        {
            "jobs": [{
                "id": "cert-1",
                "cert_id": "cert-1",
                "enabled": True,
                "schedule_minute": 720,
                "last_started_at": "2026-08-20T12:00:00-03:00",
                "next_run_at": "2026-08-22T12:00:00-03:00",
            }]
        },
    )

    now = datetime(2026, 8, 21, 14, 0, tzinfo=portal_nacional.PORTAL_TIMEZONE)
    portal_nacional._rebalance_automatic_schedules(now)

    job = portal_nacional._load_automatic_state(ctx)["jobs"][0]
    assert portal_nacional._parse_portal_datetime(job["next_run_at"]) == now


def test_rebalance_keeps_next_day_after_job_started_today(monkeypatch, tmp_path: Path) -> None:
    _configure_storage(monkeypatch, tmp_path)
    ctx = _ctx("empresa", "usuario")
    portal_nacional._save_automatic_state(
        ctx,
        {
            "jobs": [{
                "id": "cert-1",
                "cert_id": "cert-1",
                "enabled": True,
                "schedule_minute": 720,
                "last_started_at": "2026-08-21T08:30:00-03:00",
                "next_run_at": "2026-08-22T12:00:00-03:00",
            }]
        },
    )

    now = datetime(2026, 8, 21, 14, 0, tzinfo=portal_nacional.PORTAL_TIMEZONE)
    portal_nacional._rebalance_automatic_schedules(now)

    next_run = portal_nacional._parse_portal_datetime(
        portal_nacional._load_automatic_state(ctx)["jobs"][0]["next_run_at"]
    )
    assert next_run and next_run.date() == date(2026, 8, 22)


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
    assert "function renderAutomaticRuns(state)" in source
    assert 'data-auto-company="${esc(job.id)}"' in source
    assert 'data-download-auto-filtered' in source
    assert 'data-auto-competence' in source
    assert 'automatic/download?${query}' in source
    assert "renderAutomaticRuns(state)" in source
    assert 'switchSection("automaticSection"); setMsg("ok", "Captura automática iniciada.' in source
    assert 'Captura em andamento' in source
    assert 'if(activeRunId)' in source
    assert 'activeRunId||job.last_status==="rodando"' not in source
    assert "A captura terminou com pendências. O próximo ciclo retomará o período não confirmado." not in source
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


def test_scheduler_resumes_orphaned_automatic_checkpoint(monkeypatch, tmp_path: Path) -> None:
    _configure_storage(monkeypatch, tmp_path)
    ctx = _ctx("empresa", "usuario")
    run_ids = ["run-recebidas", "run-emitidas"]
    for run_id, status in zip(run_ids, ("rodando", "criada")):
        run_dir = portal_nacional._runs_root(ctx) / run_id
        run_dir.mkdir(parents=True)
        portal_nacional._save_json(run_dir / "run.json", {"run_id": run_id, "status": status, "config": {"automatic": True}})
    portal_nacional._save_automatic_state(ctx, {
        "jobs": [{"id": "cert-1", "cert_id": "cert-1", "enabled": True, "last_run_ids": run_ids}]
    })
    started = {}
    monkeypatch.setattr(portal_nacional, "_automatic_records", lambda: [
        (ctx, portal_nacional._load_automatic_state(ctx), portal_nacional._load_automatic_state(ctx)["jobs"][0])
    ])
    monkeypatch.setattr(portal_nacional, "_rebalance_automatic_schedules", lambda now=None: None)
    monkeypatch.setattr(portal_nacional, "_cleanup_automatic_runs", lambda *args, **kwargs: 0)
    monkeypatch.setattr(portal_nacional, "_any_portal_runtime_active", lambda: False)
    monkeypatch.setattr(portal_nacional, "_start_jobs", lambda _ctx, dirs, retry_only=False: started.update({"dirs": dirs, "retry_only": retry_only}))

    result = portal_nacional._run_automatic_scheduler_cycle(
        datetime(2026, 8, 11, 18, 0, tzinfo=portal_nacional.PORTAL_TIMEZONE)
    )

    assert result["reason"] == "checkpoint_resume"
    assert [path.name for path in started["dirs"]] == run_ids
    assert started["retry_only"] is True


def test_solver_outage_deferral_is_enabled_only_for_automatic_runs(tmp_path: Path) -> None:
    cfg = {
        "modo": "recebidas",
        "tipo_download": "ambos",
        "cert_index": 0,
        "concorrencia": 4,
        "retries": 8,
        "data_inicial": "01/08/2026",
        "data_final": "22/08/2026",
    }

    manual = portal_nacional._build_command(cfg, tmp_path / "manual", retry_only=True)
    automatic = portal_nacional._build_command(
        {**cfg, "automatic": True},
        tmp_path / "automatic",
        retry_only=True,
    )

    assert "--defer-solver-outage" not in manual
    assert "--defer-solver-outage" in automatic


def test_scheduler_starts_fresh_due_job_before_deferred_retry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_storage(monkeypatch, tmp_path)
    waiting_ctx = _ctx("empresa", "usuario-esperando")
    due_ctx = _ctx("empresa", "usuario-novo")
    waiting_dir = portal_nacional._runs_root(waiting_ctx) / "run-waiting"
    waiting_dir.mkdir(parents=True)
    portal_nacional._save_json(
        waiting_dir / "run.json",
        {
            "run_id": waiting_dir.name,
            "status": "aguardando_solver",
            "solver_retry_at": "2026-08-11T17:45:00-03:00",
            "config": {"automatic": True},
        },
    )
    waiting_state = {
        "jobs": [
            {
                "id": "waiting",
                "last_run_ids": [waiting_dir.name],
                "next_run_at": "2026-08-12T09:00:00-03:00",
            }
        ]
    }
    due_state = {
        "jobs": [
            {
                "id": "due",
                "enabled": True,
                "next_run_at": "2026-08-11T17:50:00-03:00",
            }
        ]
    }
    records = [
        (waiting_ctx, waiting_state, waiting_state["jobs"][0]),
        (due_ctx, due_state, due_state["jobs"][0]),
    ]
    started = {}
    monkeypatch.setattr(portal_nacional, "_automatic_records", lambda: records)
    monkeypatch.setattr(portal_nacional, "_rebalance_automatic_schedules", lambda now=None: None)
    monkeypatch.setattr(portal_nacional, "_reconcile_automatic_state", lambda *_args: False)
    monkeypatch.setattr(portal_nacional, "_cleanup_automatic_runs", lambda *args, **kwargs: 0)
    monkeypatch.setattr(portal_nacional, "_any_portal_runtime_active", lambda: False)
    monkeypatch.setattr(
        portal_nacional,
        "_start_jobs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("checkpoint deferido furou a fila diaria")
        ),
    )
    monkeypatch.setattr(
        portal_nacional,
        "_start_automatic_job",
        lambda ctx, job, reason: started.update(
            {"ctx": ctx, "job": job, "reason": reason}
        )
        or [tmp_path / "fresh-run"],
    )
    monkeypatch.setattr(
        portal_nacional,
        "_record_automatic_started",
        lambda _ctx, _state, job, run_dirs, **_kwargs: job.update(
            {"last_run_ids": [path.name for path in run_dirs]}
        ),
    )

    result = portal_nacional._run_automatic_scheduler_cycle(
        datetime(2026, 8, 11, 18, 0, tzinfo=portal_nacional.PORTAL_TIMEZONE)
    )

    assert result["started"] is True
    assert result["job_id"] == "due"
    assert started["ctx"] == due_ctx
    assert started["reason"] == "schedule"


def test_scheduler_skips_deferred_solver_run_until_retry_time(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_storage(monkeypatch, tmp_path)
    ctx = _ctx("empresa", "usuario")
    run_dir = portal_nacional._runs_root(ctx) / "run-waiting"
    run_dir.mkdir(parents=True)
    portal_nacional._save_json(
        run_dir / "run.json",
        {
            "run_id": run_dir.name,
            "status": "aguardando_solver",
            "solver_retry_at": "2026-08-11T18:15:00-03:00",
            "config": {"automatic": True},
        },
    )
    job = {"last_run_ids": [run_dir.name]}

    monkeypatch.setattr(
        portal_nacional,
        "_active_runtime",
        lambda _ctx: None,
    )
    monkeypatch.setattr(
        portal_nacional,
        "datetime",
        type(
            "FrozenDateTime",
            (datetime,),
            {
                "now": classmethod(
                    lambda cls, tz=None: datetime(
                        2026, 8, 11, 18, 0, tzinfo=portal_nacional.PORTAL_TIMEZONE
                    )
                )
            },
        ),
    )

    assert portal_nacional._stale_automatic_run_dirs(ctx, job) == []


def test_sequence_worker_defers_entire_batch_on_solver_outage(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_storage(monkeypatch, tmp_path)
    ctx = _ctx("empresa", "usuario")
    run_dirs = []
    for name in ("run-recebidas", "run-emitidas"):
        run_dir = portal_nacional._runs_root(ctx) / name
        run_dir.mkdir(parents=True)
        portal_nacional._save_json(
            run_dir / "run.json",
            {"run_id": name, "status": "criada", "config": {"automatic": True}},
        )
        run_dirs.append(run_dir)

    monkeypatch.setattr(portal_nacional, "_run_process", lambda *_args: 75)
    scope = "empresa:usuario"
    portal_nacional._RUNTIME[scope] = {"stop_requested": False}
    portal_nacional._sequence_worker(scope, run_dirs, retry_only=True)

    runs = [portal_nacional._load_json(path / "run.json", {}) for path in run_dirs]
    assert [run["status"] for run in runs] == [
        "aguardando_solver",
        "aguardando_solver",
    ]
    assert all(run.get("solver_retry_at") for run in runs)


def test_automatic_accumulated_download_deduplicates_and_honors_cutoff(monkeypatch, tmp_path: Path) -> None:
    _configure_storage(monkeypatch, tmp_path)
    ctx = _ctx("empresa", "usuario")
    for run_id, created, names in (
        ("run-old", "2026-08-09T10:00:00-03:00", ["nota-1.xml"]),
        ("run-new", "2026-08-10T10:00:00-03:00", ["nota-1.xml", "nota-2.xml"]),
    ):
        run_dir = portal_nacional._runs_root(ctx) / run_id
        downloads = run_dir / "downloads"
        downloads.mkdir(parents=True)
        items = {}
        for index, name in enumerate(names):
            file_path = downloads / name
            file_path.write_text(f"<NFSe>{run_id}-{index}</NFSe>", encoding="utf-8")
            items[f"item-{index}"] = {"status": "baixado", "competencia": "2026-08", "files": [str(file_path)]}
        portal_nacional._save_json(
            run_dir / "run.json",
            {"created_at": created, "config": {"automatic": True, "automatic_job_id": "cert-1", "modo": "recebidas"}},
        )
        portal_nacional._save_json(run_dir / "indice.json", {"items": items})

    all_entries, all_runs = portal_nacional._automatic_accumulated_entries(ctx, "cert-1")
    old_entries, old_runs = portal_nacional._automatic_accumulated_entries(ctx, "cert-1", date(2026, 8, 9))

    assert sorted(entry["path"].name for entry in all_entries) == ["nota-1.xml", "nota-2.xml"]
    assert [path.name for path in all_runs] == ["run-old", "run-new"]
    assert [entry["path"].name for entry in old_entries] == ["nota-1.xml"]
    assert [path.name for path in old_runs] == ["run-old"]


def test_automatic_capture_history_keeps_errors_and_counts_only_new_notes(monkeypatch, tmp_path: Path) -> None:
    _configure_storage(monkeypatch, tmp_path)
    ctx = _ctx("empresa", "usuario")
    for run_id, created, status, item_ids in (
        ("20260810-recebidas-x", "2026-08-10T10:00:00-03:00", "finalizado", ["n1"]),
        ("20260811-recebidas-x", "2026-08-11T10:00:00-03:00", "finalizado_com_erros", ["n1", "n2"]),
    ):
        run_dir = portal_nacional._runs_root(ctx) / run_id
        downloads = run_dir / "downloads"
        downloads.mkdir(parents=True)
        items = {}
        for item_id in item_ids:
            path = downloads / f"{item_id}.xml"
            path.write_text("<NFSe><dCompet>2026-08-01</dCompet></NFSe>", encoding="utf-8")
            items[item_id] = {"status": "baixado", "files_by_tipo": {"xml": str(path)}}
        portal_nacional._save_json(run_dir / "run.json", {
            "run_id": run_id,
            "created_at": created,
            "updated_at": created,
            "status": status,
            "config": {"automatic": True, "automatic_job_id": "cert-1", "modo": "recebidas", "certificate_alias": "SIM7"},
        })
        portal_nacional._save_json(run_dir / "indice.json", {"items": items})

    history = portal_nacional._automatic_capture_history(ctx)

    assert [capture["status"] for capture in history] == ["finalizado_com_erros", "finalizado"]
    assert history[0]["new_notes"] == 1
    assert history[0]["total_accumulated"] == 2
    assert history[1]["new_notes"] == 1


def test_automatic_history_does_not_call_stale_created_child_running(monkeypatch, tmp_path: Path) -> None:
    _configure_storage(monkeypatch, tmp_path)
    ctx = _ctx("empresa", "usuario")
    for modo, status in (("recebidas", "interrompida"), ("emitidas", "criada")):
        run_id = f"20260810-120000-{modo}-x"
        run_dir = portal_nacional._runs_root(ctx) / run_id
        run_dir.mkdir(parents=True)
        portal_nacional._save_json(run_dir / "run.json", {
            "run_id": run_id,
            "created_at": "2026-08-10T12:00:00-03:00",
            "updated_at": "2026-08-10T12:01:00-03:00",
            "status": status,
            "config": {"automatic": True, "automatic_job_id": "cert-1", "modo": modo},
        })

    history = portal_nacional._automatic_capture_history(ctx)

    assert history[0]["status"] == "finalizado_com_erros"


def test_automatic_job_progress_reads_live_index(monkeypatch, tmp_path: Path) -> None:
    _configure_storage(monkeypatch, tmp_path)
    ctx = _ctx("empresa", "usuario")
    run_dir = portal_nacional._runs_root(ctx) / "run-live"
    run_dir.mkdir(parents=True)
    portal_nacional._save_json(
        run_dir / "run.json",
        {"status": "rodando", "heartbeat_at": "2026-08-12T09:30:00", "updated_at": "2026-08-12T09:30:00"},
    )
    portal_nacional._save_json(
        run_dir / "indice.json",
        {
            "items": {
                "ok": {"status": "baixado"},
                "wait": {"status": "pendente"},
            },
            "events": [{"event": "item_waiting_solver"}],
        },
    )

    progress = portal_nacional._automatic_job_progress(ctx, {"last_run_ids": ["run-live"]})

    assert progress["baixados"] == 1
    assert progress["pendentes"] == 1
    assert progress["heartbeat_at"] == "2026-08-12T09:30:00"
    assert progress["last_event"]["event"] == "item_waiting_solver"


def test_portal_runtime_metrics_reports_process_and_scheduler(monkeypatch) -> None:
    class AliveThread:
        @staticmethod
        def is_alive() -> bool:
            return True

    class AliveProcess:
        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(portal_nacional, "_AUTOMATIC_THREAD", AliveThread())
    portal_nacional._RUNTIME.clear()
    portal_nacional._RUNTIME["empresa:usuario"] = {
        "thread": AliveThread(),
        "process": AliveProcess(),
        "run_id": "run-live",
        "started_at": "2026-08-12T09:00:00",
        "heartbeat_at": "2026-08-12T09:30:00",
    }
    try:
        metrics = portal_nacional.portal_runtime_metrics()
    finally:
        portal_nacional._RUNTIME.clear()

    assert metrics["active"] == 1
    assert metrics["runs"][0]["process_alive"] is True
    assert metrics["automatic_scheduler_alive"] is True
