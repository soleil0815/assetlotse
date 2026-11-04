"""Self-registration of new staff members.

Modes (Administration → Settings):
  off       – no registration (default)
  approval  – account is created, an admin must approve it
  open      – account is active immediately
Optional extras: email confirmation (if email delivery is active), allowed email domains,
default group, link to the privacy policy, notification of the admins.

Protection when exposed to the internet: honeypot field, minimum fill-in time, per-IP limit,
reserved names, no names that exist in Active Directory.
"""
from __future__ import annotations

import logging
import re
import time

from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request, session,
                   url_for)
from flask_login import current_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func

from ..i18n import _, _p
from ..models import MIN_PASSWORD_LENGTH, ROLE_ADMIN, ROLE_READER, Group, User, db, get_bool, get_setting
from . import form_str

bp = Blueprint("register", __name__)
log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,39}$")
RESERVED = {"admin", "administrator", "root", "system", "assetlotse", "support", "service", "postmaster",
            "webmaster", "hostmaster", "info", "noreply", "no-reply", "guest", "gast", "test", "krbtgt"}
VERIFY_MAX_AGE = 48 * 3600
MIN_FILL_SECONDS = 3
REG_PER_HOUR = 5
ATTEMPTS_PER_HOUR = 30      # every submission counts – slows down probing for existing names/addresses


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
    """Base address for links in emails. Only the configured address – the Host header of an anonymous request
    can be chosen freely (links to foreign sites). Exception: a signed-in admin triggers the email themselves."""
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
    """True/False, or None if AD could not be checked."""
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
        log.warning("Registration: AD check for %s not possible: %s", username, exc)
        return None


_KEY = "~reg~|"   # counter in the login_attempts table – shared by all server processes


def _rate_limited(key: str, limit: int = REG_PER_HOUR) -> bool:
    from datetime import datetime, timedelta

    from ..models import LoginAttempt
    since = datetime.now() - timedelta(hours=1)
    return LoginAttempt.query.filter(LoginAttempt.key == _KEY + key[:250],
                                     LoginAttempt.created_at >= since).count() >= limit


def _count(key: str) -> None:
    from ..models import LoginAttempt
    db.session.add(LoginAttempt(key=_KEY + key[:250], username="-"))
    db.session.commit()


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
        log.warning("Confirmation email not sent: no address for links in emails configured "
                    "(Administration → Email)")
        return False
    token = _serializer().dumps({"uid": user.id, "email": user.email})
    link = f"{base}{url_for('register.verify', token=token)}"
    return _send("verify", user.email, user=user, link=link)


def _change_serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="email-change")


EMAIL_CHANGES_PER_HOUR = 3


def send_email_change(user: User, new_email: str) -> str:
    """Apply a self-registered account's new address only after confirmation (as during registration).
    Stores it as "awaiting confirmation". Result: "sent", "limit", "no_base" or "failed"."""
    key = f"emailchange|{user.id}"
    if _rate_limited(key, EMAIL_CHANGES_PER_HOUR):
        return "limit"
    base = _base_url()
    from ..mailer import mail_active, render_mail, send_mail
    if not base or not mail_active():
        return "no_base"
    _count(key)
    user.pending_email = new_email
    db.session.commit()
    # the token is valid only for exactly this pending address: a later change invalidates older links.
    token = _change_serializer().dumps({"uid": user.id, "email": new_email})
    link = f"{base}{url_for('register.confirm_email_change', token=token)}"
    subject, text, html = render_mail("email_change", user=user, link=link)
    ok, _detail = send_mail(new_email, subject, text, html, kind="account", user_id=user.id)
    return "sent" if ok else "failed"


def email_taken(email: str, user: User) -> bool:
    """Does the address already belong to another account? (Each address is allowed only once for registration.)"""
    return User.query.filter(func.lower(User.email) == email.lower(), User.id != user.id).first() is not None


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


