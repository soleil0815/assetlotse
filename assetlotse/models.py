"""AssetLotse data model."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, func
from sqlalchemy.engine import Engine
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    """Enable foreign keys and WAL for SQLite."""
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA secure_delete=ON")   # overwrite deleted data (e.g. old keys) with zeros
        cur.close()


MIN_PASSWORD_LENGTH = 10   # for all new passwords (setup, profile, administration, registration)

ROLE_READER = "reader"
ROLE_MANAGER = "manager"
ROLE_ADMIN = "admin"
from .i18n import _l  # noqa: E402

ROLES = {
    ROLE_READER: _l("Staff member"),
    ROLE_MANAGER: _l("Manager"),
    ROLE_ADMIN: _l("Administrator"),
}

user_groups = db.Table(
    "user_groups",
    db.Column("user_id", db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    db.Column("group_id", db.Integer, db.ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)


class Group(db.Model):
    __tablename__ = "groups"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    description = db.Column(db.String(500), default="")
    # Optional: DN of an AD group – members are assigned automatically at sign-in
    ldap_dn = db.Column(db.String(500), default="")
    color = db.Column(db.String(16), default="#0b7285")
    # No lending: items of this group are never issued (e.g. permanently installed or internal-only
    # devices) – neither at the desk nor from the inventory nor via self-checkout.
    no_lending = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)

    assets = db.relationship("Asset", back_populates="group")
    users = db.relationship("User", secondary=user_groups, back_populates="groups")

    def __repr__(self):
        return f"<Group {self.name}>"


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(200), default="")
    email = db.Column(db.String(200), default="")
    password_hash = db.Column(db.String(256))
    auth_source = db.Column(db.String(10), default="local")  # local | ldap
    ldap_dn = db.Column(db.String(500), default="")
    ldap_guid = db.Column(db.String(64), default="", index=True)   # fixed AD identifier (to detect renames)
    # Deactivated by the AD sync (disabled/deleted/no longer authorized in AD) – becomes active again
    # automatically as soon as the person is authorized in AD again. Manually disabled accounts stay disabled.
    deactivated_by_sync = db.Column(db.Boolean, default=False, nullable=False)
    # 2.6.1: "Don't show again" on the passkey hint after a password sign-in on a phone
    passkey_hint_off = db.Column(db.Boolean, default=False, nullable=False)
    role = db.Column(db.String(20), default=ROLE_READER, nullable=False)
    # Manager of the entire inventory (all groups) – only ever set explicitly, never as a side effect: formerly
    # a "manager without a group" counted as manager of everything; then it was enough to take away someone's
    # last group (e.g. via the AD sync) and they suddenly managed all groups.
    manages_all = db.Column(db.Boolean, default=False, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    last_login = db.Column(db.DateTime)
    email_notifications = db.Column(db.Boolean, default=True, nullable=False)
    # Self-registration
    self_registered = db.Column(db.Boolean, default=False, nullable=False)
    email_verified = db.Column(db.Boolean, default=True, nullable=False)
    pending_approval = db.Column(db.Boolean, default=False, nullable=False)
    # Two-factor sign-in
    totp_secret = db.Column(db.Text, default="")          # encrypted (Fernet, key derived from SECRET_KEY)
    totp_enabled = db.Column(db.Boolean, default=False, nullable=False)
    totp_last_step = db.Column(db.Integer, default=0, nullable=False)  # against reuse of a code
    recovery_codes = db.Column(db.Text, default="")       # JSON list of hashed one-time codes
    # Incremented to invalidate all existing sessions and "Stay signed in" cookies
    auth_epoch = db.Column(db.Integer, default=0, nullable=False)
    # Language of the UI and of emails ("" = browser or default language)
    language = db.Column(db.String(5), default="", nullable=False)
    # News: read up to this program version ("" = never opened)
    news_seen = db.Column(db.String(20), default="", nullable=False)
    # New email address that still has to be confirmed via link (self-registered accounts)
    pending_email = db.Column(db.String(200), default="", nullable=False)

    groups = db.relationship("Group", secondary=user_groups, back_populates="users")
    loans = db.relationship("Loan", back_populates="user", foreign_keys="Loan.user_id")

    # --- Flask-Login ---
    @property
    def is_active(self):  # noqa: D401
        return bool(self.active)

    def get_id(self) -> str:
        # The ID contains the "session generation": incrementing it immediately invalidates all old
        # sessions and remember cookies of this account.
        return f"{self.id}:{self.auth_epoch or 0}"

    def end_all_sessions(self):
        self.auth_epoch = (self.auth_epoch or 0) + 1

    # --- Passwords ---
    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)
        if self.id is not None:   # existing account: sign out other devices
            self.end_all_sessions()

    def check_password(self, password: str) -> bool:
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    # --- Roles ---
    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_manager(self) -> bool:
        return self.role in (ROLE_MANAGER, ROLE_ADMIN)

    @property
    def role_label(self) -> str:
        return str(ROLES.get(self.role, self.role))

    @property
    def name(self) -> str:
        return self.display_name or self.username

    @property
    def initials(self) -> str:
        parts = [p for p in self.name.replace(".", " ").split() if p]
        return "".join(p[0] for p in parts[:2]).upper() or "?"

    @property
    def group_ids(self) -> set[int]:
        return {g.id for g in self.groups}

    @property
    def is_global_manager(self) -> bool:
        """Admins and managers explicitly set to "entire inventory" manage everything. A manager without a
        group and without this checkbox only manages general items."""
        return self.is_admin or (self.role == ROLE_MANAGER and bool(self.manages_all))

    def can_see(self, asset: "Asset", catalog_all: bool = False) -> bool:
        if catalog_all or self.is_global_manager or asset.group_id is None:
            return True
        return asset.group_id in self.group_ids

    def can_borrow(self, asset: "Asset") -> bool:
        """Staff members only get items of their groups (or general items)."""
        if asset.group_id is None or self.is_global_manager:
            return True
        return asset.group_id in self.group_ids

    def can_manage(self, asset: "Asset | None" = None) -> bool:
        if not self.is_manager:
            return False
        if self.is_global_manager or asset is None or asset.group_id is None:
            return True
        return asset.group_id in self.group_ids

    def can_view_secret(self, asset: "Asset") -> bool:
        """Only administrators and managers who are explicitly members of the item's group may view and change
        confidential data (e.g. BitLocker keys). Deliberately stricter than can_manage: "General" or "manager
        without a group" do not unlock secrets – otherwise, e.g., a manager who loses their last group would
        suddenly get access to all keys."""
        if self.is_admin:
            return True
        return self.role == ROLE_MANAGER and asset.group_id is not None and asset.group_id in self.group_ids

    def can_view_own_secret(self, asset: "Asset") -> bool:
        """The person the item is currently issued to may view its confidential data themselves – only if this
        is enabled for the item and only while the loan is running (it ends with the return). The route also
        requires the person's own password and logs every access."""
        if not (self.active and asset.secret_holder_view and asset.has_secret and (asset.quantity or 0) == 1):
            return False
        # The loan must have been issued by someone who may see the data themselves. Otherwise, e.g., a manager
        # without this right (or anyone via "Self-checkout") could issue the device to themselves and read the key.
        return any(l.user_id == self.id and l.issued_by is not None and l.issued_by.active
                   and l.issued_by.can_view_secret(asset) for l in asset.active_loans)

    def manageable_groups(self) -> list["Group"]:
        if self.is_global_manager:
            return Group.query.order_by(Group.name).all()
        return sorted(self.groups, key=lambda g: g.name.lower())

    @property
    def active_loans(self):
        return [l for l in self.loans if l.returned_at is None]

    @property
    def has_2fa(self) -> bool:
        return bool(self.totp_enabled) or bool(self.webauthn_credentials)


