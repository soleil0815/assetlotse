"""Tests für ASSETLOTSE – ausführen mit:  python -m unittest discover -s tests -v"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="assetlotse-test-")
os.environ["ASSETLOTSE_DATA_DIR"] = _tmp
os.environ["DATABASE_URL"] = "sqlite://"

from assetlotse import create_app  # noqa: E402
from assetlotse import ldap_auth  # noqa: E402
from assetlotse.models import Asset, Group, Loan, User, db, set_setting  # noqa: E402


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False,
                               "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.app.config.pop("_setup_done", None)
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    # Hilfen
    def make_user(self, name, role="reader", pw="geheim123", groups=()):
        u = User(username=name, display_name=name.title(), role=role, auth_source="local")
        u.set_password(pw)
        u.groups = list(groups)
        db.session.add(u)
        db.session.commit()
        return u

    def login(self, name, pw="geheim123"):
        self.c.post("/logout")
        return self.c.post("/login", data={"username": name, "password": pw}, follow_redirects=True)


class SetupAndAuthTests(BaseCase):
    def test_setup_flow(self):
        r = self.c.get("/", follow_redirects=False)
        self.assertIn("/setup", r.headers["Location"])
        r = self.c.post("/setup", data={"username": "admin", "password": "supersecret", "password2": "supersecret",
                                       "org_name": "IT-Ausgabe"}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("IT-Ausgabe", r.get_data(as_text=True))
        self.assertTrue(User.query.filter_by(username="admin", role="admin").first())
        # Setup danach nicht mehr erreichbar
        self.assertIn("/login", self.c.get("/setup").headers["Location"])

    def test_login_logout_and_wrong_password(self):
        self.make_user("admin", "admin")
        r = self.c.post("/login", data={"username": "admin", "password": "falsch"})
        self.assertEqual(r.status_code, 401)
        r = self.login("ADMIN")  # Groß-/Kleinschreibung egal
        self.assertIn("Übersicht", r.get_data(as_text=True))
        self.c.post("/logout")
        self.assertIn("/login", self.c.get("/").headers["Location"])

    def test_inactive_user_cannot_login(self):
        self.make_user("admin", "admin")
        u = self.make_user("max")
        u.active = False
        db.session.commit()
        r = self.c.post("/login", data={"username": "max", "password": "geheim123"})
        self.assertEqual(r.status_code, 401)

    def test_open_redirect_blocked(self):
        self.make_user("admin", "admin")
        r = self.c.post("/login?next=https://evil.example", data={"username": "admin", "password": "geheim123"})
        self.assertEqual(r.headers["Location"], "/")


class PermissionTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.g7 = Group(name="Klasse 7a")
        self.g8 = Group(name="Klasse 8b")
        db.session.add_all([self.g7, self.g8])
        db.session.commit()
        self.admin = self.make_user("admin", "admin")
        self.lib7 = self.make_user("lehrer7", "manager", groups=[self.g7])
        self.libglobal = self.make_user("verwalter", "manager")
        self.libglobal.manages_all = True          # ausdrücklich: verwaltet den gesamten Bestand
        db.session.commit()
        self.r7 = self.make_user("schueler7", groups=[self.g7])
        self.r8 = self.make_user("schueler8", groups=[self.g8])
        self.b7 = Asset(name="Laptop 7a", group_id=self.g7.id, quantity=1, inventory_no="IT-0007",
                        serial_number="SN-7A-123")
        self.b8 = Asset(name="Laptop 8b", group_id=self.g8.id, quantity=1)
        self.ball = Asset(name="Allgemeines Headset", quantity=2)
        db.session.add_all([self.b7, self.b8, self.ball])
        # Diese Tests prüfen auch die Selbstausleihe und befristete Ausleihen
        set_setting("self_checkout", "1")
        set_setting("loan_days", "28")
        db.session.commit()

    def test_reader_catalog_only_own_groups(self):
        self.login("schueler7")
        html = self.c.get("/gegenstaende").get_data(as_text=True)
        self.assertIn("Laptop 7a", html)
        self.assertIn("Allgemeines Headset", html)
        self.assertNotIn("Laptop 8b", html)
        self.assertEqual(self.c.get(f"/gegenstaende/{self.b8.id}").status_code, 404)

    def test_catalog_visibility_all(self):
        set_setting("catalog_visibility", "all")
        db.session.commit()
        self.login("schueler7")
        self.assertIn("Laptop 8b", self.c.get("/gegenstaende").get_data(as_text=True))
        # Ausleihen trotzdem verboten
        self.c.post(f"/gegenstaende/{self.b8.id}/ausgeben")
        self.assertEqual(Loan.query.count(), 0)

    def test_reader_borrow_rules(self):
        self.login("schueler7")
        self.c.post(f"/gegenstaende/{self.b7.id}/ausgeben")
        self.assertEqual(Loan.query.filter_by(user_id=self.r7.id).count(), 1)
        self.c.post(f"/gegenstaende/{self.b8.id}/ausgeben")  # fremde Gruppe
        self.assertEqual(Loan.query.filter_by(asset_id=self.b8.id).count(), 0)
        self.c.post(f"/gegenstaende/{self.ball.id}/ausgeben")
        self.assertEqual(Loan.query.filter_by(user_id=self.r7.id).count(), 2)
        # zweimal dasselbe Gerät geht nicht
        self.c.post(f"/gegenstaende/{self.ball.id}/ausgeben")
        self.assertEqual(Loan.query.filter_by(user_id=self.r7.id).count(), 2)

    def test_no_copy_available(self):
        self.login("schueler7")
        self.c.post(f"/gegenstaende/{self.b7.id}/ausgeben")
        self.login("lehrer7")
        r = self.c.post(f"/gegenstaende/{self.b7.id}/ausgeben", data={"user_id": self.lib7.id}, follow_redirects=True)
        self.assertIn("Kein Stück verfügbar", r.get_data(as_text=True))

    def test_reader_cannot_manage(self):
        self.login("schueler7")
        self.assertEqual(self.c.get("/gegenstaende/neu").status_code, 403)
        self.assertEqual(self.c.get("/admin/benutzer").status_code, 403)
        self.assertEqual(self.c.post(f"/gegenstaende/{self.b7.id}/loeschen").status_code, 403)
        # Für andere ausleihen ist verboten
        self.assertEqual(self.c.post(f"/gegenstaende/{self.b7.id}/ausgeben", data={"user_id": self.r8.id}).status_code, 403)

    def test_group_manager_scope(self):
        self.login("lehrer7")
        self.assertEqual(self.c.get(f"/gegenstaende/{self.b7.id}/bearbeiten").status_code, 200)
        self.assertEqual(self.c.get(f"/gegenstaende/{self.b8.id}/bearbeiten").status_code, 403)
        self.assertEqual(self.c.get("/admin/benutzer").status_code, 403)
        # darf keine Geräte in fremde Gruppen verschieben
        r = self.c.post(f"/gegenstaende/{self.b7.id}/bearbeiten", data={"name": "X", "group_id": self.g8.id, "quantity": 1})
        self.assertEqual(r.status_code, 403)
        # Verleihen an Mitglied der eigenen Gruppe
        self.c.post(f"/gegenstaende/{self.b7.id}/ausgeben", data={"user_id": self.r7.id})
        self.assertEqual(Loan.query.filter_by(user_id=self.r7.id).count(), 1)

    def test_manager_cannot_lend_to_wrong_group(self):
        self.login("verwalter")
        r = self.c.post(f"/gegenstaende/{self.b7.id}/ausgeben", data={"user_id": self.r8.id}, follow_redirects=True)
        self.assertIn("darf diesen Gegenstand nicht bekommen", r.get_data(as_text=True))
        self.assertEqual(Loan.query.count(), 0)

    def test_global_manager_manages_all(self):
        self.login("verwalter")
        self.assertEqual(self.c.get(f"/gegenstaende/{self.b8.id}/bearbeiten").status_code, 200)

    def test_return_extend(self):
        set_setting("self_return", "0")
        set_setting("max_extensions", "1")
        db.session.commit()
        self.login("schueler7")
        self.c.post(f"/gegenstaende/{self.b7.id}/ausgeben")
        loan = Loan.query.one()
        due = loan.due_at
        self.c.post(f"/ausleihen/{loan.id}/verlaengern")
        db.session.refresh(loan)
        self.assertGreater(loan.due_at, due)
        r = self.c.post(f"/ausleihen/{loan.id}/verlaengern", follow_redirects=True)
        self.assertIn("maximale Anzahl", r.get_data(as_text=True))
        self.assertEqual(self.c.post(f"/ausleihen/{loan.id}/rueckgabe").status_code, 403)
        self.login("lehrer7")
        self.c.post(f"/ausleihen/{loan.id}/rueckgabe")
        db.session.refresh(loan)
        self.assertIsNotNone(loan.returned_at)

    def test_max_loans(self):
        set_setting("max_loans", "1")
        db.session.commit()
        self.login("schueler7")
        self.c.post(f"/gegenstaende/{self.b7.id}/ausgeben")
        r = self.c.post(f"/gegenstaende/{self.ball.id}/ausgeben", follow_redirects=True)
        self.assertIn("maximale Anzahl", r.get_data(as_text=True))

    def test_desk_lend_and_return_by_number(self):
        self.login("lehrer7")
        r = self.c.post("/theke", data={"modus": "ausgabe", "benutzer": "schueler7", "code": "it-0007",
                                        "auto": "1"}, follow_redirects=True)
        self.assertIn("ausgegeben", r.get_data(as_text=True))
        self.assertEqual(Loan.query.filter_by(user_id=self.r7.id, returned_at=None).count(), 1)
        r = self.c.post("/theke", data={"modus": "rueckgabe", "code": "SN-7A-123", "auto": "1"}, follow_redirects=True)
        self.assertIn("zurückgenommen", r.get_data(as_text=True))
        self.assertEqual(Loan.query.filter_by(returned_at=None).count(), 0)

    def test_overdue_listing(self):
        loan = Loan(asset=self.b7, user=self.r7, due_at=date.today() - timedelta(days=3))
        db.session.add(loan)
        db.session.commit()
        self.login("admin")
        html = self.c.get("/ausleihen?ansicht=ueberfaellig").get_data(as_text=True)
        self.assertIn("3 Tg. überfällig", html)
        self.assertIn("Laptop 7a", self.c.get("/").get_data(as_text=True))

    def test_all_pages_render(self):
        self.login("admin")
        loan = Loan(asset=self.b7, user=self.r7, due_at=date.today())
        db.session.add(loan)
        db.session.commit()
        for url in ["/", "/gegenstaende", "/gegenstaende?status=verfuegbar", "/gegenstaende?q=buch&sort=new", f"/gegenstaende/{self.b7.id}",
                    "/gegenstaende/neu", f"/gegenstaende/neu?vorlage={self.b7.id}", f"/gegenstaende/{self.b7.id}/bearbeiten",
                    "/gegenstaende?kategorie=Laptop", "/ausleihen", "/ausleihen?ansicht=verlauf", "/ausleihen?nur=meine",
                    "/theke", "/theke?modus=rueckgabe", "/theke?benutzer=schueler7", "/profil",
                    "/admin/benutzer", "/admin/benutzer/neu", f"/admin/benutzer/{self.r7.id}", "/admin/gruppen",
                    "/admin/gruppen/neu", f"/admin/gruppen/{self.g7.id}", "/admin/einstellungen",
                    "/admin/verzeichnisdienst", "/gegenstaende/export.csv"]:
            with self.subTest(url=url):
                self.assertEqual(self.c.get(url).status_code, 200, url)
        self.assertEqual(self.c.get("/gegenstaende/9999").status_code, 404)


class AssetTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.make_user("admin", "admin")
        self.login("admin")
        db.session.commit()

    def test_create_edit_delete(self):
        self.c.post("/gegenstaende/neu", data={"name": "Headset", "category": "Audio", "manufacturer": "Jabra",
                                               "model": "Evolve2 40", "inventory_no": "IT-0100", "serial_number": "J-1",
                                               "quantity": "2"})
        asset = Asset.query.one()
        self.assertEqual((asset.quantity, asset.category, asset.maker_model, asset.inventory_no),
                         (2, "Audio", "Jabra Evolve2 40", "IT-0100"))
        self.c.post(f"/gegenstaende/{asset.id}/bearbeiten", data={"name": "Headset Jabra", "quantity": "3"})
        db.session.refresh(asset)
        self.assertEqual(asset.name, "Headset Jabra")
        self.c.post(f"/gegenstaende/{asset.id}/loeschen")
        self.assertEqual(Asset.query.count(), 0)

    def test_title_required(self):
        r = self.c.post("/gegenstaende/neu", data={"name": ""}, follow_redirects=True)
        self.assertIn("Bezeichnung ist erforderlich", r.get_data(as_text=True))

    def test_cannot_delete_lent_asset(self):
        u = self.make_user("leser")
        b = Asset(name="T", quantity=1)
        db.session.add(b)
        db.session.add(Loan(asset=b, user=u, due_at=date.today()))
        db.session.commit()
        self.c.post(f"/gegenstaende/{b.id}/loeschen")
        self.assertEqual(Asset.query.count(), 1)


class LdapTests(BaseCase):
    def test_rule_matching(self):
        dn = "CN=Max Muster,OU=7a,OU=Schueler,DC=schule,DC=local"
        groups = ["CN=Bib-Admins,OU=Gruppen,DC=schule,DC=local"]
        cfg = {"ldap_admin_rules": "cn=bib-admins, ou=Gruppen, dc=schule, dc=local",
               "ldap_manager_rules": "OU=Lehrer,DC=schule,DC=local",
               "ldap_login_rules": "OU=Schueler,DC=schule,DC=local"}
        self.assertEqual(ldap_auth.resolve_role(dn, groups, cfg)[0], "admin")
        self.assertEqual(ldap_auth.resolve_role(dn, [], cfg)[0], "reader")
        self.assertIsNone(ldap_auth.resolve_role("CN=X,OU=Gaeste,DC=schule,DC=local", [], cfg)[0])
        # Teilstring einer OU darf nicht greifen
        self.assertFalse(ldap_auth.rule_matches("OU=Schueler,DC=schule,DC=local",
                                                "CN=Y,OU=AltSchueler,DC=schule,DC=local", set()))

    def test_group_name_rules(self):
        dn = "CN=Max,OU=Benutzer,DC=schule,DC=local"
        groups = ["CN=IT-Verwalter,OU=Gruppen,DC=schule,DC=local",
                  "CN=Domänen-Benutzer,CN=Users,DC=schule,DC=local"]
        cfg = {"ldap_admin_rules": "IT-Admins", "ldap_manager_rules": "it-verwalter",
               "ldap_login_rules": "Domänen-Benutzer"}
        self.assertEqual(ldap_auth.resolve_role(dn, groups, cfg), ("manager", "it-verwalter"))
        self.assertEqual(ldap_auth.resolve_role(dn, groups[1:], cfg)[0], "reader")
        self.assertIsNone(ldap_auth.resolve_role(dn, [], cfg)[0])
        # Teilname greift nicht
        self.assertFalse(ldap_auth.rule_matches("Bibliothek", dn, {ldap_auth.normalize_dn(groups[0])}))

    def test_group_sync_by_name(self):
        self.make_user("admin", "admin")
        g = Group(name="Klasse 7a", ldap_dn="Klasse7a")
        db.session.add(g)
        set_setting("ldap_enabled", "1")
        db.session.commit()
        info = ldap_auth.LDAPUser(username="lena", dn="CN=Lena,DC=x", groups=["CN=Klasse7a,OU=G,DC=x"], role="reader")
        with mock.patch("assetlotse.views.main.authenticate", return_value=info):
            self.c.post("/login", data={"username": "lena", "password": "x"})
        self.assertEqual([x.name for x in User.query.filter_by(username="lena").one().groups], ["Klasse 7a"])

    def test_admin_lists_ad_groups(self):
        self.make_user("admin", "admin")
        self.login("admin")
        fake = [{"name": "IT-Admins", "dn": "CN=IT-Admins,DC=x", "description": "Admins"}]
        with mock.patch.object(ldap_auth.Directory, "list_groups", return_value=fake) as lg:
            html = self.c.post("/admin/verzeichnisdienst", data={
                "action": "list_groups", "group_query": "Bib", "ldap_server": "ldap://dc1",
                "ldap_base_dn": "DC=x"}).get_data(as_text=True)
            lg.assert_called_once_with("Bib")
        self.assertIn('data-name="IT-Admins"', html)

    def test_ldap_login_creates_user_and_syncs_groups(self):
        self.make_user("admin", "admin")
        g = Group(name="Klasse 7a", ldap_dn="CN=Klasse7a,OU=Gruppen,DC=schule,DC=local")
        manual = Group(name="Leseclub")
        db.session.add_all([g, manual])
        set_setting("ldap_enabled", "1")
        db.session.commit()
        info = ldap_auth.LDAPUser(username="mmuster", dn="CN=Max,OU=Schueler,DC=schule,DC=local",
                                  display_name="Max Muster", email="max@schule.local",
                                  groups=["cn=klasse7a,ou=gruppen,dc=schule,dc=local"], role="reader")
        with mock.patch("assetlotse.views.main.authenticate", return_value=info):
            r = self.c.post("/login", data={"username": "mmuster", "password": "x"}, follow_redirects=True)
        self.assertIn("Hallo, Max", r.get_data(as_text=True))
        u = User.query.filter_by(username="mmuster").one()
        self.assertEqual(u.auth_source, "ldap")
        self.assertEqual([x.name for x in u.groups], ["Klasse 7a"])
        # manuell zugeordnete Gruppe bleibt, AD-Gruppe wird bei Austritt entfernt
        u.groups.append(manual)
        db.session.commit()
        info.groups, info.role = [], "manager"
        self.c.post("/logout")
        with mock.patch("assetlotse.views.main.authenticate", return_value=info):
            self.c.post("/login", data={"username": "mmuster", "password": "x"})
        db.session.refresh(u)
        self.assertEqual([x.name for x in u.groups], ["Leseclub"])
        self.assertEqual(u.role, "manager")

    def test_ldap_login_denied_without_rule(self):
        self.make_user("admin", "admin")
        set_setting("ldap_enabled", "1")
        db.session.commit()
        info = ldap_auth.LDAPUser(username="gast", dn="CN=Gast,DC=x", role=None)
        with mock.patch("assetlotse.views.main.authenticate", return_value=info):
            r = self.c.post("/login", data={"username": "gast", "password": "x"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("Benutzername oder Passwort ist falsch", r.get_data(as_text=True))
        self.assertIsNone(User.query.filter_by(username="gast").first())

    def test_ldap_error_does_not_leak(self):
        self.make_user("admin", "admin")
        set_setting("ldap_enabled", "1")
        db.session.commit()
        with mock.patch("assetlotse.views.main.authenticate", side_effect=ldap_auth.LDAPError("geheimer DC")):
            r = self.c.post("/login", data={"username": "a", "password": "b"})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("geheimer DC", r.get_data(as_text=True))

    def test_local_admin_wins_over_ldap(self):
        self.make_user("admin", "admin")
        set_setting("ldap_enabled", "1")
        db.session.commit()
        with mock.patch("assetlotse.views.main.authenticate") as auth:
            r = self.login("admin")
            auth.assert_not_called()
        self.assertIn("Übersicht", r.get_data(as_text=True))

    def test_empty_password_rejected(self):
        d = ldap_auth.Directory({"ldap_server": "ldap://127.0.0.1:1", "ldap_base_dn": "DC=x"})
        with self.assertRaises(ldap_auth.LDAPError):
            d.connect("CN=x", "")

    def test_unreachable_server_gives_ldap_error(self):
        d = ldap_auth.Directory({"ldap_server": "ldap://127.0.0.1:1", "ldap_base_dn": "DC=x",
                                 "ldap_bind_dn": "CN=svc", "ldap_bind_password": "pw", "ldap_timeout": "2"})
        with self.assertRaises(ldap_auth.LDAPError):
            d.test_connection()

    def test_admin_ldap_page_test_and_save(self):
        self.make_user("admin", "admin")
        self.login("admin")
        r = self.c.post("/admin/verzeichnisdienst", data={
            "action": "test_bind", "ldap_server": "ldap://127.0.0.1:1", "ldap_base_dn": "DC=x",
            "ldap_bind_dn": "CN=svc", "ldap_bind_password": "pw", "ldap_timeout": "2"})
        self.assertIn("Test fehlgeschlagen", r.get_data(as_text=True))
        self.c.post("/admin/verzeichnisdienst", data={
            "action": "save", "ldap_enabled": "1", "ldap_server": "ldap://dc1", "ldap_base_dn": "DC=x",
            "ldap_bind_password": "pw1", "ldap_login_rules": "OU=A,DC=x\n\n  OU=B,DC=x  "})
        from assetlotse.models import get_setting
        self.assertEqual(get_setting("ldap_login_rules"), "OU=A,DC=x\nOU=B,DC=x")
        self.assertEqual(get_setting("ldap_bind_password"), "pw1")
        # leeres Passwortfeld behält das gespeicherte
        self.c.post("/admin/verzeichnisdienst", data={"action": "save", "ldap_server": "ldap://dc1", "ldap_base_dn": "DC=x"})
        self.assertEqual(get_setting("ldap_bind_password"), "pw1")
        self.assertNotIn("pw1", self.c.get("/admin/verzeichnisdienst").get_data(as_text=True))


class AdminTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user("admin", "admin")
        self.login("admin")

    def test_user_crud(self):
        g = Group(name="G1")
        db.session.add(g)
        db.session.commit()
        self.c.post("/admin/benutzer/neu", data={"username": "neu", "password": "1234567890", "role": "manager",
                                                 "active": "1", "groups": [str(g.id)]})
        u = User.query.filter_by(username="neu").one()
        self.assertEqual(u.role, "manager")
        self.assertEqual(u.groups, [g])
        r = self.c.post("/admin/benutzer/neu", data={"username": "NEU", "password": "1234567890", "role": "reader"},
                        follow_redirects=True)
        self.assertIn("bereits vergeben", r.get_data(as_text=True))
        self.c.post(f"/admin/benutzer/{u.id}/loeschen")
        self.assertIsNone(User.query.filter_by(username="neu").first())

    def test_cannot_demote_self_or_last_admin(self):
        r = self.c.post(f"/admin/benutzer/{self.admin.id}", data={"username": "admin", "role": "reader", "active": "1"},
                        follow_redirects=True)
        self.assertIn("nicht entziehen", r.get_data(as_text=True))
        db.session.refresh(self.admin)
        self.assertEqual(self.admin.role, "admin")

    def test_group_delete_makes_assets_general(self):
        g = Group(name="G")
        db.session.add(g)
        db.session.commit()
        b = Asset(name="B", group_id=g.id)
        db.session.add(b)
        db.session.commit()
        self.c.post(f"/admin/gruppen/{g.id}/loeschen")
        db.session.refresh(b)
        self.assertIsNone(b.group_id)

    def test_settings(self):
        self.c.post("/admin/einstellungen", data={"org_name": "IT-Ausgabe", "loan_days": "21"})
        from assetlotse.models import get_setting
        self.assertEqual(get_setting("org_name"), "IT-Ausgabe")
        self.assertEqual(get_setting("loan_days"), "21")
        self.assertEqual(get_setting("self_checkout"), "0")
        # 0 = unbefristet ist erlaubt
        self.c.post("/admin/einstellungen", data={"loan_days": "0"})
        self.assertEqual(get_setting("loan_days"), "0")


if __name__ == "__main__":
    unittest.main()
