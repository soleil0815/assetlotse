"""Two-factor sign-in: sign-in step 2 and management in the profile."""
from __future__ import annotations

import logging
import time

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template, request, session,
                   url_for)
from flask_login import current_user, login_required, login_user

from .. import lockout, twofa
from ..i18n import _
from ..models import User, WebAuthnCredential, db
from . import form_str, safe_next

bp = Blueprint("security", __name__)
log = logging.getLogger(__name__)

PENDING_TTL = 300  # seconds allowed for the second step


# ---------------------------------------------------------------- Sign-in

def start_login(user: User, remember: bool, next_url: str | None):
    """After a successful password check: either ask for the second factor or sign in directly."""
    from datetime import datetime
    from .. import netzone
    session.pop("mfa_pending", None)
    session.pop("mfa_skipped", None)
    session.pop("mfa_ok", None)
    internal = netzone.is_internal()
    if twofa.should_challenge(user, internal):
        session["mfa_pending"] = {"uid": user.id, "remember": bool(remember), "next": next_url or "",
                                  "ts": time.time()}
        return redirect(url_for("security.challenge"))
    # second factor skipped only because of "internal network"? Then the session is valid internally only:
    # no "stay signed in", and a new sign-in is required from outside.
    skipped = internal and twofa.needed_outside(user)
    if skipped:
        remember = False
    user.last_login = datetime.now()
    db.session.commit()
    login_user(user, remember=remember)
    if skipped:
        session["mfa_skipped"] = True
    if twofa.must_set_up(user, internal):
        session["mfa_setup"] = True
        flash(_("Please set up a second factor now (authenticator app or security key)."), "warning")
        return redirect(url_for("security.settings"))
    return redirect(safe_next(next_url) or url_for("main.dashboard"))


def _pending_user() -> User | None:
    p = session.get("mfa_pending")
    if not p or time.time() - p.get("ts", 0) > PENDING_TTL:
        session.pop("mfa_pending", None)
        return None
    user = db.session.get(User, p["uid"])
    return user if user is not None and user.active else None


def _finish(user: User):
    from datetime import datetime
    p = session.pop("mfa_pending", {})
    session.pop("webauthn_state", None)
    session.pop("mfa_skipped", None)
    user.last_login = datetime.now()
    db.session.commit()
    login_user(user, remember=p.get("remember", False))
    session["mfa_ok"] = True   # second factor confirmed in this sign-in (needed for managing factors)
    return safe_next(p.get("next")) or url_for("main.dashboard")


MFA_MAX_FAILS = 10   # failed second-factor attempts per sign-in, then the password is required again


def _account_throttled(user: User) -> bool:
    """Account-wide throttle (many failed attempts from many addresses): only reject; the pending sign-in
    or running session stays – otherwise an attacker could end it with failed attempts."""
    from .. import netzone
    return lockout.user_throttled(user.username, internal=netzone.is_internal())


def _too_many_mfa_fails(user: User) -> bool:
    """Too many wrong codes for this (still pending) sign-in or for the account overall?

    Counted in the database – so replaying an old session cookie does not help.
    When exceeded, the pending sign-in is discarded."""
    p = session.get("mfa_pending") or {}
    if lockout.mfa_fails_since(user.username, p.get("ts", 0)) >= MFA_MAX_FAILS:
        session.pop("mfa_pending", None)
        session.pop("webauthn_state", None)
        if current_user.is_authenticated:
            # step-up confirmation from a running session: the session ends too,
            # otherwise the counter could be reset again and again by restarting.
            from flask_login import logout_user
            session.clear()
            logout_user()
        log.warning("Too many failed second-factor attempts for %s – sign-in discarded", user.username)
        return True
    return False


def _fail_key(user: User) -> str:
    return f"2fa|{user.username.lower()}|{request.remote_addr}"


