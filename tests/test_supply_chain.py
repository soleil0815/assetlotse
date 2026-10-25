"""Build and delivery chain: locked versions and their limits (scripts/lock.py), decisions of the workflow
(scripts/ci.py), base check (scripts/eol_check.py), Trivy flow with a stand-in Trivy, and the workflow
itself (.github/workflows/docker.yml): schedule, order of the steps, permissions, pinning."""
import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ci          # noqa: E402
import eol_check   # noqa: E402
import lock        # noqa: E402

WF = (ROOT / ".github" / "workflows" / "docker.yml").read_text()
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def jobs():
    body = "\n" + WF.split("\njobs:\n", 1)[1]
    parts = re.split(r"\n  ([a-z-]+):\n", body)
    return dict(zip(parts[1::2], parts[2::2]))


def job_permissions(text):
    head = text.split("    steps:")[0]
    m = re.search(r"    permissions:\n((?:      .*\n)+)", head + "\n")
    return m.group(1) if m else None


# ====================================================================== lock.py: limits
class VersionLimitTests(unittest.TestCase):
    def test_upper_bound(self):
        self.assertEqual(lock.upper_bound("3.1.3"), "4")              # same major version
        self.assertEqual(lock.upper_bound("50.0.2"), "51")
        self.assertEqual(lock.upper_bound("0.6.3"), "0.7")            # before 1.0: same minor version
        self.assertEqual(lock.upper_bound("0.10"), "0.11")
        self.assertIsNone(lock.upper_bound("2026.10.23"))             # calendar version: no limit
        self.assertIsNone(lock.upper_bound("2025.2"))

    def test_constraint_and_within(self):
        self.assertEqual(lock.constraint("Flask", "3.1.3"), "Flask>=3.1.3,<4")
        self.assertEqual(lock.constraint("certifi", "2026.10.23"), "certifi>=2026.10.23")
        self.assertTrue(lock.within_limits("3.1.3", "3.9.0"))
        self.assertFalse(lock.within_limits("3.1.3", "4.0.0"))         # never the next major version
        self.assertFalse(lock.within_limits("3.1.3", "3.1.2"))         # never lower
        self.assertFalse(lock.within_limits("0.6.3", "0.7.0"))
        self.assertTrue(lock.within_limits("2026.10.19", "2027.1.1"))

    def test_base_tag_stays_in_line_and_release(self):
        tags = ["3.13.17-slim-trixie", "3.13.18-slim-bookworm", "3.13.19-alpine", "3.14.1-slim-trixie",
                "3.13.18-slim", "3.13.17-trixie", "3.13.9-slim-trixie", "3.13.20-windowsservercore",
                "3.13-slim-trixie", "3.13.17rc1-slim-trixie"]
        self.assertEqual(lock.pick_base_tag(tags, 3, 13, "trixie", 16), 17)
        self.assertEqual(lock.pick_base_tag(tags, 3, 13, "trixie", 18), 18)    # never lower than today
        self.assertEqual(lock.pick_base_tag([], 3, 13, "trixie", 16), 16)


# ====================================================================== lock.py: files
REQS = textwrap.dedent("""\
    # header – versions on Python 3.13.16.
    # Web framework
    Flask==3.1.3
    Werkzeug==3.1.9
    colorama==0.4.6 ; platform_system == "Windows"     # needed by click on Windows
    gone==1.0
    gunicorn==26.2.0 ; sys_platform != "win32"
    waitress==3.0.2 ; sys_platform == "win32"
    """)


