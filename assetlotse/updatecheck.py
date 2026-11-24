"""Update check (2.9.0): is a newer version published?

Once a day the scheduler asks GitHub for the newest release of this program (the public address the release page
uses as well). Nothing about the installation is sent – GitHub only sees the server's IP address, as with every
``docker pull``. Administrators then see a notice at the top of the page.

Switched off with the setting ``update_check`` (Settings → General) or for good with ``ASSETLOTSE_UPDATE_CHECK=0``
in ``.env`` (servers without internet access). Errors (offline, firewall, unexpected answer) never reach the users:
they are kept for the diagnostics page and the program log.

Only program versions count (``2.9.0``). The monthly security images (``2.8.1-s1``) carry the same program and reach
everyone who follows ``latest`` anyway – they are not announced: their release (marked "Latest" on GitHub after the
patch day) counts as its program version (``v2.9.0-s1`` → ``2.9.0``).
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime

from . import DEFAULT_SOURCE_URL, __version__

log = logging.getLogger(__name__)

TIMEOUT = 10                 # seconds for the request to GitHub
MAX_ANSWER = 512 * 1024      # never read more than this (the answer is a few kilobytes)
_VERSION = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
_SECURITY_TAG = re.compile(r"^(v?\d{1,4}\.\d{1,4}\.\d{1,4})-s\d{1,4}$")
_REPO = re.compile(r"^https://github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$")


def parse_version(value) -> tuple[int, int, int] | None:
    """'2.9.0' or 'v2.9.0' → (2, 9, 0); anything else (also '2.8.1-s1', pre-releases) → None."""
    m = _VERSION.match(str(value or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


def repository() -> str:
    """owner/name on GitHub: this repository – or the one of a modified version (ASSETLOTSE_SOURCE_URL)."""
    m = _REPO.match((os.environ.get("ASSETLOTSE_SOURCE_URL") or "").strip() or DEFAULT_SOURCE_URL)
    if not m:
        m = _REPO.match(DEFAULT_SOURCE_URL)
    return f"{m.group(1)}/{m.group(2)}"


def disabled_by_env() -> bool:
    return (os.environ.get("ASSETLOTSE_UPDATE_CHECK") or "").strip().lower() in ("0", "false", "no", "off")


def enabled() -> bool:
    from .models import get_bool
    return not disabled_by_env() and get_bool("update_check")


def release_url(version: str) -> str:
    """Built here, never taken from the answer: always a link to the release page of the repository."""
    return f"https://github.com/{repository()}/releases/tag/v{version}"


def fetch_latest() -> str:
    """Newest published version on GitHub, e.g. '2.9.0'. Raises RuntimeError with a short English reason."""
    url = f"https://api.github.com/repos/{repository()}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"AssetLotse/{__version__} (update check)",
        "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 – fixed https URL
            raw = resp.read(MAX_ANSWER + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub answered HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"GitHub not reachable ({str(reason)[:120]})") from None
    if len(raw) > MAX_ANSWER:
        raise RuntimeError("Unexpected answer from GitHub (too large)")
    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError("Unexpected answer from GitHub (no JSON)") from None
    tag = str(data.get("tag_name") or "").strip() if isinstance(data, dict) else ""
    m = _SECURITY_TAG.match(tag)          # security update of a program version (after every patch day)
    version = parse_version(m.group(1) if m else tag)
    if version is None or (isinstance(data, dict) and (data.get("draft") or data.get("prerelease"))):
        raise RuntimeError("Unexpected answer from GitHub (no version)")
    return ".".join(str(x) for x in version)


def error_text(error: str) -> str:
    """The stored (English) reason of a failed check, as a sentence in the user's language."""
    from .i18n import gettext as _
    error = str(error or "")
    m = re.fullmatch(r"GitHub answered HTTP (\d{3})", error)
    if m:
        if m.group(1) in ("403", "429"):
            return _("GitHub is limiting requests right now (HTTP {code}) – the next check tries again.",
                     code=m.group(1))
        if m.group(1) == "404":
            return _("GitHub does not know the repository or it has no release yet (HTTP 404).")
        return _("GitHub answered with an error (HTTP {code}) – the next check tries again.", code=m.group(1))
    m = re.fullmatch(r"GitHub not reachable \((.*)\)", error, re.S)
    if m:
        return _("The server cannot reach GitHub ({reason}). Without internet access, switch the check off with "
                 "ASSETLOTSE_UPDATE_CHECK=0 in .env.", reason=m.group(1))
    if error.startswith("Unexpected answer from GitHub"):
        return _("GitHub sent an unexpected answer – the next check tries again.")
    return error


def check_now(now: datetime | None = None) -> tuple[bool, str]:
    """Ask GitHub and remember the answer. Returns (ok, newest version or error text)."""
    from .models import db, set_setting
    now = now or datetime.now()
    set_setting("update_checked_at", now.isoformat(timespec="seconds"))
    try:
        latest = fetch_latest()
    except RuntimeError as exc:
        set_setting("update_error", str(exc))
        db.session.commit()
        log.warning("Update check: %s", exc)
        return False, str(exc)
    set_setting("update_latest", latest)
    set_setting("update_error", "")
    db.session.commit()
    if is_newer(latest):
        log.info("Update check: version %s is available (running %s)", latest, __version__)
    else:
        log.debug("Update check: %s is up to date", __version__)
    return True, latest


def run_daily(now: datetime | None = None) -> tuple[bool, str] | None:
    """Called by the scheduler (every few minutes): once a day, the first time shortly after the start."""
    from .mailer import _claim_today
    if not enabled():
        return None
    now = now or datetime.now()
    if not _claim_today(now.date().isoformat(), key="update_check_last_run"):
        return None
    return check_now(now)


def is_newer(latest: str, running: str = __version__) -> bool:
    new, cur = parse_version(latest), parse_version(running)
    return bool(new and cur and new > cur)


def available() -> str:
    """Newer version for the notice to administrators – '' if none, switched off or hidden for this version."""
    from .models import get_setting
    if not enabled():
        return ""
    latest = get_setting("update_latest")
    if not is_newer(latest) or get_setting("update_dismissed") == latest:
        return ""
    return latest


def pinned_tag() -> str:
    """A fixed version in .env (ASSETLOTSE_TAG=2.8.1): then docker compose pull alone does not update."""
    tag = (os.environ.get("ASSETLOTSE_TAG") or "").strip()
    return tag if tag and tag != "latest" else ""


def status() -> dict:
    """For the diagnostics page and the diagnostic package."""
    from .models import get_bool, get_setting
    latest = get_setting("update_latest")
    return {"enabled": enabled(), "disabled_by_env": disabled_by_env(), "setting": get_bool("update_check"),
            "running": __version__, "latest": latest or None, "newer": is_newer(latest),
            "checked_at": get_setting("update_checked_at") or None, "error": get_setting("update_error") or None,
            "dismissed": get_setting("update_dismissed") or None, "pinned_tag": pinned_tag() or None,
            "repository": repository()}
