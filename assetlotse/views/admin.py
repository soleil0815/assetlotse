"""Administration: Benutzer, Gruppen, Einstellungen, Active Directory."""
from __future__ import annotations

import logging
import os

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from sqlalchemy import func, or_

from ..i18n import _, _n
from ..ldap_auth import Directory, LDAPError, group_cn, parse_rules
from ..models import (MIN_PASSWORD_LENGTH, DEFAULT_SETTINGS, ROLE_ADMIN, ROLE_MANAGER, ROLES, Asset, Group, Loan, User, db, get_bool,
                      get_setting, set_setting)
from . import admin_required, form_bool, form_int, form_str, safe_next

bp = Blueprint("admin", __name__, url_prefix="/admin")


# Änderungen, mit denen sich jemand Zugang verschaffen oder Schutz abschalten könnte
_SECURITY_ENDPOINTS = {"admin.user_new", "admin.user_edit", "admin.user_delete", "admin.user_reset_2fa",
                       "admin.user_unlock", "admin.settings", "admin.ldap"}


@bp.before_request
def _require_second_factor_for_changes():
    """Eine Sitzung, die im internen Netz ohne zweiten Faktor begonnen wurde, darf sicherheitsrelevante
    Einstellungen (Benutzer, Rollen, 2FA, Netze, AD) erst nach Bestätigung des zweiten Faktors ändern.

    Sonst könnte jemand mit dem Admin-Passwort im LAN z. B. die eigene 2FA zurücksetzen, einen neuen
    Faktor anlegen und sich damit von außen anmelden. Erscheinungsbild, Gruppen, E-Mail und Datensicherung
    bleiben ohne Rückfrage änderbar."""
    import time
    from flask import session
    if (request.method == "POST" and request.endpoint in _SECURITY_ENDPOINTS and current_user.is_authenticated
            and session.get("mfa_skipped") and current_user.has_2fa and not session.get("mfa_ok")):
        flash(_("Bitte bestätige zuerst deinen zweiten Faktor – danach die Änderung noch einmal speichern."), "warning")
        back = (request.path if request.endpoint in ("admin.user_new", "admin.user_edit", "admin.settings", "admin.ldap")
                else url_for("admin.users"))
        session["mfa_pending"] = {"uid": current_user.id, "remember": False, "next": back, "ts": time.time()}
        return redirect(url_for("security.challenge"))
    return None
log = logging.getLogger(__name__)

LDAP_TEXT = ["ldap_server", "ldap_security", "ldap_ca_file", "ldap_bind_dn", "ldap_upn_suffix",
             "ldap_base_dn", "ldap_user_attr", "ldap_user_filter", "ldap_timeout"]
LDAP_MULTI = ["ldap_login_rules", "ldap_manager_rules", "ldap_admin_rules"]
LDAP_BOOL = ["ldap_enabled", "ldap_verify_cert", "ldap_check_hostname", "ldap_nested_groups", "ldap_group_sync",
             "ldap_cache_enabled"]


def _id(value) -> int | None:
    """Datenbank-ID aus einem Formularwert – nur ASCII-Ziffern (kein „²“), nicht zu groß für SQLite."""
    value = (value or "").strip()
    if value.isascii() and value.isdecimal() and len(value) <= 18:
        return int(value)
    return None


def _ids(values) -> set[int]:
    return {i for i in map(_id, values) if i is not None}


def _admin_count(exclude_id: int | None = None) -> int:
    q = User.query.filter_by(role=ROLE_ADMIN, active=True)
    if exclude_id:
        q = q.filter(User.id != exclude_id)
    return q.count()


# ---------------------------------------------------------------- Benutzer

@bp.route("/benutzer")
@admin_required
def users():
    q = (request.args.get("q") or "").strip()
    role = request.args.get("rolle", "")
    query = User.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(User.username.ilike(like), User.display_name.ilike(like),
                                 User.email.ilike(like)))
    if role in ROLES:
        query = query.filter(User.role == role)
    status = request.args.get("status", "")
    if status == "neu":
        query = query.filter(User.pending_approval.is_(True))
    elif status == "registriert":
        query = query.filter(User.self_registered.is_(True))
    elif status == "gesperrt":
        query = query.filter(User.active.is_(False), User.pending_approval.is_(False), User.email_verified.is_(True))
    from .. import lockout
    locked = lockout.locked_usernames()
    from sqlalchemy.orm import selectinload
    users = (query.options(selectinload(User.groups), selectinload(User.webauthn_credentials))
             .order_by(User.pending_approval.desc(), User.username).all())
    # Sperren werden unter dem vereinheitlichten Namen geführt („DOMÄNE\anna“ = „anna“)
    locked = {u.username.lower() for u in users if lockout.norm(u.username) in locked}
    if status == "anmeldesperre":
        users = [u for u in users if u.username.lower() in locked]
    active_counts = dict(db.session.query(Loan.user_id, func.count(Loan.id))
                         .filter(Loan.returned_at.is_(None)).group_by(Loan.user_id).all())
    return render_template("admin/users.html", users=users, q=q, role=role, status=status,
                           active_counts=active_counts, locked=locked)


