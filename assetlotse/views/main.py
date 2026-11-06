"""Sign-in, initial setup, dashboard, profile."""
from __future__ import annotations

import logging
from datetime import date, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError

from ..ldap_auth import LDAPError, authenticate, ldap_enabled, normalize_dn, rule_matches
from .. import lockout
from ..i18n import _
from ..models import (MIN_PASSWORD_LENGTH, ROLE_ADMIN, ROLE_MANAGER, Asset, Group, Loan, User, db, get_bool, get_setting,
                      manageable_assets_query, set_setting, visible_assets_query, catalog_hidden)
from . import form_str, safe_next

bp = Blueprint("main", __name__)
log = logging.getLogger(__name__)

# Protection against password guessing: see lockout.py (locks stored in the database)


def find_ldap_account(info) -> User | None:
    """Account for an AD person: first via the immutable AD identifier (also after a rename in AD), otherwise via
    the username."""
    if getattr(info, "guid", ""):
        user = User.query.filter(User.auth_source == "ldap", User.ldap_guid == info.guid).first()
        if user is not None:
            return user
    return User.query.filter(func.lower(User.username) == info.username.lower()).first()


def legacy_locked(info, user: User | None) -> bool:
    """Locked legacy account without an AD identifier: neither take it over nor overwrite it with new data – it may
    belong to a former person whose name has been reassigned. An administrator decides."""
    return bool(user is not None and user.auth_source == "ldap" and not user.active and not user.ldap_guid
                and getattr(info, "guid", ""))


def may_reactivate(user: User) -> bool:
    """May an account locked by the AD sync become active again automatically?

    Only with a stored AD identifier: for legacy accounts without one (created before 1.6, already gone from AD
    at the first sync) it is not certain whether the same person is returning or the name was reassigned – then
    an administrator decides."""
    return bool(user.deactivated_by_sync and user.ldap_guid)


def guid_conflict(info, user: User | None) -> bool:
    """Same username, but a different person according to the immutable identifier (e.g. a former employee's name
    reassigned)? Then the new person must not get the old account and its history."""
    return bool(user is not None and user.auth_source == "ldap" and user.ldap_guid
                and getattr(info, "guid", "") and user.ldap_guid != info.guid)


def sync_ldap_user(info, existing: User | None, ldap_groups: list | None = None,
                   group_sync: bool | None = None) -> User:
    """Sync an account with the data from AD (sign-in, session check, account takeover, background sync).

    `ldap_groups`/`group_sync` can be passed preloaded (background sync of many accounts)."""
    user = existing
    if user is None:
        user = User(username=info.username, auth_source="ldap")
        db.session.add(user)
    elif user.username != info.username and user.username.lower() != info.username.lower():
        # renamed in AD (found via the immutable identifier): adopt the name if it is free
        clash = User.query.filter(func.lower(User.username) == info.username.lower(), User.id != user.id).first()
        if clash is None:
            log.info("AD account %s is now called %s – renamed", user.username, info.username)
            user.username = info.username
    user.auth_source = "ldap"
    user.password_hash = None
    user.display_name = info.display_name
    user.email = info.email
    user.ldap_dn = info.dn
    guid = getattr(info, "guid", "")
    if guid and user.ldap_guid != guid and (user.active is not False or user.ldap_guid) and not (
            User.query.filter(User.ldap_guid == guid, User.id != (user.id or 0)).first()):
        # Never assign an identifier another account already has; never attach one to a locked account without an
        # identifier: whether the same person is returning or the name was reassigned is decided by an administrator
        # (activate the account manually – the identifier is adopted afterwards).
        user.ldap_guid = info.guid
    user.role = info.role
    if info.role != ROLE_MANAGER:
        user.manages_all = False      # otherwise the flag would come back on a later promotion
    if group_sync if group_sync is not None else get_bool("ldap_group_sync"):
        user_groups = {normalize_dn(g) for g in info.groups}
        for group in (ldap_groups if ldap_groups is not None else Group.query.filter(Group.ldap_dn != "").all()):
            member = rule_matches(group.ldap_dn, "", user_groups)
            if member and group not in user.groups:
                user.groups.append(group)
            elif not member and group in user.groups:
                user.groups.remove(group)
    return user


_DUMMY_HASH = None


