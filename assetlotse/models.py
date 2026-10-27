"""Datenmodell von AssetLotse."""
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
    """Fremdschlüssel und WAL für SQLite aktivieren."""
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA secure_delete=ON")   # Gelöschtes (z. B. alte Schlüssel) mit Nullen überschreiben
        cur.close()


MIN_PASSWORD_LENGTH = 10   # für alle neuen Passwörter (Einrichtung, Profil, Verwaltung, Registrierung)

ROLE_READER = "reader"
ROLE_MANAGER = "manager"
ROLE_ADMIN = "admin"
from .i18n import _l  # noqa: E402

ROLES = {
    ROLE_READER: _l("Mitarbeiter"),
    ROLE_MANAGER: _l("Verwalter"),
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
    # Optional: DN einer AD-Gruppe – Mitglieder werden beim Login automatisch zugeordnet
    ldap_dn = db.Column(db.String(500), default="")
    color = db.Column(db.String(16), default="#0b7285")
    # Kein Verleih: Gegenstände dieser Gruppe werden nie ausgegeben (z. B. fest verbaute oder nur intern
    # genutzte Geräte) – weder an der Theke noch aus dem Bestand noch per Selbstausleihe.
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
    ldap_guid = db.Column(db.String(64), default="", index=True)   # feste AD-Kennung (Umbenennungen erkennen)
    # Vom AD-Abgleich deaktiviert (im AD gesperrt/gelöscht/nicht mehr berechtigt) – wird automatisch wieder aktiv,
    # sobald die Person im AD wieder berechtigt ist. Von Hand gesperrte Konten bleiben gesperrt.
    deactivated_by_sync = db.Column(db.Boolean, default=False, nullable=False)
    role = db.Column(db.String(20), default=ROLE_READER, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    last_login = db.Column(db.DateTime)
    email_notifications = db.Column(db.Boolean, default=True, nullable=False)
    # Selbstregistrierung
    self_registered = db.Column(db.Boolean, default=False, nullable=False)
    email_verified = db.Column(db.Boolean, default=True, nullable=False)
    pending_approval = db.Column(db.Boolean, default=False, nullable=False)
    # Zwei-Faktor-Anmeldung
    totp_secret = db.Column(db.Text, default="")          # verschlüsselt (Fernet, Schlüssel aus SECRET_KEY)
    totp_enabled = db.Column(db.Boolean, default=False, nullable=False)
    totp_last_step = db.Column(db.Integer, default=0, nullable=False)  # gegen Wiederverwendung eines Codes
    recovery_codes = db.Column(db.Text, default="")       # JSON-Liste gehashter Einmal-Codes
    # Wird erhöht, um alle bestehenden Sitzungen und „Angemeldet bleiben“-Cookies ungültig zu machen
    auth_epoch = db.Column(db.Integer, default=0, nullable=False)
    # Sprache der Oberfläche und der E-Mails ("" = Browser bzw. Standardsprache)
    language = db.Column(db.String(5), default="", nullable=False)
    # Neuigkeiten: bis zu dieser Programmversion gelesen ("" = noch nie geöffnet)
    news_seen = db.Column(db.String(20), default="", nullable=False)

    groups = db.relationship("Group", secondary=user_groups, back_populates="users")
    loans = db.relationship("Loan", back_populates="user", foreign_keys="Loan.user_id")

    # --- Flask-Login ---
    @property
    def is_active(self):  # noqa: D401
        return bool(self.active)

    def get_id(self) -> str:
        # Die Kennung enthält die „Sitzungs-Generation“: erhöht man sie, sind alle alten Sitzungen
        # und Erinnerungs-Cookies dieses Kontos sofort ungültig.
        return f"{self.id}:{self.auth_epoch or 0}"

    def end_all_sessions(self):
        self.auth_epoch = (self.auth_epoch or 0) + 1

    # --- Passwörter ---
    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)
        if self.id is not None:   # bestehendes Konto: andere Geräte abmelden
            self.end_all_sessions()

    def check_password(self, password: str) -> bool:
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    # --- Rollen ---
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
        """Admins und Verwalter ohne Gruppenzuordnung verwalten den gesamten Bestand."""
        return self.is_admin or (self.role == ROLE_MANAGER and not self.groups)

    def can_see(self, asset: "Asset", catalog_all: bool = False) -> bool:
        if catalog_all or self.is_global_manager or asset.group_id is None:
            return True
        return asset.group_id in self.group_ids

    def can_borrow(self, asset: "Asset") -> bool:
        """Mitarbeiter bekommen nur Gegenstände ihrer Gruppen (oder allgemeine Gegenstände)."""
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
        """Vertrauliche Angaben (z. B. BitLocker-Schlüssel) sehen und ändern dürfen nur Administratoren und
        Verwalter, die ausdrücklich Mitglied der Gruppe des Gegenstands sind. Bewusst strenger als can_manage:
        „Allgemein“ oder „Verwalter ohne Gruppe“ öffnen keine Geheimnisse – sonst bekäme z. B. ein Verwalter,
        der seine letzte Gruppe verliert, plötzlich Zugriff auf alle Schlüssel."""
        if self.is_admin:
            return True
        return self.role == ROLE_MANAGER and asset.group_id is not None and asset.group_id in self.group_ids

    def can_view_own_secret(self, asset: "Asset") -> bool:
        """Die Person, der der Gegenstand gerade ausgegeben ist, darf seine vertraulichen Angaben selbst ansehen –
        nur wenn das beim Gegenstand freigegeben ist und nur, solange die Ausgabe läuft (mit der Rückgabe ist es
        vorbei). Die Route verlangt zusätzlich das eigene Passwort und protokolliert jeden Abruf."""
        if not (self.active and asset.secret_holder_view and asset.has_secret and (asset.quantity or 0) == 1):
            return False
        # Die Ausgabe muss von jemandem stammen, der die Angaben selbst sehen darf. Sonst könnte sich z. B. ein
        # Verwalter ohne dieses Recht (oder jeder per „Selbst ausleihen“) das Gerät ausgeben und den Schlüssel lesen.
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
    """Ein Gegenstand (Laptop, USB-Stick, Beamer …) bzw. eine Sammelposition (z. B. 20 HDMI-Kabel)."""
    __tablename__ = "assets"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(300), nullable=False)               # Bezeichnung
    category = db.Column(db.String(200), default="", index=True)   # frei wählbar, z. B. „Laptop“
    manufacturer = db.Column(db.String(200), default="")
    model = db.Column(db.String(200), default="")
    serial_number = db.Column(db.String(200), default="", index=True)
    inventory_no = db.Column(db.String(100), default="", index=True)  # Inventarnummer / Etikett
    # Vergleichsschlüssel für die Eindeutigkeit (ohne Leerzeichen, Groß-/Kleinschreibung vereinheitlicht – auch
    # Umlaute); leer = NULL, damit beliebig viele Gegenstände ohne Nummer erlaubt sind. Wird automatisch gesetzt.
    inventory_key = db.Column(db.String(200), nullable=True)
    serial_key = db.Column(db.String(200), nullable=True)
    description = db.Column(db.Text, default="")
    location = db.Column(db.String(200), default="")  # Standort (Raum, Schrank …)
    image_file = db.Column(db.String(200), default="")
    quantity = db.Column(db.Integer, default=1, nullable=False)
    # Vertrauliche Angaben (z. B. BitLocker-Wiederherstellungsschlüssel, PIN) – nur verschlüsselt gespeichert,
    # sichtbar nur für Verwalter dieses Gegenstands; jedes Anzeigen wird protokolliert.
    secret = db.Column(db.Text, default="")
    secret_updated_at = db.Column(db.DateTime)
    # Freigabe: Wer den Gegenstand gerade hat, darf die vertraulichen Angaben selbst ansehen (Passwort + Protokoll)
    secret_holder_view = db.Column(db.Boolean, default=False, nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    # Anzahl früherer Ausleihen, deren Einzeldaten nach Ablauf der Aufbewahrungsfrist gelöscht wurden
    archived_loans = db.Column(db.Integer, default=0, nullable=False)

    group = db.relationship("Group", back_populates="assets")
    loans = db.relationship("Loan", back_populates="asset", cascade="all, delete-orphan",
                            order_by="Loan.loaned_at.desc()")
    # Protokoll bleibt beim Löschen des Gegenstands erhalten (asset_id wird leer, Bezeichnung ist mitgespeichert)
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
        """False für Gegenstände einer Gruppe mit „Kein Verleih“."""
        return not (self.group is not None and self.group.no_lending)

    @property
    def image(self) -> str:
        from flask import url_for
        return url_for("assets.image", filename=self.image_file) if self.image_file else ""

    @property
    def maker_model(self) -> str:
        return " ".join(p for p in (self.manufacturer, self.model) if p)

    @property
    def hue(self) -> int:
        return sum(ord(c) for c in (self.category or self.name or "x")) % 360

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
        except Exception:  # noqa: BLE001 – z. B. anderer Schlüssel nach Wiederherstellung
            return ""


class SecretAccess(db.Model):
    """Protokoll: wer hat die vertraulichen Angaben eines Gegenstands angesehen, geändert oder gelöscht.

    Name des Gegenstands und der Person werden als Text mitgespeichert, damit das Protokoll auch nach dem
    Löschen des Gegenstands oder Kontos aussagekräftig bleibt (gelöscht wird es nur über die Aufbewahrungsfrist)."""
    __tablename__ = "secret_access"
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, index=True)
    asset_label = db.Column(db.String(400), default="")
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    user_label = db.Column(db.String(300), default="")
    at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    # view | change | delete | remove | holder (von der ausleihenden Person angezeigt) | share_on | share_off
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
    due_at = db.Column(db.Date, nullable=True)   # leer = unbefristet (z. B. dauerhaft ausgegebener Laptop)
    returned_at = db.Column(db.DateTime)
    extensions = db.Column(db.Integer, default=0, nullable=False)
    note = db.Column(db.String(500), default="")

    # Höchstens eine offene Ausleihe je Gegenstand und Person (Schutz vor gleichzeitigen Anfragen).
    # Teil-Indizes gibt es nur in SQLite und PostgreSQL; MySQL/MariaDB verlässt sich auf die Zeilensperre.
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
    """Sicherheitsschlüssel (YubiKey & Co.) bzw. Passkey eines Benutzers."""
    __tablename__ = "webauthn_credentials"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(100), default="Sicherheitsschlüssel")
    rp_id = db.Column(db.String(253), nullable=False)          # Domain, für die der Schlüssel gilt
    credential_id = db.Column(db.String(1400), nullable=False, index=True)  # websafe-base64
    credential_data = db.Column(db.Text, nullable=False)       # AttestedCredentialData (base64)
    sign_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    last_used_at = db.Column(db.DateTime)

    user = db.relationship("User", backref=db.backref("webauthn_credentials", cascade="all, delete-orphan"))