class LockFileTests(unittest.TestCase):
    def test_pins_skip_windows_only(self):
        pins = lock.read_pins(REQS)
        self.assertIn("gunicorn", pins)
        self.assertNotIn("colorama", pins)
        self.assertNotIn("waitress", pins)

    def test_render_keeps_layout(self):
        new = {"flask": ("Flask", "3.2.0"), "werkzeug": ("Werkzeug", "3.1.9"),
               "gunicorn": ("gunicorn", "26.3.1"), "blinker": ("blinker", "1.9.0")}
        out = lock.render_requirements(REQS, new, "3.13.17")
        self.assertIn("versions on Python 3.13.17.", out)
        self.assertIn("# Web framework\nFlask==3.2.0\n", out)
        self.assertIn('gunicorn==26.3.1 ; sys_platform != "win32"', out)
        self.assertIn('colorama==0.4.6 ; platform_system == "Windows"', out)     # Windows lines stay
        self.assertIn('waitress==3.0.2 ; sys_platform == "win32"', out)
        self.assertNotIn("gone==", out)                                          # no longer needed
        self.assertTrue(out.rstrip().endswith("# Further sub-dependencies (added by scripts/lock.py)\nblinker==1.9.0"))

    def test_os_lock_roundtrip(self):
        text = lock.render_os_lock("20261026T041700Z", "13 trixie", {"zlib1g": "1:1.3", "bash": "5.2", "Z": "1"})
        snap, pkgs, debian = lock.read_os_lock(text)
        self.assertEqual((snap, debian), ("20261026T041700Z", "13 trixie"))
        self.assertEqual(pkgs, {"zlib1g": "1:1.3", "bash": "5.2", "Z": "1"})
        body = [l.split()[0] for l in text.splitlines() if not l.startswith("#")]
        self.assertEqual(body, ["Z", "bash", "zlib1g"])                          # like LC_ALL=C sort

    def test_repository_is_consistent(self):
        self.assertEqual(lock.check(ROOT), [])


# ====================================================================== lock.py: refresh with stand-ins
class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "build").mkdir()
        (self.tmp / "Dockerfile").write_text(
            f"ARG PYTHON_IMAGE=python:3.13.16-slim-trixie@sha256:{DIGEST_A}\nFROM ${{PYTHON_IMAGE}}\n"
            "ARG PIP_VERSION=26.2.2\n")
        (self.tmp / lock.OS_LOCK).write_text(
            lock.render_os_lock("20261019T000000Z", "13 trixie", {"bash": "5.2.37-2", "libssl3t64": "3.5.1-1"}))
        (self.tmp / "requirements.txt").write_text("# on Python 3.13.16.\nFlask==3.1.3\ncertifi==2026.10.19\n")
        (self.tmp / "requirements.in").write_text("Flask>=3.1,<4\n")
        self.result = {"python": "3.13.16", "pip": "26.2.2", "debian": "13 trixie",
                       "packages": {"bash": "5.2.37-2", "libssl3t64": "3.5.1-1"},
                       "pins": {"flask": ("Flask", "3.1.3"), "certifi": ("certifi", "2026.10.19")}}
        self.patch = 16
        self.calls = []
        orig = (lock.hub_tags, lock.digest_of, lock.resolve)
        self.addCleanup(lambda: setattr(lock, "hub_tags", orig[0]) or setattr(lock, "digest_of", orig[1])
                        or setattr(lock, "resolve", orig[2]))
        lock.hub_tags = lambda major, minor: [f"{major}.{minor}.{self.patch}-slim-trixie"]
        lock.digest_of = lambda ref: DIGEST_B                      # the registry always has a new digest
        lock.resolve = lambda image, snap, state, root: self.calls.append((image, snap)) or json.loads(json.dumps(self.result))

    def files(self):
        return {p: (self.tmp / p).read_text() for p in ("Dockerfile", lock.OS_LOCK, "requirements.txt")}

    def test_nothing_new_changes_nothing(self):
        before = self.files()
        self.assertEqual(lock.refresh(self.tmp, str(self.tmp / "r.json")), [])
        self.assertEqual(self.files(), before)          # new snapshot or digest alone is not a change
        self.assertFalse(json.loads((self.tmp / "r.json").read_text())["changed"])
        self.assertTrue(self.calls[0][0].startswith("python:3.13.16-slim-trixie@sha256:"))

    def test_new_debian_packages(self):
        self.result["packages"] = {"bash": "5.2.37-2", "libssl3t64": "3.5.1-1+deb13u1", "newpkg": "1"}
        changes = lock.refresh(self.tmp)
        self.assertEqual([(c["name"], c["old"], c["new"]) for c in changes],
                         [("libssl3t64", "3.5.1-1", "3.5.1-1+deb13u1"), ("newpkg", None, "1")])
        snap, pkgs, _ = lock.read_os_lock((self.tmp / lock.OS_LOCK).read_text())
        self.assertEqual(snap, self.calls[0][1])
        self.assertIn(f"python:3.13.16-slim-trixie@sha256:{DIGEST_B}", (self.tmp / "Dockerfile").read_text())

    def test_new_python_patch_and_packages(self):
        self.patch = 17
        self.result.update(python="3.13.17", pip="26.3")
        self.result["pins"] = {"flask": ("Flask", "3.2.0"), "certifi": ("certifi", "2026.10.23")}
        kinds = {(c["kind"], c["name"]): (c["old"], c["new"]) for c in lock.refresh(self.tmp)}
        self.assertEqual(kinds[("base", "python")], ("3.13.16", "3.13.17"))
        self.assertEqual(kinds[("python", "flask")], ("3.1.3", "3.2.0"))
        self.assertEqual(kinds[("pip", "pip")], ("26.2.2", "26.3"))
        df = (self.tmp / "Dockerfile").read_text()
        self.assertIn(f"python:3.13.17-slim-trixie@sha256:{DIGEST_B}", df)
        self.assertIn("ARG PIP_VERSION=26.3", df)
        self.assertIn("Flask==3.2.0", (self.tmp / "requirements.txt").read_text())
        self.assertIn("on Python 3.13.17.", (self.tmp / "requirements.txt").read_text())

    def test_limits_stop_the_refresh(self):
        before = self.files()
        for change in ({"pins": {"flask": ("Flask", "4.0.0"), "certifi": ("certifi", "2026.10.19")}},
                       {"pins": {"flask": ("Flask", "3.1.2"), "certifi": ("certifi", "2026.10.19")}},
                       {"python": "3.14.0"}, {"debian": "14 forky"}, {"pip": "27.0"}):
            with self.subTest(change=change):
                saved = json.loads(json.dumps(self.result))
                self.result.update(change)
                with self.assertRaises(SystemExit):
                    lock.refresh(self.tmp)
                self.assertEqual(self.files(), before)
                self.result = saved

    def test_constraints_passed_to_the_resolver(self):
        state = lock.read_state(self.tmp)
        lines = sorted(lock.constraint(n, v) for n, v in state["pins"].values())
        self.assertEqual(lines, ["Flask>=3.1.3,<4", "certifi>=2026.10.19"])


