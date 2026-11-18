"""Diagnostic package (2.7.0): an anonymized ZIP an administrator can hand on for support.

Contents: README.txt, summary.txt (findings in plain text), diagnose.json (everything structured) and log.txt
(the short program log from the data folder, see applog.py).

What makes it anonymous
* Data is never exported row by row – only counts, states and the program's own results.
* Every text that leaves (log lines, error messages, results, settings) goes through ``Anonymizer``: e-mail
  addresses, IP addresses, host names, AD paths (CN=…,DC=…) and every word that occurs in a personal or
  identifying database field (user names, names, item data, groups, locations …) or in an identifying setting is
  replaced by a fixed pseudonym such as ``user-3``, ``email-2@example.invalid``, ``ip-4 (private)`` – the same
  value always gets the same pseudonym, so the connections stay visible.
* Secrets (passwords, keys, 2FA data, BitLocker data) are never read for the package.

Keeping it up to date – enforced by tests (tests/test_diagnose.py):
* Every setting the program uses must appear in exactly one of SETTINGS_SHOW / SETTINGS_ANON / SETTINGS_COUNT /
  SETTINGS_MASK, every environment variable in ENV_SHOW / ENV_ANON / ENV_MASK, every text column of every table
  in COLUMNS. A new setting, variable or column without that decision makes the test suite fail.
* Tables, row counts and the database schema are read generically from the models – new tables are included
  automatically.
"""
from __future__ import annotations

import io
import ipaddress
import json
import os
import platform
import re
import shutil
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

FORMAT = 1                  # structure of diagnose.json – raise on incompatible changes
LOG_LINES = 3000            # newest lines of the program log in log.txt

# ---------------------------------------------------------------- classification

# Settings: value shown as is (numbers, switches, choices, times written by the program)
SETTINGS_SHOW = {
    "default_language", "loan_days", "extension_days", "max_extensions", "max_loans", "self_checkout",
    "self_return", "self_extend", "catalog_visibility", "session_idle_hours", "session_idle_hours_admin",
    "session_idle_hours_external", "session_warn", "session_end_on_close", "passkey_login", "ldap_enabled",
    "ldap_security", "ldap_verify_cert", "ldap_check_hostname", "ldap_user_attr", "ldap_nested_groups",
    "ldap_group_sync", "ldap_cache_enabled", "ldap_cache_hours", "ldap_timeout", "ldap_cache_running",
    "ldap_cache_setup_confirmed", "ldap_cache_synced_at", "ldap_cache_attempt_at", "mail_enabled", "smtp_port",
    "smtp_security", "smtp_verify_cert", "mail_on_loan", "mail_on_return", "mail_reminder", "mail_reminder_days",
    "mail_overdue", "mail_overdue_repeat_days", "mail_overdue_max", "mail_digest", "mail_hour", "mail_last_run",
    "mail_retries", "mail_retry_after", "backup_enabled", "backup_keep", "backup_hour", "backup_last_ok",
    "retention_months", "mfa_internal", "mfa_policy", "registration_mode", "registration_verify_email",
    "registration_notify_admins", "registration_group_id", "brand_bg_dim", "brand_logo_plate", "brand_accent",
    "brand_brass", "brand_side", "login_show_shelf", "setup_completed", "app_version",
}
# Settings: value through the anonymizer (structure stays visible: scheme, port, IP class, error text)
SETTINGS_ANON = {
    "ldap_server", "ldap_ca_file", "ldap_bind_dn", "ldap_upn_suffix", "ldap_base_dn", "ldap_user_filter",
    "smtp_host", "smtp_user", "smtp_from", "smtp_reply_to", "app_base_url", "internal_networks",
    "mail_last_result", "backup_last_result", "backup_last_error", "retention_last_result", "ldap_cache_error",
    "ldap_cache_warning", "ldap_cache_result",
}
# Settings: only the number of entries (lines)
SETTINGS_COUNT = {
    "ldap_login_rules", "ldap_manager_rules", "ldap_admin_rules", "own_public_hosts", "registration_domains",
    "ldap_tls_certs", "ldap_cache_pending",
}
# Settings: only whether something is set (secrets and free texts that identify the organization)
SETTINGS_MASK = {
    "ldap_bind_password", "smtp_password", "org_name", "smtp_from_name", "mail_footer", "imprint_url",
    "privacy_url", "brand_logo", "brand_favicon", "brand_bg", "brand_logo_text", "login_eyebrow", "login_text",
}
# Settings whose value identifies the organization (hosts, domains, AD paths and groups, own texts) – their words
# are replaced in every text of the package
SETTINGS_IDENTIFYING = {
    "ldap_server", "ldap_ca_file", "ldap_bind_dn", "ldap_upn_suffix", "ldap_base_dn", "ldap_user_filter",
    "ldap_login_rules", "ldap_manager_rules", "ldap_admin_rules", "smtp_host", "smtp_user", "smtp_from",
    "smtp_reply_to", "app_base_url", "own_public_hosts", "registration_domains", "org_name", "smtp_from_name",
    "mail_footer", "imprint_url", "privacy_url", "brand_logo_text", "login_eyebrow", "login_text",
}

