"""Typprüfung mit mypy (schrittweise, siehe mypy.ini) und was sie gefunden hat.

- Die Module in den strengen Abschnitten von mypy.ini müssen fehlerfrei sein.
- Alle anderen Funde werden nur aufgelistet (kein Fehler) – sie werden Modul für Modul abgebaut.
Ohne installiertes mypy wird der Test übersprungen; im Prüflauf auf GitHub ist mypy Pflicht
(ASSETLOTSE_REQUIRE_MYPY=1).
"""
import configparser
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def strict_modules() -> list[str]:
    cp = configparser.ConfigParser()
    cp.read(ROOT / "mypy.ini")
    mods = []
    for section in cp.sections():
        if section.startswith("mypy-") and cp[section].get("disallow_untyped_defs") == "True":
            mods += [m.strip() for m in section[len("mypy-"):].split(",")]
    return mods


def module_of(path: str) -> str:
    p = Path(path)
    if p.parts[0] == "scripts":
        return p.stem                                   # scripts/ has no package: "ci", "lock" …
    return ".".join(p.with_suffix("").parts).removesuffix(".__init__")


class TypeCheckTest(unittest.TestCase):
    def test_strict_modules_have_no_findings(self):
        try:
            from mypy import api
        except ImportError:
            if os.environ.get("ASSETLOTSE_REQUIRE_MYPY") == "1":
                self.fail("mypy is required here (tests/requirements-typing.txt)")
            self.skipTest("mypy not installed")
        out, err, _status = api.run(["--config-file", str(ROOT / "mypy.ini"), "--no-error-summary",
                                     "--cache-dir", os.devnull, "--no-incremental"]
                                    if os.name != "nt" else ["--config-file", str(ROOT / "mypy.ini")])
        self.assertNotIn("error: ", err, err)                        # mypy itself must run
        strict = set(strict_modules())
        self.assertGreaterEqual(len(strict), 7)
        findings = [l for l in out.splitlines() if re.match(r"^\S+\.py:\d+: error:", l)]
        bad = [l for l in findings if module_of(l.split(":", 1)[0]) in strict]
        self.assertEqual(bad, [], "\n" + "\n".join(bad))
        # the rest is only listed (shown with -s / in the log of the check run)
        print(f"\nmypy: {len(findings)} finding(s) in modules that are not strict yet")

    def test_config_is_consistent(self):
        text = (ROOT / "mypy.ini").read_text()
        self.assertIn("python_version = 3.13", text)
        for mod in strict_modules():
            path = ROOT / "scripts" / f"{mod}.py" if "." not in mod else ROOT / (mod.replace(".", "/") + ".py")
            self.assertTrue(path.exists(), mod)
        req = (ROOT / "tests" / "requirements-typing.txt").read_text()
        for line in req.splitlines():
            if line and not line.startswith("#"):
                self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[\w.]+$")
        self.assertIn("mypy==", req)


class FoundByTypeCheckTest(unittest.TestCase):
    """Fehler, die mypy gefunden hat – damit sie nicht wiederkommen."""

    def test_refused_bind_gives_a_readable_message(self):
        # "msg=" collided with the first parameter of _(): a wrong service password ended in a TypeError (500)
        from test_app import BaseCase  # noqa: F401 – sets up the test environment
        from assetlotse import create_app
        from assetlotse.ldap_auth import Directory, LDAPError
        app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://"})
        conn = mock.MagicMock()
        conn.bind.return_value = False
        conn.result = {"description": "invalidCredentials", "message": "80090308: LdapErr: DSID-0C09042A"}
        with app.test_request_context("/"), mock.patch("ldap3.Connection", return_value=conn), \
                mock.patch.object(Directory, "_server_pool", return_value=object()):
            d = Directory.__new__(Directory)
            d.timeout, d.security = 5, "ldaps"
            with self.assertRaises(LDAPError) as cm:
                d.connect("CN=svc", "falsch")
        self.assertIn("invalidCredentials", str(cm.exception))
        self.assertIn("DSID-0C09042A", str(cm.exception))
        conn.unbind.assert_called_once()


if __name__ == "__main__":
    unittest.main()
