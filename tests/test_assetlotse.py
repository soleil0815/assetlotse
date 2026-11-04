"""AssetLotse-specific: confidential details, open-ended issue, templates, categories, number search."""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-test-"))

from assetlotse import create_app  # noqa: E402
from assetlotse.models import (Asset, Group, Loan, SecretAccess, User, db, get_setting,  # noqa: E402
                               set_setting)

PW = "geheim12345"
KEY = "448899-112233-556677-001122-334455-667788-990011-223344"


class Base(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from assetlotse.models import ensure_indexes
        ensure_indexes()   # as on a real start: among other things unique inventory/serial numbers
        from flask import g

        def _fresh():
            g.pop("_login_user", None)
            g.pop("_assetlotse_locale", None)
        self.app.before_request_funcs.setdefault(None, []).insert(0, _fresh)
        self.it, self.hr = Group(name="IT"), Group(name="Personal")
        db.session.add_all([self.it, self.hr])
        db.session.commit()
        self.admin = self.make("admin", "admin")
        self.vera = self.make("vera", "manager", [self.it])       # manager of the IT group
        self.hans = self.make("hans", "manager", [self.hr])       # manager of the Personal group
        self.mia = self.make("mia", "reader", [self.it])
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def make(self, name, role, groups=()):
        u = User(username=name, display_name=name.title(), role=role, email=f"{name}@example.org")
        u.set_password(PW)
        u.groups = list(groups)
        db.session.add(u)
        db.session.commit()
        return u

    def login(self, name):
        self.c = self.app.test_client()
        r = self.c.post("/login", data={"username": name, "password": PW})
        self.assertEqual(r.status_code, 302)

    def laptop(self, secret=KEY, **kw):
        # inventory/serial numbers are unique: the first laptop gets IT-0042/SN-1, further ones consecutive numbers
        n = Asset.query.count()
        a = Asset(name=kw.pop("name", "Laptop Dell"), category="Laptop",
                  inventory_no=kw.pop("inventory_no", "IT-0042" if n == 0 else f"IT-{42 + n:04d}"),
                  serial_number=kw.pop("serial_number", "SN-1" if n == 0 else f"SN-{n + 1}"),
                  group_id=kw.pop("group_id", self.it.id), **kw)
        if secret:
            a.set_secret(secret)
        db.session.add(a)
        db.session.commit()
        return a


class SecretTests(Base):
    def test_stored_encrypted_and_readable(self):
        a = self.laptop()
        self.assertTrue(a.secret.startswith("enc:"))
        self.assertNotIn("448899", a.secret)
        self.assertEqual(a.get_secret(), KEY)
        self.assertIsNotNone(a.secret_updated_at)

    def test_create_with_secret_via_form_is_logged(self):
        self.login("vera")
        self.c.post("/gegenstaende/neu", data={"name": "USB-Stick", "secret": "PIN 4711", "group_id": self.it.id,
                                               "quantity": "1"})
        a = Asset.query.one()
        self.assertEqual(a.get_secret(), "PIN 4711")
        self.assertEqual([(e.user_id, e.action) for e in a.secret_log], [(self.vera.id, "change")])

    def test_not_in_detail_page_until_revealed(self):
        a = self.laptop()
        self.login("vera")
        html = self.c.get(f"/gegenstaende/{a.id}").get_data(as_text=True)
        self.assertNotIn(KEY, html)
        self.assertIn("Vertrauliche Angaben", html)
        r = self.c.post(f"/gegenstaende/{a.id}/vertraulich")
        self.assertEqual(r.status_code, 200)
        self.assertIn(KEY, r.get_data(as_text=True))
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")
        self.assertEqual([(e.user_id, e.action) for e in SecretAccess.query], [(self.vera.id, "view")])
        # the log is shown on the detail page
        self.assertIn("angezeigt", self.c.get(f"/gegenstaende/{a.id}").get_data(as_text=True))

    def test_only_managers_of_this_item(self):
        a = self.laptop()
        self.login("mia")
        html = self.c.get(f"/gegenstaende/{a.id}").get_data(as_text=True)
        self.assertNotIn("Vertrauliche Angaben", html)
        self.assertEqual(self.c.post(f"/gegenstaende/{a.id}/vertraulich").status_code, 403)
        self.login("hans")   # manager of another group
        self.assertEqual(self.c.post(f"/gegenstaende/{a.id}/vertraulich").status_code, 403)
        self.assertEqual(SecretAccess.query.count(), 0)
        self.login("admin")
        self.assertIn(KEY, self.c.post(f"/gegenstaende/{a.id}/vertraulich").get_data(as_text=True))

    def test_reveal_needs_post(self):
        a = self.laptop()
        self.login("vera")
        self.assertEqual(self.c.get(f"/gegenstaende/{a.id}/vertraulich").status_code, 405)

    def test_edit_form_never_contains_secret_and_empty_field_keeps_it(self):
        a = self.laptop()
        self.login("vera")
        html = self.c.get(f"/gegenstaende/{a.id}/bearbeiten").get_data(as_text=True)
        self.assertNotIn(KEY, html)
        self.assertIn("bereits vertrauliche Angaben hinterlegt", html)
        self.c.post(f"/gegenstaende/{a.id}/bearbeiten", data={"name": "Laptop Dell 2", "group_id": self.it.id,
                                                               "quantity": "1", "secret": ""})
        db.session.refresh(a)
        self.assertEqual((a.name, a.get_secret()), ("Laptop Dell 2", KEY))
        self.assertEqual(SecretAccess.query.count(), 0)

    def test_replace_and_delete(self):
        a = self.laptop()
        self.login("vera")
        self.c.post(f"/gegenstaende/{a.id}/bearbeiten", data={"name": a.name, "group_id": self.it.id,
                                                               "quantity": "1", "secret": "neu"})
        db.session.refresh(a)
        self.assertEqual(a.get_secret(), "neu")
        self.c.post(f"/gegenstaende/{a.id}/bearbeiten", data={"name": a.name, "group_id": self.it.id,
                                                               "quantity": "1", "remove_secret": "1"})
        db.session.refresh(a)
        self.assertEqual((a.secret, a.has_secret, a.secret_updated_at), ("", False, None))
        self.assertEqual([e.action for e in SecretAccess.query.order_by(SecretAccess.id)], ["change", "delete"])

    def test_never_exported(self):
        self.laptop()
        self.login("admin")
        text = self.c.get("/gegenstaende/export.csv").get_data(as_text=True)
        self.assertNotIn(KEY, text)
        self.assertNotIn("enc:", text)
        self.assertIn("IT-0042", text)

    def test_wrong_key_gives_message_not_error(self):
        a = self.laptop()
        a.secret = "enc:kaputt"
        db.session.commit()
        self.login("vera")
        r = self.c.post(f"/gegenstaende/{a.id}/vertraulich", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("nicht lesbar", r.get_data(as_text=True))
        self.assertEqual(SecretAccess.query.count(), 0)

    def test_access_log_follows_retention(self):
        from assetlotse.retention import run_retention
        a = self.laptop()
        db.session.add_all([SecretAccess(asset_id=a.id, user_id=self.vera.id, at=datetime.now() - timedelta(days=500)),
                            SecretAccess(asset_id=a.id, user_id=self.vera.id, at=datetime.now())])
        db.session.commit()
        res = run_retention()
        self.assertEqual(res["secret_log"], 1)
        self.assertEqual(SecretAccess.query.count(), 1)

    def test_log_survives_deleting_item_and_user(self):
        a = self.laptop()
        self.login("vera")
        self.c.post(f"/gegenstaende/{a.id}/vertraulich")
        self.c.post(f"/gegenstaende/{a.id}/loeschen")
        self.assertEqual(Asset.query.count(), 0)
        entries = SecretAccess.query.order_by(SecretAccess.id).all()
        self.assertEqual([e.action for e in entries], ["view", "remove"])
        self.assertEqual({e.asset_id for e in entries}, {None})
        self.assertEqual(entries[0].asset_label, "Laptop Dell (IT-0042)")
        self.assertEqual(entries[0].user_label, "Vera (vera)")

    def test_general_items_only_for_admins(self):
        """“General” does not open secrets – not even for managers without a group."""
        a = self.laptop(group_id=None)
        boss = self.make("boss", "manager")          # manager without a group (manages the entire stock)
        for name in ("vera", "hans", "boss"):
            self.login(name)
            with self.subTest(name=name):
                self.assertEqual(self.c.post(f"/gegenstaende/{a.id}/vertraulich").status_code, 403)
                self.assertNotIn("Anzeigen</button>", self.c.get(f"/gegenstaende/{a.id}").get_data(as_text=True))
        # may still edit the item, but not overwrite the secret
        self.login("boss")
        r = self.c.post(f"/gegenstaende/{a.id}/bearbeiten", data={"name": "Neu", "quantity": "1", "secret": "x"},
                        follow_redirects=True)
        self.assertIn("nicht gespeichert", r.get_data(as_text=True))
        db.session.refresh(a)
        self.assertEqual((a.name, a.get_secret()), ("Neu", KEY))
        self.login("admin")
        self.assertIn(KEY, self.c.post(f"/gegenstaende/{a.id}/vertraulich").get_data(as_text=True))

    def test_losing_last_group_does_not_open_secrets(self):
        a = self.laptop()
        other = self.laptop(group_id=self.hr.id, inventory_no="HR-1")
        self.login("hans")
        self.assertEqual(self.c.post(f"/gegenstaende/{other.id}/vertraulich").status_code, 200)
        self.c.post("/logout")
        self.login("admin")
        self.c.post(f"/admin/gruppen/{self.hr.id}/loeschen", follow_redirects=True)
        self.assertIsNone(db.session.get(Group, self.hr.id))     # this way hans loses his only group
        self.c.post("/logout")
        self.login("hans")
        for x in (a, other):
            with self.subTest(asset=x.name):
                self.assertEqual(self.c.post(f"/gegenstaende/{x.id}/vertraulich").status_code, 403)


class OpenEndedLoanTests(Base):
    def test_defaults(self):
        self.assertEqual(get_setting("loan_days"), "0")
        self.assertEqual(get_setting("self_checkout"), "0")
        self.assertEqual(get_setting("org_name"), "AssetLotse")

    def test_issue_without_due_date_by_default(self):
        a = self.laptop()
        self.login("vera")
        r = self.c.post(f"/gegenstaende/{a.id}/ausgeben", data={"user_id": self.mia.id}, follow_redirects=True)
        self.assertIn("unbefristet", r.get_data(as_text=True))
        loan = Loan.query.one()
        self.assertIsNone(loan.due_at)
        self.assertFalse(loan.is_overdue)
        self.assertIsNone(loan.days_left)

    def test_due_date_or_open_ended_by_choice(self):
        set_setting("loan_days", "14")
        db.session.commit()
        a, b, c = self.laptop(), self.laptop(name="B", inventory_no="2"), self.laptop(name="C", inventory_no="3")
        self.login("vera")
        self.c.post(f"/gegenstaende/{a.id}/ausgeben", data={"user_id": self.mia.id})
        self.c.post(f"/gegenstaende/{b.id}/ausgeben", data={"user_id": self.mia.id, "no_due": "1"})
        self.c.post(f"/gegenstaende/{c.id}/ausgeben", data={"user_id": self.mia.id, "due_at": "2030-05-01"})
        due = {l.asset_id: l.due_at for l in Loan.query}
        self.assertEqual(due, {a.id: date.today() + timedelta(days=14), b.id: None, c.id: date(2030, 5, 1)})

    def test_open_ended_never_overdue_extended_or_reminded(self):
        from assetlotse.mailer import collect_due, send_manual_reminder
        set_setting("mail_reminder", "1")
        set_setting("mail_overdue", "1")
        db.session.commit()
        a = self.laptop()
        loan = Loan(asset_id=a.id, user_id=self.mia.id, due_at=None, loaned_at=datetime.now() - timedelta(days=900))
        db.session.add(loan)
        db.session.commit()
        self.assertEqual(collect_due(), {})
        with self.app.test_request_context():
            set_setting("mail_enabled", "1")
            set_setting("smtp_host", "localhost")
            ok, msg = send_manual_reminder(loan)
        self.assertFalse(ok)
        self.login("mia")
        self.c.post(f"/ausleihen/{loan.id}/verlaengern")
        db.session.refresh(loan)
        self.assertEqual((loan.due_at, loan.extensions), (None, 0))
        self.login("admin")
        self.assertNotIn("Laptop Dell", self.c.get("/ausleihen?ansicht=ueberfaellig").get_data(as_text=True))
        html = self.c.get("/ausleihen").get_data(as_text=True)
        self.assertIn("unbefristet", html)
        self.assertNotIn("Verlängern</button>", html)

    def test_staff_cannot_take_items_without_self_checkout(self):
        a = self.laptop()
        self.login("mia")
        html = self.c.get(f"/gegenstaende/{a.id}").get_data(as_text=True)
        self.assertNotIn("Für mich ausleihen", html)
        self.assertIn("von der Verwaltung", html)
        self.assertEqual(self.c.post(f"/gegenstaende/{a.id}/ausgeben").status_code, 403)
        self.assertEqual(Loan.query.count(), 0)

    def test_my_items_shows_issued_items(self):
        a = self.laptop()
        db.session.add(Loan(asset_id=a.id, user_id=self.mia.id, due_at=None, note="mit Netzteil"))
        db.session.commit()
        self.login("mia")
        html = self.c.get("/ausleihen").get_data(as_text=True)
        self.assertIn("Meine Gegenstände", html)
        self.assertIn("Laptop Dell", html)
        self.assertIn("mit Netzteil", html)
        self.assertIn("Dir ist 1 Gegenstand ausgegeben.", self.c.get("/").get_data(as_text=True))


class FormTests(Base):
    def test_template_copies_details_and_photo_but_not_numbers_or_secret(self):
        a = self.laptop(manufacturer="Dell", model="Latitude 5440", location="", description="mit Netzteil")
        a.image_file = "abc.jpg"
        db.session.commit()
        self.login("vera")
        html = self.c.get(f"/gegenstaende/neu?vorlage={a.id}").get_data(as_text=True)
        for text in ('value="Laptop Dell"', 'value="Latitude 5440"', "mit Netzteil", 'value="Laptop"', "abc.jpg",
                     f'name="image_from" value="{a.id}"'):
            self.assertIn(text, html)
        for text in ('value="IT-0042"', 'value="SN-1"', KEY):
            self.assertNotIn(text, html)

    def test_template_of_foreign_group_is_ignored(self):
        a = self.laptop(group_id=self.hr.id, manufacturer="Geheimfirma")
        self.login("vera")
        self.assertNotIn("Geheimfirma", self.c.get(f"/gegenstaende/neu?vorlage={a.id}").get_data(as_text=True))

    def test_save_and_next_opens_template(self):
        self.login("vera")
        r = self.c.post("/gegenstaende/neu", data={"name": "USB-Stick", "category": "USB-Stick", "inventory_no": "U-1",
                                                   "group_id": self.it.id, "quantity": "1", "next_action": "another"})
        new = Asset.query.one()
        self.assertTrue(r.headers["Location"].endswith(f"/gegenstaende/neu?vorlage={new.id}"))

    def test_duplicate_numbers_rejected(self):
        self.laptop()
        self.login("vera")
        for field, value in (("inventory_no", "IT-0042"), ("inventory_no", "it-0042 "), ("serial_number", "sn-1")):
            r = self.c.post("/gegenstaende/neu", data={"name": "Zweiter", field: value, "group_id": self.it.id,
                                                       "quantity": "1"}, follow_redirects=True)
            html = r.get_data(as_text=True)
            self.assertIn("ist bereits vergeben (bei „Laptop Dell“)", html)
            self.assertIn('value="Zweiter"', html)          # input stays in the form
            self.assertEqual(Asset.query.count(), 1)

    def test_name_required(self):
        self.login("vera")
        r = self.c.post("/gegenstaende/neu", data={"name": " ", "quantity": "1"}, follow_redirects=True)
        self.assertIn("Bezeichnung ist erforderlich", r.get_data(as_text=True))
        self.assertEqual(Asset.query.count(), 0)


class InventoryTests(Base):
    def test_category_filter_and_suggestions(self):
        self.laptop(secret="")
        db.session.add(Asset(name="Headset", category="Audio", quantity=5))
        db.session.commit()
        self.login("vera")
        html = self.c.get("/gegenstaende?kategorie=Audio").get_data(as_text=True)
        self.assertIn("Headset", html)
        self.assertNotIn("Laptop Dell", html)
        form = self.c.get("/gegenstaende/neu").get_data(as_text=True)
        self.assertIn('<option value="Audio">', form)
        self.assertIn('<option value="Laptop">', form)

    def test_search_by_serial_and_inventory_number(self):
        self.laptop(secret="", serial_number="5CG3381XYZ")
        self.login("mia")
        self.assertIn("Laptop Dell", self.c.get("/gegenstaende?q=5cg3381").get_data(as_text=True))
        self.assertIn("Laptop Dell", self.c.get("/gegenstaende?q=IT-0042").get_data(as_text=True))

    def test_desk_issue_by_serial_number(self):
        self.laptop(secret="", serial_number="5CG3381XYZ")
        self.login("vera")
        r = self.c.post("/theke", data={"modus": "ausgabe", "benutzer": "mia", "code": "5cg3381xyz", "auto": "1"},
                        follow_redirects=True)
        self.assertIn("an Mia ausgegeben", r.get_data(as_text=True))
        self.assertEqual(Loan.query.one().user_id, self.mia.id)

    def test_export_lists_holder(self):
        a = self.laptop(secret="")
        db.session.add(Loan(asset_id=a.id, user_id=self.mia.id, due_at=None))
        db.session.commit()
        self.login("vera")
        lines = self.c.get("/gegenstaende/export.csv").get_data(as_text=True).strip().splitlines()
        self.assertEqual(lines[0].lstrip("﻿").split(";")[-1], "Ausgegeben an")
        self.assertEqual(lines[1].split(";")[-1], "Mia")

    def test_pages_render_in_both_languages(self):
        a = self.laptop()
        db.session.add(Loan(asset_id=a.id, user_id=self.mia.id, due_at=None))
        db.session.commit()
        self.login("admin")
        for lang in ("de", "en"):
            self.c.post(f"/sprache/{lang}", data={"next": "/"})
            for url in ["/", "/gegenstaende", f"/gegenstaende/{a.id}", f"/gegenstaende/{a.id}/bearbeiten",
                        "/gegenstaende/neu", "/ausleihen", "/theke", "/theke?modus=rueckgabe", "/hilfe", "/info"]:
                with self.subTest(lang=lang, url=url):
                    self.assertEqual(self.c.get(url).status_code, 200)
            self.assertEqual(self.c.post(f"/gegenstaende/{a.id}/vertraulich").status_code, 200)


if __name__ == "__main__":
    unittest.main()


class LabelTests(Base):
    def test_code128_symbols(self):
        from assetlotse import labels as L
        self.assertEqual(len(L._PATTERNS), 107)
        for i, p in enumerate(L._PATTERNS):
            self.assertEqual(sum(map(int, p)), 13 if i == 106 else 11)
        v = L.code128_values("IT-0042")
        self.assertEqual(v[0], L.START_B)
        self.assertEqual(v[-1], L.STOP)
        self.assertEqual(v[-2], (v[0] + sum(i * x for i, x in enumerate(v[1:-2], start=1))) % 103)
        self.assertEqual(L.code128_values("20240042")[0], L.START_C)        # digits compact as Code C
        with self.assertRaises(ValueError):
            L.code128_values("Ü-12")

    def test_barcode_value_falls_back_to_internal_number(self):
        from assetlotse import labels as L
        a = self.laptop(secret="")
        self.assertEqual(L.barcode_value(a), "IT-0042")
        a.inventory_no = "Ü-12"
        self.assertEqual(L.barcode_value(a), f"#{a.id}")
        a.inventory_no = ""
        self.assertEqual(L.barcode_value(a), f"#{a.id}")

    def _pages(self, pdf: bytes) -> int:
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        import re
        return int(re.search(rb"/Type /Pages /Count (\d+)", pdf).group(1))

    def test_single_label_pdf(self):
        a = self.laptop()
        self.login("vera")
        r = self.c.get(f"/gegenstaende/{a.id}/etikett.pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/pdf")
        self.assertEqual(self._pages(r.data), 1)
        self.assertIn(b"(IT-0042) Tj", r.data)
        self.assertNotIn(b"448899", r.data)          # confidential details never on the label
        self.assertIn(b"/MediaBox [0 0 175.75 82.2]", r.data)   # 62 × 29 mm
        r = self.c.get(f"/gegenstaende/{a.id}/etikett.pdf?anzahl=3")
        self.assertEqual(self._pages(r.data), 3)
        self.assertEqual(self._pages(self.c.get(f"/gegenstaende/{a.id}/etikett.pdf?anzahl=999").data), 50)

    def test_labels_for_selection(self):
        self.laptop(secret="")
        self.laptop(secret="", name="Zweiter", inventory_no="IT-0043")
        db.session.add(Asset(name="Headset", category="Audio"))
        db.session.add(Asset(name="Fremd", category="Laptop", group_id=self.hr.id))
        db.session.commit()
        self.login("vera")
        r = self.c.get("/gegenstaende/etiketten.pdf?kategorie=Laptop")
        self.assertEqual(self._pages(r.data), 2)       # own group only, laptops only
        self.assertNotIn(b"Fremd", r.data)
        r = self.c.get("/gegenstaende/etiketten.pdf?kategorie=Gibtsnicht", follow_redirects=True)
        self.assertIn("Keine Gegenstände für Etiketten", r.get_data(as_text=True))

    def test_labels_only_for_managers(self):
        a = self.laptop()
        self.login("mia")
        self.assertEqual(self.c.get(f"/gegenstaende/{a.id}/etikett.pdf").status_code, 403)
        self.assertEqual(self.c.get("/gegenstaende/etiketten.pdf").status_code, 403)
        self.login("hans")
        self.assertEqual(self.c.get(f"/gegenstaende/{a.id}/etikett.pdf").status_code, 403)

    def test_umlauts_and_special_characters_in_text(self):
        a = self.laptop(secret="", name="Schlüssel (Haus) \\ Ä", inventory_no="Ü-12")
        self.login("admin")
        pdf = self.c.get(f"/gegenstaende/{a.id}/etikett.pdf").data
        self.assertIn(b"Schl\\374ssel \\(Haus\\) \\\\ \\304", pdf)
        self.assertIn(f"(\\334-12 \\(#{a.id}\\)) Tj".encode(), pdf)   # inventory number readable, barcode #ID


class DashboardTests(Base):
    def test_key_figures_and_categories(self):
        a = self.laptop(secret="")
        h = Asset(name="Headset", category="Audio", quantity=4, group_id=self.it.id)
        db.session.add(h)
        db.session.commit()
        db.session.add_all([Loan(asset_id=a.id, user_id=self.mia.id, due_at=date.today() - timedelta(days=2)),
                            Loan(asset_id=h.id, user_id=self.mia.id, due_at=date.today() + timedelta(days=3))])
        db.session.commit()
        self.login("vera")
        html = self.c.get("/").get_data(as_text=True)
        self.assertIn("Bestand nach Kategorie", html)
        self.assertIn("40 % ausgelastet", html)          # 2 of 5 pieces issued
        self.assertIn("1 in den nächsten 7 Tagen fällig", html)
        self.assertIn('style="width: 25%"', html)         # Audio: 1 of 4
        self.login("mia")
        self.assertNotIn("Bestand nach Kategorie", self.c.get("/").get_data(as_text=True))


class ReviewRegressionTests(Base):
    """Findings from the 1.1.0 code review."""

    def test_moving_item_into_own_group_does_not_reveal_secret(self):
        a = self.laptop(group_id=None)                      # “General”: key for admins only
        self.login("vera")
        self.c.post(f"/gegenstaende/{a.id}/bearbeiten", data={"name": a.name, "quantity": "1",
                                                               "group_id": self.it.id, "secret": "überschrieben"})
        db.session.refresh(a)
        self.assertIsNone(a.group_id)
        self.assertEqual(a.get_secret(), KEY)
        self.assertEqual(self.c.post(f"/gegenstaende/{a.id}/vertraulich").status_code, 403)

    def test_manager_cannot_delete_item_with_hidden_secret(self):
        a = self.laptop(group_id=None)
        self.login("vera")
        self.c.post(f"/gegenstaende/{a.id}/loeschen")
        self.assertEqual(Asset.query.count(), 1)

    def test_hash_code_is_internal_id_not_name_search(self):
        beamer = Asset(name="Beamer", quantity=1)
        other = Asset(name="Leihgerät #4x", quantity=1)
        db.session.add_all([beamer, other])
        db.session.commit()
        self.login("admin")
        self.c.post("/theke", data={"modus": "ausgabe", "benutzer": "mia", "code": f"#{beamer.id}", "auto": "1"})
        self.assertEqual([l.asset_id for l in Loan.query], [beamer.id])
        # name search only offers a choice – never an automatic issue
        self.c.post("/theke", data={"modus": "ausgabe", "benutzer": "vera", "code": "Leihg", "auto": "1"})
        self.assertEqual(Loan.query.count(), 1)

    def test_inventory_number_must_not_start_with_hash(self):
        self.login("admin")
        r = self.c.post("/gegenstaende/neu", data={"name": "X", "inventory_no": "#7", "quantity": "1"},
                        follow_redirects=True)
        self.assertIn("nicht mit „#“ beginnen", r.get_data(as_text=True))
        self.assertEqual(Asset.query.count(), 0)

    def test_staff_do_not_see_who_has_items(self):
        a = self.laptop(secret="")
        db.session.add(Loan(asset_id=a.id, user_id=self.vera.id, due_at=None))
        db.session.commit()
        self.login("mia")
        html = self.c.get("/gegenstaende").get_data(as_text=True)
        self.assertNotIn("Ausgegeben an", html)
        self.assertNotIn("Vera", html.split("<main")[1])
        self.login("vera")
        self.assertIn("Vera", self.c.get("/gegenstaende").get_data(as_text=True).split("<main")[1])

    def test_long_inventory_number_falls_back_but_is_printed(self):
        from assetlotse import labels as L
        a = self.laptop(secret="", inventory_no="INSTITUT-IT-2026-000042")
        self.assertEqual(L.barcode_value(a), f"#{a.id}")
        self.assertEqual(L.label_for(a).text, f"INSTITUT-IT-2026-000042 (#{a.id})")
        a.inventory_no = "1234567890123456"                 # digits: compact as Code C, is kept
        self.assertEqual(L.barcode_value(a), "1234567890123456")

    def test_text_fits_with_umlauts_and_foreign_letters(self):
        from assetlotse import labels as L
        self.assertEqual(L.printable("Łódź Bu\u0308ro"), "Lódz Büro")   # ó exists in cp1252, ź does not; an NFD umlaut is composed
        inner = (L.LABEL_W_MM - 2 * L.MARGIN_MM) * L.MM
        t = L.fit("Überwachungsmonitor ÖÄÜ Größenverstellbar für Übungsräume", 9, inner, True)
        self.assertTrue(t.endswith("…"))
        self.assertLessEqual(L.text_width(t, 9, True), inner)


class _FakeConn:
    """Minimal LDAP stand-in for the people search: the name search (filter with *) returns the fixed entries
    (respecting size_limit), group queries return nothing – the groups come from memberOf."""

    def __init__(self, entries):
        self.entries, self.response, self.filters = entries, [], []

    def search(self, base, flt, scope, attributes=None, size_limit=0):
        self.filters.append(flt)
        if "=*" not in flt:
            self.response = []
            return False
        rows = self.entries[:size_limit] if size_limit else self.entries
        self.response = [{"type": "searchResEntry", "dn": dn, "attributes": a, "raw_attributes": {}}
                         for dn, a in rows]
        return True

    def unbind(self):
        pass


AD_CFG = {"ldap_server": "dc1.institut.local", "ldap_base_dn": "OU=Institut,DC=institut,DC=local",
          "ldap_bind_dn": "CN=svc,DC=institut,DC=local", "ldap_bind_password": "x", "ldap_user_attr": "sAMAccountName",
          "ldap_user_filter": "(objectClass=user)", "ldap_nested_groups": "0",
          "ldap_login_rules": "AL-Mitarbeiter", "ldap_manager_rules": "OU=IT,OU=Institut,DC=institut,DC=local",
          "ldap_admin_rules": "AL-Admins"}


def _ad_entry(sam, name, groups=(), ou="OU=Personal,OU=Institut,DC=institut,DC=local", uac=512,
              classes=("top", "person", "organizationalPerson", "user"), expires="9223372036854775807"):
    return (f"CN={name},{ou}", {"sAMAccountName": sam, "displayName": name, "mail": f"{sam}@institut.de",
                                "memberOf": [f"CN={g},OU=Gruppen,DC=institut,DC=local" for g in groups],
                                "userAccountControl": uac, "objectClass": list(classes), "accountExpires": expires})


class AdSearchTests(Base):
    def directory(self, entries):
        from unittest import mock
        from assetlotse.ldap_auth import Directory
        d = Directory(AD_CFG)
        conn = _FakeConn(entries)
        d.service_connection = mock.Mock(return_value=conn)
        return d, conn

    def test_only_people_allowed_by_the_rules_are_found(self):
        d, conn = self.directory([
            _ad_entry("amueller", "Anna Müller", ["AL-Mitarbeiter"]),
            _ad_entry("bmueller", "Bernd Müller", []),                                     # no rule -> invisible
            _ad_entry("cmueller", "Carla Müller", ["AL-Mitarbeiter"], uac=514),            # disabled in AD
            _ad_entry("dmueller", "Dirk Müller", [], ou="OU=IT,OU=Institut,DC=institut,DC=local"),   # OU rule
            _ad_entry("emueller", "Eva Müller", ["AL-Admins"]),
        ])
        found, more = d.search_users("Müller")
        self.assertEqual({u.username: u.role for u in found}, {"amueller": "reader", "dmueller": "manager", "emueller": "admin"})
        self.assertFalse(more)
        # same user filter as on login; in AD only active, non-expired people (server side)
        flt = conn.filters[0]
        self.assertTrue(flt.startswith("(&(objectClass=user)(|"))
        for part in ("(objectCategory=person)", "(!(userAccountControl:1.2.840.113556.1.4.803:=2))", "accountExpires>="):
            self.assertIn(part, flt)
        d2, conn2 = self.directory([])
        d2.search_users("a*)(cn=*")
        self.assertIn("a\\2a\\29\\28cn=\\2a", conn2.filters[0])

    def test_computers_groups_and_expired_accounts_are_hidden(self):
        d, _conn = self.directory([
            _ad_entry("PC-ANNA$", "PC-ANNA", ["AL-Mitarbeiter"], classes=("top", "user", "computer")),
            _ad_entry("abt-anna", "Abt-Annalen", ["AL-Mitarbeiter"], classes=("top", "group")),
            _ad_entry("anna.alt", "Anna Alt", ["AL-Mitarbeiter"], expires="130000000000000000"),   # 2012
            _ad_entry("anna", "Anna Neu", ["AL-Mitarbeiter"], expires="0"),
        ])
        found, _more = d.search_users("anna")
        self.assertEqual([u.username for u in found], ["anna"])

    def test_known_accounts_are_skipped_before_the_limit(self):
        entries = [_ad_entry(f"mueller{i:02}", f"Müller {i:02}", ["AL-Mitarbeiter"]) for i in range(12)]
        d, _conn = self.directory(entries)
        known = {f"Mueller{i:02}" for i in range(10)}          # case does not matter
        found, more = d.search_users("Müller", exclude=known)
        self.assertEqual([u.username for u in found], ["mueller10", "mueller11"])
        self.assertFalse(more)

    def test_candidate_budget_reports_more(self):
        entries = [_ad_entry(f"x{i}", f"Anna X{i}", []) for i in range(40)] + [_ad_entry("zett", "Anna Zett", ["AL-Mitarbeiter"])]
        d, _conn = self.directory(entries)
        found, more = d.search_users("anna", candidates=30)
        self.assertEqual(found, [])
        self.assertTrue(more)                                 # -> notice “please search more precisely”

    def test_short_query_and_missing_service_account(self):
        from assetlotse.ldap_auth import Directory, LDAPError
        d, conn = self.directory([_ad_entry("amueller", "Anna Müller", ["AL-Mitarbeiter"])])
        self.assertEqual(d.search_users("A"), ([], False))
        self.assertEqual(conn.filters, [])
        with self.assertRaises(LDAPError):
            Directory({**AD_CFG, "ldap_bind_dn": ""}).search_users("Anna")

    def _enable_ad(self):
        for k, v in AD_CFG.items():
            set_setting(k, v)
        set_setting("ldap_enabled", "1")
        db.session.commit()

    def _ldap_user(self, username, role="reader", name="Anna Müller"):
        from assetlotse.ldap_auth import LDAPUser
        return LDAPUser(username=username, dn=f"CN={name},OU=Personal,OU=Institut,DC=institut,DC=local",
                        display_name=name, email=f"{username}@institut.de", groups=[], role=role)

    def test_desk_shows_ad_people_and_takes_them_over(self):
        from unittest import mock
        from assetlotse.ldap_auth import Directory
        self._enable_ad()
        self.laptop(secret="", group_id=None)        # “General” – anyone may get it
        self.login("vera")
        with mock.patch.object(Directory, "search_users", return_value=([self._ldap_user("amueller")], False)):
            html = self.c.get("/theke?modus=ausgabe&benutzer=Müller").get_data(as_text=True)
        self.assertIn("Aus dem Active Directory", html)
        self.assertIn("amueller@institut.de", html)
        self.assertIsNone(User.query.filter_by(username="amueller").first())
        # take over: the server checks the person again in AD
        with mock.patch.object(Directory, "check_account", return_value=self._ldap_user("amueller")) as chk:
            r = self.c.post("/theke/ad-konto", data={"username": "amueller"})
        chk.assert_called_once_with("amueller")
        self.assertIn("benutzer=amueller", r.headers["Location"])
        anna = User.query.filter_by(username="amueller").one()
        self.assertEqual((anna.auth_source, anna.role, anna.display_name, anna.password_hash),
                         ("ldap", "reader", "Anna Müller", None))
        self.assertNotIn("Administrator", html.split("Aus dem Active Directory")[1].split("</section>")[0])
        self.c.post("/theke", data={"modus": "ausgabe", "benutzer": "amueller", "code": "IT-0042", "auto": "1"})
        self.assertEqual(Loan.query.one().user_id, anna.id)

    def test_takeover_refused_when_not_allowed_or_disabled(self):
        from unittest import mock
        from assetlotse.ldap_auth import Directory
        self._enable_ad()
        self.login("vera")
        for result in (None, self._ldap_user("bmueller", role=None)):
            with self.subTest(result=result), \
                    mock.patch.object(Directory, "check_account", return_value=result):
                r = self.c.post("/theke/ad-konto", data={"username": "bmueller"}, follow_redirects=True)
                self.assertIn("nicht (mehr) für AssetLotse freigegeben", r.get_data(as_text=True))
        self.assertIsNone(User.query.filter_by(username="bmueller").first())

    def test_local_account_is_never_taken_over(self):
        from unittest import mock
        from assetlotse.ldap_auth import Directory
        self._enable_ad()
        self.login("vera")
        with mock.patch.object(Directory, "check_account", return_value=self._ldap_user("mia")):
            r = self.c.post("/theke/ad-konto", data={"username": "mia"}, follow_redirects=True)
        self.assertIn("bereits ein lokales Konto", r.get_data(as_text=True))
        db.session.refresh(self.mia)
        self.assertEqual(self.mia.auth_source, "local")

    def test_only_managers_and_only_with_ad(self):
        self.login("mia")
        self.assertEqual(self.c.post("/theke/ad-konto", data={"username": "x"}).status_code, 403)
        self.login("vera")
        self.assertEqual(self.c.post("/theke/ad-konto", data={"username": "x"}).status_code, 400)   # AD off

    def test_ad_error_does_not_break_desk(self):
        from unittest import mock
        from assetlotse.ldap_auth import Directory, LDAPError
        self._enable_ad()
        self.login("vera")
        with mock.patch.object(Directory, "search_users", side_effect=LDAPError("Bind abgelehnt DSID-0815")):
            r = self.c.get("/theke?modus=ausgabe&benutzer=Mi")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertNotIn("DSID-0815", html)          # technical details for administrators only
        self.assertIn("nicht erreichbar", html)
        self.assertIn("Mia", html)                  # local matches still there
        self.login("admin")
        with mock.patch.object(Directory, "search_users", side_effect=LDAPError("Bind abgelehnt DSID-0815")):
            self.assertIn("DSID-0815", self.c.get("/theke?modus=ausgabe&benutzer=Mi").get_data(as_text=True))

    def test_ad_role_applies_only_from_first_own_login(self):
        from unittest import mock
        from assetlotse.ldap_auth import Directory
        self._enable_ad()
        self.login("vera")
        with mock.patch.object(Directory, "check_account", return_value=self._ldap_user("boss", role="admin", name="Chefin")):
            self.c.post("/theke/ad-konto", data={"username": "boss"})
        self.assertEqual(User.query.filter_by(username="boss").one().role, "reader")

    def test_takeover_twice_is_harmless(self):
        from unittest import mock
        from assetlotse.ldap_auth import Directory
        self._enable_ad()
        self.login("vera")
        with mock.patch.object(Directory, "check_account", return_value=self._ldap_user("amueller")):
            self.c.post("/theke/ad-konto", data={"username": "amueller"})
            r = self.c.post("/theke/ad-konto", data={"username": "amueller"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(User.query.filter_by(username="amueller").count(), 1)

    def test_exact_username_needs_no_ad_query(self):
        from unittest import mock
        from assetlotse.ldap_auth import Directory
        self._enable_ad()
        self.login("vera")
        with mock.patch.object(Directory, "search_users") as srch:
            self.c.get("/theke?modus=ausgabe&benutzer=mia")
        srch.assert_not_called()

    def test_expired_account_is_refused_on_takeover(self):
        from assetlotse import ldap_auth
        self.assertTrue(ldap_auth._expired("130000000000000000"))
        self.assertFalse(ldap_auth._expired("0"))
        self.assertFalse(ldap_auth._expired("9223372036854775807"))
        self.assertFalse(ldap_auth._expired(str(ldap_auth._filetime_now() + 10**12)))
        self.assertFalse(ldap_auth._is_person_account({}, "PC01$"))

    def test_unique_local_match_is_selected_without_ad(self):
        self.login("vera")
        html = self.c.get("/theke?modus=ausgabe&benutzer=ia").get_data(as_text=True)
        self.assertIn("(mia)", html)                # selected directly


class DeskStockSearchTests(Base):
    def setUp(self):
        super().setUp()
        self.a1 = self.laptop(secret="")
        self.a2 = Asset(name="Headset Jabra", category="Audio", quantity=2, group_id=self.it.id)
        self.a3 = Asset(name="Headset fremd", category="Audio", quantity=1, group_id=self.hr.id)
        self.a4 = Asset(name="Beamer", category="Beamer", quantity=1, group_id=None)
        db.session.add_all([self.a2, self.a3, self.a4])
        db.session.commit()
        db.session.add(Loan(asset_id=self.a4.id, user_id=self.vera.id, due_at=None))
        db.session.commit()

    def test_search_by_text_and_category_only_own_stock(self):
        self.login("vera")
        html = self.c.get("/theke?modus=ausgabe&benutzer=mia&suche=headset").get_data(as_text=True)
        self.assertIn("Headset Jabra", html)
        self.assertNotIn("Headset fremd", html)           # other group: vera cannot issue it
        html = self.c.get("/theke?modus=ausgabe&benutzer=mia&kategorie=Laptop").get_data(as_text=True)
        self.assertIn("Laptop Dell", html)
        self.assertNotIn("Headset Jabra", html)

    def test_only_available_by_default(self):
        self.login("vera")
        html = self.c.get("/theke?modus=ausgabe&benutzer=mia&suche=beamer").get_data(as_text=True)
        self.assertIn("Nichts gefunden.", html)
        html = self.c.get("/theke?modus=ausgabe&benutzer=mia&suche=beamer&frei=0").get_data(as_text=True)
        self.assertIn(">Beamer<", html)

    def test_issue_from_search_keeps_search(self):
        self.login("vera")
        r = self.c.post("/theke", data={"modus": "ausgabe", "benutzer": "mia", "asset_id": self.a2.id,
                                        "suche": "headset", "kategorie": "Audio", "frei": "1"})
        self.assertEqual(Loan.query.filter_by(user_id=self.mia.id).one().asset_id, self.a2.id)
        loc = r.headers["Location"]
        for part in ("benutzer=mia", "suche=headset", "kategorie=Audio"):
            self.assertIn(part, loc)

    def test_cannot_issue_foreign_group_via_search_post(self):
        self.login("vera")
        self.assertEqual(self.c.post("/theke", data={"modus": "ausgabe", "benutzer": "mia",
                                                     "asset_id": self.a3.id}).status_code, 403)

    def test_without_search_no_results_panel(self):
        self.login("vera")
        html = self.c.get("/theke?modus=ausgabe&benutzer=mia").get_data(as_text=True)
        self.assertIn("Im Bestand suchen", html)
        self.assertNotIn("Nichts gefunden.", html)


class ReviewRound2Tests(DeskStockSearchTests):
    """Findings from the second review round of the desk."""

    def test_scan_line_keeps_focus_after_issuing_from_search(self):
        self.login("vera")
        html = self.c.get("/theke?modus=ausgabe&benutzer=mia&suche=headset").get_data(as_text=True)
        self.assertRegex(html, r'id="desk-code"[^>]*autofocus')
        html = self.c.get("/theke?modus=ausgabe&benutzer=mia&suche=headset&fokus=suche").get_data(as_text=True)
        self.assertNotRegex(html, r'id="desk-code"[^>]*autofocus')
        # the scan form carries the search along
        self.assertIn('<input type="hidden" name="suche" value="headset">', html)

    def test_search_only_shows_items_the_person_may_get(self):
        self.login("admin")
        hans = self.hans                                    # group Personal
        html = self.c.get(f"/theke?modus=ausgabe&benutzer={hans.username}&suche=headset").get_data(as_text=True)
        self.assertIn("Headset fremd", html)
        self.assertNotIn("Headset Jabra", html)             # group IT – hans may not get it

    def test_categories_limited_to_own_stock(self):
        db.session.add(Asset(name="Geheim", category="HR-Spezial", group_id=self.hr.id))
        db.session.commit()
        self.login("vera")
        self.assertNotIn("HR-Spezial", self.c.get("/theke?modus=ausgabe&benutzer=mia").get_data(as_text=True))
        self.assertNotIn("HR-Spezial", self.c.get("/gegenstaende/neu").get_data(as_text=True))

    def test_contacts_are_no_accounts(self):
        from assetlotse import ldap_auth
        self.assertFalse(ldap_auth._is_person_account({"objectClass": ["top", "person", "contact"]}, "kontakt"))
        self.assertFalse(ldap_auth._is_person_account({"objectClass": ["top", "organizationalUnit"]}, "ou"))
        self.assertTrue(ldap_auth._is_person_account({"objectClass": ["top", "inetOrgPerson"]}, "anna"))
        self.assertTrue(ldap_auth._is_person_account({}, "anna"))

    def test_concurrent_takeover_does_not_crash(self):
        from unittest import mock
        from assetlotse.ldap_auth import Directory, LDAPUser
        from assetlotse.views import main as main_view
        for k, v in AD_CFG.items():
            set_setting(k, v)
        set_setting("ldap_enabled", "1")
        db.session.commit()
        info = LDAPUser(username="race", dn="CN=Race,DC=x", display_name="Race", role="reader")
        orig = main_view.sync_ldap_user

        def sync_with_race(i, existing):
            # a parallel click creates the account right now (own connection)
            with db.engine.begin() as conn:
                conn.execute(User.__table__.insert().values(username="race", role="reader", auth_source="ldap",
                                                            active=True, email_notifications=True, self_registered=False,
                                                            email_verified=True, pending_approval=False,
                                                            totp_enabled=False, totp_last_step=0, auth_epoch=0, language=""))
            return orig(i, existing)
        self.login("vera")
        with mock.patch.object(Directory, "check_account", return_value=info), \
                mock.patch.object(main_view, "sync_ldap_user", side_effect=sync_with_race):
            r = self.c.post("/theke/ad-konto", data={"username": "race"})
        self.assertEqual(r.status_code, 302)
