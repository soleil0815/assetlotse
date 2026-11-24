"""Code audit 2026-11 (second run, 2.9.1): every confirmed finding has a test here that fails on 2.9.0."""
import ast
import io
import logging
import shutil
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from test_app import BaseCase

from assetlotse import applog
from assetlotse.models import LoginAttempt, User, db

ROOT = Path(__file__).resolve().parent.parent


class DiagnosticLogTests(BaseCase):
    """The diagnostic package promises: names and user names are replaced by placeholders. Values that were not
    (or no longer) in the database stayed in log.txt in clear text – deleted accounts, a name searched at the desk,
    a password typed into the user name field; so did user names that are also program words ("anna")."""

    def setUp(self):
        super().setUp()
        self.instance = Path(self.app.instance_path)
        shutil.rmtree(applog.log_dir(self.instance), ignore_errors=True)
        self.addCleanup(shutil.rmtree, applog.log_dir(self.instance), True)
        handler = applog.DataLogHandler(applog.log_dir(self.instance) / applog.LOG_NAME)
        lg = logging.getLogger("assetlotse")
        lg.addHandler(handler)
        old = lg.level
        lg.setLevel(logging.INFO)
        self.addCleanup(lg.setLevel, old)
        self.addCleanup(lg.removeHandler, handler)

    def package(self) -> str:
        self.login("chefin")
        r = self.c.post("/admin/diagnose/paket")
        self.assertEqual(r.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            return zf.read("log.txt").decode()

    def test_deleted_user_is_not_in_the_package(self):
        self.make_user("chefin", role="admin")
        victim = self.make_user("kzwiebelmeier")
        self.login("chefin")
        self.c.post(f"/admin/benutzer/{victim.id}/entsperren")
        self.c.post(f"/admin/benutzer/{victim.id}/loeschen")
        self.assertIsNone(User.query.filter_by(username="kzwiebelmeier").first())
        log = self.package()
        self.assertIn("Sign-in lock of user-", log)
        self.assertNotIn("kzwiebelmeier", log)

    def test_desk_search_text_is_not_in_the_package(self):
        self.make_user("chefin", role="admin")
        self.login("chefin")
        with mock.patch("assetlotse.views.loans.ad_search_available", return_value=True), \
                mock.patch("assetlotse.ldap_auth.Directory") as directory:
            directory.return_value.search_users.return_value = ([], False)
            self.c.get("/theke?modus=ausgabe&benutzer=Hildegard+Quaschning")
        log = self.package()
        self.assertIn("AD people search at the desk", log)
        self.assertNotIn("Quaschning", log)
        self.assertNotIn("Hildegard", log)

    def test_password_typed_as_user_name_is_not_in_the_package(self):
        from assetlotse import lockout
        from assetlotse.ldap_auth import LDAPError
        self.make_user("chefin", role="admin")
        with mock.patch("assetlotse.views.main.ldap_enabled", return_value=True), \
                mock.patch("assetlotse.views.main.authenticate", side_effect=LDAPError("user not found")):
            self.c.post("/login", data={"username": "Sommer2026!Kaffee", "password": "x"})
        LoginAttempt.query.update({LoginAttempt.created_at: datetime.now() - timedelta(days=2)})
        db.session.commit()
        lockout.cleanup()                      # the next day the lock table no longer knows the typed text
        log = self.package()
        self.assertIn("LDAP sign-in user-", log)
        self.assertNotIn("Sommer2026", log)
        self.assertNotIn("Kaffee", log)

    def test_user_names_that_are_program_words(self):
        self.make_user("chefin", role="admin")
        self.make_user("anna")                 # "anna" also occurs in the program source
        logging.getLogger("assetlotse.views.admin").info("Sign-in lock of %s lifted by %s",
                                                         applog.personal("anna"), applog.personal("chefin"))
        log = self.package()
        self.assertNotIn("anna", log)
        self.assertNotIn("chefin", log)

    def test_other_log_outputs_unchanged(self):
        # the container output (docker compose logs) shows the value as it is, without markers
        record = logging.LogRecord("assetlotse", logging.INFO, __file__, 1, "Sign-in of %s", (applog.personal("rita"),),
                                   None)
        self.assertEqual(logging.Formatter("%(message)s").format(record), "Sign-in of rita")
        self.assertEqual(record.args, ("rita",))

    def test_every_logged_user_name_is_marked(self):
        # guard for future log lines: user names and typed text in log calls go through applog.personal()
        missing = []
        for path in (ROOT / "assetlotse").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "log"):
                    for arg in node.args[1:]:
                        text = ast.unparse(arg)
                        if text.endswith("username") or text in ("query", "email", "to"):
                            missing.append(f"{path.name}:{node.lineno} {text}")
        self.assertEqual(missing, [])


class UpdateCheckAfterPatchDayTests(BaseCase):
    """After every patch day GitHub marks the security release (v2.9.0-s1) as "Latest": the update check failed
    every day ("no version") until the next program version – installations pinned to an older version heard
    nothing about newer ones."""

    def test_security_release_counts_as_its_program_version(self):
        import os

        from test_updatecheck import NEWER, github

        from assetlotse import __version__, updatecheck
        with mock.patch.dict(os.environ, {"ASSETLOTSE_UPDATE_CHECK": "", "ASSETLOTSE_SOURCE_URL": ""}):
            for tag, want in ((f"v{__version__}-s1", __version__), (f"v{NEWER}-s12", NEWER)):
                patch, _ = github(tag)
                with self.subTest(tag=tag), patch:
                    self.assertEqual(updatecheck.check_now(), (True, want))
            for tag in (f"v{NEWER}-rc1", f"v{NEWER}-s", f"v{NEWER}-s1-x"):
                patch, _ = github(tag)
                with self.subTest(tag=tag), patch:
                    self.assertFalse(updatecheck.check_now()[0])


