"""AssetLotse – managing and issuing work equipment (inventory).

Copyright (C) 2026 The AssetLotse authors
SPDX-License-Identifier: AGPL-3.0-or-later

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero
General Public License, version 3 or (at your option) any later version. It is provided WITHOUT ANY
WARRANTY. Details: see the file LICENSE.
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

__version__ = "2.1.0"
APP_NAME = "AssetLotse"
LICENSE_NAME = "GNU AGPL v3 or later"
# Where to find the source code (AGPL section 13). Anyone running a modified AssetLotse enters the
# address of their own modified source code here or in ASSETLOTSE_SOURCE_URL.
DEFAULT_SOURCE_URL = ""
# shown to administrators only, in the sidebar ("Buy me a coffee")
SUPPORT_URL = "https://buymeacoffee.com/soleil0815"
AUTHOR_URL = "https://helfi.space"


import hashlib as _hashlib

_ASSET_REV = _hashlib.sha1(f"assetlotse-{__version__}".encode()).hexdigest()[:8]

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "font-src 'self'; connect-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'self'")

log = logging.getLogger(__name__)
LDAP_RECHECK_SECONDS = 4 * 3600   # re-check AD accounts of running sessions every 4 hours

login_manager = LoginManager()
csrf = CSRFProtect()


def _engine_options(url: str) -> dict:
    opts = {"pool_pre_ping": True}
    if url.startswith(("mysql", "mariadb")):
        # MySQL defaults to REPEATABLE READ: after waiting for the item's lock, an issue would see the old
        # state and could issue the last piece a second time.
        opts["isolation_level"] = "READ COMMITTED"
    return opts


def _secret_key(instance: Path) -> str:
    """Session and encryption key: ASSETLOTSE_SECRET_KEY or <data folder>/secret_key.

    The file is created atomically (O_EXCL): if several workers start at once, exactly one creates
    the key and the others read it. An empty key is never used."""
    key = os.environ.get("ASSETLOTSE_SECRET_KEY", "")
    if key.strip():
        if len(key.strip()) < 32:
            # The keys for BitLocker data are derived from it too – too short can be guessed offline
            log.warning("ASSETLOTSE_SECRET_KEY is shorter than 32 characters. Please use a long random value "
                        "(e.g. python3 -c 'import secrets; print(secrets.token_hex(32))') – "
                        "or leave the variable empty and AssetLotse creates one itself.")
        return key   # unchanged as up to 2.9 (spaces are part of the key)
    f = instance / "secret_key"
    try:
        fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        for _ in range(50):           # another worker is writing right now – wait briefly
            key = f.read_text().strip()
            if key:
                return key
            time.sleep(0.1)
        raise SystemExit(
            f"AssetLotse: The key file {f} is empty. If AssetLotse has never run with this data folder, "
            f"delete the file; otherwise restore it from a backup (it belongs to the database – without it, "
            f"2FA sign-ins and stored SMTP/AD passwords no longer work, and confidential data such as "
            f"BitLocker keys cannot be read).")
    key = secrets.token_hex(32)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
        fh.flush()
        os.fsync(fh.fileno())
    return key


@contextmanager
def _startup_lock(instance: Path):
    """Exclusive lock on <data folder>/.startup.lock for the start-up steps (key, tables, columns,
    migrations). gunicorn starts several workers at once – without the lock they would create the
    same tables/columns in parallel and abort with "table already exists"."""
    try:
        fh = open(instance / ".startup.lock", "a+b")
    except OSError as exc:
        log.warning("Start-up lock %s not available (%s) – continuing without it", instance / ".startup.lock", exc)
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
            except OSError as exc:    # e.g. NFS without locking (ENOLCK)
                log.warning("Start-up lock not possible (%s) – continuing without it", exc)
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
            except Exception as exc:  # noqa: BLE001 – the lock is a safeguard, not a requirement
                log.warning("Start-up lock not possible (%s) – continuing without it", exc)
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
    """Does the key match the database? With a different key, 2FA secrets and the stored SMTP/AD
    passwords could no longer be read. On the first start a check value (HMAC of a constant, not the
    key) is stored; if it does not match later, AssetLotse does not start – unless
    ASSETLOTSE_ACCEPT_NEW_KEY=1 is set (then the new key applies)."""
    from .models import set_setting
    expected = _key_fingerprint(app.config["SECRET_KEY"])
    stored = get_setting(KEY_CHECK_SETTING)
    if stored == expected:
        return
    if stored and os.environ.get("ASSETLOTSE_ACCEPT_NEW_KEY", "").strip() != "1":
        msg = ("AssetLotse: The key (ASSETLOTSE_SECRET_KEY or the file secret_key in the data folder) does "
               "NOT match the database. With this key, 2FA sign-ins fail and stored SMTP/AD passwords as well "
               "as all confidential data (e.g. BitLocker keys) cannot be read. Please restore the key that "
               "belongs to the database (secret_key from the same backup as the database). If the new key "
               "should apply anyway, start once with ASSETLOTSE_ACCEPT_NEW_KEY=1 – then reset 2FA for "
               "everyone and re-enter the SMTP/AD password; the confidential data is then LOST.")
        log.error("%s\n%s\n%s", "!" * 78, msg, "!" * 78)
        raise SystemExit(msg)
    if stored:
        log.warning("New key accepted (ASSETLOTSE_ACCEPT_NEW_KEY=1): existing 2FA setups and stored SMTP/AD "
                    "passwords can no longer be read and must be set up again; confidential item data is lost "
                    "and must be entered again.")
    set_setting(KEY_CHECK_SETTING, expected)
    db.session.commit()


def _ensure_timezone(environ=None, zoneinfo_dirs=None, tzdata_root=None, tzset=None) -> str | None:
    """TZ (e.g. Europe/Berlin) only takes effect if the system has the time zone file (/usr/share/zoneinfo).
    If it is missing, the file from the Python package "tzdata" is used if installed; otherwise a clear
    warning is logged (times would then be UTC). Returns the TZ value in use."""
    environ = os.environ if environ is None else environ
    tzset = tzset or getattr(time, "tzset", None)
    raw = (environ.get("TZ") or "").strip()
    name = raw[1:] if raw.startswith(":") else raw
    if not name or tzset is None:
        return None
    if ".." in name.split("/"):
        pass                          # invalid – warning below
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
            return raw                # POSIX spec such as CET-1CEST,M3.5.0,M10.5.0/3 – needs no file
        if tzdata_root is None:
            try:
                from importlib.resources import files
                tzdata_root = str(files("tzdata") / "zoneinfo")
            except Exception:  # noqa: BLE001 – package tzdata not installed
                tzdata_root = None
        candidate = os.path.join(tzdata_root, *name.split("/")) if tzdata_root else None
        if candidate and os.path.isfile(candidate):
            environ["TZ"] = ":" + candidate
            tzset()
            log.info("Time zone %s loaded from the Python package tzdata", name)
            return environ["TZ"]
    if name.upper() in ("UTC", "GMT", "ETC/UTC", "ETC/GMT", "UTC0", "GMT0"):
        return raw                    # UTC anyway without a file
    log.warning("Time zone TZ=%s not found – times in UTC. Check in the container: "
                "\"ls /usr/share/zoneinfo/%s\"; spelling like Europe/Berlin.", raw, name)
    return None


DB_FILE = "assetlotse.db"


def _sqlite_file(instance: Path) -> Path:
    """Default database in the data directory.

    Protection against an "empty restart": if assetlotse.db is missing but another database file is in
    the data directory, the program does not start instead of silently creating a new, empty inventory."""
    db_file = instance / DB_FILE
    if not db_file.exists():
        others = sorted(p.name for p in instance.glob("*.db") if p.is_file() and p.stat().st_size > 0)
        if others:
            raise SystemExit(
                f"The data directory {instance} contains no {DB_FILE}, but: {', '.join(others)}. "
                f"If that is your existing database, please rename it, e.g.: mv {others[0]} {DB_FILE} – "
                f"otherwise move the file away. (Protection against accidentally starting with an empty inventory.)")
    return db_file


def _backup_before_upgrade() -> str | None:
    """When a new version runs on an existing database for the first time, create a backup before any change
    to the database. This also covers an update without update.sh (e.g. an update tool pulls a new image).
    Returns the previously recorded version (None = new/empty database)."""
    from sqlalchemy import inspect as sa_inspect, text

    from . import backup
    try:
        if "settings" not in sa_inspect(db.engine).get_table_names():
            return None                                   # new database – nothing to back up
        row = db.session.execute(text("SELECT value FROM settings WHERE key = 'app_version'")).first()
        has_users = "users" in sa_inspect(db.engine).get_table_names() and \
            db.session.execute(text("SELECT 1 FROM users LIMIT 1")).first() is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("Version check at start-up not possible: %s", exc)
        db.session.rollback()
        return None
    finally:
        db.session.remove()
    previous = (row[0] if row else "") or ""
    if previous == __version__ or (not previous and not has_users):
        return previous or None
    from_text = previous or "a version before 1.8.0"
    if previous and _version_tuple(previous) > _version_tuple(__version__):
        raise SystemExit(_too_new_message(previous))     # touch nothing – follow-up errors would be worse
    if not backup.supported():
        log.info("Update from %s to %s – automatic backup only with SQLite; please back up yourself.",
                 from_text, __version__)
        return previous
    try:
        path = backup.create_backup(f"-vor-{__version__}")   # stored file name suffix – parsed by backup.NAME_RE, keep
        log.info("Update from %s to %s: backup before the update created (%s)", from_text, __version__, path.name)
    except Exception as exc:  # noqa: BLE001 – better to start than to fail, but report it clearly
        log.error("Update from %s to %s: backup before the update FAILED: %s", from_text, __version__, exc)
    return previous


def _version_tuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in v.split("-")[0].split("."))


def _too_new_message(db_version: str) -> str:
    """Older version on a database of a newer one: say clearly what to do (German and English – the
    message goes to the container log; the UI language is not known yet at this point)."""
    from . import backup
    name = ""
    if backup.supported():
        for item in backup.list_backups():                       # newest first
            m = backup.NAME_RE.match(item["name"])
            if m and m.group(3) == f"-vor-{db_version}":          # exactly this version (not 2.0.10 for 2.0.1)
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


COMPOSE_VERSION = 2    # docker-compose.yml revision this program expects (ASSETLOTSE_COMPOSE_VERSION)


def compose_outdated() -> str:
    """Empty – or the docker-compose.yml version found, if it is older than expected.
    Only in the container (ASSETLOTSE_CONTAINER from the image) – without a compose file there is nothing to check."""
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
        probe = instance / f".write-test-{os.getpid()}"   # one file per process (workers start at the same time)
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
    except PermissionError as exc:
        raise SystemExit(
            f"AssetLotse: No write permission in the data directory {instance} (user ID {os.getuid()}). "
            f"Please adjust the permissions, e.g. on the host: sudo chown -R {os.getuid()}:{os.getgid()} <data-folder>"
        ) from exc

    with _startup_lock(instance):     # see _startup_lock: workers start at the same time
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

    # Reverse proxy and HTTPS are detected automatically (see proxy.py)
    from .proxy import install as install_proxy_support
    install_proxy_support(app, os.environ)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "main.login"
    login_manager.login_message = "Please sign in."
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
        # Session from an older "generation" (password changed, signed out everywhere …) -> invalid
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

    # Maintenance (docker compose run --rm assetlotse restore …): no version check, no change to the
    # database – otherwise a backup could not be restored exactly when the start-up aborts.
    maintenance = os.environ.get("ASSETLOTSE_MAINTENANCE") == "1"
    outdated = compose_outdated()
    app.config["COMPOSE_OUTDATED"] = outdated
    if outdated and not maintenance:
        log.warning("The docker-compose.yml is outdated (revision %s, expected %s): ./update.sh puts the new one in "
                    "place (the old one is kept as docker-compose.yml.alt) – or take the file from the release.",
                    outdated, COMPOSE_VERSION)

    # ---- Start-up steps under an exclusive lock (several gunicorn workers start at the same time) ----
    with _startup_lock(instance), app.app_context():
        if not maintenance:
            previous_version = _backup_before_upgrade()
            db.create_all()
            from .models import ensure_columns
            added = ensure_columns()
            for col in added:
                logging.getLogger(__name__).info("Database updated: column %s added", col)
            if "users.manages_all" in added:
                from .models import migrate_manages_all
                n = migrate_manages_all()
                if n:
                    logging.getLogger(__name__).info("%d manager(s) without a group keep managing the entire inventory "
                                                     "(now as an explicit setting)", n)
            from .models import ensure_indexes; ensure_indexes()  # noqa: E702
            _check_secret_key(app)
            from .models import set_setting
            from .models import encrypt_stored_secrets
            if encrypt_stored_secrets():
                logging.getLogger(__name__).info("Stored credentials (AD, SMTP) are now kept encrypted")
            if previous_version != __version__:
                set_setting("app_version", __version__)
                db.session.commit()

    # Background job for email reminders (daily at the configured hour)
    if not app.config.get("TESTING") and not maintenance and os.environ.get("ASSETLOTSE_SCHEDULER", "1") == "1":
        from .mailer import start_scheduler
        start_scheduler(app)

    # ------------------------------------------------------------ Initial setup
    @app.before_request
    def require_setup():
        if request.endpoint in ("main.setup", "main.set_language", "static", "main.branding_file") or request.endpoint is None:
            return None
        if not app.config.get("_setup_done"):
            if User.query.filter_by(role=ROLE_ADMIN).first() is None:
                # Once set up → never back to the web setup (see main.setup)
                # Only a truly empty database may be set up via the web
                if get_setting("setup_completed") != "1" and User.query.first() is None:
                    return redirect(url_for("main.setup"))
            elif get_setting("setup_completed") != "1":
                from sqlalchemy.exc import IntegrityError
                from .models import set_setting as _set
                try:
                    _set("setup_completed", "1")
                    db.session.commit()
                except IntegrityError:   # another process was faster
                    db.session.rollback()
            app.config["_setup_done"] = True
        return None

    # ------------------------------------------------------------ Re-check LDAP sessions
    @app.before_request
    def recheck_ldap_session():
        """Anyone disabled or deleted in AD, or removed from the authorized group, must not keep working
        with an old session (or "Stay signed in")."""
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
            session["ldap_ok_at"] = now    # cannot be checked without a service account
            return None
        try:
            info = Directory().check_account(current_user.username)
        except LDAPError as exc:
            log.warning("LDAP re-check for %s not possible: %s", current_user.username, exc)
            session["ldap_ok_at"] = now - LDAP_RECHECK_SECONDS + 600   # again in 10 minutes
            return None
        except Exception:  # noqa: BLE001 – an AD problem must not lock anyone out
            log.exception("LDAP re-check failed")
            session["ldap_ok_at"] = now - LDAP_RECHECK_SECONDS + 600
            return None
        from .views.main import guid_conflict
        if guid_conflict(info, current_user):
            log.warning("LDAP account %s: the user name now belongs to a different person in AD – session ended",
                        current_user.username)
            info = None
        if info is None or info.role is None:
            log.info("LDAP account %s no longer authorized – session ended", current_user.username)
            session.clear()     # first – logout_user() records in it that the remember cookie is to be deleted
            logout_user()
            flash(_("Your session has expired. Please sign in again."), "info")
            return redirect(url_for("main.login"))
        from .views.main import sync_ldap_user
        sync_ldap_user(info, current_user._get_current_object())
        db.session.commit()
        session["ldap_ok_at"] = now
        return None

    # ------------------------------------------------------------ Template helpers
    from .branding import context as brand_context
    from .branding import org_name as brand_org_name

    @app.context_processor
    def inject_globals():
        ctx = {
            "org_name": brand_org_name(),
            "ROLES": ROLES,
            "today": date.today(),
            "version": __version__,
            "support_url": SUPPORT_URL,
            "app_name": APP_NAME,
            "license_name": LICENSE_NAME,
            "author_url": AUTHOR_URL,
            "source_url": os.environ.get("ASSETLOTSE_SOURCE_URL", DEFAULT_SOURCE_URL),
            "imprint_url": get_setting("imprint_url"),
            "retention_months": get_int_setting("retention_months"),
            "privacy_url": get_setting("privacy_url"),
            # Cache key for CSS/JS without the version number (not publicly recognizable)
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
        # Outside a request (e.g. sending mail in the background) there is no user
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
        # Strict Content Security Policy: no inline or third-party JavaScript, no external sources.
        # (For SVG logos the route sets an even stricter policy of its own.)
        resp.headers.setdefault("Content-Security-Policy", CSP)
        # Never store signed-in pages (personal data, exports) in shared caches/CDNs or on the browser's
        # disk. A route's own header (images, logos, manual) is kept.
        if "Cache-Control" not in resp.headers and current_user.is_authenticated:
            resp.headers["Cache-Control"] = "private, no-store"
        return resp

    # ------------------------------------------------------------ Error pages
    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("error.html", code=403, title=_("Access denied"),
                               text=_("You don't have permission to do that.")), 403

    @app.errorhandler(OverflowError)
    def too_large_number(_e):
        # e.g. /admin/benutzer/99999999999999999999 – number too large for the database
        db.session.rollback()
        return not_found(None)

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404, title=_("Not found"),
                               text=_("This page or item does not exist.")), 404

    @app.errorhandler(IntegrityError)
    def integrity_error(_e):
        # Rare race (e.g. the same name created in two windows at once): no error 500, but back to the
        # form with a notice
        db.session.rollback()
        logging.getLogger(__name__).warning("Save rejected (unique value already exists): %s", _e.orig)
        flash(_("This could not be saved because the entry already "
                "exists. Please check and try again."), "error")
        from urllib.parse import urlsplit

        from .views import safe_next
        ref = urlsplit(request.referrer or "")
        back = (ref.path + (f"?{ref.query}" if ref.query else "")) if ref.netloc == request.host else ""
        return redirect(safe_next(back) or url_for("main.dashboard"))

    @app.errorhandler(CSRFError)
    def csrf_error(_e):
        return render_template("error.html", code=400, title=_("Session expired"),
                               text=_("Please reload the page and try again.")), 400

    # ------------------------------------------------------------ CLI
    @app.cli.command("reset-password")
    @click.argument("username")
    @click.password_option()
    def reset_password(username, password):
        """Reset the password of a local account (e.g. admin)."""
        user = User.query.filter_by(username=username).first()
        if not user:
            raise click.ClickException(f"User {username} not found.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(f"The password must have at least {MIN_PASSWORD_LENGTH} characters.")
        user.set_password(password)
        user.auth_source = "local"
        user.active = True
        db.session.commit()
        click.echo(f"Password set for {username}.")

    @app.cli.command("reset-2fa")
    @click.argument("username")
    def reset_2fa_cmd(username):
        """Reset an account's second factor (e.g. phone/security key lost)."""
        from .twofa import reset_2fa
        user = User.query.filter_by(username=username).first()
        if not user:
            raise click.ClickException(f"User {username} not found.")
        reset_2fa(user)
        click.echo(f"Two-factor sign-in reset for {username}.")

    @app.cli.command("send-reminders")
    def send_reminders():
        """Send due reminders/overdue notices now (e.g. via cron)."""
        from .mailer import format_stats, run_notifications
        click.echo(format_stats(run_notifications()))

    @app.cli.command("apply-retention")
    def apply_retention():
        """Apply the retention period now (delete old loans and the mail log)."""
        from .retention import format_result, run_retention
        click.echo(format_result(run_retention()))

    @app.cli.command("backup")
    def backup_cmd():
        """Back up the database now."""
        from .backup import run_now
        ok, msg = run_now("-manuell")   # stored file name suffix – parsed by backup.NAME_RE, keep
        if not ok:
            raise click.ClickException(msg)
        click.echo(msg)

    @app.cli.command("list-backups")
    def list_backups_cmd():
        """List existing backups (newest first)."""
        from .backup import backup_dir, human_size, list_backups
        click.echo(f"Folder: {backup_dir()}")
        for b in list_backups():
            click.echo(f"  {b['name']}  {human_size(b['size']):>8}  {b['kind']}")

    @app.cli.command("restore-backup")
    @click.argument("name")
    @click.option("--yes", is_flag=True, help="Do not ask for confirmation")
    def restore_backup_cmd(name, yes):
        """Restore a backup – ONLY with the server stopped! The current state is backed up first."""
        from .backup import BackupError, restore_backup
        if not yes:
            click.confirm(f"Replace the database with {name}? The server must be stopped for this.", abort=True)
        try:
            safety = restore_backup(name)
        except BackupError as exc:
            raise click.ClickException(str(exc))
        if safety:
            click.echo(f"Previous state backed up as {safety.name}")
        click.echo(f"{name} has been restored. Start the server again now.")

    @app.cli.command("create-admin")
    @click.argument("username")
    @click.password_option()
    def create_admin(username, password):
        """Create a local administrator account."""
        if User.query.filter_by(username=username).first():
            raise click.ClickException("User already exists.")
        user = User(username=username, display_name=username, role=ROLE_ADMIN, auth_source="local")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(f"The password must have at least {MIN_PASSWORD_LENGTH} characters.")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Administrator {username} created.")

    return app
