"""Ausgabe, Rückgabe, Verlängerung und Ausgabetheke."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import contains_eager

from ..i18n import _, format_day
from ..models import (ROLE_READER, Asset, Loan, User, count_active_loans, db, default_due_date, number_key, get_bool, get_int, get_setting,
                      catalog_hidden, manageable_assets_query)
from ..mailer import notify_loan, notify_return, send_manual_reminder
from . import get_by_id, manager_required, parse_id, safe_next

bp = Blueprint("loans", __name__)
log = logging.getLogger(__name__)
PER_PAGE = 50


def _back(default: str):
    return redirect(safe_next(request.form.get("next")) or default)


def lock_asset(asset_id: int) -> None:
    """Schreibsperre für diesen Gegenstand bis zum Ende der Transaktion holen – vor jeder Prüfung.

    Die leere Änderung (Wert auf sich selbst setzen) startet bei SQLite die Schreibtransaktion
    (RESERVED-Sperre: gleichzeitige Ausgaben warten aufeinander) und sperrt bei MySQL/PostgreSQL
    die Zeile des Gegenstands; SELECT … FOR UPDATE sichert das dort zusätzlich ab (SQLite ignoriert es)."""
    db.session.execute(update(Asset).where(Asset.id == asset_id).values(updated_at=Asset.updated_at)
                       .execution_options(synchronize_session=False))
    if db.session.get_bind().dialect.name != "sqlite":
        db.session.query(Asset.id).filter(Asset.id == asset_id).with_for_update().first()


DEFAULT_DUE = object()   # „Standard-Leihdauer verwenden“ (None heißt: unbefristet)


def lend_asset(asset: Asset, borrower: User, due=DEFAULT_DUE, note: str = "") -> tuple[bool, str]:
    """Zentrale Ausgabelogik inkl. aller Prüfungen."""
    if not borrower.active:
        return False, _("{name} ist gesperrt.", name=borrower.name)
    if not borrower.can_borrow(asset):
        grp = asset.group.name if asset.group else "–"
        return False, _("{name} gehört nicht zur Gruppe „{group}“ und darf diesen Gegenstand nicht bekommen.", name=borrower.name, group=grp)
    limit = get_int("max_loans", 0)
    try:
        lock_asset(asset.id)
        # Ab hier frisch aus der Datenbank zählen – parallele Anfragen warten bis zum Commit.
        quantity = db.session.query(Asset.quantity).filter(Asset.id == asset.id).scalar() or 0
        active = dict(db.session.query(Loan.user_id, func.count(Loan.id))
                      .filter(Loan.asset_id == asset.id, Loan.returned_at.is_(None)).group_by(Loan.user_id).all())
        if borrower.id in active:
            db.session.rollback()
            return False, _("{name} hat diesen Gegenstand bereits.", name=borrower.name)
        if sum(active.values()) >= quantity:
            db.session.rollback()
            return False, _("Kein Stück verfügbar.")
        if limit and count_active_loans(borrower.id) >= limit:
            db.session.rollback()
            return False, _("{name} hat bereits die maximale Anzahl von {limit} Gegenständen.", name=borrower.name, limit=limit)
        loan = Loan(asset_id=asset.id, user_id=borrower.id, issued_by_id=current_user.id,
                    due_at=default_due_date() if due is DEFAULT_DUE else due, note=note[:500])
        db.session.add(loan)
        db.session.commit()
    except IntegrityError:   # eindeutiger Index: dieselbe Person hat den Gegenstand gerade eben schon bekommen
        db.session.rollback()
        return False, _("{name} hat diesen Gegenstand bereits.", name=borrower.name)
    except OperationalError:  # z. B. Datenbank zu lange gesperrt
        db.session.rollback()
        return False, _("Die Datenbank ist gerade beschäftigt. Bitte versuch es gleich noch einmal.")
    notify_loan(loan)
    if loan.due_at is None:
        return True, _("„{title}“ an {name} ausgegeben (unbefristet).", title=asset.name, name=borrower.name)
    return True, _("„{title}“ an {name} ausgegeben – zurück bis {date}.", title=asset.name, name=borrower.name,
                   date=format_day(loan.due_at))


def return_loan(loan: Loan) -> str:
    # Nur zurückbuchen, wenn noch offen – ein Doppelklick oder zwei Fenster buchen sonst zweimal (zwei Mails)
    res = db.session.execute(update(Loan).where(Loan.id == loan.id, Loan.returned_at.is_(None))
                             .values(returned_at=datetime.now(), returned_by_id=current_user.id))
    db.session.commit()
    db.session.refresh(loan)
    if res.rowcount != 1:
        return _("„{title}“ war bereits zurückgegeben.", title=loan.asset.name)
    notify_return(loan)
    return _("„{title}“ von {name} zurückgenommen.", title=loan.asset.name, name=loan.user.name)


@bp.route("/gegenstaende/<int:asset_id>/ausgeben", methods=["POST"])
@login_required
def borrow(asset_id):
    asset = get_by_id(Asset, asset_id) or abort(404)
    if catalog_hidden(current_user):
        abort(404)   # ohne Bestand weder Selbstausleihe noch Hinweis, ob es die Nummer gibt
    target_id = parse_id(request.form.get("user_id"))
    due = DEFAULT_DUE
    if target_id and target_id != current_user.id:
        if not current_user.can_manage(asset):
            abort(403)
        borrower = get_by_id(User, target_id) or abort(404)
    else:
        borrower = current_user
        if not current_user.can_manage(asset) and (not get_bool("self_checkout") or catalog_hidden(current_user)):
            abort(403)
    if current_user.can_manage(asset) and request.form.get("no_due"):
        due = None
    elif current_user.can_manage(asset) and request.form.get("due_at"):
        try:
            due = date.fromisoformat(request.form["due_at"])
        except ValueError:
            flash(_("Ungültiges Rückgabedatum."), "error")
            return _back(url_for("assets.detail", asset_id=asset.id))
        if isinstance(due, date) and not (date.today() <= due <= date.today() + timedelta(days=3650)):
            flash(_("Das Fälligkeitsdatum muss zwischen heute und in zehn Jahren liegen."), "error")
            return _back(url_for("assets.detail", asset_id=asset.id))
    ok, msg = lend_asset(asset, borrower, due, request.form.get("note", ""))
    flash(msg, "success" if ok else "error")
    return _back(url_for("assets.detail", asset_id=asset.id))


@bp.route("/ausleihen/<int:loan_id>/rueckgabe", methods=["POST"])
@login_required
def give_back(loan_id):
    loan = get_by_id(Loan, loan_id) or abort(404)
    own = loan.user_id == current_user.id and get_bool("self_return")
    if not (own or current_user.can_manage(loan.asset)):
        abort(404 if catalog_hidden(current_user) and loan.user_id != current_user.id else 403)
    if loan.returned_at:
        flash(_("Diese Ausleihe ist bereits abgeschlossen."), "info")
    else:
        flash(return_loan(loan), "success")
    return _back(url_for("loans.index"))


@bp.route("/ausleihen/<int:loan_id>/verlaengern", methods=["POST"])
@login_required
def extend(loan_id):
    loan = get_by_id(Loan, loan_id) or abort(404)
    manager = current_user.can_manage(loan.asset)
    own = loan.user_id == current_user.id and get_bool("self_extend")
    if not (manager or own):
        abort(404 if catalog_hidden(current_user) and loan.user_id != current_user.id else 403)
    max_ext, days = get_int("max_extensions", 2), get_int("extension_days", 14)
    for _attempt in range(3):
        if loan.returned_at:
            flash(_("Diese Ausleihe ist bereits abgeschlossen."), "info")
            break
        if loan.due_at is None:
            flash(_("Diese Ausleihe ist unbefristet – eine Verlängerung ist nicht nötig."), "info")
            break
        if not manager and loan.extensions >= max_ext:
            flash(_("Die maximale Anzahl an Verlängerungen ist erreicht. Bitte wende dich an die Verwaltung."), "error")
            break
        old_ext = loan.extensions
        new_due = max(loan.due_at, date.today()) + timedelta(days=days)
        # Nur übernehmen, wenn in der Zwischenzeit niemand sonst verlängert oder zurückgegeben hat
        res = db.session.execute(
            update(Loan).where(Loan.id == loan.id, Loan.returned_at.is_(None), Loan.extensions == old_ext)
            .values(extensions=Loan.extensions + 1, due_at=new_due).execution_options(synchronize_session=False))
        db.session.commit()
        if res.rowcount == 1:
            flash(_("Verlängert bis {date}.", date=format_day(new_due)), "success")
            break
        db.session.refresh(loan)   # parallele Änderung – mit dem neuen Stand noch einmal prüfen
    return _back(url_for("loans.index"))


@bp.route("/ausleihen/<int:loan_id>/erinnern", methods=["POST"])
@manager_required
def remind(loan_id):
    loan = get_by_id(Loan, loan_id) or abort(404)
    if not current_user.can_manage(loan.asset):
        abort(403)
    if loan.returned_at:
        flash(_("Diese Ausleihe ist bereits abgeschlossen."), "info")
        return _back(url_for("loans.index"))
    ok, msg = send_manual_reminder(loan)
    flash(_("Erinnerung an {email} gesendet.", email=loan.user.email) if ok
          else _("Keine Erinnerung gesendet: {reason}", reason=msg),
          "success" if ok else "error")
    return _back(url_for("loans.index"))


@bp.route("/ausleihen")
@login_required
def index():
    view = request.args.get("ansicht", "aktiv")
    q = (request.args.get("q") or "").strip()
    query = Loan.query.join(Asset).join(User, Loan.user_id == User.id)
    if current_user.is_manager and request.args.get("nur") != "meine":
        query = query.filter(Loan.asset_id.in_(manageable_assets_query(current_user).with_entities(Asset.id)))
        scope = "alle"
    else:
        query = query.filter(Loan.user_id == current_user.id)
        scope = "meine"
    if view == "ueberfaellig":   # unbefristete Ausleihen (due_at leer) sind nie überfällig
        query = query.filter(Loan.returned_at.is_(None), Loan.due_at < date.today()).order_by(Loan.due_at)
    elif view == "verlauf":
        query = query.filter(Loan.returned_at.isnot(None)).order_by(Loan.returned_at.desc())
    else:
        view = "aktiv"
        query = query.filter(Loan.returned_at.is_(None)).order_by(Loan.due_at.is_(None), Loan.due_at)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Asset.name.ilike(like), Asset.inventory_no.ilike(like),
                                 Asset.serial_number.ilike(like), Asset.category.ilike(like),
                                 User.username.ilike(like), User.display_name.ilike(like)))
    total = query.order_by(None).count()
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(max(1, request.args.get("seite", 1, type=int)), pages)
    loans = (query.options(contains_eager(Loan.asset), contains_eager(Loan.user))
             .order_by(Loan.id).offset((page - 1) * PER_PAGE).limit(PER_PAGE).all())
    from ..mailer import mail_active
    return render_template("loans/index.html", loans=loans, view=view, q=q, scope=scope, mail_on=mail_active(),
                           page=page, pages=pages, total=total,
                           self_return=get_bool("self_return"), self_extend=get_bool("self_extend"),
                           max_ext=get_int("max_extensions", 2))


# ---------------------------------------------------------------- Ausgabetheke

def _find_assets(code: str) -> tuple[list, bool]:
    """Gegenstände zu einem gescannten/getippten Code. Liefert (Treffer, genau).

    „#12“ ist immer die interne Nummer (so steht sie auf Etiketten ohne Inventarnummer) – nie eine Namenssuche.
    Danach Inventarnummer (genau) → Seriennummer (genau) → Bezeichnung (Teilwort, nur zur Auswahl).
    `genau` ist False bei Treffern der Namenssuche: dann wird nie automatisch aus- oder zurückgegeben."""
    code = (code or "").strip()
    if not code:
        return [], False
    base = manageable_assets_query(current_user)
    if re.fullmatch(r"#\d+", code):
        ident = parse_id(code[1:])
        return (base.filter(Asset.id == ident).all() if ident else []), True
    # Vergleich über die normalisierten Schlüssel (wie Vorschläge und Dubletten-Prüfung) – auch „Ü-12“ = „ü-12“
    key = number_key(code)
    for col in (Asset.inventory_key, Asset.serial_key):
        found = base.filter(col == key).limit(20).all() if key else []
        if found:
            return found, True
    return base.filter(Asset.name.ilike(f"%{code}%")).limit(20).all(), False


def ad_search_available() -> bool:
    """Personensuche im Active Directory: nur mit aktivem AD und Dienstkonto."""
    from ..ldap_auth import ldap_enabled
    return ldap_enabled() and bool(get_setting("ldap_bind_dn"))


def _find_people(query: str):
    """Person für die Ausgabe finden. Liefert (gewählt, lokale Treffer, AD-Treffer, AD-Fehlermeldung).

    Genauer Benutzername → sofort gewählt. Sonst lokale Konten (Name/Benutzername enthält den Text) und – falls
    eingerichtet – Personen aus dem Active Directory, die sich nach den AD-Regeln anmelden dürften, aber noch
    kein Konto in AssetLotse haben. Automatisch gewählt wird nur, wenn es insgesamt genau einen Treffer gibt."""
    exact = User.query.filter(func.lower(User.username) == query.lower()).first()
    if exact is not None:
        return exact, [], [], ""
    like = f"%{query}%"
    people = (User.query.filter(User.active.is_(True),
                                or_(User.display_name.ilike(like), User.username.ilike(like)))
              .order_by(User.display_name, User.username).limit(11).all())
    ad_people, ad_error, ad_more = [], "", False
    from .. import ad_cache
    if ad_search_available() and len(query) >= 2 and ad_cache.ready():
        # schnell: Zwischenspeicher (im Hintergrund abgeglichen); nur ohne Treffer zusätzlich live im AD
        ad_people = ad_cache.search(query, limit=11)
        ad_more = len(ad_people) > 10
        ad_people = ad_people[:10]
    if ad_search_available() and len(query) >= 2 and not ad_people:
        from ..ldap_auth import Directory, LDAPError
        log.info("AD-Personensuche an der Theke durch %s: %r", current_user.username, query)
        try:
            known = {u for (u,) in db.session.query(User.username).all()}
            ad_people, ad_more = Directory().search_users(query, exclude=known)
        except LDAPError as exc:
            log.warning("AD-Personensuche fehlgeschlagen: %s", exc)
            # Details (Bind-Fehler, DNs …) nur für Administratoren – Verwalter bekommen eine allgemeine Meldung
            ad_error = str(exc) if current_user.is_admin else _("Das Active Directory ist gerade nicht erreichbar.")
        except Exception:  # noqa: BLE001 – ein AD-Problem darf die Theke nicht lahmlegen
            log.exception("AD-Personensuche fehlgeschlagen")
            ad_error = _("Das Active Directory ist gerade nicht erreichbar.")
    if len(people) == 1 and not ad_people and not ad_more:
        return people[0], [], [], ad_error
    if ad_more:
        flash(_("Es gibt möglicherweise weitere Personen im Active Directory – bitte genauer suchen."), "info")
    elif len(people) > 10:
        flash(_("Mehr als 10 Konten passen – bitte genauer suchen."), "info")
    elif not people and not ad_people:
        flash(_("Keine Person zu „{query}“ gefunden.", query=query), "error")
    return None, people[:10], ad_people, ad_error


@bp.route("/theke/personen")
@manager_required
def people_suggest():
    """Vorschläge beim Tippen im Personenfeld der Theke: Konten in AssetLotse und – aus dem Zwischenspeicher –
    Personen aus dem AD, die sich noch nie angemeldet haben. Live im AD wird hier nie gesucht (zu langsam)."""
    from flask import jsonify
    from .. import ad_cache
    q = " ".join((request.args.get("q") or "").split())[:100]
    items = []
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{esc}%"
        users = (User.query.filter(User.active.is_(True),
                                   or_(User.display_name.ilike(like, escape="\\"), User.username.ilike(like, escape="\\"),
                                       User.email.ilike(like, escape="\\")))
                 .order_by(User.display_name, User.username).limit(8).all())
        for u in users:
            items.append({"name": u.name, "sub": u.username + (" · AD" if u.auth_source == "ldap" else ""),
                          "url": url_for("loans.desk", modus="ausgabe", benutzer=u.username)})
        if ad_search_available() and ad_cache.ready():
            for p in ad_cache.search(q, limit=max(0, 10 - len(items)) or 1):
                items.append({"name": p.display_name or p.username,
                              "sub": " · ".join(x for x in (p.username, p.email) if x),
                              "badge": _("AD – noch kein Konto"),
                              "post": {"url": url_for("loans.desk_ad_account"), "username": p.username}})
    resp = jsonify({"items": items[:10]})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/theke/ad-konto", methods=["POST"])
@manager_required
def desk_ad_account():
    """Konto einer Person aus dem Active Directory übernehmen (ohne dass sie sich je angemeldet hat).

    Der Server prüft die Person erneut im AD – Formularwerte werden nie übernommen: Konto muss existieren,
    aktiv sein und nach den AD-Regeln eine Rolle bekommen."""
    from ..ldap_auth import Directory, LDAPAmbiguous, LDAPError
    from .main import sync_ldap_user
    username = (request.form.get("username") or "").strip()[:200]
    back = url_for("loans.desk", modus="ausgabe")
    if not username or not ad_search_available():
        abort(400)
    try:
        info = Directory().check_account(username)
    except LDAPAmbiguous:
        flash(_("„{name}“ ist im Active Directory nicht eindeutig – bitte die Administration informieren.",
                name=username), "error")
        return redirect(back)
    except LDAPError as exc:
        log.warning("AD-Kontoübernahme %s fehlgeschlagen: %s", username, exc)
        flash(_("Active Directory: {error}", error=str(exc)) if current_user.is_admin
              else _("Das Active Directory ist gerade nicht erreichbar."), "error")
        return redirect(back)
    if info is None or info.role is None:
        from .. import ad_cache
        ad_cache.forget(username)          # nicht bis zum nächsten Abgleich weiter anzeigen
        db.session.commit()
        flash(_("„{name}“ ist im Active Directory nicht (mehr) für AssetLotse freigegeben.", name=username), "error")
        return redirect(back)
    from .main import find_ldap_account, guid_conflict, legacy_locked
    existing = find_ldap_account(info)
    if legacy_locked(info, existing):
        flash(_("Zu „{name}“ gibt es ein gesperrtes älteres Konto ohne AD-Kennung – ein Administrator muss prüfen, ob "
                "es dieselbe Person ist (Konto aktivieren oder umbenennen).", name=info.username), "error")
        return redirect(back)
    if guid_conflict(info, existing):
        flash(_("Den Benutzernamen „{name}“ hat in AssetLotse ein früheres Konto einer anderen Person. Bitte das alte "
                "Konto unter Administration → Benutzer umbenennen.", name=info.username), "error")
        return redirect(back)
    if existing is not None and existing.auth_source != "ldap":
        flash(_("Es gibt bereits ein lokales Konto „{name}“ – es wird nicht mit dem Active Directory verknüpft.",
                name=existing.username), "error")
        return redirect(back)
    previous_role = existing.role if existing is not None else None
    try:
        # kein Autoflush während des Abgleichs: sonst schlägt ein paralleler Doppelklick schon hier fehl
        from .main import may_reactivate
        reactivate = existing is not None and may_reactivate(existing)   # vor dem Abgleich prüfen
        with db.session.no_autoflush:
            user = sync_ldap_user(info, existing)
            if reactivate:                      # vom AD-Abgleich gesperrt, laut AD wieder berechtigt
                user.active, user.deactivated_by_sync = True, False
    except IntegrityError:
        db.session.rollback()
        return redirect(url_for("loans.desk", modus="ausgabe", benutzer=info.username))
    # Die Rolle aus dem AD gilt erst ab der ersten eigenen Anmeldung (dort wird sie ohnehin neu bestimmt).
    # Bis dahin ist ein an der Theke übernommenes Konto nur Mitarbeiter – so bekommt es z. B. keine
    # Verwalter-/Admin-Mails mit Personendaten, solange die Person AssetLotse nie benutzt hat.
    user.role = previous_role or ROLE_READER
    try:
        db.session.commit()
    except IntegrityError:          # Doppelklick: das Konto wurde gerade eben angelegt
        db.session.rollback()
        user = User.query.filter(func.lower(User.username) == info.username.lower()).first()
        if user is None:
            abort(409)
        return redirect(url_for("loans.desk", modus="ausgabe", benutzer=user.username))
    from .. import ad_cache
    ad_cache.forget(user.username)         # hat jetzt ein Konto – gehört nicht mehr in den Zwischenspeicher
    db.session.commit()
    log.info("AD-Konto %s an der Ausgabetheke übernommen durch %s (Rolle %s)", user.username,
             current_user.username, user.role)
    if existing is None:
        flash(_("{name} aus dem Active Directory übernommen.", name=user.name), "success")
    return redirect(url_for("loans.desk", modus="ausgabe", benutzer=user.username))


STOCK_LIMIT = 30


def _stock_args() -> dict:
    """Parameter der Bestandssuche an der Theke (Suchtext, Kategorie, nur verfügbare)."""
    frei = request.values.get("frei", "1")
    return {"suche": (request.values.get("suche") or "").strip()[:100],
            "kategorie": (request.values.get("kategorie") or "").strip()[:200],
            "frei": "0" if frei == "0" else "1"}


def _stock_search(stock: dict, borrower: User | None = None):
    """Bestand an der Theke durchsuchen – nur Gegenstände, die dieser Verwalter ausgeben darf und (falls eine
    Person gewählt ist) die diese Person auch bekommen darf. None, solange weder Suchtext noch Kategorie da ist."""
    if not stock["suche"] and not stock["kategorie"]:
        return None
    from sqlalchemy.orm import selectinload
    from .assets import _apply_filters
    f = {"q": stock["suche"], "gruppe": "", "kategorie": stock["kategorie"], "standort": "",
         "status": "verfuegbar" if stock["frei"] == "1" else ""}
    query = _apply_filters(manageable_assets_query(current_user), f)
    if borrower is not None and not borrower.is_global_manager:
        query = query.filter(Asset.group_id.is_(None) | Asset.group_id.in_(list(borrower.group_ids) or [-1]))
    return (query
            .options(selectinload(Asset.loans.and_(Loan.returned_at.is_(None))), selectinload(Asset.group))
            .order_by(Asset.name, Asset.inventory_no, Asset.id).limit(STOCK_LIMIT + 1).all())


@bp.route("/theke", methods=["GET", "POST"])
@manager_required
def desk():
    mode = request.values.get("modus", "ausgabe")
    if mode not in ("ausgabe", "rueckgabe"):
        mode = "ausgabe"
    code = (request.values.get("code") or "").strip()
    user_q = (request.values.get("benutzer") or "").strip()[:120]
    borrower, people, ad_people, ad_error = None, [], [], ""
    if user_q:
        borrower, people, ad_people, ad_error = _find_people(user_q)

    assets, loans = [], []
    if request.method == "POST":
        if mode == "rueckgabe" and request.form.get("loan_id"):
            loan = get_by_id(Loan, parse_id(request.form.get("loan_id")) or 0) or abort(404)
            if not current_user.can_manage(loan.asset):
                abort(403)
            if loan.returned_at is None:
                flash(return_loan(loan), "success")
            return redirect(url_for("loans.desk", modus="rueckgabe"))
        if mode == "ausgabe" and request.form.get("asset_id") and borrower:
            asset = get_by_id(Asset, parse_id(request.form.get("asset_id")) or 0) or abort(404)
            if not current_user.can_manage(asset):
                abort(403)
            ok, msg = lend_asset(asset, borrower)
            flash(msg, "success" if ok else "error")
            # Bestandssuche bleibt erhalten – so kann man gleich das nächste Teil auswählen
            return redirect(url_for("loans.desk", modus="ausgabe", benutzer=borrower.username,
                                    **{k: v for k, v in _stock_args().items() if v}))

    if request.method == "POST" and "code" in request.form:
        # Häkchen „sofort aus-/zurückgeben“ merken – auch über Rückgabe-Knopf, Vorschläge und Ergebnisliste hinweg
        session["desk_auto"] = "1" if request.form.get("auto") == "1" else "0"
    if code:
        assets, exact = _find_assets(code)
        if mode == "rueckgabe":
            loans = [l for b in assets for l in b.active_loans]
            if exact and len(loans) == 1 and request.method == "POST" and request.values.get("auto") == "1":
                flash(return_loan(loans[0]), "success")
                return redirect(url_for("loans.desk", modus="rueckgabe"))
        elif mode == "ausgabe" and borrower and exact and len(assets) == 1 and request.method == "POST" \
                and request.values.get("auto") == "1":
            ok, msg = lend_asset(assets[0], borrower)
            flash(msg, "success" if ok else "error")
            return redirect(url_for("loans.desk", modus="ausgabe", benutzer=borrower.username,
                                    **{k: v for k, v in _stock_args().items() if v and k != "frei"}))
        if not assets:
            flash(_("Kein Gegenstand zu „{code}“ gefunden.", code=code), "error")

    stock = _stock_args()
    stock_results = _stock_search(stock, borrower) if mode == "ausgabe" else None
    from .assets import category_names
    # Gruppen-Verwalter sehen nur Ausleihen und Gruppen, die sie selbst verwalten
    borrower_loans = [l for l in borrower.active_loans if current_user.can_manage(l.asset)] if borrower else []
    managed = {g.id for g in current_user.manageable_groups()}
    borrower_groups = [g.name for g in borrower.groups if g.id in managed] if borrower else []
    return render_template("loans/desk.html", mode=mode, code=code, assets=assets, loans=loans,
                           borrower=borrower, borrower_loans=borrower_loans,
                           borrower_groups=borrower_groups, people=people, ad_people=ad_people, ad_error=ad_error,
                           ad_search=ad_search_available(),
                           user_q=user_q, stock=stock, stock_results=stock_results,
                           categories=category_names(manageable_assets_query(current_user)) if mode == "ausgabe" else [],
                           focus_search=request.args.get("fokus") == "suche",
                           auto=session.get("desk_auto", "1"))
