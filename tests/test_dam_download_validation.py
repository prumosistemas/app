import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import flow_dam  # noqa: E402
from flow_core import FlowConfig, FlowContext  # noqa: E402
from flow_errors import FlowError  # noqa: E402


class DamDownloadValidationTests(unittest.TestCase):
    def test_zero_byte_pdf_is_invalid_and_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "DAM_tipo_1.pdf"
            path.write_bytes(b"")

            valid, message = flow_dam._validar_pdf_salvo(str(path))
            flow_dam._remover_arquivo_invalido(str(path))

        self.assertFalse(valid)
        self.assertIn("Muito pequeno (0 bytes)", message)
        self.assertFalse(path.exists())

    def test_non_pdf_payload_is_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "DAM_tipo_1.pdf"
            path.write_bytes(b"<html>erro do portal</html>" * 10)

            valid, message = flow_dam._validar_pdf_salvo(str(path))

        self.assertFalse(valid)
        self.assertIn("Header inválido", message)

    def test_emitir_dams_raises_when_every_attempt_failed(self):
        async def fail_all(_page, tipo, _pasta, _ctx):
            raise FlowError(
                "DAM_DOWNLOAD_INVALID",
                f"invalid {tipo}",
                short_message="O DAM baixado veio vazio ou não é um PDF válido.",
                retryable=True,
            )

        with patch.object(flow_dam, "_emitir_dam_tipo", side_effect=fail_all):
            with self.assertRaises(FlowError) as caught:
                asyncio.run(flow_dam.emitir_dams(FakePage(), "unused", self._ctx()))

        self.assertEqual(caught.exception.code, "DAM_EMITIR_FAILED")
        self.assertTrue(caught.exception.retryable)

    def test_emitir_dams_keeps_partial_success(self):
        async def one_success(_page, tipo, _pasta, _ctx):
            if tipo == "1":
                return True
            raise FlowError("DAM_DOWNLOAD_INVALID", f"invalid {tipo}", retryable=True)

        with patch.object(flow_dam, "_emitir_dam_tipo", side_effect=one_success):
            result = asyncio.run(flow_dam.emitir_dams(FakePage(), "unused", self._ctx()))

        self.assertEqual(result, {"0": False, "1": True, "2": False})

    @staticmethod
    def _ctx():
        config = FlowConfig(
            run_id="run",
            run_dir="",
            run_log_file="",
            cnpj_dir="",
            step_timeout_sec=10,
            nav_timeout_ms=10_000,
            selector_timeout_ms=10_000,
            close_timeout_sec=5,
            goto_retries=1,
            headless=True,
        )
        return FlowContext(flow="dam", cnpj="12345678000190", mes="05/2026", config=config)


class FakePage:
    async def wait_for_selector(self, *_args, **_kwargs):
        return None


if __name__ == "__main__":
    unittest.main()
