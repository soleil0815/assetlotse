"""Diagnostic package (2.7.0): anonymous, complete, and kept up to date with the program.

The classification tests fail as soon as a new setting, environment variable or database text column appears
that diagnose.py does not know yet – the decision "may this go into the package, and how?" is then due."""
import io
import json
import logging
import re
import shutil
import zipfile
from pathlib import Path

import sqlalchemy as sa
from test_app import BaseCase

from assetlotse import applog, diagnose
from assetlotse.models import (DEFAULT_SETTINGS, Asset, DirectoryPerson, Group, Loan, Location, LoginAttempt, MailLog,
                               SecretAccess, UserSession, WebAuthnCredential, db, set_setting)

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [*sorted((ROOT / "assetlotse").rglob("*.py")), ROOT / "gunicorn.conf.py", ROOT / "wsgi.py"]
WSGI_KEYS = {"HTTP_HOST", "HTTP_X_FORWARDED_PORT", "HTTP_X_FORWARDED_PROTO", "PATH_INFO", "QUERY_STRING",
             "REMOTE_ADDR", "REQUEST_METHOD", "SERVER_SOFTWARE", "HTTP_X_FORWARDED_FOR"}


def source_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in SOURCES)


class ClassificationTests(BaseCase):
    def test_every_setting_is_classified_once(self):
        used = set(DEFAULT_SETTINGS)
        used |= set(re.findall(r'(?:get_setting|set_setting|get_bool|get_int)\(\s*"([a-z0-9_]+)"', source_text()))
        sets = [diagnose.SETTINGS_SHOW, diagnose.SETTINGS_ANON, diagnose.SETTINGS_COUNT, diagnose.SETTINGS_MASK]
        for key in sorted(used):
            with self.subTest(setting=key):
                self.assertEqual(sum(key in s for s in sets), 1,
                                 f"Setting {key!r}: put it into exactly one of SETTINGS_SHOW/ANON/COUNT/MASK in "
                                 f"diagnose.py (and into SETTINGS_IDENTIFYING if it names the organization)")
        from assetlotse.models import SECRET_SETTINGS
        self.assertTrue(SECRET_SETTINGS <= diagnose.SETTINGS_MASK)
        self.assertFalse(SECRET_SETTINGS & diagnose.SETTINGS_IDENTIFYING)

    def test_every_environment_variable_is_classified_once(self):
        text = source_text()
        used = set(re.findall(r'(?:environ|env)(?:\.get)?[(\[]\s*"([A-Z][A-Z0-9_]+)"', text))
        used |= set(re.findall(r'getenv\(\s*"([A-Z][A-Z0-9_]+)"', text))
        used |= set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), re.M))
        # passed in by docker-compose.yml, read by update.sh / the entrypoint from .env
        used |= set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)", (ROOT / "docker-compose.yml").read_text()))
        used |= set(re.findall(r"env_value ([A-Z][A-Z0-9_]+)", (ROOT / "update.sh").read_text()))
        used |= {v for v in re.findall(r"\$\{?([A-Z][A-Z0-9_]+)", (ROOT / "docker-entrypoint.sh").read_text())
                 if v.startswith("ASSETLOTSE_")}
        used -= WSGI_KEYS
        sets = [diagnose.ENV_SHOW, diagnose.ENV_ANON, diagnose.ENV_MASK]
        for name in sorted(used):
            with self.subTest(env=name):
                self.assertEqual(sum(name in s for s in sets), 1,
                                 f"Environment variable {name!r}: put it into exactly one of ENV_SHOW/ANON/MASK")
        self.assertIn("ASSETLOTSE_SECRET_KEY", diagnose.ENV_MASK)
        self.assertIn("DATABASE_URL", diagnose.ENV_MASK)

    def test_every_text_column_is_classified(self):
        for table in db.metadata.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, (sa.String, sa.Text)):
                    with self.subTest(column=f"{table.name}.{col.name}"):
                        self.assertIn(f"{table.name}.{col.name}", diagnose.COLUMNS,
                                      "New text column: classify it in diagnose.COLUMNS (personal category, "
                                      "'secret' or 'plain')")


