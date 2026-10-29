#!/usr/bin/env python3
"""Sets the exact versions of everything in the AssetLotse image – always WITHIN the base ("Unterbau"):

- base image: the newest patch release of the same Python line on the same Debian release
  (e.g. python:3.13.<newest>-slim-trixie), pinned by tag AND digest. Never another line or release.
- Debian packages: all packages of the image (also those of the base image) at one snapshot of the
  Debian archive (snapshot.debian.org), listed with exact versions per CPU architecture:
  build/debian-packages.lock (amd64) and build/debian-packages.arm64.lock (arm64).
- Python packages incl. all sub-dependencies (requirements.txt) and pip itself (ARG PIP_VERSION):
  the newest version within the same major version (before 1.0: the same minor version), never lower
  than today. Calendar versions (certifi, tzdata: 2026.x) have no upper limit.

If nothing changes in content, no file is touched (a new snapshot alone is not a change).

  python3 scripts/lock.py refresh [--report changes.json]   # needs Docker (+ arm64 emulation) and Internet
  # the same in steps, each architecture on its own machine (GitHub: native amd64 and arm64 runners):
  python3 scripts/lock.py targets --out targets.json         # base image, digest, snapshot (Internet)
  python3 scripts/lock.py resolve --targets targets.json --out resolved-amd64.json   # Docker, this machine's arch
  python3 scripts/lock.py apply --targets targets.json --resolved resolved-amd64.json --resolved resolved-arm64.json
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
OS_LOCK = "build/debian-packages.lock"                     # amd64 (name kept from before arm64 builds)
OS_LOCKS = {"amd64": OS_LOCK, "arm64": "build/debian-packages.arm64.lock"}
ARCHES = tuple(OS_LOCKS)
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


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def read_state(root: Path) -> dict:
    return state_from_texts((root / DOCKERFILE).read_text(), (root / OS_LOCK).read_text(), (root / REQS).read_text(),
                            {a: _read(root / f) for a, f in OS_LOCKS.items() if a != "amd64"})


def state_at(ref: str, root: Path = ROOT) -> dict:
    """The locked state as it was in a git commit (e.g. the last published release)."""
    show = lambda f: subprocess.run(["git", "show", f"{ref}:{f}"], cwd=root, check=True, text=True,
                                    stdout=subprocess.PIPE).stdout
    def maybe(f):
        try:
            return show(f)
        except subprocess.CalledProcessError:
            return ""                                 # releases before that lock file existed
    return state_from_texts(show(DOCKERFILE), maybe(OS_LOCK), show(REQS),
                            {a: maybe(f) for a, f in OS_LOCKS.items() if a != "amd64"})


def state_from_texts(dockerfile: str, oslock: str, reqs: str, other_locks: dict[str, str] | None = None) -> dict:
    base = BASE_RE.search(dockerfile)
    pip = PIP_RE.search(dockerfile)
    if not base or not pip:
        raise SystemExit("Dockerfile: ARG PYTHON_IMAGE=python:X.Y.Z-slim-<codename>@sha256:<digest> "
                         "and ARG PIP_VERSION=<version> are required")
    os_ = read_os_lock(oslock)
    by_arch = {"amd64": os_}
    for arch, text in (other_locks or {}).items():
        by_arch[arch] = read_os_lock(text)
    return {
        "major": int(base["major"]), "minor": int(base["minor"]), "patch": int(base["patch"]),
        "codename": base["codename"], "digest": base["digest"], "pip": pip["version"],
        "snapshot": os_["snapshot"], "security": os_["security"], "debian": os_["debian"], "arch": os_["arch"],
        "packages": os_["packages"], "pins": read_pins(reqs), "locks": by_arch,
    }


def compare(old: dict, new: dict) -> list[dict]:
    """What changed in content between two locked states (snapshot or digest alone do not count)."""
    changes = []
    if old["patch"] != new["patch"] or (old["major"], old["minor"]) != (new["major"], new["minor"]):
        changes.append({"kind": "base", "name": "python", "old": f"{old['major']}.{old['minor']}.{old['patch']}",
                        "new": f"{new['major']}.{new['minor']}.{new['patch']}"})
    changes += debian_changes({a: old["locks"].get(a, {}).get("packages", {}) for a in ARCHES},
                              {a: new["locks"].get(a, {}).get("packages", {}) for a in ARCHES})
    if old["pip"] != new["pip"]:
        changes.append({"kind": "pip", "name": "pip", "old": old["pip"], "new": new["pip"]})
    changes += diff_versions("python", {n: v for n, (_, v) in old["pins"].items()},
                             {n: v for n, (_, v) in new["pins"].items()})
    return changes


def debian_changes(old: dict[str, dict], new: dict[str, dict]) -> list[dict]:
    """Debian changes of amd64, plus those of the other architectures that differ (named "pkg (arm64)").
    An architecture whose old list is empty (it was added) only counts in the lock file, not as changes."""
    base = diff_versions("debian", old.get("amd64", {}), new.get("amd64", {}))
    seen = {(c["name"], c["old"], c["new"]) for c in base}
    extra = []
    for arch in ARCHES[1:]:
        if not old.get(arch):
            continue
        for c in diff_versions("debian", old[arch], new.get(arch, {})):
            if (c["name"], c["old"], c["new"]) not in seen:
                extra.append(dict(c, name=f"{c['name']} ({arch})"))
    return base + extra


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
    for arch, name in OS_LOCKS.items():
        lock = state["locks"].get(arch) or {}
        if not lock.get("packages"):
            problems.append(f"{name}: no packages locked for {arch} yet – run: python3 scripts/lock.py refresh "
                            "(or Run workflow → relock)")
            continue
        if lock["arch"] != arch:
            problems.append(f"{name}: is for {lock['arch']!r}, expected {arch}")
        if (lock["snapshot"], lock["security"], lock["debian"]) != (state["snapshot"], state["security"], state["debian"]):
            problems.append(f"{name}: snapshot/Debian differ from {OS_LOCK} – set both with scripts/lock.py refresh")
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


def resolve(image: str, snapshot: str, security: str, state: dict, root: Path, arch: str | None = None) -> dict:
    platform = ["--platform", f"linux/{arch}"] if arch else []
    with tempfile.TemporaryDirectory() as work:
        Path(work, "requirements.in").write_text((root / REQS_IN).read_text())
        Path(work, "constraints.txt").write_text(
            "".join(constraint(n, v) + "\n" for n, v in state["pins"].values()))
        out = run(["docker", "run", "--rm", "--pull", "always", *platform,
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


def targets(root: Path = ROOT) -> dict:
    """What all architectures are resolved against: the base image (newest patch, digest of the multi-arch
    index) and the newest completed snapshots of the Debian archive."""
    state = read_state(root)
    major, minor, codename = state["major"], state["minor"], state["codename"]
    patch = pick_base_tag(hub_tags(major, minor), major, minor, codename, state["patch"])
    tag = f"python:{major}.{minor}.{patch}-slim-{codename}"
    digest = digest_of(tag)
    snapshot, security = latest_snapshots()
    return {"patch": patch, "digest": digest, "image": f"{tag}@sha256:{digest}", "snapshot": snapshot,
            "security": security}


def resolve_arch(t: dict, root: Path = ROOT, arch: str | None = None) -> dict:
    """Resolve on this machine (arch None = its own architecture) – or emulated with --platform."""
    res = resolve(t["image"], t["snapshot"], t["security"], read_state(root), root, arch)
    res["pins"] = {k: list(v) for k, v in res["pins"].items()}
    return res


def apply(t: dict, resolved: list[dict], root: Path = ROOT, report_path: str | None = None) -> list[dict]:
    """Write the new versions (Dockerfile, lock files, requirements.txt) – only within the limits, and only if
    every architecture got exactly the same Python packages and pip."""
    state = read_state(root)
    major, minor, codename = state["major"], state["minor"], state["codename"]
    by_arch = {r["arch"]: r for r in resolved}
    missing = [a for a in ARCHES if a not in by_arch]
    if missing:
        raise SystemExit(f"no resolved package list for {', '.join(missing)} – stopped")
    new = by_arch["amd64"]
    new_pins = {k: tuple(v) for k, v in new["pins"].items()}
    for arch, r in by_arch.items():
        if not r["python"].startswith(f"{major}.{minor}."):
            raise SystemExit(f"{arch}: Python {r['python']} is outside the line {major}.{minor} – stopped")
        if not r["debian"].endswith(" " + codename):
            raise SystemExit(f"{arch}: Debian {r['debian']} is not {codename} – stopped")
        if (r["pip"], {k: tuple(v) for k, v in r["pins"].items()}) != (new["pip"], new_pins):
            raise SystemExit(f"{arch} resolved other Python packages than amd64 – stopped (the image must contain "
                             "the same versions on every architecture)")
    for name, (_, version) in new_pins.items():
        if name in state["pins"] and not within_limits(state["pins"][name][1], version, name):
            raise SystemExit(f"{name} {state['pins'][name][1]} -> {version} is outside the limit – stopped")
    if not within_limits(state["pip"], new["pip"], "pip"):
        raise SystemExit(f"pip {state['pip']} -> {new['pip']} is outside the limit – stopped")

    patch = t["patch"]
    changes = []
    if patch != state["patch"]:
        changes.append({"kind": "base", "name": "python",
                        "old": f"{major}.{minor}.{state['patch']}", "new": f"{major}.{minor}.{patch}"})
    old_pkgs = {a: state["locks"].get(a, {}).get("packages", {}) for a in ARCHES}
    new_pkgs = {a: by_arch[a]["packages"] for a in ARCHES}
    changes += debian_changes(old_pkgs, new_pkgs)
    added = [a for a in ARCHES if not old_pkgs[a]]          # e.g. arm64 locked for the first time
    if changes or added:                                    # new base and/or new Debian packages
        dockerfile = (root / DOCKERFILE).read_text()
        dockerfile = BASE_RE.sub("ARG PYTHON_IMAGE=" + base_ref(major, minor, patch, codename, t["digest"]), dockerfile)
        (root / DOCKERFILE).write_text(dockerfile)
        for arch in ARCHES:
            r = by_arch[arch]
            (root / OS_LOCKS[arch]).write_text(render_os_lock(t["snapshot"], t["security"], r["debian"], r["arch"],
                                                              r["packages"]))
    if new["pip"] != state["pip"]:
        changes.append({"kind": "pip", "name": "pip", "old": state["pip"], "new": new["pip"]})
        dockerfile = (root / DOCKERFILE).read_text()
        (root / DOCKERFILE).write_text(PIP_RE.sub(f"ARG PIP_VERSION={new['pip']}", dockerfile))
    py_changes = diff_versions("python", {n: v for n, (_, v) in state["pins"].items()},
                               {n: v for n, (_, v) in new_pins.items()})
    if py_changes or patch != state["patch"]:
        text = (root / REQS).read_text()
        (root / REQS).write_text(render_requirements(text, new_pins, new["python"]))
    changes += py_changes
    if report_path:
        Path(report_path).write_text(json.dumps({"changed": bool(changes), "changes": changes, "added": added},
                                                indent=1) + "\n")
    print(f"{len(changes)} change(s)" if changes else "No new versions – nothing changed.", file=sys.stderr)
    for a in added:
        print(f"  {OS_LOCKS[a]}: locked for the first time", file=sys.stderr)
    for c in changes:
        print(f"  {c['kind']:7} {c['name']}: {c['old'] or '–'} -> {c['new'] or '–'}", file=sys.stderr)
    return changes


def refresh(root: Path = ROOT, report_path: str | None = None) -> list[dict]:
    """All steps on one machine: the other architecture runs emulated (Docker with binfmt/QEMU)."""
    t = targets(root)
    return apply(t, [resolve_arch(t, root, arch) for arch in ARCHES], root, report_path)


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
    t = sub.add_parser("targets", help="base image, digest and snapshots to resolve against (Internet)")
    t.add_argument("--out", required=True)
    v = sub.add_parser("resolve", help="resolve all packages on this machine (Docker)")
    v.add_argument("--targets", required=True)
    v.add_argument("--out", required=True)
    v.add_argument("--arch", help="emulate another architecture (default: this machine's)")
    w = sub.add_parser("apply", help="write the resolved versions of all architectures")
    w.add_argument("--targets", required=True)
    w.add_argument("--resolved", action="append", required=True)
    w.add_argument("--report")
    args = ap.parse_args(argv)
    if args.cmd == "targets":
        Path(args.out).write_text(json.dumps(targets(ROOT), indent=1) + "\n")
        return 0
    if args.cmd == "resolve":
        res = resolve_arch(json.loads(Path(args.targets).read_text()), ROOT, args.arch)
        Path(args.out).write_text(json.dumps(res, indent=1) + "\n")
        print(f"resolved for {res['arch']}: {len(res['packages'])} Debian packages, {len(res['pins'])} Python packages",
              file=sys.stderr)
        return 0
    if args.cmd == "apply":
        os.chdir(ROOT)
        apply(json.loads(Path(args.targets).read_text()), [json.loads(Path(f).read_text()) for f in args.resolved],
              ROOT, args.report)
        return 0
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