# ====================================================================== Dockerfile and build scripts
class DockerfileTests(unittest.TestCase):
    df = (ROOT / "Dockerfile").read_text()

    def test_base_pinned_by_version_and_digest(self):
        m = lock.BASE_RE.search(self.df)
        self.assertIsNotNone(m, "ARG PYTHON_IMAGE=python:X.Y.Z-slim-<codename>@sha256:<digest>")
        self.assertEqual(m["codename"], "trixie")
        self.assertRegex(self.df, r"(?m)^ARG PIP_VERSION=\d+\.\d+(\.\d+)?$")

    def test_os_packages_from_lock(self):
        self.assertIn("COPY build/apt-snapshot.sh build/os-install.sh build/debian-packages.lock /tmp/build/", self.df)
        self.assertIn("RUN sh /tmp/build/os-install.sh", self.df)
        snap = (ROOT / "build" / "apt-snapshot.sh").read_text()
        self.assertIn("https://snapshot.debian.org/archive/debian/${ts}", snap)
        self.assertIn("https://snapshot.debian.org/archive/debian-security/${ts}", snap)
        self.assertIn("full-upgrade", snap)                                   # also the base image's packages
        inst = (ROOT / "build" / "os-install.sh").read_text()
        self.assertIn("diff -u /tmp/locked-packages /tmp/installed-packages", inst)

    def test_python_packages_exact_and_no_package_manager_left(self):
        self.assertIn("pip install --no-deps --only-binary=:all: -r requirements.txt", self.df)
        self.assertIn("pip check", self.df)
        self.assertIn("pip uninstall -y pip", self.df)
        self.assertIn("rm -rf /usr/local/lib/python3.*/ensurepip", self.df)
        self.assertIn('! python -c "import pip"', self.df)
        for gone in ("OS_UPGRADE", "PKG_UPGRADE", "requirements.in;"):
            self.assertNotIn(gone, self.df)

    def test_test_tools_exact(self):
        for line in (ROOT / "tests" / "requirements-test.txt").read_text().splitlines():
            if line and not line.startswith("#"):
                self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[\w.]+$")


