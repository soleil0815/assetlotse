"""Datenschutz bei Betrieb im Internet: keine Fremdserver im Browser, Rechtliches-Links, Cover-Proxy."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-test-"))

from assetlotse import create_app  # noqa: E402
from assetlotse.models import Asset, User, db, get_setting, set_setting  # noqa: E402
from assetlotse.views import assets as assets_view  # noqa: E402

PKG = Path(__file__).resolve().parent.parent / "assetlotse"
EXTERNAL = re.compile(r"""(?:src|href)\s*=\s*["'](?:https?:)?//""", re.I)


class Base(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        for name, role in (("admin", "admin"), ("bib", "manager"), ("leser", "reader")):
            u = User(username=name, display_name=name.title(), role=role, auth_source="local")
            u.set_password("geheim12345")
            db.session.add(u)
        db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def login(self, name):
        self.c.post("/logout")
        self.c.post("/login", data={"username": name, "password": "geheim12345"})


class NoThirdPartyTests(Base):
    def test_static_files_reference_no_external_hosts(self):
        for f in list((PKG / "static").rglob("*.js")) + list((PKG / "static").rglob("*.css")):
            text = f.read_text(encoding="utf-8")
            hit = re.search(r"(?:https?:)?//(?!www\.w3\.org)[a-z0-9-]+\.[a-z0-9.-]*[a-z]{2,}", text, re.I)
            with self.subTest(file=f.name):
                self.assertIsNone(hit, f"{f.name} verweist auf {hit.group(0) if hit else ''}")

    def test_pages_load_nothing_from_outside(self):
        set_setting("imprint_url", "https://institut.example/impressum")
        db.session.commit()
        pages = ["/login"]
        html = self.c.get("/login").get_data(as_text=True)
        self.assertNotIn("googleapis", html)
        self.login("admin")
        for url in ["/", "/gegenstaende", "/gegenstaende/neu", "/info", "/admin/einstellungen"]:
            page = self.c.get(url).get_data(as_text=True)
            pages.append(url)
            # nur ausgehende Links (Impressum usw.) sind erlaubt, keine eingebundenen Ressourcen
            for m in re.finditer(r"""<(link|script|img)\b[^>]*>""", page, re.I):
                with self.subTest(url=url, tag=m.group(0)[:80]):
                    self.assertIsNone(EXTERNAL.search(m.group(0)))

    def test_fonts_are_bundled(self):
        css = (PKG / "static/css/fonts.css").read_text()
        for name in re.findall(r"url\(\.\./fonts/([^)]+)\)", css):
            self.assertTrue((PKG / "static/fonts" / name).is_file(), name)
        self.assertTrue((PKG / "static/fonts/LICENSE-Inter.txt").is_file())


class LegalLinkTests(Base):
    def test_hidden_when_not_configured(self):
        html = self.c.get("/login").get_data(as_text=True)
        self.assertNotIn("Impressum", html)

    def test_shown_on_login_sidebar_and_registration(self):
        self.login("admin")
        self.c.post("/admin/einstellungen", data={"imprint_url": "https://institut.example/impressum",
                                                 "privacy_url": "https://institut.example/datenschutz",
                                                 "registration_mode": "open", "loan_days": "28"})
        self.assertEqual(get_setting("imprint_url"), "https://institut.example/impressum")
        sidebar = self.c.get("/").get_data(as_text=True)
        self.assertIn("https://institut.example/datenschutz", sidebar)
        self.c.post("/logout")
        login = self.c.get("/login").get_data(as_text=True)
        self.assertIn("https://institut.example/impressum", login)
        self.assertIn("https://institut.example/datenschutz", login)
        reg = self.c.get("/registrieren").get_data(as_text=True)
        self.assertIn("https://institut.example/datenschutz", reg)

    def test_rejects_javascript_urls(self):
        self.login("admin")
        self.c.post("/admin/einstellungen", data={"imprint_url": "javascript:alert(1)", "loan_days": "28"})
        self.assertEqual(get_setting("imprint_url"), "")

    def test_mail_footer_contains_links(self):
        from assetlotse.mailer import render_mail
        set_setting("imprint_url", "https://institut.example/impressum")
        set_setting("privacy_url", "https://institut.example/datenschutz")
        db.session.commit()
        with self.app.test_request_context():
            _, _, html = render_mail("test")
        self.assertIn("https://institut.example/impressum", html)
        self.assertIn("https://institut.example/datenschutz", html)


def _real_png() -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.effect_noise((120, 160), 80).convert("RGB").save(buf, "PNG")
    return buf.getvalue()


PNG = _real_png()


if __name__ == "__main__":
    unittest.main()
