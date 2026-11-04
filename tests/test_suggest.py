"""Suggestions while typing (top search, issue desk) and checking of inventory/serial numbers."""
import pytest
from test_assetlotse import Base

from assetlotse.models import Asset, Loan, db, set_setting


class SuggestTests(Base):
    def items(self, **params):
        r = self.c.get("/vorschlaege", query_string=params)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")
        return r.get_json()["items"]

    def test_search_finds_by_name_number_and_serial(self):
        self.laptop(secret=None)
        db.session.add(Asset(name="Externe Festplatte 2 TB", category="Festplatte", inventory_no="IT-0207",
                             serial_number="WX12", quantity=1))
        db.session.commit()
        self.login("vera")
        self.assertEqual([i["name"] for i in self.items(q="festpl")], ["Externe Festplatte 2 TB"])
        self.assertEqual([i["name"] for i in self.items(q="IT-0207")], ["Externe Festplatte 2 TB"])
        self.assertEqual([i["name"] for i in self.items(q="wx12")], ["Externe Festplatte 2 TB"])
        hit = self.items(q="Laptop")[0]
        self.assertEqual((hit["available"], hit["quantity"]), (1, 1))
        self.assertTrue(hit["url"].startswith("/gegenstaende/"))
        self.assertEqual(self.items(q=""), [])

    def test_exact_number_first(self):
        self.laptop(secret=None, name="B-Gerät", inventory_no="X-10")
        self.laptop(secret=None, name="A-Gerät", inventory_no="X-1")
        self.login("vera")
        self.assertEqual(self.items(q="x-1")[0]["name"], "A-Gerät")

    def test_search_respects_visibility(self):
        self.laptop(secret=None, group_id=self.hr.id, name="Personal-Laptop")
        self.login("mia")                                   # staff member of group IT, default "groups"
        self.assertEqual(self.items(q="Personal"), [])

    @pytest.mark.catalog_default
    def test_search_empty_for_staff_without_catalog(self):
        self.laptop(secret=None)
        self.login("mia")
        self.assertEqual(self.items(q="Laptop"), [])

    def test_desk_only_for_managers_and_respects_groups(self):
        self.laptop(secret=None, name="IT-Laptop")
        self.laptop(secret=None, name="HR-Laptop", group_id=self.hr.id)
        self.login("mia")
        self.assertEqual(self.c.get("/vorschlaege?fuer=theke&q=Laptop").status_code, 403)
        self.login("vera")                                  # manages IT only
        names = [i["name"] for i in self.items(fuer="theke", q="Laptop")]
        self.assertEqual(names, ["IT-Laptop"])
        self.login("admin")
        hits = {i["name"]: i for i in self.items(fuer="theke", q="Laptop", benutzer="mia")}
        self.assertEqual(set(hits), {"IT-Laptop"})          # mia is not in Personal
        self.assertTrue(hits["IT-Laptop"]["can_issue"])

    def test_desk_marks_issued_items(self):
        a = self.laptop(secret=None)
        db.session.add(Loan(asset_id=a.id, user_id=self.admin.id))
        db.session.commit()
        self.login("vera")
        hit = self.items(fuer="theke", q="Laptop", benutzer="mia")[0]
        self.assertFalse(hit["can_issue"])
        self.assertEqual(hit["available"], 0)

    def test_desk_issue_via_suggestion(self):
        a = self.laptop(secret=None)
        self.login("vera")
        self.c.post("/theke", data={"modus": "ausgabe", "benutzer": "mia", "code": "Lap", "asset_id": a.id})
        self.assertEqual(Loan.query.filter_by(asset_id=a.id, user_id=self.mia.id).count(), 1)

    def test_desk_page_and_navbar_wired(self):
        self.login("vera")
        html = self.c.get("/theke?modus=ausgabe&benutzer=mia").get_data(as_text=True)
        self.assertIn('data-suggest-for="theke"', html)
        self.assertIn('data-suggest-user="mia"', html)
        self.assertIn('data-suggest-for="suche"', html)


