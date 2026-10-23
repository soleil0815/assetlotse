"""Sicherung beim ersten Start einer neuen Version (Update ohne update.sh, z. B. durch ein Update-Tool)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ASSETLOTSE_DATA_DIR", tempfile.mkdtemp(prefix="assetlotse-test-"))

import assetlotse  # noqa: E402
from assetlotse import create_app  # noqa: E402
from assetlotse.models import User, db, get_setting, set_setting  # noqa: E402


class UpgradeBackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cx-upg-"))
        self.backups = self.tmp / "backups"
        p = mock.patch.dict(os.environ, {"ASSETLOTSE_BACKUP_DIR": str(self.backups), "ASSETLOTSE_SCHEDULER": "0"})
        p.start()
        self.addCleanup(p.stop)

    def app(self):
        return create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{self.tmp / 'a.db'}"})

    def files(self):
        return sorted(p.name for p in self.backups.glob("*.db")) if self.backups.exists() else []

    def test_backup_once_per_new_version(self):
        app = self.app()                                   # neue Datenbank: nichts zu sichern
        with app.app_context():
            self.assertEqual(get_setting("app_version"), assetlotse.__version__)
            db.session.add(User(username="admin", role="admin"))
            db.session.commit()
        self.assertEqual(self.files(), [])
        self.app()                                         # gleiche Version: keine Sicherung
        self.assertEqual(self.files(), [])
        with app.app_context():
            set_setting("app_version", "1.0.0")            # so, als liefe vorher 1.0.0
            db.session.commit()
        with self.assertLogs("assetlotse", "INFO") as cm:
            app2 = self.app()
        files = self.files()
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith("-vor-update.db"), files)
        self.assertTrue(any("Update von 1.0.0" in m for m in cm.output))
        with app2.app_context():
            self.assertEqual(get_setting("app_version"), assetlotse.__version__)
        self.app()
        self.assertEqual(len(self.files()), 1)             # nur beim ersten Start der neuen Version

    def test_existing_database_from_before_version_tracking(self):
        app = self.app()
        with app.app_context():
            db.session.add(User(username="admin", role="admin"))
            set_setting("app_version", "")                 # Datenbank aus einer Version ohne Eintrag
            db.session.commit()
        self.app()
        self.assertEqual(len(self.files()), 1)

    def test_backup_name_is_listed(self):
        app = self.app()
        with app.app_context():
            db.session.add(User(username="admin", role="admin"))
            set_setting("app_version", "1.0.0")
            db.session.commit()
        app = self.app()
        with app.app_context():
            from assetlotse.backup import list_backups
            self.assertEqual(len(list_backups()), 1)


if __name__ == "__main__":
    unittest.main()
