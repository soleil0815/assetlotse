"""Define locations – managers and administrators.

Once at least one location is defined, it is picked from a list when adding items instead of being typed.
Items still store the location as text; renaming updates it on all affected items.
"""
from __future__ import annotations

import re

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from ..i18n import _, _n
from ..models import Asset, Location, db, manageable_assets_query
from . import form_str, get_by_id, manager_required, parse_id

bp = Blueprint("locations", __name__)
MAX_LEN = 200


def _natural(name: str):
    """Natural sort: "Regal 2" before "Regal 10"."""
    return [int(t) if t.isascii() and t.isdecimal() else t.lower() for t in re.split(r"([0-9]+)", name or "")]


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def names() -> list[str]:
    return sorted((l.name for l in Location.query.all()), key=_natural)


def active() -> bool:
    return db.session.query(Location.id).first() is not None


def canonical(value: str) -> str | None:
    """Return the matching defined location (case-insensitive), otherwise None."""
    value = (value or "").strip()
    if not value:
        return ""
    wanted = _norm(value)
    return next((l.name for l in Location.query.all() if _norm(l.name) == wanted), None)


def check(value: str, previous: str = "") -> tuple[str, str | None]:
    """Check the location for an item. Returns (value, error message or None).

    Without defined locations any text is allowed. With a list, only its entries – an already
    stored value that is not (yet) listed may stay unchanged."""
    value = (value or "").strip()[:MAX_LEN]
    if not active():
        return value, None
    c = canonical(value)
    if c is not None:
        return c, None
    if value == (previous or "").strip():
        return value, None
    return previous or "", _("Location “{value}” is not defined. Please choose from the list.", value=value)


def _usage() -> dict[str, int]:
    """Locations in use with their counts – only in the inventory the user may manage (group managers
    do not see free-text locations of other groups)."""
    rows = (manageable_assets_query(current_user).with_entities(Asset.location, func.count(Asset.id))
            .filter(Asset.location.isnot(None), Asset.location != "").group_by(Asset.location).all())
    return {loc: n for loc, n in rows}


def _can_change_all(location: str) -> bool:
    """May the user change all items with this location? (group managers only their own)"""
    total = Asset.query.filter(Asset.location == location).count()
    mine = manageable_assets_query(current_user).filter(Asset.location == location).count()
    return total == mine


def _assets(n: int) -> str:
    return _n("{n} item", "{n} items", n)


def _move_assets(old: str, new: str) -> int:
    n = Asset.query.filter(Asset.location == old).update({Asset.location: new}, synchronize_session=False)
    return n


@bp.route("/standorte")
@manager_required
def index():
    import difflib
    usage = _usage()
    listed = sorted(Location.query.all(), key=lambda l: _natural(l.name))
    exact = {l.name for l in listed}
    by_norm = {_norm(l.name): l for l in listed}
    unlisted = []
    for value, n in sorted(usage.items(), key=lambda x: _natural(x[0])):
        if value in exact:
            continue
        # suggestion: same spelling except case/whitespace, otherwise the closest name (typo)
        hit = by_norm.get(_norm(value))
        if hit is None:
            close = difflib.get_close_matches(_norm(value), list(by_norm), n=1, cutoff=0.75)
            hit = by_norm[close[0]] if close else None
        unlisted.append({"value": value, "count": n, "suggest": hit.id if hit else None,
                         "variant": hit is not None and _norm(value) == _norm(hit.name)})
    return render_template("locations.html", locations=listed, usage=usage, unlisted=unlisted)


