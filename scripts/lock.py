#!/usr/bin/env python3
"""Sets the exact versions of everything in the AssetLotse image – always WITHIN the base ("Unterbau"):

- base image: the newest patch release of the same Python line on the same Debian release
  (e.g. python:3.13.<newest>-slim-trixie), pinned by tag AND digest. Never another line or release.
- Debian packages: all packages of the image (also those of the base image) at one snapshot of the
  Debian archive (snapshot.debian.org), listed with exact versions in build/debian-packages.lock.
- Python packages incl. all sub-dependencies (requirements.txt) and pip itself (ARG PIP_VERSION):
  the newest version within the same major version (before 1.0: the same minor version), never lower
  than today. Calendar versions (certifi, tzdata: 2026.x) have no upper limit.

If nothing changes in content, no file is touched (a new snapshot alone is not a change).

  python3 scripts/lock.py refresh [--report changes.json]   # needs Docker and Internet
  python3 scripts/lock.py check                              # offline consistency check (used by the tests)
  python3 scripts/lock.py changes --since <git ref> --report changes.json   # what changed since a release

Only the Python standard library is used, so it runs with any python3 (e.g. on a GitHub runner).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = "Dockerfile"
OS_LOCK = "build/debian-packages.lock"
REQS = "requirements.txt"
REQS_IN = "requirements.in"

BASE_RE = re.compile(r"^ARG PYTHON_IMAGE=python:(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
                     r"-slim-(?P<codename>[a-z]+)@sha256:(?P<digest>[0-9a-f]{64})$", re.M)
PIP_RE = re.compile(r"^ARG PIP_VERSION=(?P<version>\d+(?:\.\d+)*)$", re.M)
PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;#]+)(?P<rest>.*)$")
SNAP_RE = re.compile(r"^\d{8}T\d{6}Z$")
CALENDAR_FROM = 1990          # a "major version" this high is a year: certifi 2026.10.23, tzdata 2026.1


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ---------------------------------------------------------------- version limits
CALENDAR_NAMES = {"pip"}    # calendar versions by name: pip counts YY.N (26.2 = 2026)


def upper_bound(version: str, name: str = "") -> str | None:
    """Exclusive upper limit for updates: same major version, before 1.0 the same minor version,
    no limit for calendar versions."""
    m = re.match(r"(\d+)(?:\.(\d+))?", version)
    if not m:
        raise ValueError(f"cannot read version {version!r}")
    major, minor = int(m.group(1)), int(m.group(2) or 0)
    if major >= CALENDAR_FROM or norm(name) in CALENDAR_NAMES:
        return None
    if major == 0:
        return f"0.{minor + 1}"
    return str(major + 1)


def constraint(name: str, version: str) -> str:
    upper = upper_bound(version, name)
    return f"{name}>={version}" + (f",<{upper}" if upper else "")


def _key(version: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", version))


def within_limits(old: str, new: str, name: str = "") -> bool:
    """True if `new` is allowed as an update of `old` (not lower, not beyond the limit)."""
    if _key(new) < _key(old):
        return False
    upper = upper_bound(old, name)
    return upper is None or _key(new) < _key(upper)


# ---------------------------------------------------------------- files
def windows_only(rest: str) -> bool:
    marker = rest.split("#", 1)[0]
    return bool(re.search(r'==\s*"(win32|Windows)"', marker))


def read_pins(text: str) -> dict[str, tuple[str, str]]:
    """Linux-relevant exact pins: normalized name -> (name as written, version)."""
    pins = {}
    for line in text.splitlines():
        m = PIN_RE.match(line.strip())
        if m and not windows_only(m.group("rest")):
            pins[norm(m.group("name"))] = (m.group("name"), m.group("version"))
    return pins


def render_requirements(text: str, new: dict[str, tuple[str, str]], python_version: str | None = None) -> str:
    """requirements.txt with new versions: comments, grouping and Windows lines are kept, packages that are
    no longer needed are dropped, new sub-dependencies are added at the end."""
    out, used = [], set()
    for line in text.splitlines():
        m = PIN_RE.match(line.strip())
        if not m or windows_only(m.group("rest")):
            if python_version and line.startswith("#"):
                line = re.sub(r"(on Python )\d+\.\d+\.\d+", rf"\g<1>{python_version}", line)
            out.append(line)
            continue
        key = norm(m.group("name"))
        if key not in new:
            continue                                   # no longer needed
        used.add(key)
        out.append(f"{m.group('name')}=={new[key][1]}{m.group('rest')}")
    extra = sorted(k for k in new if k not in used)
    if extra:
        while out and not out[-1].strip():
            out.pop()
        out += ["", "# Further sub-dependencies (added by scripts/lock.py)"]
        out += [f"{new[k][0]}=={new[k][1]}" for k in extra]
    return "\n".join(out).rstrip("\n") + "\n"


def _header(text: str, key: str, default: str = "") -> str:
    m = re.search(rf"^# {key}: (.+)$", text, re.M)
    return m.group(1).strip() if m else default


def read_os_lock(text: str) -> dict:
    pkgs = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, version = line.split(" ", 1)
            pkgs[name] = version
    return {"snapshot": _header(text, "snapshot", "none"), "security": _header(text, "snapshot-security", "none"),
            "debian": _header(text, "debian"), "arch": _header(text, "arch"), "packages": pkgs}


def render_os_lock(snapshot: str, security: str, debian: str, arch: str, pkgs: dict[str, str]) -> str:
    lines = ["# Debian packages of the AssetLotse image – exact versions (generated by scripts/lock.py, do not edit).",
             f"# snapshot: {snapshot}", f"# snapshot-security: {security}", f"# debian: {debian}", f"# arch: {arch}"]
    lines += [f"{n} {v}" for n, v in sorted(pkgs.items(), key=lambda kv: kv[0].encode())]
    return "\n".join(lines) + "\n"


def read_state(root: Path) -> dict:
    return state_from_texts((root / DOCKERFILE).read_text(), (root / OS_LOCK).read_text(), (root / REQS).read_text())


def state_at(ref: str, root: Path = ROOT) -> dict:
    """The locked state as it was in a git commit (e.g. the last published release)."""
    show = lambda f: subprocess.run(["git", "show", f"{ref}:{f}"], cwd=root, check=True, text=True,
                                    stdout=subprocess.PIPE).stdout
    try:
        oslock = show(OS_LOCK)
    except subprocess.CalledProcessError:
        oslock = ""                                   # releases before the lock file existed
    return state_from_texts(show(DOCKERFILE), oslock, show(REQS))


def state_from_texts(dockerfile: str, oslock: str, reqs: str) -> dict:
    base = BASE_RE.search(dockerfile)
    pip = PIP_RE.search(dockerfile)
    if not base or not pip:
        raise SystemExit("Dockerfile: ARG PYTHON_IMAGE=python:X.Y.Z-slim-<codename>@sha256:<digest> "
                         "and ARG PIP_VERSION=<version> are required")
    os_ = read_os_lock(oslock)
    return {
        "major": int(base["major"]), "minor": int(base["minor"]), "patch": int(base["patch"]),
        "codename": base["codename"], "digest": base["digest"], "pip": pip["version"],
        "snapshot": os_["snapshot"], "security": os_["security"], "debian": os_["debian"], "arch": os_["arch"],
        "packages": os_["packages"], "pins": read_pins(reqs),
    }


def compare(old: dict, new: dict) -> list[dict]:
    """What changed in content between two locked states (snapshot or digest alone do not count)."""
    changes = []
    if old["patch"] != new["patch"] or (old["major"], old["minor"]) != (new["major"], new["minor"]):
        changes.append({"kind": "base", "name": "python", "old": f"{old['major']}.{old['minor']}.{old['patch']}",
                        "new": f"{new['major']}.{new['minor']}.{new['patch']}"})
    changes += diff_versions("debian", old["packages"], new["packages"])
    if old["pip"] != new["pip"]:
        changes.append({"kind": "pip", "name": "pip", "old": old["pip"], "new": new["pip"]})
    changes += diff_versions("python", {n: v for n, (_, v) in old["pins"].items()},
                             {n: v for n, (_, v) in new["pins"].items()})
    return changes


def base_ref(major, minor, patch, codename, digest) -> str:
    return f"python:{major}.{minor}.{patch}-slim-{codename}@sha256:{digest}"


# ---------------------------------------------------------------- check (offline)
def check(root: Path = ROOT) -> list[str]:
    problems = []
    try:
        state = read_state(root)
    except SystemExit as e:
        return [str(e)]
    for key in ("snapshot", "security"):
        if state[key] != "none" and not SNAP_RE.match(state[key]):
            problems.append(f"{OS_LOCK}: invalid {key} timestamp {state[key]!r}")
    if state["snapshot"] == "none" or state["security"] == "none" or not state["packages"]:
        problems.append(f"{OS_LOCK}: no packages locked yet – run: python3 scripts/lock.py refresh")
    if state["debian"] and not state["debian"].endswith(" " + state["codename"]):
        problems.append(f"{OS_LOCK}: Debian {state['debian']!r} does not match the base image ({state['codename']})")
    for line in (root / REQS).read_text().splitlines():
        s = line.split("#", 1)[0].strip()
        if s and not PIN_RE.match(s):
            problems.append(f"{REQS}: not an exact version: {line!r}")
    pins = state["pins"]
    for line in (root / REQS_IN).read_text().splitlines():
        s = line.split("#", 1)[0].split(";", 1)[0].strip()
        if not s:
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)>=([\d.]+),<([\d.]+)$", s)
        if not m:
            problems.append(f"{REQS_IN}: needs the form name>=X,<limit: {line!r}")
            continue
        name, low, high = m.groups()
        if upper_bound(low) != high:
            problems.append(f"{REQS_IN}: {name}: the limit must be <{upper_bound(low)} (same major version)")
        if ";" in line and windows_only(line.split(";", 1)[1]):
            continue
        if norm(name) not in pins:
            problems.append(f"{REQS_IN}: {name} is missing in {REQS}")
    return problems


# ---------------------------------------------------------------- refresh (Docker + Internet)
def run(cmd: list[str], **kw) -> str:
    print("+", " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd, check=True, text=True, stdout=subprocess.PIPE, **kw).stdout


def hub_tags(major: int, minor: int) -> list[str]:
    url = (f"https://hub.docker.com/v2/namespaces/library/repositories/python/tags"
           f"?page_size=100&name={major}.{minor}.")
    tags = []
    while url:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = json.load(r)
        tags += [t["name"] for t in data.get("results", [])]
        url = data.get("next")
    return tags


def pick_base_tag(tags: list[str], major: int, minor: int, codename: str, current_patch: int) -> int:
    """Highest patch release of exactly this Python line and Debian release – never lower than today."""
    pat = re.compile(rf"^{major}\.{minor}\.(\d+)-slim-{re.escape(codename)}$")
    patches = [int(m.group(1)) for t in tags if (m := pat.match(t))]
    return max([current_patch] + patches)


def digest_of(ref: str) -> str:
    manifest = json.loads(run(["docker", "buildx", "imagetools", "inspect", "--format", "{{json .Manifest}}", ref]))
    digest = manifest["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SystemExit(f"unexpected digest for {ref}: {digest}")
    return digest.split(":", 1)[1]


def blocks(output: str) -> dict:
    res, lines = {}, output.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("@@") and line.endswith("-begin"):
            name = line[2:-6]
            end = lines.index(f"@@{name}-end", i)
            res[name] = "\n".join(lines[i + 1:end])
        elif line.startswith("@@") and " " in line:
            key, value = line[2:].split(" ", 1)
            res[key] = value
    return res


def latest_snapshots() -> tuple[str, str]:
    """The newest COMPLETED imports of snapshot.debian.org (archive "debian" and "debian-security").
    Taking "now" instead would race with an import that is still running."""
    try:
        with urllib.request.urlopen("https://snapshot.debian.org/mr/timestamp/", timeout=120) as r:
            result = json.load(r)["result"]
        return max(t for t in result["debian"] if SNAP_RE.match(t)), \
            max(t for t in result["debian-security"] if SNAP_RE.match(t))
    except Exception as e:                                   # fallback: a day back is always complete
        ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y%m%dT%H%M%SZ")
        print(f"snapshot.debian.org/mr/timestamp not usable ({e}) – using {ts}", file=sys.stderr)
        return ts, ts


def resolve(image: str, snapshot: str, security: str, state: dict, root: Path) -> dict:
    with tempfile.TemporaryDirectory() as work:
        Path(work, "requirements.in").write_text((root / REQS_IN).read_text())
        Path(work, "constraints.txt").write_text(
            "".join(constraint(n, v) + "\n" for n, v in state["pins"].values()))
        out = run(["docker", "run", "--rm", "--pull", "always",
                   "-v", f"{root / 'build'}:/lock:ro", "-v", f"{work}:/work:ro",
                   "-v", f"{root / 'scripts' / 'resolve-in-image.sh'}:/resolve.sh:ro",
                   image, "sh", "/resolve.sh", snapshot, security, constraint("pip", state["pip"])])
    b = blocks(out)
    report = json.loads(b["report"])
    pins = {norm(i["metadata"]["name"]): (i["metadata"]["name"], i["metadata"]["version"]) for i in report["install"]}
    pkgs = dict(line.split(" ", 1) for line in b["packages"].splitlines() if line)
    debian_version, codename = b["debian"].split()
    return {"python": b["python"], "pip": b["pip"], "pins": pins, "packages": pkgs,
            "debian": f"{debian_version} {codename}", "arch": b["arch"]}


def diff_versions(kind: str, old: dict[str, str], new: dict[str, str]) -> list[dict]:
    return [{"kind": kind, "name": n, "old": old.get(n), "new": new.get(n)}
            for n in sorted(set(old) | set(new)) if old.get(n) != new.get(n)]


def refresh(root: Path = ROOT, report_path: str | None = None) -> list[dict]:
    state = read_state(root)
    major, minor, codename = state["major"], state["minor"], state["codename"]
    patch = pick_base_tag(hub_tags(major, minor), major, minor, codename, state["patch"])
    tag = f"python:{major}.{minor}.{patch}-slim-{codename}"
    digest = digest_of(tag)
    snapshot, security = latest_snapshots()
    new = resolve(f"{tag}@sha256:{digest}", snapshot, security, state, root)

    # the limits, checked once more on the result
    if not new["python"].startswith(f"{major}.{minor}."):
        raise SystemExit(f"Python {new['python']} is outside the line {major}.{minor} – stopped")
    if not new["debian"].endswith(" " + codename):
        raise SystemExit(f"Debian {new['debian']} is not {codename} – stopped")
    for name, (_, version) in new["pins"].items():
        if name in state["pins"] and not within_limits(state["pins"][name][1], version, name):
            raise SystemExit(f"{name} {state['pins'][name][1]} -> {version} is outside the limit – stopped")
    if not within_limits(state["pip"], new["pip"], "pip"):
        raise SystemExit(f"pip {state['pip']} -> {new['pip']} is outside the limit – stopped")

    changes = []
    if patch != state["patch"]:
        changes.append({"kind": "base", "name": "python",
                        "old": f"{major}.{minor}.{state['patch']}", "new": f"{major}.{minor}.{patch}"})
    changes += diff_versions("debian", state["packages"], new["packages"])
    if changes:                                           # new base and/or new Debian packages
        dockerfile = (root / DOCKERFILE).read_text()
        dockerfile = BASE_RE.sub("ARG PYTHON_IMAGE=" + base_ref(major, minor, patch, codename, digest), dockerfile)
        (root / DOCKERFILE).write_text(dockerfile)
        (root / OS_LOCK).write_text(render_os_lock(snapshot, security, new["debian"], new["arch"], new["packages"]))
    if new["pip"] != state["pip"]:
        changes.append({"kind": "pip", "name": "pip", "old": state["pip"], "new": new["pip"]})
        dockerfile = (root / DOCKERFILE).read_text()
        (root / DOCKERFILE).write_text(PIP_RE.sub(f"ARG PIP_VERSION={new['pip']}", dockerfile))
    py_changes = diff_versions("python", {n: v for n, (_, v) in state["pins"].items()},
                               {n: v for n, (_, v) in new["pins"].items()})
    if py_changes or patch != state["patch"]:
        text = (root / REQS).read_text()
        (root / REQS).write_text(render_requirements(text, new["pins"], new["python"]))
    changes += py_changes
    if report_path:
        Path(report_path).write_text(json.dumps({"changed": bool(changes), "changes": changes}, indent=1) + "\n")
    print(f"{len(changes)} change(s)" if changes else "No new versions – nothing changed.", file=sys.stderr)
    for c in changes:
        print(f"  {c['kind']:7} {c['name']}: {c['old'] or '–'} -> {c['new'] or '–'}", file=sys.stderr)
    return changes


def older_than(old: dict, new: dict) -> list[str]:
    """What in `new` is OLDER than in `old` (the newest published build) – a new program version must never
    ship older packages than what servers already run."""
    problems = []
    if (new["major"], new["minor"], new["patch"]) < (old["major"], old["minor"], old["patch"]):
        problems.append(f"base image Python {new['major']}.{new['minor']}.{new['patch']} is older than "
                        f"{old['major']}.{old['minor']}.{old['patch']}")
    for key, label in (("snapshot", "Debian snapshot"), ("security", "Debian security snapshot")):
        if old[key] != "none" and new[key] != "none" and new[key] < old[key]:
            problems.append(f"{label} {new[key]} is older than {old[key]}")
    if _key(new["pip"]) < _key(old["pip"]):
        problems.append(f"pip {new['pip']} is older than {old['pip']}")
    for name, (_, version) in old["pins"].items():
        if name in new["pins"] and _key(new["pins"][name][1]) < _key(version):
            problems.append(f"{name} {new['pins'][name][1]} is older than {version}")
    return problems


def _github_error(title: str):
    """Exit messages as annotations on the run page (GitHub Actions), so the cause is readable there."""
    def wrap(fn):
        def run(*a, **k):
            try:
                return fn(*a, **k)
            except SystemExit as e:
                if isinstance(e.code, str) and os.environ.get("GITHUB_ACTIONS"):
                    print(f"::error title={title}::{e.code}")
                    raise SystemExit(1) from None
                raise
            except Exception as e:  # noqa: BLE001 – network, Docker, pip: one readable line on the run page
                if os.environ.get("GITHUB_ACTIONS"):
                    print(f"::error title={title}::{type(e).__name__}: {e}")
                raise
        return run
    return wrap


@_github_error("scripts/lock.py")
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refresh", help="set new versions within the limits (Docker + Internet)")
    r.add_argument("--report", help="write the list of changes as JSON")
    sub.add_parser("check", help="offline consistency check")
    o = sub.add_parser("not-older", help="fail if anything is older than in a git commit (the newest published build)")
    o.add_argument("--than", required=True)
    c = sub.add_parser("changes", help="content changes against a git commit (e.g. the last published release)")
    c.add_argument("--since", required=True)
    c.add_argument("--report", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "not-older":
        try:
            old = state_at(args.than, ROOT)
        except (SystemExit, subprocess.CalledProcessError):
            print(f"{args.than}: no locked state to compare with – skipped", file=sys.stderr)
            return 0
        problems = older_than(old, read_state(ROOT))
        if problems:
            raise SystemExit(f"This version would ship OLDER packages than the published {args.than}: "
                             + "; ".join(problems) + " – merge the newest lock files (git pull) and push again")
        return 0
    if args.cmd == "changes":
        changes = compare(state_at(args.since, ROOT), read_state(ROOT))
        Path(args.report).write_text(json.dumps({"changed": bool(changes), "changes": changes}, indent=1) + "\n")
        print(f"{len(changes)} change(s) since {args.since}", file=sys.stderr)
        return 0
    if args.cmd == "check":
        problems = check(ROOT)
        for p in problems:
            print("ERROR:", p, file=sys.stderr)
        return 1 if problems else 0
    os.chdir(ROOT)
    refresh(ROOT, args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
