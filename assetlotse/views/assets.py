"""Bestand: Gegenstände anzeigen, anlegen, bearbeiten, löschen, exportieren."""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import os

from flask import (Blueprint, Response, abort, current_app, flash, jsonify, make_response, redirect,
                   render_template, request, send_from_directory, url_for)
from flask_login import current_user, login_required
from sqlalchemy import case, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from werkzeug.utils import secure_filename

from ..i18n import _, get_locale
from ..models import (Asset, Group, Loan, SecretAccess, User, db, get_bool, get_int, get_setting,
                      catalog_hidden, manageable_assets_query, visible_assets_query)
from . import form_int, form_str, get_by_id, manager_required, parse_id

bp = Blueprint("assets", __name__)
log = logging.getLogger(__name__)

PER_PAGE = 30
TEXT_FIELDS = {  # Feldname -> maximale Länge
    "name": 300, "category": 200, "manufacturer": 200, "model": 200, "serial_number": 200,
    "inventory_no": 100, "location": 200,
}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_QUANTITY = 10_000
MAX_SECRET = 4000


def _active_loan_counts():
    """Unterabfrage: Gegenstand-ID → Zahl der offenen Ausleihen (für Filter und Export)."""
    return (db.session.query(Loan.asset_id.label("asset_id"), func.count(Loan.id).label("n"))
            .filter(Loan.returned_at.is_(None)).group_by(Loan.asset_id).subquery())


def _with_active_loans():
    """Nur die offenen Ausleihen mitladen – genug für „verfügbar“, ohne den ganzen Verlauf."""
    return selectinload(Asset.loans.and_(Loan.returned_at.is_(None)))


def _get_asset_or_404(asset_id: int) -> Asset:
    asset = get_by_id(Asset, asset_id)
    if asset is None:
        abort(404)
    return asset


def _check_group_allowed(group_id):
    if group_id is None:
        return None
    group = get_by_id(Group, group_id)
    if group is None or group not in current_user.manageable_groups():
        abort(403)
    return group.id


def category_names(query=None) -> list[str]:
    """Bisher verwendete Kategorien (für die Auswahl beim Erfassen und den Filter) – auf Wunsch nur aus einem
    eingeschränkten Bestand (z. B. dem, den ein Gruppen-Verwalter verwalten darf)."""
    base = query if query is not None else Asset.query
    rows = (base.with_entities(Asset.category).filter(Asset.category.isnot(None), Asset.category != "")
            .distinct().all())
    return sorted({r[0] for r in rows}, key=str.lower)


KNOWN_LIMIT = 1000


def known_values(query) -> dict:
    """Vorschläge beim Erfassen aus dem vorhandenen Bestand (nur, was dieser Verwalter verwalten darf):
    Bezeichnungen, Hersteller, Modelle – und was zu einer Bezeichnung bzw. einem Modell meist dazugehört,
    damit das Formular leere Felder gleich ausfüllen kann (z. B. Modell „Vostro 3400“ → Hersteller „Dell“)."""
    from collections import Counter, defaultdict
    rows = (query.with_entities(Asset.name, Asset.category, Asset.manufacturer, Asset.model)
            .order_by(Asset.id.desc()).limit(KNOWN_LIMIT).all())
    by_name, by_model = defaultdict(Counter), defaultdict(Counter)
    for name, category, maker, model in rows:
        if name:
            by_name[name][(category or "", maker or "", model or "")] += 1
        if model:
            by_model[model][(category or "", maker or "")] += 1

    def most(counter):
        return counter.most_common(1)[0][0]
    names = {n: dict(zip(("category", "manufacturer", "model"), most(c))) for n, c in by_name.items()}
    models = {m: dict(zip(("category", "manufacturer"), most(c))) for m, c in by_model.items()}
    makers = {r[2] for r in rows if r[2]}
    return {"names": names, "models": models, "manufacturers": sorted(makers, key=str.lower)}


# ---------------------------------------------------------------- Fotos

def _clean_image(raw: bytes) -> bytes | None:
    """Nur echte Bilder annehmen und neu kodieren (entfernt eingeschleuste Inhalte und Metadaten)."""
    from PIL import Image
    from ..branding import BrandingError, _open_image
    try:
        img = _open_image(raw, draft=(1200, 1200))
        img = img.convert("RGB")
        img.thumbnail((1200, 1200), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=86, optimize=True)
        return out.getvalue()
    except BrandingError:
        return None
    except Exception as exc:  # noqa: BLE001 – z. B. abgeschnittenes JPEG (OSError) oder Dekompressionsbombe
        log.info("Bild nicht lesbar: %s", exc)
        return None


def _store_image_bytes(data: bytes) -> str:
    name = f"{hashlib.sha1(data).hexdigest()[:20]}.jpg"
    path = os.path.join(current_app.config["IMAGE_DIR"], name)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return name


