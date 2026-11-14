"""2.6.0: sign-in with a passkey (no user name, no password; Face ID/fingerprint/PIN on the device).

A small software authenticator creates and signs passkeys like a phone does, so registration and sign-in run
through the real fido2 verification end to end.
"""
import base64
import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-test-"))

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fido2 import cbor  # noqa: E402
from fido2.cose import ES256  # noqa: E402
from fido2.webauthn import AttestedCredentialData, AuthenticatorData  # noqa: E402

from assetlotse import create_app  # noqa: E402
from assetlotse.ldap_auth import LDAPError, LDAPUser  # noqa: E402
from assetlotse.models import User, WebAuthnCredential, db, set_setting  # noqa: E402

HOST = "asset.example.org"
ORIGIN = f"https://{HOST}"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Phone:
    """Software authenticator: one passkey per account (like the iCloud keychain), counter 0 like Apple's."""
    UP, UV, AT = 0x01, 0x04, 0x40

    def __init__(self, rp_id=HOST, verify_user=True):
        self.rp_id, self.verify_user = rp_id, verify_user
        self.keys = {}   # credential id -> (private key, user handle)

    def flags(self, *extra):
        f = self.UP | (self.UV if self.verify_user else 0)
        for e in extra:
            f |= e
        return f

    def create(self, options: dict) -> dict:
        pk = options["publicKey"]
        key = ec.generate_private_key(ec.SECP256R1())
        cred_id = os.urandom(16)
        self.keys[cred_id] = (key, b64u_dec(pk["user"]["id"]))
        acd = AttestedCredentialData.create(b"\0" * 16, cred_id, ES256.from_cryptography_key(key.public_key()))
        auth = AuthenticatorData.create(hashlib.sha256(self.rp_id.encode()).digest(), self.flags(self.AT), 0, acd)
        client = json.dumps({"type": "webauthn.create", "challenge": pk["challenge"], "origin": ORIGIN}).encode()
        att = cbor.encode({"fmt": "none", "attStmt": {}, "authData": bytes(auth)})
        return {"id": b64u(cred_id), "rawId": b64u(cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(client), "attestationObject": b64u(att)}}

    def get(self, options: dict, cred_id: bytes | None = None, counter: int = 0, handle: bytes | None = None) -> dict:
        pk = options["publicKey"]
        cred_id = cred_id or next(iter(self.keys))
        key, user_handle = self.keys[cred_id]
        auth = AuthenticatorData.create(hashlib.sha256(self.rp_id.encode()).digest(), self.flags(), counter)
        client = json.dumps({"type": "webauthn.get", "challenge": pk["challenge"], "origin": ORIGIN}).encode()
        sig = key.sign(bytes(auth) + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(cred_id), "rawId": b64u(cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(client), "authenticatorData": b64u(bytes(auth)),
                             "signature": b64u(sig), "userHandle": b64u(handle or user_handle)}}


def https_client(app, base):
    """Test client whose requests all go to https://<host> (passkeys need HTTPS and are bound to the host)."""
    from flask.testing import FlaskClient

    class Client(FlaskClient):
        def open(self, *args, **kw):
            kw.setdefault("base_url", base)
            return super().open(*args, **kw)

        def session_transaction(self, *args, **kw):
            kw.setdefault("base_url", base)
            return super().session_transaction(*args, **kw)
    app.test_client_class = Client
    try:
        return app.test_client()
    finally:
        app.test_client_class = None


class PasskeyBase(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        set_setting("setup_completed", "1")
        db.session.commit()
        self.c = self.new_browser()
        self.phone = Phone()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def new_browser(self, base=ORIGIN):
        from flask import g
        g.pop("_login_user", None)
        return https_client(self.app, base)

    def make(self, name, role="reader", source="local"):
        u = User(username=name, display_name=name.title(), role=role, auth_source=source)
        if source == "local":
            u.set_password("geheim12345")
        db.session.add(u)
        db.session.commit()
        return u

    def password_login(self, name):
        r = self.c.post("/login", data={"username": name, "password": "geheim12345"})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:300])

    def register_passkey(self, name="iPhone"):
        r = self.c.post("/profil/sicherheit/schluessel/start", json={"passkey": True})
        self.assertEqual(r.status_code, 200, r.get_json())
        options = r.get_json()["options"]
        sel = options["publicKey"]["authenticatorSelection"]
        self.assertEqual((sel["residentKey"], sel["userVerification"]), ("required", "required"))
        r = self.c.post("/profil/sicherheit/schluessel/speichern",
                        json={"credential": self.phone.create(options), "name": name})
        return r

    def passkey_login(self, client=None, **get_kw):
        c = client or self.c
        r = c.post("/login/passkey/start", json={})
        self.assertEqual(r.status_code, 200, r.get_json())
        options = r.get_json()["options"]
        self.assertNotIn("allowCredentials", options["publicKey"])       # no user name: discoverable
        self.assertEqual(options["publicKey"]["userVerification"], "required")
        return c.post("/login/passkey/pruefen", json={"credential": self.phone.get(options, **get_kw),
                                                      "next": "/gegenstaende"})


