import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import domain  # noqa: E402


class AccountCredentialsValidationTests(unittest.TestCase):
    def test_hydration_rejects_credentials_that_cannot_be_decrypted(self):
        account = {"id": "acc_test", "alias": "Conta teste", "usuario": "", "senha": ""}
        with patch.object(domain, "get_account_or_404", return_value=account):
            with self.assertRaises(HTTPException) as raised:
                domain.hydrate_tasks_with_current_accounts(
                    domain.direct_local_context(),
                    [{"account_id": "acc_test", "flow_mode": "notas"}],
                )
        self.assertEqual(raised.exception.status_code, 400)

    def test_hydration_preserves_valid_credentials(self):
        account = {"id": "acc_test", "alias": "Conta teste", "usuario": " usuario ", "senha": "senha"}
        with patch.object(domain, "get_account_or_404", return_value=account):
            hydrated = domain.hydrate_tasks_with_current_accounts(
                domain.direct_local_context(),
                [{"account_id": "acc_test", "flow_mode": "notas"}],
            )
        self.assertEqual(hydrated[0]["usuario"], "usuario")
        self.assertEqual(hydrated[0]["senha"], "senha")


if __name__ == "__main__":
    unittest.main()
