"""Fixes from the review: stock, issue, desk, locations."""
import io
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-test-"))

from PIL import Image  # noqa: E402
from sqlalchemy import event, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from assetlotse import create_app  # noqa: E402
from assetlotse.models import Asset, Group, Loan, Location, User, db, ensure_indexes, set_setting  # noqa: E402
from assetlotse.views import assets as assets_view  # noqa: E402
from assetlotse.views import loans as loans_view  # noqa: E402

PW = "geheim12345"
NO_PROXY = {"NO_PROXY": "*", "no_proxy": "*"}


def _image(fmt="PNG", size=(300, 300)):
    buf = io.BytesIO()
    Image.effect_noise(size, 80).convert("RGB").save(buf, fmt)
    return buf.getvalue()


PNG = _image()


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def serve(handler):
    srv = _Server(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Base(unittest.TestCase):
    db_uri = "sqlite://"

    def setUp(self):
        self.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": self.db_uri,
                               "PROPAGATE_EXCEPTIONS": False})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        from flask import g

        def _fresh():
            db.session.rollback()
            g.pop("_login_user", None)
            g.pop("_assetlotse_locale", None)
        self.app.before_request_funcs.setdefault(None, []).insert(0, _fresh)
        self.ga, self.gb = Group(name="A"), Group(name="B")
        db.session.add_all([self.ga, self.gb])
        db.session.commit()
        self.admin = self.make("admin", "admin")
        self.biba = self.make("biba", "manager", [self.ga])
        self.leser = self.make("leser", "reader", [self.ga])
        self.lb = self.make("lb", "reader", [self.gb])
        self.c = self.app.test_client()
        set_setting("self_checkout", "1")   # tests also check self-checkout
        set_setting("loan_days", "28")
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def make(self, name, role, groups=()):
        u = User(username=name, display_name=name.title(), role=role)
        u.set_password(PW)
        u.groups = list(groups)
        db.session.add(u)
        db.session.commit()
        return u

    def login(self, name):
        db.session.rollback()
        self.c = self.app.test_client()
        r = self.c.post("/login", data={"username": name, "password": PW})
        self.assertEqual(r.status_code, 302)
        return self.c

    def client_for(self, name):
        c = self.app.test_client()
        c.post("/login", data={"username": name, "password": PW})
        return c

    def asset(self, **kw):
        b = Asset(name=kw.pop("title", "T"), **kw)
        db.session.add(b)
        db.session.commit()
        return b

    def loan(self, asset, user, returned=False):
        l = Loan(asset_id=asset.id, user_id=user.id, due_at=date.today() + timedelta(days=14),
                 returned_at=datetime.now() if returned else None)
        db.session.add(l)
        db.session.commit()
        return l


# ---------------------------------------------------------------- 1. concurrent loans (file SQLite)

