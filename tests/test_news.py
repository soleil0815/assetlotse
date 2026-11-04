"""In-app news: visibility per role, unread dot, remembering what was read, languages."""
import json
from pathlib import Path
from unittest import mock

from test_assetlotse import Base

import assetlotse
from assetlotse import news
from assetlotse.models import User, db

ROOT = Path(__file__).resolve().parent.parent

SAMPLE = {"entries": [
    {"version": "9.0.0", "date": "2099-01-01", "kind": "feature", "audience": "all",
     "de": {"title": "Zukunft", "text": "nie zeigen"}, "en": {"title": "Future", "text": "never"}},
    {"version": "2.0.0", "date": "2026-10-24", "kind": "feature", "audience": "all",
     "de": {"title": "Für alle", "text": "Text A"}, "en": {"title": "For everyone", "text": "Text A en"}},
    {"version": "2.0.0", "date": "2026-10-24", "kind": "security", "audience": "manager",
     "de": {"title": "Für Verwalter", "text": "Text B"}, "en": {"title": "For managers", "text": "Text B en"}},
    {"version": "2.0.0", "date": "2026-10-24", "kind": "fix", "audience": "admin",
     "de": {"title": "Für Admins", "text": "Text C"}},
    {"version": "1.0.0", "date": "2026-10-01", "kind": "feature", "audience": "all",
     "de": {"title": "Alt", "text": "alter Eintrag"}, "en": {"title": "Old", "text": "old entry"}},
]}


class NewsFileTest(Base):
    def test_shipped_news_file_is_valid(self):
        data = json.loads((ROOT / "assetlotse" / "news.json").read_text(encoding="utf-8"))
        current = news.version_tuple(assetlotse.__version__)
        versions = []
        for e in data["entries"]:
            self.assertIn(e["kind"], news.KINDS)
            self.assertIn(e["audience"], ("all", "manager", "admin"))
            for lang in ("de", "en"):
                self.assertTrue(e[lang]["title"].strip() and e[lang]["text"].strip(), (e["version"], lang))
            self.assertLessEqual(news.version_tuple(e["version"]), current)
            versions.append(news.version_tuple(e["version"]))
        self.assertEqual(versions, sorted(versions, reverse=True))      # newest first
        self.assertEqual(max(versions), current, "news.json is missing an entry for the current version")


class NewsTest(Base):
    def setUp(self):
        super().setUp()
        news._entries.cache_clear()
        p1 = mock.patch.object(news, "__version__", "2.0.0")
        p2 = mock.patch.object(news, "_load_raw", return_value=SAMPLE)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)
        self.addCleanup(news._entries.cache_clear)

    def titles(self, html):
        return [t for t in ("Zukunft", "Für alle", "Für Verwalter", "Für Admins", "Alt") if t in html]

    def test_visibility_by_role_and_no_future_entries(self):
        for name, expected in (("mia", ["Für alle", "Alt"]),
                               ("vera", ["Für alle", "Für Verwalter", "Alt"]),
                               ("admin", ["Für alle", "Für Verwalter", "Für Admins", "Alt"])):
            with self.subTest(user=name):
                self.login(name)
                html = self.c.get("/neuigkeiten").get_data(as_text=True)
                self.assertEqual(self.titles(html), expected)
                self.c.post("/logout")

    def test_dot_until_read_then_gone(self):
        self.login("mia")
        html = self.c.get("/").get_data(as_text=True)
        self.assertIn("news-dot", html)                      # news of the running version; older ones count as read
        self.assertIn('href="/neuigkeiten"', html)
        page = self.c.get("/neuigkeiten").get_data(as_text=True)
        self.assertIn("is-new", page)
        self.assertEqual(page.count("is-new"), 1)            # only the 2.0.0 entry, not the old one
        self.assertEqual(User.query.filter_by(username="mia").first().news_seen, "2.0.0")
        html = self.c.get("/").get_data(as_text=True)
        self.assertNotIn("news-dot", html)
        self.assertNotIn("is-new", self.c.get("/neuigkeiten").get_data(as_text=True))

    def test_after_update_only_newer_entries_are_new(self):
        u = User.query.filter_by(username="admin").first()
        u.news_seen = "1.0.0"
        db.session.commit()
        self.login("admin")
        page = self.c.get("/neuigkeiten").get_data(as_text=True)
        self.assertEqual(page.count("is-new"), 3)

    def test_english_with_fallback(self):
        self.login("admin")
        self.c.post("/sprache/en")
        page = self.c.get("/neuigkeiten").get_data(as_text=True)
        self.assertIn("For everyone", page)
        self.assertIn("Für Admins", page)                    # no English text: German one
