"""2.1.0: English is the source language – German catalogue, contexts and German texts of earlier versions."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-test-"))

from assetlotse import create_app, i18n  # noqa: E402
from assetlotse.models import db, set_setting  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class SourceLanguageTests(unittest.TestCase):
    def test_only_the_german_catalogue_is_shipped(self):
        names = sorted(p.name for p in (ROOT / "assetlotse" / "translations").glob("*.json"))
        self.assertEqual(names, ["de.json"])

    def test_english_and_german(self):
        with i18n.force_locale("en"):
            self.assertEqual(i18n._("Incorrect username or password."), "Incorrect username or password.")
        with i18n.force_locale("de"):
            self.assertEqual(i18n._("Incorrect username or password."), "Benutzername oder Passwort ist falsch.")

    def test_same_english_word_with_two_german_meanings(self):
        with i18n.force_locale("de"):
            self.assertEqual(i18n._("Location"), "Standort")
            self.assertEqual(i18n._p("storage", "Location"), "Ablage")
            self.assertEqual(i18n._("Available"), "Frei")
            self.assertEqual(i18n._p("status", "Available"), "Verfügbar")
        with i18n.force_locale("en"):
            self.assertEqual(i18n._p("storage", "Location"), "Location")

    def test_german_texts_of_earlier_versions_appear_in_english(self):
        with i18n.force_locale("en"):
            for german, english in (("vor Update", "before update"), ("direkt", "direct"), ("Stamm-CA", "Root CA"),
                                    ("Willkommen", "Welcome")):
                self.assertEqual(i18n._(german), english)
        with i18n.force_locale("de"):
            self.assertEqual(i18n._("vor Update"), "vor Update")
            self.assertEqual(i18n._("before update"), "vor Update")

    def test_catalogue_keys_are_unique_per_meaning(self):
        de = json.loads((ROOT / "assetlotse" / "translations" / "de.json").read_text(encoding="utf-8"))
        self.assertIn("Location|storage", de)
        self.assertEqual(de["Location|storage"], "Ablage")


class StoredGermanDefaultTests(unittest.TestCase):
    def test_login_page_with_the_german_default_text_of_earlier_versions(self):
        tmp = tempfile.mkdtemp(prefix="al-en-")
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp}/a.db"})
        with app.app_context():
            set_setting("setup_completed", "1")
            set_setting("login_eyebrow", "Willkommen")         # stored by a version before 2.1
            db.session.commit()
        c = app.test_client()
        en = c.get("/login", headers={"Accept-Language": "en"}).get_data(as_text=True)
        de = c.get("/login", headers={"Accept-Language": "de"}).get_data(as_text=True)
        self.assertIn("Welcome", en)
        self.assertNotIn("Willkommen", en)
        self.assertIn("Willkommen", de)


if __name__ == "__main__":
    unittest.main()
