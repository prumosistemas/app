import sys
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

import monitor_agent  # noqa: E402


class MonitorAgentTests(unittest.TestCase):
    def test_parse_percent(self):
        self.assertEqual(monitor_agent.parse_percent("12.5%"), 12.5)

    def test_parse_bytes(self):
        self.assertEqual(monitor_agent.parse_bytes("1.5GiB / 12GiB"), 1610612736)
        self.assertEqual(monitor_agent.parse_bytes("800MiB / 2GiB"), 838860800)

    def test_persist_removes_samples_older_than_five_days(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_file = root / "metrics.sqlite3"
            latest_file = root / "latest.json"
            now = int(time.time())
            with patch.multiple(
                monitor_agent,
                OUTPUT_ROOT=root,
                DB_FILE=db_file,
                LATEST_FILE=latest_file,
            ):
                monitor_agent.init_db()
                connection = sqlite3.connect(db_file)
                try:
                    connection.execute(
                        "INSERT INTO metrics (ts, payload) VALUES (?, '{}')",
                        (now - monitor_agent.RETENTION_SECONDS - 1,),
                    )
                    connection.commit()
                finally:
                    connection.close()
                monitor_agent.persist({"ts": now, "host": {}})
                connection = sqlite3.connect(db_file)
                try:
                    rows = connection.execute("SELECT ts FROM metrics ORDER BY ts").fetchall()
                finally:
                    connection.close()

            self.assertEqual(rows, [(now,)])


if __name__ == "__main__":
    unittest.main()