class AnonymityTests(BaseCase):
    """Every personal column, every identifying setting and every secret gets a marker value; the markers are also
    written to the program log. None of them may appear anywhere in the package."""

    def setUp(self):
        super().setUp()
        self.instance = Path(self.app.instance_path)
        shutil.rmtree(applog.log_dir(self.instance), ignore_errors=True)
        self.addCleanup(shutil.rmtree, applog.log_dir(self.instance), True)

    def fill(self) -> list[str]:
        admin = self.make_user("chefin", role="admin")
        reader = self.make_user("leser")
        group = Group(name="g")
        db.session.add_all([group, Location(name="l"), DirectoryPerson(username="d", username_key="d"),
                            LoginAttempt(key="k", username="k")])
        asset = Asset(name="a")
        db.session.add(asset)
        db.session.flush()
        db.session.add_all([Loan(asset_id=asset.id, user_id=reader.id),
                            SecretAccess(asset_id=asset.id, user_id=admin.id),
                            WebAuthnCredential(user_id=reader.id, rp_id="x", credential_id="c", credential_data="d"),
                            MailLog(kind="test"), UserSession(user_id=reader.id, token_hash="t" * 64)])
        db.session.commit()
        markers: list[str] = []
        self.secret_markers: list[str] = []      # never read for the package (and never logged by the program)
        n = 0
        for table in db.metadata.sorted_tables:
            for col in table.columns:
                category = diagnose.COLUMNS.get(f"{table.name}.{col.name}")
                if category in (None, "plain", "setting"):
                    continue
                count = db.session.execute(sa.select(sa.func.count()).select_from(table)).scalar()
                self.assertGreater(count, 0, f"test needs a row in {table.name} – add one in fill()")
                pk = list(table.primary_key.columns)[0]
                for (row_id,) in db.session.execute(sa.select(pk)).all():
                    n += 1
                    if category == "email":
                        value = f"qzxmail{n}@qzxdomain{n}.example.org"
                        markers += [f"qzxmail{n}", f"qzxdomain{n}"]
                    elif category == "dn":
                        value = f"CN=Qzxcn{n} Qzxsn{n},OU=Qzxou{n},DC=qzxdc{n},DC=local"
                        markers += [f"qzxcn{n}", f"qzxsn{n}", f"qzxou{n}", f"qzxdc{n}"]
                    elif category == "host":
                        value = f"qzxhost{n}.qzxzone{n}.de"
                        markers += [f"qzxhost{n}", f"qzxzone{n}"]
                    else:
                        value = f"Qzxval{n} Qzxword{n}"
                        (self.secret_markers if category == "secret" else markers).extend([f"qzxval{n}", f"qzxword{n}"])
                    db.session.execute(table.update().where(pk == row_id).values({col.name: value}))
        db.session.commit()
        from assetlotse.models import SECRET_SETTINGS, Setting
        for i, key in enumerate(sorted(diagnose.SETTINGS_IDENTIFYING | diagnose.SETTINGS_MASK)):
            set_setting(key, f"https://qzxset{i}.qzxorg{i}.de/ Qzxtext{i} CN=Qzxgroup{i},DC=x")
        db.session.commit()
        for i, key in enumerate(sorted(diagnose.SETTINGS_IDENTIFYING | diagnose.SETTINGS_MASK)):
            row = db.session.get(Setting, key)
            if row is None or f"Qzxtext{i}" not in row.value and key not in SECRET_SETTINGS:
                continue              # the setting does not take such a value (e.g. a file name only)
            words = [f"qzxset{i}", f"qzxorg{i}", f"qzxtext{i}", f"qzxgroup{i}"]
            (self.secret_markers if key in SECRET_SETTINGS else markers).extend(words)
        self.markers = markers
        return markers

    def write_log(self, markers):
        handler = applog.DataLogHandler(applog.log_dir(self.instance) / applog.LOG_NAME)
        logger = logging.getLogger("assetlotse.test_diagnose")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        logger.setLevel(logging.INFO)
        for m in markers:
            logger.warning("Sign-in as %s from 198.51.100.%d failed (mail %s@%s.de, host %s.corp.example.de)",
                           m, len(m) % 250, m, m, m)
        try:
            raise ValueError(f"bad value '{markers[0]}' for qzxtrace2@example.com at 10.1.2.3")
        except ValueError:
            logger.exception("Unexpected error")
        markers += ["qzxtrace2", "198.51.100.", "10.1.2.3"]

    def package(self) -> dict[str, str]:
        self.make_user("diagadmin", role="admin")
        self.login("diagadmin")
        r = self.c.post("/admin/diagnose/paket")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/zip")
        self.assertIn("no-store", r.headers["Cache-Control"])
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            return {n: zf.read(n).decode("utf-8") for n in zf.namelist()}

    def test_nothing_personal_or_secret_in_the_package(self):
        markers = self.fill()
        self.write_log(markers)
        files = self.package()
        self.assertEqual(sorted(files), ["README.txt", "diagnose.json", "log.txt", "summary.txt"])
        everything = "\n".join(files.values()).lower()
        for m in markers + self.secret_markers:
            with self.subTest(marker=m):
                self.assertNotIn(m.lower(), everything)
        self.assertNotIn("diagadmin", everything)
        self.assertNotIn("chefin", everything)
        # the log is still useful: structure and pseudonyms are there
        self.assertIn("Sign-in as", files["log.txt"])
        self.assertRegex(files["log.txt"], r"ip-\d+ \(private\)")
        self.assertIn("@example.invalid", files["log.txt"])
        self.assertIn("Traceback", files["log.txt"])
        self.assertIn("ValueError", files["log.txt"])

    def test_report_is_complete(self):
        self.fill()
        report = json.loads(self.package()["diagnose.json"])
        self.assertEqual(report["format"], diagnose.FORMAT)
        for section in ("runtime", "request", "storage", "database", "backups", "security", "users", "inventory",
                        "mail", "ldap", "settings", "environment", "packages", "log", "findings"):
            with self.subTest(section=section):
                self.assertIn(section, report)
                if isinstance(report[section], dict):
                    self.assertNotIn("section_error", report[section])
        # every table is counted, the schema is checked generically
        self.assertEqual(set(report["database"]["tables"]), {t.name for t in db.metadata.sorted_tables})
        self.assertEqual(report["database"]["missing_columns"], {})
        self.assertEqual(report["users"]["roles"]["admin"], 2)
        # settings: shown, anonymized, counted or only "set"
        s = report["settings"]
        self.assertEqual(s["loan_days"]["value"], str(DEFAULT_SETTINGS["loan_days"]))
        self.assertTrue(s["smtp_password"]["set"])
        self.assertNotIn("value", s["smtp_password"])
        self.assertIn("entries", s["ldap_login_rules"])
        self.assertIn("host-", s["ldap_server"]["value"])
        self.assertEqual(report["environment"].get("DATABASE_URL"), {"set": True, "scheme": "sqlite"})

    def test_secret_values_never_read(self):
        set_setting("smtp_password", "Qzxsupersecret")
        db.session.commit()
        self.assertNotIn("qzxsupersecret", json.dumps(self.package()).lower())


