"""1.11.0: Nummer übernehmen statt neu anlegen, Gruppen ohne Verleih, Ausgabe aus dem Bestand, Vorschläge beim
Erfassen, Foto der Vorlage, eigene Angaben für lokale Konten."""
from test_assetlotse import Base

from assetlotse.models import Asset, Group, Loan, User, db


def form(a, **kw):
    d = {"name": a.name, "category": a.category or "", "manufacturer": a.manufacturer or "", "model": a.model or "",
         "inventory_no": a.inventory_no or "", "serial_number": a.serial_number or "", "quantity": str(a.quantity),
         "group_id": str(a.group_id or ""), "location": a.location or ""}
    d.update(kw)
    return d


class NumberTakeOverTests(Base):
    def test_wrong_number_can_be_moved_in_one_step(self):
        a = self.laptop(secret=None)                   # SN-1 – steht hier versehentlich
        b = self.laptop(secret=None)                   # SN-2
        self.login("vera")
        html = self.c.post(f"/gegenstaende/{b.id}/bearbeiten", data=form(b, serial_number="SN-1"),
                           follow_redirects=True).get_data(as_text=True)
        self.assertIn("bereits vergeben", html)
        self.assertIn(f'name="take_serial_number" value="{a.id}"', html)      # Angebot zur Übernahme
        self.assertEqual(db.session.get(Asset, b.id).serial_number, "SN-2")
        html = self.c.post(f"/gegenstaende/{b.id}/bearbeiten",
                           data=form(b, serial_number="sn-1", take_serial_number=str(a.id)),
                           follow_redirects=True).get_data(as_text=True)
        self.assertIn("Änderungen gespeichert", html)
        self.assertIn("wurde bei „Laptop Dell“ entfernt", html)
        db.session.expire_all()
        self.assertEqual(db.session.get(Asset, b.id).serial_number, "sn-1")
        self.assertEqual(db.session.get(Asset, a.id).serial_number, "")
        self.assertIsNone(db.session.get(Asset, a.id).serial_key)
        # jetzt ist SN-2 frei und kann bei A eingetragen werden – nichts bleibt „vergeben“
        r = self.c.post(f"/gegenstaende/{a.id}/bearbeiten", data=form(db.session.get(Asset, a.id), serial_number="SN-2"),
                        follow_redirects=True)
        self.assertIn("Änderungen gespeichert", r.get_data(as_text=True))

    def test_take_over_both_numbers_in_one_step(self):
        a = self.laptop(secret=None)                   # IT-0042 / SN-1
        c = self.laptop(secret=None)                   # IT-0044 / SN-3 (zweiter Gegenstand)
        b = self.laptop(secret=None)
        self.login("vera")
        # beide Nummern von A
        r = self.c.post(f"/gegenstaende/{b.id}/bearbeiten", data=form(
            b, inventory_no="IT-0042", serial_number="SN-1",
            take_inventory_no=str(a.id), take_serial_number=str(a.id)), follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Änderungen gespeichert", r.get_data(as_text=True))
        db.session.expire_all()
        self.assertEqual((db.session.get(Asset, b.id).inventory_no, db.session.get(Asset, b.id).serial_number),
                         ("IT-0042", "SN-1"))
        self.assertEqual((db.session.get(Asset, a.id).inventory_no, db.session.get(Asset, a.id).serial_number), ("", ""))
        # je eine Nummer von zwei verschiedenen Gegenständen (C hat Inventar-, B jetzt Seriennummer)
        a2 = db.session.get(Asset, a.id)
        r = self.c.post(f"/gegenstaende/{a2.id}/bearbeiten", data=form(
            a2, inventory_no=c.inventory_no, serial_number="SN-1",
            take_inventory_no=str(c.id), take_serial_number=str(b.id)), follow_redirects=True)
        self.assertIn("Änderungen gespeichert", r.get_data(as_text=True))

    def test_ticked_offer_stays_ticked_while_the_other_number_conflicts(self):
        a = self.laptop(secret=None)
        b = self.laptop(secret=None)
        self.login("vera")
        html = self.c.post(f"/gegenstaende/{b.id}/bearbeiten", data=form(
            b, inventory_no=a.inventory_no, serial_number=a.serial_number, take_inventory_no=str(a.id)),
            follow_redirects=True).get_data(as_text=True)
        self.assertIn(f'name="take_inventory_no" value="{a.id}" checked', html)
        self.assertIn(f'name="take_serial_number" value="{a.id}" >', html)
        db.session.expire_all()
        self.assertEqual(db.session.get(Asset, a.id).inventory_no, a.inventory_no)      # nichts geändert

    def test_take_over_when_creating(self):
        a = self.laptop(secret=None)
        self.login("vera")
        r = self.c.post("/gegenstaende/neu", data={"name": "Neu", "serial_number": "SN-1", "quantity": "1",
                                                   "group_id": str(self.it.id), "take_serial_number": str(a.id)})
        self.assertEqual(r.status_code, 302)
        db.session.expire_all()
        self.assertEqual(Asset.query.filter_by(name="Neu").one().serial_number, "SN-1")
        self.assertEqual(db.session.get(Asset, a.id).serial_number, "")

    def test_no_take_over_from_a_foreign_group(self):
        a = self.laptop(secret=None, group_id=self.hr.id)          # vera verwaltet „Personal“ nicht
        b = self.laptop(secret=None)
        self.login("vera")
        html = self.c.post(f"/gegenstaende/{b.id}/bearbeiten",
                           data=form(b, serial_number=a.serial_number, take_serial_number=str(a.id)),
                           follow_redirects=True).get_data(as_text=True)
        self.assertIn("bei einem Gegenstand einer anderen Gruppe", html)
        self.assertNotIn("take_serial_number", html)
        db.session.expire_all()
        self.assertEqual(db.session.get(Asset, a.id).serial_number, a.serial_number)


class NoLendingGroupTests(Base):
    def setUp(self):
        super().setUp()
        self.fixed = Group(name="Kein Verleih", no_lending=True)
        db.session.add(self.fixed)
        db.session.commit()
        self.mia.groups.append(self.fixed)
        self.vera.groups.append(self.fixed)
        db.session.commit()

    def test_never_lent_and_not_offered_at_the_desk(self):
        a = self.laptop(secret=None, name="Beamer fest", group_id=self.fixed.id)
        self.login("vera")
        r = self.c.post(f"/gegenstaende/{a.id}/ausgeben", data={"user_id": self.mia.id}, follow_redirects=True)
        self.assertIn("werden nicht verliehen", r.get_data(as_text=True))
        self.assertEqual(Loan.query.count(), 0)
        sug = self.c.get("/vorschlaege?q=Beamer&fuer=theke&benutzer=mia").get_json()
        self.assertEqual(sug["items"], [])
        self.assertIn("Beamer fest", str(self.c.get("/vorschlaege?q=Beamer").get_json()))   # Suche oben: ja
        html = self.c.get("/theke?modus=ausgabe&benutzer=mia&suche=Beamer").get_data(as_text=True)
        self.assertNotIn("Beamer fest", html)
        r = self.c.post("/theke", data={"modus": "ausgabe", "benutzer": "mia", "code": a.inventory_no, "auto": "1"},
                        follow_redirects=True)
        self.assertIn("werden nicht verliehen", r.get_data(as_text=True))
        self.assertEqual(Loan.query.count(), 0)
        detail = self.c.get(f"/gegenstaende/{a.id}").get_data(as_text=True)
        self.assertIn("deren Gegenstände nicht verliehen werden", detail)
        self.assertNotIn("Ausgeben an …", detail)
        self.assertIn("kein Verleih", self.c.get("/gegenstaende").get_data(as_text=True))

    def test_switch_in_group_form_and_returns_still_work(self):
        a = self.laptop(secret=None, group_id=self.it.id)
        db.session.add(Loan(asset_id=a.id, user_id=self.mia.id, issued_by_id=self.admin.id))
        db.session.commit()
        self.login("admin")
        self.c.post(f"/admin/gruppen/{self.it.id}",
                    data={"name": "IT", "color": "#123456", "no_lending": "1",
                          "members": [str(u.id) for u in self.it.users]})
        db.session.expire_all()
        self.assertTrue(db.session.get(Group, self.it.id).no_lending)
        loan = Loan.query.one()
        self.c.post(f"/ausleihen/{loan.id}/rueckgabe")
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Loan, loan.id).returned_at)


