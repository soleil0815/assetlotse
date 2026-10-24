"""Anmeldung, Ersteinrichtung, Übersicht, Profil."""
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
from ..models import (MIN_PASSWORD_LENGTH, ROLE_ADMIN, Asset, Group, Loan, User, db, get_bool, get_setting,
                      manageable_assets_query, set_setting, visible_assets_query, catalog_hidden)
from . import form_str, safe_next

bp = Blueprint("main", __name__)
log = logging.getLogger(__name__)

# Einfacher Schutz gegen Passwort-Raten: 8 Fehlversuche pro Benutzer/IP -> 5 Minuten Sperre
# Schutz gegen Passwort-Raten: siehe lockout.py (Sperre in der Datenbank)


def find_ldap_account(info) -> User | None:
    """Konto zu einer AD-Person: zuerst über die feste AD-Kennung (auch nach Umbenennung im AD), sonst über den
    Benutzernamen."""
    if getattr(info, "guid", ""):
        user = User.query.filter(User.auth_source == "ldap", User.ldap_guid == info.guid).first()
        if user is not None:
            return user
    return User.query.filter(func.lower(User.username) == info.username.lower()).first()


def legacy_locked(info, user: User | None) -> bool:
    """Gesperrtes Altkonto ohne AD-Kennung: weder übernehmen noch mit neuen Daten überschreiben – es könnte einer
    früheren Person gehören, deren Name neu vergeben wurde. Entscheidet ein Administrator."""
    return bool(user is not None and user.auth_source == "ldap" and not user.active and not user.ldap_guid
                and getattr(info, "guid", ""))


def may_reactivate(user: User) -> bool:
    """Darf ein vom AD-Abgleich gesperrtes Konto automatisch wieder aktiv werden?

    Nur mit gespeicherter AD-Kennung: Bei Altkonten ohne Kennung (vor 1.6 angelegt, beim ersten Abgleich schon
    nicht mehr im AD) ist nicht sicher, ob dieselbe Person zurückkommt oder ihr Name neu vergeben wurde – dann
    entscheidet ein Administrator."""
    return bool(user.deactivated_by_sync and user.ldap_guid)


def guid_conflict(info, user: User | None) -> bool:
    """Gleicher Benutzername, aber laut fester Kennung eine andere Person (z. B. Name eines Ausgeschiedenen neu
    vergeben)? Dann darf die neue Person nicht das alte Konto samt Verlauf bekommen."""
    return bool(user is not None and user.auth_source == "ldap" and user.ldap_guid
                and getattr(info, "guid", "") and user.ldap_guid != info.guid)


