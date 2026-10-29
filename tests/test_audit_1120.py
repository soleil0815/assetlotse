"""Code-Audit 1.12.0: Verwalter nie nebenbei für alles zuständig, Ersteinrichtung nur direkt, E-Mail-Änderung
gehärtet, Theke/Verlängerung/Verfügbar bei „Kein Verleih“, Aufbewahrung nur nach erfolgreicher Sicherung,
Übersicht robust, Mail-Fußzeile, update.sh und Build-Kette."""
import os
import re
import subprocess
import sys
import types
from datetime import datetime
from pathlib import Path
from unittest import mock

from test_app import BaseCase
from test_assetlotse import Base

from assetlotse.models import Group, Loan, User, db, get_setting, set_setting

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ci    # noqa: E402
import lock  # noqa: E402


# ====================================================================== Rechte
class ManagerScopeTests(Base):
    def test_ad_sync_taking_the_last_group_does_not_make_a_global_manager(self):
        from assetlotse.views.main import sync_ldap_user
        self.it.ldap_dn = "CN=IT,OU=Gruppen,DC=institut,DC=local"
        self.vera.auth_source = "ldap"
        db.session.commit()
        hr_item = self.laptop(secret=None, group_id=self.hr.id)
        info = types.SimpleNamespace(username="vera", display_name="Vera", email="", dn="CN=vera", guid="",
                                     role="manager", groups=[])          # aus der AD-Gruppe IT entfernt
        sync_ldap_user(info, self.vera, group_sync=True)
        db.session.commit()
        self.assertEqual(self.vera.groups, [])
        self.assertFalse(self.vera.is_global_manager)
        self.assertFalse(self.vera.can_manage(hr_item))

    def test_admin_ticks_manages_all(self):
        self.login("admin")
        self.c.post(f"/admin/benutzer/{self.vera.id}", data={
            "username": "vera", "display_name": "Vera", "role": "manager", "active": "1", "manages_all": "1"})
        db.session.expire_all()
        u = db.session.get(User, self.vera.id)
        self.assertTrue(u.manages_all and u.is_global_manager)
        self.c.post(f"/admin/benutzer/{self.vera.id}", data={
            "username": "vera", "display_name": "Vera", "role": "reader", "active": "1", "manages_all": "1"})
        db.session.expire_all()
        self.assertFalse(db.session.get(User, self.vera.id).manages_all)     # nur für Verwalter


class SetupOnlyDirectTests(BaseCase):
    def test_forwarded_header_from_the_docker_bridge_is_ignored(self):
        r = self.c.get("/setup", headers={"X-Forwarded-For": "192.168.1.5"},
                       environ_base={"REMOTE_ADDR": "172.17.0.1"})
        self.assertEqual(r.status_code, 403)
        r = self.c.get("/setup", environ_base={"REMOTE_ADDR": "192.168.1.5"})     # direkt aus dem LAN
        self.assertEqual(r.status_code, 200)
        r = self.c.get("/setup", environ_base={"REMOTE_ADDR": "203.0.113.9"})     # direkt aus dem Internet
        self.assertEqual(r.status_code, 403)


# ====================================================================== E-Mail-Änderung
class MailChangeHardeningTests(Base):
    def setUp(self):
        super().setUp()
        for k, v in {"registration_mode": "open", "registration_verify_email": "1", "mail_enabled": "1",
                     "smtp_host": "mail.test", "app_base_url": "http://localhost", "registration_domains": ""}.items():
            set_setting(k, v)
        self.mia.self_registered = True
        self.mia.email = "mia@schule.de"
        db.session.commit()
        self.sent = []
        p = mock.patch("assetlotse.mailer.send_mail",
                       side_effect=lambda to, subject, text, html, **kw: self.sent.append((to, text)) or (True, ""))
        p.start()
        self.addCleanup(p.stop)

    def change(self, mail):
        return self.c.post("/profil", data={"action": "data", "display_name": "Mia", "email": mail},
                           follow_redirects=True).get_data(as_text=True)

    def link(self, n=-1):
        return re.search(r"http://localhost(/profil/email-bestaetigen/\S+)", self.sent[n][1]).group(1)

    def test_pending_shown_and_old_links_die(self):
        self.login("mia")
        html = self.change("b@schule.de")
        self.assertIn("Wartet auf Bestätigung", html)
        first = self.link()
        self.change("c@schule.de")                       # neue Vormerkung: der erste Link gilt nicht mehr
        self.assertEqual(self.c.get(first).status_code, 404)
        self.c.get(self.link())
        db.session.expire_all()
        u = db.session.get(User, self.mia.id)
        self.assertEqual((u.email, u.pending_email), ("c@schule.de", ""))
        self.assertEqual(self.c.get(self.link()).status_code, 404)      # nur einmal

    def test_rate_limit_and_no_free_text_in_the_mail(self):
        self.mia.display_name = "Klick hier: http://evil.example"
        db.session.commit()
        self.login("mia")
        for i in range(4):
            html = self.change(f"x{i}@schule.de")
        self.assertIn("Zu viele Änderungen", html)
        self.assertEqual(len(self.sent), 3)
        self.assertNotIn("evil.example", self.sent[0][1])

    def test_domain_checked_again_on_confirmation(self):
        self.login("mia")
        self.change("neu@fremd.org")
        link = self.link()
        set_setting("registration_domains", "schule.de")
        db.session.commit()
        self.assertIn("nicht erlaubt", self.c.get(link, follow_redirects=True).get_data(as_text=True))
        db.session.expire_all()
        self.assertEqual(db.session.get(User, self.mia.id).email, "mia@schule.de")

    def test_unique_for_every_local_account(self):
        self.vera.email = "vera@schule.de"
        db.session.commit()
        self.login("hans")                               # lokales Konto, nicht selbst registriert
        html = self.c.post("/profil", data={"action": "data", "display_name": "Hans", "email": "Vera@Schule.de"},
                           follow_redirects=True).get_data(as_text=True)
        self.assertIn("bereits von einem anderen Konto", html)

    def test_footer_about_notifications_only_in_loan_mails(self):
        from assetlotse.mailer import render_mail
        with self.app.test_request_context("/"):
            _s, text, html = render_mail("email_change", user=self.mia, link="http://x")
            self.assertNotIn("abschalten", html)
            a = self.laptop(secret=None)
            loan = Loan(asset_id=a.id, user_id=self.mia.id, issued_by_id=self.admin.id)
            db.session.add(loan)
            db.session.commit()
            _s, _t, html = render_mail("loan", user=self.mia, loan=loan, asset=a)
            self.assertIn("abschalten", html)