class Asset(db.Model):
    """An item (laptop, USB stick, projector …) or a bulk item (e.g. 20 HDMI cables)."""
    __tablename__ = "assets"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(300), nullable=False)               # name
    category = db.Column(db.String(200), default="", index=True)   # free text, e.g. "Laptop"
    manufacturer = db.Column(db.String(200), default="")
    model = db.Column(db.String(200), default="")
    serial_number = db.Column(db.String(200), default="", index=True)
    inventory_no = db.Column(db.String(100), default="", index=True)  # inventory number / label
    # Comparison key for uniqueness (without spaces, case-folded – umlauts too); empty = NULL so that any
    # number of items without a number is allowed. Set automatically.
    inventory_key = db.Column(db.String(200), nullable=True)
    serial_key = db.Column(db.String(200), nullable=True)
    description = db.Column(db.Text, default="")
    location = db.Column(db.String(200), default="")  # location (room, cabinet …)
    image_file = db.Column(db.String(200), default="")
    quantity = db.Column(db.Integer, default=1, nullable=False)
    # Confidential data (e.g. BitLocker recovery key, PIN) – stored encrypted only, visible only to managers
    # of this item; every view is logged.
    secret = db.Column(db.Text, default="")
    secret_updated_at = db.Column(db.DateTime)
    # Sharing: whoever currently has the item may view the confidential data themselves (password + log)
    secret_holder_view = db.Column(db.Boolean, default=False, nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    # Number of earlier loans whose details were deleted after the retention period expired
    archived_loans = db.Column(db.Integer, default=0, nullable=False)

    group = db.relationship("Group", back_populates="assets")
    loans = db.relationship("Loan", back_populates="asset", cascade="all, delete-orphan",
                            order_by="Loan.loaned_at.desc()")
    # The log is kept when the item is deleted (asset_id becomes empty, the name is stored alongside)
    secret_log = db.relationship("SecretAccess", back_populates="asset", passive_deletes=True,
                                 order_by="SecretAccess.at.desc()")

    @property
    def active_loans(self):
        return [l for l in self.loans if l.returned_at is None]

    @property
    def available(self) -> int:
        return max(0, (self.quantity or 0) - len(self.active_loans))

    @property
    def lendable(self) -> bool:
        """False for items of a group with "No lending"."""
        return not (self.group is not None and self.group.no_lending)

    @property
    def image(self) -> str:
        from flask import url_for
        return url_for("assets.image", filename=self.image_file) if self.image_file else ""

    @property
    def maker_model(self) -> str:
        return " ".join(p for p in (self.manufacturer, self.model) if p)

    @property
    def has_secret(self) -> bool:
        return bool(self.secret)

    def set_secret(self, value: str) -> None:
        value = (value or "").strip()
        self.secret = (_ENC + _secret_box("asset-secret:").encrypt(value.encode()).decode()) if value else ""
        self.secret_updated_at = datetime.now() if value else None

    def get_secret(self) -> str:
        if not self.secret:
            return ""
        try:
            return _secret_box("asset-secret:").decrypt(self.secret[len(_ENC):].encode()).decode()
        except Exception:  # noqa: BLE001 – e.g. different key after a restore
            return ""


class SecretAccess(db.Model):
    """Log: who viewed, changed or deleted an item's confidential data.

    The names of the item and of the person are stored as text as well, so that the log stays meaningful even
    after the item or account is deleted (it is only deleted via the retention period)."""
    __tablename__ = "secret_access"
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, index=True)
    asset_label = db.Column(db.String(400), default="")
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    user_label = db.Column(db.String(300), default="")
    at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    # view | change | delete | remove | holder (viewed by the borrower) | share_on | share_off
    action = db.Column(db.String(10), default="view", nullable=False)

    @classmethod
    def record(cls, asset: "Asset", user: "User", action: str) -> "SecretAccess":
        label = asset.name + (f" ({asset.inventory_no})" if asset.inventory_no else "")
        entry = cls(asset_id=asset.id, asset_label=label[:400], user_id=user.id,
                    user_label=f"{user.name} ({user.username})"[:300], action=action)
        db.session.add(entry)
        return entry

    asset = db.relationship("Asset", back_populates="secret_log")
    user = db.relationship("User")