class IssueFromInventoryTests(Base):
    def test_from_list_to_desk_to_asset(self):
        a = self.laptop(secret=None)
        self.login("vera")
        self.assertIn(f"/theke?modus=ausgabe&amp;gegenstand={a.id}", self.c.get("/gegenstaende").get_data(as_text=True))
        html = self.c.get(f"/theke?modus=ausgabe&gegenstand={a.id}").get_data(as_text=True)
        self.assertIn("Aus dem Bestand ausgeben", html)
        self.assertIn(f'name="gegenstand" value="{a.id}"', html)          # bleibt bei der Personenwahl erhalten
        self.assertIn(f"gegenstand={a.id}", self.c.get(f"/theke/personen?q=mia&gegenstand={a.id}").get_json()["items"][0]["url"])
        html = self.c.get(f"/theke?modus=ausgabe&gegenstand={a.id}&benutzer=mia").get_data(as_text=True)
        self.assertIn("„Laptop Dell“ an Mia ausgeben", html)
        r = self.c.post("/theke", data={"modus": "ausgabe", "benutzer": "mia", "gegenstand": a.id, "asset_id": a.id})
        self.assertTrue(r.headers["Location"].endswith(f"/gegenstaende/{a.id}"))
        self.assertEqual(Loan.query.one().user_id, self.mia.id)

    def test_foreign_asset_is_ignored(self):
        a = self.laptop(secret=None, group_id=self.hr.id)
        self.login("vera")
        html = self.c.get(f"/theke?modus=ausgabe&gegenstand={a.id}&benutzer=mia").get_data(as_text=True)
        self.assertNotIn("Aus dem Bestand ausgeben", html)


