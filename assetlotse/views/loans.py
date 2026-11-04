"""Checkout, return, extension and the issue desk."""
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
                      catalog_hidden, lendable_clause, manageable_assets_query)
from ..mailer import notify_loan, notify_return, send_manual_reminder
from . import get_by_id, manager_required, parse_id, safe_next

bp = Blueprint("loans", __name__)
log = logging.getLogger(__name__)
PER_PAGE = 50


def _back(default: str):
    return redirect(safe_next(request.form.get("next")) or default)


def lock_asset(asset_id: int) -> None:
    """Acquire a write lock on this item until the end of the transaction – before any check.

    The no-op update (setting a value to itself) starts the write transaction on SQLite
    (RESERVED lock: concurrent checkouts wait for each other) and locks the item's row on
    MySQL/PostgreSQL; SELECT … FOR UPDATE adds extra safety there (SQLite ignores it)."""
    db.session.execute(update(Asset).where(Asset.id == asset_id).values(updated_at=Asset.updated_at)
                       .execution_options(synchronize_session=False))
    if db.session.get_bind().dialect.name != "sqlite":
        db.session.query(Asset.id).filter(Asset.id == asset_id).with_for_update().first()


DEFAULT_DUE = object()   # "use default loan period" (None means open-ended)


def lend_asset(asset: Asset, borrower: User, due=DEFAULT_DUE, note: str = "") -> tuple[bool, str]:
    """Central checkout logic including all checks."""
    if not borrower.active:
        return False, _("{name} is blocked.", name=borrower.name)
    if not asset.lendable:
        return False, _("“{title}” belongs to the group “{group}” – its items are not lent out.",
                        title=asset.name, group=asset.group.name)
    if not borrower.can_borrow(asset):
        grp = asset.group.name if asset.group else "–"
        return False, _("{name} is not a member of the group “{group}” and may not receive this item.", name=borrower.name, group=grp)
    limit = get_int("max_loans", 0)
    try:
        lock_asset(asset.id)
        if limit and db.session.get_bind().dialect.name != "sqlite":
            # per-person maximum: lock the person too, otherwise two concurrent checkouts of different
            # items would both count too few (SQLite writes sequentially anyway)
            db.session.query(User.id).filter(User.id == borrower.id).with_for_update().first()
        # from here on, count fresh from the database – concurrent requests wait until commit.
        quantity = db.session.query(Asset.quantity).filter(Asset.id == asset.id).scalar() or 0
        active = dict(db.session.query(Loan.user_id, func.count(Loan.id))
                      .filter(Loan.asset_id == asset.id, Loan.returned_at.is_(None)).group_by(Loan.user_id).all())
        if borrower.id in active:
            db.session.rollback()
            return False, _("{name} already has this item.", name=borrower.name)
        if sum(active.values()) >= quantity:
            db.session.rollback()
            return False, _("No piece available.")
        if limit and count_active_loans(borrower.id) >= limit:
            db.session.rollback()
            return False, _("{name} already has the maximum of {limit} items.", name=borrower.name, limit=limit)
        loan = Loan(asset_id=asset.id, user_id=borrower.id, issued_by_id=current_user.id,
                    due_at=default_due_date() if due is DEFAULT_DUE else due, note=note[:500])
        db.session.add(loan)
        db.session.commit()
    except IntegrityError:   # unique index: the same person has just received this item
        db.session.rollback()
        return False, _("{name} already has this item.", name=borrower.name)
    except OperationalError:  # e.g. database locked for too long
        db.session.rollback()
        return False, _("The database is busy right now. Please try again in a moment.")
    notify_loan(loan)
    if loan.due_at is None:
        return True, _("“{title}” issued to {name} (open-ended).", title=asset.name, name=borrower.name)
    return True, _("“{title}” issued to {name} – return by {date}.", title=asset.name, name=borrower.name,
                   date=format_day(loan.due_at))


def return_loan(loan: Loan) -> str:
    # only return if still open – otherwise a double click or two windows would return twice (two emails)
    res = db.session.execute(update(Loan).where(Loan.id == loan.id, Loan.returned_at.is_(None))
                             .values(returned_at=datetime.now(), returned_by_id=current_user.id))
    db.session.commit()
    db.session.refresh(loan)
    if res.rowcount != 1:
        return _("“{title}” had already been returned.", title=loan.asset.name)
    notify_return(loan)
    return _("“{title}” returned by {name}.", title=loan.asset.name, name=loan.user.name)


