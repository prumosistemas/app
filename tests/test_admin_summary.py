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


if __name__ == "__main__":
    unittest.main()