@bp.route("/login/2fa", methods=["GET", "POST"])
def challenge():
    user = _pending_user()
    if user is None:
        flash(_("Your sign-in has expired. Please sign in again."), "info")
        return redirect(url_for("main.login"))
    key = _fail_key(user)
    mode = request.values.get("modus", "")
    if request.method == "POST":
        if _too_many_mfa_fails(user):
            flash(_("Too many failed attempts with the second factor. Please sign in again."), "error")
            return redirect(url_for("main.login"))
        if lockout.throttled(key) or _account_throttled(user):
            flash(_("Too many failed attempts. Please wait a few minutes."), "error")
            return render_template("security/challenge.html", user=user, mode=mode), 429
        code = form_str("code", 40)
        ok = False
        if mode == "wiederherstellung":
            ok = twofa.use_recovery_code(user, code)
            if ok:
                left = twofa.recovery_codes_left(user)
                flash(_("Signed in with a recovery code. {n} code(s) left.", n=left), "warning")
        else:
            ok = twofa.verify_user_totp(user, code)
        if ok:
            lockout.clear(key)
            return redirect(_finish(user))
        lockout.fail(key, user.username)
        log.info("2FA failed for %s", user.username)
        flash(_("The code is invalid."), "error")
    return render_template("security/challenge.html", user=user, mode=mode,
                           totp_broken=twofa.totp_unreadable(user),
                           has_keys=any(c.rp_id == twofa.rp_id_for(request.host) for c in user.webauthn_credentials))


@bp.route("/login/2fa/schluessel/start", methods=["POST"])
def challenge_key_begin():
    user = _pending_user()
    if user is None:
        return jsonify({"ok": False, "error": _("Sign-in expired.")}), 400
    options, state = twofa.webauthn_auth_begin(user, twofa.rp_id_for(request.host))
    if options is None:
        return jsonify({"ok": False, "error": _("No security key is registered for this address.")}), 400
    session["webauthn_state"] = state
    return jsonify({"ok": True, "options": options})


@bp.route("/login/2fa/schluessel/pruefen", methods=["POST"])
def challenge_key_complete():
    user = _pending_user()
    state = session.pop("webauthn_state", None)
    if user is None or state is None:
        return jsonify({"ok": False, "error": _("Sign-in expired.")}), 400
    key = _fail_key(user)
    if _too_many_mfa_fails(user):
        flash(_("Too many failed attempts with the second factor. Please sign in again."), "error")
        return jsonify({"ok": False, "error": _("Too many failed attempts with the second factor. Please sign in again."),
                        "redirect": url_for("main.login")}), 429
    if lockout.throttled(key) or _account_throttled(user):
        return jsonify({"ok": False, "error": _("Too many failed attempts.")}), 429
    try:
        ok = twofa.webauthn_auth_complete(user, twofa.rp_id_for(request.host), state, request.get_json(force=True))
    except Exception as exc:  # noqa: BLE001
        log.info("WebAuthn sign-in %s failed: %s", user.username, exc)
        ok = False
    if not ok:
        lockout.fail(key, user.username)
        return jsonify({"ok": False, "error": _("The security key was not accepted.")}), 400
    lockout.clear(key)
    return jsonify({"ok": True, "redirect": _finish(user)})


# ---------------------------------------------------------------- Sign-in with a passkey (2.6.0)

PASSKEY_TTL = 300   # seconds between "start" and "check"


def passkey_available_for(user: User) -> bool:
    """May this account use a passkey? AD accounts only with a service account: without one, AssetLotse
    could not check at sign-in whether the account is still active and authorized in AD."""
    from ..ldap_auth import ldap_enabled
    from ..models import get_setting
    if not twofa.passkey_login_enabled():
        return False
    if user.auth_source == "ldap":
        return ldap_enabled() and bool(get_setting("ldap_bind_dn"))
    return True


def _passkey_fail(key: str, message: str, status: int = 400):
    lockout.fail(key, "")
    return jsonify({"ok": False, "error": message}), status


def _passkey_account_check(user: User) -> str | None:
    """The same gates as the password sign-in – for AD accounts checked live via the service account
    (still present, enabled, not expired, matching the role rules). Returns an error message or None."""
    from ..ldap_auth import Directory, LDAPError
    generic = _("This passkey cannot be used to sign in. Please sign in with user name and password.")
    if user.self_registered and not user.email_verified:
        return _("Please confirm your email address first.")
    if user.pending_approval:
        return _("Your account is still waiting for approval by the management team.")
    if user.auth_source == "ldap":
        if not passkey_available_for(user):
            return generic
        from .main import guid_conflict, may_reactivate, sync_ldap_user
        try:
            info = Directory().check_account(user.username)
        except LDAPError as exc:
            log.warning("Passkey sign-in %s: Active Directory not reachable: %s", user.username, exc)
            return _("The Active Directory cannot be reached right now. Please try again later.")
        if info is None or info.role is None or guid_conflict(info, user):
            log.info("Passkey sign-in %s rejected: not (or no longer) authorized in AD", user.username)
            return generic
        reactivate = may_reactivate(user)
        sync_ldap_user(info, user)
        if reactivate:
            user.active, user.deactivated_by_sync = True, False
        db.session.commit()
        session["ldap_ok_at"] = int(time.time())
    if not user.active:
        log.info("Passkey sign-in of locked account %s rejected", user.username)
        return generic
    return None