@bp.route("/gegenstaende/<int:asset_id>/ausgeben", methods=["POST"])
@login_required
def borrow(asset_id):
    asset = get_by_id(Asset, asset_id) or abort(404)
    if catalog_hidden(current_user):
        abort(404)   # without inventory access: no self-checkout and no hint whether the number exists
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
            flash(_("Invalid return date."), "error")
            return _back(url_for("assets.detail", asset_id=asset.id))
        if isinstance(due, date) and not (date.today() <= due <= date.today() + timedelta(days=3650)):
            flash(_("The due date must be between today and ten years from now."), "error")
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
        flash(_("This loan has already been closed."), "info")
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
    if not manager and not loan.asset.lendable:
        # group with "no lending": returning yes, self-extending no (managers decide themselves)
        flash(_("Items of the group “{group}” are not lent out "
                "– please return it or contact the management.", group=loan.asset.group.name), "error")
        return _back(url_for("loans.index"))
    max_ext, days = get_int("max_extensions", 2), get_int("extension_days", 14)
    for _attempt in range(3):
        if loan.returned_at:
            flash(_("This loan has already been closed."), "info")
            break
        if loan.due_at is None:
            flash(_("This loan is open-ended – no extension needed."), "info")
            break
        if not manager and loan.extensions >= max_ext:
            flash(_("The maximum number of extensions has been reached. Please contact the management team."), "error")
            break
        old_ext = loan.extensions
        new_due = max(loan.due_at, date.today()) + timedelta(days=days)
        # apply only if nobody else has extended or returned it in the meantime
        res = db.session.execute(
            update(Loan).where(Loan.id == loan.id, Loan.returned_at.is_(None), Loan.extensions == old_ext)
            .values(extensions=Loan.extensions + 1, due_at=new_due).execution_options(synchronize_session=False))
        db.session.commit()
        if res.rowcount == 1:
            flash(_("Extended until {date}.", date=format_day(new_due)), "success")
            break
        db.session.refresh(loan)   # concurrent change – check again with the new state
    return _back(url_for("loans.index"))