# ====================================================================== Theke, Verlängerung, Verfügbar
class LendingEdgeTests(Base):
    def setUp(self):
        super().setUp()
        self.fixed = Group(name="Fest", no_lending=True)
        db.session.add(self.fixed)
        db.session.commit()
        self.mia.groups.append(self.fixed)
        self.vera.groups.append(self.fixed)
        db.session.commit()

    def test_borrower_cannot_extend_no_lending_items(self):
        set_setting("self_extend", "1")
        a = self.laptop(secret=None, group_id=self.it.id)
        loan = Loan(asset_id=a.id, user_id=self.mia.id, issued_by_id=self.admin.id,
                    due_at=datetime.now().date())
        db.session.add(loan)
        a.group_id = self.fixed.id
        db.session.commit()
        self.login("mia")
        r = self.c.post(f"/ausleihen/{loan.id}/verlaengern", follow_redirects=True)
        self.assertIn("werden nicht verliehen", r.get_data(as_text=True))
        db.session.expire_all()
        self.assertEqual(db.session.get(Loan, loan.id).extensions, 0)

    def test_available_filter_and_dashboard_skip_no_lending(self):
        self.laptop(secret=None, name="Fest verbaut", group_id=self.fixed.id)
        self.login("vera")
        self.assertNotIn("Fest verbaut", self.c.get("/gegenstaende?status=verfuegbar").get_data(as_text=True))
        self.assertIn("Fest verbaut", self.c.get("/gegenstaende").get_data(as_text=True))

    def test_preset_disabled_when_the_person_already_has_it(self):
        a = self.laptop(secret=None, quantity=5)
        db.session.add(Loan(asset_id=a.id, user_id=self.mia.id, issued_by_id=self.admin.id))
        db.session.commit()
        self.login("vera")
        html = self.c.get(f"/theke?modus=ausgabe&gegenstand={a.id}&benutzer=mia").get_data(as_text=True)
        self.assertIn("hat ihn schon", html)
        self.assertRegex(html, r'<button class="btn primary lg" autofocus disabled')

    def test_ad_network_error_at_the_desk_gives_a_message(self):
        self.login("vera")
        with mock.patch("assetlotse.views.loans.ad_search_available", return_value=True), \
                mock.patch("assetlotse.ldap_auth.Directory.check_account", side_effect=RuntimeError("socket")):
            r = self.c.post("/theke/ad-konto", data={"username": "neu"}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("nicht erreichbar", r.get_data(as_text=True))


# ====================================================================== Hintergrund
class BackgroundTests(Base):
    def test_retention_waits_for_a_successful_backup(self):
        from assetlotse import retention
        set_setting("backup_enabled", "1")
        set_setting("backup_hour", "2")
        set_setting("backup_last_ok", "2026-10-27T02:00:00")
        db.session.commit()
        with mock.patch("assetlotse.backup.supported", return_value=True):
            self.assertIsNone(retention.run_daily(datetime(2026, 10, 28, 9, 0)))      # heute noch keine Sicherung
            self.assertEqual(get_setting("retention_last_run"), "")
            set_setting("backup_last_ok", "2026-10-28T02:00:05")
            db.session.commit()
            self.assertIsNotNone(retention.run_daily(datetime(2026, 10, 28, 9, 0)))

    def test_one_manager_failing_does_not_stop_the_overview(self):
        from datetime import date, timedelta

        from assetlotse import mailer
        for u in (self.admin, self.vera):
            u.email = f"{u.username}@x.de"
        a = self.laptop(secret=None)
        db.session.add(Loan(asset_id=a.id, user_id=self.mia.id, issued_by_id=self.admin.id,
                            due_at=date.today() - timedelta(days=2)))
        db.session.commit()
        calls = []

        def render(template, **kw):
            calls.append(kw["user"].username)
            if len(calls) == 1:
                raise RuntimeError("database is locked")
            return "s", "t", "h"
        with self.app.test_request_context("/"), mock.patch.object(mailer, "render_mail", side_effect=render), \
                mock.patch.object(mailer, "send_mail", return_value=(True, "")):
            self.assertEqual(mailer._send_digests({}, None, date.today()), 1)
        self.assertEqual(len(calls), 2)


# ====================================================================== Build-Kette und update.sh
class BuildChainTests(Base):
    def test_pip_is_a_calendar_version(self):
        self.assertIsNone(lock.upper_bound("26.2.2", "pip"))
        self.assertTrue(lock.within_limits("26.2.2", "27.0", "pip"))
        self.assertEqual(lock.upper_bound("26.2.2"), "27")                   # andere Pakete: Hauptversion

    def test_never_older_than_the_published_build(self):
        state = lock.read_state(ROOT)
        older = dict(state, patch=state["patch"] - 1, snapshot="20200101T000000Z",
                     pins=dict(state["pins"], flask=("Flask", "3.0.0")))
        problems = lock.older_than(state, older)
        self.assertEqual(len(problems), 3)
        self.assertEqual(lock.older_than(state, state), [])

    def test_exit_messages_become_annotations(self):
        env = dict(os.environ, GITHUB_ACTIONS="true", GITHUB_OUTPUT="/dev/null")
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "ci.py"), "plan-security", "--image", "i",
                            "--version", "1.0.0", "--git-tags", "/dev/null"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("::error title=scripts/ci.py::", r.stdout)

    def test_workflow_fixes(self):
        wf = (ROOT / ".github" / "workflows" / "docker.yml").read_text()
        self.assertIn("format('docker-other-{0}', github.run_id)", wf)              # check runs never queue-cancel
        self.assertIn("':(exclude).trivyignore'", wf)
        self.assertIn('python3 scripts/lock.py not-older --than "v${previous}"', wf)
        pub = wf[wf.index("\n  publish:\n"):wf.index("\n  repair:\n")]
        main_check = pub.index("git_auth ls-remote origin refs/heads/main")
        self.assertLess(main_check, pub.index('push_image', main_check))
        self.assertIn('if [ "$state" = "$local_state" ]', pub)                       # re-run continues
        repair = wf[wf.index("\n  repair:\n"):wf.index("\n  relock:\n")]
        self.assertIn("gh release create", repair)

    def test_update_script_knows_security_builds(self):
        # 2.0.0: the new update.sh takes -sN builds with --version; going back uses the build label (tests in
        # test_docker_standard.py)
        sh = (ROOT / "update.sh").read_text()
        self.assertIn("io.assetlotse.build", sh)
        self.assertIn("*[!0-9.s-]*", sh)
        df = (ROOT / "Dockerfile").read_text()
        self.assertIn("dpkg --purge", df)
        self.assertIn("! command -v apt-get", df)