def _image_from_template(asset: Asset) -> None:
    """„Ähnlichen anlegen“: das Foto der Vorlage übernehmen (Bilddateien sind nach Inhalt benannt und werden
    nie gelöscht – mehrere Gegenstände dürfen dieselbe Datei verwenden)."""
    template = get_by_id(Asset, parse_id(request.form.get("image_from")) or 0)
    if template is not None and template.image_file and current_user.can_manage(template):
        asset.image_file = template.image_file


def _apply_image(asset: Asset):
    upload = request.files.get("image_upload")
    if upload and upload.filename:
        ext = os.path.splitext(secure_filename(upload.filename))[1].lower()
        clean = _clean_image(upload.read()) if ext in IMAGE_EXT else None
        if clean is None:
            flash(_("Foto: Die Datei ist kein gültiges Bild (erlaubt: JPG, PNG, WEBP oder GIF)."), "error")
        else:
            asset.image_file = _store_image_bytes(clean)
            return
    if request.form.get("remove_image"):
        asset.image_file = ""


@bp.route("/bilder/<path:filename>")
@login_required
def image(filename):
    resp = send_from_directory(current_app.config["IMAGE_DIR"], secure_filename(filename),
                               max_age=60 * 60 * 24 * 30)
    resp.cache_control.public = False      # nur im Browser zwischenspeichern, nie in fremden Caches/CDNs
    resp.cache_control.private = True
    return resp


# ---------------------------------------------------------------- Bestand

FILTER_KEYS = ("q", "gruppe", "kategorie", "standort", "status")


def _filter_args() -> dict:
    return {k: (request.args.get(k) or "").strip() for k in FILTER_KEYS}


def _apply_filters(query, f: dict):
    """Suchbegriff und Filter des Bestands (auch für den Etikettendruck der aktuellen Auswahl)."""
    if f["q"]:
        like = f"%{f['q']}%"
        query = query.filter(or_(Asset.name.ilike(like), Asset.category.ilike(like),
                                 Asset.manufacturer.ilike(like), Asset.model.ilike(like),
                                 Asset.serial_number.ilike(like), Asset.inventory_no.ilike(like),
                                 Asset.location.ilike(like), Asset.description.ilike(like)))
    if f["gruppe"] == "allgemein":
        query = query.filter(Asset.group_id.is_(None))
    elif parse_id(f["gruppe"]):
        query = query.filter(Asset.group_id == parse_id(f["gruppe"]))
    if f["kategorie"]:
        query = query.filter(Asset.category == f["kategorie"])
    if f["standort"]:
        query = query.filter(Asset.location == f["standort"])
    if f["status"] in ("verfuegbar", "ausgegeben"):   # in SQL filtern statt jeden Gegenstand einzeln zu laden
        active = _active_loan_counts()
        lent = func.coalesce(active.c.n, 0)
        query = query.outerjoin(active, active.c.asset_id == Asset.id)
        query = query.filter(Asset.quantity > lent if f["status"] == "verfuegbar" else lent > 0)
    return query


@bp.route("/gegenstaende")
@login_required
def index():
    if catalog_hidden(current_user):   # Mitarbeiter sehen nur ihre eigenen Gegenstände
        return redirect(url_for("loans.index"))
    f = _filter_args()
    q, group, category, location, status = f["q"], f["gruppe"], f["kategorie"], f["standort"], f["status"]
    sort = request.args.get("sort", "name")
    page = max(1, request.args.get("seite", 1, type=int))
    query = _apply_filters(visible_assets_query(current_user), f)

    order = {
        "name": Asset.name.asc(), "category": Asset.category.asc(),
        "inventory": Asset.inventory_no.asc(), "new": Asset.created_at.desc(),
    }.get(sort, Asset.name.asc())
    total = query.order_by(None).count()
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(page, pages)
    loans_opt = _with_active_loans().selectinload(Loan.user) if current_user.is_manager else _with_active_loans()
    assets = (query.options(loans_opt, selectinload(Asset.group))
              .order_by(order, Asset.id).offset((page - 1) * PER_PAGE).limit(PER_PAGE).all())

    if current_user.is_global_manager or get_setting("catalog_visibility") == "all":
        groups = Group.query.order_by(Group.name).all()
    else:
        groups = sorted(current_user.groups, key=lambda g: g.name.lower())
    from .locations import names as location_names
    return render_template("assets/index.html", assets=assets, q=q, group=group, status=status,
                           sort=sort, page=page, pages=pages, total=total, groups=groups,
                           category=category, categories=category_names(visible_assets_query(current_user)),
                           location=location, location_names=location_names())


def _render_detail(asset: Asset, secret: str | None = None, own_secret: str | None = None):
    users = []
    if current_user.can_manage(asset):
        users = [u for u in User.query.filter_by(active=True).options(selectinload(User.groups))
                 .order_by(User.display_name, User.username) if u.can_borrow(asset)]
    own_loan = next((l for l in asset.active_loans if l.user_id == current_user.id), None)
    return render_template("assets/detail.html", asset=asset, users=users, own_loan=own_loan, own_secret=own_secret,
                           self_checkout=get_bool("self_checkout"),
                           self_return=get_bool("self_return"),
                           can_extend=bool(own_loan) and own_loan.due_at is not None and (
                               current_user.can_manage(asset) or (
                                   get_bool("self_extend") and own_loan.extensions < get_int("max_extensions", 2))),
                           loan_days=get_int("loan_days", 0), secret=secret)


