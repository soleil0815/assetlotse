"""Automatische Datensicherung der Datenbank (SQLite).

- täglich zur eingestellten Uhrzeit (Standard 2 Uhr), auch bei mehreren Server-Prozessen nur einmal
- im laufenden Betrieb über die Online-Backup-Funktion von SQLite (konsistent, ohne Anhalten)
- jede Sicherung wird mit PRAGMA integrity_check geprüft, erst dann gilt sie als fertig
- es werden die neuesten N Sicherungen aufbewahrt (Standard 10), ältere werden gelöscht
- Ablage: <Datenverzeichnis>/backups oder ASSETLOTSE_BACKUP_DIR (z. B. ein NAS-Laufwerk)

Bei MySQL/MariaDB oder PostgreSQL sichert man besser mit den Werkzeugen des Datenbankservers.
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
NAME_RE = re.compile(r"^assetlotse-(\d{4}-\d{2}-\d{2})_(\d{6})(-[a-z-]*[a-z])?(-\d+)?\.db$")
PART_MAX_AGE = 3600   # unfertige *.part-Dateien erst nach 1 Stunde als Reste abgebrochener Läufe löschen
KIND_LABELS = {"": "automatisch", "-manuell": "manuell", "-vor-wiederherstellung": "vor Wiederherstellung"}
KEEP_MIN, KEEP_MAX = 1, 100


class BackupError(Exception):
    pass


# ---------------------------------------------------------------- Orte

def backup_dir() -> Path:
    custom = os.environ.get("ASSETLOTSE_BACKUP_DIR")
    d = Path(custom) if custom else Path(current_app.instance_path) / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def database_file() -> Path | None:
    """Pfad der SQLite-Datei – oder None bei anderen Datenbanken bzw. In-Memory-Datenbank."""
    url = db.engine.url
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database)


def supported() -> bool:
    return database_file() is not None


def human_size(n: int, lang: str | None = None) -> str:
    """Dateigröße – Dezimalzeichen nach Sprache (12,3 MB / 12.3 MB)."""
    if n < 1024 * 1024:
        return f"{max(1, round(n / 1024))} KB"
    if lang is None:
        from .i18n import get_locale
        try:
            lang = get_locale()
        except Exception:  # noqa: BLE001 – außerhalb einer Anfrage
            lang = "de"
    text = f"{n / 1024 / 1024:.1f} MB"
    return text if lang == "en" else text.replace(".", ",")


def localized_message(msg: str) -> str:
    """Gespeicherte Meldung (deutsch) in der aktuellen Sprache – für Oberfläche und E-Mail."""
    import re
    from .i18n import _, get_locale
    en = get_locale() == "en"
    patterns = [
        (r"^(\S+) gesichert \((.+)\)$", lambda m: _("{name} gesichert ({size})", name=m.group(1),
                                                     size=m.group(2).replace(",", ".") if en else m.group(2))),
        (r"^Sicherung fehlgeschlagen: (.*)$", lambda m: _("Sicherung fehlgeschlagen: {error}", error=m.group(1))),
        (r"^Datenbankdatei (.+) nicht gefunden\.$", lambda m: _("Datenbankdatei {path} nicht gefunden.", path=m.group(1))),
        (r"^Sicherung (\S+) nicht gefunden\.$", lambda m: _("Sicherung {name} nicht gefunden.", name=m.group(1))),
        (r"^Sicherung (\S+) ist beschädigt\.$", lambda m: _("Sicherung {name} ist beschädigt.", name=m.group(1))),
    ]
    for pat, fn in patterns:
        m = re.match(pat, msg or "", re.S)
        if m:
            return fn(m)
    return _(msg) if msg else msg


def keep_count() -> int:
    return min(KEEP_MAX, max(KEEP_MIN, get_int("backup_keep", 10)))


# ---------------------------------------------------------------- Sichern

def _integrity_ok(path: Path) -> bool:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def create_backup(kind: str = "", now: datetime | None = None, *, rotate_after: bool = True) -> Path:
    """Sicherung anlegen; kind: '' (automatisch), '-manuell', '-vor-wiederherstellung'."""
    src_path = database_file()
    if src_path is None:
        raise BackupError("Automatische Sicherung gibt es nur für SQLite.")
    if not src_path.exists():
        raise BackupError(f"Datenbankdatei {src_path} nicht gefunden.")
    now = now or datetime.now()
    target_dir = backup_dir()
    name = f"{PREFIX}{now:%Y-%m-%d_%H%M%S}{kind}.db"
    final = target_dir / name
    n = 1
    while final.exists():  # zwei Sicherungen in derselben Sekunde
        final = target_dir / name.replace(".db", f"-{n}.db")
        n += 1
    part = final.with_suffix(".part")
    src = sqlite3.connect(str(src_path), timeout=30)
    dst = sqlite3.connect(str(part))
    try:
        src.backup(dst)
        # Die Kopie übernimmt den WAL-Modus der Quelle – als eigenständige Datei ohne -wal/-shm ablegen
        dst.execute("PRAGMA journal_mode=DELETE").fetchone()
    except sqlite3.Error as exc:
        dst.close()
        _unlink_part(part)
        raise BackupError(f"Sicherung fehlgeschlagen: {exc}") from exc
    finally:
        src.close()
    dst.close()
    _unlink_siblings(part)
    if not _integrity_ok(part):
        _unlink_part(part)
        raise BackupError("Die Sicherung ist beschädigt (integrity_check) und wurde verworfen.")
    _unlink_siblings(part)
    try:
        os.chmod(part, 0o600)  # enthält personenbezogene Daten
    except OSError:
        pass
    os.replace(part, final)
    if rotate_after:
        rotate()
    return final


def _unlink_siblings(path: Path):
    for suffix in ("-wal", "-shm", "-journal"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _unlink_part(path: Path):
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
        seq = int(m.group(4)[1:]) if m.group(4) else 0   # „-1“, „-2“ …: später in derselben Sekunde
        items.append({"name": p.name, "path": p, "size": p.stat().st_size, "created": created,
                      "kind": KIND_LABELS.get(kind, "manuell"), "_seq": seq})
    items.sort(key=lambda x: (x["created"], x["_seq"]), reverse=True)
    return items


def rotate(keep: int | None = None) -> int:
    keep = keep_count() if keep is None else keep
    removed = 0
    for item in list_backups()[keep:]:
        try:
            item["path"].unlink()
            removed += 1
        except OSError as exc:
            log.warning("Alte Sicherung %s konnte nicht gelöscht werden: %s", item["name"], exc)
    # Reste abgebrochener Läufe – aber nicht, was ein anderer Prozess gerade noch schreibt
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


# ---------------------------------------------------------------- Wiederherstellen (nur bei angehaltener App!)

def restore_backup(name: str) -> Path:
    """Sicherung zurückspielen. Vorher wird der aktuelle Stand gesichert („vor Wiederherstellung“)."""
    src = find_backup(name)
    if src is None:
        raise BackupError(f"Sicherung {name} nicht gefunden.")
    if not _integrity_ok(src):
        raise BackupError(f"Sicherung {name} ist beschädigt.")
    target = database_file()
    if target is None:
        raise BackupError("Wiederherstellen geht nur mit SQLite.")
    # ohne Aufräumen – sonst könnte gerade die Sicherung gelöscht werden, die zurückgespielt werden soll
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


# ---------------------------------------------------------------- Zeitplan und Status

def _record(ok: bool, message: str, now: datetime):
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
        log.exception("Datensicherung fehlgeschlagen")
        msg = str(exc) if isinstance(exc, BackupError) else f"Sicherung fehlgeschlagen: {exc}"
        _record(False, msg, now)
        return False, msg
    msg = f"{path.name} gesichert ({human_size(size)})"
    _record(True, msg, now)
    log.info("Datensicherung: %s", msg)
    return True, msg


def run_daily(now: datetime | None = None) -> tuple[bool, str] | None:
    """Vom Zeitplan aufgerufen (alle paar Minuten); sichert einmal pro Tag ab der eingestellten Uhrzeit."""
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
    """Kurzer Warnhinweis für Admins, wenn die letzte Sicherung fehlgeschlagen ist."""
    if not get_bool("backup_enabled") or not get_setting("backup_last_error"):
        return ""
    if get_setting("backup_last_ok") > get_setting("backup_last_error"):
        return ""
    return _("Die letzte Datensicherung ist fehlgeschlagen.")


def _notify_admins(message: str):
    from .mailer import mail_active, render_mail, send_mail
    if not mail_active():
        return
    for admin in User.query.filter_by(role=ROLE_ADMIN, active=True):
        if admin.email:
            try:
                # Meldung erst in der Sprache des Empfängers übersetzen (render_mail stellt sie ein)
                subject, text, html = render_mail("backup_failed", user=admin, message=message,
                                                  localize=localized_message)
                send_mail(admin.email, subject, text, html, kind="backup", user_id=admin.id)
            except Exception:  # noqa: BLE001
                log.exception("Hinweis auf fehlgeschlagene Sicherung konnte nicht gesendet werden")
