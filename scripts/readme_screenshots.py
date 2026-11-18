"""Screenshots for the README (.github/readme/*-de.png / *-en.png).

Starts AssetLotse with neutral example data in a temporary folder, signs in as administrator and photographs the
dashboard, the inventory and the issue desk in German and English (1360 x 820, scale 2).

    python3 scripts/readme_screenshots.py            # needs Playwright with Chromium

Run it whenever the visible interface changes, so the GitHub page shows the current program.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".github" / "readme"
PORT = 5191
PASSWORD = "Beispiel-Kennwort-123"


LOC = {"de": ("Raum 1.04 – Schrank A", "Raum 0.12 – Ausgabe", "Besprechungsraum 2"),
       "en": ("Room 1.04 – Cabinet A", "Room 0.12 – Service desk", "Meeting room 2")}
ITEMS = {
    "de": [("Laptop 14\"", "Laptop"), ("Laptop 15\"", "Laptop"), ("Headset", "Headset"), ("USB-Stick 64 GB", "USB-Stick"),
           ("Externe Festplatte 2 TB", "Festplatte"), ("Beamer", "Beamer"), ("Diensthandy", "Handy"),
           ("Dokumentenkamera", "Kamera")],
    "en": [("Laptop 14\"", "Laptop"), ("Laptop 15\"", "Laptop"), ("Headset", "Headset"), ("USB stick 64 GB", "USB stick"),
           ("External drive 2 TB", "Drive"), ("Projector", "Projector"), ("Work phone", "Phone"),
           ("Document camera", "Camera")],
}
ORG = {"de": "Institut Beispiel", "en": "Example Institute"}


def seed(app, lang: str) -> None:
    from assetlotse.models import (ROLE_ADMIN, ROLE_MANAGER, ROLE_READER, Asset, Group, Loan, Location, User, db,
                                   set_setting)
    with app.app_context():
        db.create_all()
        set_setting("setup_completed", "1")
        set_setting("org_name", ORG[lang])
        set_setting("default_language", lang)
        admin = User(username="admin", display_name="Andrea Admin", role=ROLE_ADMIN, auth_source="local",
                     email="admin@firma.local", email_verified=True)
        admin.set_password(PASSWORD)
        people = [User(username=u, display_name=n, role=r, auth_source="local", email=f"{u}@firma.local",
                       email_verified=True)
                  for u, n, r in (("mmeyer", "Maria Meyer", ROLE_READER), ("tschulz", "Tom Schulz", ROLE_READER),
                                  ("lwagner", "Lena Wagner", ROLE_READER), ("kbecker", "Karl Becker", ROLE_MANAGER),
                                  ("sfischer", "Sara Fischer", ROLE_READER))]
        db.session.add_all([admin, *people])
        it = Group(name="IT", color="#3b82f6")
        l1, l2, l3 = LOC[lang]
        db.session.add_all([it, Location(name=l1), Location(name=l2), Location(name=l3)])
        db.session.flush()
        spec = [("Lenovo", "ThinkPad T14", 6, l1), ("Dell", "Latitude 5550", 4, l1), ("Jabra", "Evolve2 55", 8, l2),
                ("SanDisk", "Ultra Flair", 10, l2), ("WD", "My Passport", 3, l1), ("Epson", "EB-W51", 2, l3),
                ("Samsung", "Galaxy A55", 5, l1), ("IPEVO", "V4K", 1, l3)]
        rows = [(name, cat, *rest) for (name, cat), rest in zip(ITEMS[lang], spec)]
        assets = []
        n = 1
        for name, cat, man, model, count, loc in rows:
            for i in range(count):
                a = Asset(name=name, category=cat, manufacturer=man, model=model, location=loc,
                          inventory_no=f"PC-{n:04d}", serial_number=f"SN{100000 + n}",
                          group_id=it.id if man in ("Lenovo", "Dell", "Samsung") else None)
                assets.append(a)
                n += 1
        db.session.add_all(assets)
        db.session.flush()
        today = date.today()
        plan = [(0, 0, None), (1, 1, None), (6, 2, today + timedelta(days=10)), (11, 3, today - timedelta(days=3)),
                (12, 4, today + timedelta(days=1)), (19, 0, today + timedelta(days=14)), (31, 1, None),
                (34, 2, today - timedelta(days=1)), (36, 3, None), (10, 4, today + timedelta(days=30))]
        for idx, person, due in plan:
            db.session.add(Loan(asset_id=assets[idx].id, user_id=people[person].id, issued_by_id=admin.id,
                                loaned_at=datetime.now() - timedelta(days=5 + idx), due_at=due))
        db.session.commit()


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server

    from assetlotse import create_app
    shots = (("dashboard", "/"), ("inventory", "/gegenstaende"), ("desk", "/theke"))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for n, (lang, locale) in enumerate((("de", "de-DE"), ("en", "en-GB"))):
            data = tempfile.mkdtemp(prefix=f"assetlotse-readme-{lang}-")
            os.environ["ASSETLOTSE_DATA_DIR"] = data
            app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{data}/assetlotse.db"})
            seed(app, lang)
            server = make_server("localhost", PORT + n, app)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://localhost:{PORT + n}"
            ctx = browser.new_context(viewport={"width": 1360, "height": 820}, device_scale_factor=2, locale=locale,
                                      color_scheme="light")
            page = ctx.new_page()
            page.goto(base + "/login")
            page.fill("#username", "admin")
            page.fill("#password", PASSWORD)
            page.click("form button.primary")
            page.wait_for_load_state("networkidle")
            for name, path in shots:
                page.goto(base + path)
                page.wait_for_load_state("networkidle")
                target = OUT / f"{name}-{lang}.png"
                page.screenshot(path=str(target))
                print("written", target.relative_to(ROOT))
            ctx.close()
            server.shutdown()
        browser.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
