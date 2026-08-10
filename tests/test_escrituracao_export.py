import base64
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from flow_errors import FlowError  # noqa: E402
from flow_escrituracao import _validate_exportacao_excel, baixar_exportacao  # noqa: E402


def _xlsx_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["CNPJ", "Valor"])
    sheet.append(["12345678000190", 10])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


class _FakeElement:
    async def is_visible(self):
        return True


class _FakePage:
    def __init__(self, response):
        self.response = response

    async def query_selector(self, selector):
        return _FakeElement() if selector.endswith("fileButton") else None

    async def eval_on_selector(self, selector, script):
        self.selector = selector
        self.script = script
        return self.response


class EscrituracaoExportTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _ctx(log_file):
        return SimpleNamespace(
            flow="escrituracao",
            cnpj="12345678000190",
            mes="08/2026",
            empresa="",
            step="",
            config=SimpleNamespace(run_id="test", run_log_file=log_file),
        )

    def test_validates_real_xlsx_structure(self):
        extension, rows = _validate_exportacao_excel(_xlsx_bytes())
        self.assertEqual(extension, ".xlsx")
        self.assertEqual(rows, 2)

    def test_rejects_empty_file(self):
        with self.assertRaisesRegex(ValueError, "vazio"):
            _validate_exportacao_excel(b"")

    async def test_browser_fetch_is_saved_atomically(self):
        data = _xlsx_bytes()
        page = _FakePage({
            "ok": True,
            "status": 200,
            "byteLength": len(data),
            "base64": base64.b64encode(data).decode("ascii"),
        })
        with tempfile.TemporaryDirectory() as directory:
            ctx = self._ctx(os.path.join(directory, "run.log"))
            await baixar_exportacao(page, "12345678000190", directory, ctx)
            path = os.path.join(directory, "exportacao_12345678000190.xlsx")
            self.assertTrue(os.path.exists(path))
            self.assertGreater(os.path.getsize(path), 0)

    async def test_zero_byte_response_is_retryable_error(self):
        page = _FakePage({"ok": True, "status": 200, "byteLength": 0, "base64": ""})
        with tempfile.TemporaryDirectory() as directory:
            ctx = self._ctx(os.path.join(directory, "run.log"))
            with self.assertRaises(FlowError) as caught:
                await baixar_exportacao(page, "12345678000190", directory, ctx)
            self.assertEqual(caught.exception.code, "EXPORTACAO_ARQUIVO_INVALIDO")
            self.assertTrue(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
