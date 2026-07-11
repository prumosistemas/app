import sys
import unittest
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from flow_errors import FlowError  # noqa: E402
from run_queue import execute_flow, is_safe_retryable_result  # noqa: E402


class AutoRetryPolicyTests(unittest.TestCase):
    def test_business_errors_are_not_safe_even_if_flagged_retryable(self):
        for code in ("CNPJ_INEXISTENTE", "MENSAGEM_NA_TELA", "LOGIN_ERROR"):
            self.assertFalse(is_safe_retryable_result({"retryable": True, "erro_code": code}))

    def test_known_transient_error_is_safe(self):
        self.assertTrue(is_safe_retryable_result({"retryable": True, "erro_code": "TIMEOUT"}))

    def test_cnpj_mismatch_is_retryable_because_portal_can_reuse_stale_grid(self):
        self.assertTrue(is_safe_retryable_result({"retryable": True, "erro_code": "CNPJ_MISMATCH"}))

    def test_non_retryable_error_is_not_safe(self):
        self.assertFalse(is_safe_retryable_result({"retryable": False, "erro_code": "TIMEOUT"}))

    def test_missing_account_credentials_have_specific_code(self):
        import asyncio

        with self.assertRaises(FlowError) as raised:
            asyncio.run(
                execute_flow(
                    item={"cnpj": "123", "flow_mode": "notas", "usuario": "", "senha": ""},
                    mes="06/2026",
                    run_key="scope:run",
                    attempt_run_dir=".",
                    run_log_file="logs.txt",
                )
            )
        self.assertEqual(raised.exception.code, "ACCOUNT_CREDENTIALS_MISSING")


if __name__ == "__main__":
    unittest.main()