class Loan(db.Model):
    __tablename__ = "loans"
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    issued_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    returned_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    loaned_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    due_at = db.Column(db.Date, nullable=True)   # empty = open-ended (e.g. a permanently issued laptop)
    returned_at = db.Column(db.DateTime)
    extensions = db.Column(db.Integer, default=0, nullable=False)
    note = db.Column(db.String(500), default="")

    # At most one open loan per item and person (protection against concurrent requests).
    # Partial indexes exist only in SQLite and PostgreSQL; MySQL/MariaDB relies on the row lock.
    __table_args__ = (
        db.Index("uq_loans_active_asset_user", "asset_id", "user_id", unique=True,
                 sqlite_where=db.text("returned_at IS NULL"),
                 postgresql_where=db.text("returned_at IS NULL")).ddl_if(dialect=("sqlite", "postgresql")),
    )

    asset = db.relationship("Asset", back_populates="loans")
    user = db.relationship("User", back_populates="loans", foreign_keys=[user_id])
    issued_by = db.relationship("User", foreign_keys=[issued_by_id])
    returned_by = db.relationship("User", foreign_keys=[returned_by_id])

    @property
    def is_active(self) -> bool:
        return self.returned_at is None

    @property
    def is_overdue(self) -> bool:
        return self.returned_at is None and self.due_at is not None and self.due_at < date.today()

    @property
    def days_left(self) -> int | None:
        return None if self.due_at is None else (self.due_at - date.today()).days