# ====================================================================== ci.py
class ModeTests(unittest.TestCase):
    def test_schedule_matches_workflow(self):
        crons = re.findall(r'- cron: "([^"]+)"', WF)
        self.assertEqual(sorted(crons), sorted([ci.WEEKLY_CRON, ci.DAILY_CRON]))
        self.assertEqual(ci.WEEKLY_CRON.split()[-1], "1")                    # Mondays
        self.assertLess(int(ci.WEEKLY_CRON.split()[1]), 7)                     # early in the morning
        self.assertEqual(ci.DAILY_CRON.split()[2:], ["*", "*", "*"])          # every day

    def test_modes(self):
        main = ci.MAIN_REF
        m = ci.mode("push", ref=main)
        self.assertEqual((m["kind"], m["build"], m["publish"]), ("release", True, True))
        self.assertFalse(ci.mode("push", ref="refs/heads/feature")["publish"])     # only from master
        m = ci.mode("schedule", ci.WEEKLY_CRON, ref=main)
        self.assertEqual((m["kind"], m["publish"], m["eol"], m["scan_latest"]), ("security", True, True, False))
        m = ci.mode("schedule", ci.DAILY_CRON, ref=main)
        self.assertEqual((m["build"], m["scan_latest"], m["publish"]), (False, True, False))
        m = ci.mode("workflow_dispatch", input_mode="check", ref=main)
        self.assertEqual((m["kind"], m["build"], m["publish"]), ("check", True, False))
        self.assertTrue(ci.mode("workflow_dispatch", input_mode="security-update", ref=main)["publish"])
        self.assertFalse(ci.mode("workflow_dispatch", input_mode="security-update", ref=main,
                                 input_publish="false")["publish"])               # trial run
        self.assertFalse(ci.mode("workflow_dispatch", input_mode="security-update", ref="refs/heads/x")["publish"])
        self.assertTrue(ci.mode("workflow_dispatch", input_mode="relock", ref=main)["relock"])
        self.assertTrue(ci.mode("workflow_dispatch", input_mode="eol-check", ref=main)["eol"])
        with self.assertRaises(SystemExit):
            ci.mode("schedule", "0 0 * * *", ref=main)


