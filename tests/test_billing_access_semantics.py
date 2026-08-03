from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BillingAccessSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.worker = (ROOT / "cloudflare" / "worker.js").read_text(encoding="utf-8")
        cls.admin = (ROOT / "admin.html").read_text(encoding="utf-8")
        cls.master = (ROOT / "master.html").read_text(encoding="utf-8")
        cls.index = (ROOT / "index.html").read_text(encoding="utf-8")

    def test_payment_confirmation_preserves_manual_deactivation(self) -> None:
        active_branch = self.worker.split("if (state.active) {", 1)[1].split("return state;", 1)[0]
        self.assertIn("SET billing_disabled = 0", active_branch)
        self.assertIn("disabled = CASE WHEN manual_disabled = 1 THEN 1 ELSE 0 END", active_branch)

    def test_payment_pending_marks_every_member_as_billing_blocked(self) -> None:
        pending_branch = self.worker.split("if (state.active) {", 1)[1].split("return state;", 2)[1]
        self.assertIn("SET billing_disabled = 1", pending_branch)
        self.assertNotIn("AND manual_disabled = 0", pending_branch)

    def test_blocked_member_message_is_only_reached_after_password_check(self) -> None:
        login = self.worker.split("async function handleLoginPost", 1)[1].split("async function handleLogout", 1)[0]
        self.assertLess(login.index("const passwordOk = await verifyPassword"), login.index("memberAccessDeniedPayload"))
        self.assertIn('memberAccessDeniedPayload("billing_pending", ownerEmail)', login)
        self.assertIn("admin_email", self.worker)

    def test_admin_distinguishes_pending_from_manual_and_hides_reactivate(self) -> None:
        self.assertIn("Pagamento pendente", self.admin)
        self.assertIn("!u.blocked_by_billing", self.admin)
        self.assertIn("Number(u.manual_disabled) === 1", self.admin)
        self.assertNotIn('data-section="adminOverviewSection"', self.admin)

    def test_owner_and_master_views_surface_pending_state(self) -> None:
        self.assertIn('id="billingPendingBanner"', self.admin)
        self.assertIn('id="billingPendingBanner"', self.index)
        self.assertIn("company.billing_pending", self.master)
        self.assertIn("Pendente", self.master)


if __name__ == "__main__":
    unittest.main()
