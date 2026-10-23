"""Anmeldesperre nach Fehlversuchen – in der Datenbank, damit sie für alle Server-Prozesse gilt
und ein Administrator sie in der Oberfläche aufheben kann.

8 Fehlversuche je Benutzername und Adresse innerhalb von 5 Minuten sperren diese Kombination für
5 Minuten (Passwort und zweiter Faktor werden getrennt gezählt).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import LoginAttempt, db

MAX_FAILS = 8
WINDOW = timedelta(minutes=5)


def norm(username: str) -> str:
    """Benutzername für Sperrschlüssel: wie bei der AD-Anmeldung ohne „DOMÄNE\\“ und „@…“, klein geschrieben.

    Sonst ergäben „DOMÄNE\\anna“, „anna@x“ und „ANNA“ jeweils eine eigene, frische Sperre."""
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
    """Zusätzliche Bremse je Benutzername über alle Adressen (gegen Raten mit wechselnden IPs).

    Gilt immer; im internen Netz mit höherer Grenze, damit Fehlversuche von außen niemanden im Hausnetz
    so schnell aussperren."""
    since = datetime.now() - USER_WINDOW
    limit = USER_MAX_FAILS_INTERNAL if internal else USER_MAX_FAILS
    return LoginAttempt.query.filter(LoginAttempt.username == norm(username),
                                     LoginAttempt.created_at >= since).count() >= limit


def mfa_fails_since(username: str, ts: float) -> int:
    """Fehlversuche beim zweiten Faktor für diesen Benutzer seit dem Zeitpunkt ts (Unix-Zeit)."""
    since = max(datetime.fromtimestamp(ts or 0), datetime.now() - USER_WINDOW)
    return LoginAttempt.query.filter(LoginAttempt.username == norm(username), LoginAttempt.key.like("2fa|%"),
                                     LoginAttempt.created_at >= since).count()


def clear(key: str) -> None:
    LoginAttempt.query.filter_by(key=key).delete()
    db.session.commit()


def locked_usernames() -> set[str]:
    """Benutzernamen, die gerade (von mindestens einer Adresse aus) gesperrt sind."""
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
