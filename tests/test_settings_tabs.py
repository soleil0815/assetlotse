"""2.5.2: the settings page is divided into topic tabs (one form, one save button)."""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-test-"))

from assetlotse import create_app  # noqa: E402
from assetlotse.models import User, db, get_setting, set_setting  # noqa: E402
from assetlotse.views.admin import SETTINGS_TABS  # noqa: E402

# which field lives in which tab
EXPECTED = {
    "allgemein": ["org_name", "default_language"],
    "ausgabe": ["catalog_visibility", "loan_days", "extension_days", "max_extensions", "max_loans",
                "self_checkout", "self_extend", "self_return"],
    "konten": ["registration_mode", "registration_verify_email", "registration_notify_admins",
               "registration_domains", "registration_group_id"],
    "sicherheit": ["mfa_policy", "mfa_internal", "passkey_login", "session_idle_hours", "session_idle_hours_admin",
                   "session_idle_hours_external", "session_warn", "session_end_on_close",
                   "internal_networks", "own_public_hosts"],
    "datenschutz": ["imprint_url", "privacy_url", "retention_months"],
}


class SettingsTabsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        set_setting("setup_completed", "1")
        u = User(username="anna", display_name="Anna", role="admin", auth_source="local")
        u.set_password("geheim12345")
        db.session.add(u)
        db.session.commit()
        self.c = self.app.test_client()
        self.c.post("/login", data={"username": "anna", "password": "geheim12345"})

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def panels(self):
        html = self.c.get("/admin/einstellungen").get_data(as_text=True)
        parts = re.split(r'<div class="grid grid-2" id="([a-z]+)" data-tab-panel>', html)
        return html, {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}

    def test_every_field_in_its_tab(self):
        html, panels = self.panels()
        self.assertEqual(tuple(panels), SETTINGS_TABS)
        for tab in SETTINGS_TABS:
            self.assertIn(f'href="#{tab}" data-tab="{tab}"', html)
        for tab, fields in EXPECTED.items():
            for field in fields:
                self.assertIn(f'name="{field}"', panels[tab], (tab, field))
        # nothing left outside the tabs (besides the hidden fields of the form)
        form = html[html.index("data-settings-form"):html.index("</form>", html.index("data-settings-form"))]
        all_fields = set(re.findall(r'name="([a-z_]+)"', form)) - {"csrf_token", "tab"}
        self.assertEqual(all_fields, {f for fs in EXPECTED.values() for f in fs})
        # cards that other pages link to
        self.assertIn('id="aufbewahrung"', panels["datenschutz"])
        self.assertIn('id="abmeldung"', panels["sicherheit"])

    def test_save_returns_to_the_open_tab(self):
        form = {"org_name": "Institut", "session_idle_hours": "12", "tab": "sicherheit"}
        r = self.c.post("/admin/einstellungen", data=form)
        self.assertEqual(r.headers["Location"], "/admin/einstellungen#sicherheit")
        self.assertEqual(get_setting("org_name"), "Institut")
        form["tab"] = "javascript:alert(1)"                          # unknown tab: no anchor
        r = self.c.post("/admin/einstellungen", data=form)
        self.assertEqual(r.headers["Location"], "/admin/einstellungen")

    def test_registration_points_to_the_new_place(self):
        _, panels = self.panels()
        self.assertIn("Datenschutz → Rechtliches", panels["konten"])


if __name__ == "__main__":
    unittest.main()