class AnonymizerTests(BaseCase):
    def test_patterns(self):
        a = diagnose.Anonymizer(diagnose.program_words())
        a.add("user", "mmueller")
        a.add("person", "Michael Müller")
        out = a.scrub("AD sign-in as mmueller (Michael Müller) from 192.168.1.20 via ldaps://dc01.firma.local:636 "
                      "bind CN=Michael Müller,OU=Staff,DC=firma,DC=local to m.mueller@firma.de FIRMA\\mmueller "
                      "proxy 172.18.0.0/16 public 8.8.8.8 key 0123456789abcdef0123456789abcdef0123456789abcdef "
                      "smtp.firma.de:587 at 10:07:09 127.0.0.1 ::1 sqlalchemy.engine line 1967")
        for word in ("mmueller", "Michael", "Müller", "192.168", "dc01", "firma", "Staff", "8.8.8.8", "0123456789"):
            self.assertNotIn(word, out)
        self.assertIn("ip-1 (private)", out)
        self.assertRegex(out, r"ip-\d+ \(public\)")
        self.assertIn("ldaps://host-", out)
        self.assertIn(":636", out)
        self.assertIn("@example.invalid", out)
        self.assertIn("<redacted>", out)
        for kept in ("10:07:09", "127.0.0.1", "::1", "sqlalchemy.engine", "line 1967", ":587"):
            self.assertIn(kept, out)
        # public registries are no secret
        self.assertEqual(a.scrub("image ghcr.io/soleil0815/assetlotse:latest https://ghcr.io/x"),
                         "image ghcr.io/soleil0815/assetlotse:latest https://ghcr.io/x")
        # stable: the same value gets the same pseudonym
        self.assertEqual(a.scrub("mmueller"), a.scrub("MMUELLER"))

    def test_program_words_stay(self):
        a = diagnose.Anonymizer(diagnose.program_words())
        a.add("person", "Error Database")        # unlucky names must not destroy the log
        self.assertEqual(a.scrub("Error: Database locked"), "Error: Database locked")