class WebAuthnCredential(db.Model):
    """A user's security key (YubiKey & co.) or passkey."""
    __tablename__ = "webauthn_credentials"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # stored default name – compared in views/security.py and twofa.py, keep
    name = db.Column(db.String(100), default="Sicherheitsschlüssel")
    rp_id = db.Column(db.String(253), nullable=False)          # domain the key is valid for
    credential_id = db.Column(db.String(1400), nullable=False, index=True)  # websafe-base64
    credential_data = db.Column(db.Text, nullable=False)       # AttestedCredentialData (base64)
    sign_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    last_used_at = db.Column(db.DateTime)
    # 2.6.0: registered as a passkey (stored on the device, unlocked with Face ID/fingerprint/PIN) – usable for
    # signing in without user name and password. A plain security key only serves as the second factor.
    passkey = db.Column(db.Boolean, default=False, nullable=False)

    user = db.relationship("User", backref=db.backref("webauthn_credentials", cascade="all, delete-orphan"))


class MailLog(db.Model):
    """Log of all sent (or failed) emails – also used to avoid sending reminders twice."""
    __tablename__ = "mail_log"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)
    kind = db.Column(db.String(30), nullable=False, index=True)  # loan|return|reminder|overdue|digest|manual|test
    to_addr = db.Column(db.String(300), default="")
    subject = db.Column(db.String(300), default="")
    status = db.Column(db.String(10), default="sent")  # sent|error|skipped
    error = db.Column(db.String(1000), default="")
    loan_id = db.Column(db.Integer, db.ForeignKey("loans.id", ondelete="SET NULL"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))


class Location(db.Model):
    """Predefined location (shelf, room …) – to choose from when entering items, against typos."""
    __tablename__ = "locations"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    note = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=datetime.now)


class DirectoryPerson(db.Model):
    """Cache: people from Active Directory who WOULD be allowed to sign in (by the AD rules), for quick search
    at the issue desk. Fully synced in the background – anyone disabled, expired, deleted or no longer authorized
    in AD is removed at the next sync. Only the bare minimum is stored; when an account is taken over, the person
    is checked directly in AD again anyway."""
    __tablename__ = "directory_people"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(200), nullable=False)
    username_key = db.Column(db.String(200), nullable=False, unique=True)   # lower-case
    display_name = db.Column(db.String(300), default="")
    email = db.Column(db.String(300), default="")
    synced_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class LoginAttempt(db.Model):
    """Failed sign-in attempt (for the temporary lockout, see lockout.py)."""
    __tablename__ = "login_attempts"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(300), nullable=False, index=True)       # e.g. "anna|203.0.113.5" or "2fa|anna|…"
    username = db.Column(db.String(200), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)


class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, default="")