class MailLog(db.Model):
    """Protokoll aller versendeten (oder fehlgeschlagenen) E-Mails – dient auch dazu,
    Erinnerungen nicht doppelt zu verschicken."""
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
    """Festgelegter Standort (Regal, Raum …) – zur Auswahl beim Erfassen, gegen Tippfehler."""
    __tablename__ = "locations"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    note = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=datetime.now)


class DirectoryPerson(db.Model):
    """Zwischenspeicher: Personen aus dem Active Directory, die sich anmelden DÜRFTEN (nach den AD-Regeln), für die
    schnelle Suche an der Ausgabetheke. Wird im Hintergrund komplett abgeglichen – wer im AD deaktiviert, abgelaufen,
    gelöscht oder nicht mehr berechtigt ist, fliegt beim nächsten Abgleich raus. Gespeichert wird nur das Nötigste;
    beim Übernehmen eines Kontos wird die Person ohnehin noch einmal direkt im AD geprüft."""
    __tablename__ = "directory_people"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(200), nullable=False)
    username_key = db.Column(db.String(200), nullable=False, unique=True)   # klein geschrieben
    display_name = db.Column(db.String(300), default="")
    email = db.Column(db.String(300), default="")
    synced_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


class LoginAttempt(db.Model):
    """Fehlgeschlagener Anmeldeversuch (für die zeitweise Sperre, siehe lockout.py)."""
    __tablename__ = "login_attempts"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(300), nullable=False, index=True)       # z. B. "anna|203.0.113.5" oder "2fa|anna|…"
    username = db.Column(db.String(200), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False, index=True)


