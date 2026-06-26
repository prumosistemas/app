import asyncio
import sys
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from flow_core import detect_portal_access_block  # noqa: E402
from flow_errors import PortalAccessBlockedError  # noqa: E402


class _FakeLocator:
    def __init__(self, text):
        self._text = text

    async def inner_text(self, timeout=0):
        return self._text


class _FakePage:
    def __init__(self, title, body):
        self._title = title
        self._body = body

    async def title(self):
        return self._title

    def locator(self, selector):
        assert selector == "body"
        return _FakeLocator(self._body)


class PortalAccessBlockTests(unittest.TestCase):
    def test_detects_geo_ip_filter_page(self):
        page = _FakePage(
            "Forbidden!",
            "This site has been blocked by the network administrator. "
            "Block reason: GEO-IP Filter Alert. IP address: 35.198.235.66 "
            "URL: idp2.sefin.fortaleza.ce.gov.br",
        )

        with self.assertRaises(PortalAccessBlockedError) as caught:
            asyncio.run(detect_portal_access_block(page))

        self.assertEqual(caught.exception.code, "PORTAL_ACCESS_BLOCKED")
        self.assertIn("35.198.235.66", caught.exception.message)

    def test_allows_normal_login_page(self):
        page = _FakePage("Login", "Usuario Senha Entrar")
        asyncio.run(detect_portal_access_block(page))


if __name__ == "__main__":
    unittest.main()
