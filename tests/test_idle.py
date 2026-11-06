"""2.2.0: automatic sign-out after inactivity (assetlotse/idle.py)."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-test-"))

from assetlotse import create_app, idle  # noqa: E402
from assetlotse.models import User, db, get_setting, set_setting  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LAN = {"REMOTE_ADDR": "192.168.1.20"}
INTERNET = {"REMOTE_ADDR": "203.0.113.9"}
HOUR = 3600


class Base(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        set_setting("setup_completed", "1")
        db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def new_browser(self):
        """A second browser. The tests keep one app context open, so Flask's g (where Flask-Login keeps the
        user of the request) would otherwise carry over from the previous client."""
        from flask import g
        g.pop("_login_user", None)
        self.c = self.app.test_client()

    def make(self, name, role="reader"):
        u = User(username=name, display_name=name.title(), role=role, auth_source="local")
        u.set_password("geheim12345")
        db.session.add(u)
        db.session.commit()
        return u

    def setting(self, **kw):
        for k, v in kw.items():
            set_setting(k, v)
        db.session.commit()

    def login(self, name, env=LAN, remember=False):
        data = {"username": name, "password": "geheim12345"}
        if remember:
            data["remember"] = "1"
        r = self.c.post("/login", data=data, environ_base=env)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])
        return r

    def idle_for(self, seconds):
        with self.c.session_transaction() as s:
            s["seen"] = int(time.time()) - seconds

    def seen(self):
        with self.c.session_transaction() as s:
            return s.get("seen")

    def signed_in(self, env=LAN):
        return self.c.get("/", environ_base=env).status_code == 200


class LimitTests(Base):
    def test_default_twelve_hours(self):
        self.make("rita")
        self.login("rita")
        self.idle_for(11 * HOUR)
        self.assertTrue(self.signed_in())
        self.assertGreater(self.seen(), time.time() - 60)              # the request counted as activity
        self.idle_for(12 * HOUR + 60)
        r = self.c.get("/ausleihen", environ_base=LAN)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        self.assertIn("next=", r.headers["Location"])
        page = self.c.get("/login", environ_base=LAN).get_data(as_text=True)
        self.assertIn("automatisch abgemeldet", page)
        self.assertFalse(self.signed_in())

    def test_shorter_limit_for_administrators(self):
        self.make("rita")
        self.make("anna", "admin")
        self.setting(session_idle_hours_admin="1")
        for name, out in (("rita", False), ("anna", True)):
            self.new_browser()
            self.login(name)
            self.idle_for(2 * HOUR)
            self.assertEqual(self.signed_in(), not out, name)

    def test_shorter_limit_from_outside_only(self):
        self.make("rita")
        self.setting(session_idle_hours_external="0.5")
        self.login("rita")
        self.idle_for(HOUR)
        self.assertTrue(self.signed_in(LAN))
        self.idle_for(HOUR)
        self.assertFalse(self.signed_in(INTERNET))

    def test_shortest_applies_and_zero_switches_off(self):
        u = self.make("anna", "admin")
        self.setting(session_idle_hours="2", session_idle_hours_admin="5", session_idle_hours_external="3")
        self.assertEqual(idle.limit_seconds(u, internal=True), 2 * HOUR)
        self.assertEqual(idle.limit_seconds(u, internal=False), 2 * HOUR)
        self.setting(session_idle_hours="0")
        self.assertEqual(idle.limit_seconds(u, internal=True), 5 * HOUR)    # the role limit still applies
        self.setting(session_idle_hours_admin="")
        self.assertEqual(idle.limit_seconds(u, internal=True), 0)
        self.make("rita")
        self.login("rita")
        self.idle_for(400 * HOUR)
        self.assertTrue(self.signed_in())
        self.assertNotIn("data-idle", self.c.get("/", environ_base=LAN).get_data(as_text=True))

    def test_start_value_from_environment(self):
        u = self.make("rita")
        with mock.patch.dict(os.environ, {"ASSETLOTSE_SESSION_HOURS": "3"}):
            self.assertEqual(idle.limit_seconds(u, internal=True), 3 * HOUR)
            self.setting(session_idle_hours="8")                            # the setting wins once saved
            self.assertEqual(idle.limit_seconds(u, internal=True), 8 * HOUR)
        for bad in ("abc", "-1", "0.1", "1000"):
            with mock.patch.dict(os.environ, {"ASSETLOTSE_SESSION_HOURS": bad}):
                self.assertEqual(idle.env_default(), "12", bad)

    def test_new_sign_in_starts_the_clock(self):
        self.make("rita")
        self.login("rita")
        self.idle_for(13 * HOUR)
        self.c.post("/logout", environ_base=LAN)
        self.login("rita")                       # an old "seen" must not end the new session right away
        self.assertTrue(self.signed_in())

    def test_parse_hours(self):
        self.assertEqual(idle.parse_hours("12"), "12")
        self.assertEqual(idle.parse_hours("0,5"), "0.5")
        self.assertEqual(idle.parse_hours(" 1.50 "), "1.5")
        self.assertEqual(idle.parse_hours(""), "")
        self.assertEqual(idle.parse_hours("0", allow_off=True), "0")
        for bad in ("0", "0.2", "721", "nan", "inf", "-2", "x"):
            self.assertIsNone(idle.parse_hours(bad), bad)


class BrowserTests(Base):
    def test_status_does_not_extend_keepalive_does(self):
        self.make("rita")
        self.login("rita")
        self.idle_for(HOUR)
        before = self.seen()
        r = self.c.get("/sitzung/status", environ_base=LAN)
        self.assertEqual(r.status_code, 200)
        self.assertAlmostEqual(r.get_json()["remaining"], 11 * HOUR, delta=60)
        self.assertEqual(self.seen(), before)
        r = self.c.post("/sitzung/weiter", environ_base=LAN)
        self.assertAlmostEqual(r.get_json()["remaining"], 12 * HOUR, delta=60)
        self.assertGreater(self.seen(), before)

    def test_expired_status_answers_401(self):
        self.make("rita")
        self.login("rita")
        self.idle_for(13 * HOUR)
        r = self.c.get("/sitzung/status", environ_base=LAN)
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.get_json()["signed_out"])
        self.assertEqual(self.c.get("/sitzung/status").status_code, 401)        # signed out stays signed out
        page = self.c.get("/login", environ_base=LAN).get_data(as_text=True)
        self.assertIn("automatisch abgemeldet", page)                          # the page the browser opens says why

    def test_page_carries_the_data_for_the_notice(self):
        self.make("rita")
        self.login("rita")
        html = self.c.get("/", environ_base=LAN).get_data(as_text=True)
        self.assertIn('data-idle-warn="120"', html)
        import re
        left = int(re.search(r'data-idle-remaining="(\d+)"', html).group(1))
        self.assertAlmostEqual(left, 12 * HOUR, delta=60)
        self.assertIn("Weiterarbeiten", html)
        self.setting(session_warn="0")
        html = self.c.get("/", environ_base=LAN).get_data(as_text=True)
        self.assertIn('data-idle-warn="0"', html)                            # no notice, but still leaves
        js = (ROOT / "assetlotse" / "static" / "js" / "app.js").read_text()
        self.assertIn('$("[data-idle]")', js)
        self.assertIn("window.location.assign(url)", js)                    # GET, never re-sends a form
        self.assertIn('data-idle-login="/login"', html)


class BrowserCloseTests(Base):
    def cookie(self, response, name):
        return [h for h in response.headers.getlist("Set-Cookie") if h.startswith(name + "=")]

    def test_stay_signed_in_makes_the_session_cookie_persistent(self):
        self.make("rita")
        r = self.login("rita", remember=True)
        self.assertTrue(self.cookie(r, "remember_token"))
        self.assertIn("Expires=", self.cookie(r, "session")[0])
        self.new_browser()
        r = self.login("rita")
        self.assertNotIn("Expires=", self.cookie(r, "session")[0])           # without: ends with the browser

    def test_end_on_close_switches_off_stay_signed_in(self):
        self.make("rita")
        self.setting(session_end_on_close="1")
        self.assertNotIn('name="remember"', self.c.get("/login").get_data(as_text=True))
        r = self.login("rita", remember=True)
        self.assertEqual([h for h in self.cookie(r, "remember_token") if "Expires=Thu, 01 Jan 1970" not in h], [])
        self.assertNotIn("Expires=", self.cookie(r, "session")[0])

    def test_end_on_close_rejects_an_old_remember_cookie(self):
        self.make("rita")
        self.login("rita", remember=True)
        self.setting(session_end_on_close="1")
        token = self.c.get_cookie("remember_token").value
        self.new_browser()                                                    # "browser restarted"
        self.c.set_cookie("remember_token", token)
        r = self.c.get("/", environ_base=LAN)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        self.assertFalse(self.signed_in())


class SettingsPageTests(Base):
    def setUp(self):
        super().setUp()
        self.make("anna", "admin")
        self.login("anna")

    def save(self, **kw):
        form = {"org_name": "AssetLotse", "session_idle_hours": "12", "session_idle_hours_admin": "",
                "session_idle_hours_external": "", "session_warn": "1"}
        form.update(kw)
        return self.c.post("/admin/einstellungen", data=form, environ_base=LAN, follow_redirects=True)

    def test_section_shown(self):
        html = self.c.get("/admin/einstellungen", environ_base=LAN).get_data(as_text=True)
        self.assertIn("Automatische Abmeldung", html)
        self.assertIn('name="session_idle_hours" value="12"', html)
        self.assertIn('name="session_end_on_close"', html)

    def test_save_and_reject(self):
        self.save(session_idle_hours="8", session_idle_hours_admin="1,5", session_idle_hours_external="2",
                  session_end_on_close="1")
        self.assertEqual(get_setting("session_idle_hours"), "8")
        self.assertEqual(get_setting("session_idle_hours_admin"), "1.5")
        self.assertEqual(get_setting("session_idle_hours_external"), "2")
        self.assertEqual(get_setting("session_end_on_close"), "1")
        page = self.save(session_idle_hours="", session_idle_hours_admin="0", session_warn="")
        text = page.get_data(as_text=True)
        self.assertIn("nicht geändert", text)
        self.assertEqual(get_setting("session_idle_hours"), "8")              # invalid → old value stays
        self.assertEqual(get_setting("session_idle_hours_admin"), "1.5")
        self.assertEqual(get_setting("session_warn"), "0")
        self.save(session_idle_hours="0", session_idle_hours_admin="")
        self.assertEqual(get_setting("session_idle_hours"), "0")
        self.assertEqual(get_setting("session_idle_hours_admin"), "")


if __name__ == "__main__":
    unittest.main()
