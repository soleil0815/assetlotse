""""Buy me a coffee" link: visible to administrators only, opens the external page safely."""
import re

from test_app import BaseCase

from assetlotse import SUPPORT_URL


class SupportLinkTests(BaseCase):
    def page(self, name, role):
        self.make_user(name, role=role)
        self.login(name)
        return self.c.get("/", follow_redirects=True).get_data(as_text=True)

    def test_admins_see_it(self):
        html = self.page("chefin", "admin")
        link = re.search(r'<a class="side-coffee"[^>]*>', html)
        self.assertIsNotNone(link)
        self.assertIn(f'href="{SUPPORT_URL}"', link.group(0))
        self.assertIn('target="_blank"', link.group(0))
        self.assertIn('rel="noopener noreferrer"', link.group(0))     # the page cannot reach back into AssetLotse
        self.assertIn("Buy me a coffee", html)

    def test_nobody_else_sees_it(self):
        for name, role in (("vera", "manager"), ("mia", "reader")):
            with self.subTest(role=role):
                html = self.page(name, role)
                self.assertNotIn("side-coffee", html)
                self.assertNotIn(SUPPORT_URL, html)
                self.c.post("/logout")

    def test_not_on_the_sign_in_page(self):
        html = self.c.get("/login").get_data(as_text=True)
        self.assertNotIn(SUPPORT_URL, html)

    def test_address(self):
        self.assertEqual(SUPPORT_URL, "https://buymeacoffee.com/soleil0815")


class SourceLinkTests(BaseCase):
    """AGPL section 13: the About page always offers the source code."""

    def test_default_is_the_public_repository(self):
        import os
        from unittest import mock
        self.make_user("chefin", role="admin")
        self.login("chefin")
        for value in (None, "", "  "):
            with self.subTest(value=value):
                env = {k: v for k, v in os.environ.items() if k != "ASSETLOTSE_SOURCE_URL"}
                if value is not None:
                    env["ASSETLOTSE_SOURCE_URL"] = value
                with mock.patch.dict(os.environ, env, clear=True):
                    html = self.about()
                self.assertIn('href="https://github.com/soleil0815/assetlotse"', html)
        with mock.patch.dict(os.environ, {"ASSETLOTSE_SOURCE_URL": "https://git.example.org/fork"}):
            self.assertIn('href="https://git.example.org/fork"', self.about())

    def about(self):
        from flask import url_for
        with self.app.test_request_context():
            path = url_for("main.about")
        return self.c.get(path).get_data(as_text=True)


class PublicRepoWorkflowTests(BaseCase):
    def test_checks_never_sign_in_to_the_registry(self):
        from pathlib import Path
        wf = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "docker.yml").read_text()
        prep = wf[wf.index("\n  prepare:\n"):wf.index("\n  build:\n")]
        login = prep.index("docker/login-action@")
        self.assertIn("if: needs.mode.outputs.kind != 'check'", prep[login - 120:login])
        reg = prep.index("- name: Registry state")
        self.assertIn("if: needs.mode.outputs.kind != 'check'", prep[reg:reg + 120])
        self.assertNotIn("pull_request_target", wf)                   # fork code never runs with write access
        self.assertTrue((Path(__file__).resolve().parent.parent / "SECURITY.md").exists())