class RaceTests(Base):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cx-race-")
        self.db_uri = f"sqlite:///{self.dir}/race.db"
        super().setUp()

    def run_parallel(self, jobs, patch_target, patch_attr, key=None):
        """Let all requests meet at the same point (before the check)."""
        barrier = threading.Barrier(len(jobs), timeout=3)
        real = getattr(patch_target, patch_attr)

        def waiting(*a, **k):
            result = real(*a, **k)
            if key is None or key in a:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
            return result
        out = []
        with mock.patch.object(patch_target, patch_attr, waiting):
            threads = [threading.Thread(target=lambda j=j: out.append(j())) for j in jobs]
            [t.start() for t in threads]
            [t.join(30) for t in threads]
        db.session.rollback()
        db.session.expire_all()
        return out

    def test_last_copy_is_lent_only_once(self):
        b = self.asset(title="Letztes", quantity=1)
        users = [self.make(f"u{i}", "reader") for i in range(4)]
        clients = [self.client_for(u.username) for u in users]
        bid = b.id
        out = self.run_parallel([lambda c=c: c.post(f"/gegenstaende/{bid}/ausgeben").status_code for c in clients],
                                User, "can_borrow")
        self.assertEqual(out, [302] * 4)
        self.assertEqual(Loan.query.filter_by(asset_id=b.id, returned_at=None).count(), 1)

    def test_same_user_same_asset_only_once(self):
        b = self.asset(title="Viele", quantity=5)
        c1, c2, c3 = (self.client_for("leser") for _ in range(3))
        bid = b.id
        self.run_parallel([lambda c=c: c.post(f"/gegenstaende/{bid}/ausgeben").status_code for c in (c1, c2, c3)],
                          User, "can_borrow")
        self.assertEqual(Loan.query.filter_by(asset_id=b.id, user_id=self.leser.id, returned_at=None).count(), 1)

    def test_max_loans_not_exceeded(self):
        set_setting("max_loans", "1")
        db.session.commit()
        b1, b2, b3 = (self.asset(title=f"B{i}", quantity=3) for i in range(3))
        clients = [self.client_for("leser") for _ in range(3)]
        ids = [b1.id, b2.id, b3.id]
        self.run_parallel([lambda c=c, i=i: c.post(f"/gegenstaende/{i}/ausgeben").status_code
                           for c, i in zip(clients, ids)], User, "can_borrow")
        self.assertEqual(Loan.query.filter_by(user_id=self.leser.id, returned_at=None).count(), 1)

    def test_self_extend_respects_maximum(self):
        set_setting("self_extend", "1")
        set_setting("max_extensions", "1")
        db.session.commit()
        b = self.asset(quantity=1)
        l = self.loan(b, self.leser)
        clients = [self.client_for("leser") for _ in range(3)]
        lid = l.id
        self.run_parallel([lambda c=c: c.post(f"/ausleihen/{lid}/verlaengern").status_code for c in clients],
                          loans_view, "get_bool", key="self_extend")
        self.assertEqual(db.session.get(Loan, l.id).extensions, 1)


class ActiveLoanIndexTests(Base):
    def test_second_open_loan_rejected_by_database(self):
        b = self.asset(quantity=3)
        self.loan(b, self.leser)
        self.loan(b, self.leser, returned=True)   # finished ones do not count
        with self.assertRaises(IntegrityError):
            self.loan(b, self.leser)
        db.session.rollback()

    def test_existing_duplicates_only_warn(self):
        db.session.execute(text("DROP INDEX uq_loans_active_asset_user"))
        db.session.commit()
        b = self.asset(quantity=3)
        self.loan(b, self.leser)
        self.loan(b, self.leser)
        with self.assertLogs("assetlotse.models", "WARNING"):
            self.assertFalse(ensure_indexes())
        Loan.query.filter_by(asset_id=b.id).first().returned_at = datetime.now()
        db.session.commit()
        self.assertTrue(ensure_indexes())

    def test_lend_message_when_already_lent(self):
        b = self.asset(quantity=3)
        self.loan(b, self.leser)
        self.login("leser")
        r = self.c.post(f"/gegenstaende/{b.id}/ausgeben", follow_redirects=True)
        self.assertIn("hat diesen Gegenstand bereits", r.get_data(as_text=True))


# ---------------------------------------------------------------- 4. desk: inventory/serial number before internal number

class DeskSearchTests(Base):
    def test_digits_match_inventory_number_not_internal_id(self):
        target = self.asset(title="Alpha")
        other = self.asset(title="Beta", inventory_no=str(target.id))
        lower = self.asset(title="Gamma", inventory_no="ab-7")
        serial = self.asset(title="Delta", serial_number="SN-X1")
        self.login("admin")
        with self.app.test_request_context():
            from flask_login import login_user
            login_user(self.admin)
            self.assertEqual([b.id for b in loans_view._find_assets(str(target.id))[0]], [other.id])
            self.assertEqual([b.id for b in loans_view._find_assets("AB-7")[0]], [lower.id])
            self.assertEqual([b.id for b in loans_view._find_assets("sn-x1")[0]], [serial.id])
            self.assertEqual([b.id for b in loans_view._find_assets(f"#{target.id}")[0]], [target.id])
            self.assertEqual(loans_view._find_assets("987654"), ([], False))
            self.assertEqual(loans_view._find_assets("#99999999999999999999"), ([], True))