# Environment variables
ENV_SHOW = {
    "ASSETLOTSE_LOGLEVEL", "ASSETLOTSE_PORT", "ASSETLOTSE_SESSION_HOURS", "ASSETLOTSE_FORCE_HTTPS",
    "ASSETLOTSE_SECURE_COOKIES", "ASSETLOTSE_ACCEPT_NEW_KEY", "ASSETLOTSE_CONTAINER", "ASSETLOTSE_COMPOSE_VERSION",
    "ASSETLOTSE_MAINTENANCE", "ASSETLOTSE_SCHEDULER", "ASSETLOTSE_I18N_PARTS", "ASSETLOTSE_TAG",
    "ASSETLOTSE_BEHIND_PROXY", "TZ", "TZDIR", "PUID", "PGID", "FLASK_DEBUG", "PORT",
}
ENV_ANON = {"ASSETLOTSE_TRUSTED_PROXIES", "ASSETLOTSE_BIND", "ASSETLOTSE_DATA_DIR", "ASSETLOTSE_BACKUP_DIR",
            "ASSETLOTSE_IMAGE", "ASSETLOTSE_REGISTRY_IMAGE", "HOST"}
ENV_MASK = {"ASSETLOTSE_SECRET_KEY", "ASSETLOTSE_SOURCE_URL", "DATABASE_URL"}
# Public container registries and code hosts: not identifying, shown as they are (ghcr.io/…/assetlotse)
PUBLIC_HOSTS = {"ghcr.io", "docker.io", "registry-1.docker.io", "index.docker.io", "quay.io", "github.com",
                "gitlab.com", "codeberg.org", "lscr.io"}

# Text columns of the database: "personal" (identifies people, the organization or its inventory – its words
# are pseudonymized in every text of the package), "secret" (never read for the package) or "plain" (program
# values such as role or status – harmless).
COLUMNS = {
    "users.username": "user", "users.display_name": "person", "users.email": "email",
    "users.pending_email": "email", "users.ldap_dn": "dn", "users.ldap_guid": "id",
    "users.password_hash": "secret", "users.totp_secret": "secret", "users.recovery_codes": "secret",
    "users.auth_source": "plain", "users.role": "plain", "users.language": "plain", "users.news_seen": "plain",
    "directory_people.username": "user", "directory_people.username_key": "user",
    "directory_people.display_name": "person", "directory_people.email": "email",
    "groups.name": "group", "groups.description": "text", "groups.ldap_dn": "dn", "groups.color": "plain",
    "locations.name": "place", "locations.note": "text",
    "login_attempts.key": "user", "login_attempts.username": "user",
    "settings.key": "plain", "settings.value": "setting",       # handled by the SETTINGS_* sets
    "assets.name": "item", "assets.category": "item", "assets.manufacturer": "item", "assets.model": "item",
    "assets.serial_number": "item", "assets.inventory_no": "item", "assets.inventory_key": "item",
    "assets.serial_key": "item", "assets.description": "text", "assets.location": "place",
    "assets.image_file": "item", "assets.secret": "secret",
    "webauthn_credentials.name": "text", "webauthn_credentials.rp_id": "host",
    "webauthn_credentials.credential_id": "secret", "webauthn_credentials.credential_data": "secret",
    "loans.note": "text",
    "secret_access.asset_label": "item", "secret_access.user_label": "person", "secret_access.action": "plain",
    "mail_log.kind": "plain", "mail_log.status": "plain", "mail_log.to_addr": "email", "mail_log.subject": "text",
    "mail_log.error": "text",
}

# ---------------------------------------------------------------- anonymizer

_KEEP_IPS = {"127.0.0.1", "0.0.0.0", "::1", "::", "255.255.255.255"}
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# DN component: the value may contain spaces ("CN=Max Muster,OU=…") but ends before the next word without "="
_DN_PART = re.compile(r"\b(CN|OU|DC|O|L|ST|C|UID)=((?:[^,\\\n\"'<>]|\\.)+?)(?=,|$|[\"'<>)\]]|\s+[^\s=]*(?:\s|$))",
                      re.I | re.M)
_URL = re.compile(r"\b([a-z][a-z0-9+.-]*://)(?:([^\s/@:]+)(?::[^\s/@]*)?@)?(\[[0-9a-fA-F:.]+\]|[^\s/:?#\"'<>]+)")
_DOMAIN_USER = re.compile(r"(?<![\w\\])([A-Z][A-Z0-9-]{1,14})\\{1,2}([\w.$-]+)")   # NETBIOS\user
_IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(/\d{1,2})?(?![\w.])")
_IPV6 = re.compile(r"(?<![\w:])([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})(/\d{1,3})?(?![\w:])")
_TLDS = ("de", "com", "org", "net", "local", "lan", "intern", "internal", "intra", "corp", "home", "eu", "at",
         "ch", "io", "edu", "gov", "info", "biz", "dev", "app", "cloud", "online", "arpa", "test", "example")
_FQDN = re.compile(r"(?<![\w.@/-])((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+(?:" + "|".join(_TLDS)
                   + r"))(?![\w-])(?!\.\w)", re.I)
_LONG_TOKEN = re.compile(r"(?<![\w+=-])[A-Za-z0-9_+=-]{40,}(?![\w+=-])")
_WORD = re.compile(r"[^\W_]+")
CATEGORIES = ("user", "person", "email", "host", "ip", "dn", "id", "group", "place", "item", "text", "org")