# ====================================================================== Nachträge aus dem zweiten Review
class ReviewFollowUpTests(Base):
    def test_ad_demotion_clears_the_whole_inventory_flag(self):
        from assetlotse.views.main import sync_ldap_user
        self.vera.auth_source, self.vera.manages_all = "ldap", True
        db.session.commit()
        info = types.SimpleNamespace(username="vera", display_name="Vera", email="", dn="CN=vera", guid="",
                                     role="reader", groups=[])
        sync_ldap_user(info, self.vera, group_sync=False)
        self.assertFalse(self.vera.manages_all)
        info.role = "manager"                                    # später wieder Verwalter: nicht automatisch alles
        sync_ldap_user(info, self.vera, group_sync=False)
        self.assertFalse(self.vera.is_global_manager)

    def test_admin_changing_the_address_voids_a_pending_link(self):
        self.mia.pending_email = "alt@x.de"
        db.session.commit()
        self.login("admin")
        self.c.post(f"/admin/benutzer/{self.mia.id}", data={
            "username": "mia", "display_name": "Mia", "email": "neu@x.de", "role": "reader", "active": "1"})
        db.session.expire_all()
        self.assertEqual(db.session.get(User, self.mia.id).pending_email, "")

    def test_retention_waits_when_a_later_backup_failed(self):
        from assetlotse import retention
        for k, v in {"backup_enabled": "1", "backup_hour": "2", "backup_last_ok": "2026-10-28T01:00:00",
                     "backup_last_error": "2026-10-28T02:00:03"}.items():
            set_setting(k, v)
        db.session.commit()
        with mock.patch("assetlotse.backup.supported", return_value=True):
            self.assertIsNone(retention.run_daily(datetime(2026, 10, 28, 9, 0)))

    def test_publish_compares_the_archive_config_digest(self):
        wf = (ROOT / ".github" / "workflows" / "docker.yml").read_text()
        self.assertNotIn("docker inspect -f '{{.Id}}' assetlotse:scan", wf)
        self.assertIn('manifest.json | python3 -c', wf)
        self.assertIn("config_of()", wf)