def _save_user(user: User, is_new: bool):
    username = form_str("username", 120)
    if not username:
        return _("Benutzername fehlt.")
    clash = User.query.filter(func.lower(User.username) == username.lower(), User.id != (user.id or 0)).first()
    if clash:
        return _("Dieser Benutzername ist bereits vergeben.")
    local = is_new or user.auth_source == "local"
    role = request.form.get("role", "reader") if local else user.role  # AD-Rolle kommt aus den Regeln
    if role not in ROLES:
        return _("Unbekannte Rolle.")
    active = form_bool("active")
    if user.id == current_user.id and (role != ROLE_ADMIN or not active):
        return _("Du kannst dir selbst die Administratorrechte nicht entziehen.")
    if user.role == ROLE_ADMIN and (role != ROLE_ADMIN or not active) and _admin_count(user.id) == 0:
        return _("Es muss mindestens ein aktiver Administrator bestehen bleiben.")
    password = request.form.get("password") or ""
    if user.auth_source == "local" or is_new:
        if is_new and len(password) < MIN_PASSWORD_LENGTH:
            return _("Bitte ein Passwort mit mindestens {n} Zeichen vergeben.", n=MIN_PASSWORD_LENGTH)
        if password and len(password) < MIN_PASSWORD_LENGTH:
            return _("Das Passwort muss mindestens {n} Zeichen haben.", n=MIN_PASSWORD_LENGTH)
    if user.username != username:
        if not is_new and user.auth_source == "ldap":
            log.info("AD-Konto %s von %s umbenannt in %s", user.username, current_user.username, username)
        user.username = username      # auch AD-Konten: z. B. altes Konto beiseiteschieben, wenn der Name neu vergeben ist
    from ..mailer import valid_email
    email = form_str("email", 200)
    if email and not valid_email(email):
        return _("Bitte eine gültige E-Mail-Adresse eingeben (nur eine Adresse).")
    user.display_name = form_str("display_name", 200)
    user.email = email
    if user.auth_source == "local" or is_new:
        user.role = role
    if bool(user.active) != active:
        user.deactivated_by_sync = False     # von Hand geändert: der AD-Abgleich schaltet nicht zurück
    user.active = active
    if active and user.pending_approval:   # „Konto aktiv“ angehakt = freigegeben
        user.pending_approval = False
    if active and not user.email_verified:  # Admin bestätigt die Adresse stellvertretend
        user.email_verified = True
    user.email_notifications = form_bool("email_notifications")
    if password and (user.auth_source == "local" or is_new):
        user.auth_source = "local"
        user.set_password(password)
    ids = _ids(request.form.getlist("groups"))
    had_groups = bool(user.groups)
    user.groups = Group.query.filter(Group.id.in_(ids)).all() if ids else []
    if user.role == ROLE_MANAGER and had_groups and not user.groups:
        flash(_("Hinweis: Ein Verwalter ohne Gruppe verwaltet den gesamten Bestand."), "warning")
    return None


@bp.route("/benutzer/neu", methods=["GET", "POST"])
@admin_required
def user_new():
    user = User(active=True, role="reader", auth_source="local")
    if request.method == "POST":
        err = _save_user(user, True)
        if err:
            flash(err, "error")
        else:
            db.session.add(user)
            db.session.commit()
            flash(_("Benutzer {name} angelegt.", name=user.username), "success")
            return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", user=user, groups=Group.query.order_by(Group.name).all(),
                           is_new=True)


