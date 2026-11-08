"""Tests for automatic reverse proxy and HTTPS detection."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-proxy-"))

from flask import Flask, jsonify, make_response, request  # noqa: E402

from assetlotse.proxy import install, parse_networks  # noqa: E402


def make_app(env):
    app = Flask(__name__)

    @app.route("/")
    def index():
        resp = make_response(jsonify(ip=request.remote_addr, scheme=request.scheme, host=request.host))
        resp.set_cookie("session", "x", httponly=True)
        return resp

    install(app, env)
    return app.test_client()


FWD = {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "bib.example.de"}


class ProxyTests(unittest.TestCase):
    def test_default_trusts_private_proxy(self):
        c = make_app({})
        r = c.get("/", headers=FWD, environ_base={"REMOTE_ADDR": "172.18.0.5"})
        # X-Forwarded-Host is deliberately not applied (otherwise links in emails could be redirected);
        # reverse proxies such as NPM, Caddy and Traefik pass the Host header through unchanged anyway.
        self.assertEqual(r.json, {"ip": "203.0.113.7", "scheme": "https", "host": "localhost"})
        self.assertIn("Secure", r.headers["Set-Cookie"])

    def test_public_client_cannot_spoof(self):
        c = make_app({})
        r = c.get("/", headers=FWD, environ_base={"REMOTE_ADDR": "198.51.100.9"})
        self.assertEqual(r.json["ip"], "198.51.100.9")
        self.assertEqual(r.json["scheme"], "http")
        self.assertNotIn("Secure", r.headers["Set-Cookie"])

    def test_plain_http_in_lan_keeps_working(self):
        c = make_app({})
        r = c.get("/", environ_base={"REMOTE_ADDR": "192.168.0.50"})
        self.assertEqual(r.json["scheme"], "http")
        self.assertNotIn("Secure", r.headers["Set-Cookie"])   # login via http://IP:9300 keeps working

    def test_only_last_forwarded_hop_is_used(self):
        c = make_app({})
        r = c.get("/", headers={**FWD, "X-Forwarded-For": "1.2.3.4, 203.0.113.7"},
                  environ_base={"REMOTE_ADDR": "10.0.0.2"})
        self.assertEqual(r.json["ip"], "203.0.113.7")

    def test_disabled_and_custom(self):
        r = make_app({"ASSETLOTSE_TRUSTED_PROXIES": "none"}).get("/", headers=FWD, environ_base={"REMOTE_ADDR": "10.0.0.2"})
        self.assertEqual(r.json["ip"], "10.0.0.2")
        r = make_app({"ASSETLOTSE_BEHIND_PROXY": "0"}).get("/", headers=FWD, environ_base={"REMOTE_ADDR": "10.0.0.2"})
        self.assertEqual(r.json["ip"], "10.0.0.2")
        c = make_app({"ASSETLOTSE_TRUSTED_PROXIES": "192.168.0.11"})
        self.assertEqual(c.get("/", headers=FWD, environ_base={"REMOTE_ADDR": "192.168.0.11"}).json["ip"], "203.0.113.7")
        self.assertEqual(c.get("/", headers=FWD, environ_base={"REMOTE_ADDR": "192.168.0.12"}).json["ip"], "192.168.0.12")

    def test_secure_cookie_modes(self):
        r = make_app({"ASSETLOTSE_SECURE_COOKIES": "1"}).get("/", environ_base={"REMOTE_ADDR": "192.168.0.50"})
        self.assertIn("Secure", r.headers["Set-Cookie"])
        r = make_app({"ASSETLOTSE_SECURE_COOKIES": "0"}).get("/", headers=FWD, environ_base={"REMOTE_ADDR": "10.0.0.2"})
        self.assertNotIn("Secure", r.headers["Set-Cookie"])

    def test_parse_networks(self):
        self.assertEqual(len(parse_networks("private")), 6)
        self.assertEqual(parse_networks("none"), [])
        self.assertEqual(len(parse_networks("10.0.0.0/8, kaputt, ::1")), 2)

    def test_full_app_login_behind_proxy(self):
        from assetlotse import create_app
        from assetlotse.models import User, db
        app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        with app.app_context():
            db.drop_all(); db.create_all()
            u = User(username="admin", role="admin"); u.set_password("geheim123")
            db.session.add(u); db.session.commit()
            c = app.test_client()
            r = c.post("/login", data={"username": "admin", "password": "geheim123"}, headers=FWD,
                       environ_base={"REMOTE_ADDR": "172.18.0.5"})
            self.assertEqual(r.status_code, 302)
            self.assertTrue(all("Secure" in v for v in r.headers.getlist("Set-Cookie")))
            db.session.remove()


if __name__ == "__main__":
    unittest.main()


class ForceHttpsTests(unittest.TestCase):
    """2.4.0: HTTPS is enforced for the proxy's host name (Observatory: redirection and HSTS)."""

    PROXY = {"REMOTE_ADDR": "192.168.2.10"}

    def get(self, env=None, headers=None, path="/", **kw):
        return make_app(env or {}).get(path, headers=headers or {}, environ_base={**self.PROXY, **kw})

    def test_plain_http_through_proxy_is_redirected(self):
        r = self.get(headers={"X-Forwarded-Proto": "http", "Host": "asset.aap-dresden.de"},
                     path="/gegenstaende?q=Laptop%20Dell")
        self.assertEqual(r.status_code, 308)
        self.assertEqual(r.headers["Location"], "https://asset.aap-dresden.de/gegenstaende?q=Laptop%20Dell")
        self.assertNotIn("Set-Cookie", r.headers)                             # nothing of the app is served
        r = self.get(headers={"X-Forwarded-Proto": "http", "Host": "asset.aap-dresden.de:80"})
        self.assertEqual(r.headers["Location"], "https://asset.aap-dresden.de/")   # port 80 dropped
        r = self.get(headers={"X-Forwarded-Port": "80", "Host": "asset.aap-dresden.de"})   # only the port
        self.assertEqual((r.status_code, r.headers["Location"]), (308, "https://asset.aap-dresden.de/"))
        r = self.get(headers={"X-Forwarded-Proto": "https", "X-Forwarded-Port": "80", "Host": "asset.aap-dresden.de"})
        self.assertEqual(r.status_code, 200)                                  # the protocol header wins

    def test_https_through_proxy_gets_hsts(self):
        r = self.get(headers={"X-Forwarded-Proto": "https", "Host": "asset.aap-dresden.de"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Strict-Transport-Security"], "max-age=31536000")
        self.assertIn("Secure", r.headers["Set-Cookie"])

    def test_left_alone(self):
        cases = [
            ({}, {"Host": "192.168.2.221:9343"}),                                  # direct http://IP:9343
            ({}, {"Host": "asset.aap-dresden.de"}),                                # proxy without X-Forwarded-Proto
            ({}, {"X-Forwarded-Port": "443", "Host": "asset.aap-dresden.de"}),
            ({}, {"X-Forwarded-Proto": "http", "Host": "192.168.2.221:9343"}),     # IP address: no https there
            ({}, {"X-Forwarded-Proto": "http", "Host": "[fd00::5]:9343"}),
            ({}, {"X-Forwarded-Proto": "http", "Host": "localhost"}),
            ({}, {"X-Forwarded-Proto": "http", "Host": "evil.example/x"}),         # not a host name
            ({"ASSETLOTSE_FORCE_HTTPS": "0"}, {"X-Forwarded-Proto": "http", "Host": "asset.aap-dresden.de"}),
        ]
        for env, headers in cases:
            r = self.get(env, headers)
            self.assertEqual(r.status_code, 200, (env, headers))
            self.assertNotIn("Strict-Transport-Security", r.headers, (env, headers))

    def test_no_hsts_when_switched_off(self):
        r = self.get({"ASSETLOTSE_FORCE_HTTPS": "0"}, {"X-Forwarded-Proto": "https", "Host": "a.example.de"})
        self.assertNotIn("Strict-Transport-Security", r.headers)

    def test_public_client_cannot_trigger_a_redirect(self):
        # a forged X-Forwarded-Proto from the internet is ignored like every other forwarded header
        r = make_app({}).get("/", headers={"X-Forwarded-Proto": "http", "Host": "asset.aap-dresden.de"},
                             environ_base={"REMOTE_ADDR": "198.51.100.9"})
        self.assertEqual(r.status_code, 200)

    def test_full_app_headers(self):
        from assetlotse import create_app
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        c = app.test_client()
        r = c.get("/login", headers={"X-Forwarded-Proto": "https", "Host": "asset.aap-dresden.de"},
                  environ_base=self.PROXY)
        self.assertEqual(r.headers["Strict-Transport-Security"], "max-age=31536000")
        self.assertEqual(r.headers["Cross-Origin-Opener-Policy"], "same-origin")
        r = c.get("/login", headers={"X-Forwarded-Proto": "http", "Host": "asset.aap-dresden.de"},
                  environ_base=self.PROXY)
        self.assertEqual((r.status_code, r.headers["Location"]), (308, "https://asset.aap-dresden.de/login"))