class Anonymizer:
    """Replaces identifying parts of texts by stable pseudonyms (within one package)."""

    def __init__(self, stopwords: set[str] | None = None) -> None:
        self._map: dict[tuple[str, str], str] = {}
        self._count: dict[str, int] = {}
        self._words: dict[str, str] = {}        # lower-case word -> category
        self._stop = {w.lower() for w in (stopwords or set())} | {
            "example", "invalid", "private", "public", "loopback", "link", "local", "redacted", "user"} | set(CATEGORIES)

    # -- known values
    def add(self, category: str, value) -> None:
        """Remember the words of a personal value: they are replaced wherever they occur."""
        if not value or not isinstance(value, str):
            return
        for m in _EMAIL.finditer(value):
            self.pseudonym("email", m.group(0))
            self._add_words("host", m.group(0).split("@", 1)[1])
        for word in _WORD.findall(value):
            self._add_word(category, word)

    def _add_words(self, category: str, value: str) -> None:
        for word in _WORD.findall(value):
            self._add_word(category, word)

    def _add_word(self, category: str, word: str) -> None:
        w = word.lower()
        if w in self._stop or w in self._words:
            return
        if w.isdigit() and len(w) < 6:      # short numbers would hit line numbers, ports, counts
            return
        if not w.isdigit() and len(w) < 3:
            return
        self._words[w] = category

    # -- pseudonyms
    def pseudonym(self, category: str, value: str) -> str:
        key = (category, value.lower())
        if key not in self._map:
            n = self._count[category] = self._count.get(category, 0) + 1
            self._map[key] = f"{category}-{n}"
        return self._map[key]

    def _email(self, m: re.Match) -> str:
        return self.pseudonym("email", m.group(0)) + "@example.invalid"

    def _ip(self, text: str, prefix: str | None) -> str | None:
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            return None
        if text in _KEEP_IPS:
            return text + (prefix or "")
        kind = ("loopback" if ip.is_loopback else "link-local" if ip.is_link_local
                else "private" if ip.is_private else "public")
        return f"{self.pseudonym('ip', ip.compressed)}{prefix or ''} ({kind})"

    def _host(self, host: str) -> str:
        if not host:
            return host
        bare = host.strip("[]")
        ip = self._ip(bare, None)
        if ip is not None:
            return ip
        if bare.lower() == "localhost" or bare.lower() in PUBLIC_HOSTS:
            return bare
        return self.pseudonym("host", bare)

    def scrub(self, text) -> str:
        if text is None:
            return ""
        s = str(text)
        if not s:
            return s
        s = _EMAIL.sub(self._email, s)
        s = _URL.sub(lambda m: m.group(1) + ("user@" if m.group(2) else "") + self._host(m.group(3)), s)
        s = _DN_PART.sub(lambda m: f"{m.group(1)}={self.pseudonym('dn', m.group(2).strip())}", s)
        s = _DOMAIN_USER.sub(self._domain_user, s)
        s = _IPV4.sub(lambda m: self._ip(m.group(1), m.group(2)) or m.group(0), s)
        s = _IPV6.sub(self._ipv6, s)
        s = _FQDN.sub(self._fqdn, s)
        s = _LONG_TOKEN.sub("<redacted>", s)
        return self._replace_words(s)

    def _domain_user(self, m: re.Match) -> str:
        return f"{self.pseudonym('host', m.group(1))}\\{self.pseudonym('user', m.group(2))}"

    def _ipv6(self, m: re.Match) -> str:
        if m.group(1).count(":") < 2 or not re.search(r"[0-9A-Fa-f]", m.group(1)):
            return m.group(0)
        return self._ip(m.group(1), m.group(2)) or m.group(0)

    def _fqdn(self, m: re.Match) -> str:
        if m.group(1).lower() in PUBLIC_HOSTS:
            return m.group(0)
        if m.group(1).split(".", 1)[0].lower() in _module_names():   # a Python module path, not a host name
            return m.group(0)
        return self.pseudonym("host", m.group(1))

    def _replace_words(self, s: str) -> str:
        if not self._words:
            return s

        def repl(m: re.Match) -> str:
            word = m.group(0)
            category = self._words.get(word.lower())
            return word if category is None else self.pseudonym(category, word)
        return _WORD.sub(repl, s)


_MODULES: set[str] = set()


def _module_names() -> set[str]:
    """Top-level names of importable Python modules (werkzeug, sqlalchemy …)."""
    if not _MODULES:
        from importlib import metadata
        try:
            _MODULES.update(n.lower() for n in metadata.packages_distributions())
        except Exception:  # noqa: BLE001
            pass
        _MODULES.update(n.split(".", 1)[0].lower() for n in list(sys.modules))
        _MODULES.update(n.lower() for n in sys.stdlib_module_names)
    return _MODULES


def program_words() -> set[str]:
    """Words of the program itself (code, log messages) and installed packages: never pseudonymized,
    so log lines and tracebacks stay readable even if a user is called "Error"."""
    words: set[str] = set()
    base = Path(__file__).resolve().parent
    for p in base.rglob("*.py"):
        try:
            words.update(w.lower() for w in _WORD.findall(p.read_text(encoding="utf-8")))
        except OSError:
            continue
    words.update(m.split(".", 1)[0].lower() for m in sys.modules)
    return words