@bp.route("/standorte/neu", methods=["POST"])
@manager_required
def create():
    added, skipped = [], []
    existing = {n.lower() for n in names()}
    for line in (request.form.get("names") or "").splitlines():
        name = re.sub(r"\s+", " ", line).strip()[:MAX_LEN]
        if not name:
            continue
        if name.lower() in existing:
            skipped.append(name)
            continue
        db.session.add(Location(name=name, note=form_str("note", 300) if len(added) == 0 else ""))
        existing.add(name.lower())
        added.append(name)
    db.session.commit()
    if added:
        flash(_("{n} location(s) added: {names}", n=len(added),
                names=", ".join(added[:8]) + (" …" if len(added) > 8 else "")), "success")
    if skipped:
        flash(_("Already exist: {names}", names=", ".join(skipped[:8])), "info")
    if not added and not skipped:
        flash(_("Please enter at least one name."), "error")
    return redirect(url_for("locations.index"))


@bp.route("/standorte/<int:loc_id>/umbenennen", methods=["POST"])
@manager_required
def rename(loc_id):
    loc = get_by_id(Location, loc_id)
    if loc is None:
        return redirect(url_for("locations.index"))
    new = re.sub(r"\s+", " ", form_str("name", MAX_LEN)).strip()
    if not new:
        flash(_("The name must not be empty."), "error")
    elif new.lower() != loc.name.lower() and canonical(new) is not None:
        flash(_("“{new}” already exists. To merge, delete “{old}” after the items have been moved.",
                new=new, old=loc.name), "error")
    elif not _can_change_all(loc.name):
        flash(_("“{name}” is also used by items of other groups – only administrators "
                "or managers without a group can rename it.", name=loc.name), "error")
    else:
        old = loc.name
        n = _move_assets(old, new)
        loc.name = new
        loc.note = form_str("note", 300)
        db.session.commit()
        if n and old != new:
            flash(_("“{old}” is now called “{new}” – {assets} updated.", old=old, new=new, assets=_assets(n)), "success")
        else:
            flash(_("“{old}” is now called “{new}”.", old=old, new=new), "success")
    return redirect(url_for("locations.index"))


@bp.route("/standorte/<int:loc_id>/loeschen", methods=["POST"])
@manager_required
def delete(loc_id):
    loc = get_by_id(Location, loc_id)
    if loc is None:
        return redirect(url_for("locations.index"))
    n = Asset.query.filter(Asset.location == loc.name).count()
    if n:
        flash(_("“{name}” is still assigned to {assets}. Rename it first or move the items.",
                name=loc.name, assets=_assets(n)), "error")
    else:
        db.session.delete(loc)
        db.session.commit()
        flash(_("Location “{name}” deleted.", name=loc.name), "success")
    return redirect(url_for("locations.index"))


@bp.route("/standorte/bestand", methods=["POST"])
@manager_required
def adopt():
    """Adopt a value found in the inventory but not defined, or replace it with a location."""
    old = (request.form.get("value") or "")[:MAX_LEN]   # deliberately not stripped: "Regal 1 " with a trailing space is a typo too
    target = request.form.get("target", "")
    # count only in one's own inventory – otherwise locations of other groups could be guessed
    count = manageable_assets_query(current_user).filter(Asset.location == old).count() if old else 0
    if not old or not count:
        flash(_("This entry no longer occurs in the inventory."), "info")
    elif target == "__neu__" and not _can_change_all(old):
        flash(_("“{name}” is also used by items of other groups – only administrators "
                "or managers without a group can replace it.", name=old), "error")
    elif target == "__neu__":
        clean = re.sub(r"\s+", " ", old).strip()
        if canonical(clean) is None:
            db.session.add(Location(name=clean))
        if clean != old:
            _move_assets(old, canonical(clean) or clean)
        db.session.commit()
        flash(_("“{name}” is now a defined location.", name=clean), "success")
    else:
        loc = get_by_id(Location, parse_id(target) or 0)
        if loc is None:
            flash(_("Please choose a location."), "error")
        elif not _can_change_all(old):
            flash(_("“{name}” is also used by items of other groups – only administrators "
                    "or managers without a group can replace it.", name=old), "error")
        else:
            n = _move_assets(old, loc.name)
            db.session.commit()
            flash(_("“{old}” replaced by “{new}” – {assets} updated.", old=old, new=loc.name, assets=_assets(n)), "success")
    return redirect(url_for("locations.index"))
