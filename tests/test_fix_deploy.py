"""Betrieb/Deployment: Startsperre, Schlüssel, Schema-Nachrüstung, update.sh, Entrypoint, Zeitzone."""
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-test-"))

import assetlotse  # noqa: E402
from assetlotse import _ensure_timezone, _secret_key, _startup_lock, create_app  # noqa: E402
from assetlotse.models import Asset, Group, Setting, User, _add_column_ddl, db, ensure_columns, get_setting  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HAS_FORK = "fork" in mp.get_all_start_methods()


# ------------------------------------------------------------------ Hilfen für Mehrprozess-Tests
def _start_app(data_dir, barrier, q):
    os.environ["ASSETLOTSE_DATA_DIR"] = data_dir
    os.environ["ASSETLOTSE_SCHEDULER"] = "0"
    os.environ.pop("ASSETLOTSE_SECRET_KEY", None)
    os.environ.pop("DATABASE_URL", None)
    barrier.wait()
    try:
        app = create_app()
        q.put(("ok", app.config["SECRET_KEY"]))
    except BaseException as exc:  # noqa: BLE001
        q.put(("err", f"{type(exc).__name__}: {str(exc)[:200]}"))


def _read_key(data_dir, barrier, q):
    barrier.wait()
    try:
        q.put(("ok", _secret_key(Path(data_dir))))
    except BaseException as exc:  # noqa: BLE001
        q.put(("err", repr(exc)))


def _run_pair(target, data_dir):
    ctx = mp.get_context("fork")
    barrier, q = ctx.Barrier(2), ctx.Queue()
    ps = [ctx.Process(target=target, args=(data_dir, barrier, q)) for _ in range(2)]
    for p in ps:
        p.start()
    res = [q.get(timeout=60) for _ in ps]
    for p in ps:
        p.join(30)
    return res


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cx-deploy-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ------------------------------------------------------------------ 1. Startsperre / gleichzeitiger Start
@unittest.skipUnless(HAS_FORK, "braucht fork")
class StartupRaceTest(TmpDir):
    def test_two_workers_start_on_empty_data_dir(self):
        for i in range(5):
            d = os.path.join(self.tmp, f"d{i}")
            os.mkdir(d)
            res = _run_pair(_start_app, d)
            self.assertEqual([r for r in res if r[0] != "ok"], [], res)
            key = Path(d, "secret_key").read_text().strip()
            self.assertTrue(key)
            self.assertEqual({r[1] for r in res}, {key})

    def test_lock_is_exclusive(self):
        import fcntl
        inst = Path(self.tmp)
        with _startup_lock(inst):
            with open(inst / ".startup.lock", "a+b") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with open(inst / ".startup.lock", "a+b") as other:   # nach dem Block wieder frei
            fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(other.fileno(), fcntl.LOCK_UN)

    def test_lock_unavailable_is_not_fatal(self):
        with _startup_lock(Path(self.tmp) / "gibt-es-nicht"):
            pass


class EnsureColumnsTolerantTest(TmpDir):
    def test_duplicate_column_from_other_process_is_skipped(self):
        """Hat ein anderer Prozess die Spalte inzwischen angelegt, bricht der Start nicht ab."""
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{self.tmp}/x.db"})
        with app.app_context():
            import sqlalchemy
            real_inspect = sqlalchemy.inspect

            class StaleInspector:
                def __init__(self, insp):
                    self.insp = insp

                def has_table(self, name):
                    return self.insp.has_table(name)

                def get_columns(self, name):   # Stand „vor“ dem anderen Prozess: users.language fehlt
                    cols = self.insp.get_columns(name)
                    return [c for c in cols if not (name == "users" and c["name"] == "language")]

            with mock.patch("sqlalchemy.inspect", lambda e: StaleInspector(real_inspect(e))):
                self.assertEqual(ensure_columns(), [])
            db.session.remove()
            db.engine.dispose()

    def test_missing_column_is_added(self):
        uri = f"sqlite:///{self.tmp}/y.db"
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": uri})
        with app.app_context():
            with db.engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE users DROP COLUMN language")
            self.assertEqual(ensure_columns(), ["users.language"])
            db.session.remove()
            db.engine.dispose()


# ------------------------------------------------------------------ 2. secret_key
class SecretKeyTest(TmpDir):
    def test_concurrent_creation_same_key(self):
        if not HAS_FORK:
            self.skipTest("braucht fork")
        with mock.patch.dict(os.environ, {"ASSETLOTSE_SECRET_KEY": ""}):
            for i in range(30):
                d = os.path.join(self.tmp, f"k{i}")
                os.mkdir(d)
                res = _run_pair(_read_key, d)
                key = Path(d, "secret_key").read_text().strip()
                self.assertTrue(key)
                self.assertEqual(res, [("ok", key), ("ok", key)])

    def test_file_mode_0600(self):
        with mock.patch.dict(os.environ, {"ASSETLOTSE_SECRET_KEY": ""}):
            _secret_key(Path(self.tmp))
        self.assertEqual(os.stat(Path(self.tmp, "secret_key")).st_mode & 0o777, 0o600)

    def test_empty_key_file_is_never_used(self):
        Path(self.tmp, "secret_key").write_text("  \n")
        with mock.patch.dict(os.environ, {"ASSETLOTSE_SECRET_KEY": "   "}), \
                mock.patch.object(assetlotse.time, "sleep"):
            with self.assertRaises(SystemExit) as cm:
                _secret_key(Path(self.tmp))
        self.assertIn("leer", str(cm.exception))

    def test_waits_for_other_writer(self):
        f = Path(self.tmp, "secret_key")
        f.write_text("")
        calls = []

        def fake_sleep(_):
            calls.append(1)
            if len(calls) == 3:
                f.write_text("abc123\n")
        with mock.patch.dict(os.environ, {"ASSETLOTSE_SECRET_KEY": ""}), \
                mock.patch.object(assetlotse.time, "sleep", fake_sleep):
            self.assertEqual(_secret_key(Path(self.tmp)), "abc123")