# ---------------------------------------------------------------- Pages

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
        # bots: hidden field filled in or form submitted within a fraction of a second
        started = session.pop("reg_started", 0)
        if form_str("homepage_url", 200) or (started and time.time() - started < MIN_FILL_SECONDS):
            log.info("Registration from %s discarded as bot", ip)
            return render_template("register_done.html", state="check_mail" if needs_verification() else "approval")
        if not started:  # session expired or similar – just show the form again
            session["reg_started"] = time.time() - MIN_FILL_SECONDS
            flash(_("Please submit the form again."), "info")
            return render_template("register.html", v=values, privacy=privacy), 400
        if _rate_limited(ip) or _rate_limited(f"try|{ip}", ATTEMPTS_PER_HOUR):
            flash(_("Too many registrations from your connection. Please try again later."), "error")
            return render_template("register.html", v=values, privacy=privacy), 429

        _count(f"try|{ip}")
        username, name, email = values["username"], values["display_name"], values["email"].lower()
        password = request.form.get("password") or ""
        error = None
        if needs_verification() and not _base_url():
            log.warning("Registration rejected: email confirmation is on, but no address for links in "
                        "emails is configured (Administration → Email)")
            flash(_("Registration is not possible right now. Please try again later."), "error")
            return render_template("register.html", v=values, privacy=privacy), 503
        if not USERNAME_RE.match(username):
            error = _("Username: 3–40 characters, only letters, digits, full stop, hyphen and underscore.")
        elif username.lower() in RESERVED:
            error = _("This username is not available.")
        elif not name:
            error = _("Please enter your name.")
        elif not __import__("assetlotse.mailer", fromlist=["valid_email"]).valid_email(email):
            error = _p("alt", "Please enter a valid email address.")
        elif not _allowed_domain(email):
            error = _("Registration is not possible with this email address.")
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = _("The password must be at least {n} characters long.", n=MIN_PASSWORD_LENGTH)
        elif password.lower() in (username.lower(), email):
            error = _("The password must not be the same as your username or email address.")
        elif password != request.form.get("password2"):
            error = _("The passwords do not match.")
        elif privacy and not request.form.get("privacy"):
            error = _("Please confirm the privacy policy.")
        elif User.query.filter(func.lower(User.username) == username.lower()).first():
            error = _("This username is already taken.")
        elif User.query.filter(func.lower(User.email) == email).first():
            # deliberately reported openly: it could not be hidden anyway (the username would otherwise show
            # as free or taken). Probing is throttled by the per-connection limit.
            error = _("An account with this email address already exists.")
        else:
            in_ad = _exists_in_directory(username)
            if in_ad is None:
                error = _("Registration is not possible right now. Please try again later.")
            elif in_ad:
                error = _("This username is already taken.")
        if error:
            flash(error, "error")
            session["reg_started"] = time.time() - MIN_FILL_SECONDS  # resubmitting is allowed immediately
            return render_template("register.html", v=values, privacy=privacy), 400

        _count(ip)
        verify = needs_verification()
        from ..i18n import get_locale
        user = User(username=username, display_name=name, email=email, role=ROLE_READER, auth_source="local",
                    self_registered=True, email_verified=not verify, pending_approval=mode() == "approval",
                    active=False, language=get_locale())   # emails in the language used for registration
        user.set_password(password)
        gid = get_setting("registration_group_id")
        if gid.isdigit():
            group = db.session.get(Group, int(gid))
            if group:
                user.groups.append(group)
        maybe_activate(user)
        db.session.add(user)
        db.session.commit()
        log.info("New registration: %s (%s) – approval required: %s, confirmation required: %s",
                 username, email, user.pending_approval, verify)
        if verify:
            if not send_verification(user):
                flash(_("The confirmation email could not be sent right now. "
                        "Sign in later – you can request it again there."), "warning")
            return render_template("register_done.html", state="check_mail", email=email)
        notify_admins(user)
        if user.active:
            from .security import start_login
            flash(_("Welcome! Your account is set up."), "success")
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
        log.info("Email confirmed: %s", user.username)
    return render_template("register_done.html", state="verified", approval=user.pending_approval)


@bp.route("/profil/email-bestaetigen/<token>")
def confirm_email_change(token):
    try:
        data = _change_serializer().loads(token, max_age=VERIFY_MAX_AGE)
    except SignatureExpired:
        flash(_("The confirmation link has expired. Please enter the new address in your profile again."), "error")
        return redirect(url_for("main.profile"))
    except BadSignature:
        abort(404)
    user = db.session.get(User, data.get("uid"))
    new = (data.get("email") or "").strip()
    # only the most recently pending address, only for accounts that are still local and self-registered
    if (user is None or not new or (user.pending_email or "").lower() != new.lower()
            or user.auth_source != "local" or not user.self_registered):
        abort(404)
    user.pending_email = ""
    if not _allowed_domain(new):
        db.session.commit()
        flash(_("This email address is not allowed for your account."), "error")
        return redirect(url_for("main.profile"))
    if email_taken(new, user):
        db.session.commit()
        flash(_("This email address is now used by another account."), "error")
        return redirect(url_for("main.profile"))
    user.email = new
    db.session.commit()
    log.info("Email address changed and confirmed: %s", user.username)
    flash(_("Your new email address is confirmed."), "success")
    return redirect(url_for("main.profile"))


@bp.route("/registrieren/erneut-senden", methods=["POST"])
def resend():
    uid = session.pop("resend_uid", None)
    user = db.session.get(User, uid) if uid else None
    if user is not None and not user.email_verified and needs_verification():
        key = f"resend|{user.id}"
        if not _rate_limited(key):
            _count(key)
            send_verification(user)
    return render_template("register_done.html", state="check_mail", email=user.email if user else "")
