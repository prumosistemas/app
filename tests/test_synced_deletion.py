import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import main  # noqa: E402


class SyncedDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_cleanup_removes_kv_memory_and_folder_when_idle(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            db_file = output / "iss.db"
            company = "company_test"
            user = "user_test"
            folder = output / "empresas" / company / "colaboradores" / user / "runs"
            folder.mkdir(parents=True)
            (folder / "logs.txt").write_text("ok", encoding="utf-8")
            self._create_db(db_file, f"empresa:{company}:membro:{user}:datasets")

            with self._patch_runtime(output, db_file), \
                 patch.object(main, "_request_stop_for_ctx", new=AsyncMock(return_value=[])), \
                 patch.object(main.GLOBAL_QUEUE, "remove_owner", new=AsyncMock(return_value=2)), \
                 patch.object(main, "active_jobs_for_scope", return_value=0):
                main.RUNS[f"{company}:{user}:run"] = {"scope_id": f"{company}:{user}"}
                result = await main._delete_member_data(company, user)

            self.assertTrue(result["completed"])
            self.assertEqual(result["removed_queued_groups"], 2)
            self.assertFalse((output / "empresas" / company / "colaboradores" / user).exists())
            self.assertEqual(self._count_rows(db_file), 0)

    async def test_member_cleanup_waits_while_worker_is_active(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            db_file = output / "iss.db"
            self._create_db(db_file, "empresa:company_test:membro:user_test:datasets")

            with self._patch_runtime(output, db_file), \
                 patch.object(main, "_request_stop_for_ctx", new=AsyncMock(return_value=[])), \
                 patch.object(main.GLOBAL_QUEUE, "remove_owner", new=AsyncMock(return_value=0)), \
                 patch.object(main, "active_jobs_for_scope", return_value=1):
                result = await main._delete_member_data("company_test", "user_test")

            self.assertFalse(result["completed"])
            self.assertEqual(result["status"], "stopping")
            self.assertEqual(self._count_rows(db_file), 1)

    async def test_company_cleanup_removes_all_member_data_when_idle(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            db_file = output / "iss.db"
            company = "company_test"
            folder = output / "empresas" / company / "colaboradores" / "user_test" / "runs"
            folder.mkdir(parents=True)
            (folder / "logs.txt").write_text("ok", encoding="utf-8")
            self._create_db(db_file, f"empresa:{company}:membro:user_test:datasets")
            self._insert_kv(db_file, f"empresa:{company}:membro:other_user:runs")
            self._insert_kv(db_file, "empresa:other_company:membro:user_test:datasets")

            with self._patch_runtime(output, db_file), \
                 patch.object(main.GLOBAL_QUEUE, "remove_company", new=AsyncMock(return_value=3)), \
                 patch.object(main, "active_jobs_for_company", return_value=0):
                main.RUNS[f"{company}:user_test:run"] = {"company_id": company}
                result = await main._delete_company_data(company)

            self.assertTrue(result["completed"])
            self.assertEqual(result["removed_queued_groups"], 3)
            self.assertFalse((output / "empresas" / company).exists())
            self.assertEqual(self._count_rows(db_file), 1)

    def _patch_runtime(self, output: Path, db_file: Path):
        def connect():
            connection = sqlite3.connect(db_file)
            connection.row_factory = sqlite3.Row
            return connection

        main.RUNS.clear()
        return patch.multiple(main, OUTPUT_ROOT=str(output), db_connect=connect)

    @staticmethod
    def _create_db(db_file: Path, key: str):
        connection = sqlite3.connect(db_file)
        try:
            connection.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL)")
            connection.execute("INSERT INTO kv (key, value, updated_at) VALUES (?, '{}', 1)", (key,))
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _insert_kv(db_file: Path, key: str):
        connection = sqlite3.connect(db_file)
        try:
            connection.execute("INSERT INTO kv (key, value, updated_at) VALUES (?, '{}', 1)", (key,))
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _count_rows(db_file: Path) -> int:
        connection = sqlite3.connect(db_file)
        try:
            return int(connection.execute("SELECT COUNT(*) FROM kv").fetchone()[0])
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
