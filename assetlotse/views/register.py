"""Selbstregistrierung neuer Mitarbeiter.

Modi (Administration → Einstellungen):
  off       – keine Registrierung (Standard)
  approval  – Konto wird angelegt, ein Admin muss es freigeben
  open      – Konto ist sofort aktiv
Zusätzlich optional: E-Mail-Bestätigung (wenn E-Mail-Versand aktiv), erlaubte E-Mail-Domains,
Standardgruppe, Link zu Datenschutzhinweisen, Benachrichtigung der Admins.

Schutz bei Betrieb im Internet: Honeypot-Feld, Mindest-Ausfüllzeit, Begrenzung pro IP,
reservierte Namen, keine Namen, die es im Active Directory gibt.
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict

from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request, session,
                   url_for)
from flask_login import current_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func

from ..i18n import _
from ..models import MIN_PASSWORD_LENGTH, ROLE_ADMIN, ROLE_READER, Group, User, db, get_bool, get_setting
from . import form_str

bp = Blueprint("register", __name__)
log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,39}$")
RESERVED = {"admin", "administrator", "root", "system", "assetlotse", "support", "service", "postmaster",
            "webmaster", "hostmaster", "info", "noreply", "no-reply", "guest", "gast", "test", "krbtgt"}
VERIFY_MAX_AGE = 48 * 3600
MIN_FILL_SECONDS = 3
_REG_LOG: dict[str, list[float]] = defaultdict(list)
REG_PER_HOUR = 5
ATTEMPTS_PER_HOUR = 30      # jedes Absenden zählt – bremst das Abfragen vorhandener Namen/Adressen


def mode() -> str:
    m = get_setting("registration_mode")
    return m if m in ("off", "approval", "open") else "off"


def enabled() -> bool:
    return mode() != "off"


def needs_verification() -> bool:
    from ..mailer import mail_active
    return get_bool("registration_verify_email") and mail_active()


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="email-verify")


def _base_url() -> str | None:
    """Adresse für Links in E-Mails. Nur die eingestellte Adresse – die Host-Angabe einer anonymen Anfrage
    ist frei wählbar (Links auf fremde Seiten). Ausnahme: ein angemeldeter Admin löst die Mail selbst aus."""
    url = (get_setting("app_base_url") or "").rstrip("/")
    if url:
        return url
    if current_user.is_authenticated and current_user.is_admin:
        return request.host_url.rstrip("/")
    return None


def _allowed_domain(email: str) -> bool:
    domains = [d.strip().lower().lstrip("@") for d in re.split(r"[,\s;]+", get_setting("registration_domains") or "")
               if d.strip()]
    if not domains:
        return True
    host = email.rsplit("@", 1)[-1].lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _exists_in_directory(username: str) -> bool | None:
    """True/False, oder None wenn das AD nicht geprüft werden konnte."""
    from ..ldap_auth import Directory, LDAPAmbiguous, LDAPUserNotFound, ldap_enabled
    if not ldap_enabled():
        return False
    try:
        d = Directory()
        if not d.bind_dn:
            return None
        conn = d.service_connection()
        try:
            d.find_user(conn, username)
            return True
        except LDAPUserNotFound:
            return False
        except LDAPAmbiguous:
            return True
        finally:
            conn.unbind()
    except Exception as exc:  # noqa: BLE001
        log.warning("Registrierung: AD-Prüfung für %s nicht möglich: %s", username, exc)
        return None


def _rate_limited(ip: str, limit: int = REG_PER_HOUR) -> bool:
    now = time.time()
    _REG_LOG[ip] = [t for t in _REG_LOG[ip] if now - t < 3600]
    return len(_REG_LOG[ip]) >= limit


def maybe_activate(user: User):
    if user.email_verified and not user.pending_approval:
        user.active = True


# ---------------------------------------------------------------- Mails

def _send(template: str, to: str, **ctx):
    from ..mailer import mail_active, render_mail, send_mail
    if not mail_active() or not to:
        return False
    subject, text, html = render_mail(template, **ctx)
    ok, _detail = send_mail(to, subject, text, html, kind="account", user_id=ctx.get("user").id if ctx.get("user") else None)
    return ok


def send_verification(user: User) -> bool:
    base = _base_url()
    if not base:
        log.warning("Bestätigungs-E-Mail nicht gesendet: keine Adresse für Links in E-Mails eingestellt "
                    "(Verwaltung → E-Mail)")
        return False
    token = _serializer().dumps({"uid": user.id, "email": user.email})
    link = f"{base}{url_for('register.verify', token=token)}"
    return _send("verify", user.email, user=user, link=link)


def notify_admins(user: User):
    if not get_bool("registration_notify_admins"):
        return
    admins = User.query.filter_by(role=ROLE_ADMIN, active=True).all()
    base = _base_url()
    link = f"{base}{url_for('admin.users', status='neu')}" if base else ""
    for a in admins:
        if a.email and a.email_notifications:
            _send("registration_admin", a.email, user=a, new_user=user, link=link,
                  approval=user.pending_approval)


def send_approved(user: User):
    if user.email and user.email_notifications:
        base = _base_url()
        _send("approved", user.email, user=user, link=f"{base}{url_for('main.login')}" if base else "")


# ---------------------------------------------------------------- Seiten

@bp.route("/registrieren", methods=["GET", "POST"])
def register():
    if not enabled():
        abort(404)
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    privacy = get_setting("privacy_url")
    values = {}
    if request.method == "POST":
        ip = request.remote_addr or "?"
        values = {k: form_str(k, 200) for k in ("username", "display_name", "email")}
        # Bots: verstecktes Feld ausgefüllt oder Formular in Sekundenbruchteilen abgeschickt
        started = session.pop("reg_started", 0)
        if form_str("homepage_url", 200) or (started and time.time() - started < MIN_FILL_SECONDS):
            log.info("Registrierung von %s als Bot verworfen", ip)
            return render_template("register_done.html", state="check_mail" if needs_verification() else "approval")
        if not started:  # Sitzung abgelaufen o. Ä. – Formular einfach erneut anzeigen
            session["reg_started"] = time.time() - MIN_FILL_SECONDS
            flash(_("Bitte sende das Formular noch einmal ab."), "info")
            return render_template("register.html", v=values, privacy=privacy), 400
        if _rate_limited(ip) or _rate_limited(f"try|{ip}", ATTEMPTS_PER_HOUR):
            flash(_("Zu viele Registrierungen von deinem Anschluss. Bitte versuche es später erneut."), "error")
            return render_template("register.html", v=values, privacy=privacy), 429

        _REG_LOG[f"try|{ip}"].append(time.time())
        username, name, email = values["username"], values["display_name"], values["email"].lower()
        password = request.form.get("password") or ""
        error = None
        if needs_verification() and not _base_url():
            log.warning("Registrierung abgelehnt: E-Mail-Bestätigung ist an, aber keine Adresse für Links in "
                        "E-Mails eingestellt (Verwaltung → E-Mail)")
            flash(_("Die Registrierung ist gerade nicht möglich. Bitte versuche es später erneut."), "error")
            return render_template("register.html", v=values, privacy=privacy), 503
        if not USERNAME_RE.match(username):
            error = _("Benutzername: 3–40 Zeichen, nur Buchstaben, Ziffern, Punkt, Binde- und Unterstrich.")
        elif username.lower() in RESERVED:
            error = _("Dieser Benutzername ist nicht verfügbar.")
        elif not name:
            error = _("Bitte gib deinen Namen an.")
        elif not __import__("assetlotse.mailer", fromlist=["valid_email"]).valid_email(email):
            error = _("Bitte gib eine gültige E-Mail-Adresse an.")
        elif not _allowed_domain(email):
            error = _("Mit dieser E-Mail-Adresse ist keine Registrierung möglich.")
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = _("Das Passwort muss mindestens {n} Zeichen haben.", n=MIN_PASSWORD_LENGTH)
        elif password.lower() in (username.lower(), email):
            error = _("Das Passwort darf nicht dem Benutzernamen oder der E-Mail-Adresse entsprechen.")
        elif password != request.form.get("password2"):
            error = _("Die Passwörter stimmen nicht überein.")
        elif privacy and not request.form.get("privacy"):
            error = _("Bitte bestätige die Datenschutzhinweise.")
        elif User.query.filter(func.lower(User.username) == username.lower()).first():
            error = _("Dieser Benutzername ist bereits vergeben.")
        elif User.query.filter(func.lower(User.email) == email).first():
            # Bewusst offen gemeldet: Verbergen ließe sich das ohnehin nicht (der Benutzername wäre sonst frei
            # bzw. belegt). Gebremst wird das Abfragen über die Grenze je Anschluss.
            error = _("Mit dieser E-Mail-Adresse gibt es bereits ein Konto.")
        else:
            in_ad = _exists_in_directory(username)
            if in_ad is None:
                error = _("Die Registrierung ist gerade nicht möglich. Bitte versuche es später erneut.")
            elif in_ad:
                error = _("Dieser Benutzername ist bereits vergeben.")
        if error:
            flash(error, "error")
            session["reg_started"] = time.time() - MIN_FILL_SECONDS  # erneutes Absenden sofort erlaubt
            return render_template("register.html", v=values, privacy=privacy), 400

        _REG_LOG[ip].append(time.time())
        verify = needs_verification()
        from ..i18n import get_locale
        user = User(username=username, display_name=name, email=email, role=ROLE_READER, auth_source="local",
                    self_registered=True, email_verified=not verify, pending_approval=mode() == "approval",
                    active=False, language=get_locale())   # Mails in der Sprache der Registrierung
        user.set_password(password)
        gid = get_setting("registration_group_id")
        if gid.isdigit():
            group = db.session.get(Group, int(gid))
            if group:
                user.groups.append(group)
        maybe_activate(user)
        db.session.add(user)
        db.session.commit()
        log.info("Neue Registrierung: %s (%s) – Freigabe nötig: %s, Bestätigung nötig: %s",
                 username, email, user.pending_approval, verify)
        if verify:
            if not send_verification(user):
                flash(_("Die Bestätigungs-E-Mail konnte gerade nicht gesendet werden. Melde dich später an – "
                        "dort kannst du sie erneut anfordern."), "warning")
            return render_template("register_done.html", state="check_mail", email=email)
        notify_admins(user)
        if user.active:
            from .security import start_login
            flash(_("Willkommen! Dein Konto ist eingerichtet."), "success")
            return start_login(user, False, None)
        return render_template("register_done.html", state="approval")

    session["reg_started"] = time.time()
    return render_template("register.html", v=values, privacy=privacy)


@bp.route("/registrieren/bestaetigen/<token>")
def verify(token):
    try:
        data = _serializer().loads(token, max_age=VERIFY_MAX_AGE)
    except SignatureExpired:
        return render_template("register_done.html", state="expired"), 400
    except BadSignature:
        abort(404)
    user = db.session.get(User, data.get("uid"))
    if user is None or (user.email or "").lower() != (data.get("email") or "").lower():
        abort(404)
    if not user.email_verified:
        user.email_verified = True
        maybe_activate(user)
        db.session.commit()
        notify_admins(user)
        log.info("E-Mail bestätigt: %s", user.username)
    return render_template("register_done.html", state="verified", approval=user.pending_approval)


@bp.route("/registrieren/erneut-senden", methods=["POST"])
def resend():
    uid = session.pop("resend_uid", None)
    user = db.session.get(User, uid) if uid else None
    if user is not None and not user.email_verified and needs_verification():
        key = f"resend|{user.id}"
        if not _rate_limited(key):
            _REG_LOG[key].append(time.time())
            send_verification(user)
    return render_template("register_done.html", state="check_mail", email=user.email if user else "")