class OwnMailChangeTests(Base):
    """Selbst registrierte Konten: neue Adresse nur einmal vergeben und – mit Bestätigung – erst nach dem Link."""
    def setUp(self):
        super().setUp()
        from unittest import mock

        from assetlotse.models import set_setting
        for k, v in {"registration_mode": "open", "registration_verify_email": "1", "mail_enabled": "1",
                     "smtp_host": "mail.test", "app_base_url": "http://localhost", "registration_domains": ""}.items():
            set_setting(k, v)
        self.mia.self_registered = True
        self.mia.email = "mia@schule.de"
        db.session.commit()
        self.sent = []
        patcher = mock.patch("assetlotse.mailer.send_mail",
                             side_effect=lambda to, subject, text, html, **kw: self.sent.append((to, text)) or (True, ""))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_new_address_only_after_confirmation(self):
        import re
        self.login("mia")
        r = self.c.post("/profil", data={"action": "data", "display_name": "Mia M", "email": "neu@schule.de"},
                        follow_redirects=True)
        self.assertIn("Bestätigungslink an neu@schule.de", r.get_data(as_text=True))
        db.session.expire_all()
        u = db.session.get(User, self.mia.id)
        self.assertEqual((u.display_name, u.email), ("Mia M", "mia@schule.de"))   # Name sofort, Adresse noch nicht
        self.assertEqual(self.sent[0][0], "neu@schule.de")
        link = re.search(r"http://localhost(/profil/email-bestaetigen/\S+)", self.sent[0][1]).group(1)
        self.c.get(link)
        db.session.expire_all()
        self.assertEqual(db.session.get(User, self.mia.id).email, "neu@schule.de")
        self.assertEqual(self.c.get(link).status_code, 404)          # alter Link gilt nicht mehr

    def test_address_of_another_account_is_refused(self):
        self.vera.email = "vera@schule.de"
        db.session.commit()
        self.login("mia")
        r = self.c.post("/profil", data={"action": "data", "display_name": "Mia", "email": "VERA@schule.de"},
                        follow_redirects=True)
        self.assertIn("bereits von einem anderen Konto", r.get_data(as_text=True))
        self.assertEqual(self.sent, [])