DEFAULT_SETTINGS = {
    "org_name": "AssetLotse",
    "default_language": "de",   # language for visitors without their own choice and for emails
    "loan_days": "0",              # 0 = open-ended (default for work equipment)
    "extension_days": "14",
    "max_extensions": "2",
    "max_loans": "0",
    "self_checkout": "0",          # staff members may check out items themselves
    "self_return": "0",
    "self_extend": "1",
    # own = staff members only see their own items (no inventory) | groups | all
    "catalog_visibility": "own",
    # Automatic sign-out (idle.py); session_idle_hours: start value ASSETLOTSE_SESSION_HOURS, otherwise 12
    "session_idle_hours_admin": "",
    "session_idle_hours_external": "",
    "session_warn": "1",
    "session_end_on_close": "0",
    # Sign-in with a passkey (twofa.passkey_*): on by default
    "passkey_login": "1",
    # Active Directory / LDAP
    "ldap_enabled": "0",
    "ldap_server": "",
    "ldap_security": "none",  # none | starttls | ldaps
    "ldap_verify_cert": "1",
    "ldap_ca_file": "",
    "ldap_tls_certs": "",       # certificates fetched from the DC and confirmed (PEM)
    "ldap_check_hostname": "1",
    "ldap_bind_dn": "",
    "ldap_bind_password": "",
    "ldap_upn_suffix": "",
    "ldap_base_dn": "",
    "ldap_user_attr": "sAMAccountName",
    "ldap_user_filter": "(objectClass=user)",
    "ldap_login_rules": "",
    "ldap_manager_rules": "",
    "ldap_admin_rules": "",
    "ldap_nested_groups": "1",
    "ldap_group_sync": "1",
    # Cache of AD people for the search at the issue desk (synced in the background)
    "ldap_cache_enabled": "1",
    "ldap_cache_hours": "1",
    "ldap_timeout": "8",
    # Email (SMTP)
    "mail_enabled": "0",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_security": "starttls",  # none | starttls | ssl
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_from_name": "",
    "smtp_reply_to": "",
    "smtp_verify_cert": "1",
    "app_base_url": "",
    "mail_on_loan": "1",
    "mail_on_return": "0",
    "mail_reminder": "1",
    "mail_reminder_days": "2",
    "mail_overdue": "1",
    "mail_overdue_repeat_days": "7",
    "mail_overdue_max": "3",
    "mail_digest": "0",
    "mail_hour": "7",
    "backup_enabled": "1",     # daily backup (SQLite only)
    "backup_keep": "10",       # number of backups kept
    "backup_hour": "2",        # from this hour on
    "retention_months": "12",  # delete completed loans and the mail log after X months; 0 = never
    "mfa_internal": "same",    # two-factor in the internal network: same | optional | skip
    "internal_networks": "",   # empty = private networks (10/8, 172.16/12, 192.168/16, fc00::/7)
    "own_public_hosts": "",    # own internet connection: static IP or DynDNS name, also counts as internal
    "mail_footer": "",
    "mail_last_run": "",
    "mail_last_result": "",
    # Two-factor sign-in: optional | admins | managers | all
    "mfa_policy": "optional",
    # Self-registration: off | approval | open
    "registration_mode": "off",
    "registration_verify_email": "1",
    "registration_domains": "",
    "registration_group_id": "",
    "imprint_url": "",
    "privacy_url": "",
    "registration_notify_admins": "1",
    # Appearance
    "brand_logo": "",
    "brand_favicon": "",
    "brand_bg": "",
    "brand_bg_dim": "45",
    "brand_logo_text": "1",
    "brand_logo_plate": "0",
    "brand_accent": "",
    "brand_brass": "",
    "brand_side": "",
    "login_show_shelf": "1",
    "login_eyebrow": "Welcome",
    "login_text": "Who has what? Issue items, check them back in and keep track.",
}


# Credentials that are stored only encrypted in the database (and thus in backups)
SECRET_SETTINGS = {"ldap_bind_password", "smtp_password"}
_ENC = "enc:"