def build_anonymizer() -> Anonymizer:
    """Anonymizer that knows every personal value in the database and every identifying setting."""
    import sqlalchemy as sa

    from .models import Setting, db
    anon = Anonymizer(program_words())
    order = ["user", "person", "email", "dn", "host", "id", "group", "place", "item", "text"]
    by_category: dict[str, list] = {}
    for table in db.metadata.sorted_tables:
        for col in table.columns:
            if not isinstance(col.type, (sa.String, sa.Text)):
                continue
            category = COLUMNS.get(f"{table.name}.{col.name}", "text")   # unknown: treat as personal
            if category in ("plain", "secret", "setting"):
                continue
            by_category.setdefault(category, []).append(col)
    for category in order + [c for c in by_category if c not in order]:
        for col in by_category.get(category, []):
            try:
                for (value,) in db.session.execute(sa.select(col).where(col.isnot(None)).distinct()):
                    anon.add(category, value)
            except Exception:  # noqa: BLE001 – a missing column must not stop the package
                db.session.rollback()
    from .models import SECRET_SETTINGS
    for row in Setting.query.all():
        if row.key in SETTINGS_IDENTIFYING or (row.key in SETTINGS_MASK and row.key not in SECRET_SETTINGS):
            anon.add("org", row.value)
    return anon


# ---------------------------------------------------------------- helpers

def _setting_class(key: str) -> str:
    if key in SETTINGS_SHOW:
        return "show"
    if key in SETTINGS_ANON:
        return "anon"
    if key in SETTINGS_COUNT:
        return "count"
    return "mask"        # unknown keys: the safe choice


def _setting_plain(key: str, value: str) -> str:
    from .models import SECRET_SETTINGS
    if key in SECRET_SETTINGS:
        return ""
    return value or ""


def _lines(value: str) -> int:
    return len([ln for ln in (value or "").splitlines() if ln.strip()])