# ---------------------------------------------------------------- 6. stock/export without a query per item

class PerformanceTests(Base):
    def setUp(self):
        super().setUp()
        db.session.bulk_save_objects([Asset(name=f"Gerät {i:03}", quantity=1, location="R1" if i % 2 else "R2")
                                      for i in range(120)])
        db.session.commit()
        ids = [b.id for b in Asset.query.order_by(Asset.name).limit(30)]
        db.session.bulk_save_objects([Loan(asset_id=i, user_id=self.leser.id, due_at=date(2030, 1, 1)) for i in ids])
        db.session.bulk_save_objects([Loan(asset_id=i, user_id=self.lb.id, due_at=date(2030, 1, 1),
                                           returned_at=datetime.now()) for i in ids[30:] + ids[:5]])
        db.session.commit()
        self.queries = [0]

        @event.listens_for(db.engine, "before_cursor_execute")
        def count(*a):
            self.queries[0] += 1
        self.addCleanup(event.remove, db.engine, "before_cursor_execute", count)

    def get(self, url):
        self.queries[0] = 0
        r = self.c.get(url)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True), self.queries[0]

    def test_status_filter_and_paging_in_sql(self):
        self.login("admin")
        html, n = self.get("/gegenstaende?status=verfuegbar")
        self.assertIn("90 Einträge gefunden", html)
        self.assertLess(n, 60)
        self.assertNotIn(">Gerät 000<", html)
        html, n = self.get("/gegenstaende?status=ausgegeben&seite=2")
        self.assertIn("30 Einträge gefunden", html)
        self.assertIn(">Gerät 024<", html)
        self.assertNotIn(">Gerät 030<", html)
        html, n = self.get("/gegenstaende?sort=name&seite=5")
        self.assertIn(">Gerät 119<", html)
        self.assertEqual(html.count('class="asset-card"'), assets_view.PER_PAGE)

    def test_export_uses_aggregate(self):
        self.login("admin")
        csv_text, n = self.get("/gegenstaende/export.csv")
        self.assertLess(n, 40)
        rows = [r.split(";") for r in csv_text.strip().splitlines()[1:]]
        self.assertEqual(len(rows), 120)
        avail = {r[1]: r[-2] for r in rows}
        holder = {r[1]: r[-1] for r in rows}
        self.assertEqual(avail["Gerät 000"], "0")
        self.assertEqual(holder["Gerät 000"], "Leser")
        self.assertEqual(avail["Gerät 050"], "1")
        self.assertEqual(holder["Gerät 050"], "")


# ---------------------------------------------------------------- 7.–10. visibility and permissions

class VisibilityTests(Base):
    def test_desk_hides_other_groups_loans_and_names(self):
        secret = self.asset(title="Secret-B", group_id=self.gb.id)
        mine = self.asset(title="Eigenes-A", group_id=self.ga.id)
        self.lb.groups = [self.ga, self.gb]
        db.session.commit()
        self.loan(secret, self.lb)
        self.loan(mine, self.lb)
        self.login("biba")
        html = self.c.get("/theke?benutzer=lb").get_data(as_text=True)
        self.assertNotIn("Secret-B", html)
        self.assertIn("Eigenes-A", html)
        self.assertNotIn("A, B", html)
        self.assertIn("Gruppen: A", html)

    def test_adopt_new_checks_other_groups(self):
        self.asset(title="mine", location="Regal 1 ", group_id=self.ga.id)
        theirs = self.asset(title="theirs", location="Regal 1 ", group_id=self.gb.id)
        self.login("biba")
        self.c.post("/standorte/bestand", data={"value": "Regal 1 ", "target": "__neu__"})
        db.session.expire_all()
        self.assertEqual(db.session.get(Asset, theirs.id).location, "Regal 1 ")
        self.assertEqual(Location.query.count(), 0)
        self.login("admin")
        self.c.post("/standorte/bestand", data={"value": "Regal 1 ", "target": "__neu__"})
        db.session.expire_all()
        self.assertEqual(db.session.get(Asset, theirs.id).location, "Regal 1")

    def test_no_reminder_for_returned_loan(self):
        b = self.asset(group_id=self.ga.id)
        l = self.loan(b, self.leser, returned=True)
        self.login("biba")
        with mock.patch.object(loans_view, "send_manual_reminder", return_value=(True, "")) as send:
            r = self.c.post(f"/ausleihen/{l.id}/erinnern", follow_redirects=True)
        send.assert_not_called()
        self.assertIn("bereits abgeschlossen", r.get_data(as_text=True))


