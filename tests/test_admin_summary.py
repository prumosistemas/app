import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
