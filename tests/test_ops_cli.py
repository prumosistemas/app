import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from ops.prumo_ops import build_netlify_zip, build_worker_bundle, login_secret_names
from ops.secret_store import SecretStore, redact


class OpsCliTests(unittest.TestCase):
    def test_secret_store_roundtrip_uses_encrypted_envelope(self):
        if os.name != "nt":
            self.skipTest("DPAPI only exists on Windows")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "store.json"
            store = SecretStore(path)
            store.set("EXAMPLE_TOKEN", "super-secret-test-value")
            self.assertEqual(store.require("EXAMPLE_TOKEN"), "super-secret-test-value")
            self.assertNotIn("super-secret-test-value", path.read_text(encoding="utf-8"))

    def test_redaction_never_returns_loaded_secret(self):
        self.assertEqual(redact("before abc123 after", ["abc123"]), "before [REDACTED] after")

    def test_worker_bundle_inlines_html_imports(self):
        bundle = build_worker_bundle()
        self.assertNotIn('from "../login.html"', bundle)
        self.assertIn("const loginHtml =", bundle)
        self.assertIn("export default", bundle)

    def test_netlify_zip_contains_only_public_artifacts(self):
        package = build_netlify_zip()
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            names = set(archive.namelist())
        self.assertIn("login.html", names)
        self.assertIn("_redirects", names)
        self.assertNotIn("token.txt", names)
        self.assertNotIn("AccountID.txt", names)
        self.assertFalse(any(name.startswith("server/") for name in names))

    def test_login_alias_maps_to_names_not_values(self):
        self.assertEqual(login_secret_names("master"), ("LOGIN.master.EMAIL", "LOGIN.master.PASSWORD"))


if __name__ == "__main__":
    unittest.main()