@bp.route("/ausleihen/<int:loan_id>/erinnern", methods=["POST"])
@manager_required
def remind(loan_id):
    loan = get_by_id(Loan, loan_id) or abort(404)
    if not current_user.can_manage(loan.asset):
        abort(403)
    if loan.returned_at:
        flash(_("This loan has already been closed."), "info")
        return _back(url_for("loans.index"))
    ok, msg = send_manual_reminder(loan)
    flash(_("Reminder sent to {email}.", email=loan.user.email) if ok
          else _("No reminder sent: {reason}", reason=msg),
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
    if view == "ueberfaellig":   # open-ended loans (due_at empty) are never overdue
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


# ---------------------------------------------------------------- Issue desk

def _find_assets(code: str) -> tuple[list, bool]:
    """Items for a scanned/typed code. Returns (matches, exact).

    "#12" is always the internal number (as printed on labels without an inventory number) – never a name search.
    Then inventory number (exact) → serial number (exact) → name (substring, for selection only).
    `exact` is False for name-search matches: then nothing is issued or returned automatically."""
    code = (code or "").strip()
    if not code:
        return [], False
    base = manageable_assets_query(current_user)
    if re.fullmatch(r"#\d+", code):
        ident = parse_id(code[1:])
        return (base.filter(Asset.id == ident).all() if ident else []), True
    # compare via the normalized keys (like suggestions and duplicate check) – "Ü-12" = "ü-12" too
    key = number_key(code)
    for col in (Asset.inventory_key, Asset.serial_key):
        found = base.filter(col == key).limit(20).all() if key else []
        if found:
            return found, True
    return base.filter(Asset.name.ilike(f"%{code}%")).limit(20).all(), False


def ad_search_available() -> bool:
    """People search in Active Directory: only with AD enabled and a service account."""
    from ..ldap_auth import ldap_enabled
    return ldap_enabled() and bool(get_setting("ldap_bind_dn"))


def _find_people(query: str):
    """Find a person for checkout. Returns (selected, local matches, AD matches, AD error message).

    Exact username → selected immediately. Otherwise local accounts (name/username contains the text) and – if
    configured – people from Active Directory who could sign in under the AD rules but do not have an
    AssetLotse account yet. A person is selected automatically only if there is exactly one match overall."""
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
        # fast: cache (synced in the background); only without matches also search AD live
        ad_people = ad_cache.search(query, limit=11)
        ad_more = len(ad_people) > 10
        ad_people = ad_people[:10]
    if ad_search_available() and len(query) >= 2 and not ad_people:
        from ..ldap_auth import Directory, LDAPError
        log.info("AD people search at the desk by %s: %r", current_user.username, query)
        try:
            known = {u for (u,) in db.session.query(User.username).all()}
            ad_people, ad_more = Directory().search_users(query, exclude=known)
        except LDAPError as exc:
            log.warning("AD people search failed: %s", exc)
            # details (bind errors, DNs …) only for administrators – managers get a generic message
            ad_error = str(exc) if current_user.is_admin else _("Active Directory cannot be reached right now.")
        except Exception:  # noqa: BLE001 – an AD problem must not break the desk
            log.exception("AD people search failed")
            ad_error = _("Active Directory cannot be reached right now.")
    if len(people) == 1 and not ad_people and not ad_more:
        return people[0], [], [], ad_error
    if ad_more:
        flash(_("There may be more people in Active Directory – please search more precisely."), "info")
    elif len(people) > 10:
        flash(_("More than 10 accounts match – please search more precisely."), "info")
    elif not people and not ad_people:
        flash(_("No person found for “{query}”.", query=query), "error")
    return None, people[:10], ad_people, ad_error


@bp.route("/theke/personen")
@manager_required
def people_suggest():
    """Type-ahead suggestions for the person field at the desk: AssetLotse accounts and – from the cache –
    AD people who have never signed in. AD is never searched live here (too slow)."""
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
        preset = parse_id(request.args.get("gegenstand"))
        for u in users:
            items.append({"name": u.name, "sub": u.username + (" · AD" if u.auth_source == "ldap" else ""),
                          "url": url_for("loans.desk", modus="ausgabe", benutzer=u.username, gegenstand=preset)})
        if ad_search_available() and ad_cache.ready():
            for p in ad_cache.search(q, limit=max(0, 10 - len(items)) or 1):
                items.append({"name": p.display_name or p.username,
                              "sub": " · ".join(x for x in (p.username, p.email) if x),
                              "badge": _("AD – no account yet"),
                              "post": {"url": url_for("loans.desk_ad_account", gegenstand=preset),
                                       "username": p.username}})
    resp = jsonify({"items": items[:10]})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/theke/ad-konto", methods=["POST"])
@manager_required
def desk_ad_account():
    """Take over a person's account from Active Directory (without them ever having signed in).

    The server checks the person again in AD – form values are never trusted: the account must exist,
    be active and receive a role under the AD rules."""
    from ..ldap_auth import Directory, LDAPAmbiguous, LDAPError
    from .main import sync_ldap_user
    username = (request.form.get("username") or "").strip()[:200]
    preset = parse_id(request.values.get("gegenstand"))       # from the inventory: the item is already chosen
    back = url_for("loans.desk", modus="ausgabe", gegenstand=preset)
    if not username or not ad_search_available():
        abort(400)
    try:
        info = Directory().check_account(username)
    except LDAPAmbiguous:
        flash(_("“{name}” is not unique in Active Directory – please inform the administrators.",
                name=username), "error")
        return redirect(back)
    except LDAPError as exc:
        log.warning("AD account takeover %s failed: %s", username, exc)
        flash(_("Active Directory: {error}", error=str(exc)) if current_user.is_admin
              else _("Active Directory cannot be reached right now."), "error")
        return redirect(back)
    except Exception as exc:  # noqa: BLE001 – e.g. network error from ldap3: notice instead of error page
        log.exception("AD account takeover %s failed", username)
        flash(_("Active Directory: {error}", error=str(exc)) if current_user.is_admin
              else _("Active Directory cannot be reached right now."), "error")
        return redirect(back)
    if info is None or info.role is None:
        from .. import ad_cache
        ad_cache.forget(username)          # stop showing it until the next sync
        db.session.commit()
        flash(_("“{name}” is not (or no longer) permitted for AssetLotse in Active Directory.", name=username), "error")
        return redirect(back)
    from .main import find_ldap_account, guid_conflict, legacy_locked
    existing = find_ldap_account(info)
    if legacy_locked(info, existing):
        flash(_("There is a locked older account for “{name}” without an AD identifier – an administrator "
                "must check whether it is the same person (activate or rename the account).", name=info.username), "error")
        return redirect(back)
    if guid_conflict(info, existing):
        flash(_("In AssetLotse, the username “{name}” belongs to an earlier account of a different "
                "person. Please rename the old account under Administration → Users.", name=info.username), "error")
        return redirect(back)
    if existing is not None and existing.auth_source != "ldap":
        flash(_("A local account “{name}” already exists – it will not be linked to Active Directory.",
                name=existing.username), "error")
        return redirect(back)
    previous_role = existing.role if existing is not None else None
    try:
        # no autoflush during sync: otherwise a concurrent double click would already fail here
        from .main import may_reactivate
        reactivate = existing is not None and may_reactivate(existing)   # check before syncing
        with db.session.no_autoflush:
            user = sync_ldap_user(info, existing)
            if reactivate:                      # locked by the AD sync, permitted again according to AD
                user.active, user.deactivated_by_sync = True, False
    except IntegrityError:
        db.session.rollback()
        return redirect(url_for("loans.desk", modus="ausgabe", benutzer=info.username, gegenstand=preset))
    # The AD role applies only from the person's first own sign-in (where it is determined anew anyway).
    # Until then an account taken over at the desk is only a staff member – so it does not receive, e.g.,
    # manager/admin emails with personal data while the person has never used AssetLotse.
    user.role = previous_role or ROLE_READER
    try:
        db.session.commit()
    except IntegrityError:          # double click: the account was just created
        db.session.rollback()
        user = User.query.filter(func.lower(User.username) == info.username.lower()).first()
        if user is None:
            abort(409)
        return redirect(url_for("loans.desk", modus="ausgabe", benutzer=user.username, gegenstand=preset))
    from .. import ad_cache
    ad_cache.forget(user.username)         # now has an account – no longer belongs in the cache
    db.session.commit()
    log.info("AD account %s taken over at the issue desk by %s (role %s)", user.username,
             current_user.username, user.role)
    if existing is None:
        flash(_("{name} taken over from Active Directory.", name=user.name), "success")
    return redirect(url_for("loans.desk", modus="ausgabe", benutzer=user.username, gegenstand=preset))


STOCK_LIMIT = 30


def _stock_args() -> dict:
    """Parameters of the inventory search at the desk (search text, category, available only)."""
    free = request.values.get("frei", "1")
    return {"suche": (request.values.get("suche") or "").strip()[:100],
            "kategorie": (request.values.get("kategorie") or "").strip()[:200],
            "frei": "0" if free == "0" else "1"}


def _stock_search(stock: dict, borrower: User | None = None):
    """Search the inventory at the desk – only items this manager may issue and (if a person is
    selected) that this person may receive. None while there is neither search text nor category."""
    if not stock["suche"] and not stock["kategorie"]:
        return None
    from sqlalchemy.orm import selectinload
    from .assets import _apply_filters
    f = {"q": stock["suche"], "gruppe": "", "kategorie": stock["kategorie"], "standort": "",
         "status": "verfuegbar" if stock["frei"] == "1" else ""}
    query = _apply_filters(manageable_assets_query(current_user).filter(lendable_clause()), f)
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
    # coming from the inventory: the item is already chosen, only the person is missing
    preset = _preset_asset() if mode == "ausgabe" else None

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
            if preset is not None and preset.id == asset.id:
                # came from the inventory: back to the item (on error, to person selection)
                if ok:
                    return redirect(url_for("assets.detail", asset_id=asset.id))
                return redirect(url_for("loans.desk", modus="ausgabe", gegenstand=asset.id,
                                        benutzer=borrower.username))
            # keep the inventory search – so the next piece can be picked right away
            return redirect(url_for("loans.desk", modus="ausgabe", benutzer=borrower.username,
                                    **{k: v for k, v in _stock_args().items() if v}))

    if request.method == "POST" and "code" in request.form:
        # remember the "issue/return immediately" checkbox – also across return button, suggestions and result list
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
                                    gegenstand=preset.id if preset is not None else None,
                                    **{k: v for k, v in _stock_args().items() if v and k != "frei"}))
        if not assets:
            flash(_("No item found for “{code}”.", code=code), "error")

    stock = _stock_args()
    stock_results = _stock_search(stock, borrower) if mode == "ausgabe" else None
    from .assets import category_names
    # group managers only see loans and groups they manage themselves
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
                           auto=session.get("desk_auto", "1"), preset=preset)


def _preset_asset() -> Asset | None:
    """Item to be issued from the inventory (?gegenstand=<id>) – only if this manager may issue it;
    otherwise the parameter is silently ignored."""
    ident = parse_id(request.values.get("gegenstand"))
    asset = get_by_id(Asset, ident) if ident else None
    if asset is None or not current_user.can_manage(asset):
        return None
    if not asset.lendable:
        flash(_("“{title}” belongs to the group “{group}” – its items are not lent out.",
                title=asset.name, group=asset.group.name), "error")
        return None
    return asset
