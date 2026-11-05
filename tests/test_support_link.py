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