# ------------------------------------------------------------------ 6. Schlüssel-Prüfwert
class KeyMarkerTest(TmpDir):
    def _app(self, key):
        return create_app({"TESTING": True, "SECRET_KEY": key,
                           "SQLALCHEMY_DATABASE_URI": f"sqlite:///{self.tmp}/k.db"})

    def _dispose(self, app):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_changed_key_refuses_start_unless_accepted(self):
        app = self._app("schluessel-a")
        with app.app_context():
            marker = get_setting("secret_key_check")
            self.assertTrue(marker)
            self.assertNotIn("schluessel-a", marker)
        self._dispose(app)
        self._dispose(self._app("schluessel-a"))          # gleicher Schlüssel: startet
        with mock.patch.dict(os.environ, {"ASSETLOTSE_ACCEPT_NEW_KEY": ""}):
            with self.assertRaises(SystemExit) as cm:
                self._app("schluessel-b")
        self.assertIn("passt", str(cm.exception))
        self.assertIn("ASSETLOTSE_ACCEPT_NEW_KEY", str(cm.exception))
        with mock.patch.dict(os.environ, {"ASSETLOTSE_ACCEPT_NEW_KEY": "1"}):
            app = self._app("schluessel-b")
        with app.app_context():
            self.assertNotEqual(get_setting("secret_key_check"), marker)
        self._dispose(app)
        self._dispose(self._app("schluessel-b"))          # danach ohne Variable
        with self.assertRaises(SystemExit):
            self._app("schluessel-a")

    def test_existing_db_without_marker_gets_one(self):
        app = self._app("k1")
        with app.app_context():
            db.session.delete(db.session.get(Setting, "secret_key_check"))
            db.session.commit()
        self._dispose(app)
        app = self._app("k1")
        with app.app_context():
            self.assertTrue(get_setting("secret_key_check"))
        self._dispose(app)


# ------------------------------------------------------------------ 7. DDL für ensure_columns
class AddColumnDDLTest(unittest.TestCase):
    def ddl(self, dialect, col):
        return _add_column_ddl(col.table, col, dialect)

    def test_reserved_table_names_are_quoted(self):
        from sqlalchemy.dialects import mysql, sqlite
        col = Group.__table__.c.name
        self.assertTrue(self.ddl(mysql.dialect(), col)[0].startswith("ALTER TABLE `groups` ADD COLUMN name "))
        # „key“ ist in MySQL reserviert
        k = Setting.__table__.c.key
        self.assertIn("ADD COLUMN `key`", self.ddl(mysql.dialect(), k)[0])
        self.assertTrue(self.ddl(sqlite.dialect(), User.__table__.c.language)[0]
                        .startswith("ALTER TABLE users ADD COLUMN language VARCHAR"))

    def test_mysql_text_column_without_default(self):
        from sqlalchemy.dialects import mysql
        stmts = self.ddl(mysql.dialect(), User.__table__.c.totp_secret)
        self.assertEqual(stmts[0], "ALTER TABLE users ADD COLUMN totp_secret TEXT")
        self.assertEqual(stmts[1], "UPDATE users SET totp_secret = ''")
        # VARCHAR behält DEFAULT + NOT NULL
        self.assertEqual(self.ddl(mysql.dialect(), User.__table__.c.language),
                         ["ALTER TABLE users ADD COLUMN language VARCHAR(5) DEFAULT '' NOT NULL"])

    def test_postgres_and_sqlite_keep_text_default(self):
        from sqlalchemy.dialects import postgresql, sqlite
        for d in (postgresql.dialect(), sqlite.dialect()):
            self.assertEqual(self.ddl(d, User.__table__.c.totp_secret),
                             ["ALTER TABLE users ADD COLUMN totp_secret TEXT DEFAULT ''"])
        self.assertEqual(self.ddl(postgresql.dialect(), User.__table__.c.totp_enabled),
                         ["ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN DEFAULT false NOT NULL"])

    def test_all_model_columns_compile(self):
        from sqlalchemy.dialects import mysql, postgresql, sqlite
        for d in (mysql.dialect(), postgresql.dialect(), sqlite.dialect()):
            for table in db.metadata.sorted_tables:
                for col in table.columns:
                    stmts = self.ddl(d, col)
                    self.assertTrue(stmts[0].startswith("ALTER TABLE "))
                    if d.name == "mysql" and any(t in stmts[0] for t in ("TEXT", "BLOB")):
                        self.assertNotIn("DEFAULT", stmts[0])