def _safe(fn, anon: Anonymizer):
    """Run one section; an error there is recorded instead of breaking the whole package."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        try:
            from .models import db
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"section_error": f"{type(exc).__name__}: {anon.scrub(exc)}"}


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat(timespec="seconds") if isinstance(value, datetime) else value.isoformat()
    return str(value)


def _proc_uptime(pid: str) -> int | None:
    try:
        ticks = os.sysconf("SC_CLK_TCK")
        start = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]) / ticks
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        return int(uptime - start)
    except Exception:  # noqa: BLE001
        return None


def _meminfo() -> dict:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            if k in ("MemTotal", "MemAvailable"):
                out[k] = int(v.split()[0]) * 1024
    except Exception:  # noqa: BLE001
        pass
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        out["cgroup_limit"] = None if raw == "max" else int(raw)
        out["cgroup_current"] = int(Path("/sys/fs/cgroup/memory.current").read_text().strip())
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------- sections

def _runtime(anon: Anonymizer, req) -> dict:
    from . import __version__, compose_outdated
    now = datetime.now().astimezone()
    try:
        load = os.getloadavg()
    except OSError:
        load = None
    return {
        "version": __version__,
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": platform.system(), "release": platform.release(), "machine": platform.machine(),
        "container": os.environ.get("ASSETLOTSE_CONTAINER") == "1",
        "server": anon.scrub(req.environ.get("SERVER_SOFTWARE", "")) if req is not None else "cli",
        "pid": os.getpid(), "process_uptime_s": _proc_uptime("self"), "container_uptime_s": _proc_uptime("1"),
        "uid": os.getuid(), "gid": os.getgid(),
        "cpus": os.cpu_count(), "loadavg": load, "memory": _meminfo(),
        "local_time": now.isoformat(timespec="seconds"), "utc_offset": now.strftime("%z"),
        "tz": os.environ.get("TZ", ""), "tzname": now.tzname(),
        "zoneinfo_present": Path("/usr/share/zoneinfo").is_dir(),
        "compose_outdated": compose_outdated() or None,
        "maintenance": os.environ.get("ASSETLOTSE_MAINTENANCE") == "1",
        "scheduler": os.environ.get("ASSETLOTSE_SCHEDULER", ""),
    }


def _packages() -> dict:
    from importlib import metadata
    out = {}
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if name:
            out[name.lower()] = dist.version
    return dict(sorted(out.items()))


def _storage(app, anon: Anonymizer) -> dict:
    from . import applog, backup
    instance = Path(app.instance_path)
    out: dict = {"data_dir": anon.scrub(str(instance))}
    try:
        probe = instance / f".diag-write-test-{os.getpid()}"
        probe.write_text("x")
        probe.unlink()
        out["data_dir_writable"] = True
    except OSError as exc:
        out["data_dir_writable"] = False
        out["data_dir_error"] = anon.scrub(exc)
    try:
        du = shutil.disk_usage(instance)
        out["disk"] = {"total": du.total, "free": du.free}
    except OSError:
        pass
    db_file = backup.database_file()
    if db_file is not None:
        out["database_file"] = {"size": db_file.stat().st_size if db_file.exists() else None}
        for suffix in ("-wal", "-shm"):
            side = db_file.with_name(db_file.name + suffix)
            if side.exists():
                out["database_file"][suffix.strip("-")] = side.stat().st_size
    out["images"] = sum(1 for _ in (instance / "images").glob("*")) if (instance / "images").is_dir() else 0
    out["https_hosts_known"] = (len([ln for ln in (instance / "https-hosts.txt").read_text().splitlines() if ln.strip()])
                                if (instance / "https-hosts.txt").is_file() else 0)
    out["log_files"] = {p.name: p.stat().st_size for p in applog.log_files(instance)}
    return out


def _backups(anon: Anonymizer) -> dict:
    from . import backup
    from .models import get_bool, get_setting
    out: dict = {"supported": backup.supported(), "enabled": get_bool("backup_enabled"),
                 "keep": backup.keep_count(), "custom_dir": bool(os.environ.get("ASSETLOTSE_BACKUP_DIR")),
                 "last_ok": get_setting("backup_last_ok") or None,
                 "last_result": anon.scrub(get_setting("backup_last_result")) or None,
                 "last_error": anon.scrub(get_setting("backup_last_error")) or None,
                 "warning": anon.scrub(backup.warning()) or None}
    if out["supported"]:
        items = backup.list_backups()
        out["count"] = len(items)
        out["total_size"] = sum(i["size"] for i in items)
        if items:
            newest = items[0]["created"]
            out["newest"] = _iso(newest)
            out["newest_age_hours"] = round((datetime.now() - newest).total_seconds() / 3600, 1)
        kinds: dict = {}
        for i in items:
            kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
        out["kinds"] = kinds
        try:
            out["same_disk_as_data"] = os.stat(backup.backup_dir()).st_dev == os.stat(backup.database_file()).st_dev
        except Exception:  # noqa: BLE001
            pass
    return out


def _database(anon: Anonymizer) -> dict:
    import sqlalchemy as sa

    from .models import db
    engine = db.engine
    out: dict = {"dialect": engine.dialect.name, "driver": engine.dialect.driver}
    if engine.dialect.name == "sqlite":
        import sqlite3
        out["sqlite_version"] = sqlite3.sqlite_version
        with engine.connect() as conn:
            out["journal_mode"] = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
            out["quick_check"] = anon.scrub(conn.exec_driver_sql("PRAGMA quick_check").scalar())
            out["foreign_keys"] = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
    else:
        try:
            with engine.connect() as conn:
                out["server_version"] = ".".join(map(str, engine.dialect.server_version_info or ()))
        except Exception:  # noqa: BLE001
            pass
    inspector = sa.inspect(engine)
    existing = set(inspector.get_table_names())
    tables, missing_tables, missing_columns, extra_columns = {}, [], {}, {}
    for table in db.metadata.sorted_tables:
        if table.name not in existing:
            missing_tables.append(table.name)
            continue
        have = {c["name"] for c in inspector.get_columns(table.name)}
        want = {c.name for c in table.columns}
        if want - have:
            missing_columns[table.name] = sorted(want - have)
        if have - want:
            extra_columns[table.name] = sorted(have - want)
        try:
            tables[table.name] = db.session.execute(sa.select(sa.func.count()).select_from(table)).scalar()
        except Exception:  # noqa: BLE001
            db.session.rollback()
            tables[table.name] = None
    out.update(tables=tables, missing_tables=missing_tables, missing_columns=missing_columns,
               extra_columns=extra_columns, unknown_tables=sorted(existing - {t.name for t in db.metadata.sorted_tables}))
    return out


def _users() -> dict:
    from .models import ROLE_ADMIN, User, WebAuthnCredential, db
    users = User.query.all()
    since = datetime.now() - timedelta(days=30)
    roles: dict = {}
    sources: dict = {}
    for u in users:
        roles[u.role] = roles.get(u.role, 0) + 1
        sources[u.auth_source or "local"] = sources.get(u.auth_source or "local", 0) + 1
    admins = [u for u in users if u.role == ROLE_ADMIN and u.active]
    passkey_users = {c.user_id for c in WebAuthnCredential.query.filter_by(passkey=True)}
    return {
        "total": len(users), "active": sum(u.active for u in users), "roles": roles, "sources": sources,
        "active_admins": len(admins), "active_admins_without_2fa": sum(not u.has_2fa for u in admins),
        "with_totp": sum(bool(u.totp_enabled) for u in users),
        "security_keys": WebAuthnCredential.query.filter_by(passkey=False).count(),
        "passkeys": WebAuthnCredential.query.filter_by(passkey=True).count(),
        "users_with_passkey": len(passkey_users),
        "passkey_hint_off": sum(bool(u.passkey_hint_off) for u in users),
        "self_registered": sum(bool(u.self_registered) for u in users),
        "pending_approval": sum(bool(u.pending_approval) for u in users),
        "email_unverified": sum(not u.email_verified for u in users),
        "deactivated_by_sync": sum(bool(u.deactivated_by_sync) for u in users),
        "signed_in_last_30_days": sum(1 for u in users if u.last_login and u.last_login >= since),
        "without_email": sum(1 for u in users if not (u.email or "").strip()),
        "manages_all": sum(bool(u.manages_all) for u in users),
        "languages": {lang or "auto": n for lang, n in db.session.query(User.language, db.func.count()).group_by(User.language)},
    }


def _inventory() -> dict:
    from .models import Asset, Group, Loan, Location
    today = date.today()
    open_loans = Loan.query.filter(Loan.returned_at.is_(None))
    return {
        "assets": Asset.query.count(), "groups": Group.query.count(),
        "groups_from_ad": Group.query.filter(Group.ldap_dn != "").count(),
        "locations": Location.query.count(),
        "loans_open": open_loans.count(),
        "loans_overdue": open_loans.filter(Loan.due_at.isnot(None), Loan.due_at < today).count(),
        "loans_open_ended": open_loans.filter(Loan.due_at.is_(None)).count(),
        "loans_total": Loan.query.count(),
    }


def _security(app, anon: Anonymizer) -> dict:
    from . import twofa
    from .models import LoginAttempt, get_setting
    since = datetime.now() - timedelta(hours=24)
    env_key = os.environ.get("ASSETLOTSE_SECRET_KEY", "").strip()
    return {
        "secret_key_source": "env" if env_key else "file",
        "secret_key_env_short": bool(env_key) and len(env_key) < 32,
        "trusted_proxies_default": app.config.get("TRUSTED_PROXIES_DEFAULT"),
        "force_https": app.config.get("FORCE_HTTPS"),
        "secure_cookies": (os.environ.get("ASSETLOTSE_SECURE_COOKIES") or "auto"),
        "mfa_policy": get_setting("mfa_policy"),
        "passkey_login": twofa.passkey_login_enabled(),
        "failed_sign_ins_24h": LoginAttempt.query.filter(LoginAttempt.created_at >= since).count(),
        "internal_networks": anon.scrub(get_setting("internal_networks")),
    }


def _request(req, anon: Anonymizer) -> dict | None:
    if req is None:
        return None
    host = req.host.rsplit(":", 1)[0] if not req.host.startswith("[") else req.host.split("]")[0] + "]"
    try:
        ipaddress.ip_address(host.strip("[]"))
        host_kind = "ip"
    except ValueError:
        host_kind = "localhost" if host == "localhost" else "domain"
    forwarded = sorted(h for h in req.headers.keys() if h.lower().startswith(("x-forwarded-", "forwarded", "x-real-ip")))
    ua = req.user_agent.string or ""
    browser = next((name for name, pat in (("Edge", "Edg/"), ("Firefox", "Firefox/"), ("Chrome", "Chrome/"),
                                           ("Safari", "Safari/")) if pat in ua), "other")
    device = ("iOS" if re.search(r"iPhone|iPad", ua) else "Android" if "Android" in ua else
              "macOS" if "Mac OS X" in ua else "Windows" if "Windows" in ua else "Linux" if "Linux" in ua else "other")
    return {
        "secure": req.is_secure, "scheme": req.scheme, "host_kind": host_kind, "host": anon.scrub(req.host),
        "client": anon.scrub(req.remote_addr or ""), "forwarded_headers": forwarded,
        "x_forwarded_proto": req.headers.get("X-Forwarded-Proto"),
        "via_proxy": bool(req.environ.get("assetlotse.proxied") or forwarded),
        "browser": browser, "device": device,
    }


def _mail(anon: Anonymizer) -> dict:
    from .models import MailLog, get_bool, get_setting
    now = datetime.now()
    out: dict = {"enabled": get_bool("mail_enabled"), "host_set": bool(get_setting("smtp_host")),
                 "last_run": get_setting("mail_last_run") or None,
                 "last_result": anon.scrub(get_setting("mail_last_result")) or None}
    for days in (7, 30):
        counts: dict = {}
        for status, in MailLog.query.with_entities(MailLog.status).filter(MailLog.created_at >= now - timedelta(days=days)):
            counts[status] = counts.get(status, 0) + 1
        out[f"last_{days}_days"] = counts
    out["recent_errors"] = [
        {"at": _iso(m.created_at), "kind": m.kind, "error": anon.scrub(m.error)}
        for m in MailLog.query.filter_by(status="error").order_by(MailLog.created_at.desc()).limit(10)]
    return out


def _ldap(anon: Anonymizer) -> dict:
    from . import ad_cache
    from .models import get_bool, get_setting
    server = get_setting("ldap_server")
    out: dict = {"enabled": get_bool("ldap_enabled"), "server": anon.scrub(server),
                 "security": get_setting("ldap_security"), "verify_cert": get_bool("ldap_verify_cert"),
                 "service_account": bool(get_setting("ldap_bind_dn")),
                 "rules": {k: _lines(get_setting(k)) for k in ("ldap_login_rules", "ldap_manager_rules", "ldap_admin_rules")},
                 "own_ca_certificates": (get_setting("ldap_tls_certs") or "").count("BEGIN CERTIFICATE")}
    try:
        st = ad_cache.status()
        out["cache"] = {"enabled": st["enabled"], "available": st["available"], "synced_at": _iso(st["synced_at"]),
                        "people": st["count"], "error": anon.scrub(st["error"]) or None,
                        "warning": anon.scrub(st["warning"]) or None, "pending": len(st["pending"] or []),
                        "running": st["running"], "hours": st["hours"]}
    except Exception as exc:  # noqa: BLE001
        out["cache"] = {"section_error": f"{type(exc).__name__}: {anon.scrub(exc)}"}
    return out


def _settings(anon: Anonymizer) -> dict:
    from .models import DEFAULT_SETTINGS, Setting
    stored = {row.key: row.value for row in Setting.query.all()}
    out = {}
    for key in sorted(set(DEFAULT_SETTINGS) | set(stored)):
        value = stored.get(key)
        cls = _setting_class(key)
        entry: dict = {"stored": key in stored}
        if value is None:
            value = str(DEFAULT_SETTINGS.get(key, ""))
        if cls == "show":
            entry["value"] = anon.scrub(value)
        elif cls == "anon":
            entry["value"] = anon.scrub(_setting_plain(key, value))
        elif cls == "count":
            entry["entries"] = _lines(value)
        else:
            entry["set"] = bool((value or "").strip())
        if key in stored and key in DEFAULT_SETTINGS:
            entry["default"] = stored[key] == str(DEFAULT_SETTINGS[key])
        out[key] = entry
    return out


def _environment(anon: Anonymizer) -> dict:
    out = {}
    names = ENV_SHOW | ENV_ANON | ENV_MASK | {k for k in os.environ if k.startswith(("ASSETLOTSE_", "HELFI_"))}
    for name in sorted(names):
        if name not in os.environ:
            continue
        value = os.environ[name]
        if name in ENV_SHOW:
            out[name] = anon.scrub(value)
        elif name in ENV_ANON:
            out[name] = anon.scrub(value)
        elif name == "DATABASE_URL":
            out[name] = {"set": bool(value.strip()), "scheme": value.split(":", 1)[0] if ":" in value else ""}
        else:
            out[name] = {"set": bool(value.strip()), "length": len(value.strip())}
    return out


def _log(app, anon: Anonymizer | None) -> tuple[list[str], dict]:
    from . import applog
    lines: list[str] = []
    for p in applog.log_files(Path(app.instance_path)):
        try:
            lines.extend(p.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    lines = lines[-LOG_LINES:]
    stats: dict = {"lines": len(lines)}
    since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    for level in ("WARNING", "ERROR", "CRITICAL"):
        stats[level.lower() + "_24h"] = sum(1 for ln in lines if f" {level} " in ln[:60] and ln[:19] >= since)
    stats["first"] = lines[0][:19] if lines else None
    return ([anon.scrub(ln) for ln in lines] if anon is not None else []), stats


# ---------------------------------------------------------------- findings

def findings(report: dict) -> list[dict]:
    """Things worth a look – code, level (error|warning|info) and parameters. Text: see finding_text."""
    out: list[dict] = []

    def add(code, level, **params):
        out.append({"code": code, "level": level, "params": params})

    db_ = report.get("database") or {}
    if db_.get("quick_check") not in (None, "ok"):
        add("db_check", "error", result=db_.get("quick_check"))
    if db_.get("missing_tables") or db_.get("missing_columns"):
        add("db_schema", "error", tables=", ".join(db_.get("missing_tables") or []),
            columns=", ".join(f"{t}.{c}" for t, cols in (db_.get("missing_columns") or {}).items() for c in cols))
    st = report.get("storage") or {}
    if st.get("data_dir_writable") is False:
        add("data_dir_readonly", "error")
    disk = st.get("disk") or {}
    if disk.get("free") is not None and disk["free"] < 500 * 1024 * 1024:
        add("disk_low", "warning", free_mb=disk["free"] // (1024 * 1024))
    rt = report.get("runtime") or {}
    if rt.get("compose_outdated"):
        add("compose_outdated", "warning", found=rt["compose_outdated"])
    if not rt.get("zoneinfo_present", True):
        add("no_zoneinfo", "warning")
    if rt.get("maintenance"):
        add("maintenance", "info")
    bk = report.get("backups") or {}
    if bk.get("supported"):
        if not bk.get("enabled"):
            add("backup_off", "warning")
        elif not bk.get("count"):
            add("backup_none", "warning")
        elif bk.get("newest_age_hours") is None or bk["newest_age_hours"] > 48:
            add("backup_old", "warning", hours=bk.get("newest_age_hours"))
        if bk.get("last_error"):
            add("backup_error", "warning", error=bk["last_error"])
        if bk.get("same_disk_as_data"):
            add("backup_same_disk", "info")
    sec = report.get("security") or {}
    if sec.get("secret_key_env_short"):
        add("secret_short", "warning")
    us = report.get("users") or {}
    if us.get("active_admins") == 0:
        add("no_admin", "error")
    elif us.get("active_admins") == 1:
        add("one_admin", "info")
    if us.get("active_admins_without_2fa"):
        add("admins_without_2fa", "warning", n=us["active_admins_without_2fa"])
    rq = report.get("request")
    if rq and not rq.get("secure"):
        add("no_https", "warning")
    ml = report.get("mail") or {}
    if ml.get("enabled") and not ml.get("host_set"):
        add("mail_no_host", "warning")
    errors = (ml.get("last_7_days") or {}).get("error", 0)
    if errors:
        add("mail_errors", "warning", n=errors)
    ld = report.get("ldap") or {}
    if ld.get("enabled"):
        if not ld.get("verify_cert"):
            add("ldap_no_verify", "warning")
        if not ld.get("service_account"):
            add("ldap_no_service_account", "info")
        cache = ld.get("cache") or {}
        if cache.get("error"):
            add("ldap_cache_error", "warning", error=cache["error"])
    lg = report.get("log") or {}
    if lg.get("error_24h") or lg.get("critical_24h"):
        add("log_errors", "warning", n=(lg.get("error_24h") or 0) + (lg.get("critical_24h") or 0))
    for name, section in report.items():
        if isinstance(section, dict) and "section_error" in section:
            add("section_failed", "warning", section=name, error=section["section_error"])
    rank = {"error": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda f: rank.get(f["level"], 3))


_TEXTS = {
    "db_check": "Database check reports a problem: {result}",
    "db_schema": "Database structure incomplete – missing tables: {tables}; missing columns: {columns}",
    "data_dir_readonly": "The data folder cannot be written.",
    "disk_low": "Little disk space left in the data folder: {free_mb} MB.",
    "compose_outdated": "docker-compose.yml on the server is out of date (version {found}) – run ./update.sh.",
    "no_zoneinfo": "Time zone data is missing – times are in UTC.",
    "maintenance": "Maintenance mode is on.",
    "backup_off": "Automatic backups are switched off.",
    "backup_none": "There is no backup yet.",
    "backup_old": "The newest backup is older than two days ({hours} hours).",
    "backup_error": "The last backup failed: {error}",
    "backup_same_disk": "Backups are on the same disk as the database – copy them elsewhere regularly.",
    "secret_short": "ASSETLOTSE_SECRET_KEY is shorter than 32 characters.",
    "no_admin": "There is no active administrator.",
    "one_admin": "Only one active administrator – a second one helps if you lock yourself out.",
    "admins_without_2fa": "{n} administrator(s) without a second factor.",
    "no_https": "This page was not opened over HTTPS – passkeys and secure cookies need HTTPS.",
    "mail_no_host": "E-mail is switched on, but no SMTP server is entered.",
    "mail_errors": "{n} e-mail(s) failed in the last 7 days.",
    "ldap_no_verify": "The Active Directory certificate is not checked.",
    "ldap_no_service_account": "Active Directory without service account – AD accounts cannot use passkeys and "
                               "people cannot be searched at the desk.",
    "ldap_cache_error": "Active Directory sync reports an error: {error}",
    "log_errors": "{n} error(s) in the program log in the last 24 hours.",
    "section_failed": "Part \"{section}\" could not be read: {error}",
}


def finding_text(finding: dict, translate=None) -> str:
    """Text of a finding – English, or translated with ``translate`` (i18n._) for the page."""
    text = _TEXTS.get(finding["code"], finding["code"])
    params = {k: ("–" if v in (None, "") else v) for k, v in finding.get("params", {}).items()}
    if translate is not None:
        return translate(text, **params)
    return text.format(**params)


# ---------------------------------------------------------------- package

def collect(app, req=None, with_log: bool = True) -> tuple[dict, list[str]]:
    """The report (dict) and the anonymized log lines (none with with_log=False – only the log statistics)."""
    if with_log:
        anon = _safe(build_anonymizer, Anonymizer())
        if not isinstance(anon, Anonymizer):          # building it failed: keep going with a strict one
            anon = Anonymizer(program_words())
    else:
        anon = Anonymizer()       # page view: only for the administrator's own eyes, no package leaves
    report: dict = {
        "format": FORMAT,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "web" if req is not None else "cli",
    }
    report["runtime"] = _safe(lambda: _runtime(anon, req), anon)
    report["request"] = _safe(lambda: _request(req, anon), anon)
    report["storage"] = _safe(lambda: _storage(app, anon), anon)
    report["database"] = _safe(lambda: _database(anon), anon)
    report["backups"] = _safe(lambda: _backups(anon), anon)
    report["security"] = _safe(lambda: _security(app, anon), anon)
    report["users"] = _safe(_users, anon)
    report["inventory"] = _safe(_inventory, anon)
    report["mail"] = _safe(lambda: _mail(anon), anon)
    report["ldap"] = _safe(lambda: _ldap(anon), anon)
    report["settings"] = _safe(lambda: _settings(anon), anon)
    report["environment"] = _safe(lambda: _environment(anon), anon)
    report["packages"] = _safe(_packages, anon)
    log_lines: list[str] = []
    try:
        log_lines, report["log"] = _log(app, anon if with_log else None)
    except Exception as exc:  # noqa: BLE001
        report["log"] = {"section_error": f"{type(exc).__name__}: {anon.scrub(exc)}"}
    report["findings"] = findings(report)
    report["pseudonyms"] = {k: v for k, v in sorted(anon._count.items())}   # how many of each kind
    return report, log_lines


README = """AssetLotse – Diagnosepaket / diagnostic package
=================================================

