import asyncio
import sys
import unittest
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from flow_errors import classify_exception  # noqa: E402


class FlowErrorClassificationTests(unittest.TestCase):
    def test_blank_timeout_error_is_retryable_timeout(self):
        spec = classify_exception(asyncio.TimeoutError())

        self.assertEqual(spec.code, "TIMEOUT")
        self.assertTrue(spec.retryable)

    def test_connection_reset_is_retryable_network_error(self):
        spec = classify_exception(Exception("Page.goto: net::ERR_CONNECTION_RESET at https://iss.fortaleza.ce.gov.br/grpfor/oauth2/login"))

        self.assertEqual(spec.code, "NETWORK_ERROR")
        self.assertTrue(spec.retryable)

    def test_network_changed_is_retryable_network_error(self):
        spec = classify_exception(Exception("Page.goto: net::ERR_NETWORK_CHANGED at https://iss.fortaleza.ce.gov.br/grpfor/home.seam"))

        self.assertEqual(spec.code, "NETWORK_ERROR")
        self.assertTrue(spec.retryable)


if __name__ == "__main__":
    unittest.main()