class PasskeyLocalTests(PasskeyBase):
    def test_register_and_sign_in_without_password(self):
        self.make("rita")
        self.password_login("rita")
        r = self.register_passkey()
        self.assertTrue(r.get_json()["ok"], r.get_json())
        cred = WebAuthnCredential.query.one()
        self.assertTrue(cred.passkey)
        self.assertEqual((cred.name, cred.rp_id), ("iPhone", HOST))
        # new browser, no password
        self.c = self.new_browser()
        r = self.passkey_login()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["redirect"], "/gegenstaende")
        self.assertEqual(self.c.get("/").status_code, 200)
        with self.c.session_transaction() as s:
            self.assertTrue(s.get("mfa_ok"))                           # counts as the second factor
        db.session.expire_all()
        self.assertIsNotNone(WebAuthnCredential.query.one().last_used_at)

    def test_no_second_factor_prompt_even_if_mandatory(self):
        set_setting("mfa_policy", "all")
        db.session.commit()
        self.make("rita")
        self.password_login("rita")              # no 2FA yet: sent to the setup page
        self.assertTrue(self.register_passkey().get_json()["ok"])
        self.c = self.new_browser()
        r = self.passkey_login()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get("/").status_code, 200)            # no challenge, no setup redirect

    def test_registration_needs_user_verification(self):
        self.make("rita")
        self.password_login("rita")
        self.phone.verify_user = False             # a plain key without PIN/biometrics
        r = self.register_passkey()
        self.assertEqual(r.status_code, 400)
        self.assertEqual(WebAuthnCredential.query.count(), 0)

    def test_sign_in_needs_user_verification(self):
        self.make("rita")
        self.password_login("rita")
        self.register_passkey()
        self.c = self.new_browser()
        self.phone.verify_user = False
        self.assertEqual(self.passkey_login().status_code, 400)
        self.assertEqual(self.c.get("/").status_code, 302)

    def test_plain_security_key_cannot_sign_in_alone(self):
        self.make("rita")
        self.password_login("rita")
        r = self.c.post("/profil/sicherheit/schluessel/start", json={})
        opts = r.get_json()["options"]
        self.assertEqual(opts["publicKey"]["authenticatorSelection"]["residentKey"], "discouraged")
        self.c.post("/profil/sicherheit/schluessel/speichern", json={"credential": self.phone.create(opts)})
        self.assertFalse(WebAuthnCredential.query.one().passkey)
        self.c = self.new_browser()
        self.assertEqual(self.passkey_login().status_code, 400)

    def test_wrong_user_handle_and_cloned_counter(self):
        self.make("rita")
        other = self.make("otto")
        self.password_login("rita")
        self.register_passkey()
        self.c = self.new_browser()
        r = self.passkey_login(handle=f"user-{other.id}".encode())
        self.assertEqual(r.status_code, 400)
        WebAuthnCredential.query.one().sign_count = 10
        db.session.commit()
        self.assertEqual(self.passkey_login(counter=5).status_code, 400)
        self.assertEqual(self.passkey_login(counter=11).status_code, 200)

    def test_other_address_is_refused(self):
        self.make("rita")
        self.password_login("rita")
        self.register_passkey()
        other = self.new_browser("https://other.example.org")
        r = other.post("/login/passkey/start", json={})
        self.phone.rp_id = "other.example.org"
        r = other.post("/login/passkey/pruefen", json={"credential": self.phone.get(r.get_json()["options"])})
        self.assertEqual(r.status_code, 400)

    def test_disabled_account_and_switched_off(self):
        u = self.make("rita")
        self.password_login("rita")
        self.register_passkey()
        u.active = False
        db.session.commit()
        self.c = self.new_browser()
        self.assertEqual(self.passkey_login().status_code, 403)
        u.active = True
        set_setting("passkey_login", "0")
        db.session.commit()
        self.assertEqual(self.c.post("/login/passkey/start", json={}).status_code, 404)
        self.assertNotIn("data-passkey-login", self.c.get("/login").get_data(as_text=True))

    def test_expired_and_replayed_state(self):
        self.make("rita")
        self.password_login("rita")
        self.register_passkey()
        self.c = self.new_browser()
        r = self.c.post("/login/passkey/start", json={})
        assertion = self.phone.get(r.get_json()["options"])
        with self.c.session_transaction() as s:
            s["passkey_state"] = {**s["passkey_state"], "ts": s["passkey_state"]["ts"] - 3600}
        self.assertEqual(self.c.post("/login/passkey/pruefen", json={"credential": assertion}).status_code, 400)
        # the state is used up – sending the same answer again does not help
        self.assertEqual(self.c.post("/login/passkey/pruefen", json={"credential": assertion}).status_code, 400)

    def test_failures_are_throttled(self):
        from assetlotse import lockout
        self.make("rita")
        self.password_login("rita")
        self.register_passkey()
        self.c = self.new_browser()
        self.phone.verify_user = False
        for _ in range(lockout.MAX_FAILS):
            self.passkey_login()
        self.assertEqual(self.c.post("/login/passkey/start", json={}).status_code, 429)

    def test_login_page_and_profile(self):
        html = self.c.get("/login").get_data(as_text=True)
        self.assertIn('autocomplete="username webauthn"', html)
        self.assertIn("data-passkey-login", html)
        self.make("rita")
        self.password_login("rita")
        page = self.c.get("/profil/sicherheit").get_data(as_text=True)
        self.assertIn('id="passkey"', page)
        self.assertIn("data-passkey", page)

    def test_help_describes_passkeys(self):
        self.make("rita")
        self.password_login("rita")
        help_page = self.c.get("/hilfe?thema=security").get_data(as_text=True)
        self.assertIn("Passkey anlegen", help_page)
        self.assertIn("Mit Passkey anmelden", self.c.get("/hilfe?thema=login").get_data(as_text=True))
        set_setting("passkey_login", "0")
        db.session.commit()
        self.assertNotIn("Passkey anlegen", self.c.get("/hilfe?thema=security").get_data(as_text=True))