# ---------------------------------------------------------------- 11. invalid input → no 500

class MalformedInputTests(Base):
    def test_no_server_errors(self):
        b = self.asset(group_id=self.ga.id, location="foo")
        Location.query.delete()
        db.session.add(Location(name="²"))
        db.session.commit()
        self.login("leser")
        self.assertEqual(self.c.get("/gegenstaende?gruppe=%C2%B2").status_code, 200)
        self.assertEqual(self.c.get("/gegenstaende/" + "9" * 25).status_code, 404)
        self.login("biba")
        H = "9" * 30
        checks = [
            self.c.post(f"/gegenstaende/{b.id}/bearbeiten", data={"name": "x", "group_id": "²", "quantity": "1"}),
            self.c.get("/theke?code=%C2%B2"),
            self.c.post("/standorte/bestand", data={"value": "foo", "target": "²"}),
            self.c.post("/standorte/bestand", data={"value": "foo", "target": H}),
            self.c.post(f"/gegenstaende/{b.id}/ausgeben", data={"user_id": H}),
            self.c.post("/theke", data={"modus": "rueckgabe", "loan_id": H}),
            self.c.post(f"/ausleihen/{H}/erinnern"),
            self.c.get("/standorte"),
        ]
        for r in checks:
            self.assertLess(r.status_code, 500, r.request.path)

    def test_huge_numbers_are_clamped(self):
        b = self.asset(group_id=self.ga.id)
        self.login("biba")
        r = self.c.post(f"/gegenstaende/{b.id}/bearbeiten", data={"name": "x", "quantity": "9" * 30})
        self.assertEqual(r.status_code, 302)
        db.session.expire_all()
        b = db.session.get(Asset, b.id)
        self.assertEqual(b.quantity, assets_view.MAX_QUANTITY)


# ---------------------------------------------------------------- 12./13. paging

class PagingTests(Base):
    def test_catalogue_pages_keep_location(self):
        db.session.add(Location(name="R1"))
        db.session.commit()
        for i in range(45):
            self.asset(title=f"b{i:02}", location="R1")
        self.login("leser")
        html = self.c.get("/gegenstaende?standort=R1").get_data(as_text=True)
        links = re.findall(r'href="(/gegenstaende\?[^"]*seite=2[^"]*)"', html)
        self.assertTrue(links)
        self.assertTrue(all("standort=R1" in l for l in links))

    def test_loan_list_is_paginated(self):
        db.session.bulk_save_objects([Asset(name=f"L{i:03}", quantity=1) for i in range(120)])
        db.session.commit()
        db.session.bulk_save_objects([Loan(asset_id=b.id, user_id=self.leser.id, due_at=date(2030, 1, 1))
                                      for b in Asset.query.all()])
        db.session.commit()
        self.login("admin")
        html = self.c.get("/ausleihen?q=L").get_data(as_text=True)
        self.assertEqual(html.count('class="cell-asset '), 50)
        self.assertIn("seite=3", html)
        self.assertIn("q=L", re.search(r'href="([^"]*seite=3[^"]*)"', html).group(1))
        html = self.c.get("/ausleihen?q=L&seite=3").get_data(as_text=True)
        self.assertEqual(html.count('class="cell-asset '), 20)


if __name__ == "__main__":
    unittest.main()