# ------------------------------------------------------------------ 8. Zeitzone
class TimezoneFallbackTest(TmpDir):
    def test_system_zoneinfo_present(self):
        zdir = Path(self.tmp, "sys")
        (zdir / "Europe").mkdir(parents=True)
        (zdir / "Europe" / "Berlin").write_bytes(b"TZif")
        env, calls = {"TZ": "Europe/Berlin"}, []
        self.assertEqual(_ensure_timezone(env, [str(zdir)], tzdata_root="", tzset=lambda: calls.append(1)),
                         "Europe/Berlin")
        self.assertEqual(env["TZ"], "Europe/Berlin")
        self.assertEqual(calls, [])

    def test_falls_back_to_tzdata_package(self):
        pkg = Path(self.tmp, "tzdata", "zoneinfo", "Europe")
        pkg.mkdir(parents=True)
        (pkg / "Berlin").write_bytes(b"TZif")
        env, calls = {"TZ": "Europe/Berlin"}, []
        res = _ensure_timezone(env, [str(Path(self.tmp, "leer"))], tzdata_root=str(pkg.parent),
                               tzset=lambda: calls.append(1))
        self.assertEqual(env["TZ"], ":" + str(pkg / "Berlin"))
        self.assertEqual(res, env["TZ"])
        self.assertEqual(calls, [1])

    def test_missing_everywhere_warns(self):
        env = {"TZ": "Europe/Berlin"}
        with self.assertLogs("assetlotse", "WARNING") as cm:
            self.assertIsNone(_ensure_timezone(env, [self.tmp], tzdata_root=self.tmp, tzset=lambda: None))
        self.assertIn("Zeitzone TZ=Europe/Berlin nicht gefunden – Uhrzeiten in UTC", cm.output[0])
        self.assertEqual(env["TZ"], "Europe/Berlin")

    def test_posix_and_utc_need_no_file(self):
        for tz in ("CET-1CEST,M3.5.0,M10.5.0/3", "UTC", ""):
            env = {"TZ": tz}
            with mock.patch.object(assetlotse.log, "warning") as w:
                _ensure_timezone(env, [self.tmp], tzdata_root=self.tmp, tzset=lambda: None)
            w.assert_not_called()

    def test_no_path_traversal(self):
        env = {"TZ": "../../etc/passwd"}
        with self.assertLogs("assetlotse", "WARNING"):
            _ensure_timezone(env, [self.tmp], tzdata_root=self.tmp, tzset=lambda: None)
        self.assertEqual(env["TZ"], "../../etc/passwd")


# ------------------------------------------------------------------ 3./4./5. Skripte und Doku
class ScriptsAndDocsTest(unittest.TestCase):
    def test_shell_syntax(self):
        subprocess.run(["bash", "-n", str(ROOT / "update.sh")], check=True)
        subprocess.run(["sh", "-n", str(ROOT / "docker-entrypoint.sh")], check=True)

    def test_update_script_never_deletes_before_copy_and_backs_up_via_entrypoint(self):
        s = (ROOT / "update.sh").read_text()
        self.assertNotIn("rm -rf ./assetlotse", s)
        self.assertIn('exec -T "$SERVICE" docker-entrypoint.sh flask --app wsgi backup', s)
        self.assertLess(s.index("2. Backup"), s.index("3. Swap the files"))
        self.assertNotIn("-cf - . |", s)

    def test_docs_use_entrypoint_for_exec(self):
        for p in ["README.md", "README.en.md", "assetlotse/templates/help/de/maintenance.html",
                  "assetlotse/templates/help/en/maintenance.html"]:
            text = (ROOT / p).read_text()
            self.assertNotIn("compose exec assetlotse flask", text, p)
            self.assertNotIn("compose exec -T assetlotse flask", text, p)
        for p in ["assetlotse/templates/help/de/maintenance.html", "assetlotse/templates/help/en/maintenance.html"]:
            self.assertIn("docker compose exec assetlotse docker-entrypoint.sh", (ROOT / p).read_text())

    def test_docs_explain_secret_key(self):
        for p, word in (("README.md", "2FA"), ("README.en.md", "2FA"), (".env.example", "2FA")):
            text = (ROOT / p).read_text()
            self.assertIn("ASSETLOTSE_ACCEPT_NEW_KEY", text, p)
            self.assertIn(word, text)

    def test_entrypoint_handles_backup_dir(self):
        s = (ROOT / "docker-entrypoint.sh").read_text()
        self.assertIn("ASSETLOTSE_BACKUP_DIR", s)