DE: Dieses Paket beschreibt den Zustand einer AssetLotse-Installation, damit bei Problemen geholfen werden kann.
Es ist anonymisiert: Es enthält keine Datensätze (nur Anzahlen), keine Passwörter, Schlüssel, 2FA- oder
BitLocker-Daten. Namen, Benutzernamen, E-Mail-Adressen, IP-Adressen, Rechnernamen, AD-Pfade sowie Angaben zu
Gegenständen, Gruppen und Standorten sind durch Platzhalter ersetzt (z. B. user-3, ip-2 (private)). Gleiche
Werte bekommen innerhalb eines Pakets denselben Platzhalter; eine Zuordnungstabelle gibt es nicht.

EN: This package describes the state of an AssetLotse installation so that problems can be diagnosed.
It is anonymized: no records (only counts), no passwords, keys, 2FA or BitLocker data. Names, user names, e-mail
addresses, IP addresses, host names, AD paths and details of items, groups and locations are replaced by
placeholders (e.g. user-3, ip-2 (private)). Equal values get the same placeholder within one package; there is
no mapping table.

Files: summary.txt (findings), diagnose.json (all details), log.txt (newest lines of the program log).
"""


def summary_text(report: dict) -> str:
    rt = report.get("runtime") or {}
    lines = [f"AssetLotse {rt.get('version', '?')} – diagnostic package (format {report['format']})",
             f"Created: {report['created_utc']} UTC ({report['source']})",
             f"Python {rt.get('python', '?')} on {rt.get('system', '?')} {rt.get('machine', '')}, "
             f"container: {rt.get('container')}", "", "Findings:"]
    found = report.get("findings") or []
    if not found:
        lines.append("  none")
    for f in found:
        lines.append(f"  [{f['level']}] {finding_text(f)}")
    return "\n".join(lines) + "\n"


def build_zip(app, req=None) -> tuple[bytes, str]:
    """The ZIP file (bytes) and its file name."""
    from . import __version__
    report, log_lines = collect(app, req)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"assetlotse-diagnose-{__version__}-{stamp}.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", README)
        zf.writestr("summary.txt", summary_text(report))
        zf.writestr("diagnose.json", json.dumps(report, indent=2, ensure_ascii=False, default=str))
        zf.writestr("log.txt", "\n".join(log_lines) + ("\n" if log_lines else ""))
    return buf.getvalue(), name


def live_findings(app, req=None) -> list[dict]:
    """Findings for the admin page (without building the log and the package)."""
    report, _ = collect(app, req, with_log=False)
    return report["findings"]