class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, default="")


DEFAULT_SETTINGS = {
    "org_name": "AssetLotse",
    "default_language": "de",   # Sprache für Besucher ohne eigene Wahl und für E-Mails
    "loan_days": "0",              # 0 = unbefristet (Standard bei Arbeitsgeräten)
    "extension_days": "14",
    "max_extensions": "2",
    "max_loans": "0",
    "self_checkout": "0",          # Mitarbeiter dürfen sich selbst etwas ausleihen
    "self_return": "0",
    "self_extend": "1",
    # own = Mitarbeiter sehen nur ihre eigenen Gegenstände (kein Bestand) | groups | all
    "catalog_visibility": "own",
    # Active Directory / LDAP
    "ldap_enabled": "0",
    "ldap_server": "",
    "ldap_security": "none",  # none | starttls | ldaps
    "ldap_verify_cert": "1",
    "ldap_ca_file": "",
    "ldap_tls_certs": "",       # vom DC abgerufene und bestätigte Zertifikate (PEM)
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
    # Zwischenspeicher der AD-Personen für die Suche an der Ausgabetheke (Abgleich im Hintergrund)
    "ldap_cache_enabled": "1",
    "ldap_cache_hours": "1",
    "ldap_timeout": "8",
    # E-Mail (SMTP)
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
    "backup_enabled": "1",     # tägliche Datensicherung (nur SQLite)
    "backup_keep": "10",       # Anzahl aufbewahrter Sicherungen
    "backup_hour": "2",        # ab dieser Uhrzeit
    "retention_months": "12",  # abgeschlossene Ausleihen und Mail-Protokoll nach X Monaten löschen; 0 = nie
    "mfa_internal": "same",    # Zwei-Faktor im internen Netz: same | optional | skip
    "internal_networks": "",   # leer = private Netze (10/8, 172.16/12, 192.168/16, fc00::/7)
    "own_public_hosts": "",    # eigener Anschluss: feste IP oder DynDNS-Name, gilt ebenfalls als intern
    "mail_footer": "",
    "mail_last_run": "",
    "mail_last_result": "",
    # Zwei-Faktor-Anmeldung: optional | admins | managers | all
    "mfa_policy": "optional",
    # Selbstregistrierung: off | approval | open
    "registration_mode": "off",
    "registration_verify_email": "1",
    "registration_domains": "",
    "registration_group_id": "",
    "imprint_url": "",
    "privacy_url": "",
    "registration_notify_admins": "1",
    # Erscheinungsbild
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
    "login_eyebrow": "Willkommen",
    "login_text": "Wer hat was? Gegenstände ausgeben, zurücknehmen und im Blick behalten.",
}


