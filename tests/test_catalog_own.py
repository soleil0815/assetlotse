"""Default since 1.4.0: staff see no stock, only the items issued to them."""
import pytest
from test_assetlotse import KEY, PW, Base

from assetlotse.models import DEFAULT_SETTINGS, Loan, db, get_setting, set_setting

pytestmark = pytest.mark.catalog_default


class OwnItemsOnlyTests(Base):
    def give(self, asset, user):
        db.session.add(Loan(asset_id=asset.id, user_id=user.id, issued_by_id=self.vera.id))
        db.session.commit()

    def test_default_is_own(self):
        self.assertEqual(DEFAULT_SETTINGS["catalog_visibility"], "own")
        self.assertEqual(get_setting("catalog_visibility"), "own")

    def test_staff_see_no_catalog(self):
        a = self.laptop(secret=None)
        self.laptop(secret=None, name="Beamer", inventory_no="B-1", serial_number="B1")
        self.login("mia")
        r = self.c.get("/gegenstaende?q=Beamer")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/ausleihen"))
        self.assertEqual(self.c.get(f"/gegenstaende/{a.id}").status_code, 404)
        html = self.c.get("/").get_data(as_text=True)
        self.assertNotIn('href="/gegenstaende"', html)      # no menu item, no search
        self.assertNotIn("Neu im Bestand", html)
        self.assertNotIn("Beamer", html)
        help_html = self.c.get("/hilfe").get_data(as_text=True)
        self.assertNotIn("Gegenstände finden", help_html)

    def test_staff_see_own_item_and_history_without_link(self):
        a = self.laptop(secret=None)
        self.give(a, self.mia)
        self.login("mia")
        self.assertEqual(self.c.get(f"/gegenstaende/{a.id}").status_code, 200)
        self.assertIn("Laptop Dell", self.c.get("/ausleihen").get_data(as_text=True))
        loan = Loan.query.one()
        from datetime import datetime
        loan.returned_at = datetime.now()
        db.session.commit()
        self.assertEqual(self.c.get(f"/gegenstaende/{a.id}").status_code, 404)
        hist = self.c.get("/ausleihen?ansicht=verlauf").get_data(as_text=True)
        self.assertIn("Laptop Dell", hist)
        self.assertNotIn(f'href="/gegenstaende/{a.id}"', hist)

    def test_no_self_checkout_even_if_enabled(self):
        set_setting("self_checkout", "1")
        db.session.commit()
        a = self.laptop(secret=None)
        self.login("mia")
        self.assertEqual(self.c.post(f"/gegenstaende/{a.id}/ausgeben", data={}).status_code, 404)
        self.assertEqual(Loan.query.count(), 0)

    def test_managers_unaffected(self):
        a = self.laptop(secret=None)
        self.login("vera")
        self.assertEqual(self.c.get("/gegenstaende").status_code, 200)
        self.assertEqual(self.c.get(f"/gegenstaende/{a.id}").status_code, 200)
        self.assertIn('href="/gegenstaende"', self.c.get("/").get_data(as_text=True))

    def test_switch_back_to_groups(self):
        a = self.laptop(secret=None)
        self.login("admin")
        r = self.c.get("/admin/einstellungen")
        self.assertIn('value="own" selected', r.get_data(as_text=True).replace("  ", " "))
        set_setting("catalog_visibility", "groups")
        db.session.commit()
        self.login("mia")
        self.assertEqual(self.c.get("/gegenstaende").status_code, 200)
        self.assertEqual(self.c.get(f"/gegenstaende/{a.id}").status_code, 200)

    def test_unknown_value_counts_as_own(self):
        set_setting("catalog_visibility", "xyz")
        db.session.commit()
        self.login("mia")
        self.assertEqual(self.c.get("/gegenstaende").status_code, 302)

    def test_holder_secret_still_works(self):
        a = self.laptop(secret_holder_view=True)
        self.give(a, self.mia)
        self.login("mia")
        self.assertIn(KEY, self.c.post(f"/gegenstaende/{a.id}/mein-schluessel", data={"password": PW})
                      .get_data(as_text=True))

    # ---- findings from the code review

    def test_self_return_from_item_page_lands_on_my_items(self):
        set_setting("self_return", "1")
        db.session.commit()
        a = self.laptop(secret=None)
        self.give(a, self.mia)
        self.login("mia")
        html = self.c.get(f"/gegenstaende/{a.id}").get_data(as_text=True)
        self.assertIn('name="next" value="/ausleihen"', html)
        loan = Loan.query.one()
        r = self.c.post(f"/ausleihen/{loan.id}/rueckgabe", data={"next": "/ausleihen"}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(db.session.get(Loan, loan.id).returned_at)

    def test_profile_history_has_no_dead_links(self):
        a = self.laptop(secret=None)
        self.give(a, self.mia)
        from datetime import datetime
        Loan.query.one().returned_at = datetime.now()
        db.session.commit()
        self.login("mia")
        html = self.c.get("/profil").get_data(as_text=True)
        self.assertIn("Laptop Dell", html)
        self.assertNotIn(f'href="/gegenstaende/{a.id}"', html)

    def test_help_does_not_point_to_inventory(self):
        self.login("mia")
        html = self.c.get("/hilfe?thema=borrow").get_data(as_text=True)
        self.assertNotIn("Such im <span class=\"ui\">Bestand</span>", html)
        self.assertIn("Ausgabetheke", html)

    def test_item_page_shows_no_stock_of_others(self):
        a = self.laptop(secret=None, quantity=20, name="HDMI-Kabel", inventory_no="", serial_number="")
        self.give(a, self.mia)
        self.login("mia")
        html = self.c.get(f"/gegenstaende/{a.id}").get_data(as_text=True)
        self.assertNotIn("Stück verfügbar", html)
        self.assertIn("Dieser Gegenstand ist Ihnen ausgegeben", html)

    def test_existing_and_missing_ids_look_the_same(self):
        a = self.laptop(secret=None)
        other = self.make("otto", "reader", [self.it])
        self.give(a, other)
        loan = Loan.query.one()
        self.login("mia")
        for url in (f"/gegenstaende/{a.id}/ausgeben", "/gegenstaende/9999/ausgeben",
                    f"/gegenstaende/{a.id}/mein-schluessel", "/gegenstaende/9999/mein-schluessel",
                    f"/ausleihen/{loan.id}/rueckgabe", "/ausleihen/9999/rueckgabe",
                    f"/ausleihen/{loan.id}/verlaengern", "/ausleihen/9999/verlaengern"):
            self.assertEqual(self.c.post(url, data={"password": PW}).status_code, 404, url)

    def test_settings_round_trip(self):
        self.login("admin")
        for value, stored in (("groups", "groups"), ("all", "all"), ("own", "own"), ("quatsch", "own")):
            self.c.post("/admin/einstellungen", data={"catalog_visibility": value, "org_name": "X"})
            db.session.expire_all()
            self.assertEqual(get_setting("catalog_visibility"), stored)
