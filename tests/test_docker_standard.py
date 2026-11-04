"""Docker standard (2.0.0): everything in the image, on the host only docker-compose.yml, .env and data.

- update.sh (sh/dash, stand-in docker from tests/fake_docker.sh): pull, health check, going back, newer
  docker-compose.yml, without internet, handover from the update.sh of older installations, cleanup that never
  touches data – also not through links or mounts
- the program: backup "vor-<version>" before a new version changes the database, no start on a database of a
  newer version (with the backup to restore), restoring while it refuses to start, version of docker-compose.yml
All tests work in temporary folders – never in /etc or real data folders.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-test-"))

import assetlotse  # noqa: E402
from assetlotse import create_app  # noqa: E402
from assetlotse.models import User, db, get_setting, set_setting  # noqa: E402

SH = shutil.which("dash") or "/bin/sh"
IMAGE = "ghcr.io/soleil0815/assetlotse"
OLD_COMPOSE = "services:\n  assetlotse:\n    image: ${ASSETLOTSE_IMAGE:-assetlotse}:${ASSETLOTSE_TAG:-1.12.0}\n    build: .\n"


class FakeDocker(unittest.TestCase):
    """An installation folder and a stand-in docker with a registry of images."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="al-std-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        for d in ("registry", "images", "tags"):
            (self.state / d).mkdir(parents=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        shutil.copy(ROOT / "tests" / "fake_docker.sh", self.bin / "docker")
        (self.bin / "docker").chmod(0o755)
        self.inst = self.tmp / "my install"          # a space in the path
        self.inst.mkdir()
        shutil.copy(ROOT / "update.sh", self.inst / "update.sh")
        (self.inst / "docker-compose.yml").write_text(OLD_COMPOSE)
        (self.inst / ".env").write_text("TZ=Europe/Berlin\nASSETLOTSE_IMAGE=assetlotse\nASSETLOTSE_TAG=1.12.0\n")
        (self.inst / "data").mkdir()
        (self.inst / "data" / "assetlotse.db").write_text("db")
        # running now: 1.12.0-s1 (built from the release pipeline)
        self.image("old", "1.12.0", build="1.12.0-s1", compose=OLD_COMPOSE)
        (self.state / "tags" / "assetlotse:1.12.0").write_text("old\n")
        (self.state / "running").write_text("old\n")
        # in the registry: 2.0.0 as latest and as 2.0.0, 2.1.0
        self.image("new", "2.0.0", compose=(ROOT / "docker-compose.yml").read_text(),
                   update=(ROOT / "update.sh").read_text() + "\n# new\n")
        self.publish("new", "latest", "2.0.0")
        self.image("v21", "2.1.0", compose=(ROOT / "docker-compose.yml").read_text())
        self.publish("v21", "2.1.0")

    def image(self, iid, version, build=None, compose=None, update=None, health="healthy", title="AssetLotse"):
        d = self.state / "images" / iid
        (d / "files").mkdir(parents=True)
        (d / "labels").write_text(f"org.opencontainers.image.title={title}\norg.opencontainers.image.version={version}\n"
                                  f"io.assetlotse.build={build or version}\n")
        (d / "health").write_text(health)
        if compose:
            (d / "files" / "docker-compose.yml").write_text(compose)
        (d / "files" / "update.sh").write_text(update or (ROOT / "update.sh").read_text())

    def publish(self, iid, *tags):
        for t in tags:
            (self.state / "registry" / f"{IMAGE}:{t}".replace("/", "_")).write_text(iid + "\n")

    def run_update(self, *args, env=None, inst=None):
        e = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_STATE=str(self.state),
                 ASSETLOTSE_HEALTH_TIMEOUT="3")
        e.update(env or {})
        return subprocess.run([SH, str((inst or self.inst) / "update.sh"), *args], cwd=self.tmp, env=e,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def env(self):
        return dict(l.split("=", 1) for l in (self.inst / ".env").read_text().splitlines() if "=" in l)

    def log(self):
        return (self.state / "log").read_text()

    def running(self):
        return (self.state / "running").read_text().strip()


class UpdateTests(FakeDocker):
    def test_update_to_latest(self):
        r = self.run_update("-y")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.running(), "new")
        self.assertEqual(self.env()["ASSETLOTSE_IMAGE"], IMAGE)          # the locally built image is left behind
        self.assertEqual(self.env()["ASSETLOTSE_TAG"], "latest")
        self.assertEqual(self.env()["TZ"], "Europe/Berlin")             # own settings stay
        self.assertEqual((self.state / "tags" / f"{IMAGE}:previous".replace("/", "_")).read_text().strip(), "old")
        # newer docker-compose.yml from the image, the old one kept
        self.assertIn("ASSETLOTSE_COMPOSE_VERSION", (self.inst / "docker-compose.yml").read_text())
        self.assertEqual((self.inst / "docker-compose.yml.alt").read_text(), OLD_COMPOSE)
        self.assertTrue((self.inst / "update.sh").read_text().endswith("# new\n"))      # update.sh from the image
        self.assertEqual((self.inst / "data" / "assetlotse.db").read_text(), "db")
        log = self.log()
        self.assertNotIn("compose build", log)
        self.assertNotIn(" load", log)
        self.assertEqual([p.name for p in self.inst.iterdir() if p.name.startswith(".update")], [])
        self.assertIn("1.12.0-s1 → 2.0.0", r.stdout)

    def test_same_compose_version_is_left_alone(self):
        own = (ROOT / "docker-compose.yml").read_text() + "# my comment\n"
        (self.inst / "docker-compose.yml").write_text(own)
        r = self.run_update("-y")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.inst / "docker-compose.yml").read_text(), own)
        self.assertFalse((self.inst / "docker-compose.yml.alt").exists())

    def test_fixed_version_and_back_to_latest(self):
        r = self.run_update("-y", "--version", "v2.1.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.env()["ASSETLOTSE_TAG"], "2.1.0")
        self.assertEqual(self.running(), "v21")
        r = self.run_update("-y")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.env()["ASSETLOTSE_TAG"], "latest")
        self.assertEqual(self.running(), "new")

    def test_invalid_or_unknown_version_changes_nothing(self):
        before = (self.inst / ".env").read_text()
        r = self.run_update("-y", "--version", "2.0.0;rm -rf /")
        self.assertNotEqual(r.returncode, 0)
        r = self.run_update("-y", "--version", "9.9.9")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing changed", r.stderr)
        self.assertEqual((self.inst / ".env").read_text(), before)
        self.assertEqual(self.running(), "old")

    def test_not_an_assetlotse_image(self):
        self.image("evil", "2.0.0", title="Something")
        self.publish("evil", "latest")
        before = (self.inst / ".env").read_text()
        r = self.run_update("-y")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not an AssetLotse image", r.stderr)
        self.assertEqual((self.inst / ".env").read_text(), before)
        self.assertEqual((self.inst / "docker-compose.yml").read_text(), OLD_COMPOSE)

    def test_failed_start_goes_back_with_yes(self):
        (self.state / "images" / "new" / "health").write_text("unhealthy")
        r = self.run_update("-y")
        self.assertEqual(r.returncode, 1)
        self.assertIn("previous version 1.12.0-s1", r.stdout)
        self.assertEqual(self.running(), "old")
        self.assertEqual(self.env()["ASSETLOTSE_TAG"], "1.12.0-s1")          # fixed – update tools leave it alone
        self.assertIn("fake log line", r.stdout)

    def test_failed_start_without_terminal_asks_and_does_not_go_back(self):
        (self.state / "images" / "new" / "health").write_text("unhealthy")
        r = self.run_update()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.running(), "new")
        self.assertIn("./update.sh --rollback", r.stdout)
        r = self.run_update("--rollback")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.running(), "old")

    def test_compose_project_of_the_running_container(self):
        r = self.run_update("-y", env={"FAKE_PROJECT": "assetlotse-prod"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("compose -p assetlotse-prod up -d", self.log())

    def test_standalone_docker_compose(self):
        (self.bin / "docker-compose").write_text(f'#!/bin/sh\necho "standalone $*" >> "$FAKE_STATE/log"\nexec "{self.bin}/docker" compose "$@"\n')
        (self.bin / "docker-compose").chmod(0o755)
        r = self.run_update("-y", env={"FAKE_NO_PLUGIN": "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("standalone up -d", self.log())

    def test_one_update_at_a_time(self):
        (self.inst / ".update.lock").mkdir()
        r = self.run_update("-y")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Another update is running", r.stderr)
        self.assertTrue((self.inst / ".update.lock").is_dir())        # someone else's lock stays
        self.assertEqual(self.running(), "old")

    def test_missing_env_is_created(self):
        (self.inst / ".env").unlink()
        r = self.run_update("-y")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.env()["ASSETLOTSE_TAG"], "latest")
        self.assertEqual(os.stat(self.inst / ".env").st_mode & 0o077, 0)

    def test_offline(self):
        img = self.tmp / "assetlotse-2.1.0-amd64.tar.gz"
        img.write_text(f"{IMAGE}:2.1.0 v21\n")
        (self.tmp / "assetlotse-2.1.0-amd64.tar.gz.sha256").write_text("0" * 64 + "  x\n")
        (self.state / "registry").rename(self.state / "registry-gone")   # no internet
        r = self.run_update("-y", "--offline", str(img))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Checksum does not match", r.stderr)
        self.assertEqual(self.running(), "old")
        import hashlib
        (self.tmp / "assetlotse-2.1.0-amd64.tar.gz.sha256").write_text(
            hashlib.sha256(img.read_bytes()).hexdigest() + "  assetlotse-2.1.0-amd64.tar.gz\n")
        r = self.run_update("-y", "--offline", str(img))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.running(), "v21")
        self.assertEqual(self.env()["ASSETLOTSE_TAG"], "2.1.0")
        self.assertNotIn("docker pull", self.log())

    def test_handover_from_the_update_sh_before_2_0(self):
        # the old script pulled the image, put the files from the image here and calls the new update.sh so:
        (self.state / "tags" / f"{IMAGE}:2.0.0".replace("/", "_")).write_text("new\n")
        (self.inst / "docker-compose.yml").write_text((ROOT / "docker-compose.yml").read_text())
        for f in ("Dockerfile", "wsgi.py"):
            (self.inst / f).write_text("x")
        r = self.run_update("-y", "--pulled", f"{IMAGE}:2.0.0", "--continue-after-swap", "1.12.0", "")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.running(), "new")
        self.assertEqual(self.env()["ASSETLOTSE_TAG"], "latest")
        self.assertIn("./update.sh --cleanup", r.stdout)
        self.assertNotIn("docker pull", self.log())
        r = self.run_update("-y", "--pulled", f"{IMAGE}:2.0.0", "--pinned", "--continue-after-swap", "1.12.0", "")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.env()["ASSETLOTSE_TAG"], "2.0.0")

    def test_clean_install_gets_no_cleanup_hint(self):
        r = self.run_update("-y")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("--cleanup", r.stdout)                 # its own temporary folder does not count
        self.assertEqual([p.name for p in self.inst.iterdir() if p.name.startswith(".update")], [])

    def test_offline_file_relative_to_where_it_is_called(self):
        (self.tmp / "dl").mkdir()
        (self.tmp / "dl" / "img.tar.gz").write_text(f"{IMAGE}:2.1.0 v21\n")
        e = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_STATE=str(self.state),
                 ASSETLOTSE_HEALTH_TIMEOUT="3")
        r = subprocess.run([SH, str(self.inst / "update.sh"), "-y", "--offline", "img.tar.gz"], cwd=self.tmp / "dl",
                           env=e, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.running(), "v21")

    def test_previous_is_only_moved_by_a_real_change(self):
        prev = self.state / "tags" / f"{IMAGE}:previous".replace("/", "_")
        self.assertEqual(self.run_update("-y").returncode, 0)
        self.assertEqual(prev.read_text().strip(), "old")
        r = self.run_update("-y")                                # nothing new: previous stays the old version
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(prev.read_text().strip(), "old")
        r = self.run_update("--rollback")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.running(), "old")

    def test_going_back_also_takes_the_previous_compose_file(self):
        v2 = (ROOT / "docker-compose.yml").read_text()
        v3 = v2.replace('ASSETLOTSE_COMPOSE_VERSION: "2"', 'ASSETLOTSE_COMPOSE_VERSION: "3"') + "# v3\n"
        (self.inst / "docker-compose.yml").write_text(v2)
        self.image("cur", "2.0.0", compose=v2)
        (self.state / "running").write_text("cur\n")
        self.image("v3img", "3.0.0", compose=v3, health="unhealthy")
        self.publish("v3img", "latest")
        r = self.run_update("-y")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.running(), "cur")
        self.assertEqual((self.inst / "docker-compose.yml").read_text(), v2)
        self.assertEqual((self.inst / "docker-compose.yml.alt").read_text(), v3)

    def test_script_is_plain_sh_and_builds_nothing(self):
        s = (ROOT / "update.sh").read_text()
        self.assertTrue(s.startswith("#!/bin/sh\n"))
        for bashism in ("[[ ", "]] ", "<<<", "${PIPESTATUS", "declare ", "local -a", "${!", "mapfile", "<(", ",,}"):
            self.assertNotIn(bashism, s)
        for gone in ("unzip", "compose build", "docker build", ".zip\" ", "pip install"):
            self.assertNotIn(gone, s)
        subprocess.run([SH, "-n", str(ROOT / "update.sh")], check=True)


