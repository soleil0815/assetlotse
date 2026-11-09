"""2.5.0: no inline styles – the Content Security Policy forbids them (style-src 'self', no 'unsafe-inline').

Former style="…" attributes are utility classes in static/css/utilities.css; brand colours, group colours and
the sign-in background come from the generated /branding/site.css. E-mails (templates/mail) keep their inline
styles: mail programs need them, and they are never served under this policy.
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-test-"))

from assetlotse import CSP, create_app  # noqa: E402
from assetlotse.models import Asset, Group, User, db, set_setting  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "assetlotse" / "templates"
UTILITIES = (ROOT / "assetlotse" / "static" / "css" / "utilities.css").read_text()


def page_templates():
    return [p for p in TEMPLATES.rglob("*.html") if "mail" not in p.relative_to(TEMPLATES).parts]


class SourceTests(unittest.TestCase):
    def test_policy_forbids_inline_styles(self):
        self.assertIn("style-src 'self';", CSP)
        self.assertNotIn("unsafe-inline", CSP)

    def test_no_style_attribute_or_element_in_page_templates(self):
        for path in page_templates():
            text = path.read_text()
            self.assertIsNone(re.search(r"\sstyle\s*=", text), path.name)
            self.assertNotIn("<style", text, path.name)

    def test_translations_carry_no_style_attribute_for_pages(self):
        import json
        de = json.loads((ROOT / "assetlotse" / "translations" / "de.json").read_text())
        mail_only = {"You can turn off notifications in your <a href=\"{url}\" style=\"color:{color};\">profile</a>."}
        bad = [k for k in de if "style=" in k and k not in mail_only]
        self.assertEqual(bad, [])

    def test_every_utility_class_is_defined(self):
        defined = set(re.findall(r"^\.(u-[a-z0-9_-]+|pct-\d+)\s*\{", UTILITIES, re.M))
        used = set()
        for path in page_templates():
            for value in re.findall(r'class="([^"]*)"', path.read_text()):
                used.update(c for c in re.findall(r"\bu-[a-z0-9_-]+", value) if not c.endswith("-"))
        self.assertEqual(used - defined, set())

    def test_utilities_load_before_app_css(self):
        base = (TEMPLATES / "base.html").read_text()
        self.assertLess(base.index("css/utilities.css"), base.index("css/app.css"))
        self.assertLess(base.index("css/app.css"), base.index("main.site_css"))
        # !important on every utility, so they keep the precedence the inline style had
        for line in UTILITIES.splitlines():
            if line.startswith(".u-"):
                self.assertIn("!important", line)


class RenderedTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        set_setting("setup_completed", "1")
        set_setting("brand_accent", "#7c3aed")
        g = Group(name="IT", color="#e11d48")
        bad = Group(name="Kaputt", color="red;}body{display:none")       # never trusted as CSS
        db.session.add_all([g, bad])
        admin = User(username="anna", display_name="Anna", role="admin", auth_source="local")
        admin.set_password("geheim12345")
        db.session.add(admin)
        db.session.add(Asset(name="Laptop", category="Laptop", group=g))
        db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_pages_have_no_inline_styles(self):
        html = self.c.get("/login").get_data(as_text=True)
        self.assertNotIn("style=", html)
        self.c.post("/login", data={"username": "anna", "password": "geheim12345"})
        for url in ("/", "/gegenstaende", "/gegenstaende/1", "/admin/gruppen", "/admin/benutzer/1",
                    "/admin/erscheinungsbild", "/admin/einstellungen", "/admin/verzeichnisdienst", "/theke",
                    "/profil/sicherheit", "/ausleihen"):
            r = self.c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertNotIn("style=", r.get_data(as_text=True), url)
            self.assertNotIn("unsafe-inline", r.headers["Content-Security-Policy"], url)

    def test_site_css_is_public_and_safe(self):
        r = self.c.get("/branding/site.css")                    # the sign-in page needs it: no login
        self.assertEqual(r.status_code, 200)
        css = r.get_data(as_text=True)
        self.assertIn("--accent:#7c3aed", css)
        self.assertIn(".grp-1{background:#e11d48}", css)
        self.assertIn(".grp-2{background:#0b7285}", css)          # invalid colour → default, nothing injected
        self.assertNotIn("display:none", css)
        self.assertEqual(r.headers["Cache-Control"], "no-cache")    # without ?v=

    def test_group_badge_uses_the_group_class(self):
        self.c.post("/login", data={"username": "anna", "password": "geheim12345"})
        html = self.c.get("/gegenstaende/1").get_data(as_text=True)
        self.assertIn('class="dot grp-1"', html)


if __name__ == "__main__":
    unittest.main()
