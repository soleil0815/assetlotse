"""The GitHub page stays current: the version line of both READMEs matches the program, and the screenshots exist."""
import re
import unittest
from pathlib import Path

from assetlotse import __version__

ROOT = Path(__file__).resolve().parent.parent


class ReadmeTests(unittest.TestCase):
    def test_version_line_matches_the_program(self):
        for name in ("README.md", "README.de.md"):
            with self.subTest(readme=name):
                head = (ROOT / name).read_text(encoding="utf-8")[:400]
                found = re.search(r"\*\*Version ([0-9][^*]*)\*\*", head)
                self.assertIsNotNone(found, f"{name}: version line missing")
                self.assertEqual(found.group(1), __version__,
                                 f"{name}: update the version line (and screenshots if the interface changed: "
                                 f"python3 scripts/readme_screenshots.py)")

    def test_screenshots_referenced_exist(self):
        for name in ("README.md", "README.de.md"):
            for img in re.findall(r'src="(\.github/readme/[^"]+)"', (ROOT / name).read_text(encoding="utf-8")):
                with self.subTest(readme=name, image=img):
                    self.assertTrue((ROOT / img).is_file())
