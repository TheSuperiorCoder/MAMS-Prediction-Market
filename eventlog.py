"""Logging for the site: the catalog of security events, and the Python log (stderr + a rotating file).

Two things are kept:
  * the **event log**, a table the admins can read (Admin → Events) with one row per notable thing that happened
    (sign-ups, log-ins, failures, holds, emails, errors, admin actions...). `log_event` in app.py writes it.
  * the **server log**, ordinary text lines in `instance/logs/mams.log` (rotated, 2 MB x 6) and on stderr, each tagged
    with the request's ID so a line, an event and the "Reference" shown on an error page can be matched up.

Nothing secret is ever logged: no passwords, no email codes, no Turnstile tokens, no API keys.
"""
import logging
import logging.handlers
import os

# kind -> (default severity, label). Severity is info | warn | alert. "alert" means somebody should look at it.
EVENTS = {
    # accounts
    "signup_ok": ("info", "Signed up"),
    "signup_refused_email": ("warn", "Sign-up refused: email not allowed"),
    "signup_rate_limited": ("warn", "Sign-up rate limited"),
    "signup_captcha_failed": ("warn", "Sign-up failed the human check"),
    "captcha_unreachable": ("warn", "Human check unreachable (sign-up rejected)"),
    "login_ok": ("info", "Logged in"),
    "login_failed": ("warn", "Wrong password"),
    "login_unknown_user": ("warn", "Log-in for an unknown username"),
    "login_locked": ("warn", "Log-in blocked: too many wrong passwords"),
    "login_rate_limited": ("warn", "Log-in rate limited (IP)"),
    "logout": ("info", "Logged out"),
    "session_expired": ("info", "Session ended (password changed elsewhere)"),
    "password_changed": ("info", "Password changed"),
    "password_change_failed": ("warn", "Password change failed"),
    # email
    "email_code_sent": ("info", "Confirmation code emailed"),
    "email_send_failed": ("alert", "Email could not be sent"),
    "email_code_wait": ("info", "Code request too soon after the last one"),
    "email_code_rate_limited": ("warn", "Too many code requests"),
    "email_refused": ("warn", "Email refused (taken, blocked or invalid)"),
    "email_code_wrong": ("warn", "Wrong confirmation code"),
    "email_code_expired": ("warn", "Confirmation code expired or used up"),
    "email_verified": ("info", "Email confirmed"),
    # risk
    "risk_held": ("alert", "Account automatically held"),
    "risk_flagged": ("warn", "Account flagged as suspicious"),
    "risk_reflagged": ("warn", "Approved account flagged again by new evidence"),
    "risk_cleared": ("info", "Account no longer suspicious"),
    "hold_blocked": ("warn", "Held account tried to act"),
    "sighting_new": ("info", "New device or network for an account"),
    # requests and errors
    "csrf_failed": ("warn", "Form rejected (expired or forged token)"),
    "admin_denied": ("warn", "Non-admin tried an admin page"),
    # credit scores
    "credit_tier_change": ("info", "Credit tier changed"),
    "credit_frozen": ("warn", "Borrowing frozen by an admin"),
    "credit_blocked_borrow": ("info", "Borrowing refused by credit rules"),
    "too_large": ("warn", "Oversized request refused"),
    "server_error": ("alert", "Server error"),
    "slow_request": ("warn", "Slow request"),
    "log_failed": ("alert", "Writing to the event log failed"),
    # stock over/unders that couldn't settle themselves
    "stock_check_failed": ("warn", "Stock market couldn't be checked"),
    "stock_push": ("warn", "Stock market closed exactly on its line"),
    # admin actions (every Admin-log entry is mirrored here as admin:<action>)
    "admin_action": ("info", "Admin action"),
}

SEVERITIES = ("info", "warn", "alert")
LEVELS = {"info": logging.INFO, "warn": logging.WARNING, "alert": logging.ERROR}
RETENTION_DAYS = 180


class RequestIdFilter(logging.Filter):
    """Adds %(request_id)s to every record: the current request's ID, or '-' outside a request."""

    def filter(self, record):
        rid = "-"
        try:
            from flask import g, has_request_context
            if has_request_context():
                rid = g.get("request_id", "-")
        except Exception:
            pass
        record.request_id = rid
        return True


def configure(app_logger, log_dir=None, level=None):
    """Send the app's logs to stderr and to a rotating file. Safe to call twice (it won't stack handlers) and safe if
    the log directory can't be written (stderr is still used)."""
    level = getattr(logging, (level or os.environ.get("MAMS_LOG_LEVEL", "INFO")).upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s")
    flt = RequestIdFilter()
    targets = [app_logger, logging.getLogger("mams")]
    for logger in targets:
        logger.setLevel(level)
    if getattr(app_logger, "_mams_configured", False):
        return
    handlers = []
    stream = logging.StreamHandler()
    handlers.append(stream)
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, "mams.log")
            fh = logging.handlers.RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
            try:
                os.chmod(path, 0o600)  # IPs and usernames: the server's account only
            except OSError:
                pass
            handlers.append(fh)
        except OSError:
            logging.getLogger("mams").warning("Can't write logs to %s; logging to stderr only.", log_dir)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(flt)
    for logger in targets:
        for h in handlers:
            logger.addHandler(h)
    app_logger._mams_configured = True