def sync_ldap_user(info, existing: User | None, ldap_groups: list | None = None,
                   group_sync: bool | None = None) -> User:
    """Konto mit den Angaben aus dem AD abgleichen (Login, Sitzungsprüfung, Kontoübernahme, Hintergrund-Abgleich).

    `ldap_groups`/`group_sync` können vorab geladen übergeben werden (Hintergrund-Abgleich vieler Konten)."""
    user = existing
    if user is None:
        user = User(username=info.username, auth_source="ldap")
        db.session.add(user)
    elif user.username != info.username and user.username.lower() != info.username.lower():
        # Im AD umbenannt (über die feste Kennung gefunden): Namen übernehmen, wenn er frei ist
        clash = User.query.filter(func.lower(User.username) == info.username.lower(), User.id != user.id).first()
        if clash is None:
            log.info("AD-Konto %s heißt jetzt %s – umbenannt", user.username, info.username)
            user.username = info.username
    user.auth_source = "ldap"
    user.password_hash = None
    user.display_name = info.display_name
    user.email = info.email
    user.ldap_dn = info.dn
    guid = getattr(info, "guid", "")
    if guid and user.ldap_guid != guid and (user.active is not False or user.ldap_guid) and not (
            User.query.filter(User.ldap_guid == guid, User.id != (user.id or 0)).first()):
        # Nie eine Kennung vergeben, die schon ein anderes Konto hat; nie an ein gesperrtes Konto ohne Kennung hängen: Ob dieselbe Person zurückkommt oder ihr Name neu vergeben
        # wurde, entscheidet ein Administrator (Konto von Hand aktivieren – danach wird die Kennung übernommen).
        user.ldap_guid = info.guid
    user.role = info.role
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
        # Sperrschlüssel aus dem normalisierten Namen: „DOMÄNE\\anna“ und „anna@x“ zählen wie „anna“
        key = f"{lockout.norm(username)}|{request.remote_addr}"
        from .. import netzone
        if lockout.throttled(key) or lockout.user_throttled(username, internal=netzone.is_internal()):
            flash(_("Zu viele Fehlversuche. Bitte warte einige Minuten."), "error")
            return render_template("login.html", username=username), 429

        user = User.query.filter(func.lower(User.username) == username.lower()).first()
        error = _("Benutzername oder Passwort ist falsch.")
        ok_user = None

        if user is not None and user.auth_source == "local":
            if user.check_password(password):
                ok_user = user
        elif not ldap_enabled():
            # Gleich lange rechnen wie bei einem echten Konto – sonst verrät die Antwortzeit,
            # ob es den Benutzernamen gibt.
            _dummy_password_check(password)
        elif ldap_enabled() and username and password:
            try:
                info = authenticate(username, password)
                if info.role is None:
                    # Bewusst dieselbe Meldung wie bei falschem Passwort: von außen soll nicht
                    # erkennbar sein, ob ein Konto existiert oder das Passwort stimmte.
                    log.info("LDAP-Login %s abgelehnt: keine Regel passt (%s)", username, info.dn)
                else:
                    # AD-Konten immer über die feste Kennung zuordnen (auch wenn es den eingetippten Namen gibt)
                    user = find_ldap_account(info)
                    if guid_conflict(info, user):
                        log.warning("AD-Anmeldung als %s abgelehnt: das AssetLotse-Konto %s gehört laut objectGUID "
                                    "zu einer anderen (früheren) Person – altes Konto unter Benutzer umbenennen",
                                    username, user.username)
                    elif legacy_locked(info, user):
                        log.warning("AD-Anmeldung als %s abgelehnt: das gesperrte Altkonto %s hat keine AD-Kennung – "
                                    "ein Administrator muss prüfen, ob es dieselbe Person ist", username, user.username)
                    elif user is not None and user.auth_source != "ldap":
                        # Ein lokales Konto wird nie per AD-Anmeldung übernommen (z. B. „DOMÄNE\\kasse“
                        # für das lokale Konto „kasse“) – gleiche Meldung wie bei falschem Passwort.
                        log.warning("AD-Anmeldung als %s abgelehnt: lokales Konto %s mit gleichem Namen "
                                    "wird nicht übernommen", username, user.username)
                    else:
                        reactivate = user is not None and may_reactivate(user)   # vor dem Abgleich prüfen
                        ok_user = sync_ldap_user(info, user)
                        if reactivate:                # vom AD-Abgleich gesperrt, laut AD wieder berechtigt
                            ok_user.active, ok_user.deactivated_by_sync = True, False
                        try:
                            db.session.commit()
                        except IntegrityError:
                            # Erste Anmeldung gleichzeitig in zwei Fenstern: das andere hat das Konto gerade
                            # angelegt – zurückrollen und das nun vorhandene Konto verwenden
                            db.session.rollback()
                            ok_user = find_ldap_account(info)
                            if ok_user is not None and (ok_user.auth_source != "ldap" or guid_conflict(info, ok_user)):
                                ok_user = None
            except LDAPError as exc:
                db.session.rollback()
                ok_user = None
                log.warning("LDAP-Login %s fehlgeschlagen: %s", username, exc)
            except Exception:  # noqa: BLE001 – niemals Details an den Client geben
                db.session.rollback()
                ok_user = None
                log.exception("Unerwarteter Fehler bei der LDAP-Anmeldung")
                error = _("Anmeldung derzeit nicht möglich. Bitte versuche es später erneut.")

        # Selbst registrierte Konten: Passwort stimmt, aber noch nicht bestätigt/freigegeben.
        # Diese Hinweise sieht nur, wer das richtige Passwort kennt.
        if ok_user is not None and ok_user.self_registered and not ok_user.email_verified:
            session["resend_uid"] = ok_user.id
            return render_template("login.html", username=username, unverified=True), 403
        if ok_user is not None and ok_user.pending_approval:
            flash(_("Dein Konto wartet noch auf die Freigabe durch die Verwaltung."), "info")
            return render_template("login.html", username=username), 403

        if ok_user is not None and not ok_user.active:
            log.info("Anmeldung von gesperrtem Konto %s abgelehnt", ok_user.username)
            ok_user = None

        if ok_user is None:
            lockout.fail(key, username)
            flash(error, "error")
            return render_template("login.html", username=username), 401

        lockout.clear(key)
        if ok_user.auth_source == "ldap":
            import time
            session["ldap_ok_at"] = int(time.time())   # gerade gegen das AD geprüft
        from .security import start_login  # zweiter Faktor, falls eingerichtet
        return start_login(ok_user, bool(request.form.get("remember")), request.args.get("next"))
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    for k in ("mfa_ok", "mfa_skipped", "mfa_setup", "mfa_pending", "show_new_codes", "totp_setup",
              "webauthn_state", "webauthn_reg_state", "ldap_ok_at"):
        session.pop(k, None)
    logout_user()
    flash(_("Du wurdest abgemeldet."), "info")
    return redirect(url_for("main.login"))