def _dummy_password_check(password: str) -> None:
    global _DUMMY_HASH
    from werkzeug.security import check_password_hash, generate_password_hash
    if _DUMMY_HASH is None:
        _DUMMY_HASH = generate_password_hash("timing-equaliser")
    check_password_hash(_DUMMY_HASH, password or "")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        username = form_str("username", 200)
        password = request.form.get("password") or ""
        # lock key from the normalized name: "DOMAIN\\anna" and "anna@x" count as "anna"
        key = f"{lockout.norm(username)}|{request.remote_addr}"
        from .. import netzone
        if lockout.throttled(key) or lockout.user_throttled(username, internal=netzone.is_internal()):
            flash(_("Too many failed attempts. Please wait a few minutes."), "error")
            return render_template("login.html", username=username), 429

        user = User.query.filter(func.lower(User.username) == username.lower()).first()
        error = _("Incorrect username or password.")
        ok_user = None

        if user is not None and user.auth_source == "local":
            if user.check_password(password):
                ok_user = user
        elif not ldap_enabled():
            # take as long as for a real account – otherwise the response time reveals
            # whether the username exists.
            _dummy_password_check(password)
        elif ldap_enabled() and username and password:
            try:
                info = authenticate(username, password)
                if info.role is None:
                    # deliberately the same message as for a wrong password: outsiders must not be
                    # able to tell whether an account exists or the password was correct.
                    log.info("LDAP sign-in %s rejected: no rule matches (%s)", username, info.dn)
                else:
                    # always match AD accounts via the immutable identifier (even if the typed name exists)
                    user = find_ldap_account(info)
                    if guid_conflict(info, user):
                        log.warning("AD sign-in as %s rejected: according to objectGUID the AssetLotse account %s "
                                    "belongs to a different (former) person – rename the old account under Users",
                                    username, user.username)
                    elif legacy_locked(info, user):
                        log.warning("AD sign-in as %s rejected: the locked legacy account %s has no AD identifier – "
                                    "an administrator must check whether it is the same person", username, user.username)
                    elif user is not None and user.auth_source != "ldap":
                        # a local account is never taken over via AD sign-in (e.g. "DOMAIN\\kasse"
                        # for the local account "kasse") – same message as for a wrong password.
                        log.warning("AD sign-in as %s rejected: local account %s with the same name "
                                    "is not taken over", username, user.username)
                    else:
                        reactivate = user is not None and may_reactivate(user)   # check before syncing
                        ok_user = sync_ldap_user(info, user)
                        if reactivate:                # locked by the AD sync, permitted again according to AD
                            ok_user.active, ok_user.deactivated_by_sync = True, False
                        try:
                            db.session.commit()
                        except IntegrityError:
                            # first sign-in in two windows at once: the other one just created the account
                            # – roll back and use the account that now exists
                            db.session.rollback()
                            ok_user = find_ldap_account(info)
                            if ok_user is not None and (ok_user.auth_source != "ldap" or guid_conflict(info, ok_user)):
                                ok_user = None
            except LDAPError as exc:
                db.session.rollback()
                ok_user = None
                log.warning("LDAP sign-in %s failed: %s", username, exc)
            except Exception:  # noqa: BLE001 – never give details to the client
                db.session.rollback()
                ok_user = None
                log.exception("Unexpected error during LDAP sign-in")
                error = _("Sign-in is currently not possible. Please try again later.")

        # self-registered accounts: password is correct, but not yet confirmed/approved.
        # Only someone who knows the correct password sees these notices.
        if ok_user is not None and ok_user.self_registered and not ok_user.email_verified:
            session["resend_uid"] = ok_user.id
            return render_template("login.html", username=username, unverified=True), 403
        if ok_user is not None and ok_user.pending_approval:
            flash(_("Your account is still waiting for approval by the management team."), "info")
            return render_template("login.html", username=username), 403

        if ok_user is not None and not ok_user.active:
            log.info("Sign-in of locked account %s rejected", ok_user.username)
            ok_user = None

        if ok_user is None:
            lockout.fail(key, username)
            flash(error, "error")
            return render_template("login.html", username=username), 401

        lockout.clear(key)
        if ok_user.auth_source == "ldap":
            import time
            session["ldap_ok_at"] = int(time.time())   # just verified against AD
        from .security import start_login  # second factor, if set up
        return start_login(ok_user, bool(request.form.get("remember")), request.args.get("next"))
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    for k in ("mfa_ok", "mfa_skipped", "mfa_setup", "mfa_pending", "show_new_codes", "totp_setup",
              "webauthn_state", "webauthn_reg_state", "ldap_ok_at", "seen"):
        session.pop(k, None)
    logout_user()
    flash(_("You have been signed out."), "info")
    return redirect(url_for("main.login"))


