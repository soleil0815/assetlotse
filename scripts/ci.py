#!/usr/bin/env python3
"""Decisions of the GitHub workflow (.github/workflows/docker.yml), in one place so they can be tested:

  mode               what a run does (release, check, security update, relock, daily scan, EOL check)
  plan-release       image tags of a new program version (latest only for the highest version)
  plan-security      next security-update number <version>-s<n> and its tags
  changelog          changelog entry of a security update with the changed packages
  check-trivyignore  every exception in .trivyignore needs a reason and an expiry date
  registry-state     what is published in ghcr.io (fails closed: an error is never "does not exist")
  check-changes      a security update / relock may only change the lock files (checked before writing)
  release-notes      text of the GitHub release: the version's section of CHANGELOG.md

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

MAIN_REF = "refs/heads/main"
WEEKLY_CRON = "17 4 * * 1"      # Mondays 04:17 UTC: security update + base (EOL) check
DAILY_CRON = "43 5 * * *"       # every day 05:43 UTC: scan the published image
RELEASE_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


# ---------------------------------------------------------------- mode
def mode(event: str, schedule: str = "", input_mode: str = "", ref: str = "", input_publish: str = "true") -> dict:
    on_main = ref == MAIN_REF
    res = {"kind": "none", "build": False, "publish": False, "scan_latest": False, "eol": False, "relock": False}
    if event == "push":
        res.update(kind="release", build=True, publish=on_main)
    elif event == "schedule":
        if schedule == WEEKLY_CRON:
            res.update(kind="security", build=True, publish=on_main, eol=True)
        elif schedule == DAILY_CRON:
            res.update(scan_latest=True)
        else:
            raise SystemExit(f"unknown schedule {schedule!r}")
    elif event == "workflow_dispatch":
        m = input_mode or "check"
        if m == "check":
            res.update(kind="check", build=True)
        elif m == "security-update":
            res.update(kind="security", build=True, publish=on_main and input_publish == "true")
        elif m == "relock":
            res.update(relock=True, publish=on_main and input_publish == "true")
        elif m == "scan-latest":
            res.update(scan_latest=True)
        elif m == "eol-check":
            res.update(eol=True)
        else:
            raise SystemExit(f"unknown mode {m!r}")
    else:
        raise SystemExit(f"unexpected event {event!r}")
    return res


# ---------------------------------------------------------------- versions
def releases(tags) -> list[tuple[int, int, int]]:
    return sorted({tuple(map(int, m.groups())) for t in tags if (m := RELEASE_RE.match(t.strip()))})


def highest_release(tags) -> str | None:
    r = releases(tags)
    return ".".join(map(str, r[-1])) if r else None


def plan_release(image: str, version: str, git_tags) -> dict:
    """Tags for a new program version: <version>, <minor> if it is the highest of its minor line,
    latest if it is the highest version overall. Security tags (-s<n>) never count."""
    v = tuple(map(int, version.split(".")))
    all_ = releases(list(git_tags) + [version])
    tags = [f"{image}:{version}"]
    if max(r for r in all_ if r[:2] == v[:2]) == v:
        tags.append(f"{image}:{v[0]}.{v[1]}")
    latest = all_[-1] == v
    if latest:
        tags.append(f"{image}:latest")
    return {"tags": tags, "latest": latest}


def next_security_number(version: str, tags) -> int:
    pat = re.compile(rf"^v?{re.escape(version)}-s(\d+)$")
    return 1 + max([0] + [int(m.group(1)) for t in tags if (m := pat.match(t.strip()))])


def plan_security(image: str, version: str, git_tags, registry_tags) -> dict:
    """A security update is always for the highest program version; the number counts per version
    (a new program version starts again at s1). Git tags and registry tags both count, so a number
    is never used twice – not even after a run that stopped halfway."""
    highest = highest_release(git_tags)
    if highest != version:
        raise SystemExit(f"The program version on main ({version}) is not the highest release ({highest}) – "
                         "a security update is only made for the highest version.")
    n = next_security_number(version, list(git_tags) + list(registry_tags))
    name = f"{version}-s{n}"
    major, minor, _ = version.split(".")
    return {"name": name, "tags": [f"{image}:{name}", f"{image}:{major}.{minor}", f"{image}:latest"]}


def expected_build(version: str, git_tags) -> str:
    """The newest published build of a program version: <version>-s<n> (git tag v<version>-s<n> is only
    set after its image was pushed) or <version>."""
    n = next_security_number(version, git_tags) - 1
    return f"{version}-s{n}" if n else version


def build_key(build: str) -> tuple | None:
    """"1.10.0" -> (1, 10, 0, 0), "1.10.0-s3" -> (1, 10, 0, 3); anything else -> None."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-s(\d+))?$", build or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def plan_repair(image: str, version: str, git_tags, digest_of, build_of) -> dict:
    """After a run that stopped halfway: which floating tags (<minor>, latest) still point to an OLDER build
    of this pipeline than the newest published one. digest_of(ref) -> digest or None; build_of(ref) -> the
    image's label io.assetlotse.build or None. A tag that points to something else (no label, or a newer
    build) is left alone – a repair only ever moves forward."""
    name = expected_build(version, git_tags)
    src = f"{image}:{name}"
    want = digest_of(src)
    if want is None:
        return {"source": src, "tags": []}
    tags = []
    for t in plan_release(image, version, git_tags)["tags"][1:]:
        if digest_of(t) == want:
            continue
        current = build_key(build_of(t)) if digest_of(t) else (0, 0, 0, 0)
        if current is not None and current < build_key(name):
            tags.append(t)
    return {"source": src, "tags": tags}


