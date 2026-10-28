"""Festgelegte Standorte: Auswahl statt Freitext, Umbenennen, Tippfehler zusammenführen."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-test-"))

from assetlotse import create_app  # noqa: E402
from assetlotse.models import Asset, Group, Location, User, db  # noqa: E402


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.g1, self.g2 = Group(name="Klasse 1"), Group(name="Klasse 2")
        db.session.add_all([self.g1, self.g2])
        db.session.commit()
        self.make("admin", "admin")
        self.make("bib", "manager")
        self.make("bib1", "manager", groups=[self.g1])
        self.make("leser", "reader")
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def make(self, name, role, groups=()):
        u = User(username=name, display_name=name.title(), role=role, auth_source="local")
        u.set_password("geheim12345")
        u.groups = list(groups)
        db.session.add(u)
        db.session.commit()
        return u

    def login(self, name):
        self.c.post("/logout")
        self.c.post("/login", data={"username": name, "password": "geheim12345"})

    def add(self, *names):
        return self.c.post("/standorte/neu", data={"names": "\n".join(names)}, follow_redirects=True)

    def test_without_list_free_text_as_before(self):
        self.login("bib")
        html = self.c.get("/gegenstaende/neu").get_data(as_text=True)
        self.assertIn('<input id="f-location" type="text" name="location"', html)
        self.c.post("/gegenstaende/neu", data={"name": "Faust", "location": "irgendwo"})
        self.assertEqual(Asset.query.one().location, "irgendwo")

    def test_create_several_and_choose_from_list(self):
        self.login("bib")
        r = self.add("Regal 10", "Regal 2", "  Lehrer   zimmer ", "regal 2")
        self.assertIn("3 Standort(e) angelegt", r.get_data(as_text=True))
        names = [l.name for l in Location.query.order_by(Location.name)]
        self.assertEqual(sorted(names), ["Lehrer zimmer", "Regal 10", "Regal 2"])
        html = self.c.get("/gegenstaende/neu").get_data(as_text=True)
        self.assertIn('<select id="f-location" name="location">', html)
        self.assertLess(html.index(">Regal 2<"), html.index(">Regal 10<"))   # natürliche Sortierung
        self.c.post("/gegenstaende/neu", data={"name": "Faust", "location": "REGAL 2"})
        self.assertEqual(Asset.query.one().location, "Regal 2")                 # Schreibweise angeglichen

    def test_typo_is_rejected(self):
        self.login("bib")
        self.add("Regal 2")
        r = self.c.post("/gegenstaende/neu", data={"name": "Faust", "location": "Regl 2"}, follow_redirects=True)
        self.assertIn("nicht festgelegt", r.get_data(as_text=True))
        self.assertEqual(Asset.query.count(), 0)

    def test_existing_unlisted_value_may_stay_on_edit(self):
        b = Asset(name="Alt", location="Keller")
        db.session.add(b)
        db.session.commit()
        self.login("bib")
        self.add("Regal 2")
        html = self.c.get(f"/gegenstaende/{b.id}/bearbeiten").get_data(as_text=True)
        self.assertIn("Keller (nicht in der Liste)", html)
        self.c.post(f"/gegenstaende/{b.id}/bearbeiten", data={"name": "Alt 2", "location": "Keller", "quantity": "1"})
        db.session.refresh(b)
        self.assertEqual((b.name, b.location), ("Alt 2", "Keller"))

    def test_rename_updates_assets(self):
        self.login("bib")
        self.add("Regal B3")
        db.session.add_all([Asset(name="A", location="Regal B3"), Asset(name="B", location="Regal B3")])
        db.session.commit()
        loc = Location.query.one()
        r = self.c.post(f"/standorte/{loc.id}/umbenennen", data={"name": "Regal B 3"}, follow_redirects=True)
        self.assertIn("2 Gegenstände angepasst", r.get_data(as_text=True))
        self.assertEqual({b.location for b in Asset.query}, {"Regal B 3"})

    def test_delete_only_when_unused(self):
        self.login("bib")
        self.add("Regal 1", "Regal 2")
        db.session.add(Asset(name="A", location="Regal 1"))
        db.session.commit()
        used, unused = Location.query.filter_by(name="Regal 1").one(), Location.query.filter_by(name="Regal 2").one()
        r = self.c.post(f"/standorte/{used.id}/loeschen", follow_redirects=True)
        self.assertIn("noch bei 1 Gegenstand eingetragen", r.get_data(as_text=True))
        self.c.post(f"/standorte/{unused.id}/loeschen")
        self.assertEqual([l.name for l in Location.query], ["Regal 1"])

    def test_merge_typos_from_existing_assets(self):
        db.session.add_all([Asset(name="A", location="Regal B3"), Asset(name="B", location="Regl B3"),
                            Asset(name="C", location="regal b3 "), Asset(name="D", location="Keller")])
        db.session.commit()
        self.login("bib")
        html = self.c.get("/standorte").get_data(as_text=True)
        self.assertIn("Im Bestand, aber nicht festgelegt", html)
        self.c.post("/standorte/bestand", data={"value": "Regal B3", "target": "__neu__"})
        target = Location.query.filter_by(name="Regal B3").one()
        self.c.post("/standorte/bestand", data={"value": "Regl B3", "target": str(target.id)})
        self.c.post("/standorte/bestand", data={"value": "regal b3 ", "target": str(target.id)})
        self.assertEqual(sorted(b.location for b in Asset.query), ["Keller", "Regal B3", "Regal B3", "Regal B3"])
        html = self.c.get("/standorte").get_data(as_text=True)
        self.assertIn("Keller", html)
        self.assertNotIn('value="Regl B3"', html)   # nicht mehr in der Liste der offenen Einträge

    def test_suggestions_for_variants_and_typos(self):
        db.session.add_all([Location(name="Regal A1"), Location(name="Regal A2"),
                            Asset(name="A", location="regal a2"), Asset(name="B", location="Regl A1"),
                            Asset(name="C", location="Keller")])
        db.session.commit()
        self.login("bib")
        html = self.c.get("/standorte").get_data(as_text=True)
        a1, a2 = Location.query.filter_by(name="Regal A1").one(), Location.query.filter_by(name="Regal A2").one()
        self.assertIn("regal a2", html)                          # Groß/Klein-Variante wird angezeigt …
        self.assertIn("Andere Schreibweise von", html)
        self.assertIn(f'<option value="{a2.id}" selected>', html)   # … mit passendem Vorschlag
        self.assertIn("Tippfehler? Ähnlich wie", html)
        self.assertIn(f'<option value="{a1.id}" selected>', html)
        self.assertIn('<option value="__neu__" selected>', html)    # „Keller“: kein Vorschlag

    def test_group_manager_cannot_change_other_groups_assets(self):
        db.session.add_all([Asset(name="Eigenes", location="Regl 1", group_id=self.g1.id),
                            Asset(name="Fremdes", location="Regl 1", group_id=self.g2.id)])
        db.session.add(Location(name="Regal 1"))
        db.session.commit()
        self.login("bib1")
        target = Location.query.one()
        r = self.c.post("/standorte/bestand", data={"value": "Regl 1", "target": str(target.id)}, follow_redirects=True)
        self.assertIn("anderer Gruppen", r.get_data(as_text=True))
        self.assertEqual({b.location for b in Asset.query}, {"Regl 1"})
        self.login("admin")
        self.c.post("/standorte/bestand", data={"value": "Regl 1", "target": str(target.id)})
        self.assertEqual({b.location for b in Asset.query}, {"Regal 1"})

    def test_catalog_filter(self):
        db.session.add_all([Asset(name="Im Regal", location="Regal 1"), Asset(name="Im Keller", location="Keller")])
        db.session.add(Location(name="Regal 1"))
        db.session.commit()
        self.login("leser")
        html = self.c.get("/gegenstaende?standort=Regal+1").get_data(as_text=True)
        self.assertIn("Im Regal", html)
        self.assertNotIn("Im Keller", html)
        self.assertIn("Alle Standorte", html)

    def test_readers_cannot_manage(self):
        self.login("leser")
        self.assertNotEqual(self.c.get("/standorte").status_code, 200)
        self.add("Regal 9")
        self.assertEqual(Location.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