def _setup_allowed() -> bool:
    import ipaddress

    from .. import netzone
    if netzone.is_internal():
        return True
    env = request.environ
    orig = env.get("werkzeug.proxy_fix.orig") or {}
    proxy_headers = ("HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_PROTO", "HTTP_X_FORWARDED_HOST", "HTTP_X_REAL_IP",
                     "HTTP_FORWARDED")
    if any(orig.get(h) or env.get(h) for h in proxy_headers):
        return False          # nur echte direkte Verbindungen von localhost – nichts, was über einen Proxy kam
    try:
        peer = orig.get("REMOTE_ADDR") or env.get("REMOTE_ADDR") or ""
        return ipaddress.ip_address(peer.split("%")[0]).is_loopback
    except ValueError:
        return False


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if User.query.filter_by(role=ROLE_ADMIN).first() is not None:
        return redirect(url_for("main.login"))
    if get_setting("setup_completed") == "1" or User.query.first() is not None:
        # Einmal eingerichtet: Auch ohne Administrator (z. B. versehentlich gelöscht) darf hier niemand
        # über das Web ein neues Admin-Konto anlegen.
        return render_template(
            "error.html", code=403, title=_("Einrichtung bereits abgeschlossen"),
            text=_("Es gibt kein Administratorkonto mehr. Lege auf dem Server eines an: "
                   "docker compose exec assetlotse docker-entrypoint.sh flask --app wsgi create-admin NAME")), 403
    if not _setup_allowed():
        # Frische Installation, offen im Internet: wer zuerst kommt, wäre Admin. Daher nur aus dem internen
        # Netz (oder über localhost/SSH-Tunnel), sonst auf dem Server per Befehl.
        log.warning("Ersteinrichtung von %s abgelehnt (nicht im internen Netz)", request.remote_addr)
        return render_template(
            "error.html", code=403, title=_("Einrichtung nur aus dem internen Netz"),
            text=_("Die Ersteinrichtung ist nur aus dem internen Netz möglich. Lege das erste Admin-Konto sonst "
                   "auf dem Server an: docker compose exec assetlotse docker-entrypoint.sh flask --app wsgi "
                   "create-admin NAME")), 403
    if request.method == "POST":
        username = form_str("username", 120)
        password = request.form.get("password") or ""
        if not username or len(password) < MIN_PASSWORD_LENGTH:
            flash(_("Benutzername und ein Passwort mit mindestens {n} Zeichen sind nötig.", n=MIN_PASSWORD_LENGTH), "error")
        elif password != request.form.get("password2"):
            flash(_("Die Passwörter stimmen nicht überein."), "error")
        else:
            admin = User(username=username, display_name=form_str("display_name", 200) or username,
                         email=form_str("email", 200), role=ROLE_ADMIN, auth_source="local")
            admin.set_password(password)
            db.session.add(admin)
            set_setting("org_name", form_str("org_name", 120) or "AssetLotse")
            set_setting("setup_completed", "1")
            db.session.commit()
            login_user(admin)
            flash(_("Willkommen! AssetLotse ist eingerichtet."), "success")
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
        free = case((Asset.quantity > n_lent, Asset.quantity - n_lent), else_=0)   # nie negativ (Altdaten)
        joined = assets_q.outerjoin(lent, lent.c.asset_id == Asset.id)
        pieces, available = joined.with_entities(func.coalesce(func.sum(Asset.quantity), 0),
                                                 func.coalesce(func.sum(free), 0)).one()
        active = active_q.count()
        secrets_q = assets_q.filter(Asset.secret.isnot(None), Asset.secret != "")
        if not current_user.is_admin:   # nur Angaben zählen, die dieser Verwalter auch sehen darf
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
        # Bestand nach Kategorie: Einträge, Stück, ausgegeben, frei
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
        if current_user.auth_source == "local":
            from ..mailer import valid_email
            mail = form_str("email", 200)
            if mail and not valid_email(mail):
                flash(_("Bitte eine gültige E-Mail-Adresse eingeben."), "error")
                return redirect(url_for("main.profile"))
            if (current_user.self_registered and mail and mail.lower() != (current_user.email or "").lower()):
                from .register import _allowed_domain
                if not _allowed_domain(mail):
                    # gleiche Regel wie bei der Registrierung
                    flash(_("Diese E-Mail-Adresse ist für dein Konto nicht erlaubt."), "error")
                    return redirect(url_for("main.profile"))
            current_user.email = mail
        db.session.commit()
        flash(_("Benachrichtigungen gespeichert."), "success")
        return redirect(url_for("main.profile"))
    if request.method == "POST":
        if current_user.auth_source != "local":
            abort(400)
        from .. import lockout
        current = request.form.get("current") or ""
        new = request.form.get("new") or ""
        key = f"pwchange|{lockout.norm(current_user.username)}"
        if lockout.throttled(key):
            # Bremse gegen Raten des aktuellen Passworts aus einer fremden Sitzung heraus
            flash(_("Zu viele Fehlversuche. Bitte warte einige Minuten."), "error")
        elif not current_user.check_password(current):
            lockout.fail(key, current_user.username)
            log.warning("Passwortwechsel: aktuelles Passwort von %s falsch", current_user.username)
            flash(_("Das aktuelle Passwort ist falsch."), "error")
        elif len(new) < MIN_PASSWORD_LENGTH:
            flash(_("Das neue Passwort muss mindestens {n} Zeichen haben.", n=MIN_PASSWORD_LENGTH), "error")
        elif new != request.form.get("new2"):
            flash(_("Die neuen Passwörter stimmen nicht überein."), "error")
        else:
            lockout.clear(key)
            current_user.set_password(new)   # meldet alle anderen Geräte ab …
            db.session.commit()
            _stay_signed_in(current_user._get_current_object())      # … dieses Gerät bleibt angemeldet
            flash(_("Passwort geändert. Andere Geräte wurden abgemeldet."), "success")
            return redirect(url_for("main.profile"))
    history = (Loan.query.filter_by(user_id=current_user.id)
               .order_by(Loan.loaned_at.desc()).limit(50).all())
    return render_template("profile.html", history=history,
                           self_extend=get_bool("self_extend"), mail_on=get_bool("mail_enabled"),
                           max_ext=int(get_setting("max_extensions") or 0))