class LabelFooterTests(BaseCase):
    """2.8.0: until the footer is saved, labels carry the organization name. But the settings page sends the
    prefilled footer with every save – saving anything froze the name, renaming the organization even froze
    the OLD name."""

    def form(self, page: str) -> dict:
        import re
        data = {}
        for m in re.finditer(r'<input[^>]*name="(label_footer(?:_shown)?)"[^>]*value="([^"]*)"', page):
            data[m.group(1)] = m.group(2)
        return data

    def test_renaming_the_organization_changes_the_footer(self):
        from assetlotse.labels import default_footer
        self.make_user("chefin", role="admin")
        self.login("chefin")
        data = self.form(self.c.get("/admin/einstellungen").get_data(as_text=True))
        self.assertEqual(data["label_footer"], "AssetLotse")
        self.c.post("/admin/einstellungen", data={**data, "org_name": "Institut für Physik", "loan_days": "14"})
        self.assertEqual(default_footer(), "Institut für Physik")
        # changed on purpose: saved, and from then on it no longer follows the name
        data = self.form(self.c.get("/admin/einstellungen").get_data(as_text=True))
        self.c.post("/admin/einstellungen", data={**data, "label_footer": "IT · 0123 456789",
                                                  "org_name": "Institut für Physik", "loan_days": "14"})
        self.c.post("/admin/einstellungen", data={**self.form(self.c.get("/admin/einstellungen").get_data(as_text=True)),
                                                  "org_name": "Physik", "loan_days": "14"})
        self.assertEqual(default_footer(), "IT · 0123 456789")
        # saved empty: no footer
        self.c.post("/admin/einstellungen", data={"label_footer": "", "label_footer_shown": "IT · 0123 456789",
                                                  "org_name": "Physik", "loan_days": "14"})
        self.assertEqual(default_footer(), "")


class EmergencyCommandCaseTests(BaseCase):
    """Sign-in ignores upper/lower case of the user name, the emergency commands did not:
    `make-admin mmuster` answered "User mmuster not found." for the AD account "MMuster"."""

    def test_make_admin_and_reset_ignore_case(self):
        u = self.make_user("MMuster")
        runner = self.app.test_cli_runner()
        r = runner.invoke(args=["make-admin", "mmuster"])
        self.assertEqual(r.exit_code, 0, r.output)
        db.session.expire_all()
        self.assertEqual(db.session.get(User, u.id).role, "admin")
        r = runner.invoke(args=["reset-2fa", "MMUSTER"])
        self.assertEqual(r.exit_code, 0, r.output)
        r = runner.invoke(args=["create-admin", "mmuster", "--password", "geheim1234567"])
        self.assertNotEqual(r.exit_code, 0)
        self.assertIn("already exists", r.output)


class AdminUserPageTests(BaseCase):
    """The user page counted passkeys as "security keys" and the reset question did not mention them."""

    def test_passkeys_counted_separately(self):
        from assetlotse.models import WebAuthnCredential
        self.make_user("chefin", role="admin")
        rita = self.make_user("rita")
        for i, passkey in enumerate((True, True, False)):
            db.session.add(WebAuthnCredential(user_id=rita.id, name=f"k{i}", rp_id="asset.example.org",
                                              credential_id=f"c{i}", credential_data="x", passkey=passkey))
        db.session.commit()
        self.login("chefin")
        page = self.c.get(f"/admin/benutzer/{rita.id}").get_data(as_text=True)
        self.assertIn("1 Sicherheitsschlüssel + 2 Passkeys", page)
        self.assertIn("Sicherheitsschlüssel und Passkeys werden entfernt", page)


class DiagnosticProxyTests(BaseCase):
    """The diagnostics read an environ key the proxy layer never sets ("assetlotse.proxied"): forged proxy headers
    from an untrusted sender looked the same as those of the real reverse proxy."""

    def request_section(self, environ, headers):
        import json
        r = self.c.post("/admin/diagnose/paket", headers=headers, environ_base=environ)
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            return json.loads(zf.read("diagnose.json"))["request"]

    def test_trusted_and_untrusted_sender(self):
        from assetlotse.proxy import install
        install(self.app, {})                 # the test app runs without the proxy layer
        self.make_user("chefin", role="admin")
        self.login("chefin")
        fwd = {"X-Forwarded-Proto": "http", "X-Forwarded-For": "203.0.113.7"}
        section = self.request_section({"REMOTE_ADDR": "198.51.100.9"}, fwd)
        self.assertEqual((section["via_proxy"], section["proxy_trusted"]), (True, False))
        section = self.request_section({"REMOTE_ADDR": "172.18.0.5"}, fwd)
        self.assertEqual((section["via_proxy"], section["proxy_trusted"]), (True, True))


class WorkflowTests(BaseCase):
    """The security update left out docs/ when checking for unreleased program changes – but docs/ goes into the
    image and the manuals are shown under Help: a newer manual on main would have shipped with an old program."""

    def test_security_update_counts_the_manuals(self):
        wf = (ROOT / ".github" / "workflows" / "docker.yml").read_text()
        self.assertNotIn("':(exclude)docs'", wf)
        self.assertIn("':(exclude)docs/README.md'", wf)
        self.assertIn("COPY docs ./docs", (ROOT / "Dockerfile").read_text())

    def test_no_weekly_security_update_texts(self):
        # since the patch day (second Tuesday) there is no Monday security update any more
        wf = (ROOT / ".github" / "workflows" / "docker.yml").read_text()
        self.assertNotIn("security update (Mondays)", wf)
        self.assertNotIn("next run (Monday", wf)
        self.assertNotIn("weekly security update", (ROOT / "scripts" / "ci.py").read_text())