@unittest.skipUnless(shutil.which("bash") and shutil.which("unzip"), "braucht bash und unzip")
class UpdateScriptRunTest(TmpDir):
    """update.sh mit Fake-docker: normales Update und Fehler mitten im Austausch."""

    def setUp(self):
        super().setUp()
        t = Path(self.tmp)
        self.bin = t / "bin"
        self.bin.mkdir()
        self.log = t / "docker.log"
        (self.bin / "docker").write_text(
            '#!/bin/bash\necho "docker $*" >> "$DOCKER_LOG"\n'
            'args="$*"; args="${args/compose -p assetlotse-prod /compose }"\n'
            'case "$args" in\n  "compose ps -q --status running assetlotse") echo abc;;\n'
            '  "compose ps -q assetlotse") echo abc;;\n'
            '  *com.docker.compose.project*) echo assetlotse-prod;;\n'
            '  *State.Running*) echo true;;\n'
            '  "image ls"*) printf "assetlotse:0.8.0\\nassetlotse:1.0.0\\nassetlotse:2.0.0\\n";;\n'
            '  "compose version") [ -n "$NO_COMPOSE_PLUGIN" ] && exit 1;;\n'
            '  inspect*) echo healthy;;\nesac\nexit 0\n')
        # altes eigenständiges docker-compose (ruft für die Protokollierung das Fake-docker auf)
        (self.bin / "docker-compose").write_text('#!/bin/bash\necho "standalone $*" >> "$DOCKER_LOG"\nexec docker compose "$@"\n')
        (self.bin / "docker-compose").chmod(0o755)
        (self.bin / "docker").chmod(0o755)
        # alte Installation
        self.inst = t / "inst mit leer"
        self._tree(self.inst, "1.0.0", extra="ALT")
        shutil.copy(ROOT / "update.sh", self.inst / "update.sh")
        (self.inst / "data").mkdir()
        (self.inst / "data" / "assetlotse.db").write_text("db")
        (self.inst / ".env").write_text("X=1\n")
        os.chmod(self.inst, 0o750)
        # Paket 2.0.0 mit Oberordner
        pkg = t / "pkg" / "assetlotse"
        self._tree(pkg, "2.0.0", extra="NEU")
        shutil.copy(ROOT / "update.sh", pkg / "update.sh")
        self.zip = t / "pakete" / "assetlotse-2.0.0.zip"
        self.zip.parent.mkdir()
        with zipfile.ZipFile(self.zip, "w") as z:
            for f in pkg.rglob("*"):
                z.write(f, f.relative_to(pkg.parent))

    @staticmethod
    def _tree(d, version, extra):
        (d / "assetlotse").mkdir(parents=True)
        (d / "assetlotse" / "__init__.py").write_text(f'__version__ = "{version}"\n')
        (d / "assetlotse" / extra).write_text(extra)
        for f in ("docker-compose.yml", "Dockerfile", "docker-entrypoint.sh", "wsgi.py", "requirements.txt"):
            (d / f).write_text("x\n")

    def run_update(self, extra_path=None):
        env = dict(os.environ, PATH=f"{extra_path + ':' if extra_path else ''}{self.bin}:{os.environ['PATH']}",
                   DOCKER_LOG=str(self.log))
        # relative Paketangabe aus einem anderen Ordner, Aufruf mit „sh“
        return subprocess.run(["sh", str(self.inst / "update.sh"), "-y", "../pakete/assetlotse-2.0.0.zip"],
                              cwd=self.zip.parent.parent / "pkg", env=env, capture_output=True, text=True)

    def version(self):
        return (self.inst / "assetlotse" / "__init__.py").read_text()

    def test_normal_update(self):
        r = self.run_update()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("2.0.0", self.version())
        self.assertFalse((self.inst / "assetlotse" / "ALT").exists())
        self.assertTrue((self.inst / "assetlotse" / "NEU").exists())
        self.assertEqual((self.inst / "data" / "assetlotse.db").read_text(), "db")
        self.assertTrue((self.inst / ".env").exists())
        self.assertEqual(os.stat(self.inst).st_mode & 0o777, 0o750)
        self.assertEqual(list(self.inst.glob(".assetlotse-update.*")), [])
        log = self.log.read_text()
        # Projektname des laufenden Containers wird übernommen (verlinkte Ordner, anderer Ordnername)
        self.assertIn("compose -p assetlotse-prod exec -T assetlotse docker-entrypoint.sh flask --app wsgi backup", log)
        self.assertIn("compose -p assetlotse-prod up -d", log)

    def test_works_with_standalone_docker_compose(self):
        with mock.patch.dict(os.environ, {"NO_COMPOSE_PLUGIN": "1"}):
            r = self.run_update()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("2.0.0", self.version())
        self.assertIn("standalone -p assetlotse-prod up -d", self.log.read_text())

    def test_creates_missing_env(self):
        (self.inst / ".env").unlink()
        r = self.run_update()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.inst / ".env").exists())

    def test_failure_mid_swap_keeps_old_install(self):
        stub = Path(self.tmp, "badmv")
        stub.mkdir()
        (stub / "mv").write_text(
            '#!/bin/bash\nn=$(( $(cat "$MVCOUNT" 2>/dev/null || echo 0) + 1 )); echo $n > "$MVCOUNT"\n'
            'if [ "$n" = 4 ]; then echo "mv: simulierter Fehler" >&2; exit 1; fi\nexec /bin/mv "$@"\n')
        (stub / "mv").chmod(0o755)
        before = sorted(str(p.relative_to(self.inst)) for p in self.inst.rglob("*"))
        with mock.patch.dict(os.environ, {"MVCOUNT": str(Path(self.tmp, "mvcount"))}):
            r = self.run_update(str(stub))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("restored", r.stdout + r.stderr)
        self.assertIn("1.0.0", self.version())
        after = sorted(str(p.relative_to(self.inst)) for p in self.inst.rglob("*") if p.name != ".assetlotse.lock")
        self.assertEqual(after, before)


@unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("setpriv")
                     and os.path.isdir("/dev/shm"), "braucht root, setpriv und /dev/shm")
