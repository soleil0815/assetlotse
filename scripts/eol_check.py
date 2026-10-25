#!/usr/bin/env python3
"""Base check: how long do the Debian release and the Python line of the image still get security updates?

Reads the base from the Dockerfile (python:X.Y.Z-slim-<codename>) and build/debian-packages.lock
(# debian: <version> <codename>) and asks https://endoflife.date (API v1). Then:
  more than 12 months left  -> note
  12 months or less         -> warning (the run stays green)
  6 months or less          -> error (the run turns red)
Nobody raises the base silently: a new Python line or Debian release is a deliberate change.

  python3 scripts/eol_check.py [--data file.json] [--today YYYY-MM-DD]
(--data: offline answers {"python/3.13": {...}, "debian/13": {...}} – used by the tests)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WARN_DAYS = 365
ERROR_DAYS = 183
API = "https://endoflife.date/api/v1/products/{product}/releases/{cycle}"


def base(root: Path = ROOT) -> list[tuple[str, str, str]]:
    df = (root / "Dockerfile").read_text()
    m = re.search(r"^ARG PYTHON_IMAGE=python:(\d+)\.(\d+)\.\d+-slim-([a-z]+)@", df, re.M)
    if not m:
        raise SystemExit("Dockerfile: ARG PYTHON_IMAGE=python:X.Y.Z-slim-<codename>@sha256:… not found")
    lock = (root / "build" / "debian-packages.lock").read_text()
    d = re.search(r"^# debian: (\d+) ([a-z]+)$", lock, re.M)
    if not d:
        raise SystemExit("build/debian-packages.lock: '# debian: <version> <codename>' not found")
    if d.group(2) != m.group(3):
        raise SystemExit(f"Debian {d.group(2)} in the lock file does not match the base image ({m.group(3)})")
    return [("python", f"{m.group(1)}.{m.group(2)}", f"Python {m.group(1)}.{m.group(2)}"),
            ("debian", d.group(1), f"Debian {d.group(1)} ({d.group(2)})")]


def eol_date(answer: dict) -> dt.date | None:
    """endoflife.date v1 answers {"result": {"eolFrom": "2029-10-31", ...}} (older form: {"eol": ...})."""
    r = answer.get("result", answer)
    value = r.get("eolFrom", r.get("eol"))
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return dt.date.fromisoformat(value)
    return None


def fetch(product: str, cycle: str) -> dict:
    req = urllib.request.Request(API.format(product=product, cycle=cycle),
                                 headers={"User-Agent": "assetlotse-eol-check", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def evaluate(items, answers: dict, today: dt.date) -> tuple[int, list[str]]:
    status, lines = 0, []
    for product, cycle, label in items:
        end = eol_date(answers[f"{product}/{cycle}"])
        if end is None:
            lines.append(f"::error title=Base::{label}: no end date for security updates found on endoflife.date")
            status = 1
            continue
        days = (end - today).days
        text = f"{label}: security updates until {end.isoformat()} ({days} days)"
        if days <= ERROR_DAYS:
            lines.append(f"::error title=Base ends soon::{text} – plan the switch to a newer "
                         f"{'Python line' if product == 'python' else 'Debian release'} now (see README)")
            status = 1
        elif days <= WARN_DAYS:
            lines.append(f"::warning title=Base ends within a year::{text} – plan the switch")
        else:
            lines.append(f"::notice title=Base::{text}")
    return status, lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data")
    ap.add_argument("--today")
    a = ap.parse_args(argv)
    items = base(ROOT)
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    answers = json.loads(Path(a.data).read_text()) if a.data else {f"{p}/{c}": fetch(p, c) for p, c, _ in items}
    status, lines = evaluate(items, answers, today)
    print("\n".join(lines))
    return status


if __name__ == "__main__":
    sys.exit(main())
