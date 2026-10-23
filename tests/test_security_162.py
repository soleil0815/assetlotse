"""Sicherheitsprüfung 1.6.2: Links in Mails, Registrierung, Schlüsselanzeige, Caches, Einrichtung, Standorte."""
import io
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-test-"))

from assetlotse import create_app  # noqa: E402
from assetlotse.models import Asset, Group, SecretAccess, User, db, get_setting, set_setting  # noqa: E402
from assetlotse.views import register as reg  # noqa: E402

LAN = "192.168.1.20"
INTERNET = "203.0.113.9"


class Base(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.c = self.app.test_client()
        reg._REG_LOG.clear()
        self.sent = []
        p = mock.patch("assetlotse.mailer.send_mail", side_effect=self._fake_send)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _fake_send(self, to, subject, text, html, **kw):
        self.sent.append({"to": to, "text": text, "html": html})
        return True, ""

    def make(self, name, role="reader", groups=(), email=None):
        u = User(username=name, display_name=name.title(), role=role, auth_source="local", email=email)
        u.set_password("geheim12345")
        u.groups = list(groups)
        db.session.add(u)
        db.session.commit()
        return u

    def login(self, name, ip=LAN):
        return self.c.post("/login", data={"username": name, "password": "geheim12345"},
                           environ_base={"REMOTE_ADDR": ip})


class RegistrationMailTests(Base):
    def setUp(self):
        super().setUp()
        self.make("admin", "admin", email="admin@schule.de")
        set_setting("setup_completed", "1")
        for k, v in {"registration_mode": "open", "mail_enabled": "1", "smtp_host": "mail.test",
                     "registration_verify_email": "0"}.items():
            set_setting(k, v)
        db.session.commit()

    def submit(self, host="evil.example", **kw):
        base = f"http://{host}"
        self.c.get("/registrieren", base_url=base)
        with self.c.session_transaction(base_url=base) as s:
            s["reg_started"] = time.time() - 10
        data = {"display_name": "Lena", "email": "lena@schule.de", "username": "lena.s",
                "password": "einlangespasswort", "password2": "einlangespasswort"}
        data.update(kw)
        return self.c.post("/registrieren", data=data, base_url=base)

    def test_no_link_from_host_header(self):
        set_setting("registration_verify_email", "1")
        db.session.commit()
        r = self.submit()
        self.assertEqual(r.status_code, 503)                       # ohne eingestellte Adresse: keine Mail mit Link
        self.assertEqual(self.sent, [])
        self.assertIsNone(User.query.filter_by(username="lena.s").first())
        set_setting("app_base_url", "https://lotse.schule.de")
        db.session.commit()
        self.submit()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("https://lotse.schule.de/registrieren/bestaetigen/", self.sent[0]["text"])
        self.assertNotIn("evil.example", self.sent[0]["text"] + self.sent[0]["html"])

    def test_admin_notice_without_base_url_has_no_foreign_link(self):
        set_setting("registration_notify_admins", "1")
        db.session.commit()
        self.submit()
        mails = [m for m in self.sent if m["to"] == "admin@schule.de"]
        self.assertEqual(len(mails), 1)
        self.assertNotIn("evil.example", mails[0]["text"] + mails[0]["html"])

    def test_admin_enabling_registration_fixes_base_url(self):
        set_setting("registration_mode", "off")
        set_setting("app_base_url", "")
        db.session.commit()
        self.login("admin")
        self.c.post("/admin/einstellungen", data={"registration_mode": "approval", "loan_days": "14"},
                    base_url="https://lotse.schule.de")
        self.assertEqual(get_setting("app_base_url"), "https://lotse.schule.de")

    def test_duplicate_email_reported_but_counted(self):
        self.make("vorhanden", email="lena@schule.de")
        r = self.submit(host="lotse.schule.de")
        self.assertEqual(r.status_code, 400)
        self.assertIn("bereits ein Konto", r.get_data(as_text=True))
        self.assertEqual(len(reg._REG_LOG["try|127.0.0.1"]), 1)
        self.assertIsNone(User.query.filter_by(username="lena.s").first())

    def test_every_attempt_counts(self):
        for i in range(reg.ATTEMPTS_PER_HOUR):
            r = self.submit(host="lotse.schule.de", username=f"x{i}", password2="falsch-falsch")
            self.assertEqual(r.status_code, 400)
        r = self.submit(host="lotse.schule.de", username="noch.einer", password2="falsch-falsch")
        self.assertEqual(r.status_code, 429)


class SecretAndCacheTests(Base):
    def setUp(self):
        super().setUp()
        self.g = Group(name="IT")
        db.session.add(self.g)
        db.session.commit()
        self.vera = self.make("vera", "manager", groups=[self.g])
        self.asset = Asset(name="Laptop", quantity=1, group_id=self.g.id)
        self.asset.set_secret("111111-222222")
        db.session.add(self.asset)
        db.session.commit()

    def test_reveal_needs_second_factor_when_skipped(self):
        self.login("vera")
        self.vera.totp_enabled = True          # 2FA vorhanden, Sitzung aber ohne bestätigten Faktor
        db.session.commit()
        r = self.c.post(f"/gegenstaende/{self.asset.id}/vertraulich")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(SecretAccess.query.count(), 0)
        page = self.c.get(f"/gegenstaende/{self.asset.id}").get_data(as_text=True)
        self.assertIn("Zweiten Faktor bestätigen", page)
        with self.c.session_transaction() as s:
            s["mfa_ok"] = True
        r = self.c.post(f"/gegenstaende/{self.asset.id}/vertraulich")
        self.assertIn("111111-222222", r.get_data(as_text=True))

    def test_secret_changes_need_second_factor_too(self):
        self.login("vera")
        self.vera.totp_enabled = True
        db.session.commit()
        self.c.post(f"/gegenstaende/{self.asset.id}/bearbeiten",
                    data={"name": "Laptop", "quantity": "1", "group_id": str(self.g.id),
                          "secret_holder_view_shown": "1", "secret_holder_view": "1", "remove_secret": ""})
        db.session.refresh(self.asset)
        self.assertFalse(self.asset.secret_holder_view)
        self.assertEqual(self.asset.get_secret(), "111111-222222")
        with self.c.session_transaction() as s:
            s["mfa_ok"] = True
        self.c.post(f"/gegenstaende/{self.asset.id}/bearbeiten",
                    data={"name": "Laptop", "quantity": "1", "group_id": str(self.g.id),
                          "secret_holder_view_shown": "1", "secret_holder_view": "1"})
        db.session.refresh(self.asset)
        self.assertTrue(self.asset.secret_holder_view)                   # mit bestätigtem Faktor geht es

    def test_logged_in_responses_not_cacheable(self):
        self.login("vera")
        for url in ("/", "/gegenstaende/export.csv", f"/gegenstaende/{self.asset.id}"):
            with self.subTest(url=url):
                cc = self.c.get(url).headers.get("Cache-Control", "")
                self.assertIn("no-store", cc)
                self.assertIn("private", cc)
        img_dir = self.app.config["IMAGE_DIR"]
        os.makedirs(img_dir, exist_ok=True)
        with open(os.path.join(img_dir, "abc.jpg"), "wb") as f:
            f.write(b"x")
        cc = self.c.get("/bilder/abc.jpg").headers["Cache-Control"]
        self.assertIn("private", cc)
        self.assertNotIn("public", cc)
        from flask import g
        g.pop("_login_user", None)
        self.assertNotIn("no-store", self.app.test_client().get("/login").headers.get("Cache-Control", ""))


class PasswordChangeTests(Base):
    def test_current_password_guessing_is_throttled(self):
        self.make("lena")
        self.login("lena")
        for _ in range(8):
            self.c.post("/profil", data={"current": "falsch", "new": "neuespasswort1", "new2": "neuespasswort1"})
        r = self.c.post("/profil", data={"current": "geheim12345", "new": "neuespasswort1", "new2": "neuespasswort1"},
                        follow_redirects=True)
        self.assertIn("Zu viele Fehlversuche", r.get_data(as_text=True))
        self.assertTrue(User.query.filter_by(username="lena").first().check_password("geheim12345"))


class FirstFactorTests(Base):
    def test_enabling_first_factor_ends_other_sessions(self):
        from assetlotse import twofa
        import re
        self.make("lena")
        other = self.app.test_client()
        other.post("/login", data={"username": "lena", "password": "geheim12345"},
                   environ_base={"REMOTE_ADDR": LAN})
        self.assertEqual(other.get("/").status_code, 200)
        self.login("lena")
        html = self.c.post("/profil/sicherheit/app/einrichten").get_data(as_text=True)
        secret = re.search(r"secret=([A-Z2-7]+)", html).group(1)
        self.c.post("/profil/sicherheit/app/bestaetigen", data={"code": twofa.totp_now(secret, time.time())})
        self.assertTrue(User.query.filter_by(username="lena").first().totp_enabled)
        from flask import g
        g.pop("_login_user", None)        # Testkontext teilt g zwischen den Anfragen
        self.assertEqual(self.c.get("/").status_code, 200)            # dieses Gerät bleibt angemeldet
        g.pop("_login_user", None)
        self.assertEqual(other.get("/").status_code, 302)             # das andere ist abgemeldet


class SetupTests(Base):
    def test_setup_only_from_internal_network(self):
        r = self.c.get("/setup", environ_base={"REMOTE_ADDR": INTERNET})
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/setup", environ_base={"REMOTE_ADDR": INTERNET},
                        data={"username": "boss", "password": "einlangespasswort", "password2": "einlangespasswort"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(User.query.count(), 0)
        # weitergereichte Angabe „localhost“ über einen Proxy zählt nicht
        r = self.c.get("/setup", environ_base={"REMOTE_ADDR": "172.18.0.2"},
                       headers={"X-Forwarded-For": "127.0.0.1"})
        self.assertEqual(r.status_code, 403)
        # Proxy auf localhost, der die Besucher-Adresse nur als X-Real-IP mitschickt
        r = self.c.get("/setup", environ_base={"REMOTE_ADDR": "127.0.0.1"},
                       headers={"X-Real-IP": INTERNET, "X-Forwarded-Proto": "https"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.c.get("/setup", environ_base={"REMOTE_ADDR": LAN}).status_code, 200)
        self.assertEqual(self.c.get("/setup", environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code, 200)


class LocationLeakTests(Base):
    def test_group_manager_sees_only_own_free_text_locations(self):
        g1, g2 = Group(name="A"), Group(name="B")
        db.session.add_all([g1, g2])
        db.session.commit()
        self.make("bib1", "manager", groups=[g1])
        db.session.add_all([Asset(name="x", quantity=1, group_id=g1.id, location="Regal A"),
                            Asset(name="y", quantity=1, group_id=g2.id, location="Tresor Vorstand")])
        db.session.commit()
        self.login("bib1")
        html = self.c.get("/standorte").get_data(as_text=True)
        self.assertIn("Regal A", html)
        self.assertNotIn("Tresor Vorstand", html)
        # Raten über „übernehmen“ verrät nichts und macht den Ort nicht öffentlich
        r = self.c.post("/standorte/bestand", data={"value": "Tresor Vorstand", "target": "__neu__"},
                        follow_redirects=True)
        self.assertIn("nicht (mehr)", r.get_data(as_text=True))
        from assetlotse.models import Location
        self.assertIsNone(Location.query.filter_by(name="Tresor Vorstand").first())


class ImageTests(Base):
    def test_only_whitelisted_formats_are_parsed(self):
        from PIL import Image

        from assetlotse.views.assets import _clean_image
        buf = io.BytesIO()
        Image.new("RGB", (20, 20)).save(buf, "BMP")
        self.assertIsNone(_clean_image(buf.getvalue()))
        buf = io.BytesIO()
        Image.new("RGB", (3000, 2000), "red").save(buf, "JPEG")
        out = _clean_image(buf.getvalue())
        self.assertEqual(Image.open(io.BytesIO(out)).size, (1200, 800))


if __name__ == "__main__":
    unittest.main()
