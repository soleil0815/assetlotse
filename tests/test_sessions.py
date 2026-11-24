"""Server-side sessions (2.10.0): signing out makes a copied session or "stay signed in" cookie worthless."""
from datetime import datetime, timedelta

from test_app import BaseCase

from assetlotse import sessions
from assetlotse.models import User, UserSession, db, set_setting


class SessionTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.anna = self.make_user("anna")
        # the test keeps one app context open for all clients – Flask-Login caches the user in it (g); several
        # browsers here must not see each other's user, so forget it before every request
        from flask import g
        self.app.url_value_preprocessor(lambda endpoint, values: g.pop("_login_user", None))

    def sign_in(self, client, remember=False):
        r = client.post("/login", data={"username": "anna", "password": "geheim123",
                                        **({"remember": "1"} if remember else {})})
        self.assertEqual(r.status_code, 302)
        return client

    def signed_in(self, client) -> bool:
        return client.get("/").status_code == 200

    def copy(self, source, *names):
        """A second browser with copies of the given cookies (e.g. taken from a shared computer)."""
        other = self.app.test_client()
        for name in names:
            cookie = source.get_cookie(name)
            if cookie is not None:
                other.set_cookie(name, cookie.value)
        return other

    def test_copied_session_cookie_is_worthless_after_sign_out(self):
        a = self.sign_in(self.app.test_client())
        thief = self.copy(a, "session")
        self.assertTrue(self.signed_in(thief))           # the copy works while the session exists …
        a.post("/logout")
        self.assertFalse(self.signed_in(a))
        self.assertFalse(self.signed_in(thief))          # … and no longer after signing out
        self.assertEqual(UserSession.query.count(), 0)

    def test_copied_remember_cookie_is_worthless_after_sign_out(self):
        a = self.sign_in(self.app.test_client(), remember=True)
        self.assertIsNotNone(a.get_cookie("remember_token"))
        thief = self.copy(a, "remember_token")           # only the "stay signed in" cookie
        self.assertTrue(self.signed_in(thief))
        a.post("/logout")
        self.assertFalse(self.signed_in(self.copy(a, "remember_token")))
        fresh = self.app.test_client()
        fresh.set_cookie("remember_token", thief.get_cookie("remember_token").value)
        self.assertFalse(self.signed_in(fresh))

    def test_each_sign_in_is_its_own_session(self):
        a = self.sign_in(self.app.test_client())
        b = self.sign_in(self.app.test_client())
        self.assertEqual(UserSession.query.filter_by(user_id=self.anna.id).count(), 2)
        a.post("/logout")
        self.assertFalse(self.signed_in(a))
        self.assertTrue(self.signed_in(b))               # the other browser stays signed in

    def test_sign_out_everywhere_and_new_password_end_the_others(self):
        a = self.sign_in(self.app.test_client())
        b = self.sign_in(self.app.test_client())
        thief = self.copy(b, "session")
        b.post("/abmelden-ueberall")
        self.assertTrue(self.signed_in(b))
        self.assertFalse(self.signed_in(a))
        self.assertFalse(self.signed_in(thief))
        c = self.sign_in(self.app.test_client())
        b.post("/profil", data={"action": "password", "current": "geheim123", "new": "neu-geheim-123",
                                "new2": "neu-geheim-123"})
        self.assertTrue(self.signed_in(b))
        self.assertFalse(self.signed_in(c))
        self.assertEqual(UserSession.query.filter_by(user_id=self.anna.id).count(), 1)

    def test_only_a_hash_of_the_token_is_stored(self):
        a = self.sign_in(self.app.test_client())
        with a.session_transaction() as s:
            token = s["_user_id"].split(":")[2]
        self.assertGreaterEqual(len(token), 40)
        row = UserSession.query.one()
        self.assertNotIn(token, row.token_hash)
        self.assertEqual(len(row.token_hash), 64)

    def test_expired_session_rejected_and_cleaned_up(self):
        a = self.sign_in(self.app.test_client())
        row = UserSession.query.one()
        row.last_seen = datetime.now() - sessions.MAX_AGE - timedelta(minutes=1)
        db.session.commit()
        self.assertFalse(self.signed_in(a))
        self.assertEqual(sessions.cleanup(), 1)
        self.assertEqual(UserSession.query.count(), 0)

    def test_last_seen_is_refreshed_but_not_on_every_request(self):
        a = self.sign_in(self.app.test_client())
        row = UserSession.query.one()
        old = datetime.now() - timedelta(hours=1)
        row.last_seen = old
        db.session.commit()
        self.assertTrue(self.signed_in(a))
        db.session.refresh(row)
        first = row.last_seen
        self.assertGreater(first, old)
        self.assertTrue(self.signed_in(a))
        db.session.refresh(row)
        self.assertEqual(row.last_seen, first)           # within TOUCH_AFTER: no write

    def test_inactivity_sign_out_deletes_the_session(self):
        set_setting("session_idle_hours", "0.25")
        db.session.commit()
        a = self.sign_in(self.app.test_client())
        thief = self.copy(a, "session")
        with a.session_transaction() as s:
            s["seen"] = 1                                 # inactive for a long time
        a.get("/")
        self.assertEqual(UserSession.query.count(), 0)
        self.assertFalse(self.signed_in(thief))

    def test_deactivated_or_deleted_account(self):
        a = self.sign_in(self.app.test_client())
        self.anna.active = False
        db.session.commit()
        self.assertFalse(self.signed_in(a))
        self.anna.active = True
        db.session.commit()
        self.assertTrue(self.signed_in(a))
        from assetlotse.retention import delete_user
        delete_user(db.session.get(User, self.anna.id))
        db.session.commit()
        self.assertEqual(UserSession.query.count(), 0)
        self.assertFalse(self.signed_in(a))

    def test_session_of_another_account_or_epoch_is_rejected(self):
        bob = self.make_user("bob")
        sid = sessions.issue(bob)
        token = sid.split(":")[2]
        for forged in (f"{self.anna.id}:0:{token}", f"{bob.id}:7:{token}", f"{bob.id}:0:", f"{bob.id}:0:x"):
            self.assertIsNone(sessions.load(forged), forged)
        self.assertEqual(sessions.load(sid).id, bob.id)

    def test_sessions_past_the_inactivity_limit_are_not_counted(self):
        a = self.sign_in(self.app.test_client())
        self.sign_in(self.app.test_client())              # a browser closed without signing out …
        old = UserSession.query.order_by(UserSession.id.desc()).first()
        old.last_seen = datetime.now() - timedelta(hours=13)   # … longer ago than the limit (12 hours)
        db.session.commit()
        self.assertEqual(sessions.count(self.anna), 1)
        self.assertIn("Nur auf diesem Gerät angemeldet", a.get("/profil/sicherheit").get_data(as_text=True))
        self.assertEqual(sessions.cleanup(), 1)

    def test_manual_deactivation_ends_the_sessions(self):
        a = self.sign_in(self.app.test_client())
        thief = self.copy(a, "session")
        self.make_user("chef", role="admin")
        admin = self.app.test_client()
        admin.post("/login", data={"username": "chef", "password": "geheim123"})
        form = {"username": "anna", "display_name": "Anna", "email": "", "role": "reader"}
        admin.post(f"/admin/benutzer/{self.anna.id}", data=form)          # "Account active" unticked
        db.session.refresh(self.anna)
        self.assertFalse(self.anna.active)
        admin.post(f"/admin/benutzer/{self.anna.id}", data={**form, "active": "1"})
        db.session.refresh(self.anna)
        self.assertTrue(self.anna.active)
        self.assertFalse(self.signed_in(thief))           # reactivating does not revive the old sign-in

    def test_security_page_counts_other_sessions(self):
        a = self.sign_in(self.app.test_client())
        self.sign_in(self.app.test_client())
        self.sign_in(self.app.test_client())
        html = a.get("/profil/sicherheit").get_data(as_text=True)
        self.assertIn("2 weitere Anmeldungen", html)
