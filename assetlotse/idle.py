"""Automatic sign-out after inactivity.

Settings (Administration → Settings → Automatic sign-out):
  session_idle_hours           for everyone (start value: ASSETLOTSE_SESSION_HOURS, otherwise 12; 0 = off)
  session_idle_hours_admin     shorter limit for administrators (empty = same as for everyone)
  session_idle_hours_external  shorter limit for access from outside (empty = same)
  session_warn                 notice in the browser 2 minutes before (default on)
  session_end_on_close         sign out when the browser closes: "Stay signed in" is switched off

The last activity is kept in the signed session cookie ("seen") and checked on every request. With
"Stay signed in" the session cookie is persistent, so the limit also holds after a browser restart.
The limit that applies is the shortest of those that fit (role, internal/external).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from flask import (Blueprint, Flask, current_app, flash, g, has_request_context, jsonify, redirect, request, session,
                   url_for)
from flask_login import current_user, logout_user, user_loaded_from_cookie, user_logged_in

from .i18n import _
from .models import get_bool, get_setting

log = logging.getLogger(__name__)

SEEN = "seen"            # session key: time of the last request that counted as activity (epoch seconds)
REFRESH_AFTER = 30       # write the session cookie again only when "seen" is at least this old
WARN_SECONDS = 120       # the notice appears 2 minutes before
MIN_HOURS, MAX_HOURS = 0.25, 720.0
DEFAULT_HOURS = "12"
# requests that never count as activity (the browser asks how much time is left)
PASSIVE = {"idle.status"}
SKIP = {None, "static", "main.branding_file", "main.site_css"}

bp = Blueprint("idle", __name__)


# ---------------------------------------------------------------- Settings

def env_default() -> str:
    """Start value for everyone: ASSETLOTSE_SESSION_HOURS (if valid), otherwise 12 hours."""
    value = parse_hours(os.environ.get("ASSETLOTSE_SESSION_HOURS"), allow_off=True)
    return value or DEFAULT_HOURS          # None (invalid) or "" (not set) → 12


def parse_hours(raw: object, allow_off: bool = False) -> str | None:
    """Hours as entered ("12", "0,5", "1.5") → normalised text, or None if invalid.
    Empty → "" (not set); 0 only with allow_off."""
    text = str(raw if raw is not None else "").strip().replace(",", ".")
    if not text:
        return ""
    try:
        hours = float(text)
    except ValueError:
        return None
    if hours != hours or hours < 0:        # NaN or negative
        return None
    if hours == 0:
        return "0" if allow_off else None
    if not MIN_HOURS <= hours <= MAX_HOURS:
        return None
    return f"{hours:g}"


def _hours(key: str, default: str = "") -> float:
    try:
        return float(get_setting(key, default) or 0)
    except ValueError:
        return 0.0


def limit_seconds(user: Any, internal: bool) -> int:
    """The inactivity limit that applies to this user and connection (0 = none)."""
    limits = [_hours("session_idle_hours", env_default())]
    if user.is_admin:
        limits.append(_hours("session_idle_hours_admin"))
    if not internal:
        limits.append(_hours("session_idle_hours_external"))
    # 0 for everyone = off; the limits for administrators / outside still apply then
    active = [h for h in limits if h > 0]
    return int(min(active) * 3600) if active else 0


def end_on_close() -> bool:
    return get_bool("session_end_on_close")


def remaining(now: int | None = None) -> int | None:
    """Seconds until the current session is signed out for inactivity (None = no limit)."""
    limit = g.get("idle_limit")
    if not limit:
        return None
    now = int(time.time()) if now is None else now
    return max(0, int(session.get(SEEN) or now) + limit - now)


# ---------------------------------------------------------------- Sign-in / remember cookie

def _on_login(sender: object, user: object, **extra: object) -> None:
    """Every sign-in starts the clock anew. "Stay signed in" → persistent session cookie, so the inactivity
    limit also holds after a browser restart; switched off by the admin → no remember cookie at all."""
    session[SEEN] = int(time.time())
    if end_on_close() and session.get("_remember") == "set":
        session["_remember"] = "clear"
    session.permanent = session.get("_remember") == "set"


def _on_cookie_login(sender: object, user: object, **extra: object) -> None:
    """Signed in again from the remember cookie (session cookie lost)."""
    g.idle_from_cookie = True
    session[SEEN] = int(time.time())
    session.permanent = True


# ---------------------------------------------------------------- Check on every request

def _sign_out(message: str) -> Any:
    session.clear()      # first – logout_user() records in it that the remember cookie is to be deleted
    logout_user()
    flash(message, "info")   # also for the browser's status request: the sign-in page it then opens shows it
    if request.path.startswith("/api/") or request.endpoint in PASSIVE or request.endpoint == "idle.keepalive" \
            or request.accept_mimetypes.best == "application/json":
        return jsonify({"ok": False, "error": _("Please sign in again."), "signed_out": True}), 401
    return redirect(url_for("main.login", next=request.full_path if request.method == "GET" else None))


def check() -> Any:
    g.idle_limit = 0
    if request.endpoint in SKIP or not current_user.is_authenticated:
        return None
    from . import netzone
    if g.pop("idle_from_cookie", False) and end_on_close():
        log.info("Session of %s restored from the remember cookie, but sign-out on closing the browser is on – "
                 "sign in again", current_user.username)
        return _sign_out(_("Please sign in again."))
    limit = limit_seconds(current_user, netzone.is_internal())
    now = int(time.time())
    seen = session.get(SEEN)
    if limit and isinstance(seen, int) and now - seen > limit:
        log.info("Session of %s signed out after %d minutes of inactivity", current_user.username,
                 (now - seen) // 60)
        return _sign_out(_("You have been signed out automatically after a period of inactivity."))
    g.idle_limit = limit
    if end_on_close():
        session.permanent = False
        if request.cookies.get(current_app.config.get("REMEMBER_COOKIE_NAME", "remember_token")):
            session["_remember"] = "clear"     # remember cookie from before the setting: delete it
    if request.endpoint in PASSIVE:
        return None
    if not isinstance(seen, int) or now - seen >= REFRESH_AFTER:
        session[SEEN] = now
    return None


# ---------------------------------------------------------------- For the browser (notice before sign-out)

@bp.route("/sitzung/status")
def status() -> Any:
    """How much time is left – does not count as activity (other tabs may have extended the session)."""
    if not current_user.is_authenticated:
        return jsonify({"ok": False, "signed_out": True}), 401
    return jsonify({"ok": True, "remaining": remaining()})


@bp.route("/sitzung/weiter", methods=["POST"])
def keepalive() -> Any:
    """ "Continue working" – counts as activity."""
    if not current_user.is_authenticated:
        return jsonify({"ok": False, "signed_out": True}), 401
    session[SEEN] = int(time.time())
    return jsonify({"ok": True, "remaining": remaining()})


def page_context() -> dict:
    """Data for base.html: when the page should warn and when it should leave (no limit → nothing)."""
    if not current_user.is_authenticated:
        return {}
    left = remaining()
    if left is None:
        return {}
    return {"idle": {"remaining": left, "warn": WARN_SECONDS if get_bool("session_warn") else 0,
                     "status_url": url_for("idle.status"), "keepalive_url": url_for("idle.keepalive"),
                     "login_url": url_for("main.login")}}


def install(app: Flask) -> None:
    """Register before the blueprints, so the check runs before the other per-request checks."""
    app.before_request(check)
    app.register_blueprint(bp)
    app.context_processor(lambda: {"idle_page": page_context(), "stay_signed_in_offered": not end_on_close()}
                          if has_request_context() else {})
    user_logged_in.connect(_on_login, app)
    user_loaded_from_cookie.connect(_on_cookie_login, app)
