"""Retention periods (GDPR: storage limitation, Art. 5(1)(e)).

Completed loans are deleted after the configured period (default 12 months after the return). Per item
only the count is kept (Asset.archived_loans) so the statistics stay correct. Mail log entries are deleted
after the same period – except those for running loans, because AssetLotse uses them to tell how many
overdue notices have been sent. The log of confidential data (who viewed/changed what and when) is also
deleted after this period.

Running and overdue loans are never deleted.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select

from .models import Asset, Loan, MailLog, SecretAccess, db, get_int, set_setting

log = logging.getLogger(__name__)
BATCH = 500


def retention_months() -> int:
    return max(0, get_int("retention_months", 12))


def cutoff_date(now: datetime | None = None, months: int | None = None) -> datetime | None:
    months = retention_months() if months is None else months
    if months <= 0:
        return None
    return (now or datetime.now()) - timedelta(days=round(months * 365.25 / 12))


def archive_loans(loan_ids: list[int]) -> int:
    """Delete loans and count them per item. Expects completed loans only."""
    total = 0
    for i in range(0, len(loan_ids), BATCH):
        ids = loan_ids[i:i + BATCH]
        per_asset = (db.session.query(Loan.asset_id, func.count(Loan.id))
                    .filter(Loan.id.in_(ids), Loan.returned_at.isnot(None)).group_by(Loan.asset_id).all())
        for asset_id, n in per_asset:
            db.session.query(Asset).filter(Asset.id == asset_id).update(
                {Asset.archived_loans: func.coalesce(Asset.archived_loans, 0) + n}, synchronize_session=False)
        db.session.query(MailLog).filter(MailLog.loan_id.in_(ids)).update(
            {MailLog.loan_id: None}, synchronize_session=False)
        total += db.session.query(Loan).filter(Loan.id.in_(ids), Loan.returned_at.isnot(None)).delete(
            synchronize_session=False)
    return total


class OpenLoansError(Exception):
    """Something is still issued to the person – then nothing is deleted."""


def delete_user(user) -> None:
    """Delete a user together with their completed loans (only counted per item, statistics stay).

    If a loan is still running (even one just created at the desk), nothing is deleted: OpenLoansError.
    Does not commit."""
    db.session.flush()
    if db.session.query(Loan.id).filter(Loan.user_id == user.id, Loan.returned_at.is_(None)).first():
        raise OpenLoansError(user.username)
    ids = [i for (i,) in db.session.query(Loan.id).filter(Loan.user_id == user.id, Loan.returned_at.isnot(None))]
    archive_loans(ids)
    db.session.query(Loan).filter(Loan.user_id == user.id, Loan.returned_at.isnot(None)).delete(
        synchronize_session=False)
    # remove already loaded loans from the session – otherwise the ORM still wants to change them on delete
    for obj in [o for o in db.session.identity_map.values() if isinstance(o, Loan) and o.id in set(ids)]:
        db.session.expunge(obj)
    db.session.expire(user, ["loans"])
    db.session.delete(user)


def run_retention(now: datetime | None = None) -> dict:
    """Apply the retention period. Returns {'loans': n, 'mails': m, 'secret_log': s} or {'off': True, …}."""
    cutoff = cutoff_date(now)
    if cutoff is None:
        return {"off": True, "loans": 0, "mails": 0, "secret_log": 0}
    old_ids = [i for (i,) in db.session.query(Loan.id).filter(Loan.returned_at.isnot(None),
                                                             Loan.returned_at < cutoff)]
    loans = archive_loans(old_ids)
    active = select(Loan.id).where(Loan.returned_at.is_(None))
    mails = (db.session.query(MailLog)
             .filter(MailLog.created_at < cutoff,
                     (MailLog.loan_id.is_(None)) | (MailLog.loan_id.notin_(active)))
             .delete(synchronize_session=False))
    # Log of confidential data (who viewed it when) – same period
    secrets = (db.session.query(SecretAccess).filter(SecretAccess.at < cutoff)
               .delete(synchronize_session=False))
    db.session.commit()
    if loans or mails or secrets:
        log.info("Retention period: deleted %d old loan(s), %d email log entries and %d confidential-data "
                 "log entries", loans, mails, secrets)
    return {"loans": loans, "mails": mails, "secret_log": secrets}


def format_result(res: dict) -> str:
    if res.get("off"):
        return "Retention period is turned off."   # translation key – shown via _() in views/admin.py
    # stored format (retention_last_result) – parsed by views/admin.py _retention_message, keep
    return (f"Gelöscht: {res['loans']} alte Ausleihe(n), {res['mails']} Einträge im E-Mail-Protokoll, "
            f"{res.get('secret_log', 0)} im Zugriffsprotokoll")


def run_daily(now: datetime | None = None) -> dict | None:
    """Once a day (only once even with several server processes)."""
    from .backup import supported as backup_supported
    from .mailer import _claim_today
    from .models import get_bool, get_int
    now = now or datetime.now()
    if get_bool("backup_enabled") and backup_supported():
        if now.hour < get_int("backup_hour", 2):
            return None        # delete only after the daily backup
        from .models import get_setting
        ok, failed = get_setting("backup_last_ok") or "", get_setting("backup_last_error") or ""
        if not ok.startswith(now.date().isoformat()) or failed > ok:
            return None        # no successful backup today or the last one failed – better delete nothing
    if not _claim_today(now.date().isoformat(), key="retention_last_run"):
        return None
    res = run_retention(now)
    set_setting("retention_last_result", f"{now:%d.%m.%Y %H:%M}: {format_result(res)}")
    db.session.commit()
    return res