def _setup_allowed() -> bool:
    """Initial setup only via a DIRECT connection from the internal network (or from localhost).

    Deliberately ignores forwarding headers such as X-Forwarded-For: anyone can send them, and connections from
    the Docker bridge would otherwise count as a trusted proxy. Setup therefore does not work through a reverse
    proxy – open it directly on the LAN instead, or create an admin account by command (README)."""
    import ipaddress

    from .. import netzone
    env = request.environ
    orig = env.get("werkzeug.proxy_fix.orig") or {}
    proxy_headers = ("HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_PROTO", "HTTP_X_FORWARDED_HOST", "HTTP_X_REAL_IP",
                     "HTTP_FORWARDED")
    if any(orig.get(h) or env.get(h) for h in proxy_headers):
        return False
    try:
        peer = ipaddress.ip_address((orig.get("REMOTE_ADDR") or env.get("REMOTE_ADDR") or "").split("%")[0])
    except ValueError:
        return False
    if getattr(peer, "ipv4_mapped", None):
        peer = peer.ipv4_mapped
    return peer.is_loopback or any(peer in n for n in netzone.internal_networks())


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if User.query.filter_by(role=ROLE_ADMIN).first() is not None:
        return redirect(url_for("main.login"))
    if get_setting("setup_completed") == "1" or User.query.first() is not None:
        # once set up: even without an administrator (e.g. deleted by accident) nobody may create
        # a new admin account via the web here.
        return render_template(
            "error.html", code=403, title=_("Setup already completed"),
            text=_("There is no administrator account any more. Create one on the server: docker compose "
                   "exec assetlotse docker-entrypoint.sh flask --app wsgi create-admin NAME")), 403
    if not _setup_allowed():
        # fresh installation exposed to the internet: whoever comes first would be admin. Hence only from the
        # internal network (or via localhost/SSH tunnel), otherwise by command on the server.
        log.warning("Initial setup from %s rejected (not on the internal network)", request.remote_addr)
        return render_template(
            "error.html", code=403, title=_("Setup only from the internal network"),
            text=_("Initial setup is only possible from the internal network. Otherwise "
                   "create the first admin account on the server: docker compose exec assetlotse "
                   "docker-entrypoint.sh flask --app wsgi create-admin NAME")), 403
    if request.method == "POST":
        username = form_str("username", 120)
        password = request.form.get("password") or ""
        if not username or len(password) < MIN_PASSWORD_LENGTH:
            flash(_("A username and a password of at least {n} characters are required.", n=MIN_PASSWORD_LENGTH), "error")
        elif password != request.form.get("password2"):
            flash(_("The passwords do not match."), "error")
        else:
            admin = User(username=username, display_name=form_str("display_name", 200) or username,
                         email=form_str("email", 200), role=ROLE_ADMIN, auth_source="local")
            admin.set_password(password)
            db.session.add(admin)
            set_setting("org_name", form_str("org_name", 120) or "AssetLotse")
            set_setting("setup_completed", "1")
            db.session.commit()
            login_user(admin)
            flash(_("Welcome! AssetLotse is set up."), "success")
            return redirect(url_for("main.dashboard"))
    return render_template("setup.html")