@bp.route("/login/passkey/start", methods=["POST"])
def passkey_begin():
    if not twofa.passkey_login_enabled():
        abort(404)
    if current_user.is_authenticated:
        return jsonify({"ok": True, "redirect": url_for("main.dashboard")})
    if lockout.throttled(f"passkey|{request.remote_addr}"):
        return jsonify({"ok": False, "error": _("Too many failed attempts. Please wait a few minutes.")}), 429
    options, state = twofa.passkey_auth_begin(twofa.rp_id_for(request.host))
    session["passkey_state"] = {"state": state, "ts": time.time()}
    return jsonify({"ok": True, "options": options})


@bp.route("/login/passkey/pruefen", methods=["POST"])
def passkey_complete():
    if not twofa.passkey_login_enabled():
        abort(404)
    from datetime import datetime
    pending = session.pop("passkey_state", None)
    key = f"passkey|{request.remote_addr}"
    if lockout.throttled(key):
        return jsonify({"ok": False, "error": _("Too many failed attempts. Please wait a few minutes.")}), 429
    if not pending or time.time() - pending.get("ts", 0) > PASSKEY_TTL:
        return jsonify({"ok": False, "error": _("Sign-in expired.")}), 400
    payload = request.get_json(force=True, silent=True) or {}
    try:
        user = twofa.passkey_auth_complete(twofa.rp_id_for(request.host), pending["state"],
                                           payload.get("credential") or {})
    except Exception as exc:  # noqa: BLE001 – invalid response: never give details to the client
        log.info("Passkey sign-in failed: %s", exc)
        user = None
    if user is None:
        return _passkey_fail(key, _("The passkey was not accepted."))
    problem = _passkey_account_check(user)
    if problem:
        return _passkey_fail(key, problem, 403)
    lockout.clear(key)
    # a passkey is two factors in one (the device + Face ID/fingerprint/PIN): no further step
    for k in ("mfa_pending", "mfa_skipped", "webauthn_state"):
        session.pop(k, None)
    user.last_login = datetime.now()
    db.session.commit()
    from ..idle import end_on_close
    login_user(user, remember=bool(payload.get("remember")) and not end_on_close())
    session["mfa_ok"] = True
    log.info("Passkey sign-in: %s", user.username)
    return jsonify({"ok": True, "redirect": safe_next(payload.get("next")) or url_for("main.dashboard")})


# ---------------------------------------------------------------- Profile: security

def _render_settings(**extra):
    rp = twofa.rp_id_for(request.host)
    return render_template("security/settings.html", rp_id=rp, required=twofa.required_for(current_user),
                           passkeys_offered=passkey_available_for(current_user),
                           passkey_login=twofa.passkey_login_enabled(),
                           totp_broken=twofa.totp_unreadable(current_user),
                           codes_left=twofa.recovery_codes_left(current_user),
                           keys=sorted(current_user.webauthn_credentials, key=lambda c: c.created_at or 0),
                           stepup_needed=not _may_manage(), **extra)


def _may_manage() -> bool:
    """Only someone who confirmed a second factor in this sign-in – or who has none yet (initial setup) – may
    add, replace or remove factors or generate new codes.

    Otherwise a session started on the internal network without a second factor could enroll its own
    factor and use it to sign in from outside."""
    return bool(session.get("mfa_ok")) or not current_user.has_2fa


def _stepup_msg() -> str:
    return _("Please confirm your second factor first (authenticator app, security "
             "key or recovery code) – you signed in to this session without it.")


def _first_factor_added(had_2fa: bool):
    """First second factor set up: whoever signed in before (possibly with a stolen password) should not
    simply stay signed in – sign out all other devices; this one stays signed in."""
    session.pop("mfa_skipped", None)   # factor just confirmed: the session no longer counts as "without 2FA"
    if had_2fa:
        return
    from .main import _stay_signed_in
    current_user.end_all_sessions()
    db.session.commit()
    _stay_signed_in(current_user._get_current_object())


