"""Login lockout after failed attempts – stored in the database so it applies to all server processes
and an administrator can lift it in the UI.

8 failed attempts per username and address within 5 minutes lock that combination for
5 minutes (password and second factor are counted separately).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import LoginAttempt, db

MAX_FAILS = 8
WINDOW = timedelta(minutes=5)


def norm(username: str) -> str:
    """Username for lockout keys: as with AD login, without "DOMAIN\\" and "@…", lowercased.

    Otherwise "DOMAIN\\anna", "anna@x" and "ANNA" would each get their own fresh lockout."""
    name = (username or "").strip()
    if "\\" in name:
        name = name.split("\\", 1)[1]
    if "@" in name:
        name = name.split("@", 1)[0]
    return name.strip().lower()


def _since() -> datetime:
    return datetime.now() - WINDOW


def throttled(key: str) -> bool:
    return LoginAttempt.query.filter(LoginAttempt.key == key, LoginAttempt.created_at >= _since()).count() >= MAX_FAILS


def fail(key: str, username: str) -> None:
    db.session.add(LoginAttempt(key=key[:300], username=norm(username)[:200]))
    db.session.commit()


USER_MAX_FAILS = 40
USER_MAX_FAILS_INTERNAL = 100
USER_WINDOW = timedelta(minutes=15)


def user_throttled(username: str, internal: bool = False) -> bool:
    """Additional throttle per username across all addresses (against guessing with changing IPs).

    Always applies; on the internal network with a higher limit, so failed attempts from outside do not
    lock anyone on the local network out as quickly."""
    since = datetime.now() - USER_WINDOW
    limit = USER_MAX_FAILS_INTERNAL if internal else USER_MAX_FAILS
    return LoginAttempt.query.filter(LoginAttempt.username == norm(username),
                                     LoginAttempt.created_at >= since).count() >= limit


def mfa_fails_since(username: str, ts: float) -> int:
    """Failed second-factor attempts for this user since time ts (Unix time)."""
    since = max(datetime.fromtimestamp(ts or 0), datetime.now() - USER_WINDOW)
    return LoginAttempt.query.filter(LoginAttempt.username == norm(username), LoginAttempt.key.like("2fa|%"),
                                     LoginAttempt.created_at >= since).count()


def clear(key: str) -> None:
    LoginAttempt.query.filter_by(key=key).delete()
    db.session.commit()


def locked_usernames() -> set[str]:
    """Usernames currently locked (from at least one address)."""
    from sqlalchemy import func
    rows = (db.session.query(LoginAttempt.username, LoginAttempt.key, func.count(LoginAttempt.id))
            .filter(LoginAttempt.created_at >= _since(), ~LoginAttempt.key.like("secret|%"))
            .group_by(LoginAttempt.username, LoginAttempt.key).all())
    return {u for u, _, n in rows if n >= MAX_FAILS}


def unlock_user(username: str) -> int:
    n = LoginAttempt.query.filter_by(username=norm(username)).delete()
    db.session.commit()
    return n


def cleanup() -> int:
    n = LoginAttempt.query.filter(LoginAttempt.created_at < datetime.now() - timedelta(days=1)).delete()
    db.session.commit()
    return n