class NumberTests(Base):
    def check(self, **params):
        r = self.c.get("/gegenstaende/nummer-pruefen", query_string=params)
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_taken_free_and_next(self):
        for n in ("IT-0041", "IT-0043"):
            self.laptop(secret=None, inventory_no=n, serial_number=f"S{n}")
        self.login("vera")
        d = self.check(feld="inventory_no", wert="it-0043")
        self.assertTrue(d["taken"])
        self.assertEqual(d["by"]["name"], "Laptop Dell")
        self.assertEqual(d["next"], "IT-0044")
        d = self.check(feld="inventory_no", wert="IT-00")
        self.assertFalse(d["taken"])
        self.assertEqual(d["numbers"], ["IT-0043", "IT-0041"])
        self.assertEqual(d["next"], "IT-0044")
        self.assertEqual(self.check(feld="inventory_no", wert="")["next"], "IT-0044")   # pattern of the last number
        self.assertFalse(self.check(feld="serial_number", wert="neu")["taken"])
        self.assertTrue(self.check(feld="serial_number", wert="sit-0041")["taken"])

    def test_own_item_does_not_count_when_editing(self):
        a = self.laptop(secret=None)
        self.login("vera")
        self.assertFalse(self.check(feld="inventory_no", wert="IT-0042", ausser=a.id)["taken"])

    def test_other_groups_items_are_not_named(self):
        self.laptop(secret=None, inventory_no="HR-1", group_id=self.hr.id)
        self.login("vera")
        d = self.check(feld="inventory_no", wert="HR-1")
        self.assertTrue(d["taken"])
        self.assertIsNone(d["by"])
        self.assertEqual(d["numbers"], [])
        r = self.c.post("/gegenstaende/neu", data={"name": "X", "inventory_no": "hr-1", "group_id": self.it.id,
                                                   "quantity": "1"}, follow_redirects=True)
        self.assertIn("bei einem Gegenstand einer anderen Gruppe", r.get_data(as_text=True))

    def test_only_managers(self):
        self.login("mia")
        self.assertEqual(self.c.get("/gegenstaende/nummer-pruefen?wert=x").status_code, 403)
        self.login("vera")
        self.assertEqual(self.c.get("/gegenstaende/nummer-pruefen?feld=name&wert=x").status_code, 400)

    def test_like_wildcards_are_literal(self):
        self.laptop(secret=None, inventory_no="A_1")
        self.laptop(secret=None, inventory_no="AB1")
        self.login("vera")
        self.assertEqual(self.check(feld="inventory_no", wert="A_")["numbers"], ["A_1"])

    def test_edit_rejects_duplicate_but_keeps_legacy_duplicate(self):
        a = self.laptop(secret=None)
        b = self.laptop(secret=None)
        self.login("vera")
        data = {"name": "B", "group_id": self.it.id, "quantity": "1", "inventory_no": a.inventory_no,
                "serial_number": b.serial_number}
        html = self.c.post(f"/gegenstaende/{b.id}/bearbeiten", data=data, follow_redirects=True).get_data(as_text=True)
        self.assertIn("ist bereits vergeben", html)
        db.session.expire_all()
        self.assertNotEqual(db.session.get(Asset, b.id).inventory_no, a.inventory_no)
        self.assertNotEqual(db.session.get(Asset, b.id).name, "B")   # nothing half-saved


class UniqueNumberReviewTests(Base):
    """Findings from the code review: umlauts, concurrent saving, legacy data, wildcards."""

    def post_new(self, **data):
        base = {"name": "Neu", "group_id": self.it.id, "quantity": "1"}
        base.update(data)
        return self.c.post("/gegenstaende/neu", data=base, follow_redirects=True).get_data(as_text=True)

    def test_umlauts_any_case_are_the_same_number(self):
        self.laptop(secret=None, inventory_no="Üa-1", serial_number="Straße-9")
        self.login("vera")
        for field, value in (("inventory_no", "üa-1"), ("inventory_no", "ÜA-1"), ("serial_number", "STRASSE-9")):
            self.assertIn("ist bereits vergeben", self.post_new(**{field: value}))
        self.assertEqual(Asset.query.count(), 1)

    def test_index_catches_race_and_keeps_input(self):
        from unittest import mock
        self.laptop(secret=None)
        b = self.laptop(secret=None)
        self.login("vera")
        with mock.patch("assetlotse.views.assets.find_number", return_value=None):   # app check "missed"
            html = self.post_new(name="Rennen", inventory_no="IT-0042")
            self.assertIn("gerade anderweitig vergeben", html)
            self.assertIn('value="Rennen"', html)
            html = self.c.post(f"/gegenstaende/{b.id}/bearbeiten", follow_redirects=True,
                               data={"name": "Geändert", "group_id": self.it.id, "quantity": "1",
                                     "inventory_no": "IT-0042"}).get_data(as_text=True)
            self.assertIn("gerade anderweitig vergeben", html)
            self.assertIn('value="Geändert"', html)          # input is kept
        self.assertEqual(Asset.query.count(), 2)
        db.session.expire_all()
        self.assertNotEqual(db.session.get(Asset, b.id).name, "Geändert")

    def test_keys_filled_for_old_rows_and_empty_numbers_allowed(self):
        from assetlotse.models import fill_number_keys
        db.session.add_all([Asset(name="ohne Nummer 1"), Asset(name="ohne Nummer 2")])
        db.session.commit()
        a = self.laptop(secret=None, inventory_no="X-1")
        db.session.execute(db.update(Asset).where(Asset.id == a.id).values(inventory_key=None))
        db.session.commit()
        self.assertEqual(fill_number_keys(), 1)
        db.session.expire_all()
        self.assertEqual(db.session.get(Asset, a.id).inventory_key, "x-1")
        self.assertIsNone(Asset.query.filter_by(name="ohne Nummer 1").one().inventory_key)

    def test_suggest_treats_wildcards_literally(self):
        self.laptop(secret=None, name="A_B")
        self.laptop(secret=None, name="AxB")
        self.login("vera")
        self.assertEqual([i["name"] for i in self.c.get("/vorschlaege?q=A_").get_json()["items"]], ["A_B"])

    def test_next_number_from_own_groups_only_when_empty(self):
        self.laptop(secret=None, inventory_no="IT-0001")
        self.laptop(secret=None, inventory_no="HR-0007", group_id=self.hr.id)   # created last, other group
        self.login("vera")
        d = self.c.get("/gegenstaende/nummer-pruefen?feld=inventory_no&wert=").get_json()
        self.assertEqual(d["next"], "IT-0002")
        self.assertIsNone(self.c.get("/gegenstaende/nummer-pruefen?feld=inventory_no&wert=I").get_json()["next"])
