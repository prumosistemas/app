import sys
import unittest
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from run_queue import is_safe_retryable_result  # noqa: E402


class AutoRetryPolicyTests(unittest.TestCase):
    def test_business_errors_are_not_safe_even_if_flagged_retryable(self):
        for code in ("CNPJ_INEXISTENTE", "CNPJ_MISMATCH", "MENSAGEM_NA_TELA", "LOGIN_ERROR"):
            self.assertFalse(is_safe_retryable_result({"retryable": True, "erro_code": code}))

    def test_known_transient_error_is_safe(self):
        self.assertTrue(is_safe_retryable_result({"retryable": True, "erro_code": "TIMEOUT"}))

    def test_non_retryable_error_is_not_safe(self):
        self.assertFalse(is_safe_retryable_result({"retryable": False, "erro_code": "TIMEOUT"}))


if __name__ == "__main__":
    unittest.main()