class EntrypointBackupDirTest(unittest.TestCase):
    def test_backup_dir_chowned_and_privileges_dropped(self):
        w = tempfile.mkdtemp(dir="/dev/shm", prefix="cx-ep-")
        try:
            os.chmod(w, 0o755)
            os.makedirs(f"{w}/data")
            os.makedirs(f"{w}/bk/alt")
            Path(f"{w}/bk/alt/x.db").write_text("x")
            env = dict(os.environ, ASSETLOTSE_DATA_DIR=f"{w}/data", ASSETLOTSE_BACKUP_DIR=f"{w}/bk",
                       PUID="1000", PGID="1000")
            r = subprocess.run(["sh", str(ROOT / "docker-entrypoint.sh"), "sh", "-c",
                                'id -u; touch "$ASSETLOTSE_BACKUP_DIR/neu" "$ASSETLOTSE_DATA_DIR/b.db"'],
                               env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip().splitlines()[-1], "1000")
            for p in ("bk", "bk/alt/x.db", "bk/neu", "data/b.db"):
                self.assertEqual(os.stat(f"{w}/{p}").st_uid, 1000, p)
            r = subprocess.run(["sh", str(ROOT / "docker-entrypoint.sh"), "true"], env=env,
                               capture_output=True, text=True)
            self.assertNotIn("setting owner", r.stdout)
        finally:
            shutil.rmtree(w, ignore_errors=True)

    def test_private_modes_and_puid_checks(self):
        w = tempfile.mkdtemp(dir="/dev/shm", prefix="cx-ep-")
        try:
            os.chmod(w, 0o755)
            env = dict(os.environ, ASSETLOTSE_DATA_DIR=f"{w}/data", PUID="1000", PGID="1000")
            r = subprocess.run(["sh", str(ROOT / "docker-entrypoint.sh"), "sh", "-c",
                                'touch "$ASSETLOTSE_DATA_DIR/neu.db"; grep NoNewPrivs /proc/self/status'],
                               env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("1", r.stdout.split("NoNewPrivs:")[-1])
            self.assertEqual(os.stat(f"{w}/data").st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(f"{w}/data/neu.db").st_mode & 0o077, 0)
            for bad in ({"PUID": "0"}, {"PGID": "0"}, {"PUID": "abc"}, {"PUID": ""}):
                with self.subTest(bad=bad):
                    r = subprocess.run(["sh", str(ROOT / "docker-entrypoint.sh"), "id", "-u"],
                                       env=dict(env, **bad), capture_output=True, text=True)
                    if bad.get("PUID") == "":       # leer = Standard 1000
                        self.assertEqual(r.returncode, 0, r.stderr)
                    else:
                        self.assertNotEqual(r.returncode, 0)
                        self.assertNotEqual(r.stdout.strip(), "0")
        finally:
            shutil.rmtree(w, ignore_errors=True)



class UpdatePackageCheckTest(UpdateScriptRunTest):
    """Paketprüfung: Prüfsumme, symbolische Links, Rechte aus dem Zip."""

    def run_plain(self):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", DOCKER_LOG=str(self.log))
        return subprocess.run(["bash", str(self.inst / "update.sh"), "-y", str(self.zip)],
                              env=env, capture_output=True, text=True)

    def test_checksum_mismatch_changes_nothing(self):
        Path(str(self.zip) + ".sha256").write_text("0" * 64 + "  assetlotse-2.0.0.zip\n")
        r = self.run_plain()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Checksum", r.stdout + r.stderr)
        self.assertIn("1.0.0", self.version())

    def test_checksum_ok_and_archived(self):
        import hashlib
        digest = hashlib.sha256(self.zip.read_bytes()).hexdigest()
        Path(str(self.zip) + ".sha256").write_text(f"{digest}  assetlotse-2.0.0.zip\n")
        r = self.run_plain()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Checksum matches", r.stdout)
        self.assertTrue((self.inst / "packages" / "assetlotse-2.0.0.zip.sha256").exists())

    def test_symlink_in_package_is_refused(self):
        info = zipfile.ZipInfo("assetlotse/link")
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        with zipfile.ZipFile(self.zip, "a") as z:
            z.writestr(info, "/etc/passwd")
        r = self.run_plain()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("symbolic links", r.stdout + r.stderr)
        self.assertIn("1.0.0", self.version())

    def test_world_writable_modes_are_dropped(self):
        info = zipfile.ZipInfo("assetlotse/assetlotse/offen.py")
        info.create_system = 3
        info.external_attr = (0o100777 << 16)
        with zipfile.ZipFile(self.zip, "a") as z:
            z.writestr(info, "x")
        r = self.run_plain()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        mode = (self.inst / "assetlotse" / "offen.py").stat().st_mode & 0o777
        self.assertEqual(mode & 0o022, 0)



@unittest.skipUnless(shutil.which("bash") and shutil.which("unzip"), "braucht bash und unzip")
class UpdateCleanupTest(UpdateScriptRunTest):
    """Übergabe an das neue update.sh und Aufräumen nach dem Update."""

    def _pkg(self, version, where):
        pkg = Path(self.tmp) / f"pkg-{version}" / "assetlotse"
        self._tree(pkg, version, extra="X")
        shutil.copy(ROOT / "update.sh", pkg / "update.sh")
        with zipfile.ZipFile(where, "w") as z:
            for f in pkg.rglob("*"):
                z.write(f, f.relative_to(pkg.parent))
        return where

    def test_handover_archives_package_and_cleans_up(self):
        (self.inst / ".assetlotse-update.alt1").mkdir()                     # Rest eines abgebrochenen Updates
        self._pkg("1.0.0", self.inst / "assetlotse-1.0.0.zip")              # loses Paket im Programmordner
        (self.inst / "pakete").mkdir()                                       # Archiv unter altem Namen (1.6.2)
        self._pkg("0.8.0", self.inst / "pakete" / "assetlotse-0.8.0.zip")   # altes Archiv
        r = self.run_update()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.log.read_text()
        self.assertEqual(log.count(" build "), 1, log)                      # nur einmal gebaut – nach der Übergabe
        self.assertIn("Cleaning up", r.stdout)
        self.assertFalse((self.inst / "pakete").exists())                  # umbenannt in packages/
        archive = sorted(p.name for p in (self.inst / "packages").iterdir())
        self.assertEqual(archive, ["assetlotse-1.0.0.zip", "assetlotse-2.0.0.zip"])   # aktuelles + voriges
        self.assertTrue(self.zip.exists())                                  # Paket von außerhalb: Original bleibt
        self.assertFalse((self.inst / "assetlotse-1.0.0.zip").exists())
        self.assertEqual(list(self.inst.glob(".assetlotse-update.*")), [])
        self.assertIn("image rm assetlotse:0.8.0", log)                     # alte Images weg …
        self.assertNotIn("image rm assetlotse:1.0.0", log)                  # … das vorige bleibt
        self.assertNotIn("image rm assetlotse:2.0.0", log)
        self.assertEqual((self.data_db()), "db")

    def data_db(self):
        return (self.inst / "data" / "assetlotse.db").read_text()

    def test_prune_removes_previous_image_too(self):
        env_args = ["--prune"]
        r = subprocess.run(["bash", str(self.inst / "update.sh"), "-y", *env_args, str(self.zip)],
                           env=dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", DOCKER_LOG=str(self.log)),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.log.read_text()
        self.assertIn("image rm assetlotse:1.0.0", log)
        self.assertNotIn("image rm assetlotse:2.0.0", log)

    def test_no_cleanup(self):
        (self.inst / ".assetlotse-update.alt1").mkdir()
        r = subprocess.run(["bash", str(self.inst / "update.sh"), "-y", "--no-cleanup", str(self.zip)],
                           env=dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", DOCKER_LOG=str(self.log)),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.inst / ".assetlotse-update.alt1").exists())
        self.assertNotIn("image rm", self.log.read_text())
        self.assertTrue((self.inst / "packages" / "assetlotse-2.0.0.zip").exists())   # archiviert wird trotzdem

    def test_cleanup_only(self):
        (self.inst / ".assetlotse-update.alt1").mkdir()
        self._pkg("0.9.0", self.inst / "assetlotse-0.9.0.zip")
        r = subprocess.run(["bash", str(self.inst / "update.sh"), "--cleanup"],
                           env=dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", DOCKER_LOG=str(self.log)),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.log.read_text()
        self.assertNotIn(" build ", log)
        self.assertNotIn(" up ", log)
        self.assertFalse((self.inst / ".assetlotse-update.alt1").exists())
        self.assertTrue((self.inst / "packages" / "assetlotse-0.9.0.zip").exists())
        self.assertNotIn("image rm assetlotse:0.8.0", log)      # jüngstes vorhandenes älteres Image bleibt
        self.assertNotIn("image rm assetlotse:1.0.0", log)      # installierte Version

    def test_cleanup_only_without_archive_keeps_previous_image(self):
        # erstes Aufräumen nach dem Update von einer Version ohne Aufräumen: kein Archiv, Vorversion unbekannt
        r = subprocess.run(["bash", str(self.inst / "update.sh"), "--cleanup"],
                           env=dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", DOCKER_LOG=str(self.log)),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("image rm assetlotse:0.8.0", self.log.read_text())   # jüngstes älteres Image bleibt

    def test_old_package_without_handover_still_works(self):
        # Paket, dessen update.sh die Übergabe nicht kennt (z. B. Rückweg zu einer alten Version)
        pkg = Path(self.tmp) / "pkg-old" / "assetlotse"
        self._tree(pkg, "2.0.0", extra="NEU")
        (pkg / "update.sh").write_text("#!/usr/bin/env bash\necho altes Skript\n")
        old_zip = Path(self.tmp) / "alt.zip"
        with zipfile.ZipFile(old_zip, "w") as z:
            for f in pkg.rglob("*"):
                z.write(f, f.relative_to(pkg.parent))
        r = subprocess.run(["bash", str(self.inst / "update.sh"), "-y", str(old_zip)],
                           env=dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", DOCKER_LOG=str(self.log)),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("altes Skript", r.stdout)
        self.assertEqual(self.log.read_text().count(" build "), 1)


class UpdatePullTest(UpdateScriptRunTest):
    """./update.sh --pull: fertiges Image von GitHub statt lokal bauen (Fake-docker mit pull/create/cp)."""

    REG = "ghcr.io/soleil0815/assetlotse"

    def setUp(self):
        super().setUp()
        t = Path(self.tmp)
        self.app = t / "app"                                  # Inhalt von /app im Image
        self._tree(self.app, "2.0.0", extra="AUS-IMAGE")
        shutil.copy(ROOT / "update.sh", self.app / "update.sh")
        (self.bin / "docker").write_text(
            '#!/bin/bash\necho "docker $*" >> "$DOCKER_LOG"\n'
            'args="$*"; args="${args/compose -p assetlotse-prod /compose }"\n'
            'case "$args" in\n'
            '  *com.docker.compose.project*) echo assetlotse-prod;;\n'
            '  "pull "*) [ -n "$FAKE_PULL_FAIL" ] && exit 1; exit 0;;\n'
            '  *image.source*) echo "${FAKE_SOURCE-https://github.com/soleil0815/assetlotse}";;\n'
            '  *RepoDigests*) echo "ghcr.io/x@sha256:abc";;\n'
            '  "image inspect -f {{index .Config.Labels"*) echo "$FAKE_VERSION";;\n'
            '  "create "*) echo cid123;;\n'
            '  "cp cid123:/app/. "*) cp -a "$FAKE_APP/." "$3";;\n'
            '  "compose ps -q assetlotse") echo abc;;\n'
            '  *State.Running*) echo true;;\n'
            '  "image ls"*) if [ "${@: -1}" = assetlotse ]; then printf "assetlotse:1.0.0\\n"; '
            'else printf "%s:latest\\n%s:2.0.0\\n%s:1.5.0\\n" "${@: -1}" "${@: -1}" "${@: -1}"; fi;;\n'
            '  inspect*) echo healthy;;\nesac\nexit 0\n')

    def run_pull(self, *args, version="2.0.0", **env):
        e = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", DOCKER_LOG=str(self.log),
                 FAKE_APP=str(self.app), FAKE_VERSION=version, **env)
        return subprocess.run(["bash", str(self.inst / "update.sh"), "-y", *args],
                              env=e, capture_output=True, text=True)

    def env_file(self):
        return (self.inst / ".env").read_text()

    def test_pull_installs_files_from_image_without_building(self):
        r = self.run_pull("--pull")                                         # Standardadresse
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("2.0.0", self.version())
        self.assertTrue((self.inst / "assetlotse" / "AUS-IMAGE").exists())
        self.assertFalse((self.inst / "assetlotse" / "ALT").exists())
        log = self.log.read_text()
        self.assertIn(f"pull {self.REG}:latest", log)
        self.assertIn(f"tag {self.REG}:latest {self.REG}:2.0.0", log)
        self.assertNotIn(" build ", log)
        self.assertIn("up -d --remove-orphans --no-build", log)
        self.assertLess(log.index("backup"), log.index(" up -d"))           # Sicherung vor dem Neustart
        env = self.env_file()
        self.assertIn("X=1", env)                                           # eigene Einträge bleiben
        for line in (f"ASSETLOTSE_REGISTRY_IMAGE={self.REG}", f"ASSETLOTSE_IMAGE={self.REG}",
                     "ASSETLOTSE_TAG=latest"):
            self.assertIn(line, env)
        self.assertIn(f"image rm {self.REG}:1.5.0", log)                   # ältere GitHub-Images weg …
        self.assertNotIn("image rm assetlotse:1.0.0", log)                 # … das vorige (lokale) bleibt
        self.assertNotIn(f"image rm {self.REG}:2.0.0", log)
        self.assertIn("Digest:  sha256:abc", r.stdout)
        self.assertIn(f"create {self.REG}:2.0.0", log)
        self.assertIn("rm -fv cid123", log)                                 # auch das anonyme Volume
        # zweites Mal: bestimmte Version, Einträge in der .env werden ersetzt, nicht doppelt
        r = self.run_pull("--pull", "--version", "v2.0.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"pull {self.REG}:2.0.0", self.log.read_text())
        self.assertEqual(self.env_file().count("ASSETLOTSE_TAG="), 1)

    def test_other_image_address_needs_explicit_yes_and_matching_source(self):
        # neue Adresse: auch mit -y nie ohne Rückfrage (ohne Terminal → Abbruch)
        r = self.run_pull("--pull", "--image", "ghcr.io/jemand/fork",
                          FAKE_SOURCE="https://github.com/jemand/fork")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("New image address", r.stdout + r.stderr)
        self.assertIn("1.0.0", self.version())
        # Image aus einem fremden Repository unter der bekannten Adresse
        r = self.run_pull("--pull", FAKE_SOURCE="https://github.com/boese/anderes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("was not built from https://github.com/soleil0815/assetlotse", r.stderr)
        self.assertIn("1.0.0", self.version())
        self.assertNotIn("create ", self.log.read_text())

    def test_cleanup_only_in_pull_mode_keeps_previous_registry_image(self):
        self.assertEqual(self.run_pull("--pull").returncode, 0)
        self.log.write_text("")
        r = self.run_pull("--cleanup")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        log = self.log.read_text()
        self.assertNotIn(f"image rm {self.REG}:1.5.0", log)                # jüngeres älteres GitHub-Image bleibt
        self.assertIn("image rm assetlotse:1.0.0", log)
        self.assertNotIn(f"image rm {self.REG}:2.0.0", log)

    def test_follows_latest_or_pinned_version(self):
        (self.inst / ".env").write_text("X=1\nASSETLOTSE_DOCKHAND_UPDATE=true\n")   # alte Einstellung aus 1.8.0
        r = self.run_pull("--pull")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        env, log = self.env_file(), self.log.read_text()
        self.assertIn("ASSETLOTSE_TAG=latest", env)
        self.assertNotIn("DOCKHAND", env)                                     # wird entfernt
        self.assertNotIn(f"image rm {self.REG}:latest", log)                # der Container folgt latest
        self.assertNotIn(f"image rm {self.REG}:2.0.0", log)
        # festlegen – Update-Tools lassen den Container dann in Ruhe
        r = self.run_pull("--pull", "--version", "2.0.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Pinned to version 2.0.0", r.stdout)
        self.assertIn("ASSETLOTSE_TAG=2.0.0", self.env_file())
        # das nächste --pull folgt wieder latest
        self.assertEqual(self.run_pull("--pull").returncode, 0)
        env = self.env_file()
        self.assertIn("ASSETLOTSE_TAG=latest", env)
        self.assertEqual(env.count("ASSETLOTSE_TAG="), 1)

    def test_plain_run_on_pull_server_never_switches_silently(self):
        self.assertEqual(self.run_pull("--pull").returncode, 0)
        self.log.write_text("")
        r = self.run_pull()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("runs ready-made images", r.stderr)
        self.assertNotIn(" build ", self.log.read_text())
        self.assertIn(f"ASSETLOTSE_IMAGE={self.REG}", self.env_file())

    def test_refused_pull_restores_latest(self):
        r = self.run_pull("--pull", FAKE_SOURCE="https://github.com/boese/anderes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"image rm {self.REG}:latest", self.log.read_text())   # vorher gab es kein latest

    def test_handover_to_older_script_without_new_options(self):
        old = (ROOT / "update.sh").read_text().replace('--pinned)', '--xpinned)').replace('--pulled)', '--xpulled)')
        (self.app / "update.sh").write_text(old)                          # Zielversion kennt die Optionen nicht
        r = self.run_pull("--pull", "--version", "2.0.0")
        self.assertNotIn("Unknown option", r.stdout + r.stderr)
        self.assertIn("2.0.0", self.version())

    def test_only_one_update_at_a_time(self):
        import fcntl
        with open(self.inst / ".assetlotse.lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            r = self.run_pull("--pull")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Another update is running", r.stderr)
        self.assertIn("1.0.0", self.version())

    def test_zip_after_pull_switches_back_to_local_image(self):
        self.assertEqual(self.run_pull("--pull").returncode, 0)
        r = self.run_pull(str(self.zip))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("locally built", r.stdout + r.stderr)
        env = self.env_file()
        self.assertIn("ASSETLOTSE_IMAGE=assetlotse", env)
        self.assertIn(f"ASSETLOTSE_REGISTRY_IMAGE={self.REG}", env)       # Adresse bleibt für das nächste --pull
        self.assertIn(" build ", self.log.read_text())

    def test_pull_failures_change_nothing(self):
        cases = [
            (("--pull",), {"FAKE_PULL_FAIL": "1"}, "docker login ghcr.io"),
            (("--pull", "--image", "GHCR.io/Gross"), {}, "Invalid image address"),
            (("--pull", str(self.zip)), {}, "not both"),
            (("--version", "2.0.0"), {}, "only work together with --pull"),
            (("--pull", "--version", "neu"), {}, "Invalid version"),
            (("--pull", "--version", "1.7"), {}, "Invalid version"),
        ]
        for args, env, msg in cases:
            with self.subTest(args=args):
                r = self.run_pull(*args, **env)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn(msg, r.stdout + r.stderr)
                self.assertIn("1.0.0", self.version())

    def test_image_without_install_files_or_wrong_label(self):
        (self.app / "update.sh").unlink()
        r = self.run_pull("--pull")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("incomplete", r.stderr)
        shutil.copy(ROOT / "update.sh", self.app / "update.sh")
        r = self.run_pull("--pull", version="2.1.0")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("do not match", r.stderr)
        self.assertIn("1.0.0", self.version())
        self.assertEqual(list(self.inst.glob(".assetlotse-update.*")), [])


# Die geerbten Tests des lokalen Weges nicht in jeder Unterklasse noch einmal laufen lassen
for _cls in (UpdatePackageCheckTest, UpdateCleanupTest, UpdatePullTest):
    for _name in [n for n in vars(UpdateScriptRunTest) if n.startswith("test_")]:
        setattr(_cls, _name, None)


class ReleaseFilesTest(unittest.TestCase):
    def test_versions_and_workflow_agree(self):
        import re
        version = re.search(r'__version__ = "(.*?)"', (ROOT / "assetlotse" / "__init__.py").read_text()).group(1)
        self.assertIn(f'image.version="{version}"', (ROOT / "Dockerfile").read_text())
        self.assertIn(f"${{ASSETLOTSE_TAG:-{version}}}", (ROOT / "docker-compose.yml").read_text())
        wf = (ROOT / ".github" / "workflows" / "docker.yml").read_text()
        self.assertIn("ghcr.io", wf)
        self.assertIn('tags: ["v*"]', wf)
        install_layer = (ROOT / "Dockerfile").read_text().split("COPY docker-compose.yml")[1].split("\n\n")[0]
        for f in ("update.sh", "Dockerfile", "docker-entrypoint.sh", "requirements.txt"):
            self.assertIn(f, install_layer)
        self.assertNotIn("update.sh", (ROOT / ".dockerignore").read_text().split())

    def test_trivy_scan_runs_before_publishing(self):
        import re
        wf = (ROOT / ".github" / "workflows" / "docker.yml").read_text()
        parts = re.split(r"\n  ([a-z-]+):\n", "\n" + wf.split("\njobs:\n", 1)[1])
        jobs = dict(zip(parts[1::2], parts[2::2]))
        self.assertEqual(list(jobs), ["build", "scan", "publish", "scan-latest"])
        self.assertIn("permissions: {}", wf.split("\njobs:\n")[0])           # nichts ohne ausdrückliche Rechte
        # bauen ohne zu veröffentlichen
        self.assertIn("push: false", jobs["build"])
        self.assertNotIn("push: true", wf)
        self.assertNotIn("write", jobs["build"])
        # prüfen: Fund bricht ab, der Prüfer sieht weder Schreibrechte noch eine Anmeldung an der Registry
        for name in ("scan", "scan-latest"):
            job = jobs[name]
            self.assertIn("trivy-action@", job)
            self.assertIn('exit-code: "1"', job)
            self.assertIn("CRITICAL", job)
            self.assertNotIn("write", job.split("steps:")[0])
            before_scan = job.split("trivy-action@")[0]
            if "login-action" in before_scan:
                self.assertIn("docker logout", before_scan)
        # veröffentlichen erst nach der Prüfung, genau das geprüfte Image, ohne Prüfer im Job
        self.assertRegex(jobs["publish"], r"needs: \[build, scan\]")
        self.assertNotIn("trivy", jobs["publish"])
        self.assertNotIn("build-push-action", jobs["publish"])
        self.assertLess(jobs["publish"].index("docker push"), jobs["publish"].index("Create release tag"))
        self.assertEqual(wf.count("trivy-action@"), 2)
        # nur der wöchentliche Lauf am Montag, kein Bauen dabei
        self.assertIn("schedule:", wf)
        self.assertIn("if: github.event_name != 'schedule'", jobs["build"])
        self.assertIn("if: github.event_name == 'schedule'", jobs["scan-latest"])
        # alle Actions über Commit-SHA festgelegt; trivy-action nicht aus den kompromittierten Versionen ≤ 0.34.2
        for ref in re.findall(r"uses:\s*(\S+)", wf):
            self.assertRegex(ref, r"@[0-9a-f]{40}$")
        for v in re.findall(r"trivy-action@[0-9a-f]{40} # v(\d+)\.(\d+)", wf):
            self.assertGreaterEqual(tuple(map(int, v)), (0, 35))
        self.assertTrue((ROOT / ".trivyignore").exists())


@unittest.skipUnless(shutil.which("docker") and subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0,
                     "braucht docker compose")
class ComposeInterpolationTest(unittest.TestCase):
    def config(self, env_text):
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(ROOT / "docker-compose.yml", Path(d) / "docker-compose.yml")
            (Path(d) / ".env").write_text(env_text)
            r = subprocess.run(["docker", "compose", "config"], cwd=d, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout

    def test_local_default_and_registry_latest(self):
        import re
        version = re.search(r'__version__ = "(.*?)"', (ROOT / "assetlotse" / "__init__.py").read_text()).group(1)
        out = self.config("")
        self.assertIn(f"image: assetlotse:{version}", out)
        out = self.config("ASSETLOTSE_IMAGE=ghcr.io/soleil0815/assetlotse\nASSETLOTSE_TAG=latest\n")
        self.assertIn("image: ghcr.io/soleil0815/assetlotse:latest", out)
        self.assertNotIn("dockhand", out)


if __name__ == "__main__":
    unittest.main()
