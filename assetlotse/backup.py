"""Automatic database backup (SQLite).

- daily at the configured hour (default 2 am), only once even with several server processes
- while running, via SQLite's online backup API (consistent, without stopping)
- every backup is checked with PRAGMA integrity_check; only then is it considered complete
- the newest N backups are kept (default 10), older ones are deleted
- location: <data directory>/backups or ASSETLOTSE_BACKUP_DIR (e.g. a NAS drive)

With MySQL/MariaDB or PostgreSQL, better back up with the database server's own tools.

Stored formats – keep them German: the file name suffixes ("-manuell", "-vor-<version>",
"-vor-wiederherstellung", "-vor-update", parsed by NAME_RE) and the result messages written to the
settings ("… gesichert (…)", "Sicherung fehlgeschlagen: …" etc., parsed by localized_message).
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from flask import current_app

from .i18n import _
from .models import ROLE_ADMIN, User, db, get_bool, get_int, get_setting, set_setting

log = logging.getLogger(__name__)

PREFIX = "assetlotse-"
NAME_RE = re.compile(r"^assetlotse-(\d{4}-\d{2}-\d{2})_(\d{6})(-vor-\d+\.\d+\.\d+|-[a-z-]*[a-z])?(-\d+)?\.db$")
PART_MAX_AGE = 3600   # delete unfinished *.part files as leftovers of aborted runs only after 1 hour
KIND_LABELS = {"": "automatic", "-manuell": "manual", "-vor-wiederherstellung": "before restore",
               "-vor-update": "before update"}
KEEP_UPGRADE = 3      # "before <version>" backups: the newest three don't count – needed by anyone going back
KEEP_MIN, KEEP_MAX = 1, 100


class BackupError(Exception):
    pass


# ---------------------------------------------------------------- Locations

def backup_dir() -> Path:
    custom = os.environ.get("ASSETLOTSE_BACKUP_DIR")
    d = Path(custom) if custom else Path(current_app.instance_path) / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def database_file() -> Path | None:
    """Path of the SQLite file – or None for other databases or an in-memory database."""
    url = db.engine.url
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database)


def supported() -> bool:
    return database_file() is not None


def human_size(n: int, lang: str | None = None) -> str:
    """File size – decimal separator by language (12,3 MB / 12.3 MB)."""
    if n < 1024 * 1024:
        return f"{max(1, round(n / 1024))} KB"
    if lang is None:
        from .i18n import get_locale
        try:
            lang = get_locale()
        except Exception:  # noqa: BLE001 – outside a request
            lang = "de"
    text = f"{n / 1024 / 1024:.1f} MB"
    return text if lang == "en" else text.replace(".", ",")


def localized_message(msg: str) -> str:
    """Stored message (German) in the current language – for the UI and email.

    The German patterns below are the stored format written by create_backup/run_now/restore_backup – keep."""
    import re
    from .i18n import _, get_locale
    en = get_locale() == "en"
    patterns = [
        (r"^(\S+) gesichert \((.+)\)$", lambda m: _("{name} backed up ({size})", name=m.group(1),
                                                     size=m.group(2).replace(",", ".") if en else m.group(2))),
        (r"^Sicherung fehlgeschlagen: (.*)$", lambda m: _("Backup failed: {error}", error=m.group(1))),
        (r"^Datenbankdatei (.+) nicht gefunden\.$", lambda m: _("Database file {path} not found.", path=m.group(1))),
        (r"^Sicherung (\S+) nicht gefunden\.$", lambda m: _("Backup {name} not found.", name=m.group(1))),
        (r"^Sicherung (\S+) ist beschädigt\.$", lambda m: _("Backup {name} is damaged.", name=m.group(1))),
    ]
    for pat, fn in patterns:
        m = re.match(pat, msg or "", re.S)
        if m:
            return fn(m)
    return _(msg) if msg else msg


def keep_count() -> int:
    return min(KEEP_MAX, max(KEEP_MIN, get_int("backup_keep", 10)))


# ---------------------------------------------------------------- Back up

def _integrity_ok(path: Path) -> bool:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def create_backup(kind: str = "", now: datetime | None = None, *, rotate_after: bool = True) -> Path:
    """Create a backup; kind: '' (automatic), '-manuell' (manual), '-vor-wiederherstellung' (before restore)."""
    src_path = database_file()
    if src_path is None:
        raise BackupError("Automatic backups are only available for SQLite.")
    if not src_path.exists():
        raise BackupError(f"Datenbankdatei {src_path} nicht gefunden.")   # stored format – parsed by localized_message, keep
    now = now or datetime.now()
    target_dir = backup_dir()
    name = f"{PREFIX}{now:%Y-%m-%d_%H%M%S}{kind}.db"
    final = target_dir / name
    n = 1
    while final.exists():  # two backups in the same second
        final = target_dir / name.replace(".db", f"-{n}.db")
        n += 1
    part = final.with_suffix(".part")
    src = sqlite3.connect(str(src_path), timeout=30)
    dst = sqlite3.connect(str(part))
    try:
        src.backup(dst)
        # The copy inherits the source's WAL mode – store it as a standalone file without -wal/-shm
        dst.execute("PRAGMA journal_mode=DELETE").fetchone()
    except sqlite3.Error as exc:
        dst.close()
        _unlink_part(part)
        raise BackupError(f"Sicherung fehlgeschlagen: {exc}") from exc   # stored format – parsed by localized_message, keep
    finally:
        src.close()
    dst.close()
    _unlink_siblings(part)
    if not _integrity_ok(part):
        _unlink_part(part)
        raise BackupError("The backup is damaged (integrity_check) and was discarded.")
    _unlink_siblings(part)
    try:
        os.chmod(part, 0o600)  # contains personal data
    except OSError:
        pass
    os.replace(part, final)
    if rotate_after:
        rotate()
    return final


def _unlink_siblings(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _unlink_part(path: Path) -> None:
    path.unlink(missing_ok=True)
    _unlink_siblings(path)


def list_backups() -> list[dict]:
    items = []
    for p in backup_dir().iterdir():
        m = NAME_RE.match(p.name)
        if not m or not p.is_file():
            continue
        created = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y-%m-%d_%H%M%S")
        kind = m.group(3) or ""
        seq = int(m.group(4)[1:]) if m.group(4) else 0   # "-1", "-2" …: later in the same second
        upgrade = kind == "-vor-update" or bool(re.match(r"-vor-\d", kind))
        items.append({"name": p.name, "path": p, "size": p.stat().st_size, "created": created,
                      "kind": KIND_LABELS["-vor-update"] if upgrade else KIND_LABELS.get(kind, "manual"),
                      "upgrade": upgrade, "_seq": seq})
    items.sort(key=lambda x: (x["created"], x["_seq"]), reverse=True)
    return items


def rotate(keep: int | None = None) -> int:
    keep = keep_count() if keep is None else keep
    removed = 0
    items = list_backups()
    protected = {i["name"] for i in [i for i in items if i["upgrade"]][:KEEP_UPGRADE]}
    for item in [i for i in items if i["name"] not in protected][keep:]:
        try:
            item["path"].unlink()
            removed += 1
        except OSError as exc:
            log.warning("Old backup %s could not be deleted: %s", item["name"], exc)
    # Leftovers of aborted runs – but not what another process is still writing
    limit = time.time() - PART_MAX_AGE
    for stale in backup_dir().glob(f"{PREFIX}*.part*"):
        try:
            if stale.is_file() and stale.stat().st_mtime < limit:
                stale.unlink()
        except OSError:
            pass
    return removed


def find_backup(name: str) -> Path | None:
    if not NAME_RE.match(name or ""):
        return None
    p = backup_dir() / name
    return p if p.is_file() else None


def delete_backup(name: str) -> bool:
    p = find_backup(name)
    if p is None:
        return False
    p.unlink()
    return True


# ---------------------------------------------------------------- Restore (only with the app stopped!)

def restore_backup(name: str) -> Path | None:
    """Restore a backup. The current state is backed up first ("before restore")."""
    src = find_backup(name)
    # stored/displayed format – parsed by localized_message, keep
    if src is None:
        raise BackupError(f"Sicherung {name} nicht gefunden.")
    if not _integrity_ok(src):
        raise BackupError(f"Sicherung {name} ist beschädigt.")
    target = database_file()
    if target is None:
        raise BackupError("Restoring only works with SQLite.")
    # without rotation – otherwise the very backup being restored could be deleted
    safety = create_backup("-vor-wiederherstellung", rotate_after=False) if target.exists() else None
    db.session.remove()
    db.engine.dispose()
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    t = sqlite3.connect(str(target), timeout=30)
    try:
        s.backup(t)
    finally:
        s.close()
        t.close()
    return safety


# ---------------------------------------------------------------- Schedule and status

def _record(ok: bool, message: str, now: datetime) -> None:
    set_setting("backup_last_result", f"{now:%d.%m.%Y %H:%M}: {message}")
    if ok:
        set_setting("backup_last_ok", now.isoformat(timespec="seconds"))
        set_setting("backup_last_error", "")
    else:
        set_setting("backup_last_error", now.isoformat(timespec="seconds"))
    db.session.commit()


def run_now(kind: str = "-manuell", now: datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.now()
    try:
        path = create_backup(kind, now)
        size = path.stat().st_size if path.exists() else 0
    except Exception as exc:  # noqa: BLE001
        log.exception("Backup failed")
        # stored format (backup_last_result) – parsed by localized_message, keep
        msg = str(exc) if isinstance(exc, BackupError) else f"Sicherung fehlgeschlagen: {exc}"
        _record(False, msg, now)
        return False, msg
    msg = f"{path.name} gesichert ({human_size(size)})"   # stored format – parsed by localized_message, keep
    _record(True, msg, now)
    log.info("Backup: %s", msg)
    return True, msg


def run_daily(now: datetime | None = None) -> tuple[bool, str] | None:
    """Called by the scheduler (every few minutes); backs up once a day from the configured hour on."""
    from .mailer import _claim_today
    now = now or datetime.now()
    if not get_bool("backup_enabled") or not supported() or now.hour < get_int("backup_hour", 2):
        return None
    if not _claim_today(now.date().isoformat(), key="backup_last_run"):
        return None
    ok, msg = run_now("", now)
    if not ok:
        _notify_admins(msg)
    return ok, msg


def warning() -> str:
    """Short warning for admins if the last backup failed."""
    if not get_bool("backup_enabled") or not get_setting("backup_last_error"):
        return ""
    if get_setting("backup_last_ok") > get_setting("backup_last_error"):
        return ""
    return _("The last backup failed.")


def _notify_admins(message: str) -> None:
    from .mailer import mail_active, render_mail, send_mail
    if not mail_active():
        return
    for admin in User.query.filter_by(role=ROLE_ADMIN, active=True):
        if admin.email:
            try:
                # Translate the message only in the recipient's language (render_mail sets it)
                subject, text, html = render_mail("backup_failed", user=admin, message=message,
                                                  localize=localized_message)
                send_mail(admin.email, subject, text, html, kind="backup", user_id=admin.id)
            except Exception:  # noqa: BLE001
                log.exception("Notice about the failed backup could not be sent")