class VersionPlanTests(unittest.TestCase):
    IMG = "ghcr.io/o/r"

    def test_release_latest_only_for_highest(self):
        tags = ["v1.9.4", "v1.9.5", "v1.9.5-s1", "v1.9.5-s7", "v2.0.0", "junk"]
        p = ci.plan_release(self.IMG, "2.1.0", tags)
        self.assertEqual(p["tags"], [f"{self.IMG}:2.1.0", f"{self.IMG}:2.1", f"{self.IMG}:latest"])
        p = ci.plan_release(self.IMG, "1.9.6", tags)                 # fix for an older line
        self.assertEqual(p["tags"], [f"{self.IMG}:1.9.6", f"{self.IMG}:1.9"])
        p = ci.plan_release(self.IMG, "1.8.9", tags)
        self.assertEqual(p["tags"], [f"{self.IMG}:1.8.9", f"{self.IMG}:1.8"])
        self.assertEqual(ci.highest_release(["v1.9.5-s3", "v1.9.5", "1.10.0", "v1.10.0-s1"]), "1.10.0")

    def test_security_numbers(self):
        self.assertEqual(ci.next_security_number("1.9.5", []), 1)
        self.assertEqual(ci.next_security_number("1.9.5", ["v1.9.5-s1", "v1.9.5-s2", "v1.9.4-s9"]), 3)
        self.assertEqual(ci.next_security_number("1.9.5", ["v1.9.5-s1", "1.9.5-s4"]), 5)   # registry counts too
        self.assertEqual(ci.next_security_number("1.10.0", ["v1.9.5-s4"]), 1)               # new version: reset
        self.assertEqual(ci.next_security_number("1.9.5", ["v1.9.50-s3", "v1.9.5-s1x"]), 1)
        p = ci.plan_security(self.IMG, "1.9.5", ["v1.9.4", "v1.9.5", "v1.9.5-s1"], ["1.9.5-s2", "latest"])
        self.assertEqual(p["name"], "1.9.5-s3")
        self.assertEqual(p["tags"], [f"{self.IMG}:1.9.5-s3", f"{self.IMG}:1.9", f"{self.IMG}:latest"])
        with self.assertRaises(SystemExit):                         # only for the highest version
            ci.plan_security(self.IMG, "1.9.4", ["v1.9.4", "v1.9.5"], [])

    def test_changelog_entry(self):
        changes = [{"kind": "base", "name": "python", "old": "3.13.16", "new": "3.13.17"},
                   {"kind": "debian", "name": "libssl3t64", "old": "3.5.1-1", "new": "3.5.1-1+deb13u1"},
                   {"kind": "debian", "name": "newpkg", "old": None, "new": "1.0"},
                   {"kind": "python", "name": "cryptography", "old": "50.0.1", "new": "50.0.2"},
                   {"kind": "python", "name": "oldpkg", "old": "1.0", "new": None}]
        entry = ci.changelog_entry("1.9.5-s2", "1.9.5", "2026-10-26", changes)
        self.assertTrue(entry.startswith("## 1.9.5-s2 – 2026-10-26\n\nSicherheitsupdate: gleiches Programm wie 1.9.5"))
        for line in ("- Basis-Image: Python 3.13.16 → 3.13.17", "- Debian: libssl3t64 3.5.1-1 → 3.5.1-1+deb13u1",
                     "- Debian: newpkg 1.0 (neu)", "- Python: cryptography 50.0.1 → 50.0.2",
                     "- Python: oldpkg 1.0 (entfernt)"):
            self.assertIn(line, entry)
        text = ci.insert_changelog("# Änderungen\n\nIntro.\n\n## 1.9.5 – 2026-10-24\n\nx\n", entry)
        self.assertLess(text.index("## 1.9.5-s2"), text.index("## 1.9.5 –"))
        self.assertTrue(text.startswith("# Änderungen\n\nIntro.\n\n## 1.9.5-s2"))
        many = ci.changelog_entry("1-s1", "1", "d", [{"kind": "debian", "name": f"p{i}", "old": "1", "new": "2"}
                                                    for i in range(100)], limit=10)
        self.assertIn("… und 90 weitere", many)

    def test_trivyignore_needs_reason_and_expiry(self):
        today = dt.date(2026, 10, 25)
        ok = "# reason: only the XYZ parser, never called\nCVE-2026-1 exp:2027-01-31\n"
        self.assertEqual(ci.check_trivyignore(ok, today), ([], []))
        errors, _ = ci.check_trivyignore("CVE-2026-2 exp:2027-01-31\n", today)
        self.assertIn("no reason", errors[0])
        errors, _ = ci.check_trivyignore("# reason: checked\nCVE-2026-3\n", today)
        self.assertIn("no expiry date", errors[0])
        errors, warnings = ci.check_trivyignore("# reason: checked\nCVE-2026-4 exp:2026-01-01\n", today)
        self.assertEqual(errors, [])
        self.assertIn("expired", warnings[0])
        self.assertEqual(ci.check_trivyignore((ROOT / ".trivyignore").read_text(), today)[0], [])


# ====================================================================== eol_check.py
class EolTests(unittest.TestCase):
    ITEMS = [("python", "3.13", "Python 3.13"), ("debian", "13", "Debian 13 (trixie)")]

    def answers(self, py, deb):
        return {"python/3.13": {"result": {"eolFrom": py}}, "debian/13": {"eol": deb}}

    def test_thresholds(self):
        today = dt.date(2026, 10, 25)
        status, lines = eol_check.evaluate(self.ITEMS, self.answers("2029-10-31", "2030-06-30"), today)
        self.assertEqual(status, 0)
        self.assertTrue(all(l.startswith("::notice") for l in lines))
        status, lines = eol_check.evaluate(self.ITEMS, self.answers("2027-09-01", "2030-06-30"), today)   # < 12 months
        self.assertEqual(status, 0)
        self.assertTrue(lines[0].startswith("::warning"))
        status, lines = eol_check.evaluate(self.ITEMS, self.answers("2029-10-31", "2027-03-01"), today)   # < 6 months
        self.assertEqual(status, 1)
        self.assertTrue(lines[1].startswith("::error"))
        status, lines = eol_check.evaluate(self.ITEMS, {"python/3.13": {"result": {"eolFrom": None}},
                                                        "debian/13": {"eol": "2030-06-30"}}, today)
        self.assertEqual(status, 1)                                    # unknown end date: red, not silent

    def test_base_read_from_repository(self):
        items = eol_check.base(ROOT)
        self.assertEqual([i[:2] for i in items], [("python", "3.13"), ("debian", "13")])