def _stay_signed_in(user: User):
    """Nach einem Wechsel der Sitzungs-Generation das aktuelle Gerät angemeldet lassen."""
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
    flash(_("Alle anderen Geräte wurden abgemeldet."), "success")
    return redirect(url_for("security.settings"))


@bp.route("/branding/<path:filename>")
def branding_file(filename):
    """Logo, Favicon und Hintergrundbild – öffentlich, da auch auf der Anmeldeseite sichtbar."""
    from flask import send_from_directory
    from werkzeug.utils import secure_filename

    from ..branding import brand_dir
    name = secure_filename(filename)
    if not name or not name.split("-", 1)[0] in ("logo", "bg", "favicon"):
        abort(404)
    resp = send_from_directory(brand_dir(), name, max_age=60 * 60 * 24 * 365)
    if name.endswith(".svg"):
        # SVG darf nie Skripte ausführen – auch nicht, wenn es direkt aufgerufen wird
        resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
        resp.headers["Content-Type"] = "image/svg+xml"
    return resp


# ---------------------------------------------------------------- Über / Lizenz (AGPL § 13)

def _license_file():
    from pathlib import Path
    from flask import current_app
    for p in (Path(current_app.root_path).parent / "LICENSE", Path(current_app.root_path) / "LICENSE"):
        if p.is_file():
            return p
    return None


@bp.route("/sprache/<code>", methods=["POST"])
def set_language(code):
    """Sprache wechseln – angemeldet dauerhaft im Profil, sonst für diese Sitzung."""
    from ..i18n import normalize
    lang = normalize(code)
    if lang is None:
        abort(404)
    session["lang"] = lang
    from flask import g
    g.pop("_assetlotse_locale", None)   # Meldungen dieser Anfrage schon in der neuen Sprache
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
