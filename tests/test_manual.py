"""Das PDF-Handbuch liegt im Programmordner und wird in der Hilfe verlinkt."""
from pathlib import Path

from test_assetlotse import Base

ROOT = Path(__file__).resolve().parent.parent


class ManualTests(Base):
    def test_manual_in_docs_and_linked(self):
        pdf = ROOT / "docs" / "Handbuch-AssetLotse.pdf"
        self.assertTrue(pdf.exists())
        self.assertEqual(pdf.read_bytes()[:5], b"%PDF-")
        self.login("mia")
        html = self.c.get("/hilfe").get_data(as_text=True)
        self.assertIn("/hilfe/handbuch/Handbuch-AssetLotse.pdf", html)
        r = self.c.get("/hilfe/handbuch/Handbuch-AssetLotse.pdf")
        self.assertEqual((r.status_code, r.mimetype), (200, "application/pdf"))
        r.close()

    def test_english_ui_falls_back_to_german_manual(self):
        self.login("mia")
        self.c.post("/sprache/en")
        html = self.c.get("/hilfe").get_data(as_text=True)
        self.assertIn("/hilfe/handbuch/Handbuch-AssetLotse.pdf", html)

    def test_only_known_files(self):
        self.login("mia")
        self.assertEqual(self.c.get("/hilfe/handbuch/README.md").status_code, 404)
        self.assertEqual(self.c.get("/hilfe/handbuch/..%2Fwsgi.py").status_code, 404)

    def test_manual_requires_login(self):
        self.assertNotEqual(self.c.get("/hilfe/handbuch/Handbuch-AssetLotse.pdf").status_code, 200)
