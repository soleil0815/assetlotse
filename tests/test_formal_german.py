"""2.3.0: the German interface addresses people formally ("Sie"), never with "du"/"ihr".

Checks every German text people see in the program: the translations (interface and e-mails), the German
help pages, the German news entries and the German README.
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# pronouns of the informal address; "ihr" alone is left out (also means "her"/"their")
INFORMAL = re.compile(r"\b(du|dich|dir|dein|deine|deinen|deinem|deiner|deines|euch|euer|eure|euren|eurem|eurer|eures)\b",
                      re.IGNORECASE)
# second-person-plural verb next to "ihr" ("braucht ihr", "ihr könnt")
INFORMAL_PLURAL = re.compile(r"\b([a-zäöüß]+t ihr|ihr ([a-zäöüß]+t|habt|seid|könnt|müsst|dürft|wollt|sollt))\b")


def offending(text):
    plain = re.sub(r"<[^>]+>|\{%.*?%\}|\{\{.*?\}\}", " ", text)          # markup and Jinja are not German text
    return INFORMAL.findall(plain) + [m[0] for m in INFORMAL_PLURAL.findall(plain)]


class FormalGermanTests(unittest.TestCase):
    def test_translations(self):
        de = json.loads((ROOT / "assetlotse" / "translations" / "de.json").read_text())
        bad = {k: v for k, v in de.items() if offending(v)}
        self.assertEqual(bad, {})

    def test_help_pages(self):
        for page in sorted((ROOT / "assetlotse" / "templates" / "help" / "de").glob("*.html")):
            self.assertEqual(offending(page.read_text()), [], page.name)

    def test_news(self):
        entries = json.loads((ROOT / "assetlotse" / "news.json").read_text())["entries"]
        for e in entries:
            self.assertEqual(offending(e["de"]["title"] + " " + e["de"]["text"]), [], e["version"])

    def test_readme(self):
        text = (ROOT / "README.md").read_text()
        text = re.sub(r"```.*?```|`[^`]*`", " ", text, flags=re.S)        # code is not German text
        self.assertEqual(offending(text), [])

    def test_mail_greeting_is_formal(self):
        de = json.loads((ROOT / "assetlotse" / "translations" / "de.json").read_text())
        self.assertEqual(de["Hallo {name},"], "Guten Tag {name},")
        self.assertEqual(de["Hallo,"], "Guten Tag,")

    def test_detector_finds_informal_address(self):
        self.assertTrue(offending("Bitte melde dich an."))
        self.assertTrue(offending("Hier legt ihr Räume an, dann braucht ihr nichts weiter."))
        self.assertFalse(offending("Bitte melden Sie sich an. Vor dem Anzeigen muss sie ihr Passwort eingeben."))


if __name__ == "__main__":
    unittest.main()
