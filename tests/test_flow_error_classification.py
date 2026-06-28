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


if __name__ == "__main__":
    unittest.main()