class CleanupTests(FakeDocker):
    """Cleanup of older installations: fixed list, asks first, never data – not through links or mounts."""

    def setUp(self):
        super().setUp()
        i = self.inst
        for d in ("assetlotse", "build", "tests", "pakete"):
            (i / d).mkdir()
            (i / d / "file.py").write_text("x")
        for f in ("Dockerfile", "wsgi.py", "requirements.txt", "README.md", "CHANGELOG.md", "assetlotse-1.9.0.zip",
                  "assetlotse-1.2 (1).zip", "assetlotse-1.9.0.zip.sha256"):
            (i / f).write_text("x")
        (i / ".assetlotse-update.abc").mkdir()
        (i / "assetlotse-1.8.0" / "assetlotse").mkdir(parents=True)     # an unpacked package
        (i / "assetlotse-1.8.0" / "assetlotse" / "__init__.py").write_text("x")
        (i / "data" / "backups").mkdir()
        (i / "data" / "backups" / "b.db").write_text("backup")
        (i / "instance").mkdir()                              # data folder of very old versions
        (i / "instance" / "x.db").write_text("db")
        (i / "docker-compose.override.yml").write_text("x")
        (i / ".env.example").write_text("x")
        self.outside = self.tmp / "nas-backup"
        self.outside.mkdir()
        (self.outside / "keep.db").write_text("keep")
        os.symlink(self.outside, i / "docs")                   # a link to data outside – left alone
        os.symlink(i / "data", i / "scripts")                  # a link to the data folder – left alone
        (i / "packages").mkdir()                               # used as backup folder (bind mount)
        (i / "packages" / "a.db").write_text("backup")
        (self.state / "mounts").write_text(f"{i / 'packages'}\n{i / 'data'}\n")
        (i / "tests" / "link").symlink_to(i / "data")          # a link inside a program folder
        (self.state / "local_images").write_text("assetlotse:1.11.0\nassetlotse:<none>\n")

    def names(self):
        return sorted(p.name for p in self.inst.iterdir())

    def test_lists_and_asks_first(self):
        before = self.names()
        r = self.run_update("--cleanup")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Will be deleted:", r.stdout)
        self.assertIn("assetlotse-1.2 (1).zip", r.stdout)
        self.assertIn("Nothing deleted", r.stdout)
        self.assertEqual(self.names(), before)

    def test_deletes_only_program_files(self):
        r = self.run_update("--cleanup", "-y")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        left = self.names()
        for gone in ("assetlotse", "build", "pakete", "Dockerfile", "wsgi.py", "requirements.txt", "README.md",
                     "CHANGELOG.md", "assetlotse-1.9.0.zip", "assetlotse-1.2 (1).zip", "assetlotse-1.9.0.zip.sha256",
                     ".assetlotse-update.abc", "assetlotse-1.8.0"):
            self.assertNotIn(gone, left)
        for kept in ("data", "instance", ".env", ".env.example", "docker-compose.yml", "docker-compose.override.yml",
                     "update.sh", "docs", "scripts", "packages", "tests"):
            self.assertIn(kept, left)
        self.assertEqual((self.inst / "data" / "assetlotse.db").read_text(), "db")
        self.assertEqual((self.inst / "data" / "backups" / "b.db").read_text(), "backup")
        self.assertEqual((self.outside / "keep.db").read_text(), "keep")
        self.assertEqual((self.inst / "packages" / "a.db").read_text(), "backup")
        self.assertEqual((self.inst / "instance" / "x.db").read_text(), "db")
        self.assertIn("docs  (link", r.stdout)
        self.assertIn("packages  (protected", r.stdout)
        self.assertIn("tests  (contains links", r.stdout)
        self.assertEqual((self.state / "removed_images").read_text(), "assetlotse:1.11.0\n")

    def test_a_mount_of_a_folder_above_does_not_block_everything(self):
        # e.g. a container manager with its stacks folder, or a monitoring agent with "/"
        (self.state / "mounts").write_text(f"/\n{self.tmp}\n{self.inst}\n{self.inst / 'data'}\n")
        r = self.run_update("--cleanup", "-y")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.inst / "assetlotse").exists())
        self.assertFalse((self.inst / "packages").exists())          # no longer mounted here
        self.assertEqual((self.inst / "data" / "assetlotse.db").read_text(), "db")

    def test_only_real_unpacked_packages_without_data(self):
        own = self.inst / "assetlotse-1.11-backup"                   # made by hand, holds a database
        (own / "assetlotse").mkdir(parents=True)
        (own / "assetlotse" / "__init__.py").write_text("x")
        (own / "assetlotse.db").write_text("db")
        other = self.inst / "assetlotse-2-notes"                     # not a package
        other.mkdir()
        r = self.run_update("--cleanup", "-y")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((own / "assetlotse.db").exists())
        self.assertTrue(other.exists())
        self.assertFalse((self.inst / "assetlotse-1.8.0").exists())

    def test_backup_dir_from_env_is_protected(self):
        (self.state / "mounts").write_text("")
        (self.inst / ".env").write_text(f"ASSETLOTSE_BACKUP_DIR={self.inst / 'build'}\n")
        r = self.run_update("--cleanup", "-y")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.inst / "build" / "file.py").exists())

    def test_data_folder_reached_through_a_link_from_a_program_entry(self):
        # "assetlotse" is a real folder that contains the data folder (e.g. data moved there by hand)
        shutil.rmtree(self.inst / "data")
        (self.inst / "assetlotse" / "data").mkdir()
        (self.inst / "assetlotse" / "data" / "assetlotse.db").write_text("db")
        os.symlink(self.inst / "assetlotse" / "data", self.inst / "data")
        (self.state / "mounts").write_text("")
        r = self.run_update("--cleanup", "-y")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.inst / "assetlotse" / "data" / "assetlotse.db").read_text(), "db")

    def test_never_in_a_source_checkout(self):
        (self.inst / ".git").mkdir()
        r = self.run_update("--cleanup", "-y")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((self.inst / "assetlotse").exists())