@bp.route("/gegenstaende/<int:asset_id>")
@login_required
def detail(asset_id):
    asset = _get_asset_or_404(asset_id)
    holds_it = any(l.user_id == current_user.id for l in asset.active_loans)   # eigenes Gerät immer sichtbar
    if not holds_it and (catalog_hidden(current_user)
                         or not current_user.can_see(asset, get_setting("catalog_visibility") == "all")):
        abort(404)
    return _render_detail(asset)


@bp.route("/gegenstaende/<int:asset_id>/vertraulich", methods=["POST"])
@manager_required
def reveal_secret(asset_id):
    """Vertrauliche Angaben anzeigen – nur für Verwalter dieses Gegenstands, jeder Abruf wird protokolliert."""
    from flask import session
    asset = _get_asset_or_404(asset_id)
    if not current_user.can_view_secret(asset):
        abort(403)
    if current_user.has_2fa and not session.get("mfa_ok"):
        # Ohne zweiten Faktor angemeldet (z. B. internes Netz): Schlüssel erst nach Bestätigung
        flash(_("Bitte bestätige zuerst deinen zweiten Faktor (Authenticator-App, Sicherheitsschlüssel oder "
                "Wiederherstellungscode) – du hast dich in dieser Sitzung ohne ihn angemeldet."), "warning")
        return redirect(url_for("assets.detail", asset_id=asset.id))
    secret = asset.get_secret()
    if asset.has_secret and not secret:
        flash(_("Die vertraulichen Angaben sind mit dem aktuellen Schlüssel nicht lesbar."), "error")
        return redirect(url_for("assets.detail", asset_id=asset.id))
    if secret:
        SecretAccess.record(asset, current_user, "view")
        db.session.commit()
        log.info("Vertrauliche Angaben von Gegenstand %s angezeigt durch %s", asset.id, current_user.username)
    resp = make_response(_render_detail(asset, secret=secret))
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _confirm_password(user: User, password: str) -> bool:
    """Passwort der angemeldeten Person erneut prüfen – lokal gegen den Hash, bei AD-Konten per Anmeldung am AD.

    Beim AD muss das Ergebnis genau dieses Konto sein und noch eine Zugangsregel erfüllen."""
    if not password:
        return False
    if user.auth_source != "ldap":
        return user.check_password(password)
    from ..ldap_auth import LDAPError, authenticate, ldap_enabled, normalize_dn
    if not ldap_enabled():
        return False
    try:
        info = authenticate(user.username, password)
    except LDAPError as exc:
        log.info("Passwortbestätigung (AD) für %s fehlgeschlagen: %s", user.username, exc)
        return False
    except Exception:  # noqa: BLE001 – Details nie an den Client
        log.exception("Unerwarteter Fehler bei der Passwortbestätigung (AD)")
        return False
    if info.role is None:
        return False
    if user.ldap_dn:
        return normalize_dn(info.dn) == normalize_dn(user.ldap_dn)
    return (info.username or "").lower() == user.username.lower()


@bp.route("/gegenstaende/<int:asset_id>/mein-schluessel", methods=["POST"])
@login_required
def reveal_own_secret(asset_id):
    """Vertrauliche Angaben für die Person, der der Gegenstand gerade ausgegeben ist – nur mit Freigabe am
    Gegenstand, nach erneuter Passworteingabe, und jeder Abruf wird protokolliert."""
    from flask import session

    from .. import lockout, netzone
    asset = _get_asset_or_404(asset_id)
    if not current_user.can_view_own_secret(asset):
        holds_it = any(l.user_id == current_user.id for l in asset.active_loans)
        abort(404 if catalog_hidden(current_user) and not holds_it else 403)
    back = redirect(url_for("assets.detail", asset_id=asset.id))
    if current_user.has_2fa and not session.get("mfa_ok"):
        # In dieser Sitzung ohne zweiten Faktor angemeldet (z. B. internes Netz): erst nachholen
        flash(_("Bitte bestätige zuerst deinen zweiten Faktor (Authenticator-App, Sicherheitsschlüssel oder "
                "Wiederherstellungscode) – du hast dich in dieser Sitzung ohne ihn angemeldet."), "warning")
        return back
    key = f"secret|{lockout.norm(current_user.username)}|{request.remote_addr}"
    if lockout.throttled(key) or lockout.user_throttled(current_user.username, internal=netzone.is_internal()):
        flash(_("Zu viele Fehlversuche. Bitte warte einige Minuten."), "error")
        return back
    if not _confirm_password(current_user, request.form.get("password") or ""):
        lockout.fail(key, current_user.username)
        log.warning("Vertrauliche Angaben von Gegenstand %s: Passwort von %s falsch", asset.id, current_user.username)
        flash(_("Das Passwort ist falsch."), "error")
        return back
    lockout.clear(key)
    # Die AD-Prüfung kann Sekunden dauern – wurde inzwischen zurückgegeben, gibt es nichts mehr
    db.session.expire(asset)
    if not current_user.can_view_own_secret(asset):
        abort(403)
    secret = asset.get_secret()
    if not secret:
        flash(_("Die vertraulichen Angaben sind mit dem aktuellen Schlüssel nicht lesbar."), "error")
        return back
    SecretAccess.record(asset, current_user, "holder")
    db.session.commit()
    log.info("Vertrauliche Angaben von Gegenstand %s angezeigt durch Ausleiher %s", asset.id, current_user.username)
    resp = make_response(_render_detail(asset, own_secret=secret))
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- Vorschläge beim Tippen

