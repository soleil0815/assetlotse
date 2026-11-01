"""AssetLotse – Verwaltung und Ausgabe von Arbeitsmitteln (Inventar).

Copyright (C) 2026 Die AssetLotse-Autoren
SPDX-License-Identifier: AGPL-3.0-or-later

Dieses Programm ist freie Software: Sie können es unter den Bedingungen der GNU Affero General
Public License, Version 3 oder (nach Ihrer Wahl) jeder späteren Version, weitergeben und/oder
verändern. Es wird OHNE JEDE GEWÄHRLEISTUNG bereitgestellt. Details: Datei LICENSE.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import click
from sqlalchemy.exc import IntegrityError
from flask import Flask, flash, has_request_context, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, logout_user
from flask_wtf.csrf import CSRFError, CSRFProtect

from .models import MIN_PASSWORD_LENGTH, ROLE_ADMIN, ROLES, Loan, User, db, get_bool, get_setting
from .models import get_int as get_int_setting
from .i18n import gettext as _

__version__ = "2.0.1"
APP_NAME = "AssetLotse"
LICENSE_NAME = "GNU AGPL v3 oder später"
# Wo der Quellcode zu finden ist (AGPL § 13). Wer AssetLotse verändert betreibt, trägt hier
# bzw. in ASSETLOTSE_SOURCE_URL die Adresse des eigenen, veränderten Quellcodes ein.
DEFAULT_SOURCE_URL = ""
AUTHOR_URL = "https://helfi.space"


import hashlib as _hashlib

_ASSET_REV = _hashlib.sha1(f"assetlotse-{__version__}".encode()).hexdigest()[:8]

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "font-src 'self'; connect-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'self'")

log = logging.getLogger(__name__)
LDAP_RECHECK_SECONDS = 4 * 3600   # AD-Konten laufender Sitzungen alle 4 Stunden nachprüfen

login_manager = LoginManager()
csrf = CSRFProtect()


def _engine_options(url: str) -> dict:
    opts = {"pool_pre_ping": True}
    if url.startswith(("mysql", "mariadb")):
        # Standard bei MySQL ist REPEATABLE READ: Nach dem Warten auf die Sperre des Gegenstands sähe eine Ausgabe
        # den alten Stand und könnte das letzte Stück ein zweites Mal ausgeben.
        opts["isolation_level"] = "READ COMMITTED"
    return opts


def _secret_key(instance: Path) -> str:
    """Sitzungs- und Verschlüsselungsschlüssel: ASSETLOTSE_SECRET_KEY oder <Datenordner>/secret_key.

    Die Datei wird atomar angelegt (O_EXCL): Starten mehrere Worker gleichzeitig, erzeugt genau einer
    den Schlüssel, die anderen lesen ihn. Ein leerer Schlüssel wird nie verwendet."""
    key = os.environ.get("ASSETLOTSE_SECRET_KEY", "")
    if key.strip():
        if len(key.strip()) < 32:
            # Daraus werden auch die Schlüssel für BitLocker-Angaben abgeleitet – zu kurz ist offline ratbar
            log.warning("ASSETLOTSE_SECRET_KEY ist kürzer als 32 Zeichen. Bitte einen langen Zufallswert "
                        "verwenden (z. B. python3 -c 'import secrets; print(secrets.token_hex(32))') – "
                        "oder die Variable leer lassen, dann erzeugt AssetLotse selbst einen.")
        return key   # unverändert wie bis 2.9 (Leerzeichen gehören zum Schlüssel)
    f = instance / "secret_key"
    try:
        fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        for _ in range(50):           # ein anderer Worker schreibt gerade – kurz warten
            key = f.read_text().strip()
            if key:
                return key
            time.sleep(0.1)
        raise SystemExit(
            f"AssetLotse: Die Schlüsseldatei {f} ist leer. Falls AssetLotse mit diesem Datenordner noch nie "
            f"gelaufen ist, die Datei löschen; sonst die Datei aus einer Sicherung zurückholen (sie gehört zur "
            f"Datenbank – ohne sie funktionieren 2FA-Anmeldungen und gespeicherte SMTP-/AD-Passwörter nicht mehr, und "
            f"die vertraulichen Angaben wie BitLocker-Schlüssel sind unlesbar).")
    key = secrets.token_hex(32)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
        fh.flush()
        os.fsync(fh.fileno())
    return key


@contextmanager
def _startup_lock(instance: Path):
    """Exklusive Sperre auf <Datenordner>/.startup.lock für die Start-Schritte (Schlüssel, Tabellen,
    Spalten, Migrationen). gunicorn startet mehrere Worker gleichzeitig – ohne Sperre würden sie
    dieselben Tabellen/Spalten parallel anlegen und mit „table already exists“ abbrechen."""
    try:
        fh = open(instance / ".startup.lock", "a+b")
    except OSError as exc:
        log.warning("Startsperre %s nicht verfügbar (%s) – fahre ohne fort", instance / ".startup.lock", exc)
        yield
        return
    unlock = None
    try:
        try:
            import fcntl
        except ImportError:           # Windows
            fcntl = None
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                unlock = lambda: fcntl.flock(fh.fileno(), fcntl.LOCK_UN)  # noqa: E731
            except OSError as exc:    # z. B. NFS ohne Sperren (ENOLCK)
                log.warning("Startsperre nicht möglich (%s) – fahre ohne fort", exc)
        else:
            try:
                import msvcrt
                fh.seek(0)
                deadline = time.monotonic() + 120
                while True:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.2)

                def unlock():
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception as exc:  # noqa: BLE001 – Sperre ist eine Absicherung, kein Muss
                log.warning("Startsperre nicht möglich (%s) – fahre ohne fort", exc)
        yield
    finally:
        try:
            if unlock is not None:
                unlock()
        finally:
            fh.close()


KEY_CHECK_SETTING = "secret_key_check"


def _key_fingerprint(key: str) -> str:
    import hmac
    return hmac.new(key.encode(), b"assetlotse-secret-key-check", "sha256").hexdigest()


def _check_secret_key(app: Flask) -> None:
    """Passt der Schlüssel zur Datenbank? Mit einem anderen Schlüssel wären 2FA-Geheimnisse und die
    gespeicherten SMTP-/AD-Passwörter nicht mehr lesbar. Beim ersten Start wird ein Prüfwert (HMAC einer
    Konstante, nicht der Schlüssel) gespeichert; passt er später nicht, startet AssetLotse nicht –
    außer mit ASSETLOTSE_ACCEPT_NEW_KEY=1 (dann gilt der neue Schlüssel)."""
    from .models import set_setting
    expected = _key_fingerprint(app.config["SECRET_KEY"])
    stored = get_setting(KEY_CHECK_SETTING)
    if stored == expected:
        return
    if stored and os.environ.get("ASSETLOTSE_ACCEPT_NEW_KEY", "").strip() != "1":
        msg = ("AssetLotse: Der Schlüssel (ASSETLOTSE_SECRET_KEY bzw. Datei secret_key im Datenordner) passt "
               "NICHT zur Datenbank. Mit diesem Schlüssel schlagen 2FA-Anmeldungen fehl und gespeicherte "
               "SMTP-/AD-Passwörter sowie alle vertraulichen Angaben (z. B. BitLocker-Schlüssel) sind nicht lesbar. "
               "Bitte den zur Datenbank gehörenden Schlüssel "
               "zurückholen (secret_key aus derselben Sicherung wie die Datenbank). Soll der neue Schlüssel "
               "trotzdem gelten, einmalig mit ASSETLOTSE_ACCEPT_NEW_KEY=1 starten – danach 2FA für alle "
               "zurücksetzen, SMTP-/AD-Passwort neu eingeben; die vertraulichen Angaben sind dann VERLOREN.")
        log.error("%s\n%s\n%s", "!" * 78, msg, "!" * 78)
        raise SystemExit(msg)
    if stored:
        log.warning("Neuer Schlüssel übernommen (ASSETLOTSE_ACCEPT_NEW_KEY=1): bisherige 2FA-Einrichtungen und "
                    "gespeicherte SMTP-/AD-Passwörter sind nicht mehr lesbar und müssen neu eingerichtet werden; "
                    "vertrauliche Angaben der Gegenstände sind verloren und müssen neu erfasst werden.")
    set_setting(KEY_CHECK_SETTING, expected)
    db.session.commit()


def _ensure_timezone(environ=None, zoneinfo_dirs=None, tzdata_root=None, tzset=None) -> str | None:
    """TZ (z. B. Europe/Berlin) wirkt nur, wenn das System die Zeitzonendatei hat (/usr/share/zoneinfo).
    Fehlt sie, wird – falls installiert – die Datei aus dem Python-Paket „tzdata“ verwendet; sonst
    eine deutliche Warnung (Uhrzeiten wären dann UTC). Gibt den verwendeten TZ-Wert zurück."""
    environ = os.environ if environ is None else environ
    tzset = tzset or getattr(time, "tzset", None)
    raw = (environ.get("TZ") or "").strip()
    name = raw[1:] if raw.startswith(":") else raw
    if not name or tzset is None:
        return None
    if ".." in name.split("/"):
        pass                          # ungültig – unten Warnung
    elif os.path.isabs(name):
        if os.path.exists(name):
            return raw
    else:
        if zoneinfo_dirs is None:
            zoneinfo_dirs = [d for d in (environ.get("TZDIR"), "/usr/share/zoneinfo", "/usr/lib/zoneinfo",
                                         "/usr/share/lib/zoneinfo") if d]
        if any(os.path.isfile(os.path.join(d, name)) for d in zoneinfo_dirs):
            return raw
        if "," in name or re.match(r"^(<[^>]+>|[A-Za-z]{3,})[+-]?\d", name):
            return raw                # POSIX-Angabe wie CET-1CEST,M3.5.0,M10.5.0/3 – braucht keine Datei
        if tzdata_root is None:
            try:
                from importlib.resources import files
                tzdata_root = str(files("tzdata") / "zoneinfo")
            except Exception:  # noqa: BLE001 – Paket tzdata nicht installiert
                tzdata_root = None
        candidate = os.path.join(tzdata_root, *name.split("/")) if tzdata_root else None
        if candidate and os.path.isfile(candidate):
            environ["TZ"] = ":" + candidate
            tzset()
            log.info("Zeitzone %s aus dem Python-Paket tzdata geladen", name)
            return environ["TZ"]
    if name.upper() in ("UTC", "GMT", "ETC/UTC", "ETC/GMT", "UTC0", "GMT0"):
        return raw                    # ohne Datei ohnehin UTC
    log.warning("Zeitzone TZ=%s nicht gefunden – Uhrzeiten in UTC. Prüfen: im Container "
                "„ls /usr/share/zoneinfo/%s“; Schreibweise wie Europe/Berlin.", raw, name)
    return None


DB_FILE = "assetlotse.db"


def _sqlite_file(instance: Path) -> Path:
    """Standard-Datenbank im Datenverzeichnis.

    Schutz vor einem „leeren Neustart“: Fehlt assetlotse.db, liegt aber eine andere Datenbankdatei im
    Datenverzeichnis, startet das Programm nicht, statt still einen neuen, leeren Bestand anzulegen."""
    db_file = instance / DB_FILE
    if not db_file.exists():
        others = sorted(p.name for p in instance.glob("*.db") if p.is_file() and p.stat().st_size > 0)
        if others:
            raise SystemExit(
                f"Im Datenverzeichnis {instance} liegt keine {DB_FILE}, aber: {', '.join(others)}. "
                f"Falls das eure bisherige Datenbank ist, bitte umbenennen, z. B.: mv {others[0]} {DB_FILE} – "
                f"sonst die Datei verschieben. (Schutz davor, versehentlich mit einem leeren Bestand zu starten.)")
    return db_file


def _backup_before_upgrade() -> str | None:
    """Läuft eine neue Version zum ersten Mal auf einer vorhandenen Datenbank, vor jeder Änderung an der
    Datenbank eine Sicherung anlegen. So ist auch ein Update ohne update.sh (z. B. ein Update-Tool zieht ein neues
    Image) abgesichert. Liefert die bisher eingetragene Version (None = neue/leere Datenbank)."""
    from sqlalchemy import inspect as sa_inspect, text

    from . import backup
    try:
        if "settings" not in sa_inspect(db.engine).get_table_names():
            return None                                   # neue Datenbank – nichts zu sichern
        row = db.session.execute(text("SELECT value FROM settings WHERE key = 'app_version'")).first()
        has_users = "users" in sa_inspect(db.engine).get_table_names() and \
            db.session.execute(text("SELECT 1 FROM users LIMIT 1")).first() is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("Versionsprüfung beim Start nicht möglich: %s", exc)
        db.session.rollback()
        return None
    finally:
        db.session.remove()
    previous = (row[0] if row else "") or ""
    if previous == __version__ or (not previous and not has_users):
        return previous or None
    from_text = previous or "einer Version vor 1.8.0"
    if previous and _version_tuple(previous) > _version_tuple(__version__):
        raise SystemExit(_too_new_message(previous))     # nichts anfassen – Folgefehler wären schlimmer
    if not backup.supported():
        log.info("Update von %s auf %s – automatische Sicherung nur bei SQLite; bitte selbst sichern.",
                 from_text, __version__)
        return previous
    try:
        path = backup.create_backup(f"-vor-{__version__}")
        log.info("Update von %s auf %s: Sicherung vor dem Update angelegt (%s)", from_text, __version__, path.name)
    except Exception as exc:  # noqa: BLE001 – lieber starten als ausfallen, aber deutlich melden
        log.error("Update von %s auf %s: Sicherung vor dem Update FEHLGESCHLAGEN: %s", from_text, __version__, exc)
    return previous


def _version_tuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in v.split("-")[0].split("."))


def _too_new_message(db_version: str) -> str:
    """Ältere Version auf einer Datenbank einer neueren: klar sagen, was zu tun ist (Deutsch und Englisch –
    die Meldung steht im Container-Protokoll, die Sprache der Oberfläche ist hier noch unbekannt)."""
    from . import backup
    name = ""
    if backup.supported():
        for item in backup.list_backups():                       # neueste zuerst
            if item["name"].startswith(backup.PREFIX) and f"-vor-{db_version}" in item["name"]:
                name = item["name"]
                break
    restore = (f"docker compose run --rm assetlotse restore {name}" if name else
               "docker compose run --rm assetlotse backups   (Liste / list)")
    return (
        f"AssetLotse {__version__}: Die Datenbank stammt von der neueren Version {db_version} – Start abgebrochen, "
        f"nichts wurde verändert.\n"
        f"  Entweder wieder Version {db_version} oder neuer starten (ASSETLOTSE_TAG in .env), oder die Sicherung "
        f"von vor dem Update zurückspielen:\n"
        f"    docker compose stop assetlotse\n    {restore}\n    docker compose up -d\n"
        f"AssetLotse {__version__}: the database comes from the newer version {db_version} – start-up aborted, "
        f"nothing was changed. Start {db_version} or newer again, or restore the backup taken before the update "
        f"(commands above).")


COMPOSE_VERSION = 2    # Stand der docker-compose.yml, den dieses Programm erwartet (ASSETLOTSE_COMPOSE_VERSION)


def compose_outdated() -> str:
    """Leer – oder die gefundene Version der docker-compose.yml, wenn sie älter ist als erwartet.
    Nur im Container (ASSETLOTSE_CONTAINER aus dem Image) – ohne Compose-Datei gibt es nichts zu prüfen."""
    if os.environ.get("ASSETLOTSE_CONTAINER") != "1":
        return ""
    raw = (os.environ.get("ASSETLOTSE_COMPOSE_VERSION") or "").strip().strip('"').strip("'")
    found = int(raw) if raw.isdigit() else 0
    return "" if found >= COMPOSE_VERSION else (raw or "1")


def create_app(test_config: dict | None = None) -> Flask:
    data_dir = os.environ.get("ASSETLOTSE_DATA_DIR")
    app = Flask(__name__, instance_path=os.path.abspath(data_dir) if data_dir else None,
                instance_relative_config=True)
    instance = Path(app.instance_path)
    try:
        instance.mkdir(parents=True, exist_ok=True)
        (instance / "images").mkdir(exist_ok=True)
        probe = instance / f".write-test-{os.getpid()}"   # je Prozess eigene Datei (Worker starten gleichzeitig)
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
    except PermissionError as exc:
        raise SystemExit(
            f"AssetLotse: Keine Schreibrechte im Datenverzeichnis {instance} (Benutzer-ID {os.getuid()}). "
            f"Bitte Rechte anpassen, z. B. auf dem Host: sudo chown -R {os.getuid()}:{os.getgid()} <datenordner>"
        ) from exc

    with _startup_lock(instance):     # siehe _startup_lock: Worker starten gleichzeitig
        secret_key = _secret_key(instance)
    app.config.update(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL") or f"sqlite:///{_sqlite_file(instance)}",
        SQLALCHEMY_ENGINE_OPTIONS=_engine_options(os.environ.get("DATABASE_URL", "")),
        IMAGE_DIR=str(instance / "images"),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_DURATION=timedelta(days=14),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        WTF_CSRF_TIME_LIMIT=None,
    )
    if test_config:
        app.config.update(test_config)

    logging.basicConfig(level=os.environ.get("ASSETLOTSE_LOGLEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _ensure_timezone()

    # Reverse-Proxy und HTTPS werden automatisch erkannt (siehe proxy.py)
    from .proxy import install as install_proxy_support
    install_proxy_support(app, os.environ)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "main.login"
    login_manager.login_message = "Bitte melde dich an."
    from .i18n import gettext as _gettext
    login_manager.localize_callback = _gettext
    login_manager.login_message_category = "info"

    @login_manager.user_loader
    def load_user(user_id):
        uid, _, epoch = str(user_id).partition(":")
        if not uid.isdigit():
            return None
        user = db.session.get(User, int(uid))
        if user is None or not user.active:
            return None
        # Sitzung aus einer älteren „Generation“ (Passwort geändert, überall abgemeldet …) -> ungültig
        if str(user.auth_epoch or 0) != (epoch or "0"):
            return None
        return user

    from .views.admin import bp as admin_bp
    from .views.assets import bp as assets_bp
    from .views.loans import bp as loans_bp
    from .views.locations import bp as locations_bp
    from .views.main import bp as main_bp
    from .views.register import bp as register_bp
    from .views.security import bp as security_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(assets_bp)
    app.register_blueprint(loans_bp)
    app.register_blueprint(locations_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(security_bp)
    app.register_blueprint(register_bp)
    from .views.help import bp as help_bp, help_url
    app.register_blueprint(help_bp)
    app.jinja_env.globals["help_url_for"] = help_url

    def _catalog_hidden_for_current() -> bool:
        from flask_login import current_user as _cu
        from .models import catalog_hidden
        return catalog_hidden(_cu)
    app.jinja_env.globals["catalog_hidden"] = _catalog_hidden_for_current

    # Wartung (docker compose run --rm assetlotse restore …): keine Versionsprüfung, keine Änderung an der
    # Datenbank – sonst ließe sich eine Sicherung gerade dann nicht zurückspielen, wenn der Start abbricht.
    maintenance = os.environ.get("ASSETLOTSE_MAINTENANCE") == "1"
    outdated = compose_outdated()
    app.config["COMPOSE_OUTDATED"] = outdated
    if outdated and not maintenance:
        log.warning("Die docker-compose.yml ist veraltet (Stand %s, erwartet %s): ./update.sh legt die neue ab "
                    "(die alte bleibt als docker-compose.yml.alt) – oder die Datei aus dem Release übernehmen.",
                    outdated, COMPOSE_VERSION)

    # ---- Start-Schritte unter exklusiver Sperre (mehrere gunicorn-Worker starten gleichzeitig) ----
    with _startup_lock(instance), app.app_context():
        if not maintenance:
            previous_version = _backup_before_upgrade()
            db.create_all()
            from .models import ensure_columns
            added = ensure_columns()
            for col in added:
                logging.getLogger(__name__).info("Datenbank aktualisiert: Spalte %s ergänzt", col)
            if "users.manages_all" in added:
                from .models import migrate_manages_all
                n = migrate_manages_all()
                if n:
                    logging.getLogger(__name__).info("%d Verwalter ohne Gruppe verwalten weiterhin den gesamten Bestand "
                                                     "(jetzt als ausdrückliche Einstellung)", n)
            from .models import ensure_indexes; ensure_indexes()  # noqa: E702
            _check_secret_key(app)
            from .models import set_setting
            from .models import encrypt_stored_secrets
            if encrypt_stored_secrets():
                logging.getLogger(__name__).info("Gespeicherte Zugangsdaten (AD, SMTP) werden jetzt verschlüsselt abgelegt")
            if previous_version != __version__:
                set_setting("app_version", __version__)
                db.session.commit()

    # Hintergrund-Job für E-Mail-Erinnerungen (täglich zur eingestellten Uhrzeit)
    if not app.config.get("TESTING") and not maintenance and os.environ.get("ASSETLOTSE_SCHEDULER", "1") == "1":
        from .mailer import start_scheduler
        start_scheduler(app)

    # ------------------------------------------------------------ Ersteinrichtung
    @app.before_request
    def require_setup():
        if request.endpoint in ("main.setup", "main.set_language", "static", "main.branding_file") or request.endpoint is None:
            return None
        if not app.config.get("_setup_done"):
            if User.query.filter_by(role=ROLE_ADMIN).first() is None:
                # Einmal eingerichtet → nie wieder zur Web-Einrichtung (siehe main.setup)
                # Nur eine wirklich leere Datenbank darf über das Web eingerichtet werden
                if get_setting("setup_completed") != "1" and User.query.first() is None:
                    return redirect(url_for("main.setup"))
            elif get_setting("setup_completed") != "1":
                from sqlalchemy.exc import IntegrityError
                from .models import set_setting as _set
                try:
                    _set("setup_completed", "1")
                    db.session.commit()
                except IntegrityError:   # anderer Prozess war schneller
                    db.session.rollback()
            app.config["_setup_done"] = True
        return None

    # ------------------------------------------------------------ LDAP-Sitzungen nachprüfen
    @app.before_request
    def recheck_ldap_session():
        """Wer im AD gesperrt, gelöscht oder aus der berechtigten Gruppe entfernt wurde, soll nicht
        mit einer alten Sitzung (oder „Angemeldet bleiben“) weiterarbeiten können."""
        if request.endpoint in (None, "static", "main.branding_file") or not current_user.is_authenticated:
            return None
        if current_user.auth_source != "ldap":
            return None
        import time
        from .ldap_auth import Directory, LDAPError, ldap_enabled
        now = int(time.time())
        if now - int(session.get("ldap_ok_at") or 0) < LDAP_RECHECK_SECONDS:
            return None
        if not ldap_enabled() or not get_setting("ldap_bind_dn"):
            session["ldap_ok_at"] = now    # ohne Dienstkonto nicht prüfbar
            return None
        try:
            info = Directory().check_account(current_user.username)
        except LDAPError as exc:
            log.warning("LDAP-Nachprüfung für %s nicht möglich: %s", current_user.username, exc)
            session["ldap_ok_at"] = now - LDAP_RECHECK_SECONDS + 600   # in 10 Minuten erneut
            return None
        except Exception:  # noqa: BLE001 – ein AD-Problem darf niemanden aussperren
            log.exception("LDAP-Nachprüfung fehlgeschlagen")
            session["ldap_ok_at"] = now - LDAP_RECHECK_SECONDS + 600
            return None
        from .views.main import guid_conflict
        if guid_conflict(info, current_user):
            log.warning("LDAP-Konto %s: der Benutzername gehört im AD inzwischen einer anderen Person – Sitzung beendet",
                        current_user.username)
            info = None
        if info is None or info.role is None:
            log.info("LDAP-Konto %s nicht mehr berechtigt – Sitzung beendet", current_user.username)
            session.clear()     # zuerst – logout_user() merkt sich darin, dass das Merk-Cookie gelöscht wird
            logout_user()
            flash(_("Deine Anmeldung ist abgelaufen. Bitte melde dich erneut an."), "info")
            return redirect(url_for("main.login"))
        from .views.main import sync_ldap_user
        sync_ldap_user(info, current_user._get_current_object())
        db.session.commit()
        session["ldap_ok_at"] = now
        return None

    # ------------------------------------------------------------ Template-Helfer
    from .branding import context as brand_context
    from .branding import org_name as brand_org_name

    @app.context_processor
    def inject_globals():
        ctx = {
            "org_name": brand_org_name(),
            "ROLES": ROLES,
            "today": date.today(),
            "version": __version__,
            "app_name": APP_NAME,
            "license_name": LICENSE_NAME,
            "author_url": AUTHOR_URL,
            "source_url": os.environ.get("ASSETLOTSE_SOURCE_URL", DEFAULT_SOURCE_URL),
            "imprint_url": get_setting("imprint_url"),
            "retention_months": get_int_setting("retention_months"),
            "privacy_url": get_setting("privacy_url"),
            # Cache-Schlüssel für CSS/JS ohne Versionsnummer (nicht öffentlich erkennbar)
            "asset_rev": _ASSET_REV,
            "ldap_on": get_bool("ldap_enabled"),
            "brand": brand_context(),
            "registration_open": get_setting("registration_mode") in ("approval", "open"),
            "pending_registrations": 0,
            "overdue_count": 0,
            "news_unread": 0,
            "compose_outdated": "",
            "compose_version": COMPOSE_VERSION,
        }
        if has_request_context() and current_user.is_authenticated:
            from .news import unread as news_unread
            ctx["news_unread"] = len(news_unread(current_user))
        # Außerhalb einer Anfrage (z. B. Mail-Versand im Hintergrund) gibt es keinen Benutzer
        if has_request_context() and current_user.is_authenticated and current_user.is_admin:
            ctx["pending_registrations"] = User.query.filter_by(pending_approval=True, email_verified=True).count()
            from .backup import warning as backup_warning
            ctx["backup_warning"] = backup_warning()
            ctx["compose_outdated"] = app.config.get("COMPOSE_OUTDATED", "")
        if has_request_context() and current_user.is_authenticated and current_user.is_manager:
            from .models import Asset, manageable_assets_query
            ctx["overdue_count"] = Loan.query.filter(
                Loan.returned_at.is_(None), Loan.due_at < date.today(),
                Loan.asset_id.in_(manageable_assets_query(current_user).with_entities(Asset.id))).count()
        return ctx

    @app.template_filter("filesize")
    def filesize(n):
        from .backup import human_size
        return human_size(n or 0)

    from . import i18n
    i18n.init_app(app)

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        # Strenge Content-Security-Policy: kein eingebettetes oder fremdes JavaScript, keine fremden Quellen.
        # (Für SVG-Logos setzt die jeweilige Route eine noch strengere eigene Richtlinie.)
        resp.headers.setdefault("Content-Security-Policy", CSP)
        # Angemeldete Seiten (Personendaten, Exporte) nie in geteilten Caches/CDNs und nicht auf der Platte
        # des Browsers ablegen. Eigene Angaben einer Route (Bilder, Logos, Handbuch) bleiben bestehen.
        if "Cache-Control" not in resp.headers and current_user.is_authenticated:
            resp.headers["Cache-Control"] = "private, no-store"
        return resp

    # ------------------------------------------------------------ Fehlerseiten
    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("error.html", code=403, title=_("Kein Zugriff"),
                               text=_("Dafür fehlt dir die Berechtigung.")), 403

    @app.errorhandler(OverflowError)
    def too_large_number(_e):
        # z. B. /admin/benutzer/99999999999999999999 – zu große Zahl für die Datenbank
        db.session.rollback()
        return not_found(None)

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404, title=_("Nicht gefunden"),
                               text=_("Diese Seite oder diesen Gegenstand gibt es nicht.")), 404

    @app.errorhandler(IntegrityError)
    def integrity_error(_e):
        # Seltener Wettlauf (z. B. derselbe Name gleichzeitig in zwei Fenstern angelegt): kein Fehler 500,
        # sondern zurück zum Formular mit Hinweis
        db.session.rollback()
        logging.getLogger(__name__).warning("Speichern abgelehnt (eindeutiger Wert schon vorhanden): %s", _e.orig)
        flash(_("Das konnte nicht gespeichert werden, weil es den Eintrag gerade schon gibt. Bitte prüfen und "
                "erneut versuchen."), "error")
        from urllib.parse import urlsplit

        from .views import safe_next
        ref = urlsplit(request.referrer or "")
        back = (ref.path + (f"?{ref.query}" if ref.query else "")) if ref.netloc == request.host else ""
        return redirect(safe_next(back) or url_for("main.dashboard"))

    @app.errorhandler(CSRFError)
    def csrf_error(_e):
        return render_template("error.html", code=400, title=_("Sitzung abgelaufen"),
                               text=_("Bitte lade die Seite neu und versuche es noch einmal.")), 400

    # ------------------------------------------------------------ CLI
    @app.cli.command("reset-password")
    @click.argument("username")
    @click.password_option()
    def reset_password(username, password):
        """Passwort eines lokalen Kontos zurücksetzen (z. B. Admin)."""
        user = User.query.filter_by(username=username).first()
        if not user:
            raise click.ClickException(f"Benutzer {username} nicht gefunden.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(f"Das Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen haben.")
        user.set_password(password)
        user.auth_source = "local"
        user.active = True
        db.session.commit()
        click.echo(f"Passwort für {username} gesetzt.")

    @app.cli.command("reset-2fa")
    @click.argument("username")
    def reset_2fa_cmd(username):
        """Zweiten Faktor eines Kontos zurücksetzen (z. B. Handy/Schlüssel verloren)."""
        from .twofa import reset_2fa
        user = User.query.filter_by(username=username).first()
        if not user:
            raise click.ClickException(f"Benutzer {username} nicht gefunden.")
        reset_2fa(user)
        click.echo(f"Zwei-Faktor-Anmeldung für {username} zurückgesetzt.")

    @app.cli.command("send-reminders")
    def send_reminders():
        """Fällige Erinnerungen/Mahnungen sofort versenden (z. B. per cron)."""
        from .mailer import format_stats, run_notifications
        click.echo(format_stats(run_notifications()))

    @app.cli.command("apply-retention")
    def apply_retention():
        """Aufbewahrungsfrist sofort anwenden (alte Ausleihen und Mail-Protokoll löschen)."""
        from .retention import format_result, run_retention
        click.echo(format_result(run_retention()))

    @app.cli.command("backup")
    def backup_cmd():
        """Datenbank jetzt sichern."""
        from .backup import run_now
        ok, msg = run_now("-manuell")
        if not ok:
            raise click.ClickException(msg)
        click.echo(msg)

    @app.cli.command("list-backups")
    def list_backups_cmd():
        """Vorhandene Sicherungen anzeigen (neueste zuerst)."""
        from .backup import backup_dir, human_size, list_backups
        click.echo(f"Ordner: {backup_dir()}")
        for b in list_backups():
            click.echo(f"  {b['name']}  {human_size(b['size']):>8}  {b['kind']}")

    @app.cli.command("restore-backup")
    @click.argument("name")
    @click.option("--yes", is_flag=True, help="Ohne Rückfrage")
    def restore_backup_cmd(name, yes):
        """Sicherung zurückspielen – NUR bei angehaltenem Server! Der aktuelle Stand wird vorher gesichert."""
        from .backup import BackupError, restore_backup
        if not yes:
            click.confirm(f"Datenbank durch {name} ersetzen? Der Server muss dafür angehalten sein.", abort=True)
        try:
            safety = restore_backup(name)
        except BackupError as exc:
            raise click.ClickException(str(exc))
        if safety:
            click.echo(f"Bisheriger Stand gesichert als {safety.name}")
        click.echo(f"{name} wurde zurückgespielt. Server jetzt wieder starten.")

    @app.cli.command("create-admin")
    @click.argument("username")
    @click.password_option()
    def create_admin(username, password):
        """Lokales Administratorkonto anlegen."""
        if User.query.filter_by(username=username).first():
            raise click.ClickException("Benutzer existiert bereits.")
        user = User(username=username, display_name=username, role=ROLE_ADMIN, auth_source="local")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(f"Das Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen haben.")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Administrator {username} angelegt.")

    return app