@bp.route("/")
@login_required
def dashboard():
    today = date.today()
    my_loans = (Loan.query.filter_by(user_id=current_user.id, returned_at=None)
                .order_by(Loan.due_at.is_(None), Loan.due_at).all())
    stats, overdue, due_soon, recent_loans, categories = None, [], [], [], []
    if current_user.is_manager:
        assets_q = manageable_assets_query(current_user)
        asset_ids = assets_q.with_entities(Asset.id)
        active_q = Loan.query.filter(Loan.returned_at.is_(None), Loan.asset_id.in_(asset_ids))
        lent = (db.session.query(Loan.asset_id, func.count(Loan.id).label("n"))
                .filter(Loan.returned_at.is_(None)).group_by(Loan.asset_id).subquery())
        n_lent = func.coalesce(lent.c.n, 0)
        from ..models import lendable_clause
        # free = lendable and not issued (never negative with legacy data; "no lending" never counts as free)
        free = case((lendable_clause() & (Asset.quantity > n_lent), Asset.quantity - n_lent), else_=0)
        joined = assets_q.outerjoin(lent, lent.c.asset_id == Asset.id)
        pieces, available = joined.with_entities(func.coalesce(func.sum(Asset.quantity), 0),
                                                 func.coalesce(func.sum(free), 0)).one()
        active = active_q.count()
        secrets_q = assets_q.filter(Asset.secret.isnot(None), Asset.secret != "")
        if not current_user.is_admin:   # count only details this manager may see
            secrets_q = secrets_q.filter(Asset.group_id.in_(list(current_user.group_ids) or [-1]))
        stats = {
            "entries": assets_q.count(),
            "pieces": pieces,
            "available": available,
            "active": active,
            "overdue": active_q.filter(Loan.due_at < today).count(),
            "due_soon": active_q.filter(Loan.due_at >= today, Loan.due_at <= today + timedelta(days=7)).count(),
            "secrets": secrets_q.count(),
        }
        stats["usage"] = min(100, round(100 * active / pieces)) if pieces else 0
        overdue = active_q.filter(Loan.due_at < today).order_by(Loan.due_at).limit(10).all()
        due_soon = (active_q.filter(Loan.due_at >= today, Loan.due_at <= today + timedelta(days=7))
                    .order_by(Loan.due_at).limit(10).all())
        recent_loans = (Loan.query.filter(Loan.asset_id.in_(asset_ids))
                        .order_by(Loan.loaned_at.desc()).limit(8).all())
        # inventory by category: entries, pieces, issued, free
        rows = (joined.with_entities(Asset.category, func.count(Asset.id), func.coalesce(func.sum(Asset.quantity), 0),
                                     func.coalesce(func.sum(lent.c.n), 0), func.coalesce(func.sum(free), 0))
                .group_by(Asset.category).all())
        categories = sorted(({"name": c or "", "entries": e, "pieces": q, "issued": i, "free": f,
                              "pct": min(100, round(100 * i / q)) if q else 0} for c, e, q, i, f in rows),
                            key=lambda r: (-r["pieces"], r["name"].lower()))
    newest = [] if stats or catalog_hidden(current_user) else visible_assets_query(current_user).order_by(Asset.created_at.desc()).limit(8).all()
    return render_template("dashboard.html", my_loans=my_loans, newest=newest, stats=stats, overdue=overdue,
                           due_soon=due_soon, recent_loans=recent_loans, categories=categories)