SUGGEST_LIMIT = 8


def _like_escape(value: str) -> str:
    """% und _ wörtlich nehmen (Escape-Zeichen: Backslash, bei LIKE mit escape="\\")."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _json(data, status: int = 200):
    resp = jsonify(data)
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/vorschlaege")
@login_required
def suggest():
    """Treffer beim Tippen – für die Suche oben (`fuer=suche`) und die Ausgabetheke (`fuer=theke`).

    Es gelten dieselben Rechte wie in Bestand bzw. Theke: Mitarbeiter ohne Bestand bekommen nichts, an der Theke
    nur Gegenstände, die dieser Verwalter ausgeben darf – und mit `benutzer` nur solche, die die Person bekommen darf."""
    q = (request.args.get("q") or "").strip()[:100]
    purpose = request.args.get("fuer", "suche")
    if purpose == "theke":
        if not current_user.is_manager:
            abort(403)
        base = manageable_assets_query(current_user)
    else:
        if catalog_hidden(current_user):
            return _json({"items": []})
        base = visible_assets_query(current_user)
    if not q:
        return _json({"items": []})
    borrower = None
    if purpose == "theke":
        from ..models import lendable_clause
        base = base.filter(lendable_clause())       # Gruppen mit „Kein Verleih“ an der Theke gar nicht anbieten
    if purpose == "theke" and request.args.get("benutzer"):
        borrower = User.query.filter(func.lower(User.username) == request.args["benutzer"].strip().lower()).first()
        if borrower is not None and not borrower.is_global_manager:
            base = base.filter(Asset.group_id.is_(None) | Asset.group_id.in_(list(borrower.group_ids) or [-1]))
    if q.startswith("#") and parse_id(q[1:]):
        query = base.filter(Asset.id == parse_id(q[1:]))
    else:
        like, esc = f"%{_like_escape(q)}%", "\\"
        query = base.filter(or_(*(c.ilike(like, escape=esc) for c in (
            Asset.name, Asset.category, Asset.manufacturer, Asset.model, Asset.serial_number, Asset.inventory_no))))
    from ..models import number_key
    key = number_key(q)
    rank = case((Asset.inventory_key == key, 0), (Asset.serial_key == key, 0),
                (Asset.inventory_no.ilike(f"{_like_escape(q)}%", escape="\\"), 1),
                (Asset.name.ilike(f"{_like_escape(q)}%", escape="\\"), 2), else_=3)
    assets = (query.options(_with_active_loans())
              .order_by(rank, Asset.name, Asset.id).limit(SUGGEST_LIMIT + 1).all())
    items = []
    for a in assets[:SUGGEST_LIMIT]:
        item = {"id": a.id, "name": a.name, "category": a.category or "",
                "numbers": " · ".join(x for x in (a.inventory_no, a.serial_number) if x),
                "available": a.available, "quantity": a.quantity,
                "url": url_for("assets.detail", asset_id=a.id)}
        if purpose == "theke":
            item["can_issue"] = a.available > 0 and (borrower is None or borrower.can_borrow(a))
        items.append(item)
    return _json({"items": items, "more": len(assets) > SUGGEST_LIMIT})


def next_free_number(value: str, base=None) -> str | None:
    """Nächste freie Inventarnummer im Schema der eingegebenen (bzw. zuletzt im eigenen Bestand vergebenen)
    Nummer, z. B. „IT-00“ → „IT-0044“, wenn IT-0043 die höchste ist. None, wenn es kein Schema gibt.

    Die höchste Nummer wird über den ganzen Bestand gesucht (Nummern sind überall eindeutig), das Schema bei leerem
    Feld aber nur im Bestand, den diese Person verwaltet."""
    import re
    value = (value or "").strip()
    if not value:
        q = (base if base is not None else Asset.query).with_entities(Asset.inventory_no)
        latest = (q.filter(Asset.inventory_no != "")
                  .order_by(Asset.created_at.desc(), Asset.id.desc()).first())
        if not latest or not re.search(r"\d$", latest[0]):
            return None
        value = latest[0]
    prefix = re.sub(r"\d+$", "", value)
    if len(prefix) < 2:          # „I“ oder „1“ passt auf fast alles – da hilft kein Vorschlag
        return None
    rows = (db.session.query(Asset.inventory_no)
            .filter(Asset.inventory_no.ilike(f"{_like_escape(prefix)}%", escape="\\"))
            .order_by(func.length(Asset.inventory_no).desc(), Asset.inventory_no.desc()).limit(200).all())
    pattern = re.compile(re.escape(prefix) + r"(\d+)$", re.IGNORECASE)
    matches = [m for (v,) in rows if v and (m := pattern.match(v))]
    if not matches:
        return None
    best = max(matches, key=lambda m: int(m.group(1)))
    prefix = best.string[:best.start(1)]          # Schreibweise der vorhandenen Nummern übernehmen
    width = max(len(m.group(1)) for m in matches)
    nxt = int(best.group(1)) + 1
    for _try in range(50):
        candidate = f"{prefix}{nxt:0{width}d}"
        if find_number("inventory_no", candidate) is None:
            return candidate
        nxt += 1
    return None


@bp.route("/gegenstaende/nummer-pruefen")
@manager_required
def check_number():
    """Beim Tippen im Formular: Ist die Inventar-/Seriennummer schon vergeben? Dazu vorhandene ähnliche
    Inventarnummern (nur aus dem eigenen Bestand) und die nächste freie Nummer."""
    field = request.args.get("feld", "inventory_no")
    if field not in NUMBER_FIELDS:
        abort(400)
    value = (request.args.get("wert") or "").strip()[:200]
    other = find_number(field, value, parse_id(request.args.get("ausser")))
    data = {"taken": other is not None, "by": None, "numbers": [], "next": None}
    if other is not None and current_user.can_manage(other):
        data["by"] = {"name": other.name, "url": url_for("assets.detail", asset_id=other.id)}
    if field == "inventory_no":
        mine = manageable_assets_query(current_user)
        rows = (mine.with_entities(Asset.inventory_no)
                .filter(Asset.inventory_no != "", Asset.inventory_no.ilike(f"{_like_escape(value)}%", escape="\\"))
                .order_by(Asset.inventory_no.desc()).limit(6).all())
        data["numbers"] = [v for (v,) in rows]
        data["next"] = next_free_number(value, mine)
    return _json(data)


# ---------------------------------------------------------------- Pflege

def _fill_asset(asset: Asset) -> str | None:
    """Formular übernehmen; liefert eine Fehlermeldung (z. B. unbekannter Standort) oder None.

    Ohne Autoflush: Die Eingaben dürfen erst nach allen Prüfungen (z. B. doppelte Nummern) in die Datenbank."""
    with db.session.no_autoflush:
        return _fill_asset_fields(asset)


def _fill_asset_fields(asset: Asset) -> str | None:
    from .locations import check as check_location
    previous_location = asset.location or ""
    for field, maxlen in TEXT_FIELDS.items():
        setattr(asset, field, form_str(field, maxlen))
    asset.location, location_error = check_location(form_str("location", 200), previous_location)
    asset.description = (request.form.get("description") or "").strip()[:20000]
    asset.quantity = form_int("quantity", 1, 0, MAX_QUANTITY)
    if asset.quantity < len(asset.active_loans):
        asset.quantity = len(asset.active_loans)
        flash(_("Die Anzahl kann nicht kleiner sein als die Zahl der ausgegebenen Stücke."), "warning")
    gid = request.form.get("group_id")
    new_group = _check_group_allowed(parse_id(gid)) if parse_id(gid) else None
    if (asset.id and asset.has_secret and new_group != asset.group_id
            and not current_user.can_view_secret(asset)):
        # Sonst könnte sich ein Verwalter Zugriff auf fremde Schlüssel verschaffen, indem er den Gegenstand
        # in seine eigene Gruppe verschiebt.
        flash(_("Die Gruppe eines Gegenstands mit vertraulichen Angaben dürfen nur Administratoren und "
                "Verwalter ändern, die diese Angaben sehen dürfen – die Gruppe bleibt unverändert."), "warning")
    else:
        asset.group_id = new_group
    if asset.inventory_no.startswith("#"):
        return _("Die Inventarnummer darf nicht mit „#“ beginnen – „#Nummer“ ist für die interne Nummer reserviert.")
    return location_error


def _apply_secret(asset: Asset, allowed: bool) -> list[str]:
    """Vertrauliche Angaben und die Freigabe für die ausleihende Person setzen/löschen.
    Liefert die Protokoll-Aktionen (leer = unverändert).

    `allowed` wird VOR dem Übernehmen des Formulars bestimmt (alte Gruppe), damit ein Gruppenwechsel im selben
    Formular keine Rechte verschafft. Beim Anlegen darf jeder Verwalter Angaben hinterlegen."""
    share = bool(request.form.get("secret_holder_view"))
    if not allowed:
        if (request.form.get("secret") or request.form.get("remove_secret")
                or ("secret_holder_view_shown" in request.form and share != bool(asset.secret_holder_view))):
            flash(_("Vertrauliche Angaben dürfen hier nur Administratoren und Verwalter der Gruppe des "
                    "Gegenstands ändern – sie wurden nicht gespeichert."), "warning")
        return []
    from flask import session
    wants_change = bool(request.form.get("secret") or request.form.get("remove_secret")
                        or ("secret_holder_view_shown" in request.form and share != bool(asset.secret_holder_view)))
    if wants_change and asset.has_secret and current_user.has_2fa and not session.get("mfa_ok"):
        # Wie beim Anzeigen: Ohne bestätigten zweiten Faktor keine Änderung an hinterlegten Angaben – sonst ließe
        # sich der Schlüssel über die Freigabe für die ausleihende Person doch ansehen.
        flash(_("Vertrauliche Angaben ändern geht erst nach Bestätigung deines zweiten Faktors – sie wurden nicht "
                "gespeichert."), "warning")
        return []
    actions = []
    if request.form.get("remove_secret") and asset.has_secret:
        asset.set_secret("")
        asset.secret_holder_view = False
        return ["delete"]
    new = (request.form.get("secret") or "").strip()[:MAX_SECRET]
    if new:
        asset.set_secret(new)
        actions.append("change")
    if share and (asset.quantity or 0) != 1:
        if not asset.secret_holder_view:
            flash(_("Die Freigabe für die ausleihende Person gibt es nur bei Einzelstücken (Anzahl 1) – "
                    "sonst sähen alle, die ein Stück haben, dieselben Angaben."), "warning")
        share = False
    if "secret_holder_view_shown" in request.form and share != bool(asset.secret_holder_view):
        asset.secret_holder_view = share
        actions.append("share_on" if share else "share_off")
    return actions


NUMBER_FIELDS = ("inventory_no", "serial_number")


def find_number(field: str, value: str, exclude_id: int | None = None) -> Asset | None:
    """Gegenstand mit dieser Inventar- bzw. Seriennummer (Groß-/Kleinschreibung egal) – über den GANZEN Bestand,
    denn eine Nummer darf es nur einmal geben."""
    from ..models import number_key
    key = number_key(value)
    if field not in NUMBER_FIELDS or not key:
        return None
    col = Asset.inventory_key if field == "inventory_no" else Asset.serial_key
    with db.session.no_autoflush:
        q = Asset.query.filter(col == key)
        if exclude_id:
            q = q.filter(Asset.id != exclude_id)
        return q.order_by(Asset.id).first()


def _number_label(field: str) -> str:
    return _("Inventarnummer") if field == "inventory_no" else _("Seriennummer")


def _number_conflicts(asset: Asset, previous: dict | None = None) -> dict:
    """Doppelte Inventar-/Seriennummern. Ergebnis:
      error      – neu eingegebene Nummer gibt es schon (Speichern abgelehnt)
      warning    – schon vorher doppelte Nummer, unverändert gelassen (Altbestand: nur Hinweis)
      offers     – {Feld: anderer Gegenstand}: Nummer steht bei einem Gegenstand, den man selbst bearbeiten darf –
                   das Formular bietet an, sie dort zu entfernen und hier einzutragen
      takeovers  – [(Feld, anderer Gegenstand)]: genau das wurde angekreuzt (beim Speichern ausführen)"""
    with db.session.no_autoflush:   # die Eingaben noch nicht schreiben (eindeutiger Index)
        return _number_conflicts_inner(asset, previous or {})


def _number_conflicts_inner(asset: Asset, previous: dict) -> dict:
    from ..models import number_key
    errors, warnings, offers, takeovers = [], [], {}, []
    for field in NUMBER_FIELDS:
        value = getattr(asset, field) or ""
        other = find_number(field, value, asset.id)
        if other is None:
            continue
        mine = current_user.can_manage(other)
        if mine and request.form.get(f"take_{field}") == str(other.id):
            takeovers.append((field, other))
            offers[field] = {"asset": other, "checked": True}   # bei anderem Fehler angehakt wieder anbieten
            continue
        where = _("bei „{name}“", name=other.name) if mine else _("bei einem Gegenstand einer anderen Gruppe")
        if asset.id and number_key(previous.get(field)) == number_key(value):
            warnings.append(_("{label} „{value}“ ist doppelt vergeben (auch {where}) – bitte bei Gelegenheit "
                              "korrigieren.", label=_number_label(field), value=value, where=where))
        else:
            errors.append(_("{label} „{value}“ ist bereits vergeben ({where}). Jede Nummer darf es nur einmal "
                            "geben.", label=_number_label(field), value=value, where=where))
            if mine:
                offers[field] = {"asset": other, "checked": False}
                errors.append(_("Steht sie dort versehentlich, kannst du sie unten übernehmen."))
    return {"error": " ".join(errors) or None, "warning": " ".join(warnings) or None,
            "offers": offers, "takeovers": takeovers}


def _take_over_numbers(takeovers) -> list[str]:
    """Nummern bei den anderen Gegenständen entfernen – vor dem Speichern dieses Gegenstands, im selben
    Schritt (sonst greift der eindeutige Index). Liefert die Hinweise für danach.

    Alles, was danach noch gebraucht wird, vorher lesen und erst am Ende verwerfen: Ein Zugriff auf einen
    verworfenen Gegenstand würde die Sitzung schreiben, solange die zweite Nummer noch doppelt ist."""
    from sqlalchemy import update
    plan = [(field, other, other.id, other.name, getattr(other, field)) for field, other in takeovers]
    with db.session.no_autoflush:          # dieser Gegenstand wird erst danach geschrieben
        for field, _other, ident, _name, _value in plan:
            key = "inventory_key" if field == "inventory_no" else "serial_key"
            db.session.execute(update(Asset).where(Asset.id == ident).values({field: "", key: None})
                               .execution_options(synchronize_session=False))
    for _field, other, *_rest in plan:
        db.session.expire(other)
    return [_("{label} „{value}“ wurde bei „{name}“ entfernt – trag dort bitte die richtige ein.",
              label=_number_label(field), value=value, name=name) for field, _o, _i, name, value in plan]


@bp.route("/gegenstaende/neu", methods=["GET", "POST"])
@manager_required
def create():
    asset = Asset(quantity=1)
    offers = {}
    if request.method == "POST":
        error = _fill_asset(asset)
        _image_from_template(asset)          # auch bei einem Fehler im Formular: Foto bleibt erhalten
        if error:
            flash(error, "error")
        elif not asset.name:
            flash(_("Eine Bezeichnung ist erforderlich."), "error")
        elif (conflict := _number_conflicts(asset))["error"]:
            flash(conflict["error"], "error")
            offers = conflict["offers"]
        else:
            asset.created_by_id = current_user.id
            _apply_image(asset)
            actions = _apply_secret(asset, allowed=True)
            notes = _take_over_numbers(conflict["takeovers"])
            db.session.add(asset)
            try:
                db.session.flush()
                for action in actions:
                    SecretAccess.record(asset, current_user, action)
                db.session.commit()
            except IntegrityError:   # gleichzeitig dieselbe Nummer vergeben (eindeutiger Index)
                db.session.rollback()
                flash(_("Die Inventar- oder Seriennummer wurde gerade anderweitig vergeben – bitte prüfen."), "error")
                return _render_form(asset, is_new=True)
            flash(_("„{name}“ wurde angelegt.", name=asset.name), "success")
            for note in notes:
                flash(note, "warning")
            if request.form.get("next_action") == "another":
                return redirect(url_for("assets.create", vorlage=asset.id))
            return redirect(url_for("assets.detail", asset_id=asset.id))
    else:
        # Vorlage: Angaben eines vorhandenen Gegenstands übernehmen – mit Foto, ohne Nummern und Geheimnis
        template = get_by_id(Asset, parse_id(request.args.get("vorlage")) or 0)
        if template is not None and current_user.can_manage(template):
            for field in ("name", "category", "manufacturer", "model", "location", "description", "image_file"):
                setattr(asset, field, getattr(template, field))
            asset.group_id = template.group_id
        gid = parse_id(request.args.get("gruppe"))
        if gid:
            asset.group_id = gid
        if request.args.get("kategorie"):
            asset.category = request.args.get("kategorie", "")[:200]
    return _render_form(asset, is_new=True, offers=offers)


@bp.route("/gegenstaende/<int:asset_id>/bearbeiten", methods=["GET", "POST"])
@manager_required
def edit(asset_id):
    asset = _get_asset_or_404(asset_id)
    if not current_user.can_manage(asset):
        abort(403)
    if request.method == "POST":
        from .loans import lock_asset
        lock_asset(asset.id)                # gleichzeitige Ausgabe wartet – Anzahl gegen frische Ausleihen prüfen
        db.session.expire(asset, ["loans"])
        may_secret = current_user.can_view_secret(asset)   # vor dem Formular: alte Gruppe zählt
        previous = {f: getattr(asset, f) for f in NUMBER_FIELDS}
        error = _fill_asset(asset)
        if error:
            flash(error, "error")
        elif not asset.name:
            flash(_("Eine Bezeichnung ist erforderlich."), "error")
        elif (conflict := _number_conflicts(asset, previous))["error"]:
            flash(conflict["error"], "error")
            return _render_form(asset, is_new=False, offers=conflict["offers"])
        else:
            warning = conflict["warning"]
            _apply_image(asset)
            for action in _apply_secret(asset, allowed=may_secret):
                SecretAccess.record(asset, current_user, action)
            notes = _take_over_numbers(conflict["takeovers"])
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash(_("Die Inventar- oder Seriennummer wurde gerade anderweitig vergeben – bitte prüfen."), "error")
                _fill_asset(asset)          # Eingaben wieder ins Formular (ohne zu speichern)
                return _render_form(asset, is_new=False)
            flash(_("Änderungen gespeichert."), "success")
            for note in notes:
                flash(note, "warning")
            if warning:
                flash(warning, "warning")
            return redirect(url_for("assets.detail", asset_id=asset.id))
    return _render_form(asset, is_new=False)


def _render_form(asset: Asset, is_new: bool, offers: dict | None = None):
    """Formular anzeigen. Ohne Autoflush: Nach einem Fehler (z. B. doppelte Nummer) soll der nicht gespeicherte
    Stand nicht nebenbei in die Datenbank geschrieben werden."""
    from .locations import names as location_names
    with db.session.no_autoflush:
        mine = manageable_assets_query(current_user)
        html = render_template("assets/form.html", asset=asset, groups=current_user.manageable_groups(),
                               is_new=is_new, location_names=location_names(),
                               categories=category_names(mine), offers=offers or {},
                               known=known_values(mine),
                               template_id=parse_id(request.values.get("vorlage") or request.form.get("image_from")))
    if db.session.dirty or db.session.new:
        db.session.rollback()   # verworfene Eingaben nicht später mit anderen Änderungen speichern
    return html


@bp.route("/gegenstaende/<int:asset_id>/loeschen", methods=["POST"])
@manager_required
def delete(asset_id):
    asset = _get_asset_or_404(asset_id)
    if not current_user.can_manage(asset):
        abort(403)
    from .loans import lock_asset
    lock_asset(asset.id)                    # keine Ausgabe zwischen Prüfung und Löschen
    db.session.expire(asset, ["loans"])
    if asset.active_loans:
        db.session.rollback()
        flash(_("Der Gegenstand ist noch ausgegeben und kann nicht gelöscht werden."), "error")
        return redirect(url_for("assets.detail", asset_id=asset.id))
    if asset.has_secret and not current_user.can_view_secret(asset):
        flash(_("Gegenstände mit vertraulichen Angaben dürfen nur Administratoren und Verwalter der Gruppe "
                "löschen."), "error")
        return redirect(url_for("assets.detail", asset_id=asset.id))
    name = asset.name
    if asset.has_secret:   # Löschen samt Geheimnis im Protokoll festhalten (bleibt nach dem Löschen erhalten)
        SecretAccess.record(asset, current_user, "remove")
        db.session.flush()
    db.session.delete(asset)
    db.session.commit()
    flash(_("„{name}“ wurde gelöscht.", name=name), "success")
    return redirect(url_for("assets.index"))


# ---------------------------------------------------------------- Export

@bp.route("/gegenstaende/export.csv")
@manager_required
def export_csv():
    """Bestand als CSV (für Excel). Vertrauliche Angaben werden nie exportiert."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([_(h) for h in ["ID", "Bezeichnung", "Kategorie", "Hersteller", "Modell", "Seriennummer",
                               "Inventarnummer", "Standort", "Gruppe", "Anzahl", "Verfügbar", "Ausgegeben an"]])

    def cell(v):
        # Schutz vor Formel-Einschleusung in Excel: Inhalte, die mit = + - @ beginnen, als Text markieren
        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + v
        return v

    rows = (manageable_assets_query(current_user)
            .options(selectinload(Asset.group), _with_active_loans().selectinload(Loan.user))
            .order_by(Asset.name, Asset.id))
    for a in rows:
        holders = ", ".join(sorted(l.user.name for l in a.active_loans))
        w.writerow([cell(v) for v in [a.id, a.name, a.category, a.manufacturer, a.model, a.serial_number,
                                      a.inventory_no, a.location, a.group.name if a.group else _("Allgemein"),
                                      a.quantity, a.available, holders]])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=" + (
                        "assetlotse-inventory.csv" if get_locale() == "en" else "assetlotse-bestand.csv")})