class PasskeyActiveDirectoryTests(PasskeyBase):
    def setUp(self):
        super().setUp()
        for k, v in {"ldap_enabled": "1", "ldap_server": "dc1.example.org", "ldap_bind_dn": "svc-asset"}.items():
            set_setting(k, v)
        db.session.commit()
        self.ad = self.make("m.becker", source="ldap")
        self.ad.ldap_guid = "guid-1"
        db.session.commit()

    def directory(self, result=None, error=None):
        """Replace the AD connection: check_account() returns `result` (or raises `error`)."""
        d = mock.MagicMock()
        if error is not None:
            d.return_value.check_account.side_effect = error
        else:
            d.return_value.check_account.return_value = result
        return mock.patch("assetlotse.ldap_auth.Directory", d)

    def ad_info(self, role="manager"):
        return LDAPUser(username="m.becker", dn="CN=Michael Becker,OU=Staff,DC=example,DC=org",
                        display_name="Michael Becker", email="m.becker@example.org", role=role, guid="guid-1")

    def enroll(self):
        # the AD sign-in itself is tested elsewhere: start from a signed-in session
        with self.c.session_transaction() as s:
            s["_user_id"] = str(self.ad.id)
            s["_fresh"] = True
        r = self.register_passkey()
        self.assertTrue(r.get_json()["ok"], r.get_json())
        self.c = self.new_browser()

    def test_checked_in_ad_at_every_sign_in(self):
        self.enroll()
        with self.directory(self.ad_info("admin")) as d:
            r = self.passkey_login()
        self.assertEqual(r.status_code, 200, r.get_json())
        d.return_value.check_account.assert_called_once_with("m.becker")
        db.session.expire_all()
        u = db.session.get(User, self.ad.id)
        self.assertEqual((u.role, u.display_name), ("admin", "Michael Becker"))     # taken over from AD

    def test_disabled_in_ad_or_no_rule(self):
        self.enroll()
        for info in (None, self.ad_info(role=None)):
            with self.directory(info):
                r = self.passkey_login()
            self.assertEqual(r.status_code, 403, info)
            self.assertEqual(self.c.get("/").status_code, 302)

    def test_ad_unreachable(self):
        self.enroll()
        with self.directory(error=LDAPError("timeout")):
            r = self.passkey_login()
        self.assertEqual(r.status_code, 403)
        self.assertIn("Active Directory", r.get_json()["error"])

    def test_not_offered_without_service_account(self):
        set_setting("ldap_bind_dn", "")
        db.session.commit()
        with self.c.session_transaction() as s:
            s["_user_id"] = str(self.ad.id)
            s["_fresh"] = True
        r = self.c.post("/profil/sicherheit/schluessel/start", json={"passkey": True})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("data-passkey ", self.c.get("/profil/sicherheit").get_data(as_text=True))
        help_page = self.c.get("/hilfe?thema=security").get_data(as_text=True)
        self.assertIn("erst zur Verfügung, wenn ein Administrator das Active-Directory-Dienstkonto", help_page)


if __name__ == "__main__":
    unittest.main()
