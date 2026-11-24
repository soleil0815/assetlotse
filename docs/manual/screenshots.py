"""Screenshots for the PDF manuals (docs/manual/img/de|en/*.jpg – not in git, made on demand).

Starts AssetLotse with neutral example data in a temporary folder (the same data as the README screenshots) and
photographs every page the manual shows – signed in as administrator and as a staff member, in German and English.

    python3 docs/manual/screenshots.py          # needs Playwright with Chromium and poppler (pdftoppm)
    python3 docs/manual/build.py de && python3 docs/manual/build.py en
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from readme_screenshots import ORG, PASSWORD, seed  # noqa: E402

PORT = 5193
STAFF = "mmeyer"
DRIVE = {"de": ("Externe Festplatte 4 TB", "Festplatte"), "en": ("External drive 4 TB", "Drive")}
FOOTER = {"de": "IT-Service · 0123 456789", "en": "IT service · 0123 456789"}


def extra_data(app, lang: str) -> int:
    """What the README data lacks: a BitLocker drive shared with its holder, settings for email/AD/labels, backups."""
    from assetlotse import backup
    from assetlotse.models import Asset, Group, Loan, User, db, set_setting
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        staff = User.query.filter_by(username=STAFF).one()
        staff.set_password(PASSWORD)
        it = Group.query.filter_by(name="IT").one()
        admin.groups.append(it)
        name, cat = DRIVE[lang]
        drive = Asset(name=name, category=cat, manufacturer="WD", model="Elements", inventory_no="PC-0100",
                      serial_number="WX12A3456789", group_id=it.id, secret_holder_view=True,
                      location=Asset.query.first().location)
        drive.set_secret("372361-584958-115654-430056-102157-679283-219659-647449")
        db.session.add(drive)
        db.session.flush()
        db.session.add(Loan(asset_id=drive.id, user_id=staff.id, issued_by_id=admin.id,
                            loaned_at=datetime.now() - timedelta(days=12), due_at=None))
        for key, value in {
            "label_footer": FOOTER[lang], "update_latest": "99.0.0", "update_checked_at":
                datetime.now().replace(microsecond=0).isoformat(),
            "mail_enabled": "1", "smtp_host": "mail.institut.local" if lang == "de" else "mail.example.local",
            "smtp_port": "587", "smtp_security": "starttls", "smtp_from": "assets@institut.local" if lang == "de"
            else "assets@example.local", "app_base_url": "https://assets.institut.local" if lang == "de"
            else "https://assets.example.local",
            "ldap_enabled": "0", "ldap_server": "ldaps://dc1.institut.local" if lang == "de" else "ldaps://dc1.example.local",
            "ldap_base_dn": "DC=institut,DC=local" if lang == "de" else "DC=example,DC=local",
            "ldap_bind_dn": "CN=svc-assetlotse,OU=Dienstkonten,DC=institut,DC=local" if lang == "de"
            else "CN=svc-assetlotse,OU=Service accounts,DC=example,DC=local",
            "ldap_admin_rules": "IT-Admins", "ldap_manager_rules": "AssetLotse-Verwalter" if lang == "de"
            else "AssetLotse-Managers", "ldap_login_rules": "Domänen-Benutzer" if lang == "de" else "Domain Users",
        }.items():
            set_setting(key, value)
        db.session.commit()
        for days in (3, 2, 1):
            backup.create_backup(now=datetime.now() - timedelta(days=days))
        backup.create_backup("manuell")
        return drive.id


def main() -> int:
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server

    from assetlotse import create_app
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for n, lang in enumerate(("de", "en")):
            out = HERE / "img" / lang
            out.mkdir(parents=True, exist_ok=True)
            data = tempfile.mkdtemp(prefix=f"assetlotse-manual-{lang}-")
            os.environ["ASSETLOTSE_DATA_DIR"] = data
            app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{data}/assetlotse.db"})
            seed(app, lang)
            drive_id = extra_data(app, lang)
            server = make_server("localhost", PORT + n, app, threaded=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://localhost:{PORT + n}"
            locale = "de-DE" if lang == "de" else "en-GB"

            def context(width=1280, height=800):
                return browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1.6,
                                           locale=locale, color_scheme="light")

            def sign_in(page, user):
                page.goto(base + "/login")
                page.fill("#username", user)
                page.fill("#password", PASSWORD)
                page.click("form button.primary")
                page.wait_for_load_state("networkidle")

            def shot(page, name, path=None, element=None, clip=None, full=False):
                if path:
                    page.goto(base + path)
                    page.wait_for_load_state("networkidle")
                target = str(out / f"{name}.jpg")
                if element:
                    page.locator(element).first.screenshot(path=target, type="jpeg", quality=78)
                else:
                    page.screenshot(path=target, type="jpeg", quality=78, clip=clip, full_page=full)
                print("written", Path(target).relative_to(ROOT))

            # signed out
            ctx = context(1100, 720)
            page = ctx.new_page()
            shot(page, "login", "/login")
            ctx.close()

            # staff member
            ctx = context()
            page = ctx.new_page()
            sign_in(page, STAFF)
            shot(page, "staff_dashboard", "/")
            page.goto(base + f"/gegenstaende/{drive_id}")
            page.fill("form[action$='/mein-schluessel'] input[name=password]", PASSWORD)
            page.click("form[action$='/mein-schluessel'] button")
            page.wait_for_load_state("networkidle")
            shot(page, "staff_detail_secret", element="section:has([data-secret])")
            ctx.close()

            # administrator
            ctx = context()
            page = ctx.new_page()
            sign_in(page, "admin")
            page.goto(base + "/admin/diagnose")
            shot(page, "update_notice", clip={"x": 0, "y": 0, "width": 1280, "height": 430})
            shot(page, "diagnose", "/admin/diagnose", full=True)
            # the notice is only for the update chapter – hide it for the other pictures
            page.locator(".update-notice form button").click()
            page.wait_for_load_state("networkidle")
            shot(page, "dashboard", "/")
            shot(page, "security", "/profil/sicherheit")
            shot(page, "catalog", "/gegenstaende")
            page.fill("form.search input[type=search]", "Laptop")
            page.wait_for_timeout(900)
            shot(page, "search_suggest", clip={"x": 0, "y": 0, "width": 760, "height": 520})
            page.goto(base + f"/gegenstaende/{drive_id}")
            page.click("form[action$='/vertraulich'] button")
            page.wait_for_load_state("networkidle")
            shot(page, "detail_manager", full=True)
            page.goto(base + "/gegenstaende/neu")
            page.fill("input[name=name]", "Laptop 14\"")
            page.fill("input[name=category]", "Laptop")
            inv = page.locator("input[name=inventory_no]")
            inv.fill("PC-00")
            page.wait_for_timeout(900)
            shot(page, "form_new")
            shot(page, "form_secret", element="section:has([name=secret])")
            page.goto(base + "/theke")
            page.fill("#f-benutzer", "Ma")
            page.wait_for_timeout(900)
            shot(page, "desk_people", clip={"x": 0, "y": 0, "width": 1280, "height": 560})
            page.goto(base + "/theke?modus=ausgabe&benutzer=tschulz")
            page.fill("#desk-code", "Laptop")
            page.wait_for_timeout(900)
            shot(page, "desk_items")
            shot(page, "desk_return", "/theke?modus=rueckgabe", clip={"x": 0, "y": 0, "width": 1280, "height": 560})
            page.goto(base + "/gegenstaende?kategorie=Laptop")
            page.click("[data-dialog-open=label-dlg]")
            page.fill("#label-dlg-count", "2")
            page.wait_for_timeout(200)
            shot(page, "label_dialog", element="#label-dlg")
            pdf = page.request.get(base + f"/gegenstaende/{drive_id}/etikett.pdf").body()
            (Path(data) / "label.pdf").write_bytes(pdf)
            subprocess.run(["pdftoppm", "-jpeg", "-r", "400", "-singlefile", str(Path(data) / "label.pdf"),
                            str(out / "label")], check=True)
            print("written", (out / "label.jpg").relative_to(ROOT))
            shot(page, "all_loans", "/ausleihen")
            shot(page, "locations", "/standorte")
            shot(page, "users", "/admin/benutzer")
            shot(page, "settings", "/admin/einstellungen")
            shot(page, "ldap_top", "/admin/verzeichnisdienst")
            shot(page, "ldap_sync", element="#ad-cache")
            shot(page, "email", "/admin/email")
            shot(page, "branding", "/admin/erscheinungsbild")
            shot(page, "backups", "/admin/datensicherung")
            ctx.close()
            server.shutdown()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