# ---------------------------------------------------------------- registry (ghcr.io, read only)
MANIFEST_TYPES = ", ".join(["application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
                            "application/vnd.docker.distribution.manifest.list.v2+json",
                            "application/vnd.docker.distribution.manifest.v2+json"])


class Registry:
    def __init__(self, repo: str, user: str, password: str, host: str = "ghcr.io"):
        self.repo, self.host = repo.lower(), host
        auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        req = urllib.request.Request(f"https://{host}/token?scope=repository:{self.repo}:pull",
                                     headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            self.token = json.load(r)["token"]

    def _get(self, url: str, method: str = "GET"):
        req = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {self.token}",
                                                                  "Accept": MANIFEST_TYPES})
        return urllib.request.urlopen(req, timeout=60)

    def digest(self, tag: str) -> str | None:
        """Digest of a tag, None if it does not exist (404). Any other answer raises – never "missing"."""
        try:
            with self._get(f"https://{self.host}/v2/{self.repo}/manifests/{tag}", "HEAD") as r:
                d = r.headers.get("Docker-Content-Digest")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise SystemExit(f"registry: {tag}: HTTP {e.code} – stopped (an error never counts as 'missing')")
        if not d:
            raise SystemExit(f"registry: {tag}: no digest in the answer – stopped")
        return d

    def _blob(self, digest: str) -> dict:
        """Blobs are served via a redirect to a storage URL – follow it WITHOUT the registry token."""
        class Stop(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        req = urllib.request.Request(f"https://{self.host}/v2/{self.repo}/blobs/{digest}",
                                     headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.build_opener(Stop).open(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308) or not e.headers.get("Location"):
                raise SystemExit(f"registry: blob {digest[:19]}: HTTP {e.code} – stopped")
            with urllib.request.urlopen(e.headers["Location"], timeout=60) as r:
                return json.load(r)

    def build_label(self, tag: str) -> str | None:
        """The label io.assetlotse.build of an image (None if it has none)."""
        def get(path):
            with self._get(f"https://{self.host}/v2/{self.repo}/{path}") as r:
                return json.load(r)
        m = get(f"manifests/{tag}")
        if "manifests" in m:                                   # an index: take the image for linux/amd64
            ref = next((x for x in m["manifests"] if x.get("platform", {}).get("architecture") == "amd64"),
                       m["manifests"][0])
            m = get(f"manifests/{ref['digest']}")
        config = self._blob(m["config"]["digest"])
        return (config.get("config", {}).get("Labels") or {}).get("io.assetlotse.build")

    def tags(self) -> list[str]:
        url, tags = f"https://{self.host}/v2/{self.repo}/tags/list?n=1000", []
        while url:
            with self._get(url) as r:
                tags += json.load(r).get("tags") or []
                link = r.headers.get("Link", "")
            m = re.search(r"<([^>]+)>;\s*rel=\"?next", link)
            url = (m.group(1) if m.group(1).startswith("http") else f"https://{self.host}{m.group(1)}") if m else None
        return tags


# ---------------------------------------------------------------- what a security update / relock may change
ALLOWED_FILES = {"requirements.txt", "build/debian-packages.lock", "CHANGELOG.md", "Dockerfile"}
ALLOWED_DOCKERFILE_LINE = re.compile(r"^[+-]ARG (PYTHON_IMAGE|PIP_VERSION)=")


def check_changes(names, dockerfile_diff: str) -> list[str]:
    errors = [f"{n} must not be changed by an automatic update" for n in names if n not in ALLOWED_FILES]
    for line in dockerfile_diff.splitlines():
        if line.startswith(("+++", "---", "@@")) or not line.startswith(("+", "-")):
            continue
        if not ALLOWED_DOCKERFILE_LINE.match(line):
            errors.append(f"Dockerfile: only ARG PYTHON_IMAGE / ARG PIP_VERSION may change, not: {line[:80]}")
    return errors


# ---------------------------------------------------------------- changelog
LABELS = {"base": "Basis-Image", "pip": "pip (nur beim Bau)", "debian": "Debian", "python": "Python"}


def changelog_entry(name: str, version: str, date: str, changes: list[dict], limit: int = 60) -> str:
    lines = [f"## {name} – {date}", "",
             f"Sicherheitsupdate: gleiches Programm wie {version}, nur aktualisierte Pakete.", ""]
    items = []
    for c in changes:
        what = f"{LABELS.get(c['kind'], c['kind'])}: {c['name']}"
        if c["kind"] == "base":
            what = f"{LABELS['base']}: Python"
        if c.get("old") and c.get("new"):
            items.append(f"- {what} {c['old']} → {c['new']}")
        elif c.get("new"):
            items.append(f"- {what} {c['new']} (neu)")
        else:
            items.append(f"- {what} {c['old']} (entfernt)")
    if len(items) > limit:
        rest = len(items) - limit
        items = items[:limit] + [f"- … und {rest} weitere (vollständig in `build/debian-packages.lock` "
                                 "und `requirements.txt`)"]
    return "\n".join(lines + items) + "\n\n"


def release_notes(changelog: str, name: str, image_ref: str) -> str:
    """The section "## <name> – <date>" of CHANGELOG.md (without its heading) plus how to get the image."""
    m = re.search(rf"^## {re.escape(name)} [–-][^\n]*\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    if not m:
        raise SystemExit(f"CHANGELOG.md has no section for {name}")
    body = m.group(1).strip()
    return (f"{body}\n\n---\nImage: `{image_ref}` – auf dem Server: `./update.sh --pull` "
            f"(oder ein Update-Tool, das `latest` folgt).\n")


def insert_changelog(text: str, entry: str) -> str:
    i = text.find("\n## ")
    if i < 0:
        return text.rstrip("\n") + "\n\n" + entry
    return text[:i + 1] + entry + text[i + 1:]


# ---------------------------------------------------------------- .trivyignore
def check_trivyignore(text: str, today: dt.date) -> tuple[list[str], list[str]]:
    """Errors: an entry without expiry date (exp:YYYY-MM-DD) or without a reason (comment line directly
    above, starting with '# reason:'). Warnings: expired entries (Trivy no longer suppresses them)."""
    errors, warnings, prev = [], [], ""
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            prev = ""
            continue
        if line.startswith("#"):
            prev = line
            continue
        entry = line.split()[0]
        exp = re.search(r"\bexp:(\d{4}-\d{2}-\d{2})\b", line)
        if not exp:
            errors.append(f"line {no}: {entry} has no expiry date (exp:YYYY-MM-DD)")
        elif dt.date.fromisoformat(exp.group(1)) < today:
            warnings.append(f"line {no}: {entry} expired on {exp.group(1)} – it is reported again")
        if not re.match(r"^#\s*reason:\s*\S", prev, re.I):
            errors.append(f"line {no}: {entry} has no reason ('# reason: …' on the line above)")
        prev = ""
    return errors, warnings


# ---------------------------------------------------------------- CLI
def _out(values: dict) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    text = "".join(f"{k}={str(v).lower() if isinstance(v, bool) else v}\n" for k, v in values.items())
    if path:
        with open(path, "a") as f:
            f.write(text)
    sys.stdout.write(text)


def _lines(path: str | None) -> list[str]:
    return [l.strip() for l in Path(path).read_text().splitlines() if l.strip()] if path else []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mode")
    m.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    m.add_argument("--schedule", default="")
    m.add_argument("--input-mode", default="")
    m.add_argument("--input-publish", default="true")
    m.add_argument("--ref", default=os.environ.get("GITHUB_REF", ""))
    r = sub.add_parser("plan-release")
    r.add_argument("--image", required=True)
    r.add_argument("--version", required=True)
    r.add_argument("--git-tags", required=True)
    s = sub.add_parser("plan-security")
    s.add_argument("--image", required=True)
    s.add_argument("--version", required=True)
    s.add_argument("--git-tags", required=True)
    s.add_argument("--registry-tags")
    c = sub.add_parser("changelog")
    c.add_argument("--name", required=True)
    c.add_argument("--version", required=True)
    c.add_argument("--report", required=True)
    c.add_argument("--file", default="CHANGELOG.md")
    c.add_argument("--date", default=dt.date.today().isoformat())
    t = sub.add_parser("check-trivyignore")
    t.add_argument("file", nargs="?", default=".trivyignore")
    g = sub.add_parser("registry-state")
    g.add_argument("--image", required=True)
    g.add_argument("--version", required=True)
    g.add_argument("--git-tags", required=True)
    g.add_argument("--tags-out", required=True)
    sub.add_parser("check-changes")
    n = sub.add_parser("release-notes")
    n.add_argument("--name", required=True)
    n.add_argument("--image", required=True)
    n.add_argument("--file", default="CHANGELOG.md")
    d = sub.add_parser("registry-digest")
    d.add_argument("--image", required=True)
    d.add_argument("--tag", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "mode":
        _out(mode(a.event, a.schedule, a.input_mode, a.ref, a.input_publish))
    elif a.cmd == "plan-release":
        p = plan_release(a.image, a.version, _lines(a.git_tags))
        _out({"tags": ",".join(p["tags"]), "latest": p["latest"]})
    elif a.cmd == "plan-security":
        p = plan_security(a.image, a.version, _lines(a.git_tags), _lines(a.registry_tags))
        _out({"name": p["name"], "tags": ",".join(p["tags"])})
    elif a.cmd == "changelog":
        changes = json.loads(Path(a.report).read_text())["changes"]
        entry = changelog_entry(a.name, a.version, a.date, changes)
        f = Path(a.file)
        f.write_text(insert_changelog(f.read_text(), entry))
        sys.stdout.write(entry)
    elif a.cmd == "registry-state":
        repo = a.image.split("/", 1)[1]
        reg = Registry(repo, os.environ["GITHUB_ACTOR"], os.environ["GH_TOKEN"])
        Path(a.tags_out).write_text("\n".join(reg.tags()) + "\n")
        git_tags = _lines(a.git_tags)
        repair = plan_repair(a.image, a.version, git_tags, lambda ref: reg.digest(ref.rsplit(":", 1)[1]),
                             lambda ref: reg.build_label(ref.rsplit(":", 1)[1]))
        _out({"version_exists": reg.digest(a.version) is not None, "repair_source": repair["source"],
              "repair_tags": ",".join(repair["tags"])})
    elif a.cmd == "registry-digest":
        reg = Registry(a.image.split("/", 1)[1], os.environ["GITHUB_ACTOR"], os.environ["GH_TOKEN"])
        print(reg.digest(a.tag) or "missing")
    elif a.cmd == "check-changes":
        names = subprocess.run(["git", "diff", "--name-only", "HEAD"], check=True, text=True,
                               stdout=subprocess.PIPE).stdout.split()
        df = subprocess.run(["git", "diff", "-U0", "HEAD", "--", "Dockerfile"], check=True, text=True,
                            stdout=subprocess.PIPE).stdout
        errors = check_changes(names, df)
        for e in errors:
            print(f"::error title=Unexpected change::{e}")
        return 1 if errors else 0
    elif a.cmd == "release-notes":
        sys.stdout.write(release_notes(Path(a.file).read_text(), a.name, a.image))
    elif a.cmd == "check-trivyignore":
        f = Path(a.file)
        errors, warnings = check_trivyignore(f.read_text() if f.exists() else "", dt.date.today())
        for w in warnings:
            print(f"::warning title=.trivyignore::{w}")
        for e in errors:
            print(f"::error title=.trivyignore::{e}")
        return 1 if errors else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
