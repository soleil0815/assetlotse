"""Update check (2.9.0): is a newer version published on GitHub? Never a real request here – urlopen is mocked."""
import io
import json
import os
import urllib.error
from datetime import datetime
from unittest import mock

from test_app import BaseCase

from assetlotse import __version__, updatecheck
from assetlotse.models import ROLE_ADMIN, ROLE_MANAGER, db, get_setting, set_setting


def bump(version: str, part: int = 1) -> str:
    v = [int(x) for x in version.split(".")]
    v[part] += 1
    return ".".join(str(x) for x in v[:part + 1] + [0] * (2 - part))


NEWER = bump(__version__)


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def github(tag=None, status=None, body=None, error=None):
    """urlopen replacement: answers like GitHub's releases/latest, records the request."""
    calls = []

    def fake(req, timeout=None):
        calls.append(req)
        if error:
            raise error
        if status:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, None)
        data = body if body is not None else json.dumps({"tag_name": tag, "draft": False, "prerelease": False,
                                                        "html_url": "https://evil.example/"}).encode()
        return Answer(data)
    return mock.patch("assetlotse.updatecheck.urllib.request.urlopen", side_effect=fake), calls


class UpdateCheckTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"ASSETLOTSE_UPDATE_CHECK": "", "ASSETLOTSE_TAG": "",
                                                 "ASSETLOTSE_SOURCE_URL": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_versions(self):
        self.assertEqual(updatecheck.parse_version("v2.10.3"), (2, 10, 3))
        self.assertIsNone(updatecheck.parse_version("2.8.1-s1"))     # security images are not announced
        self.assertIsNone(updatecheck.parse_version("3.0.0-rc1"))
        self.assertIsNone(updatecheck.parse_version(None))
        self.assertTrue(updatecheck.is_newer("2.10.0", "2.9.9"))     # numbers, not text
        self.assertFalse(updatecheck.is_newer("2.9.0", "2.9.0"))
        self.assertFalse(updatecheck.is_newer("2.8.0", "2.9.0"))
        self.assertFalse(updatecheck.is_newer("", "2.9.0"))

    def test_repository_follows_source_url_of_a_modified_version(self):
        self.assertEqual(updatecheck.repository(), "soleil0815/assetlotse")
        os.environ["ASSETLOTSE_SOURCE_URL"] = "https://github.com/someone/assetlotse-fork.git"
        self.assertEqual(updatecheck.repository(), "someone/assetlotse-fork")
        os.environ["ASSETLOTSE_SOURCE_URL"] = "https://git.example.org/x/y"      # not GitHub: ours
        self.assertEqual(updatecheck.repository(), "soleil0815/assetlotse")

    def test_check_asks_github_without_data_about_the_installation(self):
        patch, calls = github(f"v{NEWER}")
        with patch:
            self.assertEqual(updatecheck.check_now(), (True, NEWER))
        req = calls[0]
        self.assertEqual(req.full_url, "https://api.github.com/repos/soleil0815/assetlotse/releases/latest")
        self.assertIsNone(req.data)
        self.assertEqual(set(k.lower() for k in req.headers), {"accept", "user-agent", "x-github-api-version"})
        self.assertEqual(get_setting("update_latest"), NEWER)
        self.assertEqual(get_setting("update_error"), "")
        # the link is built here, never taken from the answer
        self.assertEqual(updatecheck.release_url(NEWER),
                         f"https://github.com/soleil0815/assetlotse/releases/tag/v{NEWER}")

    def test_errors_are_kept_quietly(self):
        set_setting("update_latest", NEWER)
        for kwargs, text in (({"status": 403}, "HTTP 403"), ({"body": b"<html>"}, "no JSON"),
                             ({"body": b"x" * (updatecheck.MAX_ANSWER + 10)}, "too large"),
                             ({"tag": "nightly"}, "no version"),
                             ({"error": urllib.error.URLError("timed out")}, "not reachable")):
            patch, _ = github(**kwargs)
            with self.subTest(text=text), patch:
                ok, msg = updatecheck.check_now()
                self.assertFalse(ok)
                self.assertIn(text, msg)
                self.assertIn(text, get_setting("update_error"))
                self.assertEqual(get_setting("update_latest"), NEWER)     # last good answer stays

    def test_once_a_day_and_switches(self):
        patch, calls = github(f"v{NEWER}")
        day = datetime(2026, 11, 20, 3, 0)
        with patch:
            self.assertTrue(updatecheck.run_daily(day)[0])
            self.assertIsNone(updatecheck.run_daily(day.replace(hour=9)))      # same day: nothing
            self.assertEqual(len(calls), 1)
            set_setting("update_check", "0")
            self.assertIsNone(updatecheck.run_daily(datetime(2026, 11, 21, 3)))
            set_setting("update_check", "1")
            os.environ["ASSETLOTSE_UPDATE_CHECK"] = "0"
            self.assertIsNone(updatecheck.run_daily(datetime(2026, 11, 22, 3)))
            self.assertEqual(len(calls), 1)

    def test_notice_only_for_admins_and_hideable(self):
        set_setting("update_latest", NEWER)
        db.session.commit()
        self.make_user("chef", ROLE_ADMIN)
        self.make_user("verw", ROLE_MANAGER)
        self.login("verw")
        self.assertNotIn("update-notice", self.c.get("/").get_data(as_text=True))
        self.login("chef")
        html = self.c.get("/").get_data(as_text=True)
        self.assertIn("update-notice", html)
        self.assertIn(f"AssetLotse {NEWER} ist verfügbar (installiert: {__version__})", html)
        self.assertIn(f"https://github.com/soleil0815/assetlotse/releases/tag/v{NEWER}", html)
        self.assertIn("Container-Verwaltung", html)
        os.environ["ASSETLOTSE_TAG"] = __version__                       # pinned in .env
        self.assertIn(f"ASSETLOTSE_TAG={__version__}", self.c.get("/").get_data(as_text=True))
        r = self.c.post("/admin/update/ausblenden", data={"version": NEWER, "next": "/gegenstaende"})
        self.assertEqual(r.headers["Location"], "/gegenstaende")
        self.assertNotIn("update-notice", self.c.get("/").get_data(as_text=True))
        set_setting("update_latest", bump(NEWER, 2))                      # the next version shows it again
        db.session.commit()
        self.assertIn("update-notice", self.c.get("/").get_data(as_text=True))
        r = self.c.post("/admin/update/ausblenden", data={"version": "x", "next": "https://evil.example/"})
        self.assertNotIn("evil", r.headers["Location"])
        set_setting("update_check", "0")
        db.session.commit()
        self.assertNotIn("update-notice", self.c.get("/").get_data(as_text=True))

    def test_no_notice_when_up_to_date(self):
        set_setting("update_latest", __version__)
        db.session.commit()
        self.make_user("chef", ROLE_ADMIN)
        self.login("chef")
        self.assertNotIn("update-notice", self.c.get("/").get_data(as_text=True))

    def test_diagnostics_page_and_check_now(self):
        self.make_user("chef", ROLE_ADMIN)
        self.make_user("verw", ROLE_MANAGER)
        self.login("verw")
        self.assertIn(self.c.post("/admin/update/pruefen").status_code, (302, 403))
        self.assertEqual(get_setting("update_checked_at"), "")
        self.login("chef")
        patch, calls = github(f"v{NEWER}")
        with patch:
            html = self.c.post("/admin/update/pruefen", follow_redirects=True).get_data(as_text=True)
        self.assertIn(f"Version {NEWER} ist verfügbar", html)
        self.assertIn("Jetzt prüfen", html)
        self.assertIn("Zuletzt geprüft", html)
        patch, _ = github(status=500)
        with patch:
            html = self.c.post("/admin/update/pruefen", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Letzte Prüfung fehlgeschlagen: GitHub answered HTTP 500", html)
        os.environ["ASSETLOTSE_UPDATE_CHECK"] = "0"                        # .env: never contact GitHub
        patch, calls = github(f"v{NEWER}")
        with patch:
            html = self.c.post("/admin/update/pruefen", follow_redirects=True).get_data(as_text=True)
        self.assertEqual(calls, [])
        self.assertIn("ASSETLOTSE_UPDATE_CHECK=0", html)
        self.assertNotIn("Jetzt prüfen", html)

    def test_setting_switch(self):
        self.make_user("chef", ROLE_ADMIN)
        self.login("chef")
        html = self.c.get("/admin/einstellungen").get_data(as_text=True)
        self.assertIn('name="update_check"', html)
        self.c.post("/admin/einstellungen", data={"org_name": "X", "update_check_shown": "1"})
        self.assertEqual(get_setting("update_check"), "0")
        self.c.post("/admin/einstellungen", data={"org_name": "X", "update_check_shown": "1", "update_check": "1"})
        self.assertEqual(get_setting("update_check"), "1")
        os.environ["ASSETLOTSE_UPDATE_CHECK"] = "0"
        html = self.c.get("/admin/einstellungen").get_data(as_text=True)
        self.assertNotIn('name="update_check"', html)
        self.c.post("/admin/einstellungen", data={"org_name": "X"})       # locked: the stored switch stays
        self.assertEqual(get_setting("update_check"), "1")

    def test_diagnostic_package_section(self):
        from assetlotse import diagnose
        set_setting("update_latest", NEWER)
        set_setting("update_error", "GitHub not reachable (host-a.intern.example)")
        db.session.commit()
        report, _ = diagnose.collect(self.app)
        self.assertEqual(report["update"]["latest"], NEWER)
        self.assertTrue(report["update"]["newer"])
        self.assertIn("update_available", [f["code"] for f in report["findings"]])