@bp.route("/benutzer/<int:user_id>", methods=["GET", "POST"])
@admin_required
def user_edit(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if request.method == "POST":
        err = _save_user(user, False)
        if err:
            flash(err, "error")
            db.session.rollback()
        else:
            db.session.commit()
            flash(_("Benutzer gespeichert."), "success")
            return redirect(url_for("admin.users"))
    from .. import lockout, twofa
    return render_template("admin/user_form.html", user=user, groups=Group.query.order_by(Group.name).all(),
                           is_new=False, locked=lockout.norm(user.username) in lockout.locked_usernames(),
                           totp_broken=twofa.totp_unreadable(user), required=twofa.required_for(user))


@bp.route("/benutzer/<int:user_id>/freigeben", methods=["POST"])
@admin_required
def user_approve(user_id):
    from .register import maybe_activate, send_approved
    user = db.session.get(User, user_id) or abort(404)
    if user.pending_approval:
        user.pending_approval = False
        maybe_activate(user)
        db.session.commit()
        if user.active:
            send_approved(user)
        flash(_("{name} wurde freigegeben.", name=user.name) + ("" if user.active else
              " " + _("Das Konto wird aktiv, sobald die E-Mail-Adresse bestätigt ist.")), "success")
    return redirect(safe_next(request.form.get("next")) or url_for("admin.users"))


@bp.route("/benutzer/<int:user_id>/ablehnen", methods=["POST"])
@admin_required
def user_reject(user_id):
    user = db.session.get(User, user_id) or abort(404)
    # Nur wartende bzw. unbestätigte Selbstregistrierungen – nie das eigene Konto oder einen Administrator
    if (not user.self_registered or not (user.pending_approval or not user.email_verified)
            or user.id == current_user.id or user.role == ROLE_ADMIN):
        flash(_("Nur wartende Registrierungen können abgelehnt werden."), "error")
    elif user.active_loans:
        flash(_("Der Person sind noch Gegenstände ausgegeben. Sperre das Konto stattdessen."), "error")
    else:
        from ..retention import OpenLoansError, delete_user
        name = user.name
        try:
            delete_user(user)
            db.session.commit()
            flash(_("Registrierung von {name} abgelehnt und gelöscht.", name=name), "success")
        except OpenLoansError:
            db.session.rollback()
            flash(_("Der Person sind noch Gegenstände ausgegeben. Sperre das Konto stattdessen."), "error")
    return redirect(url_for("admin.users", status="neu"))


@bp.route("/benutzer/<int:user_id>/2fa-zuruecksetzen", methods=["POST"])
@admin_required
def user_reset_2fa(user_id):
    from .. import lockout
    from ..twofa import reset_2fa, required_for
    user = db.session.get(User, user_id) or abort(404)
    reset_2fa(user)
    lockout.unlock_user(user.username)
    log.info("2FA von %s zurückgesetzt durch %s", user.username, current_user.username)
    hint = " " + _("Bei der nächsten Anmeldung wird die Einrichtung verlangt.") if required_for(user) else ""
    flash(_("Zwei-Faktor-Anmeldung von {name} zurückgesetzt – Anmeldung jetzt nur mit Passwort.", name=user.username) + hint,
          "success")
    return redirect(safe_next(request.form.get("next")) or url_for("admin.user_edit", user_id=user.id))


@bp.route("/benutzer/<int:user_id>/entsperren", methods=["POST"])
@admin_required
def user_unlock(user_id):
    from .. import lockout
    user = db.session.get(User, user_id) or abort(404)
    lockout.unlock_user(user.username)
    log.info("Anmeldesperre von %s aufgehoben durch %s", user.username, current_user.username)
    flash(_("Anmeldesperre von {name} aufgehoben.", name=user.username), "success")
    return redirect(safe_next(request.form.get("next")) or url_for("admin.user_edit", user_id=user.id))


@bp.route("/benutzer/<int:user_id>/loeschen", methods=["POST"])
@admin_required
def user_delete(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if user.id == current_user.id:
        flash(_("Du kannst dein eigenes Konto nicht löschen."), "error")
    elif user.active_loans:
        flash(_("Der Person sind noch Gegenstände ausgegeben. Sperre das Konto stattdessen."), "error")
    elif user.role == ROLE_ADMIN and _admin_count(user.id) == 0:
        flash(_("Der letzte Administrator kann nicht gelöscht werden."), "error")
    else:
        from ..retention import OpenLoansError, delete_user
        name = user.username
        try:
            delete_user(user)   # abgeschlossene Ausleihen nur noch gezählt – Statistik bleibt erhalten
            db.session.commit()
            flash(_("Benutzer {name} gelöscht.", name=name), "success")
        except OpenLoansError:      # gerade eben doch noch etwas ausgegeben
            db.session.rollback()
            flash(_("Der Person sind noch Gegenstände ausgegeben. Sperre das Konto stattdessen."), "error")
    return redirect(url_for("admin.users"))


@bp.route("/aufbewahrung/jetzt", methods=["POST"])
@admin_required
def retention_run():
    from ..retention import format_result, run_retention
    from datetime import datetime
    res = run_retention()
    set_setting("retention_last_result", f"{datetime.now():%d.%m.%Y %H:%M}: {format_result(res)}")
    db.session.commit()
    if res.get("off"):
        flash(_("Aufbewahrungsfrist ist ausgeschaltet."), "success")
    else:
        flash(_("Gelöscht: {loans} alte Ausleihe(n), {mails} Einträge im E-Mail-Protokoll, {secrets} im Zugriffsprotokoll.",
                loans=res["loans"], mails=res["mails"], secrets=res.get("secret_log", 0)), "success")
    return redirect(url_for("admin.settings") + "#aufbewahrung")


# ---------------------------------------------------------------- Datensicherung

@bp.route("/datensicherung", methods=["GET", "POST"])
@admin_required
def backups():
    from .. import backup
    if request.method == "POST":
        set_setting("backup_enabled", form_bool("backup_enabled"))
        set_setting("backup_keep", min(backup.KEEP_MAX, max(backup.KEEP_MIN, form_int("backup_keep", 10, 1))))
        hour = form_int("backup_hour", 2, 0)
        set_setting("backup_hour", hour if 0 <= hour <= 23 else 2)
        db.session.commit()
        removed = backup.rotate() if backup.supported() else 0
        flash(_("Einstellungen gespeichert.") + (" " + _n("{n} ältere Sicherung entfernt.", "{n} ältere Sicherungen entfernt.",
                                                          removed) if removed else ""), "success")
        return redirect(url_for("admin.backups"))
    supported = backup.supported()
    items = backup.list_backups() if supported else []
    return render_template("admin/backups.html", supported=supported, items=items,
                           folder=str(backup.backup_dir()) if supported else "",
                           custom_dir=bool(os.environ.get("ASSETLOTSE_BACKUP_DIR")),
                           keep=backup.keep_count(), s=get_setting, warning=backup.warning(),
                           last_result=_stamped(get_setting("backup_last_result"), _backup_message),
                           total=sum(i["size"] for i in items))


def _stamped(value: str, translate) -> str:
    """Gespeichertes Laufergebnis „TT.MM.JJJJ HH:MM: Text“ in der Sprache der Oberfläche anzeigen."""
    import re
    from datetime import datetime

    from ..i18n import format_datetime
    m = re.match(r"^(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}): (.*)$", value or "", re.S)
    if not m:
        return value
    try:
        when = format_datetime(datetime.strptime(m.group(1), "%d.%m.%Y %H:%M"))
    except ValueError:
        when = m.group(1)
    return f"{when}: {translate(m.group(2))}"


def _retention_message(msg: str) -> str:
    import re
    m = re.match(r"^Gelöscht: (\d+) alte Ausleihe\(n\), (\d+) Einträge im E-Mail-Protokoll(?:, (\d+) im Zugriffsprotokoll)?$", msg)
    if m:
        return _("Gelöscht: {loans} alte Ausleihe(n), {mails} Einträge im E-Mail-Protokoll, {secrets} im Zugriffsprotokoll",
                 loans=m.group(1), mails=m.group(2), secrets=m.group(3) or "0")
    return _(msg)


def _backup_message(msg: str) -> str:
    """Ergebnis von backup.run_now (deutsch, so auch gespeichert) in der Sprache der Oberfläche."""
    from ..backup import localized_message
    return localized_message(msg)


@bp.route("/datensicherung/jetzt", methods=["POST"])
@admin_required
def backup_now():
    from ..backup import run_now
    ok, msg = run_now("-manuell")
    flash(_backup_message(msg), "success" if ok else "error")
    return redirect(url_for("admin.backups"))


@bp.route("/datensicherung/<name>")
@admin_required
def backup_download(name):
    from ..backup import find_backup
    path = find_backup(name)
    if path is None:
        abort(404)
    log.info("Sicherung %s heruntergeladen von %s", name, current_user.username)
    resp = send_file(path, as_attachment=True, download_name=name, mimetype="application/vnd.sqlite3", max_age=0)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.route("/datensicherung/<name>/loeschen", methods=["POST"])
@admin_required
def backup_delete(name):
    from ..backup import delete_backup
    if delete_backup(name):
        flash(_("Sicherung {name} gelöscht.", name=name), "success")
    else:
        abort(404)
    return redirect(url_for("admin.backups"))


# ---------------------------------------------------------------- Gruppen

@bp.route("/gruppen")
@admin_required
def groups():
    groups = Group.query.order_by(Group.name).all()
    asset_counts = dict(db.session.query(Asset.group_id, func.count(Asset.id)).group_by(Asset.group_id).all())
    return render_template("admin/groups.html", groups=groups, asset_counts=asset_counts,
                           general_assets=asset_counts.get(None, 0))


def _save_group(group: Group):
    name = form_str("name", 120)
    if not name:
        return _("Name fehlt.")
    # In Python vergleichen: SQLite-lower() kennt keine Umlaute („Ärzte“ / „ärzte“)
    clash = any(g.name.casefold() == name.casefold() for g in Group.query.filter(Group.id != (group.id or 0)))
    if clash:
        return _("Eine Gruppe mit diesem Namen existiert bereits.")
    group.name = name
    group.description = form_str("description", 500)
    group.ldap_dn = form_str("ldap_dn", 500)
    from ..branding import clean_hex
    group.color = clean_hex(form_str("color", 16)) or "#0b7285"
    ids = _ids(request.form.getlist("members"))
    group.users = User.query.filter(User.id.in_(ids)).all() if ids else []
    return None


@bp.route("/gruppen/neu", methods=["GET", "POST"])
@admin_required
def group_new():
    group = Group(color="#0b7285")
    if request.method == "POST":
        err = _save_group(group)
        if err:
            flash(err, "error")
        else:
            db.session.add(group)
            db.session.commit()
            flash(_("Gruppe {name} angelegt.", name=group.name), "success")
            return redirect(url_for("admin.groups"))
    return render_template("admin/group_form.html", group=group,
                           users=User.query.order_by(User.username).all(), is_new=True)


@bp.route("/gruppen/<int:group_id>", methods=["GET", "POST"])
@admin_required
def group_edit(group_id):
    group = db.session.get(Group, group_id) or abort(404)
    if request.method == "POST":
        err = _save_group(group)
        if err:
            flash(err, "error")
            db.session.rollback()
        else:
            db.session.commit()
            flash(_("Gruppe gespeichert."), "success")
            return redirect(url_for("admin.groups"))
    return render_template("admin/group_form.html", group=group,
                           users=User.query.order_by(User.username).all(), is_new=False)


@bp.route("/gruppen/<int:group_id>/loeschen", methods=["POST"])
@admin_required
def group_delete(group_id):
    group = db.session.get(Group, group_id) or abort(404)
    # Ein Verwalter ohne Gruppe verwaltet den ganzen Bestand – das darf nicht nebenbei durch Löschen passieren
    orphaned = [u.name for u in group.users if u.role == ROLE_MANAGER and len(u.groups) == 1]
    if orphaned:
        flash(_("Gruppe {name} nicht gelöscht: {people} hätte(n) danach keine Gruppe mehr und würde(n) damit den "
                "gesamten Bestand verwalten. Bitte zuerst eine andere Gruppe zuweisen.",
                name=group.name, people=", ".join(orphaned)), "error")
        return redirect(url_for("admin.groups"))
    n = len(group.assets)
    for b in group.assets:
        b.group_id = None
    db.session.delete(group)
    db.session.commit()
    flash(_n("Gruppe {name} gelöscht. {n} Gegenstand ist jetzt allgemein verfügbar.",
             "Gruppe {name} gelöscht. {n} Gegenstände sind jetzt allgemein verfügbar.", n, name=group.name), "success")
    return redirect(url_for("admin.groups"))


# ---------------------------------------------------------------- Einstellungen

@bp.route("/einstellungen", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        set_setting("org_name", form_str("org_name", 120) or "AssetLotse")
        from ..i18n import normalize
        if "default_language" in request.form:
            set_setting("default_language", normalize(request.form.get("default_language")) or "de")
        set_setting("loan_days", min(form_int("loan_days", 0, 0), 3650))
        set_setting("extension_days", min(form_int("extension_days", 14, 1), 3650))
        set_setting("max_extensions", min(form_int("max_extensions", 2, 0), 100))
        set_setting("max_loans", min(form_int("max_loans", 0, 0), 1000))
        for key in ("self_checkout", "self_return", "self_extend"):
            set_setting(key, form_bool(key))
        reg = request.form.get("registration_mode")
        set_setting("registration_mode", reg if reg in ("off", "approval", "open") else "off")
        if get_setting("registration_mode") != "off" and not get_setting("app_base_url"):
            # Links in Registrierungs-Mails nie aus der Anfrage eines Fremden bauen: die Adresse, unter der
            # der Admin die Seite gerade aufruft, fest übernehmen (änderbar unter E-Mail)
            set_setting("app_base_url", request.host_url.rstrip("/"))
            flash(_("Adresse für Links in E-Mails auf {url} gesetzt. Bitte unter E-Mail prüfen, ob AssetLotse von außen so erreichbar ist.",
                    url=request.host_url.rstrip("/")), "info")
        set_setting("registration_verify_email", form_bool("registration_verify_email"))
        set_setting("registration_notify_admins", form_bool("registration_notify_admins"))
        set_setting("registration_domains", form_str("registration_domains", 500))
        gid = _id(request.form.get("registration_group_id"))
        set_setting("registration_group_id", str(gid) if gid is not None and db.session.get(Group, gid) else "")
        for key in ("imprint_url", "privacy_url"):
            url = form_str(key, 500)
            set_setting(key, url if url.startswith(("https://", "http://", "/")) else "")
        months = request.form.get("retention_months", "12")
        set_setting("retention_months", months if months in ("0", "3", "6", "12", "24", "36") else "12")
        pol = request.form.get("mfa_policy")
        set_setting("mfa_policy", pol if pol in ("optional", "admins", "managers", "all") else "optional")
        mode = request.form.get("mfa_internal")
        set_setting("mfa_internal", mode if mode in ("same", "optional", "skip") else "same")
        if "internal_networks" in request.form:
            from ..netzone import parse
            text = (request.form.get("internal_networks") or "").strip()[:2000]
            nets, bad = parse(text)
            if bad:
                flash(_("Internes Netz: ungültige Einträge {entries} – Netze nicht geändert.", entries=", ".join(bad[:5])), "error")
            else:
                set_setting("internal_networks", "\n".join(str(n) for n in nets))
        if "own_public_hosts" in request.form:
            from ..netzone import parse_own
            entries, bad = parse_own((request.form.get("own_public_hosts") or "")[:1000])
            if bad:
                flash(_("Eigener Anschluss: ungültige Einträge {entries} – nicht geändert.", entries=", ".join(bad[:5])), "error")
            else:
                set_setting("own_public_hosts", "\n".join(entries))
        vis = request.form.get("catalog_visibility")
        set_setting("catalog_visibility", vis if vis in ("own", "groups", "all") else "own")
        if get_setting("catalog_visibility") == "own" and get_bool("self_checkout"):
            flash(_("Hinweis: Die Selbstausleihe wirkt nur, wenn Mitarbeiter den Bestand sehen – bei „nur eigene "
                    "Gegenstände“ gibt es für sie keinen Ausleih-Knopf."), "warning")
        db.session.commit()
        flash(_("Einstellungen gespeichert."), "success")
        return redirect(url_for("admin.settings"))
    from ..mailer import mail_active
    from .. import netzone
    return render_template("admin/settings.html", s=get_setting,
                           retention_result=_stamped(get_setting("retention_last_result"), _retention_message),
                           connection=netzone.describe(), default_networks=netzone.DEFAULT_INTERNAL,
                           groups=Group.query.order_by(Group.name).all(), mail_on=mail_active())


# ---------------------------------------------------------------- Active Directory / LDAP

def _would_lock_out_self(cfg: dict) -> str | None:
    """Wer mit einem AD-Konto angemeldet ist, darf keine Einstellung speichern, nach der er selbst kein
    Administrator mehr wäre (z. B. vertippte Admin-Regel) – sonst sperrt er sich und ggf. alle anderen aus."""
    if current_user.auth_source != "ldap":
        return None
    if cfg.get("ldap_enabled") != "1":
        return _("Du bist mit einem AD-Konto angemeldet – das Active Directory abschalten kann nur der lokale "
                 "Administrator.")
    try:
        info = Directory(cfg).check_account(current_user.username)
    except LDAPError as exc:
        return _("Dein eigenes AD-Konto ließ sich mit den neuen Einstellungen nicht prüfen ({error}) – nicht "
                 "gespeichert. Bitte die Verbindung prüfen oder mit dem lokalen Administrator speichern.", error=exc)
    except Exception as exc:  # noqa: BLE001
        return _("Dein eigenes AD-Konto ließ sich mit den neuen Einstellungen nicht prüfen ({error}) – nicht "
                 "gespeichert. Bitte die Verbindung prüfen oder mit dem lokalen Administrator speichern.",
                 error=exc.__class__.__name__)
    if info is None or info.role != ROLE_ADMIN:
        return _("Mit diesen Einstellungen wärst du kein Administrator mehr (Rolle: {role}) – nicht gespeichert. "
                 "Bitte die Regeln prüfen.", role=str(ROLES.get(info.role, _("keine"))) if info else _("keine"))
    return None


def _ldap_form_cfg() -> dict:
    cfg = {k: form_str(k, 500) for k in LDAP_TEXT}
    for k in LDAP_MULTI:
        cfg[k] = "\n".join(parse_rules(request.form.get(k, "")))
    for k in LDAP_BOOL:
        cfg[k] = "1" if form_bool(k) else "0"
    pw = request.form.get("ldap_bind_password") or ""
    cfg["ldap_bind_password"] = pw if pw else get_setting("ldap_bind_password")
    if request.form.get("clear_bind_password"):
        cfg["ldap_bind_password"] = ""
    from ..ad_cache import HOURS_CHOICES
    hours = request.form.get("ldap_cache_hours", "")
    cfg["ldap_cache_hours"] = hours if hours.isdigit() and int(hours) in HOURS_CHOICES else "1"
    return cfg


@bp.route("/verzeichnisdienst", methods=["GET", "POST"])
@admin_required
def ldap():
    from .. import ldap_tls

    test_result, test_error, cfg, fetched, ad_groups = None, None, None, None, None
    if request.method == "POST":
        cfg = _ldap_form_cfg()
        action = request.form.get("action", "save")
        if action == "save" and (problem := _would_lock_out_self(cfg)):
            flash(problem, "error")
            action = "keep_form"      # Formular mit den Eingaben wieder anzeigen, nichts speichern
        if action == "save":
            from .. import ad_cache
            relevant = ("ldap_enabled", "ldap_server", "ldap_base_dn", "ldap_user_attr", "ldap_user_filter",
                        "ldap_bind_dn", "ldap_nested_groups", "ldap_cache_enabled",
                        "ldap_login_rules", "ldap_manager_rules", "ldap_admin_rules")
            changed = any(get_setting(k) != cfg.get(k, get_setting(k)) for k in relevant)
            for k, v in cfg.items():
                set_setting(k, v)
            db.session.commit()
            flash(_("Verzeichnisdienst-Einstellungen gespeichert."), "success")
            if not ad_cache.enabled():
                if ad_cache.clear():
                    flash(_("Zwischenspeicher der AD-Personen geleert."), "info")
            elif changed or not ad_cache.ready():
                # Regeln/Verbindung geändert: gleich neu abgleichen, damit die Theke den neuen Stand kennt
                ad_cache.sync_in_background(current_app._get_current_object())
            return redirect(url_for("admin.ldap"))
        if action == "trust_cert":
            try:
                certs = ldap_tls.load_pem_bundle(request.form.get("pending_pem", ""))
            except ValueError:
                certs = []
            if not certs:
                flash(_("Keine gültigen Zertifikate zum Übernehmen."), "error")
            elif (problem := _would_lock_out_self(cfg)):
                flash(problem, "error")       # gleiche Schutzprüfung wie beim Speichern
            else:
                for k, v in cfg.items():      # Ersteinrichtung: Server usw. gleich mit speichern
                    set_setting(k, v)
                set_setting("ldap_tls_certs", ldap_tls.to_pem(certs))
                set_setting("ldap_verify_cert", "1")
                db.session.commit()
                flash(_n("{n} Zertifikat gespeichert. Verbindungen zum DC werden jetzt damit geprüft.",
                         "{n} Zertifikate gespeichert. Verbindungen zum DC werden jetzt damit geprüft.", len(certs)), "success")
                return redirect(url_for("admin.ldap"))
        elif action == "clear_cert":
            set_setting("ldap_tls_certs", "")
            db.session.commit()
            flash(_("Gespeicherte DC-Zertifikate entfernt."), "success")
            return redirect(url_for("admin.ldap"))
        elif action == "fetch_cert":
            try:
                try:
                    directory = Directory(cfg)
                except LDAPError:
                    directory = None
                servers = [x for x in cfg.get("ldap_server", "").replace(",", " ").split() if x]
                if not servers:
                    raise ldap_tls.CertFetchError(_("Bitte zuerst einen Server eintragen."))
                timeout = int(cfg.get("ldap_timeout") or 8)
                res = ldap_tls.fetch_trust_anchors(directory, servers, cfg.get("ldap_security", "none"), timeout)
                trust_fps = {c.fingerprint(ldap_tls.hashes.SHA256()) for c in res.trust}
                fetched = {
                    "pem": ldap_tls.to_pem(res.trust),
                    "certs": [(ldap_tls.describe(c), c.fingerprint(ldap_tls.hashes.SHA256()) in trust_fps)
                              for c in res.chain],
                    "notes": res.notes,
                    "hosts": res.hostname_ok,
                }
            except ldap_tls.CertFetchError as exc:
                test_error = str(exc)
            except Exception as exc:  # noqa: BLE001
                test_error = _("Zertifikat konnte nicht abgerufen werden: {error}", error=f"{exc.__class__.__name__}: {exc}")
        elif action == "keep_form":
            pass                      # nach abgelehntem Speichern: nur die Eingaben wieder zeigen
        else:
            try:
                directory = Directory(cfg)
                if action == "test_bind":
                    test_result = {"message": directory.test_connection()}
                elif action == "list_groups":
                    ad_groups = directory.list_groups(form_str("group_query", 100))
                elif action == "test_user":
                    info = directory.authenticate(form_str("test_user", 200), request.form.get("test_password") or "")
                    test_result = {"user": info}
            except LDAPError as exc:
                test_error = str(exc)
            except Exception as exc:  # noqa: BLE001 – Admin soll den Fehler sehen
                test_error = f"{exc.__class__.__name__}: {exc}"
    values = {k: get_setting(k) for k in DEFAULT_SETTINGS if k.startswith("ldap_")}
    if cfg:
        values.update(cfg)
    has_pw = bool(get_setting("ldap_bind_password"))
    values["ldap_bind_password"] = ""
    try:
        stored = [ldap_tls.describe(c) for c in ldap_tls.load_pem_bundle(get_setting("ldap_tls_certs"))]
    except ValueError:
        stored = []
    ldap_groups = Group.query.filter(Group.ldap_dn != "").order_by(Group.name).all()
    from .. import ad_cache
    return render_template("admin/ldap.html", v=values, has_pw=has_pw, test_result=test_result,
                           cache=ad_cache.status(), cache_hours=ad_cache.HOURS_CHOICES,
                           test_error=test_error, ROLES=ROLES, ldap_groups=ldap_groups,
                           stored_certs=stored, fetched=fetched, ad_groups=ad_groups,
                           group_query=request.form.get("group_query", ""), group_cn=group_cn)


@bp.route("/verzeichnisdienst/abgleichen", methods=["POST"])
@admin_required
def ldap_cache_sync():
    """AD-Personen jetzt abgleichen (läuft im Hintergrund)."""
    from .. import ad_cache
    if not ad_cache.enabled():
        flash(_("Der Zwischenspeicher ist ausgeschaltet oder es fehlt das Dienstkonto."), "error")
    else:
        ad_cache.sync_in_background(current_app._get_current_object())
        flash(_("Abgleich gestartet – das Ergebnis steht in wenigen Sekunden hier (Seite neu laden)."), "info")
    return redirect(url_for("admin.ldap") + "#ad-cache")


@bp.route("/verzeichnisdienst/deaktivieren", methods=["POST"])
@admin_required
def ldap_cache_confirm():
    """Die beim Abgleich zurückgehaltenen Konten (nicht mehr im AD) jetzt deaktivieren."""
    from .. import ad_cache
    n = ad_cache.confirm_pending(current_user)
    flash(_n("{n} Konto deaktiviert.", "{n} Konten deaktiviert.", n), "success")
    return redirect(url_for("admin.ldap") + "#ad-cache")


# ---------------------------------------------------------------- E-Mail

MAIL_TEXT = ["smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_from", "smtp_from_name",
             "smtp_reply_to", "app_base_url", "mail_reminder_days", "mail_overdue_repeat_days",
             "mail_overdue_max", "mail_hour"]
MAIL_BOOL = ["mail_enabled", "smtp_verify_cert", "mail_on_loan", "mail_on_return", "mail_reminder",
             "mail_overdue", "mail_digest"]


def _mail_form_cfg() -> dict:
    cfg = {k: form_str(k, 300) for k in MAIL_TEXT}
    if cfg["smtp_security"] not in ("none", "starttls", "ssl"):
        cfg["smtp_security"] = "starttls"
    for k in ("smtp_port", "mail_reminder_days", "mail_overdue_repeat_days", "mail_overdue_max", "mail_hour"):
        if _id(cfg[k]) is None:
            cfg[k] = DEFAULT_SETTINGS[k]
    limits = {"smtp_port": (1, 65535), "mail_reminder_days": (0, 60), "mail_overdue_repeat_days": (1, 365),
              "mail_overdue_max": (0, 50), "mail_hour": (0, 23)}
    for k, (lo, hi) in limits.items():
        cfg[k] = str(min(hi, max(lo, int(cfg[k]))))
    cfg["app_base_url"] = cfg["app_base_url"].rstrip("/")
    if cfg["app_base_url"] and not cfg["app_base_url"].lower().startswith(("https://", "http://")):
        cfg["app_base_url"] = ""   # nur echte Web-Adressen (Links in E-Mails)
    for k in MAIL_BOOL:
        cfg[k] = "1" if form_bool(k) else "0"
    cfg["mail_footer"] = (request.form.get("mail_footer") or "").strip()[:1000]
    pw = request.form.get("smtp_password") or ""
    cfg["smtp_password"] = "" if request.form.get("clear_smtp_password") else (pw or get_setting("smtp_password"))
    return cfg


@bp.route("/email", methods=["GET", "POST"])
@admin_required
def email():
    from datetime import datetime

    from .. import mailer
    from ..i18n import format_datetime
    from ..models import MailLog

    cfg = None
    if request.method == "POST":
        cfg = _mail_form_cfg()
        action = request.form.get("action", "save")
        if action == "save":
            for k, v in cfg.items():
                set_setting(k, v)
            db.session.commit()
            flash(_("E-Mail-Einstellungen gespeichert."), "success")
            return redirect(url_for("admin.email"))
        if action == "test":
            to = form_str("test_to", 300) or current_user.email
            if not to or "@" not in to:
                flash(_("Bitte eine Empfängeradresse für die Testmail angeben."), "error")
            else:
                smtp_cfg = mailer.SmtpConfig.from_settings(cfg)
                subject, text, html = mailer.render_mail(
                    "test", server=f"{smtp_cfg.host}:{smtp_cfg.port} ({smtp_cfg.security})",
                    now=format_datetime(datetime.now()))
                ok, err = mailer.send_mail(to, subject, text, html, kind="test", cfg=smtp_cfg,
                                           user_id=current_user.id)
                if ok:
                    flash(_("Testmail an {to} gesendet. Schau ins Postfach (ggf. auch in den Spam-Ordner).", to=to), "success")
                else:
                    flash(_("Testmail fehlgeschlagen: {error}", error=err), "error")
        elif action == "run_now":
            for k, v in cfg.items():
                set_setting(k, v)
            db.session.commit()
            stats = mailer.run_notifications()
            set_setting("mail_last_result", mailer.stats_record(datetime.now(), stats, manual=True))
            db.session.commit()
            flash(_("Erinnerungslauf: {stats}", stats=mailer.format_stats(stats)), "error" if stats.get("error") else "success")
            return redirect(url_for("admin.email"))

    values = {k: get_setting(k) for k in DEFAULT_SETTINGS if k.startswith(("smtp_", "mail_")) or k == "app_base_url"}
    if cfg:
        values.update(cfg)
    has_pw = bool(get_setting("smtp_password"))
    values["smtp_password"] = ""
    logs = MailLog.query.order_by(MailLog.created_at.desc()).limit(60).all()
    pending = mailer.collect_due() if mailer.mail_active() else {}
    without_mail = User.query.filter(User.active.is_(True), (User.email == "") | (User.email.is_(None))).count()
    suggested_url = request.host_url.rstrip("/")
    values = dict(values, mail_last_result=mailer.last_result_text(values.get("mail_last_result", "")))
    return render_template("admin/email.html", v=values, has_pw=has_pw, logs=logs, pending=pending,
                           without_mail=without_mail, suggested_url=suggested_url)


# ---------------------------------------------------------------- Erscheinungsbild

@bp.route("/erscheinungsbild", methods=["GET", "POST"])
@admin_required
def branding():
    from .. import branding as br

    if request.method == "POST":
        if request.form.get("action") == "reset":
            br.reset_all()
            db.session.commit()
            flash(_("Erscheinungsbild auf Standard zurückgesetzt."), "success")
            return redirect(url_for("admin.branding"))
        errors = []
        for kind, field in (("logo", "logo"), ("bg", "bg")):
            if form_bool(f"remove_{kind}"):
                br.remove(kind)
                continue
            upload = request.files.get(field)
            if upload and upload.filename:
                try:
                    br.store(kind, upload)
                except br.BrandingError as exc:
                    errors.append(f"{_('Logo') if kind == 'logo' else _('Hintergrundbild')}: {exc}")
        for key in ("brand_accent", "brand_brass", "brand_side"):
            set_setting(key, br.clean_hex(form_str(key, 20)))
        set_setting("brand_logo_text", form_bool("brand_logo_text"))
        set_setting("brand_logo_plate", form_bool("brand_logo_plate"))
        set_setting("login_show_shelf", form_bool("login_show_shelf"))
        set_setting("brand_bg_dim", min(85, form_int("brand_bg_dim", 45, 0)))
        set_setting("login_eyebrow", form_str("login_eyebrow", 60))
        set_setting("login_text", form_str("login_text", 300))
        name = form_str("org_name", 120)
        if name:
            set_setting("org_name", name)
        db.session.commit()
        for e in errors:
            flash(e, "error")
        if not errors:
            flash(_("Erscheinungsbild gespeichert."), "success")
        return redirect(url_for("admin.branding"))
    return render_template("admin/branding.html", s=get_setting, colors=br.colors(), defaults=br.DEFAULTS)


@bp.route("/erscheinungsbild/vorschau")
@admin_required
def branding_preview():
    return render_template("login.html", preview=True)
