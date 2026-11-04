"""Code audit 2026-11 (2.0.2): regression tests for the confirmed findings.

- notice “docker-compose.yml outdated” shows the numbers instead of %(found)s/%(want)s
- message for a too-new database names exactly the backup “-vor-<version>” (not -vor-2.0.10 for 2.0.1)
- update.sh --cleanup: skips mount points, also protects the mounts from docker-compose(.override).yml without
  a container and aborts if the compose configuration cannot be read
- update.sh: a stopped container counts as the previous version (previous, output, going back)
"""
import json
import unittest

import test_docker_standard as std

import assetlotse
from assetlotse.models import User, db, set_setting


class ComposeNoticeTests(std.AppBase):
    def test_notice_shows_the_numbers(self):
        app = self.app(ASSETLOTSE_CONTAINER="1", ASSETLOTSE_COMPOSE_VERSION="1")
        with app.app_context():
            a = User(username="admin", role="admin", display_name="A")
            a.set_password("Passwort-123456")
            db.session.add(a)
            set_setting("setup_completed", "1")
            db.session.commit()
        app.config["WTF_CSRF_ENABLED"] = False
        c = app.test_client()
        c.post("/login", data={"username": "admin", "password": "Passwort-123456"})
        html = c.get("/", follow_redirects=True).get_data(as_text=True)
        self.assertIn(f"(Stand 1, erwartet {assetlotse.COMPOSE_VERSION})", html)
        self.assertNotIn("%(", html)

    def test_translation_uses_the_same_placeholders(self):
        de = json.loads((std.ROOT / "assetlotse" / "translations" / "de.json").read_text())
        key = next(k for k in de if k.startswith("The docker-compose.yml on the server is out of date"))
        for text in (key, de[key]):
            self.assertIn("{found}", text)
            self.assertIn("{want}", text)


class TooNewMessageTests(std.AppBase):
    def test_names_exactly_the_backup_before_that_version(self):
        app = self.app()
        self.backups.mkdir(exist_ok=True)
        right = "assetlotse-2026-01-01_000000-vor-2.0.1.db"
        for name in (right, "assetlotse-2099-01-01_000000-vor-2.0.10.db", "assetlotse-2098-01-01_000000-vor-12.0.1.db"):
            (self.backups / name).write_bytes(b"SQLite format 3\x00")
        with app.app_context():
            msg = assetlotse._too_new_message("2.0.1")
        self.assertIn(f"restore {right}", msg)
        self.assertNotIn("2.0.10", msg)


class CleanupMountTests(std.CleanupTests):
    def test_mount_points_are_left_alone(self):
        nas = self.inst / "assetlotse" / "nas"       # a share mounted below a program folder
        nas.mkdir()
        (nas / "wichtig.txt").write_text("keep")
        mi = self.tmp / "mountinfo"
        enc = lambda p: str(p).replace(" ", "\\040")  # noqa: E731 – as the kernel writes it
        mi.write_text(f"22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
                      f"90 22 0:50 / {enc(self.inst / 'build')} rw - nfs nas:/x rw\n"
                      f"91 22 0:51 / {enc(nas)} rw - cifs //nas/y rw\n")
        r = self.run_update("--cleanup", "-y", env={"ASSETLOTSE_MOUNTINFO": str(mi)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((nas / "wichtig.txt").read_text(), "keep")
        self.assertTrue((self.inst / "build" / "file.py").exists())
        self.assertIn("assetlotse  (is or contains a mount point", r.stdout)
        self.assertIn("build  (is or contains a mount point", r.stdout)
        self.assertFalse((self.inst / "pakete").exists())             # the rest is cleaned up as before

    def test_mounts_from_the_compose_files_without_a_container(self):
        (self.state / "running").unlink()                              # after "docker compose down"
        (self.state / "mounts").write_text("")
        (self.state / "config").write_text(
            "services:\n  assetlotse:\n    volumes:\n"
            f"      - type: bind\n        source: {self.inst / 'packages'}\n        target: /backup\n"
            f"      - {self.inst / 'pakete'}:/old:rw\n"
            "      - type: volume\n        source: assetlotse_x\n        target: /x\n")
        r = self.run_update("--cleanup", "-y")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.inst / "packages" / "a.db").read_text(), "backup")
        self.assertTrue((self.inst / "pakete" / "file.py").exists())
        self.assertIn("packages  (protected", r.stdout)
        self.assertIn("pakete  (protected", r.stdout)
        self.assertFalse((self.inst / "assetlotse").exists())

    def test_unreadable_compose_configuration_deletes_nothing(self):
        (self.state / "config_fails").write_text("")
        before = self.names()
        r = self.run_update("--cleanup", "-y")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing deleted", r.stderr)
        self.assertEqual(self.names(), before)


class StoppedContainerTests(std.FakeDocker):
    def test_stopped_container_is_the_previous_version(self):
        (self.state / "running").rename(self.state / "stopped")
        r = self.run_update("-y")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.running(), "new")
        self.assertEqual((self.state / "tags" / f"{std.IMAGE}:previous".replace("/", "_")).read_text().strip(), "old")
        self.assertIn("1.12.0-s1 → 2.0.0", r.stdout)

    def test_one_off_container_is_not_the_previous_version(self):
        # "docker compose run" without --rm left a container of an older image (listed first by "ps -a")
        (self.state / "running").rename(self.state / "stopped")
        (self.state / "oneoff").write_text("v21\n")
        r = self.run_update("-y")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.state / "tags" / f"{std.IMAGE}:previous".replace("/", "_")).read_text().strip(), "old")
        self.assertIn("1.12.0-s1 → 2.0.0", r.stdout)


class ComposeSourcesTests(std.FakeDocker):
    def test_long_syntax_of_docker_compose_v1(self):
        # docker-compose 1.x writes the long form with sorted keys: "- source: …" in the list item itself
        i = self.inst
        for d in ("packages", "build"):
            (i / d).mkdir()
            (i / d / "a.db").write_text("backup")
        (self.state / "running").unlink()
        (self.state / "config").write_text(
            "services:\n  assetlotse:\n    volumes:\n"
            f"    - source: {i / 'packages'}\n      target: /p\n      type: bind\n"
            f"    - \"{i / 'build'}:/b:rw\"\n")
        r = self.run_update("--cleanup", "-y")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((i / "packages" / "a.db").read_text(), "backup")
        self.assertEqual((i / "build" / "a.db").read_text(), "backup")
        self.assertIn("packages  (protected", r.stdout)
        self.assertIn("build  (protected", r.stdout)


if __name__ == "__main__":
    unittest.main()
