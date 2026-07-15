import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import main  # noqa: E402


class AdminSummaryTests(unittest.TestCase):
    def test_summary_counts_unique_cnpjs_items_and_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            db_file = output / "iss.db"
            company = "company_test"
            user = "user_test"
            folder = output / "empresas" / company / "colaboradores" / user / "runs"
            folder.mkdir(parents=True)
            (folder / "logs.txt").write_text("ok", encoding="utf-8")

            datasets = {
                "datasets": [
                    {"id": "a", "alias": "A", "items": [{"cnpj_digits": "11111111000191"}, {"cnpj_digits": "22222222000191"}]},
                    {"id": "b", "alias": "B", "items": [{"cnpj_digits": "11111111000191"}]},
                ]
            }
            conn = sqlite3.connect(db_file)
            try:
                conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL)")
                conn.execute(
                    "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, 1)",
                    (f"empresa:{company}:membro:{user}:datasets", json.dumps(datasets)),
                )
                conn.commit()
            finally:
                conn.close()

            def connect():
                connection = sqlite3.connect(db_file)
                connection.row_factory = sqlite3.Row
                return connection

            with patch.object(main, "OUTPUT_ROOT", str(output)), patch.object(main, "db_connect", connect):
                summary = main.build_company_admin_summary(company)

            self.assertEqual(summary["totals"]["cnpjs_unique"], 2)
            self.assertEqual(summary["totals"]["items"], 3)
            self.assertEqual(summary["totals"]["datasets"], 2)
            self.assertTrue(summary["storage"]["healthy"])
            self.assertEqual(summary["storage"]["files"], 1)

    def test_monitor_downsample_preserves_peak(self):
        samples = [
            {"ts": index, "host": {"cpu_percent": 10}, "runtime": {"queue": {}}, "errors": {}, "containers": {}}
            for index in range(100)
        ]
        samples[47]["host"]["cpu_percent"] = 99

        reduced = main._downsample_monitor_metrics(samples, max_points=10)

        self.assertLessEqual(len(reduced), 11)
        self.assertIn(99, [item["host"]["cpu_percent"] for item in reduced])

    def test_portal_modal_endpoints_skip_local_residential_fallback(self):
        env = {
            "PORTAL_NACIONAL_SOLVER_URL": "https://principal.example/solve",
            "PORTAL_NACIONAL_SOLVER_FALLBACK_URLS": (
                "https://secundario.example/solve,http://127.0.0.1:8876/solve"
            ),
        }
        with patch.dict("os.environ", env, clear=False):
            self.assertEqual(
                main._portal_modal_endpoints(),
                (
                    "https://principal.example/solve",
                    "https://secundario.example/solve",
                ),
            )

    def test_solver_runtime_status_accepts_string_output_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(main, "OUTPUT_ROOT", temporary), patch.dict("os.environ", {}, clear=True):
                self.assertEqual(main._portal_solver_runtime_status(), {})

    def test_modal_billing_uses_workspace_api(self):
        billing = SimpleNamespace(
            report=lambda **_kwargs: [
                SimpleNamespace(
                    object_id="ap-test",
                    description="prumo-portal-nacional-google-solver",
                    environment_name="main",
                    interval_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
                    cost=Decimal("1.25"),
                )
            ]
        )
        workspace = SimpleNamespace(billing=billing)
        with patch("modal.Client.from_credentials", return_value=object()), patch(
            "modal.Workspace.from_context", return_value=workspace
        ) as from_context:
            snapshot = main._modal_billing_account_snapshot(
                role="primary",
                label="Principal",
                workspace="workspace-test",
                endpoint="https://workspace-test--app.modal.run/solve",
                token_id="token-id",
                token_secret="token-secret",
                monthly_credit=Decimal("30"),
                target_app="prumo-portal-nacional-google-solver",
                month_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
                now_dt=datetime(2026, 7, 14, tzinfo=timezone.utc),
                active_host="workspace-test--app.modal.run",
            )

        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["source"], "modal.Workspace.billing.report")
        self.assertEqual(snapshot["month_to_date_cost_usd"], 1.25)
        self.assertEqual(snapshot["credits_remaining_usd"], 28.75)
        from_context.assert_called_once()


if __name__ == "__main__":
    unittest.main()