class FormHelpTests(Base):
    def test_known_values_fill_suggestions(self):
        from assetlotse.views.assets import known_values
        self.laptop(secret=None, manufacturer="Dell", model="Vostro 3400")
        self.laptop(secret=None, manufacturer="Dell", model="Vostro 3400")
        self.laptop(secret=None, name="Maus", manufacturer="Logitech", model="M185")
        k = known_values(Asset.query)
        self.assertEqual(k["models"]["Vostro 3400"], {"category": "Laptop", "manufacturer": "Dell"})
        self.assertEqual(k["names"]["Maus"]["model"], "M185")
        self.assertEqual(k["manufacturers"], ["Dell", "Logitech"])
        self.login("vera")
        html = self.c.get("/gegenstaende/neu").get_data(as_text=True)
        self.assertIn('id="asset-known"', html)
        self.assertIn('<option value="Vostro 3400">', html)
        self.assertIn('data-known-fill="model"', html)

    def test_suggestions_only_from_own_inventory(self):
        self.laptop(secret=None, group_id=self.hr.id, manufacturer="Geheimfirma")
        self.login("vera")
        self.assertNotIn("Geheimfirma", self.c.get("/gegenstaende/neu").get_data(as_text=True))

    def test_photo_of_template_is_copied(self):
        a = self.laptop(secret=None)
        a.image_file = "abc.jpg"
        db.session.commit()
        self.login("vera")
        base = {"name": "Laptop Dell", "quantity": "1", "group_id": str(self.it.id), "image_from": str(a.id)}
        self.c.post("/gegenstaende/neu", data=dict(base, inventory_no="IT-9"))
        self.assertEqual(Asset.query.filter_by(inventory_no="IT-9").one().image_file, "abc.jpg")
        self.c.post("/gegenstaende/neu", data=dict(base, inventory_no="IT-10", remove_image="1"))
        self.assertEqual(Asset.query.filter_by(inventory_no="IT-10").one().image_file, "")
        foreign = self.laptop(secret=None, group_id=self.hr.id)
        foreign.image_file = "geheim.jpg"
        db.session.commit()
        self.c.post("/gegenstaende/neu", data=dict(base, inventory_no="IT-11", image_from=str(foreign.id)))
        self.assertEqual(Asset.query.filter_by(inventory_no="IT-11").one().image_file, "")


class OwnDetailsTests(Base):
    def test_local_account_edits_name_and_mail(self):
        self.login("mia")
        self.assertIn('name="display_name"', self.c.get("/profil").get_data(as_text=True))
        self.c.post("/profil", data={"action": "data", "display_name": "Mia Muster", "email": "mia@neu.de"})
        db.session.expire_all()
        u = db.session.get(User, self.mia.id)
        self.assertEqual((u.display_name, u.email), ("Mia Muster", "mia@neu.de"))
        self.c.post("/profil", data={"action": "data", "display_name": "Mia", "email": "kaputt"})
        db.session.expire_all()
        self.assertEqual(db.session.get(User, self.mia.id).email, "mia@neu.de")

    def test_ad_account_cannot(self):
        self.login("mia")                       # angemeldet – danach als AD-Konto behandeln
        self.mia.auth_source = "ldap"
        db.session.commit()
        html = self.c.get("/profil").get_data(as_text=True)
        self.assertIn("kommen aus dem Active Directory", html)
        self.assertNotIn('name="display_name"', html)
        self.assertEqual(self.c.post("/profil", data={"action": "data", "display_name": "X"}).status_code, 400)
