"""Code-Audit 1.9.2: AD-Fehler, Löschen mit Ausleihen, doppelte Rückgabe, Theke, DNS-Zwischenspeicher, Mail-Nachholen,
Sicherungsmeldungen, Konflikte beim Speichern, Gruppen löschen."""
import types
from unittest import mock

from test_assetlotse import Base

from assetlotse.models import Group, Loan, User, db, get_setting, set_setting


class LdapTransientErrorTests(Base):
    def _dir(self):
        from assetlotse.ldap_auth import Directory
        d = types.SimpleNamespace(base_dn="DC=x", user_attr="sAMAccountName", user_filter="",
                                  _search_failed=Directory._search_failed, TRANSIENT_CODES=Directory.TRANSIENT_CODES)
        return Directory, d

    def conn(self, code):
        return types.SimpleNamespace(search=lambda *a, **k: False, response=[],
                                     result={"result": code, "description": "busy", "message": ""})

    def test_busy_server_is_an_error_not_an_unknown_user(self):
        from assetlotse.ldap_auth import LDAPError, LDAPUserNotFound
        Directory, d = self._dir()
        with self.assertRaises(LDAPError) as cm:
            Directory.find_user(d, self.conn(51), "anna")
        self.assertNotIsInstance(cm.exception, LDAPUserNotFound)
        with self.assertRaises(LDAPUserNotFound):              # wirklich nicht gefunden: weiterhin „unbekannt“
            Directory.find_user(d, self.conn(0), "anna")
        with self.assertRaises(LDAPError):                     # Gruppen: vorübergehend → Abbruch statt „keine“
            Directory._search_dns(d, self.conn(52), "DC=x", "(x=1)")
        self.assertEqual(Directory._search_dns(d, self.conn(53), "DC=x", "(x=1)"), [])   # nicht unterstützt