def _refuse_manage():
    flash(_stepup_msg(), "warning")
    return redirect(url_for("security.settings"))


@bp.route("/profil/sicherheit/bestaetigen", methods=["POST"])
@login_required
def stepup():
    """Ask for the second factor afterwards for the running session (e.g. after signing in on the internal
    network without a second factor). Uses the normal sign-in step 2."""
    target = safe_next(request.form.get("next")) or url_for("security.settings")
    if not current_user.has_2fa or session.get("mfa_ok"):
        return redirect(target)
    session["mfa_pending"] = {"uid": current_user.id, "remember": False, "next": target,
                              "ts": time.time()}
    return redirect(url_for("security.challenge"))


def _last_factor(kind: str, cred_id: int | None = None) -> bool:
    """Would removing it delete the last factor although 2FA is mandatory?"""
    if not twofa.required_for(current_user):
        return False
    keys = [c for c in current_user.webauthn_credentials if c.id != cred_id]
    totp = current_user.totp_enabled and kind != "totp"
    return not keys and not totp


@bp.route("/profil/sicherheit")
@login_required
def settings():
    return _render_settings()


@bp.route("/profil/sicherheit/app/einrichten", methods=["POST"])
@login_required
def totp_start():
    if not _may_manage():
        return _refuse_manage()
    secret = twofa.new_totp_secret()
    session["totp_setup"] = twofa.encrypt(secret)
    uri = twofa.otpauth_uri(secret, current_user.username)
    return _render_settings(setup_secret=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
                            setup_qr=twofa.qr_svg(uri), setup_uri=uri)


@bp.route("/profil/sicherheit/app/bestaetigen", methods=["POST"])
@login_required
def totp_confirm():
    if not _may_manage():
        return _refuse_manage()
    enc = session.get("totp_setup")
    secret = twofa.decrypt(enc) if enc else ""
    if not secret:
        flash(_("The setup has expired. Please start again."), "error")
        return redirect(url_for("security.settings"))
    step = twofa.check_totp(secret, form_str("code", 20))
    if step is None:
        flash(_("The code is incorrect. Check the time on your phone and the server and enter the current code."), "error")
        uri = twofa.otpauth_uri(secret, current_user.username)
        return _render_settings(setup_secret=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
                                setup_qr=twofa.qr_svg(uri), setup_uri=uri)
    session.pop("totp_setup", None)
    had_2fa = current_user.has_2fa
    current_user.totp_secret = twofa.encrypt(secret)
    current_user.totp_enabled = True
    current_user.totp_last_step = step
    db.session.commit()
    _first_factor_added(had_2fa)
    session.pop("mfa_setup", None)
    session["mfa_ok"] = True   # the user just confirmed the new factor
    codes = twofa.new_recovery_codes(current_user) if not twofa.recovery_codes_left(current_user) else None
    flash(_("Authenticator app is set up."), "success")
    return _render_settings(new_codes=codes)


@bp.route("/profil/sicherheit/app/entfernen", methods=["POST"])
@login_required
def totp_disable():
    if not _may_manage():
        return _refuse_manage()
    if _last_factor("totp"):
        flash(_("A second factor is mandatory. Set up a security key first."), "error")
    elif not twofa.totp_unreadable(current_user) and not twofa.verify_user_totp(current_user, form_str("code", 20)):
        flash(_("To remove it, please enter a current code from the app."), "error")
    else:
        current_user.totp_enabled = False
        current_user.totp_secret = ""
        db.session.commit()
        flash(_("Authenticator app removed."), "success")
    return redirect(url_for("security.settings"))


@bp.route("/profil/sicherheit/codes", methods=["POST"])
@login_required
def recovery_regenerate():
    if not current_user.has_2fa:
        abort(400)
    if not _may_manage():
        return _refuse_manage()
    codes = twofa.new_recovery_codes(current_user)
    flash(_("New recovery codes generated – the old ones are no longer valid."), "success")
    return _render_settings(new_codes=codes)


