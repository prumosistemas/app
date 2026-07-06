import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import domain  # noqa: E402
import run_queue  # noqa: E402


class DamDependencyTests(unittest.TestCase):
    def setUp(self):
        domain.RUNS.clear()

    def test_retry_dam_is_blocked_when_root_escrituracao_failed(self):
        ctx = domain.WorkerContext(
            company_id="company",
            company_name="Empresa",
            user_id="user",
            user_email="user@example.com",
            user_role="member",
            via_worker=True,
        )
        cnpj = "57085356000192"
        escrituracao_task = {"cnpj": cnpj, "flow_mode": "escrituracao", "account_alias": "conta"}
        dam_task = {"cnpj": cnpj, "flow_mode": "dam", "account_alias": "conta"}

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            root_key = domain.local_run_key(ctx, "root_1")
            retry_key = domain.local_run_key(ctx, "attempt_2")
            domain.RUNS[root_key] = {
                "scope_id": domain.scope_id(ctx),
                "run_id": "root_1",
                "root_id": "root_1",
                "status": "finished",
                "created_at": 1,
                "input_tasks": [escrituracao_task, dam_task],
                "results": [
                    {
                        "cnpj": cnpj,
                        "flow_mode": "escrituracao",
                        "status": "erro",
                        "erro_code": "CERT_BUTTON_NOT_FOUND",
                    }
                ],
            }
            domain.RUNS[retry_key] = {
                "scope_id": domain.scope_id(ctx),
                "run_id": "attempt_2",
                "root_id": "root_1",
                "status": "running",
                "created_at": 2,
                "run_dir": str(run_dir),
                "input_tasks": [dam_task],
                "results": [],
            }

            with patch.object(domain, "save_runs_state", lambda _ctx: None):
                results = asyncio.run(
                    run_queue.run_cnpj_group_serial(
                        ctx,
                        items=[dam_task],
                        mes="06/2026",
                        run_key=retry_key,
                        attempt_run_dir=str(run_dir),
                        run_log_file=str(run_dir / "logs.txt"),
                    )
                )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "erro")
        self.assertEqual(results[0]["erro_code"], "DAM_BLOCKED_BY_ESCRITURACAO")
        self.assertEqual(domain.RUNS[retry_key]["results"][0]["erro_code"], "DAM_BLOCKED_BY_ESCRITURACAO")


if __name__ == "__main__":
    unittest.main()