@bp.route("/profil", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST" and request.form.get("action") == "notify":
        current_user.email_notifications = bool(request.form.get("email_notifications"))
        db.session.commit()
        flash(_("Notifications saved."), "success")
        return redirect(url_for("main.profile"))
    if request.method == "POST" and request.form.get("action") == "data":
        # only local (e.g. self-registered) accounts change name and email – for AD accounts they come from AD
        if current_user.auth_source != "local":
            abort(400)
        from ..mailer import valid_email
        name = form_str("display_name", 200)
        mail = form_str("email", 200)
        if mail and not valid_email(mail):
            flash(_("Please enter a valid email address."), "error")
            return redirect(url_for("main.profile"))
        current_user.display_name = name
        if mail.lower() != (current_user.email or "").lower():
            from .register import _allowed_domain, email_taken, needs_verification, send_email_change
            if mail and email_taken(mail, current_user):
                db.session.rollback()
                flash(_("This email address is already used by another account."), "error")
                return redirect(url_for("main.profile"))
            if current_user.self_registered:
                # same rules as for registration: address required, allowed domain and – if confirmation
                # is required there – applied only after the link is clicked
                if not mail or not _allowed_domain(mail):
                    db.session.rollback()
                    flash(_("This email address is not allowed for your account."), "error")
                    return redirect(url_for("main.profile"))
                if needs_verification():
                    db.session.commit()                    # the name applies immediately
                    result = send_email_change(current_user, mail)
                    if result == "sent":
                        flash(_("Saved. We have sent a confirmation link to {email} "
                                "– the new address applies as soon as you open it.", email=mail), "info")
                    elif result == "limit":
                        flash(_("Name saved. Too many changes of the email "
                                "address – please try again in an hour."), "error")
                    elif result == "no_base":
                        flash(_("Name saved. The new email address cannot be confirmed "
                                "right now because email delivery is not fully set "
                                "up – please inform the administrators."), "error")
                    else:
                        flash(_("Name saved. The confirmation email could not be sent – "
                                "the address stays unchanged. Please try again later."), "error")
                    return redirect(url_for("main.profile"))
            current_user.email = mail
            current_user.pending_email = ""
        db.session.commit()
        flash(_("Your details have been saved."), "success")
        return redirect(url_for("main.profile"))
    if request.method == "POST":
        if current_user.auth_source != "local":
            abort(400)
        from .. import lockout
        current = request.form.get("current") or ""
        new = request.form.get("new") or ""
        key = f"pwchange|{lockout.norm(current_user.username)}"
        if lockout.throttled(key):
            # throttle guessing of the current password from someone else's session
            flash(_("Too many failed attempts. Please wait a few minutes."), "error")
        elif not current_user.check_password(current):
            lockout.fail(key, current_user.username)
            log.warning("Password change: wrong current password from %s", current_user.username)
            flash(_("Your current password is incorrect."), "error")
        elif len(new) < MIN_PASSWORD_LENGTH:
            flash(_("The new password must be at least {n} characters long.", n=MIN_PASSWORD_LENGTH), "error")
        elif new != request.form.get("new2"):
            flash(_("The new passwords do not match."), "error")
        else:
            lockout.clear(key)
            current_user.set_password(new)   # signs out all other devices …
            db.session.commit()
            _stay_signed_in(current_user._get_current_object())      # … this device stays signed in
            flash(_("Password changed. Other devices have been signed out."), "success")
            return redirect(url_for("main.profile"))
    history = (Loan.query.filter_by(user_id=current_user.id)
               .order_by(Loan.loaned_at.desc()).limit(50).all())
    return render_template("profile.html", history=history,
                           self_extend=get_bool("self_extend"), mail_on=get_bool("mail_enabled"),
                           max_ext=int(get_setting("max_extensions") or 0))


def _stay_signed_in(user: User):
    """Keep the current device signed in after the session generation changed."""
    remembered = bool(request.cookies.get("remember_token"))
    mfa_skipped = session.get("mfa_skipped")
    login_user(user, remember=remembered and not mfa_skipped)
    if mfa_skipped:
        session["mfa_skipped"] = True


@bp.route("/abmelden-ueberall", methods=["POST"])
@login_required
def logout_everywhere():
    current_user.end_all_sessions()
    db.session.commit()
    _stay_signed_in(current_user._get_current_object())
    flash(_("All other devices have been signed out."), "success")
    return redirect(url_for("security.settings"))


@bp.route("/branding/<path:filename>")
def branding_file(filename):
    """Logo, favicon and background image – public, since they are also shown on the sign-in page."""
    from flask import send_from_directory
    from werkzeug.utils import secure_filename

    from ..branding import brand_dir
    name = secure_filename(filename)
    if not name or not name.split("-", 1)[0] in ("logo", "bg", "favicon"):
        abort(404)
    resp = send_from_directory(brand_dir(), name, max_age=60 * 60 * 24 * 365)
    if name.endswith(".svg"):
        # SVG must never run scripts – not even when opened directly
        resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
        resp.headers["Content-Type"] = "image/svg+xml"
    return resp


# ---------------------------------------------------------------- About / license (AGPL § 13)

def _license_file():
    from pathlib import Path
    from flask import current_app
    for p in (Path(current_app.root_path).parent / "LICENSE", Path(current_app.root_path) / "LICENSE"):
        if p.is_file():
            return p
    return None


@bp.route("/sprache/<code>", methods=["POST"])
def set_language(code):
    """Switch language – stored in the profile when signed in, otherwise for this session."""
    from ..i18n import normalize
    lang = normalize(code)
    if lang is None:
        abort(404)
    session["lang"] = lang
    from flask import g
    g.pop("_assetlotse_locale", None)   # messages of this request already in the new language
    if current_user.is_authenticated:
        current_user.language = lang
        db.session.commit()
    return redirect(safe_next(request.form.get("next")) or url_for("main.dashboard"))


@bp.route("/info")
@login_required
def about():
    return render_template("about.html", has_license=_license_file() is not None)


@bp.route("/neuigkeiten")
@login_required
def news():
    from .. import news as news_mod
    from ..i18n import get_locale
    lang = get_locale()
    unread_ids = {id(e) for e in news_mod.unread(current_user)}
    groups: list[dict] = []
    for e in news_mod.for_user(current_user):
        if not groups or groups[-1]["version"] != e["version"]:
            try:
                day = date.fromisoformat(e.get("date", ""))
            except ValueError:
                day = None
            groups.append({"version": e["version"], "date": day, "items": []})
        groups[-1]["items"].append({**news_mod.text(e, lang), "kind": e["kind"], "new": id(e) in unread_ids})
    if news_mod.mark_read(current_user):
        db.session.commit()
    return render_template("news.html", groups=groups)


@bp.route("/info/lizenz")
@login_required
def license_text():
    f = _license_file()
    if f is None:
        abort(404)
    return f.read_text(encoding="utf-8"), 200, {"Content-Type": "text/plain; charset=utf-8"}