class PageTests(BaseCase):
    def test_admins_only(self):
        self.make_user("vera", role="manager")
        self.login("vera")
        self.assertEqual(self.c.get("/admin/diagnose").status_code, 403)
        self.assertEqual(self.c.post("/admin/diagnose/paket").status_code, 403)

    def test_page_shows_findings(self):
        self.make_user("chefin", role="admin")
        self.login("chefin")
        html = self.c.get("/admin/diagnose").get_data(as_text=True)
        self.assertIn("Diagnosepaket herunterladen", html)
        self.assertIn("Auffälligkeiten", html)
        # only one admin and no HTTPS in the test client
        self.assertIn("Nur ein aktiver Administrator", html)
        self.assertIn("nicht über HTTPS aufgerufen", html)
        self.assertEqual(self.c.get("/admin/diagnose/paket").status_code, 405)   # never via a link/prefetch

    def test_finding_for_missing_column(self):
        report = {"database": {"quick_check": "ok", "missing_tables": [], "missing_columns": {"users": ["x"]}}}
        codes = [f["code"] for f in diagnose.findings(report)]
        self.assertEqual(codes, ["db_schema"])
        self.assertIn("users.x", diagnose.finding_text(diagnose.findings(report)[0]))

    def test_every_finding_has_a_text_and_translation(self):
        import json as _json
        de = _json.loads((ROOT / "assetlotse" / "translations" / "de.json").read_text(encoding="utf-8"))
        for code, text in diagnose._TEXTS.items():
            for one in (text if isinstance(text, tuple) else (text,)):
                with self.subTest(code=code):
                    self.assertIn(one, de)
        for code in set(re.findall(r'add\("([a-z0-9_]+)", "(?:error|warning|info)"', (ROOT / "assetlotse" / "diagnose.py").read_text())):
            self.assertIn(code, diagnose._TEXTS)

    def test_plural_texts(self):
        one = {"code": "admins_without_2fa", "level": "warning", "params": {"n": 1}}
        two = {"code": "admins_without_2fa", "level": "warning", "params": {"n": 2}}
        self.assertEqual(diagnose.finding_text(one), "1 administrator without a second factor.")
        self.assertEqual(diagnose.finding_text(two), "2 administrators without a second factor.")
        from assetlotse.i18n import _, force_locale
        with force_locale("de"):
            self.assertEqual(diagnose.finding_text(one, _), "1 Administrator ohne zweiten Faktor.")
            self.assertEqual(diagnose.finding_text(two, _), "2 Administratoren ohne zweiten Faktor.")

    def test_every_link_target_exists(self):
        from flask import url_for
        with self.app.test_request_context():
            for code, (endpoint, args, _page) in diagnose._LINKS.items():
                with self.subTest(code=code):
                    self.assertIn(code, diagnose._TEXTS)
                    url_for(endpoint, **args)

    def test_findings_link_to_the_page_and_update_error_is_translated(self):
        from assetlotse.models import set_setting
        set_setting("update_error", "GitHub answered HTTP 403")
        set_setting("update_checked_at", "2026-11-24T02:17:05")
        db.session.commit()
        self.make_user("chefin", role="admin")
        self.login("chefin")
        html = self.c.get("/admin/diagnose").get_data(as_text=True)
        self.assertIn('<a href="/admin/benutzer?rolle=admin">Benutzer →</a>', html)
        self.assertIn("GitHub begrenzt gerade die Anfragen (HTTP 403)", html)
        self.assertNotIn("GitHub answered", html)
        self.assertIn("Zuletzt geprüft: 24.11.2026 02:17.", html)

    def test_cli(self):
        result = self.app.test_cli_runner().invoke(args=["diagnose"])
        self.assertEqual(result.exit_code, 0, result.output)
        path = Path(result.output.split(": ", 1)[1].splitlines()[0].strip())
        self.addCleanup(path.unlink)
        with zipfile.ZipFile(path) as zf:
            self.assertIn("diagnose.json", zf.namelist())
            self.assertEqual(json.loads(zf.read("diagnose.json"))["source"], "cli")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class LogFileTests(BaseCase):
    def test_rotation_and_filter(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        handler = applog.DataLogHandler(d / "logs" / "assetlotse.log", max_bytes=2000)
        own, other = logging.getLogger("assetlotse.x"), logging.getLogger("somelib")
        for lg in (own, other):
            lg.addHandler(handler)
            lg.setLevel(logging.INFO)
            self.addCleanup(lg.removeHandler, handler)
        other.info("library chatter")
        other.warning("library warning")
        for i in range(60):
            own.info("line %d", i)
        files = applog.log_files(d)
        self.assertEqual([p.name for p in files], ["assetlotse.log.1", "assetlotse.log"])
        self.assertTrue(all(p.stat().st_size <= 2000 for p in files))
        text = "".join(p.read_text() for p in files)
        self.assertIn("line 59", text)
        self.assertNotIn("library chatter", text)
        self.assertEqual((d / "logs" / "assetlotse.log").stat().st_mode & 0o777, 0o600)

    def test_unwritable_folder_is_ignored(self):
        handler = applog.DataLogHandler(Path("/proc/nonexistent/assetlotse.log"))
        handler.emit(logging.LogRecord("assetlotse", logging.ERROR, __file__, 1, "x", None, None))   # no exception


class LogInstallTests(BaseCase):
    def test_root_in_the_container_does_not_write(self):
        import os
        import tempfile
        from unittest import mock
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        root = logging.getLogger()
        before = list(root.handlers)
        self.addCleanup(lambda: [root.removeHandler(h) for h in root.handlers if h not in before])
        with mock.patch.dict(os.environ, {"ASSETLOTSE_CONTAINER": "1"}), mock.patch("os.geteuid", return_value=0):
            applog.install(d)
        self.assertEqual(root.handlers, before)
        with mock.patch.dict(os.environ, {"ASSETLOTSE_CONTAINER": "1"}), mock.patch("os.geteuid", return_value=1000):
            applog.install(d)
            applog.install(d)                                  # only once
        self.assertEqual(len(root.handlers), len(before) + 1)

