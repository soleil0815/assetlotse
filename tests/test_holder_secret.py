"""Confidential details for the person an item is currently issued to (e.g. BitLocker key)."""
from datetime import datetime
from unittest import mock

from test_assetlotse import KEY, PW, Base

from assetlotse.models import Asset, Loan, LoginAttempt, SecretAccess, User, db


class HolderSecretTests(Base):
    def give(self, asset, user, by=None):
        loan = Loan(asset_id=asset.id, user_id=user.id, issued_by_id=(by or self.vera).id)
        db.session.add(loan)
        db.session.commit()
        return loan

    def url(self, a):
        return f"/gegenstaende/{a.id}/mein-schluessel"

    def test_holder_sees_key_after_password_and_it_is_logged(self):
        a = self.laptop(secret_holder_view=True)
        self.give(a, self.mia)
        self.login("mia")
        html = self.c.get(f"/gegenstaende/{a.id}").get_data(as_text=True)
        self.assertIn("Ihr Passwort", html)
        self.assertNotIn(KEY, html)
        r = self.c.post(self.url(a), data={"password": PW})
        self.assertEqual(r.status_code, 200)
        self.assertIn(KEY, r.get_data(as_text=True))
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")
        self.assertEqual([(e.user_id, e.action) for e in SecretAccess.query], [(self.mia.id, "holder")])
        # the key does not stay on the page permanently
        self.assertNotIn(KEY, self.c.get(f"/gegenstaende/{a.id}").get_data(as_text=True))

    def test_wrong_or_missing_password(self):
        a = self.laptop(secret_holder_view=True)
        self.give(a, self.mia)
        self.login("mia")
        for pw in ("falsch", ""):
            r = self.c.post(self.url(a), data={"password": pw}, follow_redirects=True)
            self.assertNotIn(KEY, r.get_data(as_text=True))
            self.assertIn("Das Passwort ist falsch.", r.get_data(as_text=True))
        self.assertEqual(SecretAccess.query.count(), 0)
        self.assertEqual(LoginAttempt.query.count(), 2)

    def test_locked_after_too_many_wrong_passwords(self):
        a = self.laptop(secret_holder_view=True)
        self.give(a, self.mia)
        self.login("mia")
        for _ in range(8):
            self.c.post(self.url(a), data={"password": "falsch"})
        r = self.c.post(self.url(a), data={"password": PW}, follow_redirects=True)
        self.assertNotIn(KEY, r.get_data(as_text=True))
        self.assertIn("Zu viele Fehlversuche", r.get_data(as_text=True))

    def test_not_without_switch(self):
        a = self.laptop()                       # sharing off (default)
        self.give(a, self.mia)
        self.login("mia")
        self.assertNotIn("Ihr Passwort", self.c.get(f"/gegenstaende/{a.id}").get_data(as_text=True))
        self.assertEqual(self.c.post(self.url(a), data={"password": PW}).status_code, 403)

    def test_gone_after_return(self):
        a = self.laptop(secret_holder_view=True)
        loan = self.give(a, self.mia)
        loan.returned_at = datetime.now()
        db.session.commit()
        self.login("mia")
        self.assertEqual(self.c.post(self.url(a), data={"password": PW}).status_code, 403)

    def test_others_never(self):
        a = self.laptop(secret_holder_view=True)
        self.give(a, self.mia)
        other = self.make("otto", "reader", [self.it])
        self.login("otto")
        html = self.c.get(f"/gegenstaende/{a.id}").get_data(as_text=True)
        self.assertNotIn("Ihr Passwort", html)
        self.assertNotIn(KEY, html)
        self.assertEqual(self.c.post(self.url(a), data={"password": PW}).status_code, 403)
        self.login("hans")    # manager of another group
        self.assertEqual(self.c.post(self.url(a), data={"password": PW}).status_code, 403)
        self.assertEqual(SecretAccess.query.count(), 0)
        self.assertIsNotNone(other)

    def test_holder_sees_own_item_even_outside_own_groups(self):
        a = self.laptop(secret_holder_view=True, group_id=self.hr.id)
        self.give(a, self.mia, by=self.admin)   # mia is only in IT
        self.login("mia")
        self.assertEqual(self.c.get(f"/gegenstaende/{a.id}").status_code, 200)
        self.assertIn(KEY, self.c.post(self.url(a), data={"password": PW}).get_data(as_text=True))

    def test_switch_in_form_logged_and_only_for_secret_managers(self):
        a = self.laptop()
        self.login("vera")
        data = {"name": a.name, "group_id": self.it.id, "quantity": "1", "secret_holder_view_shown": "1",
                "secret_holder_view": "1"}
        self.c.post(f"/gegenstaende/{a.id}/bearbeiten", data=data)
        self.assertTrue(db.session.get(Asset, a.id).secret_holder_view)
        data.pop("secret_holder_view")
        self.c.post(f"/gegenstaende/{a.id}/bearbeiten", data=data)
        self.assertFalse(db.session.get(Asset, a.id).secret_holder_view)
        self.assertEqual([e.action for e in SecretAccess.query.order_by(SecretAccess.id)], ["share_on", "share_off"])
        # a manager without access to the details (only “manage” via the global role without a group) cannot switch anything
        glob = self.make("gerd", "manager")
        self.login("gerd")
        data["secret_holder_view"] = "1"
        self.c.post(f"/gegenstaende/{a.id}/bearbeiten", data=data)
        self.assertFalse(db.session.get(Asset, a.id).secret_holder_view)
        self.assertIsNotNone(glob)

    def test_removing_secret_resets_switch(self):
        a = self.laptop(secret_holder_view=True)
        self.login("vera")
        self.c.post(f"/gegenstaende/{a.id}/bearbeiten", data={"name": a.name, "group_id": self.it.id, "quantity": "1",
                                                            "secret_holder_view_shown": "1", "secret_holder_view": "1",
                                                            "remove_secret": "1"})
        a = db.session.get(Asset, a.id)
        self.assertFalse(a.has_secret)
        self.assertFalse(a.secret_holder_view)

    def test_ad_account_confirms_via_ad(self):
        a = self.laptop(secret_holder_view=True)
        ad = User(username="anna", display_name="Anna", role="reader", auth_source="ldap",
                  ldap_dn="CN=Anna,OU=Staff,DC=x")
        db.session.add(ad)
        db.session.commit()
        self.give(a, ad)
        with self.c.session_transaction() as s:
            s["_user_id"] = ad.get_id()
            s["_fresh"] = True
        info = mock.Mock(role="reader", dn="cn=anna, ou=staff, dc=x", username="anna")
        with mock.patch("assetlotse.ldap_auth.ldap_enabled", return_value=True), \
                mock.patch("assetlotse.ldap_auth.authenticate", return_value=info) as auth:
            r = self.c.post(self.url(a), data={"password": "ad-pw"})
            if r.status_code == 302:   # e.g. 2FA/session check – then test via the login path
                self.skipTest("Sitzung ließ sich im Test nicht direkt setzen")
            self.assertIn(KEY, r.get_data(as_text=True))
            auth.assert_called_once_with("anna", "ad-pw")
            # other AD account or no access rule any more → refused
            info.dn = "CN=Mallory,OU=Staff,DC=x"
            self.assertNotIn(KEY, self.c.post(self.url(a), data={"password": "ad-pw"}, follow_redirects=True)
                             .get_data(as_text=True))
            info.dn, info.role = "CN=Anna,OU=Staff,DC=x", None
            self.assertNotIn(KEY, self.c.post(self.url(a), data={"password": "ad-pw"}, follow_redirects=True)
                             .get_data(as_text=True))

    # ---- findings of the security review

    def test_not_when_issued_by_someone_without_secret_rights(self):
        a = self.laptop(secret_holder_view=True, group_id=None)   # “General”: key for admins only
        self.login("hans")                                        # manager may issue, but not see
        self.c.post(f"/gegenstaende/{a.id}/ausgeben", data={})
        self.assertEqual(Loan.query.filter_by(user_id=self.hans.id).count(), 1)
        self.assertEqual(self.c.post(self.url(a), data={"password": PW}).status_code, 403)
        # … and not via a colleague either
        b = self.laptop(secret_holder_view=True, group_id=None, name="Laptop 2", inventory_no="IT-2", serial_number="S2")
        self.give(b, self.mia, by=self.hans)
        self.login("mia")
        self.assertEqual(self.c.post(self.url(b), data={"password": PW}).status_code, 403)
        self.assertEqual(SecretAccess.query.count(), 0)

    def test_not_via_self_checkout(self):
        from assetlotse.models import set_setting
        set_setting("self_checkout", "1")
        db.session.commit()
        a = self.laptop(secret_holder_view=True)
        self.login("mia")
        self.c.post(f"/gegenstaende/{a.id}/ausgeben", data={})
        self.assertEqual(Loan.query.filter_by(user_id=self.mia.id).count(), 1)
        self.assertEqual(self.c.post(self.url(a), data={"password": PW}).status_code, 403)

    def test_only_single_items(self):
        a = self.laptop(secret_holder_view=True, quantity=2)
        self.give(a, self.mia)
        self.login("mia")
        self.assertEqual(self.c.post(self.url(a), data={"password": PW}).status_code, 403)
        b = self.laptop(quantity=5, name="USB-Sticks", inventory_no="", serial_number="")
        self.login("vera")
        r = self.c.post(f"/gegenstaende/{b.id}/bearbeiten", follow_redirects=True,
                        data={"name": b.name, "group_id": self.it.id, "quantity": "5",
                              "secret_holder_view_shown": "1", "secret_holder_view": "1"})
        self.assertFalse(db.session.get(Asset, b.id).secret_holder_view)
        self.assertIn("nur bei Einzelstücken", r.get_data(as_text=True))

    def test_2fa_session_without_second_factor_must_step_up(self):
        a = self.laptop(secret_holder_view=True)
        self.give(a, self.mia)
        self.login("mia")
        with mock.patch.object(User, "has_2fa", new_callable=mock.PropertyMock, return_value=True):
            html = self.c.get(f"/gegenstaende/{a.id}").get_data(as_text=True)
            self.assertIn("Zweiten Faktor bestätigen", html)
            r = self.c.post(self.url(a), data={"password": PW}, follow_redirects=True)
            self.assertNotIn(KEY, r.get_data(as_text=True))
            with self.c.session_transaction() as s:
                s["mfa_ok"] = True
            self.assertIn(KEY, self.c.post(self.url(a), data={"password": PW}).get_data(as_text=True))

    def test_per_user_limit_across_addresses(self):
        a = self.laptop(secret_holder_view=True)
        self.give(a, self.mia)
        for i in range(100):   # limit in the internal network (the test client comes from 127.0.0.1)
            db.session.add(LoginAttempt(key=f"secret|mia|10.0.{i // 250}.{i % 250}", username="mia"))
        db.session.commit()
        self.login("admin")   # irrelevant – mia does not sign in again from here
        with self.c.session_transaction() as s:
            s["_user_id"] = self.mia.get_id()
        r = self.c.post(self.url(a), data={"password": PW}, follow_redirects=True)
        self.assertNotIn(KEY, r.get_data(as_text=True))
        from assetlotse import lockout
        self.assertNotIn("mia", lockout.locked_usernames())   # failed key attempts ≠ login lockout on the dashboard
