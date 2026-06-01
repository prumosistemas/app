import importlib.util
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("monitor_agent", ROOT / "deploy" / "monitor_agent.py")
monitor_agent = importlib.util.module_from_spec(SPEC)
sys.modules["monitor_agent"] = monitor_agent
SPEC.loader.exec_module(monitor_agent)


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

    def test_sample_severity_prefers_short_lived_oom_peak(self):
        normal = {"host": {"cpu_percent": 99, "memory_percent": 80}, "errors": {"total": 0}}
        oom = {"host": {"cpu_percent": 20, "memory_percent": 40}, "errors": {"oom": 1, "total": 1}}

        self.assertGreater(monitor_agent.sample_severity(oom), monitor_agent.sample_severity(normal))

    def test_log_errors_classifies_oom_and_masks_tokens(self):
        def fake_run(command, **_kwargs):
            if command[0] == "docker":
                output = "429 Too Many Requests token=segredo\n[ITEM_ERROR] flow=notas\n"
            else:
                output = "kernel: Out of memory: Killed process 123\n"
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

        with patch.object(monitor_agent.subprocess, "run", side_effect=fake_run):
            errors = monitor_agent.log_errors()

        self.assertGreater(errors["oom"], 0)
        self.assertGreater(errors["killed"], 0)
        self.assertGreater(errors["browser_connect"], 0)
        self.assertNotIn("segredo", "\n".join(errors["recent_events"]))


if __name__ == "__main__":
    unittest.main()