# Zugangsdaten, die nur verschlüsselt in der Datenbank (und damit in Sicherungen) stehen
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
        return value          # Altbestand – wird beim Start verschlüsselt
    try:
        return _secret_box().decrypt(value[len(_ENC):].encode()).decode()
    except Exception:  # noqa: BLE001 – z. B. anderer Sitzungsschlüssel
        import logging
        logging.getLogger(__name__).warning("Gespeichertes Geheimnis %s nicht lesbar – bitte neu eingeben", key)
        return ""


def encrypt_stored_secrets() -> int:
    """Noch im Klartext gespeicherte Zugangsdaten verschlüsseln (einmalig nach dem Update)."""
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
    """Standard-Rückgabedatum; None, wenn die Leihdauer auf 0 (= unbefristet) steht."""
    days = get_int("loan_days", 0)
    return date.today() + timedelta(days=days) if days > 0 else None


def catalog_hidden(user) -> bool:
    """Sieht diese Person den Bestand gar nicht (nur „Meine Gegenstände“)? Gilt nur für Mitarbeiter;
    unbekannte Werte zählen sicherheitshalber als „nur eigene“."""
    if user is None or not getattr(user, "is_authenticated", False) or user.is_manager:
        return False
    return get_setting("catalog_visibility") not in ("groups", "all")