# ---------------------------------------------------------------- Etiketten (PDF, 62 × 29 mm)

def _labels_response(items: list, filename: str):
    from ..branding import org_name
    from ..labels import build_pdf, label_for
    pdf = build_pdf([label_for(a, org_name()) for a in items], title=_("Etiketten"))
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": f"inline; filename={filename}",
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@bp.route("/gegenstaende/<int:asset_id>/etikett.pdf")
@manager_required
def label(asset_id):
    """Etikett eines Gegenstands; ?anzahl=n druckt mehrere gleiche (z. B. für Zubehör)."""
    asset = _get_asset_or_404(asset_id)
    if not current_user.can_manage(asset):
        abort(403)
    n = min(50, max(1, request.args.get("anzahl", 1, type=int) or 1))
    name = "label" if get_locale() == "en" else "etikett"
    return _labels_response([asset] * n, f"{name}-{asset.id}.pdf")


@bp.route("/gegenstaende/etiketten.pdf")
@manager_required
def labels():
    """Etiketten für alle Gegenstände der aktuellen Auswahl im Bestand (Filter wie in der Liste)."""
    from ..labels import MAX_LABELS
    query = _apply_filters(manageable_assets_query(current_user), _filter_args())
    items = query.order_by(Asset.inventory_no, Asset.name, Asset.id).limit(MAX_LABELS + 1).all()
    if not items:
        flash(_("Keine Gegenstände für Etiketten gefunden."), "info")
        return redirect(url_for("assets.index", **{k: v for k, v in _filter_args().items() if v}))
    if len(items) > MAX_LABELS:
        flash(_("Zu viele Gegenstände für einen Druck (höchstens {n}). Bitte die Auswahl eingrenzen.",
                n=MAX_LABELS), "error")
        return redirect(url_for("assets.index", **{k: v for k, v in _filter_args().items() if v}))
    return _labels_response(items, "labels.pdf" if get_locale() == "en" else "etiketten.pdf")
