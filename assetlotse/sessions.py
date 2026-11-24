"""Server-side sessions (2.10.0).

Before 2.10.0 a session lived only in the signed cookie: "Sign out" deleted it in the browser, but a copy taken
before (another device, a shared computer, malware) stayed valid until "Sign out everywhere" or a new password.
Now every sign-in creates a row in ``user_sessions`` and the cookie carries its random token
(``<user id>:<auth_epoch>:<token>``, the same value in the "stay signed in" cookie). A request is only signed in
while that row exists:

- signing out (button, inactivity, AD re-check, 2FA rule) deletes the row,
- "Sign out everywhere", a new password, a 2FA reset delete all rows of the account (``User.end_all_sessions``),
- rows unused for longer than the "stay signed in" duration (14 days) are deleted by the scheduler.

Only the SHA-256 of the token is stored, no IP address and no browser. Sessions from before 2.10.0 (cookie without
a token) are no longer accepted: everyone signs in once after the update.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta

from flask import session
from flask_login import user_logged_in, user_logged_out

from .models import User, UserSession, db

log = logging.getLogger(__name__)

MAX_AGE = timedelta(days=14)        # = REMEMBER_COOKIE_DURATION: unused longer → expired
TOUCH_AFTER = timedelta(minutes=5)  # write last_seen at most this often


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(user: User) -> str:
    """Create a session for the user and return the ID for the cookie. Commits."""
    token = secrets.token_urlsafe(32)
    db.session.add(UserSession(user_id=user.id, token_hash=_hash(token)))
    db.session.commit()
    user.session_token = token
    return f"{user.id}:{user.auth_epoch or 0}:{token}"


def load(user_id: str) -> User | None:
    """Flask-Login user loader: valid only with an existing, unexpired session of this account generation."""
    uid, epoch, token = (str(user_id).split(":") + ["", ""])[:3]
    if not uid.isdigit() or not token:
        return None
    user = db.session.get(User, int(uid))
    if user is None or not user.active or str(user.auth_epoch or 0) != (epoch or "0"):
        return None
    row = UserSession.query.filter_by(token_hash=_hash(token)).first()
    now = datetime.now()
    if row is None or row.user_id != user.id or row.last_seen < now - MAX_AGE:
        return None
    if now - row.last_seen > TOUCH_AFTER:
        row.last_seen = now
        db.session.commit()
    user.session_token = token
    return user


def _on_login(sender: object, user: User, **extra: object) -> None:
    """login_user() stored "<id>:<epoch>" – replace it with a new server-side session (also used for the
    "stay signed in" cookie, which Flask-Login writes from session["_user_id"] after the request)."""
    session["_user_id"] = issue(user)


def _on_logout(sender: object, user: User, **extra: object) -> None:
    token = getattr(user, "session_token", None)
    if token:
        UserSession.query.filter_by(token_hash=_hash(token)).delete(synchronize_session=False)
        db.session.commit()
        user.session_token = None


def _oldest(now: datetime, idle_seconds: int) -> datetime:
    """last_seen before this: the session has ended – after MAX_AGE, or earlier through the automatic sign-out
    (which also applies with "Stay signed in"; a browser closed without signing out leaves its row behind)."""
    age = MAX_AGE
    if idle_seconds > 0:
        age = min(age, timedelta(seconds=idle_seconds) + TOUCH_AFTER)   # last_seen is written every TOUCH_AFTER
    return now - age


def _general_idle_seconds() -> int:
    from .idle import _hours, env_default
    return int(_hours("session_idle_hours", env_default()) * 3600)


def count(user: User) -> int:
    """Sessions of the account that can still be used (for "Sign out everywhere")."""
    from .idle import limit_seconds
    oldest = _oldest(datetime.now(), limit_seconds(user, internal=True))
    return UserSession.query.filter(UserSession.user_id == user.id, UserSession.last_seen >= oldest).count()


def cleanup(now: datetime | None = None) -> int:
    """Delete ended sessions and those of deleted accounts (scheduler)."""
    now = now or datetime.now()
    oldest = _oldest(now, _general_idle_seconds())
    n = UserSession.query.filter(UserSession.last_seen < oldest).delete(synchronize_session=False)
    n += UserSession.query.filter(~UserSession.user_id.in_(db.session.query(User.id))).delete(
        synchronize_session=False)
    db.session.commit()
    if n:
        log.debug("%d expired session(s) deleted", n)
    return n


def install(app) -> None:
    user_logged_in.connect(_on_login, app)
    user_logged_out.connect(_on_logout, app)