@bp.route("/profil/sicherheit/schluessel/start", methods=["POST"])
@login_required
def key_begin():
    if not _may_manage():
        return jsonify({"ok": False, "error": _stepup_msg()}), 403
    passkey = bool((request.get_json(force=True, silent=True) or {}).get("passkey"))
    if passkey and not passkey_available_for(current_user):
        return jsonify({"ok": False, "error": _("Passkeys are not available for your account.")}), 403
    options, state = twofa.webauthn_register_begin(current_user, twofa.rp_id_for(request.host), passkey=passkey)
    session["webauthn_reg_state"] = state
    session["webauthn_reg_passkey"] = passkey
    return jsonify({"ok": True, "options": options})


@bp.route("/profil/sicherheit/schluessel/speichern", methods=["POST"])
@login_required
def key_complete():
    if not _may_manage():
        session.pop("webauthn_reg_state", None)
        return jsonify({"ok": False, "error": _stepup_msg()}), 403
    state = session.pop("webauthn_reg_state", None)
    passkey = bool(session.pop("webauthn_reg_passkey", False))
    payload = request.get_json(force=True) or {}
    if state is None:
        return jsonify({"ok": False, "error": _("Registration expired.")}), 400
    had_2fa = current_user.has_2fa
    try:
        twofa.webauthn_register_complete(current_user, twofa.rp_id_for(request.host), state,
                                         payload.get("credential") or {}, str(payload.get("name") or ""),
                                         passkey=passkey)
    except Exception as exc:  # noqa: BLE001
        log.info("WebAuthn registration failed: %s", exc)
        return jsonify({"ok": False, "error": _("The key could not be registered ({error}).", error=str(exc))}), 400
    _first_factor_added(had_2fa)
    session.pop("mfa_setup", None)
    session["mfa_ok"] = True   # the new key was just used
    first = not twofa.recovery_codes_left(current_user)
    if first:
        # only encrypted into the session cookie (readable by the browser)
        import json
        session["show_new_codes"] = twofa.encrypt(json.dumps(twofa.new_recovery_codes(current_user)))
    flash(_("Passkey added – from now on you can sign in with it.") if passkey else _("Security key added."),
          "success")
    return jsonify({"ok": True, "redirect": url_for("security.settings", neu="1" if first else None)})


@bp.route("/profil/sicherheit/schluessel/<int:cred_id>/entfernen", methods=["POST"])
@login_required
def key_delete(cred_id):
    cred = db.session.get(WebAuthnCredential, cred_id)
    if cred is None or cred.user_id != current_user.id:
        abort(404)
    if not _may_manage():
        return _refuse_manage()
    if _last_factor("key", cred.id):
        flash(_("A second factor is mandatory. This is your last one – set up another one first."), "error")
    else:
        db.session.delete(cred)
        db.session.commit()
        # "Sicherheitsschlüssel" is the stored default key name (stored format – keep)
        flash(_("“{name}” removed.", name=cred.name if cred.name and cred.name != "Sicherheitsschlüssel"
                                     else _("Security key")), "success")
    return redirect(url_for("security.settings"))


@bp.before_app_request
def enforce_setup():
    """Anyone who needs 2FA according to the policy must set it up before continuing.

    Also: a session started on the internal network without a second factor is not valid from outside."""
    from flask_login import logout_user
    from .. import netzone
    if not current_user.is_authenticated or request.endpoint == "static":
        return None
    internal = netzone.is_internal()
    if session.get("mfa_skipped") and not internal and twofa.needed_outside(current_user):
        log.info("Session of %s without second factor (started internally) used from outside – sign in again",
                 current_user.username)
        session.clear()     # first – logout_user() records in it that the remember cookie must be deleted
        logout_user()
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": _("Please sign in again.")}), 401
        flash(_("Access from outside requires a new sign-in with a second factor."), "warning")
        return redirect(url_for("main.login", next=request.full_path if request.method == "GET" else None))
    if not twofa.must_set_up(current_user, internal):
        return None
    allowed = {"static", "main.logout", "main.set_language", "help.index", "security.settings", "security.totp_start", "security.totp_confirm",
               "security.key_begin", "security.key_complete"}
    if request.endpoint in allowed:
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": _("Second factor required.")}), 403
    return redirect(url_for("security.settings"))


@bp.app_context_processor
def inject_codes():
    from flask import has_request_context
    if not has_request_context():  # e.g. email templates rendered in the background
        return {}
    enc = session.pop("show_new_codes", None) if request.args.get("neu") else None
    codes = None
    if enc:
        import json
        try:
            codes = json.loads(twofa.decrypt(enc) or "null")
        except ValueError:
            codes = None
    return {"pending_codes": codes} if codes else {}