# ---------------------------------------------------------------- the program
class AppBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="al-app-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.backups = self.tmp / "backups"
        p = mock.patch.dict(os.environ, {"ASSETLOTSE_BACKUP_DIR": str(self.backups), "ASSETLOTSE_SCHEDULER": "0"})
        p.start()
        self.addCleanup(p.stop)
        self.db = self.tmp / "a.db"

    def app(self, **env):
        with mock.patch.dict(os.environ, env):
            return create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{self.db}"})

    def set_version(self, app, v):
        with app.app_context():
            if not User.query.first():
                db.session.add(User(username="admin", role="admin"))
            set_setting("app_version", v)
            db.session.commit()

    def files(self):
        return sorted(p.name for p in self.backups.glob("*.db")) if self.backups.exists() else []


class DatabaseVersionTests(AppBase):
    def test_backup_named_after_the_new_version(self):
        self.set_version(self.app(), "1.12.0")
        self.app()
        files = self.files()
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith(f"-vor-{assetlotse.__version__}.db"), files)

    def test_older_program_refuses_a_newer_database_and_names_the_backup(self):
        app = self.app()
        self.set_version(app, "1.12.0")
        self.app()                                       # update to this version: backup vor-<this version>
        backup = self.files()[0]
        self.set_version(app, "9.0.0")                   # as if a newer version ran afterwards
        # this program is older than the database now
        with self.assertRaises(SystemExit) as cm:
            self.app()
        msg = str(cm.exception.code)
        self.assertIn("newer version 9.0.0", msg)
        self.assertIn("docker compose run --rm assetlotse backups", msg)   # no backup "vor-9.0.0" here
        self.assertIn("nothing was changed", msg)
        with app.app_context():
            self.assertEqual(get_setting("app_version"), "9.0.0")      # untouched
        self.assertEqual(self.files(), [backup])                       # no new backup either

    def test_message_names_the_backup_before_that_version(self):
        app = self.app()
        self.set_version(app, "1.12.0")
        with mock.patch.object(assetlotse, "__version__", "9.0.0"):
            self.app()                                   # "9.0.0" starts: backup …-vor-9.0.0.db
        name = self.files()[0]
        self.assertIn("-vor-9.0.0", name)
        with self.assertRaises(SystemExit) as cm:
            self.app()                                   # back with the older program
        self.assertIn(f"docker compose run --rm assetlotse restore {name}", str(cm.exception.code))

    def test_restore_while_the_start_is_refused(self):
        app = self.app()
        self.set_version(app, "1.12.0")
        with mock.patch.object(assetlotse, "__version__", "9.0.0"):
            self.app()
        name = self.files()[0]
        maint = self.app(ASSETLOTSE_MAINTENANCE="1")        # what "docker compose run … restore" uses
        r = maint.test_cli_runner().invoke(args=["restore-backup", name, "--yes"])
        self.assertEqual(r.exit_code, 0, r.output)
        app = self.app()                                     # starts again – on the state before the update
        with app.app_context():
            self.assertEqual(get_setting("app_version"), assetlotse.__version__)
            self.assertEqual(User.query.count(), 1)

    def test_maintenance_mode_changes_nothing(self):
        app = self.app()
        self.set_version(app, "9.0.0")
        self.app(ASSETLOTSE_MAINTENANCE="1")
        with app.app_context():
            self.assertEqual(get_setting("app_version"), "9.0.0")
        self.assertEqual(self.files(), [])

    def test_wsgi_exits_with_code_4(self):
        data = self.tmp / "data"
        env = dict(os.environ, ASSETLOTSE_DATA_DIR=str(data), ASSETLOTSE_SCHEDULER="0", PYTHONPATH=str(ROOT))
        env.pop("DATABASE_URL", None)
        r = subprocess.run([sys.executable, "-c", "import wsgi"], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        import sqlite3
        con = sqlite3.connect(data / "assetlotse.db")
        con.execute("UPDATE settings SET value='99.0.0' WHERE key='app_version'")
        con.commit()
        con.close()
        r = subprocess.run([sys.executable, "-c", "import wsgi"], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("newer version 99.0.0", r.stderr)

    def test_upgrade_backups_survive_rotation(self):
        from assetlotse import backup
        app = self.app()
        self.set_version(app, "1.0.0")
        with app.app_context():
            self.backups.mkdir(exist_ok=True)
            names = ["assetlotse-2026-01-0%d_020000-vor-1.%d.0.db" % (d, d) for d in range(1, 6)]
            names += ["assetlotse-2026-02-0%d_020000.db" % d for d in range(1, 6)]
            for n in names:
                (self.backups / n).write_text("x")
            backup.rotate(keep=2)
            left = self.files()
            self.assertEqual(sorted(n for n in left if "-vor-" in n), sorted(names[2:5]))   # newest three
            self.assertEqual(sorted(n for n in left if "-vor-" not in n), sorted(names[8:10]))
            kinds = {b["name"]: b["kind"] for b in backup.list_backups()}
            self.assertEqual(kinds[names[4]], "before update")


class ComposeVersionTests(AppBase):
    def test_outdated_only_in_the_container(self):
        from assetlotse import compose_outdated, COMPOSE_VERSION
        with mock.patch.dict(os.environ, {"ASSETLOTSE_CONTAINER": ""}):
            self.assertEqual(compose_outdated(), "")
        with mock.patch.dict(os.environ, {"ASSETLOTSE_CONTAINER": "1"}):
            os.environ.pop("ASSETLOTSE_COMPOSE_VERSION", None)
            self.assertEqual(compose_outdated(), "1")
            os.environ["ASSETLOTSE_COMPOSE_VERSION"] = str(COMPOSE_VERSION)
            self.assertEqual(compose_outdated(), "")
            os.environ["ASSETLOTSE_COMPOSE_VERSION"] = "1"
            self.assertEqual(compose_outdated(), "1")
            del os.environ["ASSETLOTSE_COMPOSE_VERSION"]

    def test_shipped_file_has_the_expected_version(self):
        from assetlotse import COMPOSE_VERSION
        text = (ROOT / "docker-compose.yml").read_text()
        self.assertIn(f'ASSETLOTSE_COMPOSE_VERSION: "{COMPOSE_VERSION}"', text)
        self.assertNotIn("build:", text)
        self.assertIn("${ASSETLOTSE_TAG:-latest}", text)

    def test_admins_see_the_notice(self):
        app = self.app(ASSETLOTSE_CONTAINER="1", ASSETLOTSE_COMPOSE_VERSION="1")
        with app.app_context():
            a = User(username="admin", role="admin", display_name="A")
            a.set_password("Passwort-123456")
            r = User(username="leser", role="reader", display_name="L")
            r.set_password("Passwort-123456")
            db.session.add_all([a, r])
            set_setting("setup_completed", "1")
            db.session.commit()
        app.config["WTF_CSRF_ENABLED"] = False
        for user, shown in (("admin", True), ("leser", False)):
            c = app.test_client()
            c.post("/login", data={"username": user, "password": "Passwort-123456"})
            html = c.get("/", follow_redirects=True).get_data(as_text=True)
            self.assertEqual("docker-compose.yml auf dem Server ist veraltet" in html, shown, user)


class EntrypointTests(unittest.TestCase):
    def test_maintenance_commands(self):
        s = (ROOT / "docker-entrypoint.sh").read_text()
        block = s[s.index('case "${1:-}" in'):s.index("esac")]
        self.assertIn("restore) shift; export ASSETLOTSE_MAINTENANCE=1", block)
        self.assertIn("flask --app wsgi restore-backup", block)
        self.assertIn("flask --app wsgi list-backups", block)
        self.assertLess(s.index('case "${1:-}" in'), s.index("exec setpriv"))   # also without root rights

    def test_image_defaults_and_install_files(self):
        df = (ROOT / "Dockerfile").read_text()
        self.assertIn("ASSETLOTSE_CONTAINER=1", df)
        self.assertIn("TZ=Europe/Berlin", df)
        # what the update.sh of installations before 2.0.0 checks before it hands over (one-time switch)
        layer = df.split("COPY docker-compose.yml")[1].split("\n\n")[0]
        for f in ("update.sh", "Dockerfile", "docker-entrypoint.sh", "requirements.txt", ".env.example"):
            self.assertIn(f, layer)
        self.assertIn("COPY wsgi.py", df)


if __name__ == "__main__":
    unittest.main()
