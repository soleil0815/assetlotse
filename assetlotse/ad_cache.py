"""Cache of AD people for the people search at the checkout desk.

In the background (default: hourly) all people from Active Directory who would be allowed to log in under the
AD rules are read and stored in the table `directory_people` – only username, name and email.
Each sync fully replaces the stored set: anyone disabled, expired, deleted or no longer authorized in AD
is removed. If a sync fails or is incomplete, the previous state is kept
(and it is retried after 15 minutes).

Security: the cache is only used for finding people. "Take over account" still checks the person live in AD.
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
STALE_RUN = timedelta(minutes=30)      # a running sync counts as taken for this long (crash protection)
MAX_USERS = 50000
HOURS_CHOICES = (1, 2, 4, 8, 24)


def available() -> bool:
    """AD enabled and service account configured (without one the directory cannot be read)."""
    from .ldap_auth import ldap_enabled
    return ldap_enabled() and bool(get_setting("ldap_bind_dn"))


def enabled() -> bool:
    return available() and get_bool("ldap_cache_enabled")


def ready() -> bool:
    """Has there been a successful sync yet? Until then, searches go live to AD."""
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
    """Atomically claim "I am syncing now" – only one runs, even with several server processes."""
    key = "ldap_cache_running"
    if db.session.get(Setting, key) is None:
        try:
            db.session.add(Setting(key=key, value=""))
            db.session.commit()
        except Exception:  # noqa: BLE001 – another process was faster
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
    """Run the sync if it is due (or `force`). None = did not run (not due, disabled, taken)."""
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
            warnings.append(_("Roles not downgraded (otherwise no administrator would remain, or "
                              "many accounts at once): {names} – please check the AD rules.", names=", ".join(roles_kept[:20])))
        if needs_admin:
            warnings.append(_("Back in AD, but without a stored AD identifier – please check whether "
                              "it is the same person and activate the account by hand: {names}", names=", ".join(needs_admin[:20])))
        if not complete:
            warnings.append(_("The list from AD is incomplete (server size limit or more "
                              "than {n} people) – nothing was deleted or deactivated.", n=MAX_USERS))
        if duplicates:
            warnings.append(_("Several accounts belong to the same AD person: {names} – please clean up under Users.",
                              names=", ".join(duplicates[:20])))
        # warnings are not errors: the next sync runs on the normal schedule
        set_setting("ldap_cache_warning", " ".join(warnings))
        set_setting("ldap_cache_pending", json.dumps({"at": now.isoformat(), "users": pending})
                    if pending else "")
        set_setting("ldap_cache_error", "")
        result["pending"] = len(pending)
        set_setting("ldap_cache_result", json.dumps(result))
        set_setting("ldap_cache_synced_at", now.isoformat(timespec="seconds"))
        db.session.commit()
        log.info("AD sync: %(total)s people without an account (%(added)s new, %(removed)s removed); accounts: "
                 "%(updated)s updated, %(renamed)s renamed, %(deactivated)s deactivated, "
                 "%(reactivated)s reactivated", result)
        return result
    except Exception as exc:  # noqa: BLE001 – log the error, previous state stays
        db.session.rollback()
        log.warning("AD cache sync failed: %s", exc)
        set_setting("ldap_cache_error", str(exc)[:500] or exc.__class__.__name__)
        db.session.commit()
        return {"error": str(exc)}
    finally:
        try:
            _release()
        except Exception:  # noqa: BLE001 – e.g. database briefly locked: roll back and try again
            db.session.rollback()
            try:
                _release()
            except Exception:  # noqa: BLE001 – after 30 minutes at the latest the lock counts as orphaned
                db.session.rollback()
                log.warning("Could not release the AD sync lock")


MASS_DEACTIVATION = 0.2   # more than 20 % at once: probably a misconfiguration – only after confirmation
ROLE_RANK = {"reader": 0, "manager": 1, "admin": 2}


def update_accounts(people, complete: bool, now: datetime | None = None) -> tuple[dict, set[int]]:
    """Sync existing AD accounts in AssetLotse with AD – including people who have not logged in
    for a long time (AssetLotse re-checks running sessions every 4 hours anyway).

    Account ↔ AD person is matched via the stable ID (objectGUID/entryUUID); only accounts without an ID
    (legacy accounts) via the exact username. Maintained are name, email, DN, groups (with "Assign groups
    automatically"), renames and – only for accounts that have logged in themselves – the role.

    Protection against misconfiguration:
    - Roles: if no active administrator would remain, or many accounts would lose permissions at once
      (more than 20 %, at least 2), the downgrades are not applied (warning).
    - Deactivation (no longer in AD): automatic only in normal operation. Only after confirmation on the AD page
      ("pending"): administrators, as long as the initial setup is not confirmed, more than 20 % (at least 2)
      or all accounts, or if no active administrator would remain. Never with an incomplete list.
    - Reactivation: only accounts with a stored ID (otherwise a reassigned name could get the account of a
      former person – an administrator decides that).
    - Several accounts for one person: the one with a matching ID (otherwise matching name) is maintained,
      the others stay untouched (warning).

    Returns (counts, ids of the persons from `people` that belong to an account)."""
    from .models import ROLE_ADMIN, Group
    from .views.main import may_reactivate, sync_ldap_user
    now = now or datetime.now()
    stats = {"updated": 0, "renamed": 0, "deactivated": 0, "reactivated": 0, "duplicates": [], "pending": [],
             "needs_admin": [], "roles_kept": []}
    by_guid = {p.guid: p for p in people if p.guid}
    by_name = {p.username.lower(): p for p in people}
    accounts = (User.query.filter(User.auth_source == "ldap").options(selectinload(User.groups))
                .order_by(User.id).all())
    taken_names = {u.lower() for (u,) in db.session.query(User.username).all()}   # state before the sync
    ldap_groups = Group.query.filter(Group.ldap_dn != "").all()
    group_sync = get_bool("ldap_group_sync")

    # 1) Match accounts to people
    claims: dict[int, list[User]] = {}
    persons: dict[int, object] = {}
    missing: list[User] = []
    account_guids = {u.ldap_guid for u in accounts if u.ldap_guid}   # once instead of in every iteration
    for user in accounts:
        if user.ldap_guid:
            person = by_guid.get(user.ldap_guid)
            if person is None:      # ID not (or no longer) in AD; only if AD returns no IDs at all: name
                cand = by_name.get(user.username.lower())
                person = cand if cand is not None and not cand.guid else None
        else:
            person = by_name.get(user.username.lower())      # legacy account without ID: exact name only, no DN
            if person is not None and person.guid and person.guid in account_guids:
                person = None       # the person already has an account with their ID
        if person is None:
            missing.append(user)
        else:
            claims.setdefault(id(person), []).append(user)
            persons[id(person)] = person

    # 2) Maintain exactly one account per person
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
                    log.warning("AD sync: accounts %s and %s belong to the same AD person – %s stays unchanged",
                                main.username, other.username, other.username)
            user = main
            if not user.active and not user.ldap_guid and person.guid:
                if user.deactivated_by_sync:                   # locked legacy account without ID: do not touch
                    stats["needs_admin"].append(user.username)
                continue
            before = (user.username, user.display_name, user.email, user.role, sorted(g.id for g in user.groups))
            keep_name = None
            if person.username.lower() != user.username.lower():
                if person.username.lower() in taken_names:
                    log.warning("AD sync: %s is now called %s in AD – that name already exists in AssetLotse, "
                                "not renamed", user.username, person.username)
                    keep_name = user.username
                else:
                    stats["renamed"] += 1
            role, manages_all = user.role, bool(user.manages_all)
            reactivate = not user.active and may_reactivate(user)    # check before the sync (ID!)
            sync_ldap_user(person, user, ldap_groups=ldap_groups, group_sync=group_sync)
            if keep_name:
                user.username = keep_name
            if user.last_login is None:
                user.role, user.manages_all = role, manages_all   # never logged in themselves: role stays (see account takeover)
            elif ROLE_RANK.get(user.role, 0) < ROLE_RANK.get(role, 0):
                demoted.append((user, role, manages_all))
            if reactivate:
                user.active, user.deactivated_by_sync = True, False
                stats["reactivated"] += 1
            after = (user.username, user.display_name, user.email, user.role, sorted(g.id for g in user.groups))
            if after != before:
                stats["updated"] += 1

    # 3) Roles: no mass downgrade, never without an active administrator
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
            log.warning("AD sync: downgrade of %s not applied (%s) – check the AD rules",
                        ", ".join(stats["roles_kept"]), "many at once" if mass else "no administrator left")

    # 4) No longer in AD: deactivate – or only after confirmation
    active_missing = [u for u in missing if u.active]
    if complete and not active_missing:
        set_setting("ldap_cache_setup_confirmed", "1")      # nothing pending: initial setup done
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
            log.warning("AD sync: %s accounts missing in AD – awaiting confirmation: %s",
                        len(hold), ", ".join(u.username for u in hold[:50]))
    db.session.flush()
    return stats, set(claims)


def deactivate(user: User) -> None:
    log.info("AD sync: account %s deactivated (disabled, deleted or no longer authorized in AD)", user.username)
    user.active, user.deactivated_by_sync = False, True
    user.end_all_sessions()


def confirm_pending(acting_user: User) -> int:
    """Confirmed on the AD page: deactivate the accounts held back by the last sync.

    Never the acting user's own account, never the last active administrator and no accounts that have
    logged in since the sync (they are then obviously authorized again)."""
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
    """Replace the stored set. Accounts that already exist in AssetLotse are not needed in the cache."""
    known = {u.lower() for (u,) in db.session.query(User.username).all()}   # also local accounts with the same name
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
    if complete:      # delete only with a complete list: disabled, deleted, no longer authorized
        for row in existing.values():
            db.session.delete(row)
            removed += 1
    db.session.flush()
    return {"total": len(wanted), "added": added, "removed": removed}


def clear() -> int:
    """Empty the cache (when it is switched off or AD is disabled).

    Accounts deactivated by the sync stay locked: otherwise people who have left would immediately reappear at the
    desk and in reminder emails. Anyone authorized again is reactivated on the next sync or on login via AD;
    manually it can be done under Users."""
    n = DirectoryPerson.query.delete()
    for key in ("ldap_cache_synced_at", "ldap_cache_attempt_at", "ldap_cache_error", "ldap_cache_result",
                "ldap_cache_warning", "ldap_cache_pending"):
        set_setting(key, "")
    db.session.commit()
    return n


def forget(username: str) -> None:
    """Remove a person (e.g. after account takeover or when AD no longer allows them)."""
    DirectoryPerson.query.filter(DirectoryPerson.username_key == (username or "").strip().lower()).delete()


def search(query: str, limit: int = 10) -> list[DirectoryPerson]:
    """Search the cache (username, name, email). Existing accounts are left out."""
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
    """Called by the background scheduler: sync if due; switched off → remove leftovers."""
    if enabled():
        sync()
    elif get_setting("ldap_cache_synced_at") or DirectoryPerson.query.first() is not None:
        clear()


def sync_in_background(app) -> None:
    """"Sync now": in its own thread so the page does not wait."""
    def run():
        with app.app_context():
            try:
                sync(force=True)
            except Exception:  # noqa: BLE001
                log.exception("AD cache sync failed")
    if app.testing:         # in tests immediately and in the same flow
        sync(force=True)
        return
    threading.Thread(target=run, name="assetlotse-adcache", daemon=True).start()
