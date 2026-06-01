import asyncio
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import domain  # noqa: E402
import main  # noqa: E402


class RunArtifactTests(unittest.TestCase):
    def setUp(self):
        domain.RUNS.clear()

    def test_error_screenshots_are_hidden_from_file_listing(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            output = run_dir / "certidao" / "12345678000190 - EMPRESA" / "certidao"
            output.mkdir(parents=True)
            (output / "erro_20260601_145443.png").write_bytes(b"internal")
            (output / "12345678000190_certidao_iss.pdf").write_bytes(b"pdf")

            files = domain.list_run_files(str(run_dir), "attempt_1")

            self.assertEqual([item["name"] for item in files], ["12345678000190_certidao_iss.pdf"])

    def test_zip_uses_successful_attempt_and_ignores_failed_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx = self._ctx()
            with patch.object(domain, "OUTPUT_ROOT", str(root)):
                attempts_root = Path(domain.member_runs_root(ctx)) / "root_1"
                success_dir = attempts_root / "tentativa_1"
                failed_dir = attempts_root / "tentativa_2"
                success_file = success_dir / "certidao" / "12345678000190 - EMPRESA" / "certidao" / "certidao.pdf"
                failed_file = failed_dir / "certidao" / "12345678000190 - EMPRESA" / "certidao" / "erro_20260601_145443.png"
                success_file.parent.mkdir(parents=True)
                failed_file.parent.mkdir(parents=True)
                success_file.write_bytes(b"success")
                failed_file.write_bytes(b"internal")
                domain.RUNS["company:user:attempt_1"] = self._run("attempt_1", "root_1", success_dir, "ok")
                domain.RUNS["company:user:attempt_2"] = self._run("attempt_2", "root_1", failed_dir, "erro")

                zip_path = domain.create_root_zip(ctx, "root_1")
                with zipfile.ZipFile(zip_path) as archive:
                    names = archive.namelist()

            self.assertEqual(names, ["certidao/12345678000190 - EMPRESA/certidao/certidao.pdf"])

    def test_finished_run_logs_remain_available_without_crossing_flows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx = self._ctx()
            run_dir = root / "tentativa_1"
            run_dir.mkdir()
            (run_dir / "logs.txt").write_text(
                "[ITEM_START] flow=certidao cnpj=12345678000190 conta=a\n"
                "[ITEM_OK] flow=certidao cnpj=12345678000190 conta=a\n",
                encoding="utf-8",
            )
            domain.RUNS["company:user:attempt_1"] = self._run("attempt_1", "root_1", run_dir, "ok")

            certidao = asyncio.run(main.get_run_logs_tail(
                "root_1",
                cnpj="12345678000190",
                flow="certidao",
                attempt_run_id="attempt_1",
                ctx=ctx,
            ))
            dam = asyncio.run(main.get_run_logs_tail(
                "root_1",
                cnpj="12345678000190",
                flow="dam",
                attempt_run_id="attempt_1",
                ctx=ctx,
            ))

        self.assertIn("[ITEM_OK]", certidao["logs_by_attempt"][0]["logs"])
        self.assertEqual(certidao["logs_by_attempt"][0]["log_scope"], "cnpj_flow")
        self.assertEqual(dam["logs_by_attempt"], [])

    def test_notas_without_codigo_dominio_uses_company_folder_even_when_option_is_enabled(self):
        validation = {
            "valid": True,
            "items": [
                {"valid": True, "cnpj": "12.345.678/0001-90", "cnpj_digits": "12345678000190", "codigo_dominio": "", "nome_empresa": "Sem codigo", "account_id": "a"},
                {"valid": True, "cnpj": "98.765.432/0001-10", "cnpj_digits": "98765432000110", "codigo_dominio": "42", "nome_empresa": "Com codigo", "account_id": "a"},
            ],
        }
        flow_selection = {
            "12345678000190": {"notas": True},
            "98765432000110": {"notas": True},
        }
        with patch.object(domain, "validate_dataset_items", return_value=validation), \
             patch.object(domain, "hydrate_tasks_with_current_accounts", side_effect=lambda _ctx, tasks: tasks):
            tasks = domain.build_tasks_from_dataset(self._ctx(), {"items": []}, flow_selection, usar_codigo_dominio=True)

        self.assertFalse(tasks[0]["usar_codigo_dominio"])
        self.assertTrue(tasks[1]["usar_codigo_dominio"])

    @staticmethod
    def _ctx():
        return domain.WorkerContext(
            company_id="company",
            company_name="Empresa",
            user_id="user",
            user_email="user@example.com",
            user_role="member",
            via_worker=True,
        )

    @staticmethod
    def _run(run_id, root_id, run_dir, status):
        return {
            "scope_id": "company:user",
            "run_id": run_id,
            "root_id": root_id,
            "run_dir": str(run_dir),
            "status": "finished",
            "created_at": 1 if run_id == "attempt_1" else 2,
            "results": [{
                "cnpj": "12345678000190",
                "flow_mode": "certidao",
                "status": status,
            }],
        }


if __name__ == "__main__":
    unittest.main()
