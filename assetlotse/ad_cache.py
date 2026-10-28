"""Zwischenspeicher der AD-Personen für die Personensuche an der Ausgabetheke.

Im Hintergrund (Standard: stündlich) werden alle Personen aus dem Active Directory gelesen, die sich nach den
AD-Regeln anmelden dürften, und in der Tabelle `directory_people` abgelegt – nur Benutzername, Name und E-Mail.
Jeder Abgleich ersetzt den Bestand vollständig: Wer im AD deaktiviert, abgelaufen, gelöscht oder nicht mehr
berechtigt ist, fliegt raus. Schlägt ein Abgleich fehl oder ist er unvollständig, bleibt der alte Stand erhalten
(und es wird nach 15 Minuten erneut versucht).

Sicherheit: Der Zwischenspeicher dient nur zum Finden. „Konto übernehmen“ prüft die Person weiterhin live im AD.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import func, or_, update
from sqlalchemy.orm import selectinload

from .i18n import _
from .models import DirectoryPerson, Setting, User, db, get_bool, get_int, get_setting, set_setting

log = logging.getLogger(__name__)

RETRY = timedelta(minutes=15)
STALE_RUN = timedelta(minutes=30)      # so lange gilt ein laufender Abgleich als belegt (Absturzschutz)
MAX_USERS = 50000
HOURS_CHOICES = (1, 2, 4, 8, 24)


def available() -> bool:
    """AD aktiv und Dienstkonto hinterlegt (ohne Dienstkonto lässt sich das Verzeichnis nicht lesen)."""
    from .ldap_auth import ldap_enabled
    return ldap_enabled() and bool(get_setting("ldap_bind_dn"))


def enabled() -> bool:
    return available() and get_bool("ldap_cache_enabled")


def ready() -> bool:
    """Gab es schon einen erfolgreichen Abgleich? Vorher wird live im AD gesucht."""
    return enabled() and bool(get_setting("ldap_cache_synced_at"))


def interval() -> timedelta:
    hours = get_int("ldap_cache_hours", 1)
    return timedelta(hours=hours if hours in HOURS_CHOICES else 1)


def status() -> dict:
    synced = get_setting("ldap_cache_synced_at")
    return {
        "enabled": enabled(),
        "available": available(),
        "synced_at": datetime.fromisoformat(synced) if synced else None,
        "count": DirectoryPerson.query.count() if synced else 0,
        "error": get_setting("ldap_cache_error"),
        "warning": get_setting("ldap_cache_warning"),
        "pending": _pending(),
        "result": _result(),
        "running": _running(),
        "hours": int(interval().total_seconds() // 3600),
    }


def _pending_data() -> dict:
    try:
        data = json.loads(get_setting("ldap_cache_pending") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _pending() -> list[str]:
    return [str(name) for _id, name in _pending_data().get("users", [])]


def _result() -> dict | None:
    try:
        data = json.loads(get_setting("ldap_cache_result") or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _running() -> bool:
    started = get_setting("ldap_cache_running")
    if not started:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(started) < STALE_RUN
    except ValueError:
        return False


def _claim(now: datetime) -> bool:
    """Atomar „ich gleiche jetzt ab“ – auch bei mehreren Server-Prozessen läuft nur einer."""
    key = "ldap_cache_running"
    if db.session.get(Setting, key) is None:
        try:
            db.session.add(Setting(key=key, value=""))
            db.session.commit()
        except Exception:  # noqa: BLE001 – ein anderer Prozess war schneller
            db.session.rollback()
    stale = (now - STALE_RUN).isoformat(timespec="seconds")
    res = db.session.execute(update(Setting).where(Setting.key == key, or_(Setting.value == "", Setting.value < stale))
                             .values(value=now.isoformat(timespec="seconds")))
    db.session.commit()
    return res.rowcount == 1


def _release() -> None:
    db.session.execute(update(Setting).where(Setting.key == "ldap_cache_running").values(value=""))
    db.session.commit()


def due(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    last = get_setting("ldap_cache_attempt_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    wait = RETRY if get_setting("ldap_cache_error") else interval()
    return now - last_dt >= min(wait, interval())


def sync(force: bool = False, now: datetime | None = None) -> dict | None:
    """Abgleich ausführen, wenn er fällig ist (oder `force`). None = nicht gelaufen (nicht fällig, aus, belegt)."""
    now = now or datetime.now()
    if not enabled() or (not force and not due(now)) or not _claim(now):
        return None
    set_setting("ldap_cache_attempt_at", now.isoformat(timespec="seconds"))
    db.session.commit()
    try:
        from .ldap_auth import Directory
        people, complete = Directory().all_users(max_users=MAX_USERS)
        result, matched = update_accounts(people, complete, now)
        result.update(_store([p for p in people if id(p) not in matched], complete, now))
        pending, duplicates = result.pop("pending"), result.pop("duplicates")
        needs_admin, roles_kept = result.pop("needs_admin"), result.pop("roles_kept")
        warnings = []
        if roles_kept:
            warnings.append(_("Rollen nicht herabgestuft (sonst kein Administrator mehr bzw. viele Konten auf einmal): "
                              "{names} – bitte die AD-Regeln prüfen.", names=", ".join(roles_kept[:20])))
        if needs_admin:
            warnings.append(_("Wieder im AD, aber ohne gespeicherte AD-Kennung – bitte prüfen, ob es dieselbe Person "
                              "ist, und das Konto von Hand aktivieren: {names}", names=", ".join(needs_admin[:20])))
        if not complete:
            warnings.append(_("Die Liste aus dem AD ist unvollständig (Größenlimit des Servers oder mehr als {n} "
                              "Personen) – es wurde nichts gelöscht oder deaktiviert.", n=MAX_USERS))
        if duplicates:
            warnings.append(_("Mehrere Konten gehören zur selben AD-Person: {names} – bitte unter Benutzer bereinigen.",
                              names=", ".join(duplicates[:20])))
        # Warnungen sind keine Fehler: der nächste Abgleich kommt im normalen Takt
        set_setting("ldap_cache_warning", " ".join(warnings))
        set_setting("ldap_cache_pending", json.dumps({"at": now.isoformat(), "users": pending})
                    if pending else "")
        set_setting("ldap_cache_error", "")
        result["pending"] = len(pending)
        set_setting("ldap_cache_result", json.dumps(result))
        set_setting("ldap_cache_synced_at", now.isoformat(timespec="seconds"))
        db.session.commit()
        log.info("AD-Abgleich: %(total)s Personen ohne Konto (%(added)s neu, %(removed)s entfernt); Konten: "
                 "%(updated)s aktualisiert, %(renamed)s umbenannt, %(deactivated)s deaktiviert, "
                 "%(reactivated)s wieder aktiv", result)
        return result
    except Exception as exc:  # noqa: BLE001 – Fehler protokollieren, alter Stand bleibt
        db.session.rollback()
        log.warning("Abgleich des AD-Zwischenspeichers fehlgeschlagen: %s", exc)
        set_setting("ldap_cache_error", str(exc)[:500] or exc.__class__.__name__)
        db.session.commit()
        return {"error": str(exc)}
    finally:
        try:
            _release()
        except Exception:  # noqa: BLE001 – z. B. Datenbank kurz gesperrt: zurücksetzen und noch einmal
            db.session.rollback()
            try:
                _release()
            except Exception:  # noqa: BLE001 – spätestens nach 30 Minuten gilt die Sperre als verwaist
                db.session.rollback()
                log.warning("Sperre des AD-Abgleichs konnte nicht freigegeben werden")


MASS_DEACTIVATION = 0.2   # mehr als 20 % auf einmal: vermutlich falsche Einstellung – erst nach Bestätigung
ROLE_RANK = {"reader": 0, "manager": 1, "admin": 2}


def update_accounts(people, complete: bool, now: datetime | None = None) -> tuple[dict, set[int]]:
    """Vorhandene AD-Konten in AssetLotse mit dem AD abgleichen – auch von Personen, die sich lange nicht
    angemeldet haben (laufende Sitzungen prüft AssetLotse ohnehin alle 4 Stunden).

    Zuordnung Konto ↔ AD-Person über die feste Kennung (objectGUID/entryUUID); nur Konten ohne Kennung (Altkonten)
    über den genauen Benutzernamen. Gepflegt werden Name, E-Mail, DN, Gruppen (mit „Gruppen automatisch zuordnen“),
    Umbenennungen und – nur bei Konten, die sich schon selbst angemeldet haben – die Rolle.

    Schutz vor Fehleinstellungen:
    - Rollen: Würde danach kein aktiver Administrator mehr bleiben oder verlören viele Konten auf einmal Rechte
      (mehr als 20 %, ab 2), werden die Herabstufungen nicht übernommen (Warnung).
    - Deaktivieren (nicht mehr im AD): automatisch nur im Normalbetrieb. Erst nach Bestätigung auf der AD-Seite
      („pending“): Administratoren, solange die Ersteinrichtung nicht bestätigt ist, bei mehr als 20 % (ab 2)
      oder allen Konten, oder wenn danach kein aktiver Administrator bliebe. Bei unvollständiger Liste nie.
    - Wieder aktiv: nur Konten mit gespeicherter Kennung (sonst könnte ein neu vergebener Name das Konto einer
      früheren Person bekommen – das entscheidet dann ein Administrator).
    - Mehrere Konten zu einer Person: das mit passender Kennung (sonst passendem Namen) wird gepflegt, die übrigen
      bleiben unangetastet (Warnung).

    Liefert (Zahlen, ids der Personen aus `people`, die zu einem Konto gehören)."""
    from .models import ROLE_ADMIN, Group
    from .views.main import may_reactivate, sync_ldap_user
    now = now or datetime.now()
    stats = {"updated": 0, "renamed": 0, "deactivated": 0, "reactivated": 0, "duplicates": [], "pending": [],
             "needs_admin": [], "roles_kept": []}
    by_guid = {p.guid: p for p in people if p.guid}
    by_name = {p.username.lower(): p for p in people}
    accounts = (User.query.filter(User.auth_source == "ldap").options(selectinload(User.groups))
                .order_by(User.id).all())
    taken_names = {u.lower() for (u,) in db.session.query(User.username).all()}   # Stand vor dem Abgleich
    ldap_groups = Group.query.filter(Group.ldap_dn != "").all()
    group_sync = get_bool("ldap_group_sync")

    # 1) Konten den Personen zuordnen
    claims: dict[int, list[User]] = {}
    persons: dict[int, object] = {}
    missing: list[User] = []
    account_guids = {u.ldap_guid for u in accounts if u.ldap_guid}   # einmal statt in jeder Runde
    for user in accounts:
        if user.ldap_guid:
            person = by_guid.get(user.ldap_guid)
            if person is None:      # Kennung nicht (mehr) im AD; nur wenn das AD gar keine Kennungen liefert: Name
                cand = by_name.get(user.username.lower())
                person = cand if cand is not None and not cand.guid else None
        else:
            person = by_name.get(user.username.lower())      # Altkonto ohne Kennung: nur genauer Name, kein DN
            if person is not None and person.guid and person.guid in account_guids:
                person = None       # die Person hat schon ein Konto mit ihrer Kennung
        if person is None:
            missing.append(user)
        else:
            claims.setdefault(id(person), []).append(user)
            persons[id(person)] = person

    # 2) je Person genau ein Konto pflegen
    demoted: list[tuple[User, str, bool]] = []
    with db.session.no_autoflush:
        for pid, users in claims.items():
            person = persons[pid]
            same_guid = [u for u in users if person.guid and u.ldap_guid == person.guid]
            same_name = [u for u in users if u.username.lower() == person.username.lower()]
            main = (next((u for u in same_guid if u in same_name), None) or (same_guid or same_name or users)[0])
            for other in users:
                if other is not main:
                    stats["duplicates"].append(other.username)
                    log.warning("AD-Abgleich: Konten %s und %s gehören zur selben AD-Person – %s bleibt unverändert",
                                main.username, other.username, other.username)
            user = main
            if not user.active and not user.ldap_guid and person.guid:
                if user.deactivated_by_sync:                   # gesperrtes Altkonto ohne Kennung: nicht anfassen
                    stats["needs_admin"].append(user.username)
                continue
            before = (user.username, user.display_name, user.email, user.role, sorted(g.id for g in user.groups))
            keep_name = None
            if person.username.lower() != user.username.lower():
                if person.username.lower() in taken_names:
                    log.warning("AD-Abgleich: %s heißt im AD jetzt %s – den Namen gibt es in AssetLotse schon, "
                                "nicht umbenannt", user.username, person.username)
                    keep_name = user.username
                else:
                    stats["renamed"] += 1
            role, manages_all = user.role, bool(user.manages_all)
            reactivate = not user.active and may_reactivate(user)    # vor dem Abgleich (Kennung!) prüfen
            sync_ldap_user(person, user, ldap_groups=ldap_groups, group_sync=group_sync)
            if keep_name:
                user.username = keep_name
            if user.last_login is None:
                user.role, user.manages_all = role, manages_all   # noch nie selbst angemeldet: Rolle bleibt (siehe Kontoübernahme)
            elif ROLE_RANK.get(user.role, 0) < ROLE_RANK.get(role, 0):
                demoted.append((user, role, manages_all))
            if reactivate:
                user.active, user.deactivated_by_sync = True, False
                stats["reactivated"] += 1
            after = (user.username, user.display_name, user.email, user.role, sorted(g.id for g in user.groups))
            if after != before:
                stats["updated"] += 1

    # 3) Rollen: keine Massen-Herabstufung, nie ohne aktiven Administrator
    if demoted:
        signed_in = sum(1 for u in accounts if u.last_login is not None) or 1
        mass = len(demoted) >= 2 and len(demoted) > MASS_DEACTIVATION * signed_in
        db.session.flush()
        no_admin = User.query.filter(User.active.is_(True), User.role == ROLE_ADMIN).count() == 0
        if mass or no_admin:
            for user, old_role, old_all in demoted:
                if mass or old_role == ROLE_ADMIN:
                    user.role, user.manages_all = old_role, old_all
                    stats["roles_kept"].append(user.username)
            log.warning("AD-Abgleich: Herabstufung von %s nicht übernommen (%s) – AD-Regeln prüfen",
                        ", ".join(stats["roles_kept"]), "viele auf einmal" if mass else "kein Administrator mehr")

    # 4) Nicht mehr im AD: deaktivieren – oder erst nach Bestätigung
    active_missing = [u for u in missing if u.active]
    if complete and not active_missing:
        set_setting("ldap_cache_setup_confirmed", "1")      # nichts offen: Ersteinrichtung erledigt
    if complete and active_missing:
        active_total = sum(1 for u in accounts if u.active)
        missing_ids = {u.id for u in active_missing}
        db.session.flush()
        admins_left = User.query.filter(User.active.is_(True), User.role == ROLE_ADMIN,
                                        User.id.notin_(missing_ids)).count()
        confirmed = get_setting("ldap_cache_setup_confirmed") == "1"
        risky = (not confirmed
                 or (len(active_missing) >= 2 and len(active_missing) > MASS_DEACTIVATION * active_total)
                 or len(active_missing) >= active_total
                 or admins_left == 0)
        hold = active_missing if risky else [u for u in active_missing if u.role == ROLE_ADMIN]
        for user in active_missing:
            if user not in hold:
                deactivate(user)
                stats["deactivated"] += 1
        if hold:
            stats["pending"] = [[u.id, u.username] for u in hold]
            log.warning("AD-Abgleich: %s Konten fehlen im AD – warten auf Bestätigung: %s",
                        len(hold), ", ".join(u.username for u in hold[:50]))
    db.session.flush()
    return stats, set(claims)


def deactivate(user: User) -> None:
    log.info("AD-Abgleich: Konto %s deaktiviert (im AD gesperrt, gelöscht oder nicht mehr berechtigt)", user.username)
    user.active, user.deactivated_by_sync = False, True
    user.end_all_sessions()


def confirm_pending(acting_user: User) -> int:
    """Von der AD-Seite bestätigt: die beim letzten Abgleich zurückgehaltenen Konten deaktivieren.

    Nie das eigene Konto, nie den letzten aktiven Administrator und keine Konten, die sich seit dem Abgleich
    angemeldet haben (dann sind sie offensichtlich wieder berechtigt)."""
    from .models import ROLE_ADMIN
    data = _pending_data()
    try:
        at = datetime.fromisoformat(data.get("at") or "")
    except ValueError:
        at = None
    done = 0
    for ident, _name in data.get("users", []):
        user = db.session.get(User, ident)
        if user is None or not user.active or user.auth_source != "ldap" or user.id == acting_user.id:
            continue
        if at is not None and user.last_login is not None and user.last_login > at:
            continue
        if user.role == ROLE_ADMIN and User.query.filter(User.active.is_(True), User.role == ROLE_ADMIN,
                                                         User.id != user.id).count() == 0:
            continue
        deactivate(user)
        done += 1
    set_setting("ldap_cache_pending", "")
    set_setting("ldap_cache_setup_confirmed", "1")
    db.session.commit()
    return done


def _store(people, complete: bool, now: datetime) -> dict:
    """Bestand ersetzen. Konten, die es in AssetLotse schon gibt, braucht der Zwischenspeicher nicht."""
    known = {u.lower() for (u,) in db.session.query(User.username).all()}   # auch lokale Konten gleichen Namens
    wanted = {}
    for p in people:
        key = p.username.strip().lower()
        if key and key not in known:
            wanted[key] = p
    existing = {row.username_key: row for row in DirectoryPerson.query.all()}
    added = removed = 0
    for key, p in wanted.items():
        row = existing.pop(key, None)
        if row is None:
            db.session.add(DirectoryPerson(username=p.username[:200], username_key=key[:200],
                                           display_name=(p.display_name or "")[:300], email=(p.email or "")[:300],
                                           synced_at=now))
            added += 1
        else:
            row.username, row.display_name, row.email, row.synced_at = (
                p.username[:200], (p.display_name or "")[:300], (p.email or "")[:300], now)
    if complete:      # nur bei vollständiger Liste löschen: deaktiviert, gelöscht, nicht mehr berechtigt
        for row in existing.values():
            db.session.delete(row)
            removed += 1
    db.session.flush()
    return {"total": len(wanted), "added": added, "removed": removed}


def clear() -> int:
    """Zwischenspeicher leeren (beim Ausschalten oder wenn AD abgeschaltet wird).

    Vom Abgleich deaktivierte Konten bleiben gesperrt: Sonst tauchten ausgeschiedene Personen sofort wieder an der
    Theke und in Erinnerungsmails auf. Wer wieder berechtigt ist, wird beim nächsten Abgleich bzw. bei der
    Anmeldung über das AD reaktiviert; von Hand geht es unter Benutzer."""
    n = DirectoryPerson.query.delete()
    for key in ("ldap_cache_synced_at", "ldap_cache_attempt_at", "ldap_cache_error", "ldap_cache_result",
                "ldap_cache_warning", "ldap_cache_pending"):
        set_setting(key, "")
    db.session.commit()
    return n


def forget(username: str) -> None:
    """Eine Person entfernen (z. B. nach Kontoübernahme oder wenn das AD sie nicht mehr freigibt)."""
    DirectoryPerson.query.filter(DirectoryPerson.username_key == (username or "").strip().lower()).delete()


def search(query: str, limit: int = 10) -> list[DirectoryPerson]:
    """Im Zwischenspeicher suchen (Benutzername, Name, E-Mail). Bereits vorhandene Konten werden ausgelassen."""
    query = " ".join((query or "").split())[:100]
    if len(query) < 2:
        return []
    esc = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{esc}%"
    accounts = db.session.query(func.lower(User.username))
    return (DirectoryPerson.query
            .filter(or_(DirectoryPerson.username.ilike(like, escape="\\"),
                        DirectoryPerson.display_name.ilike(like, escape="\\"),
                        DirectoryPerson.email.ilike(like, escape="\\")),
                    DirectoryPerson.username_key.notin_(accounts))
            .order_by(DirectoryPerson.display_name, DirectoryPerson.username).limit(limit).all())


def maintain() -> None:
    """Vom Hintergrund-Zeitplan aufgerufen: abgleichen, wenn fällig; ausgeschaltet → Reste entfernen."""
    if enabled():
        sync()
    elif get_setting("ldap_cache_synced_at") or DirectoryPerson.query.first() is not None:
        clear()


def sync_in_background(app) -> None:
    """„Jetzt abgleichen“: im eigenen Thread, damit die Seite nicht wartet."""
    def run():
        with app.app_context():
            try:
                sync(force=True)
            except Exception:  # noqa: BLE001
                log.exception("Abgleich des AD-Zwischenspeichers fehlgeschlagen")
    if app.testing:         # in Tests sofort und im selben Ablauf
        sync(force=True)
        return
    threading.Thread(target=run, name="assetlotse-adcache", daemon=True).start()
