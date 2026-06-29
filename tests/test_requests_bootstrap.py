import asyncio
import sys
import tempfile
import unittest
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import flow_core  # noqa: E402
from flow_core import FlowConfig, FlowContext  # noqa: E402
from flow_errors import FlowError, MensagemTelaError  # noqa: E402


class RequestsBootstrapTests(unittest.TestCase):
    def _ctx(self, root: Path) -> FlowContext:
        config = FlowConfig(
            run_id="test",
            run_dir=str(root),
            run_log_file=str(root / "logs.txt"),
            cnpj_dir=str(root / "12345678000190"),
            step_timeout_sec=30,
            nav_timeout_ms=30_000,
            selector_timeout_ms=30_000,
            close_timeout_sec=5,
            goto_retries=1,
            headless=True,
        )
        return FlowContext(flow="certidao", cnpj="12345678000190", mes="06/2026", config=config)

    def test_non_retryable_bootstrap_error_is_not_fallback(self):
        original = flow_core.bootstrap_portal_requests

        def fake_bootstrap(*args, **kwargs):
            raise MensagemTelaError("Mensagem na tela")

        try:
            flow_core.bootstrap_portal_requests = fake_bootstrap
            with tempfile.TemporaryDirectory() as tmp:
                ctx = self._ctx(Path(tmp))
                with self.assertRaises(MensagemTelaError):
                    asyncio.run(
                        flow_core.try_requests_bootstrap_company(
                            None,
                            None,
                            "usuario",
                            "senha",
                            "12345678000190",
                            ctx,
                        )
                    )
        finally:
            flow_core.bootstrap_portal_requests = original

    def test_retryable_bootstrap_error_uses_fallback(self):
        original = flow_core.bootstrap_portal_requests

        def fake_bootstrap(*args, **kwargs):
            raise FlowError("TEMP", "falha temporaria", retryable=True)

        try:
            flow_core.bootstrap_portal_requests = fake_bootstrap
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                ctx = self._ctx(root)
                result = asyncio.run(
                    flow_core.try_requests_bootstrap_company(
                        None,
                        None,
                        "usuario",
                        "senha",
                        "12345678000190",
                        ctx,
                    )
                )

                self.assertIsNone(result)
                self.assertIn("REQUESTS_BOOTSTRAP_FALLBACK", (root / "logs.txt").read_text(encoding="utf-8"))
        finally:
            flow_core.bootstrap_portal_requests = original


if __name__ == "__main__":
    unittest.main()