# ====================================================================== Trivy flow with a stand-in Trivy
FAKE_TRIVY = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
[ "$1" = --version ] && { echo "Version: 0.75.0"; exit 0; }
[ -n "${FAKE_FAIL:-}" ] && exit 2
out=""; prev=""
for a in "$@"; do [ "$prev" = --output ] && out="$a"; prev="$a"; done
if [ -n "${FAKE_FINDING:-}" ]; then
  printf '{"Results":[{"Target":"Python","Vulnerabilities":[{"Severity":"HIGH","VulnerabilityID":"CVE-2026-9","PkgName":"urllib3","InstalledVersion":"2.7.0","FixedVersion":"2.8.0"}]}]}' > "$out"
else
  printf '{"Results":[{"Target":"Python"}]}' > "$out"
fi
"""


class TrivyFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        for rel in (".github/trivy-scan.sh", ".github/trivy-report.sh", ".github/install-trivy.sh",
                    "scripts/ci.py", ".trivyignore"):
            (self.tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, self.tmp / rel)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "trivy"
        fake.write_text(FAKE_TRIVY)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "calls.log"

    def scan(self, **env):
        e = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_LOG=str(self.log),
                 GITHUB_STEP_SUMMARY=str(self.tmp / "summary.md"), **env)
        return subprocess.run(["bash", str(self.tmp / ".github/trivy-scan.sh"), "image.tar", "Test"],
                              cwd=self.tmp, env=e, capture_output=True, text=True)

    def test_no_findings_passes(self):
        r = self.scan()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        args = self.log.read_text()
        for flag in ("--input image.tar", "--severity HIGH,CRITICAL", "--ignore-unfixed", "--scanners vuln,secret",
                     f"--ignorefile {self.tmp}/.trivyignore"):
            self.assertIn(flag, args)
        self.assertIn("No HIGH/CRITICAL findings", (self.tmp / "summary.md").read_text())

    def test_finding_stops_with_readable_cause(self):
        r = self.scan(FAKE_FINDING="1", CONSEQUENCE="nothing is published")
        self.assertEqual(r.returncode, 1)
        self.assertIn("::error title=Trivy::1 finding(s) – nothing is published", r.stdout)
        self.assertIn("::warning title=HIGH CVE-2026-9::urllib3 2.7.0 → fixed in 2.8.0", r.stdout)
        self.assertIn("| HIGH | CVE-2026-9 | urllib3 | 2.7.0 | 2.8.0 | Python |", (self.tmp / "summary.md").read_text())

    def test_scanner_failure_is_red(self):
        r = self.scan(FAKE_FAIL="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("the scan itself failed", r.stdout)

    def test_invalid_trivyignore_stops_before_scanning(self):
        (self.tmp / ".trivyignore").write_text("CVE-2026-1\n")
        r = self.scan()
        self.assertEqual(r.returncode, 1)
        self.assertIn("::error title=.trivyignore::", r.stdout)
        self.assertFalse(self.log.exists())

    def test_install_checks_sha256(self):
        src = (self.tmp / ".github/install-trivy.sh").read_text()
        self.assertRegex(src, r'TRIVY_VERSION="\d+\.\d+\.\d+"')
        self.assertRegex(src, r'TRIVY_SHA256="[0-9a-f]{64}"')
        self.assertIn("sha256sum -c", src)
        curl = self.bin / "curl"                                   # a download that is not the real one
        curl.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && { echo bad > "$2"; }; shift; done\n')
        curl.chmod(0o755)
        dest = self.tmp / "inst"
        r = subprocess.run(["bash", str(self.tmp / ".github/install-trivy.sh"), str(dest)],
                           env=dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}"), capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not match the fixed SHA-256", r.stdout)
        self.assertFalse((dest / "trivy").exists())


# ====================================================================== the workflow
class WorkflowTests(unittest.TestCase):
    J = jobs()

    def test_jobs(self):
        self.assertEqual(list(self.J), ["mode", "build", "test", "scan", "publish", "relock", "relock-commit",
                                        "scan-latest", "eol"])

    def test_triggers_only_main_branch(self):
        on = WF.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("  push:\n    branches: [master]\n", on)
        self.assertNotIn("tags:", on)
        self.assertIn("workflow_dispatch:", on)

    def test_permissions_minimal(self):
        self.assertIn("\npermissions: {}\n", WF)
        for name, text in self.J.items():
            perms = job_permissions(text)
            self.assertIsNotNone(perms, f"{name}: no permissions block")
            if name in ("publish", "relock-commit"):
                self.assertIn("contents: write", perms)
            else:
                self.assertNotIn("write", perms, name)

    def test_pinned_actions_and_no_trivy_action(self):
        for ref in re.findall(r"uses:\s*(\S+)", WF):
            self.assertRegex(ref, r"@[0-9a-f]{40}$")
        uses = " ".join(re.findall(r"uses:\s*(\S+)", WF))
        self.assertNotIn("aquasecurity/", uses)                      # no Trivy GitHub Action at all
        self.assertEqual(WF.count('.github/install-trivy.sh "${RUNNER_TEMP}/trivy"'), 2)   # scan + daily scan

    def test_no_stored_git_credentials(self):
        checkouts = re.findall(r"uses: actions/checkout@\S+.*?(?=\n      - |\Z)", WF, re.S)
        self.assertEqual(len(checkouts), len(self.J))
        for c in checkouts:
            self.assertIn("persist-credentials: false", c)
        self.assertNotIn("credential.helper", WF)
        self.assertNotIn("git config --global", WF)

    def test_order_build_test_scan_publish(self):
        self.assertIn("needs: build\n", self.J["test"])
        self.assertIn("needs: build\n", self.J["scan"])
        self.assertIn("needs: [build, test, scan]", self.J["publish"])
        self.assertIn("if: needs.build.outputs.publish == 'true'", self.J["publish"])
        self.assertIn("publish: ${{ needs.mode.outputs.publish == 'true' && steps.prep.outputs.skip != 'true' }}",
                      self.J["build"])
        self.assertIn("push: false", self.J["build"])
        self.assertIn("no-cache: true", self.J["build"])
        self.assertNotIn("cache-from", WF)
        self.assertTrue("--entrypoint python assetlotse:scan -m pytest" in self.J["test"])
        self.assertIn(".github/trivy-scan.sh", self.J["scan"])
        for name in ("build", "publish"):
            self.assertNotIn("trivy", self.J[name].lower())

    def test_never_overwrite_and_latest_last(self):
        pub = self.J["publish"]
        check = pub.index('if docker buildx imagetools inspect "$first"')
        self.assertLess(check, pub.index('docker push -q "$first"'))
        self.assertLess(pub.index("git apply"), pub.index('for t in "${tags[@]:1}"'))
        self.assertIn("push --atomic origin HEAD:refs/heads/master", pub)
        self.assertIn('http.https://github.com/.extraheader="$auth"', pub)
        self.assertIn('docker buildx imagetools inspect "${image}:${version}"', self.J["build"])   # release: once

    def test_security_update_flow(self):
        b = self.J["build"]
        refresh = b.index("python3 scripts/lock.py refresh")
        self.assertLess(b.index("unreleased program changes"), refresh)
        self.assertLess(refresh, b.index("git diff --quiet"))
        self.assertLess(b.index("plan-security"), b.index("ci.py changelog"))
        self.assertIn("No new versions within the base – nothing to do.", b)

    def test_one_release_at_a_time(self):
        self.assertIn("'docker-release'", WF)
        self.assertIn("cancel-in-progress: false", WF)

    def test_daily_scan_and_base_check(self):
        self.assertIn("docker logout ghcr.io", self.J["scan-latest"])
        self.assertLess(self.J["scan-latest"].index("docker logout"), self.J["scan-latest"].index("install-trivy"))
        self.assertIn("python3 scripts/eol_check.py", self.J["eol"])


if __name__ == "__main__":
    unittest.main()