def _secret_box(purpose: str = "settings-secret:"):
    import base64
    import hashlib
    from cryptography.fernet import Fernet
    from flask import current_app
    key = hashlib.sha256((purpose + current_app.config["SECRET_KEY"]).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _decrypt_setting(key: str, value: str) -> str:
    if not value.startswith(_ENC):
        return value          # legacy value – encrypted at start-up
    try:
        return _secret_box().decrypt(value[len(_ENC):].encode()).decode()
    except Exception:  # noqa: BLE001 – e.g. a different session key
        import logging
        logging.getLogger(__name__).warning("Stored secret %s cannot be read – please enter it again", key)
        return ""


def encrypt_stored_secrets() -> int:
    """Encrypt credentials still stored in plain text (once after the update)."""
    n = 0
    for key in SECRET_SETTINGS:
        row = db.session.get(Setting, key)
        if row is not None and row.value and not row.value.startswith(_ENC):
            set_setting(key, row.value)
            n += 1
    if n:
        db.session.commit()
    return n


def get_setting(key: str, default: str | None = None) -> str:
    row = db.session.get(Setting, key)
    if row is not None:
        if key in SECRET_SETTINGS and row.value:
            return _decrypt_setting(key, row.value)
        return row.value or ""
    if default is not None:
        return default
    return DEFAULT_SETTINGS.get(key, "")


def get_bool(key: str) -> bool:
    return get_setting(key) in ("1", "true", "on", "yes")


def get_int(key: str, fallback: int = 0) -> int:
    try:
        return int(get_setting(key))
    except (TypeError, ValueError):
        return fallback


def set_setting(key: str, value) -> None:
    if isinstance(value, bool):
        value = "1" if value else "0"
    if key in SECRET_SETTINGS and value:
        value = _ENC + _secret_box().encrypt(str(value).encode()).decode()
    row = db.session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)


def default_due_date() -> date | None:
    """Default due date; None if the loan period is set to 0 (= open-ended)."""
    days = get_int("loan_days", 0)
    return date.today() + timedelta(days=days) if days > 0 else None


def catalog_hidden(user) -> bool:
    """Does this person not see the inventory at all (only "My items")? Applies to staff members only;
    unknown values count as "own only" to be safe."""
    if user is None or not getattr(user, "is_authenticated", False) or user.is_manager:
        return False
    return get_setting("catalog_visibility") not in ("groups", "all")


def visible_assets_query(user: User):
    """Query of the items a user may see in the inventory."""
    q = Asset.query
    if catalog_hidden(user):   # only what is currently issued to them
        mine = db.session.query(Loan.asset_id).filter(Loan.user_id == user.id, Loan.returned_at.is_(None))
        return q.filter(Asset.id.in_(mine))
    if user.is_global_manager or get_setting("catalog_visibility") == "all":
        return q
    ids = list(user.group_ids)
    return q.filter((Asset.group_id.is_(None)) | (Asset.group_id.in_(ids or [-1])))


def manageable_assets_query(user: User):
    q = Asset.query
    if user.is_global_manager:
        return q
    ids = list(user.group_ids)
    return q.filter((Asset.group_id.is_(None)) | (Asset.group_id.in_(ids or [-1])))


def lendable_clause():
    """SQL condition: the item may be lent (no group, or a group without "No lending")."""
    return Asset.group_id.is_(None) | ~Asset.group.has(Group.no_lending.is_(True))


def count_active_loans(user_id: int) -> int:
    return db.session.query(func.count(Loan.id)).filter(
        Loan.user_id == user_id, Loan.returned_at.is_(None)
    ).scalar() or 0


def _add_column_ddl(table, col, dialect) -> list[str]:
    """SQL to add a column later (identifiers quoted correctly, e.g. `groups`/"key")."""
    from sqlalchemy import literal

    prep = dialect.identifier_preparer
    tname, cname = prep.format_table(table), prep.format_column(col)
    coltype = col.type.compile(dialect)
    ddl = f"ALTER TABLE {tname} ADD COLUMN {cname} {coltype}"
    default = getattr(col.default, "arg", None)
    if default is None or callable(default):
        return [ddl]
    lit = literal(default, col.type).compile(dialect=dialect, compile_kwargs={"literal_binds": True})
    if dialect.name in ("mysql", "mariadb") and any(t in coltype.upper() for t in ("TEXT", "BLOB", "JSON")):
        # MySQL allows no DEFAULT value for TEXT/BLOB: create the column without one, then fill existing rows
        return [ddl, f"UPDATE {tname} SET {cname} = {lit}"]
    ddl += f" DEFAULT {lit}"
    if not col.nullable:
        ddl += " NOT NULL"
    return [ddl]


ACTIVE_LOAN_INDEX = ("CREATE UNIQUE INDEX IF NOT EXISTS uq_loans_active_asset_user "
                     "ON loans (asset_id, user_id) WHERE returned_at IS NULL")


