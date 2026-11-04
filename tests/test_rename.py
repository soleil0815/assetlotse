"""Startup, about page and neutral name in the UI."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-test-"))

import assetlotse  # noqa: E402
from assetlotse import create_app  # noqa: E402
from assetlotse.models import User, db, get_setting, set_setting  # noqa: E402


class StartupTests(unittest.TestCase):
    def test_new_database_file(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual(assetlotse._sqlite_file(d).name, "assetlotse.db")

    def test_refuses_to_start_empty_when_other_database_exists(self):
        d = Path(tempfile.mkdtemp())
        (d / "alt.db").write_bytes(b"SQLite format 3\0" + b"x" * 100)
        with self.assertRaises(SystemExit) as cm:
            assetlotse._sqlite_file(d)
        self.assertIn("alt.db", str(cm.exception))
        (d / "assetlotse.db").write_bytes(b"")
        self.assertEqual(assetlotse._sqlite_file(d).name, "assetlotse.db")

    def test_default_name_is_assetlotse(self):
        d = tempfile.mkdtemp()
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{d}/neu.db"})
        with app.app_context():
            self.assertEqual(get_setting("org_name"), "AssetLotse")
            html = app.test_client().get("/setup").get_data(as_text=True)
            self.assertIn("AssetLotse", html)
            db.session.remove()

    def test_custom_org_name_untouched(self):
        d = tempfile.mkdtemp()
        uri = f"sqlite:///{d}/old.db"
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri})
        with app.app_context():
            set_setting("org_name", "IT-Ausgabe")
            db.session.commit()
            db.session.remove()
        app2 = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri})
        with app2.app_context():
            self.assertEqual(get_setting("org_name"), "IT-Ausgabe")
            db.session.remove()


class AboutPageTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        u = User(username="admin", display_name="Admin", role="admin", auth_source="local")
        u.set_password("geheim12345")
        db.session.add(u)
        db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_about_requires_login(self):
        self.assertIn("/login", self.c.get("/info").headers["Location"])
        self.assertIn("/login", self.c.get("/info/lizenz").headers["Location"])

    def test_about_shows_license_and_source(self):
        self.c.post("/login", data={"username": "admin", "password": "geheim12345"})
        os.environ["ASSETLOTSE_SOURCE_URL"] = "https://github.com/beispiel/assetlotse"
        try:
            html = self.c.get("/info").get_data(as_text=True)
        finally:
            del os.environ["ASSETLOTSE_SOURCE_URL"]
        self.assertIn("AGPL", html)
        self.assertIn("https://github.com/beispiel/assetlotse", html)
        self.assertIn(assetlotse.__version__, html)
        lic = self.c.get("/info/lizenz")
        self.assertEqual(lic.status_code, 200)
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", lic.get_data(as_text=True))

    def test_login_page_reveals_no_version(self):
        html = self.c.get("/login").get_data(as_text=True)
        self.assertNotIn(assetlotse.__version__, html)
        self.assertNotIn("/info", html)


if __name__ == "__main__":
    unittest.main()


class NoProductNameTests(unittest.TestCase):
    """With a custom name (e.g. the institution's), emails carry that name instead of "AssetLotse"."""

    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        u = User(username="admin", display_name="Admin", role="admin", auth_source="local")
        u.set_password("geheim12345")
        db.session.add(u)
        set_setting("org_name", "Bibliothek des Ausbildungsinstituts")
        db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def visible_text(self, html):
        import re
        html = re.sub(r"<(script|style)\b.*?</\1>", "", html, flags=re.S)
        html = re.sub(r"<[^>]+>", " ", html)
        return html

    def test_mails_use_org_name(self):
        from assetlotse.mailer import render_mail
        with self.app.test_request_context():
            subject, text, html = render_mail("test")
        for part in (subject, text, self.visible_text(html)):
            self.assertNotIn("assetlotse", part.lower())
        self.assertIn("Bibliothek des Ausbildungsinstituts", html)