def visible_assets_query(user: User):
    """Query der Gegenstände, die ein Benutzer im Bestand sehen darf."""
    q = Asset.query
    if catalog_hidden(user):   # nur, was ihr gerade ausgegeben ist
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
    """SQL-Bedingung: Gegenstand darf verliehen werden (keine Gruppe oder Gruppe ohne „Kein Verleih“)."""
    return Asset.group_id.is_(None) | ~Asset.group.has(Group.no_lending.is_(True))


def count_active_loans(user_id: int) -> int:
    return db.session.query(func.count(Loan.id)).filter(
        Loan.user_id == user_id, Loan.returned_at.is_(None)
    ).scalar() or 0


def _add_column_ddl(table, col, dialect) -> list[str]:
    """SQL zum Nachrüsten einer Spalte (Bezeichner korrekt gequotet, z. B. `groups`/"key")."""
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
        # MySQL erlaubt für TEXT/BLOB keinen DEFAULT-Wert: Spalte ohne anlegen, Bestand danach füllen
        return [ddl, f"UPDATE {tname} SET {cname} = {lit}"]
    ddl += f" DEFAULT {lit}"
    if not col.nullable:
        ddl += " NOT NULL"
    return [ddl]


ACTIVE_LOAN_INDEX = ("CREATE UNIQUE INDEX IF NOT EXISTS uq_loans_active_asset_user "
                     "ON loans (asset_id, user_id) WHERE returned_at IS NULL")


def number_key(value) -> str | None:
    """Vergleichsschlüssel einer Inventar-/Seriennummer: „ it-ü1 “ und „IT-Ü1“ ergeben denselben Schlüssel."""
    value = (value or "").strip()
    return value.casefold()[:200] if value else None


@event.listens_for(Asset, "before_insert")
@event.listens_for(Asset, "before_update")
def _set_number_keys(_mapper, _conn, target):
    target.inventory_key = number_key(target.inventory_no)
    target.serial_key = number_key(target.serial_number)


def fill_number_keys() -> int:
    """Schlüssel für Gegenstände aus älteren Versionen nachtragen (einmalig beim Start)."""
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
    """Indizes nachziehen, die db.create_all() bei bestehenden Tabellen nicht anlegt.

    Liefert False, wenn der Index wegen vorhandener doppelter offener Ausleihen nicht angelegt werden
    konnte – dann bleibt es bei einer Warnung im Protokoll, der Start läuft weiter."""
    import logging

    from sqlalchemy import inspect, text

    if not inspect(db.engine).has_table("assets"):
        return True
    # Inventar- und Seriennummern eindeutig (NULL = keine Nummer, beliebig oft erlaubt; geht auf allen Datenbanken).
    # Die App prüft das selbst; der Index fängt gleichzeitiges Speichern ab. Gibt es im Altbestand Doppelte,
    # bleibt es bei der Prüfung in der App.
    fill_number_keys()
    for index in NUMBER_INDEXES:
        try:
            index.create(db.engine, checkfirst=True)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "Eindeutiger Index %s nicht angelegt (doppelte Nummern im Bestand? Unter Bestand suchen und "
                "korrigieren): %s", index.name, exc)
    if db.engine.dialect.name not in ("sqlite", "postgresql") or not inspect(db.engine).has_table("loans"):
        return True
    ok = True
    try:
        with db.engine.begin() as conn:
            conn.execute(text(ACTIVE_LOAN_INDEX))
    except Exception as exc:  # noqa: BLE001 – z. B. IntegrityError bei Altdaten
        logging.getLogger(__name__).warning(
            "Eindeutiger Index für offene Ausleihen nicht angelegt (doppelte offene Ausleihen vorhanden?): %s", exc)
        ok = False
    return ok


def ensure_columns() -> list[str]:
    """Einfache Schema-Aktualisierung: fehlende Spalten bestehender Tabellen ergänzen.

    db.create_all() legt nur neue Tabellen an. Kommen in einer neuen Version Spalten hinzu,
    werden sie hier per ALTER TABLE nachgezogen (SQLite, MySQL/MariaDB, PostgreSQL).
    Jede Spalte in eigener Transaktion; hat ein anderer Prozess sie gerade angelegt
    („duplicate column“), wird das übersprungen."""
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
                    logging.getLogger(__name__).debug("Spalte %s.%s existiert bereits", table.name, col.name)
                    continue
                raise
            added.append(f"{table.name}.{col.name}")
    return added