def number_key(value) -> str | None:
    """Comparison key of an inventory/serial number: " it-ü1 " and "IT-Ü1" give the same key."""
    value = (value or "").strip()
    return value.casefold()[:200] if value else None


@event.listens_for(Asset, "before_insert")
@event.listens_for(Asset, "before_update")
def _set_number_keys(_mapper, _conn, target):
    target.inventory_key = number_key(target.inventory_no)
    target.serial_key = number_key(target.serial_number)


def fill_number_keys() -> int:
    """Fill in the keys for items from older versions (once at start-up)."""
    rows = (db.session.query(Asset.id, Asset.inventory_no, Asset.serial_number)
            .filter(((Asset.inventory_key.is_(None)) & (Asset.inventory_no != ""))
                    | ((Asset.serial_key.is_(None)) & (Asset.serial_number != ""))).all())
    for ident, inv, ser in rows:
        db.session.execute(db.update(Asset).where(Asset.id == ident).values(
            inventory_key=number_key(inv), serial_key=number_key(ser)).execution_options(synchronize_session=False))
    if rows:
        db.session.commit()
    return len(rows)


NUMBER_INDEXES = (db.Index("uq_assets_inventory_key", Asset.inventory_key, unique=True),
                  db.Index("uq_assets_serial_key", Asset.serial_key, unique=True))


def ensure_indexes() -> bool:
    """Create indexes that db.create_all() does not create for existing tables.

    Returns False if the index could not be created because of existing duplicate open loans – then
    there is just a warning in the log and start-up continues."""
    import logging

    from sqlalchemy import inspect, text

    if not inspect(db.engine).has_table("assets"):
        return True
    # Inventory and serial numbers unique (NULL = no number, allowed any number of times; works on all databases).
    # The app checks this itself; the index catches concurrent saves. If existing data has duplicates, the
    # check in the app remains the only one.
    fill_number_keys()
    for index in NUMBER_INDEXES:
        try:
            index.create(db.engine, checkfirst=True)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "Unique index %s not created (duplicate numbers in the inventory? Search the inventory and "
                "correct them): %s", index.name, exc)
    if db.engine.dialect.name not in ("sqlite", "postgresql") or not inspect(db.engine).has_table("loans"):
        return True
    ok = True
    try:
        with db.engine.begin() as conn:
            conn.execute(text(ACTIVE_LOAN_INDEX))
    except Exception as exc:  # noqa: BLE001 – e.g. IntegrityError with legacy data
        logging.getLogger(__name__).warning(
            "Unique index for open loans not created (duplicate open loans present?): %s", exc)
        ok = False
    return ok


def migrate_manages_all() -> int:
    """Once, when the column users.manages_all is added: whoever managed the entire inventory as a manager
    without a group keeps doing so – now set explicitly. From then on only the checkbox counts."""
    from sqlalchemy import exists, select, true, update
    no_group = ~exists(select(user_groups.c.user_id).where(user_groups.c.user_id == User.id))
    res = db.session.execute(update(User).where(User.role == ROLE_MANAGER, no_group).values(manages_all=true())
                             .execution_options(synchronize_session=False))
    db.session.commit()
    return res.rowcount or 0


def ensure_columns() -> list[str]:
    """Simple schema update: add missing columns to existing tables.

    db.create_all() only creates new tables. If a new version adds columns, they are added here
    via ALTER TABLE (SQLite, MySQL/MariaDB, PostgreSQL). Each column in its own transaction; if
    another process has just created it ("duplicate column"), it is skipped."""
    import logging
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import DBAPIError

    added = []
    insp = inspect(db.engine)
    dialect = db.engine.dialect
    for table in db.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            try:
                with db.engine.begin() as conn:
                    for stmt in _add_column_ddl(table, col, dialect):
                        conn.execute(text(stmt))
            except DBAPIError as exc:
                msg = str(exc.orig if exc.orig is not None else exc).lower()
                if "duplicate column" in msg or ("column" in msg and "already exists" in msg):
                    logging.getLogger(__name__).debug("Column %s.%s already exists", table.name, col.name)
                    continue
                raise
            added.append(f"{table.name}.{col.name}")
    return added
