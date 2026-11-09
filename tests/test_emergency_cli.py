"""2.5.1: emergency commands for a locked-out administrator (make-admin, unlock, list-admins)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-test-"))

from assetlotse import create_app, lockout  # noqa: E402
from assetlotse.models import User, db, set_setting  # noqa: E402


class EmergencyCliTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        set_setting("setup_completed", "1")
        for name, role, source in (("admin", "admin", "local"), ("michael", "reader", "local"),
                                   ("ad.user", "manager", "ldap")):
            u = User(username=name, display_name=name, role=role, auth_source=source)
            u.set_password("geheim12345")
            db.session.add(u)
        db.session.commit()
        self.run_cli = self.app.test_cli_runner().invoke

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def user(self, name):
        db.session.expire_all()
        return User.query.filter_by(username=name).one()

    def lock(self, name):
        for _ in range(lockout.MAX_FAILS):
            lockout.fail(f"{name}|203.0.113.9", name)
        self.assertIn(name, lockout.locked_usernames())

    def test_make_admin_restores_role_and_account(self):
        u = self.user("michael")
        u.active = False
        db.session.commit()
        r = self.run_cli(args=["make-admin", "michael"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertEqual((self.user("michael").role, self.user("michael").active), ("admin", True))
        self.assertNotIn("Active Directory", r.output)
        r = self.run_cli(args=["make-admin", "ad.user"])          # AD account: role comes back from AD rules
        self.assertEqual(self.user("ad.user").role, "admin")
        self.assertIn("Active Directory", r.output)
        r = self.run_cli(args=["make-admin", "niemand"])
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("not found", r.output)

    def test_unlock_lifts_the_lockout(self):
        self.lock("admin")
        r = self.run_cli(args=["unlock", "admin"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("Lockout lifted for admin", r.output)
        self.assertNotIn("admin", lockout.locked_usernames())

    def test_reset_password_also_unlocks(self):
        self.lock("admin")
        r = self.run_cli(args=["reset-password", "admin"], input="neuesPasswort1\nneuesPasswort1\n")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertNotIn("admin", lockout.locked_usernames())
        self.assertTrue(self.user("admin").check_password("neuesPasswort1"))

    def test_list_admins(self):
        r = self.run_cli(args=["list-admins"])
        self.assertIn("admin", r.output)
        self.assertNotIn("michael", r.output)
        u = self.user("admin")
        u.role = "reader"
        db.session.commit()
        self.assertIn("No administrator account", self.run_cli(args=["list-admins"]).output)


if __name__ == "__main__":
    unittest.main()
