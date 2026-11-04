"""Sending email via SMTP and automatic notifications.

Notifications
  * loan     – confirmation when an item is issued
  * return   – confirmation when an item is returned
  * reminder – reminder X days before the due date (once per loan)
  * overdue  – overdue notice, repeated every N days, at most M times
  * digest   – daily summary of overdue loans for managers
  * manual   – triggered by hand by a manager
Reminders are combined into one mail per user. The log (MailLog) prevents sending twice –
even when several server processes are running.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from flask import current_app, render_template
from sqlalchemy import func, update

from .i18n import _
from .models import (ROLE_ADMIN, ROLE_MANAGER, Loan, MailLog, Setting, User, db, get_bool, get_int,
                     get_setting, set_setting)

log = logging.getLogger(__name__)


class MailError(Exception):
    pass


def _org_name() -> str:
    from .branding import org_name
    return org_name()


# ---------------------------------------------------------------- Sending

@dataclass
class SmtpConfig:
    host: str
    port: int
    security: str
    user: str
    password: str
    sender: str
    sender_name: str
    reply_to: str
    verify: bool
    timeout: int = 15

    @classmethod
    def from_settings(cls, overrides: dict | None = None) -> "SmtpConfig":
        o = overrides or {}

        def s(key):
            return o[key] if key in o else get_setting(key)

        try:
            port = int(s("smtp_port") or 0)
        except ValueError:
            port = 0
        security = s("smtp_security") or "starttls"
        return cls(host=(s("smtp_host") or "").strip(), port=port or {"ssl": 465, "starttls": 587}.get(security, 25),
                   security=security, user=(s("smtp_user") or "").strip(), password=s("smtp_password") or "",
                   sender=(s("smtp_from") or "").strip(),
                   sender_name=(s("smtp_from_name") or _org_name()).strip(),
                   reply_to=(s("smtp_reply_to") or "").strip(),
                   verify=(s("smtp_verify_cert") or "1") in ("1", "true", "on"))


def _ssl_context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def smtp_connect(cfg: SmtpConfig) -> smtplib.SMTP:
    if not cfg.host:
        raise MailError(_("No SMTP server configured."))
    try:
        if cfg.security == "ssl":
            smtp = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout, context=_ssl_context(cfg.verify))
        else:
            smtp = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
            smtp.ehlo()
            if cfg.security == "starttls":
                if not smtp.has_extn("starttls"):
                    smtp.quit()
                    raise MailError(_("The server does not offer STARTTLS. Set security to “none” or “SSL/TLS”."))
                smtp.starttls(context=_ssl_context(cfg.verify))
                smtp.ehlo()
        if cfg.user:
            smtp.login(cfg.user, cfg.password)
        return smtp
    except MailError:
        raise
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(_("Sign-in to the SMTP server failed ({code}): {error}",
                          code=exc.smtp_code, error=_decode(exc.smtp_error))) from exc
    except ssl.SSLCertVerificationError as exc:
        raise MailError(_("The SMTP server's certificate is not trusted ({reason}). If necessary, "
                          "turn off “Verify certificate” (internal use only).", reason=exc.verify_message)) from exc
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(_("SMTP server {host}:{port} – {error}", host=cfg.host, port=cfg.port,
                          error=f"{exc.__class__.__name__}: {exc}")) from exc


def _decode(v):
    return v.decode(errors="replace") if isinstance(v, bytes) else str(v)


def _one_line(value: str) -> str:
    """Header lines must not contain line breaks (e.g. from an item name)."""
    return " ".join(str(value or "").split())


def build_message(cfg: SmtpConfig, to: str, subject: str, text: str, html: str | None = None) -> EmailMessage:
    if not cfg.sender:
        raise MailError(_("No sender address configured."))
    if not valid_email(to):
        raise MailError(_("Invalid recipient address: {address}", address=_one_line(to)[:100]))
    subject = _one_line(subject)
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.sender_name, cfg.sender))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.sender.split("@")[-1] or None)
    msg["Auto-Submitted"] = "auto-generated"
    if cfg.reply_to:
        msg["Reply-To"] = cfg.reply_to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def send_mail(to: str, subject: str, text: str, html: str | None = None, *, kind: str,
              loan_ids: list[int] | None = None, user_id: int | None = None,
              cfg: SmtpConfig | None = None, smtp: smtplib.SMTP | None = None) -> tuple[bool, str]:
    """Send a mail and log it (one log row per loan)."""
    cfg = cfg or SmtpConfig.from_settings()
    error = ""
    try:
        msg = build_message(cfg, to, subject, text, html)
        own = smtp is None
        conn = smtp or smtp_connect(cfg)
        try:
            refused = conn.send_message(msg)
            if refused:
                raise MailError(_("Recipient rejected: {recipients}", recipients=", ".join(refused)))
        finally:
            if own:
                try:
                    conn.quit()
                except Exception:  # noqa: BLE001
                    pass
    except MailError as exc:
        error = str(exc)
    except ValueError as exc:   # invalid header or similar – never let it abort the request
        error = _("Invalid message: {error}", error=exc)
    except (OSError, smtplib.SMTPException) as exc:
        error = f"{exc.__class__.__name__}: {_decode(getattr(exc, 'smtp_error', exc))}"
    status = "error" if error else "sent"
    for lid in (loan_ids or [None]):
        db.session.add(MailLog(kind=kind, to_addr=to[:300], subject=subject[:300], status=status,
                               error=error[:1000], loan_id=lid, user_id=user_id))
    db.session.commit()
    if error:
        log.warning("Mail to %s (%s) failed: %s", to, kind, error)
    return not error, error


# ---------------------------------------------------------------- Content

def _ctx(**extra) -> dict:
    from .branding import mail_colors
    base = (get_setting("app_base_url") or "").rstrip("/")
    logo = get_setting("brand_logo")
    # Logo only as PNG (many mail clients don't show SVG) and only with a known base URL
    logo_url = f"{base}/branding/{logo}" if base and logo and not logo.endswith(".svg") else ""
    return {"org_name": _org_name(), "base_url": base,
            "mail_logo_url": logo_url, "mail_logo_plate": get_setting("brand_logo_plate") == "1",
            "mail_show_text": get_setting("brand_logo_text") != "0", **mail_colors(),
            "footer": get_setting("mail_footer"), "today": date.today(),
            "imprint_url": get_setting("imprint_url"), "privacy_url": get_setting("privacy_url"),
            "max_ext": get_int("max_extensions", 2), "self_extend": get_bool("self_extend"), **extra}


def render_mail(template: str, **ctx) -> tuple[str, str, str]:
    """Return (subject, text, HTML) from templates/mail/<template>.txt/.html.

    The first line of the .txt template is the subject."""
    from .i18n import force_locale, user_language
    lang = ctx.pop("lang", None) or (user_language(ctx["user"]) if ctx.get("user") is not None else None)
    if not lang:   # without a recipient (e.g. test mail): language of the current request
        from .i18n import get_locale
        lang = get_locale()
    # Only these mails can be turned off in the profile ("Email notifications") – only they carry the note about it
    ctx.setdefault("notification", template in ("loan", "return", "reminder", "digest",
                                                        "registration_admin", "approved"))
    with force_locale(lang):   # in the recipient's language
        c = _ctx(**ctx)
        raw = render_template(f"mail/{template}.txt", **c).strip("\n")
        subject, _sep, body = raw.partition("\n")
        html = render_template(f"mail/{template}.html", subject=subject.strip(), **c)
    return subject.strip(), body.strip() + "\n", html


EMAIL_RE = __import__("re").compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")


def valid_email(addr: str) -> bool:
    """Exactly one simple address – no commas/spaces (otherwise several recipients would be possible)."""
    return bool(addr) and len(addr) <= 254 and bool(EMAIL_RE.match(addr))


def mail_active() -> bool:
    return get_bool("mail_enabled") and bool(get_setting("smtp_host"))


def can_notify(user: User | None) -> bool:
    return bool(user and user.active and valid_email(user.email or "") and user.email_notifications)


def _send_async(fn, *args):
    """Send mail in the background so issuing/returning does not wait for the SMTP server."""
    app = current_app._get_current_object()
    if app.config.get("MAIL_SYNC") or app.config.get("TESTING"):
        fn(*args)
        return

    def run():
        with app.app_context():
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                log.exception("Background mail sending failed")

    threading.Thread(target=run, daemon=True).start()


def _loan_mail(loan_id: int, template: str, kind: str):
    loan = db.session.get(Loan, loan_id)
    if loan is None or not can_notify(loan.user):
        return
    subject, text, html = render_mail(template, loan=loan, user=loan.user, asset=loan.asset)
    send_mail(loan.user.email, subject, text, html, kind=kind, loan_ids=[loan.id], user_id=loan.user_id)


def notify_loan(loan: Loan):
    if mail_active() and get_bool("mail_on_loan") and can_notify(loan.user):
        _send_async(_loan_mail, loan.id, "loan", "loan")


def notify_return(loan: Loan):
    if mail_active() and get_bool("mail_on_return") and can_notify(loan.user):
        _send_async(_loan_mail, loan.id, "return", "return")


def send_manual_reminder(loan: Loan) -> tuple[bool, str]:
    if not mail_active():
        return False, _("Email sending is not set up.")
    if not can_notify(loan.user):
        return False, _("{name} has no email address or has turned off notifications.", name=loan.user.name)
    if loan.due_at is None:
        return False, _("The loan is open-ended.")
    subject, text, html = render_mail("reminder", user=loan.user, due_soon=[] if loan.is_overdue else [loan],
                                      overdue=[loan] if loan.is_overdue else [])
    return send_mail(loan.user.email, subject, text, html, kind="manual", loan_ids=[loan.id], user_id=loan.user_id)


# ---------------------------------------------------------------- Daily reminders

def _sent(loan_id: int, kind: str):
    return MailLog.query.filter_by(loan_id=loan_id, kind=kind, status="sent")


def collect_due(today: date | None = None) -> dict[int, dict]:
    """Determine per user which loans need a reminder or an overdue notice."""
    today = today or date.today()
    per_user: dict[int, dict] = {}
    # Open-ended loans (without a due date) need no reminder
    active = Loan.query.filter(Loan.returned_at.is_(None), Loan.due_at.isnot(None)).all()
    rem_days = get_int("mail_reminder_days", 2)
    repeat = max(1, get_int("mail_overdue_repeat_days", 7))
    max_over = get_int("mail_overdue_max", 3)
    for loan in active:
        if not can_notify(loan.user):
            continue
        bucket = per_user.setdefault(loan.user_id, {"user": loan.user, "due_soon": [], "overdue": []})
        days_left = (loan.due_at - today).days
        if get_bool("mail_reminder") and 0 <= days_left <= rem_days and _sent(loan.id, "reminder").count() == 0:
            bucket["due_soon"].append(loan)
        elif get_bool("mail_overdue") and days_left < 0:
            prev = _sent(loan.id, "overdue")
            count = prev.count()
            if max_over and count >= max_over:
                continue
            last = prev.with_entities(func.max(MailLog.created_at)).scalar()
            if last is None or (today - last.date()).days >= repeat:
                bucket["overdue"].append(loan)
    return {uid: b for uid, b in per_user.items() if b["due_soon"] or b["overdue"]}


def _remind(user, bucket: dict, cfg, smtp) -> bool:
    """Reminder/overdue notice to one person (one mail for everything). True if everything was sent."""
    subject, text, html = render_mail("reminder", user=user, due_soon=bucket["due_soon"], overdue=bucket["overdue"])
    ok_r = ok_o = True
    if bucket["due_soon"]:
        ok_r, _err = send_mail(user.email, subject, text, html, kind="reminder", cfg=cfg, smtp=smtp,
                               loan_ids=[l.id for l in bucket["due_soon"]], user_id=user.id)
    if bucket["overdue"]:
        if bucket["due_soon"]:  # same mail, just log it additionally
            for l in bucket["overdue"]:
                db.session.add(MailLog(kind="overdue", to_addr=user.email, subject=subject,
                                       status="sent" if ok_r else "error", loan_id=l.id, user_id=user.id))
            db.session.commit()
            ok_o = ok_r
        else:
            ok_o, _err = send_mail(user.email, subject, text, html, kind="overdue", cfg=cfg, smtp=smtp,
                                   loan_ids=[l.id for l in bucket["overdue"]], user_id=user.id)
    return ok_r and ok_o


def run_notifications(today: date | None = None) -> dict:
    """Send reminders, overdue notices and the manager summary. Returns statistics."""
    stats = {"sent": 0, "errors": 0, "loans": 0, "digest": 0}
    if not mail_active():
        stats["skipped"] = "inactive"
        return stats
    today = today or date.today()
    cfg = SmtpConfig.from_settings()
    smtp = None
    try:
        smtp = smtp_connect(cfg)
    except MailError as exc:
        stats["errors"] += 1
        stats["error"] = str(exc)
        log.warning("Reminder run: %s", exc)
        return stats
    reconnected = False
    try:
        for bucket in collect_due(today).values():
            user = bucket["user"]
            try:
                ok = _remind(user, bucket, cfg, smtp)
            except Exception:  # noqa: BLE001 – one person must never abort the run for everyone else
                db.session.rollback()
                ok = False
                log.exception("Reminder to %s failed", user.username)
            stats["sent" if ok else "errors"] += 1
            stats["loans"] += len(bucket["due_soon"]) + len(bucket["overdue"])
            if not ok and not reconnected:
                # Most common reason: the mail server closed the connection after a few mails – reconnect
                # once. Failed mails do not count as sent and are retried later.
                reconnected = True
                try:
                    smtp.quit()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    smtp = smtp_connect(cfg)
                except MailError as exc:
                    smtp = None
                    stats["error"] = str(exc)
                    log.warning("Reminder run: reconnect failed: %s", exc)
                    return stats

        if get_bool("mail_digest"):
            stats["digest"] = _send_digests(cfg, smtp, today)
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass
    return stats


def _send_digests(cfg, smtp, today) -> int:
    overdue = (Loan.query.filter(Loan.returned_at.is_(None), Loan.due_at < today)
               .order_by(Loan.due_at).all())
    if not overdue:
        return 0
    sent = 0
    day_start = datetime.combine(today, datetime.min.time())
    managers = User.query.filter(User.role.in_([ROLE_MANAGER, ROLE_ADMIN]), User.active.is_(True)).all()
    for m in managers:
        if not can_notify(m):
            continue
        mine = [l for l in overdue if m.can_manage(l.asset)]
        if not mine:
            continue
        if MailLog.query.filter(MailLog.kind == "digest", MailLog.user_id == m.id, MailLog.status == "sent",
                                MailLog.created_at >= day_start).first():
            continue   # already sent today (e.g. "Send now" or send-reminders in addition)
        try:      # an error for one person (e.g. database briefly locked) does not hold up the others
            subject, text, html = render_mail("digest", user=m, loans=mine)
            ok, _err = send_mail(m.email, subject, text, html, kind="digest", cfg=cfg, smtp=smtp, user_id=m.id)
        except Exception:  # noqa: BLE001
            db.session.rollback()
            log.exception("Summary to %s not sent", m.username)
            continue
        sent += 1 if ok else 0
    return sent


# ---------------------------------------------------------------- Schedule

def _claim_today(today_str: str, key: str = "mail_last_run") -> bool:
    """Atomically claim "I am running today" – prevents duplicate runs with several processes."""
    if db.session.get(Setting, key) is None:
        try:
            db.session.add(Setting(key=key, value=""))
            db.session.commit()
        except Exception:  # noqa: BLE001 – another process was faster
            db.session.rollback()
    res = db.session.execute(update(Setting).where(Setting.key == key, Setting.value != today_str)
                             .values(value=today_str))
    db.session.commit()
    return res.rowcount == 1


RETRY_MINUTES = 15   # after a connection error, retry no earlier than this much later
MAX_RETRIES = 3      # individual failed mails: retry at most this often on the same day


def run_scheduled(now: datetime | None = None) -> dict | None:
    from datetime import timedelta
    now = now or datetime.now()
    if not mail_active() or now.hour < get_int("mail_hour", 7):
        return None
    retry_after = get_setting("mail_retry_after")
    if retry_after and retry_after > now.isoformat(timespec="seconds"):
        return None
    today_str = now.date().isoformat()
    row = db.session.get(Setting, "mail_last_run")
    previous = row.value if row is not None else ""
    if not _claim_today(today_str):
        return None
    stats = run_notifications(now.date())
    tries = get_setting("mail_retries")
    done_today = int(tries.split(":", 1)[1]) if tries.startswith(today_str + ":") and tries.split(":", 1)[1].isdigit() else 0
    if stats.get("error") or (stats.get("errors") and done_today < MAX_RETRIES):
        # SMTP unreachable or individual mails failed: release the day again so a later run catches up.
        # Reminders already sent are not repeated (only status=sent counts).
        db.session.execute(update(Setting).where(Setting.key == "mail_last_run", Setting.value == today_str)
                           .values(value=previous or ""))
        set_setting("mail_retry_after", (now + timedelta(minutes=RETRY_MINUTES)).isoformat(timespec="seconds"))
        set_setting("mail_retries", f"{today_str}:{done_today + 1}")
    elif retry_after:
        set_setting("mail_retry_after", "")
    set_setting("mail_last_result", stats_record(now, stats))
    db.session.commit()
    log.info("Reminder run: %s", stats)
    return stats


def stats_record(when: datetime, stats: dict, manual: bool = False) -> str:
    """Store the result of a run language-neutrally (shown in the viewer's language)."""
    import json
    return json.dumps({"at": when.isoformat(timespec="minutes"), "manual": manual, "stats": stats})


def last_result_text(raw: str) -> str:
    import json
    from .i18n import format_datetime
    try:
        rec = json.loads(raw)
        at = datetime.fromisoformat(rec["at"])
    except (ValueError, TypeError, KeyError):
        return raw or ""   # older entries (ready-made German text)
    label = format_datetime(at) + (" " + _("(manual)") if rec.get("manual") else "")
    return f"{label}: {format_stats(rec.get('stats') or {})}"


def format_stats(stats: dict) -> str:
    if stats.get("skipped"):
        return _("Email sending is not active.") if stats["skipped"] == "inactive" else str(stats["skipped"])
    if stats.get("error"):
        return _("Error – {error}", error=stats["error"])
    txt = _("{sent} e-mail(s) to staff members about {loans} loan(s)", sent=stats["sent"], loans=stats["loans"])
    if stats.get("digest"):
        txt += ", " + _("{n} summary/summaries to managers", n=stats["digest"])
    if stats.get("errors"):
        txt += ", " + _("{n} error(s)", n=stats["errors"])
    return txt


def cleanup_unverified(days: int = 7) -> int:
    """Delete self-registered accounts whose email was not confirmed after X days."""
    from datetime import timedelta
    limit = datetime.now() - timedelta(days=days)
    stale = User.query.filter(User.self_registered.is_(True), User.email_verified.is_(False),
                              User.created_at < limit).all()
    from .retention import OpenLoansError, delete_user
    deleted = 0
    for u in stale:
        try:
            delete_user(u)   # also with completed loans (otherwise IntegrityError)
            deleted += 1
        except OpenLoansError:
            pass
    if deleted:
        db.session.commit()
        log.info("%d unconfirmed registration(s) deleted", deleted)
    return deleted


_scheduler_started = False


def start_scheduler(app, interval: int = 300):
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def loop():
        from .backup import run_daily as run_backup_daily
        from .lockout import cleanup as cleanup_lockouts
        from .retention import run_daily as run_retention_daily
        # Each job separately: an error in one must not skip the others.
        jobs = [("clean up registrations", cleanup_unverified), ("clean up sign-in lockouts", cleanup_lockouts),
                ("backup", run_backup_daily),               # back up first …
                ("retention", run_retention_daily),         # … then clean up (waits for the backup)
                ("reminders", run_scheduled)]
        time.sleep(20)
        while True:
            for name, job in jobs:
                try:
                    with app.app_context():
                        job()
                except Exception:  # noqa: BLE001
                    log.exception("Error in the scheduler (%s)", name)
                    try:
                        with app.app_context():
                            db.session.rollback()
                    except Exception:  # noqa: BLE001
                        pass
            try:
                with app.app_context():
                    from .ad_cache import maintain as maintain_ad_cache
                    maintain_ad_cache()         # AD people for the desk (hourly or so, only when due)
            except Exception:  # noqa: BLE001
                log.exception("Error while syncing the AD cache")
            time.sleep(interval)

    threading.Thread(target=loop, name="assetlotse-mail", daemon=True).start()


__all__ = ["MailError", "SmtpConfig", "send_mail", "smtp_connect", "render_mail", "notify_loan", "notify_return",
           "send_manual_reminder", "run_notifications", "run_scheduled", "collect_due", "format_stats",
           "start_scheduler", "mail_active", "can_notify"]