class DataIntegrityTests(Base):
    def test_delete_user_never_removes_open_loans(self):
        from assetlotse.retention import OpenLoansError, delete_user
        a = self.laptop(secret=None)
        db.session.add(Loan(asset_id=a.id, user_id=self.mia.id, issued_by_id=self.admin.id))
        db.session.commit()
        with self.assertRaises(OpenLoansError):
            delete_user(self.mia)
        db.session.rollback()
        self.assertEqual(Loan.query.filter_by(user_id=self.mia.id).count(), 1)
        self.login("admin")
        r = self.c.post(f"/admin/benutzer/{self.mia.id}/loeschen", follow_redirects=True)
        self.assertIn("noch Gegenstände ausgegeben", r.get_data(as_text=True))
        self.assertIsNotNone(db.session.get(User, self.mia.id))

    def test_double_return_books_once(self):
        from flask_login import login_user

        from assetlotse.views.loans import return_loan
        a = self.laptop(secret=None)
        loan = Loan(asset_id=a.id, user_id=self.mia.id, issued_by_id=self.admin.id)
        db.session.add(loan)
        db.session.commit()
        with self.app.test_request_context("/"), mock.patch("assetlotse.views.loans.notify_return") as note:
            login_user(self.admin)
            first = return_loan(loan)
            stamp = db.session.get(Loan, loan.id).returned_at
            second = return_loan(loan)
        self.assertIn("zurückgenommen", first)
        self.assertIn("bereits zurückgegeben", second)
        self.assertEqual(note.call_count, 1)
        self.assertEqual(db.session.get(Loan, loan.id).returned_at, stamp)

    def test_group_delete_does_not_turn_manager_into_global_manager(self):
        self.login("admin")
        r = self.c.post(f"/admin/gruppen/{self.hr.id}/loeschen", follow_redirects=True)
        self.assertIn("nicht gelöscht", r.get_data(as_text=True))
        self.assertIsNotNone(db.session.get(Group, self.hr.id))
        empty = Group(name="Leer")
        db.session.add(empty)
        db.session.commit()
        self.c.post(f"/admin/gruppen/{empty.id}/loeschen")
        self.assertIsNone(db.session.get(Group, empty.id))

    def test_unique_conflict_gives_message_not_500(self):
        from sqlalchemy.exc import IntegrityError

        @self.app.route("/_konflikt", methods=["POST"])
        def _konflikt():
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))
        self.login("admin")
        r = self.c.post("/_konflikt", headers={"Referer": "http://localhost/standorte"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/standorte"))
        r = self.c.post("/_konflikt", headers={"Referer": "https://evil.example/x"})
        self.assertEqual(r.headers["Location"], "/")


class DeskTests(Base):
    def test_lookup_by_number_ignores_case_beyond_ascii(self):
        a = self.laptop(secret=None, inventory_no="Ü-12")
        db.session.add(Loan(asset_id=a.id, user_id=self.mia.id, issued_by_id=self.admin.id))
        db.session.commit()
        self.login("admin")
        html = self.c.get("/theke?modus=rueckgabe&code=ü-12").get_data(as_text=True)
        self.assertIn("Laptop Dell", html)
        self.assertNotIn("Kein Gegenstand", html)

    def test_auto_checkbox_is_remembered(self):
        self.login("admin")
        self.assertIn('name="auto" value="1" checked', self.c.get("/theke?modus=rueckgabe").get_data(as_text=True))
        self.c.post("/theke", data={"modus": "rueckgabe", "code": "nichts"})          # Häkchen abgewählt
        html = self.c.get("/theke?modus=rueckgabe").get_data(as_text=True)
        self.assertNotIn('name="auto" value="1" checked', html)
        self.c.post("/theke", data={"modus": "rueckgabe", "code": "nichts", "auto": "1"})
        self.assertIn('name="auto" value="1" checked', self.c.get("/theke?modus=rueckgabe").get_data(as_text=True))

    def test_unknown_mode_falls_back_to_issuing(self):
        self.login("admin")
        html = self.c.get("/theke?modus=quatsch").get_data(as_text=True)
        self.assertIn('class="on"', html)


class NetzoneDnsTests(Base):
    def test_failed_lookup_is_cached_too(self):
        from assetlotse import netzone
        netzone._cache.clear()
        with mock.patch("socket.getaddrinfo", side_effect=OSError("dns down")) as gai:
            for t in (1000.0, 1001.0, 1002.0):
                self.assertEqual(netzone._resolve("dyn.example.org", t), set())
        self.assertEqual(gai.call_count, 1)
        netzone._cache.clear()


class MailRetryTests(Base):
    def test_failed_mails_release_the_day_a_few_times(self):
        from datetime import datetime

        from assetlotse import mailer
        set_setting("mail_enabled", "1")
        set_setting("smtp_host", "mail.test")
        db.session.commit()
        now = datetime(2026, 10, 24, 9, 0)
        with mock.patch.object(mailer, "run_notifications", return_value={"sent": 2, "errors": 1, "loans": 3}):
            for i in range(1, 4):
                set_setting("mail_retry_after", "")
                db.session.commit()
                self.assertIsNotNone(mailer.run_scheduled(now))
                self.assertEqual(get_setting("mail_last_run"), "")          # Tag wieder frei
                self.assertEqual(get_setting("mail_retries"), f"2026-10-24:{i}")
            set_setting("mail_retry_after", "")
            db.session.commit()
            mailer.run_scheduled(now)
            self.assertEqual(get_setting("mail_last_run"), "2026-10-24")    # nach 3 Versuchen ist Schluss

    def test_one_broken_person_does_not_stop_the_rest(self):
        from datetime import date, timedelta

        from assetlotse import mailer
        set_setting("mail_enabled", "1")
        set_setting("smtp_host", "mail.test")
        set_setting("mail_overdue", "1")
        db.session.commit()
        a = self.laptop(secret=None)
        b = self.laptop(secret=None)
        for asset, user in ((a, self.mia), (b, self.vera)):
            db.session.add(Loan(asset_id=asset.id, user_id=user.id, issued_by_id=self.admin.id,
                                due_at=date.today() - timedelta(days=3)))
        db.session.commit()
        calls = []

        def remind(user, bucket, cfg, smtp):
            calls.append(user.username)
            if len(calls) == 1:
                raise RuntimeError("kaputte Vorlage")
            return True
        with mock.patch.object(mailer, "smtp_connect", return_value=mock.MagicMock()) as conn, \
                mock.patch.object(mailer, "_remind", side_effect=remind):
            stats = mailer.run_notifications(date.today())
        self.assertEqual(len(calls), 2)
        self.assertEqual((stats["sent"], stats["errors"]), (1, 1))
        self.assertEqual(conn.call_count, 2)                                  # einmal neu verbunden


class BackupMessageTests(Base):
    def test_messages_in_english(self):
        from assetlotse.backup import human_size, localized_message
        with self.app.test_request_context("/"):
            from flask import g
            g._assetlotse_locale = "en"
            self.assertEqual(localized_message("assetlotse-x.db gesichert (12,3 MB)"),
                             "assetlotse-x.db backed up (12.3 MB)")
            self.assertIn("not found", localized_message("Datenbankdatei /data/a.db nicht gefunden."))
            self.assertEqual(human_size(13 * 1024 * 1024, "en"), "13.0 MB")
        self.assertEqual(human_size(13 * 1024 * 1024, "de"), "13,0 MB")
