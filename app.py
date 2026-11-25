"""MAMS Prediction Market — bet virtual MAMS Bucks on Mass Academy events."""

import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import smtplib
import socket
import ssl
import threading
import time
from datetime import date, datetime, timedelta
import email.utils
from email.message import EmailMessage
from functools import wraps
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit

from flask import (Flask, abort, flash, g, jsonify, make_response, redirect, render_template,
                   request, session, url_for, current_app, has_app_context, has_request_context)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import credit
import eventlog
import lmsr
import security

# All times (closing times, daily bonus reset) use school time, even on servers that run in UTC.
os.environ["TZ"] = os.environ.get("MAMS_TZ", "America/New_York")
if hasattr(time, "tzset"):
    time.tzset()

# Set MAMS_PRODUCTION=1 when hosted on HTTPS behind a proxy (e.g. PythonAnywhere).
PRODUCTION = os.environ.get("MAMS_PRODUCTION") == "1"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)
DB_PATH = os.environ.get("MAMS_DB", os.path.join(INSTANCE_DIR, "mams.db"))

STARTING_BALANCE = 1000.0
DAILY_BONUS = 100.0
MIN_BET = 1.0
CURRENCY = "MAMS Bucks"
ADMIN_USERNAMES = {u.strip().lower() for u in os.environ.get("MAMS_ADMINS", "").split(",") if u.strip()}
# LMSR liquidity ("b") for a yes/no market. Higher = prices swing less per bet. At 1000, on a 50/50 market:
#   Ⓜ100 -> 55%,  Ⓜ500 -> 70%,  Ⓜ1000 (a whole starting balance) -> 82%.
# Markets with more outcomes scale up (see market_b) so a bet moves an 8-way market about as much as a 2-way one.
# The most the house can lose on a market is b * ln(outcomes), about Ⓜ693 here; fine for play money.
BASE_LIQUIDITY = 1000.0


def market_b(n_outcomes):
    return BASE_LIQUIDITY * math.log2(max(n_outcomes, 2))
MIN_RULES_LENGTH = 20
DISPUTE_WINDOW = timedelta(hours=2)  # how long traders can challenge a proposed result
DISPUTE_HOURS = int(DISPUTE_WINDOW.total_seconds() // 3600)
# Making a market costs CREATION_FEE (refunded if you withdraw your proposal, kept if it's rejected), and everyone
# else pays the market's creator ENTRY_FEE on their first bet in it. Good markets that attract traders earn their
# creator money; flooding the site with junk proposals costs money.
CREATION_FEE = 10.0
ENTRY_FEE = 10.0
# Whoever makes a market must also bet at least this much on it, so they have skin in the game.
CREATOR_MIN_BET = 10.0
# Late-trading fee, to discourage piling in once the answer is nearly known:
#   fee = LATE_FEE_RATE x shares traded x P x (1 - P) x T
# P is the trade's average price per share; T depends on how long is left before betting closes.
# P(1 - P) is tiny near 100¢, which would let people snipe near-certain outcomes cheaply, so buys pay the larger of
# that and LATE_SNIPE_RATE x shares x P^2 x T (about 10% of the bet at 100¢ in the last 30 minutes).
LATE_FEE_RATE = 0.07
LATE_SNIPE_RATE = 0.017
LATE_FEE_TIERS = [  # (less than this much time left, T), checked in order
    (timedelta(minutes=30), 6.0),
    (timedelta(hours=1), 4.0),
    (timedelta(hours=2), 2.0),
]  # more than 2 hours left: T = 0, no fee. Keep these sorted from shortest to longest.


CATEGORIES = ["Academics", "Competitions", "Sports", "Events", "Campus Life", "Weather", "Other"]
EPS = 1e-9
# Player loans: lenders pick the amount, a flat interest rate (percent of the amount) and how many days until it's due.
LOAN_MIN = 1.0
LOAN_MAX_RATE = 100.0
LOAN_MAX_DAYS = 30
# Borrowing limits and what happens when a loan isn't paid back (see the loans section below).
LOAN_CREDIT_FLOOR = 500.0   # anyone can owe at least this much in total...
LOAN_CREDIT_RATIO = 0.5     # ...or this share of cash + bets - debts, whichever is more
LOAN_MAX_ACTIVE = 5         # running loans as a borrower
LOAN_GRACE_DAYS = 2         # after the due date, before lenders may sell or take over the borrower's bets
LOAN_LATE_FEE = 0.10        # added to what's still owed when a loan goes past its due date, then again at the start of
                            # each further day it stays unpaid (compounding) until the grace period ends; goes to the lender
OWNER_USERNAME = os.environ.get("MAMS_OWNER", "Kaushik").strip()
# Optional. When set, the owner account only gets owner powers once it has confirmed this email address, so if the
# database is ever reset, whoever signs up first with the owner's username doesn't become owner.
OWNER_EMAIL = os.environ.get("MAMS_OWNER_EMAIL", "").strip()
LOGIN_CODE_LIFETIME = 10 * 60
LOGIN_CODE_RESEND_DELAY = 60  # at most one code a minute per account, and per email address
EMAIL_CLAIM_HOURS = 24  # an unconfirmed sign-up holds its email this long, so nobody else can sign up with it
LOGIN_CODE_MAX_ATTEMPTS = 5
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(INSTANCE_DIR, "secret_key")
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # readable by the server's account only
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.config.update(SECRET_KEY=_secret_key(), PERMANENT_SESSION_LIFETIME=timedelta(days=30),
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=PRODUCTION,
                  # the biggest real form is a few KB; refuse anything huge before it's parsed or hashed
                  MAX_CONTENT_LENGTH=256 * 1024)
if PRODUCTION:
    # Trust the hosting proxy's X-Forwarded-For / -Proto so we see real visitor IPs and HTTPS.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

eventlog.configure(app.logger, os.environ.get("MAMS_LOG_DIR") or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "logs"))
log = logging.getLogger("mams")

# Simple in-memory rate limiting (one server process is plenty for a school site).
# Limits are generous because a whole school can share one public IP address.
_hits = {}
_hits_lock = threading.Lock()  # Flask may serve requests on several threads
HITS_MAX_KEYS = 10000
HITS_MAX_WINDOW = 3600  # the longest window any limit uses


def _prune_hits(now):
    """Keep _hits bounded without forgetting live limits. (Clearing everything would let someone wipe every
    lockout just by trying lots of different usernames.) Drops keys with no hit inside the longest window, then,
    if still too many, the ones with the fewest recent hits (oldest first), so keys close to a limit survive a flood
    of one-off keys. Caller holds _hits_lock."""
    for key in [k for k, ts in _hits.items() if not ts or now - ts[-1] >= HITS_MAX_WINDOW]:
        del _hits[key]
    if len(_hits) > HITS_MAX_KEYS:
        by_age = sorted(_hits, key=lambda k: (len(_hits[k]), _hits[k][-1]))
        for key in by_age[:len(_hits) - int(HITS_MAX_KEYS * 0.8)]:
            del _hits[key]


TRADE_LIMITS = ((60, 60), (600, 3600))  # trades per user: 60 a minute, 600 an hour
PROPOSE_LIMIT = (10, 3600)              # market proposals per user per hour
# heavy read-only pages (leaderboard, all loans, credit pages): per account when logged in, per IP otherwise. The
# per-IP figure is higher because a whole school can share one address.
HEAVY_PAGE_LIMITS = {"user": (60, 60), "ip": (240, 60)}


def rate_limited(key, limit, window_seconds):
    now = time.time()
    with _hits_lock:
        recent = [t for t in _hits.get(key, []) if now - t < window_seconds]
        limited = len(recent) >= limit
        if not limited:
            recent.append(now)
        _hits[key] = recent
        if len(_hits) > HITS_MAX_KEYS:
            _prune_hits(now)
        return limited


# ---------------------------------------------------------------- database

def connect():
    db = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        flush_events(db)
        db.close()


# ---------------------------------------------------------------- event log

def _short(value, n):
    return str(value if value is not None else "").replace("\n", " ").replace("\r", " ")[:n]


def log_event(kind, user=None, detail="", severity=None, user_id=None, username=None):
    """Record something that happened: a row in the event log (Admin -> Events) and a line in the server log.
    `user` is a row/dict with id and username; if omitted the logged-in user (if any) is used. It never raises and
    never holds up a request: if it's called inside an open transaction the row is queued and written afterwards, so
    a rollback can't lose it and it can't wait on its own lock. Never put secrets in `detail`."""
    try:
        default_sev, label = eventlog.EVENTS.get(kind, ("info", kind))
        sev = severity if severity in eventlog.SEVERITIES else default_sev
        if user is None and user_id is None and username is None and has_request_context():
            user = g.get("user")
        if user is not None:
            user_id, username = user["id"], user["username"]
        ip = ua = did = rid = path = ""
        if has_request_context():
            ip, ua = _short(request.remote_addr, 64), _short(request.headers.get("User-Agent", ""), 300)
            did, rid, path = g.get("device_id", ""), g.get("request_id", ""), _short(request.path, 200)
        row = (time.time(), kind, sev, user_id, _short(username, 40), ip, ua, did, rid, path, _short(detail, 600))
        log.log(eventlog.LEVELS[sev], "event=%s user=%s ip=%s %s", kind, username or "-", ip or "-", _short(detail, 300))
        if not has_app_context():
            return
        db = get_db()
        if db.in_transaction:
            g.setdefault("pending_events", []).append(row)
        else:
            _insert_event(db, row)
    except Exception:
        try:
            log.exception("could not record event %s", kind)
        except Exception:
            pass


def _insert_event(db, row):
    db.execute("INSERT INTO security_events (created_at, kind, severity, user_id, username, ip, ua, device_id, "
               "request_id, path, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)


def flush_events(db):
    """Write events that were queued while a transaction was open."""
    try:
        pending = g.pop("pending_events", None)
    except RuntimeError:
        return
    for row in pending or []:
        try:
            _insert_event(db, row)
        except sqlite3.Error:
            log.exception("could not write queued event %s", row[1])


@app.before_request
def start_request():
    g.request_id = secrets.token_hex(4)
    g.started = time.time()


SLOW_REQUEST = 3.0  # seconds


@app.after_request
def finish_request(resp):
    rid = g.get("request_id")
    if rid:
        resp.headers["X-Request-ID"] = rid
    took = time.time() - g.get("started", time.time())
    if took > SLOW_REQUEST and request.endpoint not in PWA_ENDPOINTS:
        log_event("slow_request", detail=f"{request.method} {request.path} took {took:.1f}s")
    try:
        if g.get("pending_events") and "db" in g:
            flush_events(g.db)
    except Exception:
        log.exception("could not flush events")
    return resp


# Columns added after the first release. schema.sql only creates missing tables, so databases
# created earlier get these through ALTER TABLE (see migrate).
NEW_COLUMNS = {
    # session_gen: bumping it signs a user out everywhere (password change/reset).
    "users": [("session_gen", "INTEGER NOT NULL DEFAULT 0"),
              ("email", "TEXT"), ("email_verified", "INTEGER NOT NULL DEFAULT 0"),
              # email_canonical: the email with dots/+tags/aliases removed (security.normalize_email), so one
              # mailbox is one account. held: read-only until an admin approves (see user_risk).
              ("email_canonical", "TEXT"), ("held", "INTEGER NOT NULL DEFAULT 0")],
    "comments": [("hidden", "INTEGER NOT NULL DEFAULT 0")],
    "login_challenges": [("email", "TEXT")],  # the address being confirmed
    "markets": [("rules_source", "TEXT NOT NULL DEFAULT ''"), ("rules_otherwise", "TEXT NOT NULL DEFAULT ''"),
                ("proposed_outcome_id", "INTEGER"), ("proposed_by", "INTEGER"), ("proposed_at", "TEXT"),
                ("finalizes_at", "TEXT"), ("resolved_by", "INTEGER"),
                # Over/Under markets: the line, an optional unit ("%", "minutes"), and the measured result.
                ("ou_line", "REAL"), ("ou_unit", "TEXT NOT NULL DEFAULT ''"), ("ou_result", "REAL"),
                ("featured", "INTEGER NOT NULL DEFAULT 0"),
                # money left in the pot after paying Ⓜ1 per winning share, shared out among the winners
                ("leftover_paid", "REAL NOT NULL DEFAULT 0")],
    "proposals": [("rules_source", "TEXT NOT NULL DEFAULT ''"), ("rules_otherwise", "TEXT NOT NULL DEFAULT ''"),
                  ("ou_line", "REAL"), ("ou_unit", "TEXT NOT NULL DEFAULT ''"),
                  ("fee", "REAL NOT NULL DEFAULT 0"),  # creation fee paid, refunded on withdrawal
                  # the proposer's opening bet, held until approval (refunded if rejected or withdrawn)
                  ("stake", "REAL NOT NULL DEFAULT 0"), ("stake_pick", "INTEGER"),
                  ("stake_label", "TEXT NOT NULL DEFAULT ''")],
    # via: 'loan' marks shares handed from a borrower to their lender to pay off an overdue loan
    "trades": [("via", "TEXT")],
    # min_score: an offer only borrowers with at least this credit score may take
    "loans": [("min_score", "INTEGER"),
              # late_at: when the first late fee was added; penalty: all late fees so far; late_fees: how many
              ("late_at", "TEXT"), ("penalty", "REAL NOT NULL DEFAULT 0"),
              ("late_fees", "INTEGER NOT NULL DEFAULT 0")],
}


def migrate(db):
    """Add any columns an older database is missing. Safe to run repeatedly."""
    for table, columns in NEW_COLUMNS.items():
        have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    # clarifications.admin_id used to be NOT NULL; deleting an admin's account needs it nullable. SQLite can't
    # drop a constraint in place, so rebuild the table (once).
    cols = {r["name"]: r for r in db.execute("PRAGMA table_info(clarifications)")}
    if cols and cols["admin_id"]["notnull"]:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE clarifications_new (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                   "market_id INTEGER NOT NULL REFERENCES markets(id), admin_id INTEGER REFERENCES users(id), "
                   "body TEXT NOT NULL, created_at TEXT NOT NULL)")
        db.execute("INSERT INTO clarifications_new (id, market_id, admin_id, body, created_at) "
                   "SELECT id, market_id, admin_id, body, created_at FROM clarifications")
        db.execute("DROP TABLE clarifications")
        db.execute("ALTER TABLE clarifications_new RENAME TO clarifications")
        db.execute("CREATE INDEX IF NOT EXISTS idx_clarifications_market ON clarifications(market_id)")
        db.execute("COMMIT")
    # offers made under the older credit system kept their minimum score in min_credit
    if "min_credit" in {r["name"] for r in db.execute("PRAGMA table_info(loans)")}:
        db.execute("UPDATE loans SET min_score = min_credit WHERE min_score IS NULL AND min_credit > 0")
    # one account per confirmed email
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email COLLATE NOCASE) "
               "WHERE email_verified = 1")


def backfill_security(db):
    """Fill in the canonical email and the email ledger for accounts that existed before alt detection, and flag
    accounts whose emails turn out to be the same mailbox. It never adds a unique index (two old accounts that
    collapse to one address would make start-up fail); uniqueness is enforced in code for new emails instead.
    A problem here is logged and skipped: it must never stop the site from starting."""
    try:
        rows = db.execute("SELECT id, username, email, created_at FROM users WHERE email_verified = 1 "
                          "AND email IS NOT NULL AND email_canonical IS NULL ORDER BY id").fetchall()
        for r in rows:
            _, canon = security.normalize_email(r["email"])
            if canon:
                db.execute("UPDATE users SET email_canonical = ? WHERE id = ?", (canon, r["id"]))
                db.execute("INSERT OR IGNORE INTO email_ledger (canonical, first_username, status, created_at) "
                           "VALUES (?, ?, 'active', ?)", (canon, r["username"], time.time()))
        for dup in db.execute("SELECT email_canonical FROM users WHERE email_canonical IS NOT NULL "
                              "GROUP BY email_canonical HAVING COUNT(*) > 1").fetchall():
            ids = db.execute("SELECT id, username FROM users WHERE email_canonical = ?", (dup["email_canonical"],)).fetchall()
            for u in ids:
                others = ", ".join(o["username"] for o in ids if o["id"] != u["id"])
                db.execute(
                    "INSERT OR IGNORE INTO user_risk (user_id, score, level, reasons, links, status, updated_at) "
                    "VALUES (?, 60, 'high', ?, '{}', 'flagged', ?)",
                    (u["id"], json.dumps([[60, "email", f"Shares an email with {others}"]]), time.time()))
        security.prune(db)
    except Exception:  # any failure at all: this runs at import time and must never stop the site starting
        logging.getLogger("mams").exception("Security backfill failed; continuing without it.")


def require_emails_once(db):
    """When confirmed emails were introduced, everyone was signed out once so they'd be asked for one on their next
    log-in. Recorded in site_settings so it only ever happens once."""
    if db.execute("SELECT 1 FROM site_settings WHERE key = 'emails_logout_done'").fetchone():
        return
    db.execute("BEGIN IMMEDIATE")
    db.execute("UPDATE users SET session_gen = session_gen + 1")
    db.execute("INSERT INTO site_settings (key, value, updated_at) VALUES ('emails_logout_done', '1', datetime('now'))")
    db.execute("COMMIT")


def deepen_liquidity(db):
    """Bring unsettled markets created with lower liquidity up to the current setting. Every share count the
    market maker holds is scaled by the same factor, so each market's prices stay exactly where they are; only
    future bets move them less. Nobody's shares or balance change. Safe to run repeatedly."""
    db.execute("BEGIN IMMEDIATE")
    try:
        for m in db.execute("SELECT id, b FROM markets WHERE status = 'open'").fetchall():
            n = db.execute("SELECT COUNT(*) FROM outcomes WHERE market_id = ?", (m["id"],)).fetchone()[0]
            target = market_b(n)
            if m["b"] < target - 1e-9:
                factor = target / m["b"]
                db.execute("UPDATE outcomes SET shares = shares * ? WHERE market_id = ?", (factor, m["id"]))
                db.execute("UPDATE markets SET b = ? WHERE id = ?", (target, m["id"]))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def backfill_activity(db):
    """The first time the activity log exists, fill it with past trades so nobody's history starts empty."""
    if db.execute("SELECT 1 FROM activity LIMIT 1").fetchone():
        return
    db.execute(
        "INSERT INTO activity (user_id, kind, amount, balance_after, market_id, market_title, detail, created_at) "
        "SELECT t.user_id, CASE WHEN t.shares > 0 THEN 'buy' ELSE 'sell' END, ROUND(-t.amount, 2), NULL, "
        "t.market_id, m.question, "
        "(CASE WHEN t.shares > 0 THEN 'Bought ' ELSE 'Sold ' END) || printf('%.2f', ABS(t.shares)) "
        "|| ' shares of “' || o.label || '”', t.created_at "
        "FROM trades t JOIN markets m ON m.id = t.market_id JOIN outcomes o ON o.id = t.outcome_id ORDER BY t.id")


def shorten_dispute_windows(db):
    """If DISPUTE_WINDOW was made shorter, bring results that are already waiting in a challenge window onto the new
    deadline: proposed time + DISPUTE_WINDOW. Deadlines only ever move earlier, so this is safe to run repeatedly.
    Anything now past its deadline finalizes on the next request (unless someone has disputed it)."""
    db.execute("BEGIN IMMEDIATE")
    try:
        for m in db.execute("SELECT id, proposed_at, finalizes_at FROM markets WHERE status = 'open' "
                            "AND proposed_outcome_id IS NOT NULL AND proposed_at IS NOT NULL "
                            "AND finalizes_at IS NOT NULL").fetchall():
            deadline = (datetime.fromisoformat(m["proposed_at"]) + DISPUTE_WINDOW).strftime("%Y-%m-%d %H:%M:%S")
            if deadline < m["finalizes_at"]:
                db.execute("UPDATE markets SET finalizes_at = ? WHERE id = ?", (deadline, m["id"]))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


# defined before init_db(), which runs while this module is still loading
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def mark_loan_rules_start(db):
    """Record when lenders were first allowed to collect from a borrower's bets. A loan that was already running then
    gets its grace period counted from that moment, so nobody is seized on terms they never saw. A new database has
    no such loans, so its start is the beginning of time."""
    if db.execute("SELECT 1 FROM site_settings WHERE key = 'loans_v2_at'").fetchone():
        return
    running = db.execute("SELECT 1 FROM loans WHERE status = 'active' LIMIT 1").fetchone()
    db.execute("INSERT INTO site_settings (key, value, updated_at) VALUES ('loans_v2_at', ?, datetime('now'))",
               (now_str() if running else "1970-01-01 00:00:00",))


def mark_compound_late_fees(db):
    """Late fees used to be added once; now they compound daily through the grace period. Loans that were already
    overdue when that changed keep the single fee they got, so nobody is charged on terms they never saw."""
    if db.execute("SELECT 1 FROM site_settings WHERE key = 'late_fees_daily_at'").fetchone():
        return
    db.execute("UPDATE loans SET late_fees = ? WHERE late_at IS NOT NULL AND late_fees = 0", (LOAN_GRACE_DAYS,))
    db.execute("INSERT INTO site_settings (key, value, updated_at) VALUES ('late_fees_daily_at', ?, datetime('now'))",
               (now_str(),))


def init_db():
    db = connect()
    with open(os.path.join(BASE_DIR, "schema.sql")) as f:
        db.executescript(f.read())
    migrate(db)
    mark_loan_rules_start(db)
    mark_compound_late_fees(db)
    backfill_security(db)
    require_emails_once(db)
    deepen_liquidity(db)
    backfill_activity(db)
    shorten_dispute_windows(db)
    db.close()


def email_csv_path():
    """people.csv lives next to the database (instance/ on the server, which git ignores)."""
    return os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "people.csv")


def write_email_csv(db):
    """Keep instance/people.csv (name,email for every account; blank until confirmed) up to date."""
    import csv
    rows = db.execute("SELECT username, CASE WHEN email_verified = 1 THEN email ELSE '' END FROM users "
                      "ORDER BY username COLLATE NOCASE").fetchall()
    tmp = email_csv_path() + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # names and emails: server account only
    with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["name", "email"])
        w.writerows([tuple(csv_safe(v) for v in r) for r in rows])
    os.chmod(tmp, 0o600)
    os.replace(tmp, email_csv_path())


def csv_safe(value):
    """Spreadsheets run cells starting with = + - @ as formulas; a leading ' makes them plain text."""
    value = value or ""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def login_code_hash(username, code):
    value = f"{username.casefold()}:{code}".encode()
    return hmac.new(app.config["SECRET_KEY"].encode(), value, hashlib.sha256).hexdigest()


class EmailError(RuntimeError):
    """An email couldn't be sent. `reason` is a short, secret-free explanation for the logs and the admin event log;
    `permanent` means retrying won't help (bad key, unverified sender, rejected recipient)."""

    def __init__(self, reason, permanent=False):
        super().__init__(reason)
        self.reason, self.permanent = reason, permanent


def email_settings():
    """Where email goes, from the environment. MAMS_EMAIL_BACKEND is `smtp` (default), `resend` or `console`.
    `resend` is Resend's SMTP service: host smtp.resend.com, user `resend`, and the API key as the password
    (MAMS_RESEND_API_KEY). Anything else is plain SMTP from MAMS_SMTP_*. Port 465/2465 uses implicit TLS; other ports
    (587, 2587, 25) start plain and upgrade with STARTTLS."""
    backend = os.environ.get("MAMS_EMAIL_BACKEND", "smtp").strip().lower()
    env = os.environ.get
    if backend == "resend":
        host = env("MAMS_SMTP_HOST", "smtp.resend.com").strip()
        user = env("MAMS_SMTP_USERNAME", "resend").strip()
        password = (env("MAMS_RESEND_API_KEY") or env("MAMS_SMTP_PASSWORD") or "").strip()
    else:
        host, user, password = env("MAMS_SMTP_HOST", "").strip(), env("MAMS_SMTP_USERNAME", ""), env("MAMS_SMTP_PASSWORD", "")
    try:
        port = int(env("MAMS_SMTP_PORT", "587"))
    except ValueError:
        port = 587
    try:
        timeout = float(env("MAMS_SMTP_TIMEOUT", "15"))
    except ValueError:
        timeout = 15.0
    sender = env("MAMS_SMTP_FROM", user if "@" in user else "").strip()
    return {"backend": backend, "host": host, "port": port, "user": user, "password": password,
            "sender": sender, "timeout": timeout}


def _smtp_error(e):
    """Turn an smtplib/socket exception into an EmailError with a plain-English reason (never including secrets)."""
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return EmailError("SMTP login rejected: check the API key / password", permanent=True)
    if isinstance(e, smtplib.SMTPSenderRefused):
        return EmailError("sender address refused: verify the From domain with your email provider", permanent=True)
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return EmailError("recipient address refused by the mail server", permanent=True)
    if isinstance(e, smtplib.SMTPResponseException):
        return EmailError(f"mail server replied {e.smtp_code}", permanent=500 <= e.smtp_code < 600)
    if isinstance(e, (smtplib.SMTPServerDisconnected, ConnectionError)):
        return EmailError("mail server dropped the connection")
    if isinstance(e, (socket.timeout, TimeoutError)):
        return EmailError("mail server timed out")
    if isinstance(e, socket.gaierror):
        return EmailError("couldn't find the mail server (check MAMS_SMTP_HOST)", permanent=True)
    if isinstance(e, ssl.SSLError):
        return EmailError("TLS error talking to the mail server (check MAMS_SMTP_PORT)")
    return EmailError(f"{type(e).__name__} while sending")


def send_email(recipient, subject, text, html=None):
    """Send one email through the configured backend. Retries once on a dropped connection or timeout. Raises
    EmailError with a safe `reason` on failure. Returns a short description of what happened for the logs."""
    if app.config.get("TESTING"):
        raise EmailError("Email delivery is disabled during automated tests.", permanent=True)
    cfg = email_settings()
    if cfg["backend"] == "console":
        current_app.logger.warning("LOCAL EMAIL to %s: %s | %s", recipient, subject, text)
        return "console"
    if not cfg["host"] or not cfg["sender"]:
        raise EmailError("SMTP host and sender (MAMS_SMTP_FROM) must be configured before email can be sent.",
                         permanent=True)
    if cfg["backend"] == "resend" and not cfg["password"]:
        raise EmailError("MAMS_RESEND_API_KEY isn't set", permanent=True)
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = subject, cfg["sender"], recipient
    message["Message-ID"] = email.utils.make_msgid(domain=cfg["sender"].rsplit("@", 1)[-1].rstrip("> ") or None)
    message["Date"] = email.utils.formatdate(localtime=True)
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    last = None
    for attempt in (1, 2):
        started = time.time()
        try:
            ctx = ssl.create_default_context()
            if cfg["port"] in (465, 2465):
                server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=cfg["timeout"], context=ctx)
            else:
                server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=cfg["timeout"])
            with server:
                server.ehlo()
                if cfg["port"] not in (465, 2465):
                    server.starttls(context=ctx)
                    server.ehlo()
                if cfg["user"]:
                    server.login(cfg["user"], cfg["password"])
                server.send_message(message)
            return f"{cfg['backend']} via {cfg['host']}:{cfg['port']} in {int((time.time() - started) * 1000)} ms" \
                + (" (2nd try)" if attempt == 2 else "")
        except EmailError:
            raise
        except (smtplib.SMTPException, OSError) as e:
            last = _smtp_error(e)
            current_app.logger.warning("email attempt %d to %s failed: %s", attempt, cfg["host"], last.reason)
            if last.permanent:
                break
    raise last


def send_login_code_email(recipient, code):
    if not app.config.get("TESTING") and email_settings()["backend"] == "console":
        current_app.logger.warning("LOCAL VERIFICATION CODE for %s: %s", recipient, code)  # local development only
        return "console"
    text = (f"Your MAMS Prediction Market verification code is {code}. "
            "It expires in 10 minutes. If you did not request it, you can ignore this email.")
    html = ("<div style=\"font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:420px;margin:auto;padding:24px\">"
            "<h2 style=\"margin:0 0 12px\">Confirm your email</h2>"
            "<p>Enter this code on the MAMS Prediction Market page:</p>"
            f"<p style=\"font-size:32px;font-weight:700;letter-spacing:6px;margin:16px 0\">{code}</p>"
            "<p style=\"color:#666\">It expires in 10 minutes. If you didn't ask for it, you can ignore this email.</p></div>")
    return send_email(recipient, "Your MAMS Prediction Market verification code", text, html)


init_db()


# ---------------------------------------------------------------- helpers

def parse_dt(s):
    return datetime.fromisoformat(s)


def cents(x):
    return round(x + 0.0, 2)


def floor_cents(x):
    """Round down to a whole cent, the way a sale is paid out (check_trade)."""
    return math.floor(x * 100 + 1e-9) / 100


def market_state(m):
    """open | closed (past close date, awaiting a proposed result) | proposed (result proposed, challenge window
    running) | resolved | cancelled. `bundle` upgrades proposed to disputed when a challenge is waiting."""
    if m["status"] in ("resolved", "cancelled"):
        return m["status"]
    if m["proposed_outcome_id"] is not None:
        return "proposed"
    return "closed" if datetime.now() >= parse_dt(m["closes_at"]) else "open"


def is_binary(labels):
    return [x.lower() for x in labels] == ["yes", "no"]


# ---------------------------------------------------------------- over/under

def fmt_number(x):
    """85.0 -> '85', 50.5 -> '50.5', 1200 -> '1,200'."""
    return f"{x:,.4f}".rstrip("0").rstrip(".")


def ou_text(line, unit):
    """The line with its unit: '85%', '90 minutes', '50.5'."""
    unit = (unit or "").strip()
    return fmt_number(line) + ((" " + unit) if unit and unit[0].isalnum() else unit)


def ou_labels(line, unit):
    """Over is always first, Under second."""
    text = ou_text(line, unit)
    return [f"Over {text}", f"Under {text}"]


def parse_ou(f):
    """Read the line and unit from a form. Returns (line, unit, error)."""
    try:
        line = float(str(f.get("ou_line", "")).replace(",", "").strip())
        if not math.isfinite(line) or abs(line) >= 1e9:
            raise ValueError
    except ValueError:
        return None, "", "Enter the over/under line as a number, like 85 or 50.5."
    return line, f.get("ou_unit", "").strip()[:15], None


def open_disputes(db, market_id):
    return db.execute("SELECT d.*, u.username FROM disputes d JOIN users u ON u.id = d.user_id "
                      "WHERE d.market_id = ? AND d.handled_at IS NULL ORDER BY d.id", (market_id,)).fetchall()


def get_outcomes(db, market_id):
    return db.execute("SELECT * FROM outcomes WHERE market_id = ? ORDER BY position", (market_id,)).fetchall()


def _build_bundle(m, outs, stats, has_dispute):
    if m["status"] == "resolved":
        ps = [1.0 if o["id"] == m["resolved_outcome_id"] else 0.0 for o in outs]
    else:
        ps = lmsr.prices([o["shares"] for o in outs], m["b"])
    state = market_state(m)
    if state == "proposed" and has_dispute:
        state = "disputed"
    return {
        "m": m,
        "state": state,
        # "binary" = shown as a single chance % (Yes/No, and Over/Under where Over is outcome 0)
        "binary": is_binary([o["label"] for o in outs]) or m["ou_line"] is not None,
        "ou": ({"text": ou_text(m["ou_line"], m["ou_unit"]),
                "result": None if m["ou_result"] is None else ou_text(m["ou_result"], m["ou_unit"])}
               if m["ou_line"] is not None else None),
        "outcomes": [{"id": o["id"], "label": o["label"], "shares": o["shares"], "price": p}
                     for o, p in zip(outs, ps)],
        "volume": stats["volume"] if stats else 0,
        "traders": stats["traders"] if stats else 0,
    }


def bundle(db, m):
    """A market plus its outcomes and current prices, ready for templates."""
    outs = get_outcomes(db, m["id"])
    stats = db.execute(
        "SELECT COALESCE(SUM(ABS(amount)), 0) AS volume, COUNT(DISTINCT user_id) AS traders "
        "FROM trades WHERE market_id = ?", (m["id"],)).fetchone()
    has_dispute = market_state(m) == "proposed" and bool(open_disputes(db, m["id"]))
    return _build_bundle(m, outs, stats, has_dispute)


def bundle_many(db, market_rows):
    """Batch-build bundles for many markets in 3 queries instead of 2*M+1."""
    if not market_rows:
        return []
    mids = [m["id"] for m in market_rows]
    ph = ",".join("?" * len(mids))
    outs_by_mid = {mid: [] for mid in mids}
    for o in db.execute(f"SELECT * FROM outcomes WHERE market_id IN ({ph}) ORDER BY market_id, position", mids):
        outs_by_mid[o["market_id"]].append(o)
    stats_by_mid = {
        r["market_id"]: r
        for r in db.execute(
            f"SELECT market_id, COALESCE(SUM(ABS(amount)), 0) AS volume, COUNT(DISTINCT user_id) AS traders "
            f"FROM trades WHERE market_id IN ({ph}) GROUP BY market_id", mids
        )
    }
    disputed_mids = set()
    proposed_mids = [m["id"] for m in market_rows if market_state(m) == "proposed"]
    if proposed_mids:
        pph = ",".join("?" * len(proposed_mids))
        disputed_mids = {
            r["market_id"]
            for r in db.execute(
                f"SELECT DISTINCT market_id FROM disputes WHERE handled_at IS NULL AND market_id IN ({pph})",
                proposed_mids,
            )
        }
    return [
        _build_bundle(m, outs_by_mid[m["id"]], stats_by_mid.get(m["id"]), m["id"] in disputed_mids)
        for m in market_rows
    ]


def price_trends(db, market_ids, points=32):
    """Per market id: `spark`, a downsampled list of price snapshots (each a list with one price per outcome) for
    sparklines, and `delta`, each outcome's change since the last snapshot at least 24 hours old (or since the
    first snapshot when the market is younger than that). Times are school wall-clock strings, like now_str().
    Only the snapshots actually used are JSON-decoded."""
    market_ids = list(market_ids)
    if not market_ids:
        return {}
    cutoff = (parse_dt(now_str()) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    series = {}
    for r in db.execute(f"SELECT market_id, prices, created_at FROM price_history WHERE market_id IN "
                        f"({','.join('?' * len(market_ids))}) ORDER BY id", market_ids):
        series.setdefault(r["market_id"], []).append((r["created_at"], r["prices"]))
    out = {}
    for mid, s in series.items():
        latest_raw = s[-1][1]
        base_raw = s[0][1]
        for t, p_raw in s:
            if t <= cutoff:
                base_raw = p_raw
            else:
                break
        if len(s) > points:  # keep the first and last snapshot, evenly spaced in between
            s = [s[round(i * (len(s) - 1) / (points - 1))] for i in range(points)]
        latest = json.loads(latest_raw)
        base = json.loads(base_raw)
        out[mid] = {"spark": [json.loads(p_raw) for _, p_raw in s], "delta": [a - b for a, b in zip(latest, base)]}
    return out


@app.template_global()
def spark_points(trend, i=0, w=120, h=32):
    """SVG polyline points for outcome i's sparkline in a w x h box (prices 0..1, padded so the stroke fits)."""
    if not trend or not trend["spark"]:
        return ""
    vals = [p[i] if i < len(p) else 0 for p in trend["spark"]]
    if len(vals) == 1:
        vals = vals * 2
    lo, hi = min(vals), max(vals)
    if hi - lo < 0.08:  # flat-ish lines stay flat-ish instead of being blown up to full height
        mid = (hi + lo) / 2
        lo, hi = mid - 0.04, mid + 0.04
    pad = 3
    n = len(vals) - 1
    return " ".join(f"{pad + k * (w - 2 * pad) / n:.1f},{pad + (hi - v) / (hi - lo) * (h - 2 * pad):.1f}"
                    for k, v in enumerate(vals))


def snapshot_prices(db, market_id, b):
    outs = get_outcomes(db, market_id)
    ps = lmsr.prices([o["shares"] for o in outs], b)
    db.execute("INSERT INTO price_history (market_id, prices, created_at) VALUES (?, ?, ?)",
               (market_id, json.dumps([round(p, 5) for p in ps]), now_str()))


def open_book_values(db):
    """{user_id: {market_id: cash-out value}} for every unresolved market. Each value is what the user's
    whole holding in that market would sell for right now, so buying can't inflate it."""
    open_rows = db.execute("SELECT id, b FROM markets WHERE status = 'open'").fetchall()
    if not open_rows:
        return {}
    outs_by_mid = {m["id"]: [] for m in open_rows}
    mids = list(outs_by_mid.keys())
    ph = ",".join("?" * len(mids))
    for o in db.execute(f"SELECT id, market_id, shares FROM outcomes WHERE market_id IN ({ph}) ORDER BY market_id, position", mids):
        outs_by_mid[o["market_id"]].append(o)
    markets = {}
    for m in open_rows:
        outs = outs_by_mid[m["id"]]
        markets[m["id"]] = (m["b"], [o["shares"] for o in outs], {o["id"]: i for i, o in enumerate(outs)})
    holdings = {}  # (user_id, market_id) -> share vector
    for p in db.execute("SELECT p.user_id, p.outcome_id, p.shares, o.market_id FROM positions p "
                        "JOIN outcomes o ON o.id = p.outcome_id WHERE p.shares > 0"):
        if p["market_id"] in markets:
            b, q, index = markets[p["market_id"]]
            holdings.setdefault((p["user_id"], p["market_id"]), [0.0] * len(q))[index[p["outcome_id"]]] = p["shares"]
    result = {}
    for (uid, mid), h in holdings.items():
        b, q, _ = markets[mid]
        result.setdefault(uid, {})[mid] = lmsr.liquidation_value(q, b, h)
    return result


NET_WORTH_TTL = 30  # seconds; the cache is also dropped as soon as any trade, balance or loan changes
_net_worth_cache = {}
_net_worth_lock = threading.Lock()


def net_worths(db):
    """Every player's net worth. Valuing every open bet is the slowest thing the site does, so the result is reused
    until something that affects it changes (checked with a few cheap aggregates) or NET_WORTH_TTL passes."""
    version = (DB_PATH,) + tuple(db.execute(
        "SELECT (SELECT MAX(id) FROM trades), (SELECT MAX(id) FROM activity), "
        "(SELECT COUNT(*) FROM users), (SELECT ROUND(SUM(balance), 2) FROM users), "
        "(SELECT COUNT(*) FROM loans), (SELECT ROUND(SUM(owed + principal), 2) FROM loans "
        "WHERE status IN ('offered', 'active')), (SELECT COUNT(*) FROM markets WHERE status = 'open')").fetchone())
    with _net_worth_lock:
        hit = _net_worth_cache.get("v")
        if hit and hit[0] == version and time.time() - hit[1] < NET_WORTH_TTL:
            return dict(hit[2])
    worth = _compute_net_worths(db)
    with _net_worth_lock:
        _net_worth_cache["v"] = (version, time.time(), worth)
    return dict(worth)


def _compute_net_worths(db):
    worth = {u["id"]: u["balance"] for u in db.execute("SELECT id, balance FROM users")}
    for uid, per_market in open_book_values(db).items():
        worth[uid] += sum(per_market.values())
    # loans: money held in an offer is still the lender's; on a running loan the lender is owed what the
    # borrower owes
    for l in db.execute("SELECT lender_id, borrower_id, principal, owed, status FROM loans "
                        "WHERE status IN ('offered', 'active')"):
        if l["status"] == "offered":
            worth[l["lender_id"]] = worth.get(l["lender_id"], 0) + l["principal"]
        else:
            worth[l["lender_id"]] = worth.get(l["lender_id"], 0) + l["owed"]
            worth[l["borrower_id"]] = worth.get(l["borrower_id"], 0) - l["owed"]
    return worth


class TradeError(Exception):
    pass


def log_activity(db, user_id, kind, amount, market=None, detail=""):
    """Record a balance change in the activity log. Call it right after the balance UPDATE, inside the same
    transaction, so balance_after is exact."""
    row = db.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()
    db.execute("INSERT INTO activity (user_id, kind, amount, balance_after, market_id, market_title, detail, created_at) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (user_id, kind, round(amount, 2), row["balance"] if row else None,
                market["id"] if market else None, market["question"] if market else "", detail, now_str()))


def entry_fee_due(db, m, user_id):
    """What this user owes the creator before their next bet in market m: ENTRY_FEE on their first bet, else 0.
    Nothing is owed on markets without a creator, by the creator, or by anyone who traded before fees existed."""
    if m["created_by"] is None or user_id == m["created_by"]:
        return 0.0
    if db.execute("SELECT 1 FROM market_entries WHERE market_id = ? AND user_id = ?", (m["id"], user_id)).fetchone():
        return 0.0
    if db.execute("SELECT 1 FROM trades WHERE market_id = ? AND user_id = ? LIMIT 1", (m["id"], user_id)).fetchone():
        return 0.0
    return ENTRY_FEE


def creator_name(db, m):
    if m["created_by"] is None:
        return None
    row = db.execute("SELECT username FROM users WHERE id = ?", (m["created_by"],)).fetchone()
    return row["username"] if row else None


def refund_entry_fees(db, market_id, user_id=None, reason="Entry fee refunded"):
    """Give entry fees back (all entrants, or one), taking them back from the creator. Must run inside a
    transaction. The creator's balance won't go below zero."""
    m = db.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    sql, args = "SELECT * FROM market_entries WHERE market_id = ? AND fee > 0", [market_id]
    if user_id is not None:
        sql += " AND user_id = ?"
        args.append(user_id)
    for e in db.execute(sql, args).fetchall():
        payer = db.execute("SELECT username FROM users WHERE id = ?", (e["user_id"],)).fetchone()["username"]
        db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (e["fee"], e["user_id"]))
        log_activity(db, e["user_id"], "refund", e["fee"], m, reason)
        db.execute("UPDATE users SET balance = MAX(0, ROUND(balance - ?, 2)) WHERE id = ?", (e["fee"], e["creator_id"]))
        log_activity(db, e["creator_id"], "earning", -e["fee"], m, f"Entry fee returned to {payer}")
        db.execute("DELETE FROM market_entries WHERE market_id = ? AND user_id = ?", (market_id, e["user_id"]))


def leftover_bonus(db, user_id, market_id):
    """The user's share of a resolved market's leftover pot (0 if none)."""
    return db.execute("SELECT COALESCE(SUM(amount), 0) FROM activity WHERE user_id = ? AND market_id = ? "
                      "AND kind = 'payout' AND detail LIKE 'Bonus:%'", (user_id, market_id)).fetchone()[0]


def leftover_and_weights(db, market_id, outcome_id, extra=None):
    """If outcome_id wins: (leftover, {user_id: unhedged winning shares}).

    `extra` = (user_key, bought_outcome_id, shares, money) adds a hypothetical buy on top of the real positions,
    used to preview the bonus a bet would get. user_key can be a user id or any placeholder for a visitor.

    leftover = the pot (money traders put in minus what they took out selling) minus Ⓜ1 per winning share,
    rounded to the cent. It's shared by *unhedged* winning shares: your winning shares minus the shares you hold
    on every outcome at once. A full set of outcomes always costs exactly Ⓜ1 and always pays exactly Ⓜ1, so it
    carries no risk and earns no bonus; otherwise anyone could buy big equal sets and take the leftover from
    the people who actually bet on an outcome."""
    pot = db.execute("SELECT COALESCE(SUM(amount), 0) FROM trades WHERE market_id = ?", (market_id,)).fetchone()[0]
    outcome_ids = [o["id"] for o in get_outcomes(db, market_id)]
    held = {}
    for p in db.execute("SELECT p.user_id, p.outcome_id, p.shares FROM positions p JOIN outcomes o "
                        "ON o.id = p.outcome_id WHERE o.market_id = ?", (market_id,)):
        held.setdefault(p["user_id"], {})[p["outcome_id"]] = p["shares"]
    if extra:
        who, bought, shares, money = extra
        mine = held.setdefault(who, {})
        mine[bought] = mine.get(bought, 0.0) + shares
        pot += money
    winning = sum(h.get(outcome_id, 0.0) for h in held.values())
    weights = {}
    for uid, h in held.items():
        hedged = min(h.get(oid, 0.0) for oid in outcome_ids)
        unhedged = h.get(outcome_id, 0.0) - hedged
        if unhedged > EPS:
            weights[uid] = unhedged
    return cents(pot - winning), weights


def estimated_bonus(db, market_id, outcome_id, user_id, extra=None):
    """If this outcome won right now, roughly what this user's share of the leftover pot would be. It's computed
    exactly like the real payout, but changes with every trade. Pass `extra` to include a bet not yet placed."""
    leftover, weights = leftover_and_weights(db, market_id, outcome_id, extra)
    total = sum(weights.values())
    if leftover < 0.01 or user_id not in weights:
        return 0.0
    return math.floor(leftover * weights[user_id] / total * 100) / 100


def trade_block_reason(db, user_id, market_id):
    """Why this user can't trade in this market, or None if they can."""
    if db.execute("SELECT 1 FROM trade_bans WHERE user_id = ?", (user_id,)).fetchone():
        return "An admin has turned off trading for your account."
    if db.execute("SELECT 1 FROM market_blocks WHERE user_id = ? AND market_id = ?",
                  (user_id, market_id)).fetchone():
        return "An admin has blocked you from betting on this market."
    return None


def refund_user_in_market(db, user_id, market_id, reason="Refund"):
    """Undo a user's bets in an unresolved market: give back what they net-spent, remove their shares
    from the market (reversing their effect on prices) and erase their trades. Must run inside a
    transaction. Returns the amount refunded."""
    m = db.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    if m is None or m["status"] != "open":
        return 0.0  # resolved/cancelled markets are already settled
    had_trades = db.execute("SELECT 1 FROM trades WHERE user_id = ? AND market_id = ? LIMIT 1",
                            (user_id, market_id)).fetchone()
    if not had_trades:
        return 0.0
    net = db.execute("SELECT COALESCE(SUM(amount), 0) FROM trades WHERE user_id = ? AND market_id = ?",
                     (user_id, market_id)).fetchone()[0]
    for p in db.execute("SELECT p.outcome_id, p.shares FROM positions p JOIN outcomes o ON o.id = p.outcome_id "
                        "WHERE p.user_id = ? AND o.market_id = ?", (user_id, market_id)).fetchall():
        db.execute("UPDATE outcomes SET shares = ROUND(shares - ?, 2) WHERE id = ?", (p["shares"], p["outcome_id"]))
    db.execute("DELETE FROM positions WHERE user_id = ? AND outcome_id IN "
               "(SELECT id FROM outcomes WHERE market_id = ?)", (user_id, market_id))
    db.execute("DELETE FROM trades WHERE user_id = ? AND market_id = ?", (user_id, market_id))
    refund = cents(max(net, 0.0))  # if they already sold at a profit, there's nothing to give back
    if refund > 0:
        db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (refund, user_id))
    log_activity(db, user_id, "refund", refund, m, f"{reason}: your bets here were removed")
    refund_entry_fees(db, market_id, user_id, f"{reason}: entry fee returned")
    snapshot_prices(db, market_id, m["b"])
    return refund


def cancel_trade(db, trade_id, reason="Refund"):
    """Undo one share purchase in an unresolved market: give the buyer back what they paid for it, take those
    shares out of their position and out of the market (reversing its effect on prices) and erase the trade.
    If it was their last trade here, their entry fee is returned too. Late-trading fees are not refunded.
    Raises TradeError when the purchase can't be undone. Returns (refund, trade row)."""
    db.execute("BEGIN IMMEDIATE")
    try:
        t = db.execute("SELECT t.*, o.label FROM trades t JOIN outcomes o ON o.id = t.outcome_id "
                       "WHERE t.id = ?", (trade_id,)).fetchone()
        if t is None:
            raise TradeError("That trade no longer exists.")
        m = db.execute("SELECT * FROM markets WHERE id = ?", (t["market_id"],)).fetchone()
        if m is None or m["status"] != "open":
            raise TradeError("Only trades in unresolved markets can be cancelled.")
        if t["shares"] <= 0 or t["via"]:
            raise TradeError("Only share purchases can be cancelled.")
        pos = db.execute("SELECT * FROM positions WHERE user_id = ? AND outcome_id = ?",
                         (t["user_id"], t["outcome_id"])).fetchone()
        held = pos["shares"] if pos else 0.0
        if held < t["shares"] - 1e-6:
            raise TradeError(f"They've since sold some of these shares (they hold {held:,.2f} of "
                             f"{t['shares']:,.2f}). Refund all their bets in this market instead.")
        shares = min(t["shares"], held)
        refund = cents(max(t["amount"], 0.0))
        set_position(db, t["user_id"], t["outcome_id"], held - shares, max(pos["cost"] - refund, 0.0))
        db.execute("UPDATE outcomes SET shares = ROUND(shares - ?, 2) WHERE id = ?", (shares, t["outcome_id"]))
        db.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
        if refund > 0:
            db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (refund, t["user_id"]))
        log_activity(db, t["user_id"], "refund", refund, m,
                     f"{reason}: your purchase of {shares:,.2f} shares of “{t['label']}” was cancelled")
        if not db.execute("SELECT 1 FROM trades WHERE user_id = ? AND market_id = ? LIMIT 1",
                          (t["user_id"], m["id"])).fetchone():
            refund_entry_fees(db, m["id"], t["user_id"], f"{reason}: entry fee returned")
        snapshot_prices(db, m["id"], m["b"])
        db.execute("COMMIT")
        return refund, t
    except Exception:
        db.execute("ROLLBACK")
        raise


def block_user_in_market(db, user_id, market_id):
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("INSERT OR IGNORE INTO market_blocks (market_id, user_id, created_at) VALUES (?, ?, ?)",
                   (market_id, user_id, now_str()))
        refund = refund_user_in_market(db, user_id, market_id, "Refund (blocked from this market)")
        db.execute("COMMIT")
        return refund
    except Exception:
        db.execute("ROLLBACK")
        raise


def ban_user_from_trading(db, user_id):
    """Site-wide trading ban; refunds the user's bets in every unresolved market."""
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("INSERT OR IGNORE INTO trade_bans (user_id, created_at) VALUES (?, ?)", (user_id, now_str()))
        refund = 0.0
        for r in db.execute("SELECT DISTINCT market_id FROM trades WHERE user_id = ?", (user_id,)).fetchall():
            refund += refund_user_in_market(db, user_id, r["market_id"], "Refund (trading turned off)")
        db.execute("COMMIT")
        return refund
    except Exception:
        db.execute("ROLLBACK")
        raise


def late_multiplier(m, now=None):
    """T in the late-trading fee for the time left before betting closes (see LATE_FEE_TIERS); 0 if none applies."""
    left = parse_dt(m["closes_at"]) - (now or datetime.now())
    for limit, t in LATE_FEE_TIERS:
        if left < limit:
            return t
    return 0.0


def late_fee(shares, avg_price, t, buy=True):
    """F = LATE_FEE_RATE x C x P(1 - P) x T, rounded to the cent. Buys pay at least LATE_SNIPE_RATE x C x P^2 x T,
    so near-certain outcomes can't be bought cheaply at the last minute; sells (cashing out) don't."""
    if t <= 0 or shares <= 0:
        return 0.0
    p = min(max(avg_price, 0.0), 1.0)
    fee = LATE_FEE_RATE * shares * p * (1 - p) * t
    if buy:
        fee = max(fee, LATE_SNIPE_RATE * shares * p * p * t)
    return cents(fee)


def check_trade(m, outs, idx, action, value, balance=None, held=None, fee=0.0):
    """Validate a trade and price it. Returns (shares, coins, late): shares bought/sold, coins spent/received
    before fees, and the late-trading fee. Raises TradeError with a user-facing message. balance/held may be None
    to skip those checks (used for logged-out quotes). Shared by execute_trade and the /api/quote preview so they
    always agree. `fee` is the entry fee owed on this buy, if any.

    Priced in whole cents and hundredths of a share, rounding against the trader (bought shares floored, buy cost
    rounded up, sale proceeds floored) so buying and selling back can never mint coins."""
    q_centi = [lmsr.to_centi_shares(o["shares"]) for o in outs]
    label = outs[idx]["label"]
    t = late_multiplier(m)
    if action == "buy":
        spend_cents = lmsr.to_cents(value)
        if spend_cents < lmsr.to_cents(MIN_BET):
            raise TradeError(f"Minimum bet is Ⓜ{MIN_BET:.0f}.")
        try:
            shares_centi, cost_cents = lmsr.quote_buy_cents(q_centi, m["b"], idx, spend_cents)
        except ValueError as e:
            raise TradeError(str(e))
        shares = lmsr.from_centi_shares(shares_centi)
        amount = lmsr.from_cents(cost_cents)
        late = late_fee(shares, amount / shares, t)
        if balance is not None and cost_cents + lmsr.to_cents(fee) + lmsr.to_cents(late) > lmsr.to_cents(balance):
            extras = [f"Ⓜ{amount:,.2f} bet"]
            if fee:
                extras.append(f"Ⓜ{fee:,.0f} entry fee")
            if late:
                extras.append(f"Ⓜ{late:,.2f} late trading fee")
            if len(extras) > 1:
                raise TradeError(f"You need Ⓜ{amount + fee + late:,.2f} ({' + '.join(extras)}) "
                                 f"but have Ⓜ{balance:,.2f}.")
            raise TradeError(f"You don't have enough for that bet. Your balance is Ⓜ{balance:,.2f}.")
        return shares, amount, late
    if action == "sell":
        shares = value
        if held is not None and abs(shares - held) < 1e-6:
            shares = held
        sell_centi = lmsr.to_centi_shares(shares)
        if sell_centi <= 0:
            raise TradeError("Enter a number of shares to sell.")
        if held is not None and sell_centi > lmsr.to_centi_shares(held):
            raise TradeError(f"You only own {held:.2f} shares of “{label}”.")
        if sell_centi > q_centi[idx]:
            raise TradeError("Cannot sell more shares than are outstanding in the market.")
        proceeds_cents = lmsr.quote_sell_cents(q_centi, m["b"], idx, sell_centi)
        if proceeds_cents < 1:
            raise TradeError("That's worth less than Ⓜ0.01. Sell more shares at once.")
        shares = lmsr.from_centi_shares(sell_centi)
        coins = lmsr.from_cents(proceeds_cents)
        return shares, coins, min(late_fee(shares, coins / shares, t, buy=False), coins)
    raise TradeError("Unknown action.")


def set_position(db, user_id, outcome_id, shares, cost):
    """Store a user's holding of one outcome (removing it when nothing is left). Call inside a transaction."""
    if lmsr.to_centi_shares(shares) <= 0:
        db.execute("DELETE FROM positions WHERE user_id = ? AND outcome_id = ?", (user_id, outcome_id))
    else:
        db.execute("INSERT INTO positions (user_id, outcome_id, shares, cost) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT(user_id, outcome_id) DO UPDATE SET shares = excluded.shares, cost = excluded.cost",
                   (user_id, outcome_id, lmsr.from_centi_shares(lmsr.to_centi_shares(shares)), cents(cost)))


def execute_trade(db, user_id, market_id, outcome_id, action, value, min_out=None):
    """Buy (value = coins to spend) or sell (value = shares to sell). Returns a status message.
    min_out (optional) is the least the trader will accept: shares for a buy, coins for a sell. It guards
    against the price moving between the quote and the trade."""
    db.execute("BEGIN IMMEDIATE")
    try:
        m = db.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
        if m is None:
            raise TradeError("Market not found.")
        if market_state(m) != "open":
            raise TradeError("This market is no longer accepting bets.")
        block = trade_block_reason(db, user_id, market_id)
        if block:
            raise TradeError(block)
        block = overdue_block(db, user_id) if action == "buy" else None
        if block:
            raise TradeError(block)
        outs = get_outcomes(db, market_id)
        ids = [o["id"] for o in outs]
        if outcome_id not in ids:
            raise TradeError("Unknown outcome.")
        idx = ids.index(outcome_id)
        label = outs[idx]["label"]
        q = [o["shares"] for o in outs]
        user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        pos = db.execute("SELECT * FROM positions WHERE user_id = ? AND outcome_id = ?",
                         (user_id, outcome_id)).fetchone()
        held = pos["shares"] if pos else 0.0
        held_cost = pos["cost"] if pos else 0.0

        fee = entry_fee_due(db, m, user_id) if action == "buy" else 0.0
        shares, coins, late = check_trade(m, outs, idx, action, value, user["balance"], held, fee)
        if min_out is not None and (shares if action == "buy" else coins) < min_out - 1e-9:
            raise TradeError("The price moved since your quote. Check the new quote and try again.")
        if fee:
            creator = creator_name(db, m)
            db.execute("UPDATE users SET balance = ROUND(balance - ?, 2) WHERE id = ?", (fee, user_id))
            log_activity(db, user_id, "fee", -fee, m, f"Entry fee (first bet), paid to {creator}")
            db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (fee, m["created_by"]))
            log_activity(db, m["created_by"], "earning", fee, m, f"Entry fee from {user['username']}")
            db.execute("INSERT INTO market_entries (market_id, user_id, creator_id, fee, created_at) VALUES (?, ?, ?, ?, ?)",
                       (market_id, user_id, m["created_by"], fee, now_str()))
        q[idx] += shares if action == "buy" else -shares
        now_pct = lmsr.prices(q, m["b"])[idx] * 100
        shares_centi, coins_cents, held_centi = (lmsr.to_centi_shares(shares), lmsr.to_cents(coins),
                                                 lmsr.to_centi_shares(held))
        if action == "buy":
            d_shares_centi, d_cash = shares_centi, -coins
            new_shares = lmsr.from_centi_shares(held_centi + shares_centi)
            new_cost = lmsr.from_cents(lmsr.to_cents(held_cost) + coins_cents)
            msg = (f"You paid Ⓜ{coins:,.2f} for {shares:,.2f} shares of “{label}”"
                   + (f" plus a Ⓜ{fee:,.0f} entry fee to {creator}" if fee else "") +
                   f". If it wins they pay Ⓜ{new_shares:,.2f} in total. Price now {now_pct:.0f}%.")
            detail = f"Bought {shares:,.2f} shares of “{label}”"
        else:
            d_shares_centi, d_cash = -shares_centi, coins
            new_shares = lmsr.from_centi_shares(held_centi - shares_centi)
            new_cost = held_cost * (new_shares / held) if held > 0 else 0.0
            msg = f"You received Ⓜ{coins:,.2f} for {shares:,.2f} shares of “{label}”. Price now {now_pct:.0f}%."
            detail = f"Sold {shares:,.2f} shares of “{label}”"

        d_shares = lmsr.from_centi_shares(d_shares_centi)
        db.execute("UPDATE outcomes SET shares = ? WHERE id = ?",
                   (lmsr.from_centi_shares(lmsr.to_centi_shares(outs[idx]["shares"]) + d_shares_centi), outcome_id))
        db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (d_cash, user_id))
        log_activity(db, user_id, action, d_cash, m, detail)
        if late:
            t = late_multiplier(m)
            # Paid to the market's creator; on markets without one, or when the creator trades their own market
            # late, it leaves the game instead (paying yourself would cancel the penalty).
            payee = m["created_by"] if m["created_by"] not in (None, user_id) else None
            payee_name = creator_name(db, m) if payee else None
            db.execute("UPDATE users SET balance = ROUND(balance - ?, 2) WHERE id = ?", (late, user_id))
            log_activity(db, user_id, "fee", -late, m,
                         f"Late trading fee (×{fmt_number(t)}, {shares:,.2f} shares at {coins / shares * 100:.0f}¢)"
                         + (f", paid to {payee_name}" if payee else ""))
            if payee:
                db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (late, payee))
                log_activity(db, payee, "earning", late, m, f"Late trading fee from {user['username']}")
            msg += f" Late trading fee: Ⓜ{late:,.2f}" + (f" (paid to {payee_name})." if payee else ".")
        msg += f" Balance: Ⓜ{db.execute('SELECT balance FROM users WHERE id = ?', (user_id,)).fetchone()[0]:,.2f}."
        set_position(db, user_id, outcome_id, new_shares, new_cost)
        db.execute("INSERT INTO trades (user_id, market_id, outcome_id, shares, amount, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?)", (user_id, market_id, outcome_id, d_shares, -d_cash, now_str()))
        snapshot_prices(db, market_id, m["b"])
        process_limit_orders(db, market_id)
        db.execute("COMMIT")
        return msg
    except Exception:
        db.execute("ROLLBACK")
        raise


def _try_fill_limit_order(db, m, outs, idx, order):
    """Attempt to fill a triggered limit order up to its limit_price without putting the user into debt
    or exceeding held shares. Must be called inside an open transaction. Returns fill info dict or None."""
    uid = order["user_id"]
    market_id = m["id"]
    outcome_id = order["outcome_id"]
    action = order["action"]
    limit_price = float(order["limit_price"])

    if trade_block_reason(db, uid, market_id):
        return None
    if action == "buy" and overdue_block(db, uid):
        return None

    user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if user is None:
        return None
    pos = db.execute("SELECT * FROM positions WHERE user_id = ? AND outcome_id = ?",
                     (uid, outcome_id)).fetchone()
    held = pos["shares"] if pos else 0.0
    held_cost = pos["cost"] if pos else 0.0
    held_centi = lmsr.to_centi_shares(held)
    q_centi = [lmsr.to_centi_shares(o["shares"]) for o in outs]
    q = [x / lmsr.SHARES_SCALE for x in q_centi]
    t = late_multiplier(m)
    label = outs[idx]["label"]

    if action == "buy":
        rem_cents = lmsr.to_cents(order["remaining"])
        if rem_cents <= 0:
            return None
        fee = entry_fee_due(db, m, uid)
        fee_cents = lmsr.to_cents(fee)
        bal_cents = lmsr.to_cents(user["balance"])
        if bal_cents <= fee_cents:
            return None
        max_sc_price = lmsr.max_buy_shares_to_price_centi(q_centi, m["b"], idx, limit_price)
        if max_sc_price <= 0:
            return None

        # Cap shares by remaining order budget (coins before fees) and by user's available cash (never debt).
        max_spend_cents = min(rem_cents, bal_cents - fee_cents)
        if max_spend_cents <= 0:
            return None
        try:
            max_sc_spend, _ = lmsr.quote_buy_cents(q_centi, m["b"], idx, max_spend_cents)
        except ValueError:
            return None

        hi = min(max_sc_price, max_sc_spend)
        if hi <= 0:
            return None

        def buy_total_cents(sc):
            cost_c = math.ceil(lmsr.cost_for_shares(q, m["b"], idx, sc / lmsr.SHARES_SCALE) * lmsr.CENTS_PER_COIN)
            if cost_c <= 0:
                return 0, 0, 0
            sh = sc / lmsr.SHARES_SCALE
            amt = cost_c / lmsr.CENTS_PER_COIN
            late_c = lmsr.to_cents(late_fee(sh, amt / sh, t, buy=True))
            return cost_c, late_c, cost_c + fee_cents + late_c

        cost_cents, late_cents, total_cents = buy_total_cents(hi)
        if total_cents > bal_cents or cost_cents > rem_cents:
            lo, best = 1, 0
            while lo <= hi:
                mid_sc = (lo + hi) // 2
                c_c, l_c, tot_c = buy_total_cents(mid_sc)
                if tot_c <= bal_cents and c_c <= rem_cents:
                    best = mid_sc
                    lo = mid_sc + 1
                else:
                    hi = mid_sc - 1
            if best <= 0:
                return None
            shares_centi = best
            cost_cents, late_cents, _ = buy_total_cents(shares_centi)
        else:
            shares_centi = hi

        if shares_centi <= 0 or cost_cents <= 0:
            return None

        shares = lmsr.from_centi_shares(shares_centi)
        coins = lmsr.from_cents(cost_cents)
        late = lmsr.from_cents(late_cents)

        if fee:
            creator = creator_name(db, m)
            db.execute("UPDATE users SET balance = ROUND(balance - ?, 2) WHERE id = ?", (fee, uid))
            log_activity(db, uid, "fee", -fee, m, f"Entry fee (first bet), paid to {creator}")
            db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (fee, m["created_by"]))
            log_activity(db, m["created_by"], "earning", fee, m, f"Entry fee from {user['username']}")
            db.execute("INSERT INTO market_entries (market_id, user_id, creator_id, fee, created_at) VALUES (?, ?, ?, ?, ?)",
                       (market_id, uid, m["created_by"], fee, now_str()))

        d_shares_centi, d_cash = shares_centi, -coins
        new_shares = lmsr.from_centi_shares(held_centi + shares_centi)
        new_cost = lmsr.from_cents(lmsr.to_cents(held_cost) + cost_cents)
        detail = f"Limit buy: {shares:,.2f} shares of “{label}” (limit {limit_price * 100:.0f}%)"
        new_rem_cents = max(0, rem_cents - cost_cents)
        new_rem = lmsr.from_cents(new_rem_cents)
        new_status = "filled" if new_rem_cents <= 0 else "open"

    else:  # sell
        rem_sc = lmsr.to_centi_shares(order["remaining"])
        if rem_sc <= 0 or held_centi <= 0:
            return None
        max_sc_price = lmsr.max_sell_shares_to_price_centi(q_centi, m["b"], idx, limit_price)
        if max_sc_price <= 0:
            return None
        shares_centi = min(max_sc_price, rem_sc, held_centi, q_centi[idx])
        if shares_centi <= 0:
            return None
        proceeds_cents = lmsr.quote_sell_cents(q_centi, m["b"], idx, shares_centi)
        if proceeds_cents < 1:
            return None
        shares = lmsr.from_centi_shares(shares_centi)
        coins = lmsr.from_cents(proceeds_cents)
        fee = 0.0
        late = min(late_fee(shares, coins / shares, t, buy=False), coins)

        d_shares_centi, d_cash = -shares_centi, coins
        new_shares = lmsr.from_centi_shares(held_centi - shares_centi)
        new_cost = held_cost * (new_shares / held) if held > 0 else 0.0
        detail = f"Limit sell: {shares:,.2f} shares of “{label}” (limit {limit_price * 100:.0f}%)"
        new_rem_sc = max(0, rem_sc - shares_centi)
        new_rem = lmsr.from_centi_shares(new_rem_sc)
        new_status = "filled" if new_rem_sc <= 0 else "open"

    now = now_str()
    d_shares = lmsr.from_centi_shares(d_shares_centi)
    db.execute("UPDATE outcomes SET shares = ? WHERE id = ?",
               (lmsr.from_centi_shares(q_centi[idx] + d_shares_centi), outcome_id))
    db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (d_cash, uid))
    log_activity(db, uid, action, d_cash, m, detail)
    if late:
        payee = m["created_by"] if m["created_by"] not in (None, uid) else None
        payee_name = creator_name(db, m) if payee else None
        db.execute("UPDATE users SET balance = ROUND(balance - ?, 2) WHERE id = ?", (late, uid))
        log_activity(db, uid, "fee", -late, m,
                     f"Late trading fee (×{fmt_number(t)}, {shares:,.2f} shares at {coins / shares * 100:.0f}¢)"
                     + (f", paid to {payee_name}" if payee else ""))
        if payee:
            db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (late, payee))
            log_activity(db, payee, "earning", late, m, f"Late trading fee from {user['username']}")
    set_position(db, uid, outcome_id, new_shares, new_cost)
    db.execute("INSERT INTO trades (user_id, market_id, outcome_id, shares, amount, created_at, via) "
               "VALUES (?, ?, ?, ?, ?, ?, 'limit')",
               (uid, market_id, outcome_id, d_shares, -d_cash, now))
    db.execute("UPDATE limit_orders SET remaining = ?, status = ?, updated_at = ? WHERE id = ?",
               (new_rem, new_status, now, order["id"]))
    return {"order_id": order["id"], "user_id": uid, "action": action,
            "shares": shares, "coins": coins, "fee": fee, "late": late}


def process_limit_orders(db, market_id, max_steps=100):
    """Fill any triggered open limit orders on `market_id` in price-time priority.
    Must be called inside an open DB transaction. Returns list of fill dicts."""
    m = db.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    if m is None or market_state(m) != "open":
        return []
    fills = []
    skipped = {}  # order_id -> (balance_cents, held_centi, rounded_price)
    for _ in range(max_steps):
        outs = get_outcomes(db, market_id)
        if not outs:
            break
        id_to_idx = {o["id"]: i for i, o in enumerate(outs)}
        p_list = lmsr.prices([o["shares"] for o in outs], m["b"])
        orders = db.execute(
            "SELECT * FROM limit_orders WHERE market_id = ? AND status = 'open' ORDER BY id ASC",
            (market_id,),
        ).fetchall()
        candidates = []
        for o in orders:
            idx = id_to_idx.get(o["outcome_id"])
            if idx is None:
                continue
            p = p_list[idx]
            lp = float(o["limit_price"])
            if o["action"] == "buy" and p <= lp + 1e-12:
                candidates.append((-(lp - p), o["id"], idx, o))
            elif o["action"] == "sell" and p >= lp - 1e-12:
                candidates.append((-(p - lp), o["id"], idx, o))
        if not candidates:
            break
        candidates.sort(key=lambda item: (item[0], item[1]))
        filled_any = False
        for _, oid, idx, order in candidates:
            u_row = db.execute("SELECT balance FROM users WHERE id = ?", (order["user_id"],)).fetchone()
            p_row = db.execute("SELECT shares FROM positions WHERE user_id = ? AND outcome_id = ?",
                               (order["user_id"], order["outcome_id"])).fetchone()
            state_key = (
                lmsr.to_cents(u_row["balance"]) if u_row else 0,
                lmsr.to_centi_shares(p_row["shares"]) if p_row else 0,
                round(p_list[idx], 8),
            )
            if skipped.get(oid) == state_key:
                continue
            res = _try_fill_limit_order(db, m, outs, idx, order)
            if res is not None:
                fills.append(res)
                skipped.pop(oid, None)
                filled_any = True
                break
            skipped[oid] = state_key
        if not filled_any:
            break
    if fills:
        snapshot_prices(db, market_id, m["b"])
    return fills


def place_limit_order(db, user_id, market_id, outcome_id, action, value, limit_price):
    """Create a standing limit order (buy or sell) and immediately process it if marketable."""
    if action not in ("buy", "sell"):
        raise TradeError("Unknown action.")
    if not math.isfinite(limit_price) or limit_price < 0.01 - 1e-9 or limit_price > 0.99 + 1e-9:
        raise TradeError("Limit price must be between 1% and 99%.")
    limit_price = round(min(max(limit_price, 0.01), 0.99), 4)
    if action == "buy":
        amount = lmsr.from_cents(lmsr.to_cents(value))
        if lmsr.to_cents(amount) < lmsr.to_cents(MIN_BET):
            raise TradeError(f"Minimum bet is Ⓜ{MIN_BET:.0f}.")
    else:
        amount = lmsr.from_centi_shares(lmsr.to_centi_shares(value))
        if lmsr.to_centi_shares(amount) <= 0:
            raise TradeError("Enter a number of shares to sell.")

    db.execute("BEGIN IMMEDIATE")
    try:
        m = db.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
        if m is None:
            raise TradeError("Market not found.")
        if market_state(m) != "open":
            raise TradeError("This market is no longer accepting bets.")
        block = trade_block_reason(db, user_id, market_id)
        if block:
            raise TradeError(block)
        if action == "buy":
            block = overdue_block(db, user_id)
            if block:
                raise TradeError(block)
        outs = get_outcomes(db, market_id)
        ids = [o["id"] for o in outs]
        if outcome_id not in ids:
            raise TradeError("Unknown outcome.")
        label = outs[ids.index(outcome_id)]["label"]
        now = now_str()
        cur = db.execute(
            "INSERT INTO limit_orders (user_id, market_id, outcome_id, action, limit_price, amount, remaining, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (user_id, market_id, outcome_id, action, limit_price, amount, amount, now, now),
        )
        oid = cur.lastrowid
        fills = process_limit_orders(db, market_id)
        order = db.execute("SELECT * FROM limit_orders WHERE id = ?", (oid,)).fetchone()
        db.execute("COMMIT")

        my_fills = [f for f in fills if f["order_id"] == oid]
        filled_shares = sum(f["shares"] for f in my_fills)
        filled_coins = sum(f["coins"] for f in my_fills)
        pct_val = round(limit_price * 100, 1)
        pct_str = f"{pct_val:.0f}%" if pct_val == int(pct_val) else f"{pct_val:.1f}%"
        if order["status"] == "filled":
            if action == "buy":
                return (f"Limit buy filled immediately: paid Ⓜ{filled_coins:,.2f} for {filled_shares:,.2f} shares "
                        f"of “{label}” (limit {pct_str}).")
            return (f"Limit sell filled immediately: sold {filled_shares:,.2f} shares of “{label}” "
                    f"for Ⓜ{filled_coins:,.2f} (limit {pct_str}).")
        if my_fills:
            if action == "buy":
                return (f"Limit buy partially filled (Ⓜ{filled_coins:,.2f} for {filled_shares:,.2f} shares of “{label}”). "
                        f"Remaining Ⓜ{order['remaining']:,.2f} stays open at {pct_str} or lower.")
            return (f"Limit sell partially filled ({filled_shares:,.2f} shares of “{label}” for Ⓜ{filled_coins:,.2f}). "
                    f"Remaining {order['remaining']:,.2f} shares stay open at {pct_str} or higher.")
        if action == "buy":
            return (f"Limit buy placed: Ⓜ{amount:,.2f} of “{label}” at {pct_str} or lower. "
                    f"Your cash stays spendable until it triggers.")
        return f"Limit sell placed: {amount:,.2f} shares of “{label}” at {pct_str} or higher."
    except Exception:
        db.execute("ROLLBACK")
        raise


def cancel_limit_order(db, user_id, order_id):
    """Cancel a single open limit order owned by user_id. Returns True if cancelled."""
    now = now_str()
    cur = db.execute(
        "UPDATE limit_orders SET status = 'cancelled', updated_at = ? WHERE id = ? AND user_id = ? AND status = 'open'",
        (now, order_id, user_id),
    )
    db.commit()
    return cur.rowcount > 0


def cancel_all_limit_orders(db, user_id):
    """Cancel all open limit orders owned by user_id. Returns count cancelled."""
    now = now_str()
    cur = db.execute(
        "UPDATE limit_orders SET status = 'cancelled', updated_at = ? WHERE user_id = ? AND status = 'open'",
        (now, user_id),
    )
    db.commit()
    return cur.rowcount


def resolve_market(db, market_id, outcome_id, resolved_by=None, automatic=False):
    """Pay out a market. `automatic` is used when a challenge window ends: it only goes ahead if the proposed
    result is still the same, the window is over and nobody has an unhandled dispute."""
    db.execute("BEGIN IMMEDIATE")
    try:
        m = db.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
        if m is None or m["status"] != "open":
            raise TradeError("Only unresolved markets can be resolved.")
        if outcome_id not in [o["id"] for o in get_outcomes(db, market_id)]:
            raise TradeError("Unknown outcome.")
        if automatic:
            if (m["proposed_outcome_id"] != outcome_id or not m["finalizes_at"] or m["finalizes_at"] > now_str()
                    or db.execute("SELECT 1 FROM disputes WHERE market_id = ? AND handled_at IS NULL",
                                  (market_id,)).fetchone()):
                raise TradeError("Not ready to finalize.")
        outs = {o["id"]: o["label"] for o in get_outcomes(db, market_id)}
        winners = db.execute("SELECT user_id, shares FROM positions WHERE outcome_id = ?", (outcome_id,)).fetchall()
        for w in winners:
            db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (w["shares"], w["user_id"]))
            log_activity(db, w["user_id"], "payout", w["shares"], m,
                         f"Won: {w['shares']:,.2f} “{outs[outcome_id]}” shares paid Ⓜ1 each")
        # Whatever is left in the pot after paying Ⓜ1 per winning share goes to the winners in proportion to their
        # unhedged winning shares (see leftover_and_weights). If the pot is short, the house covers the gap.
        # Positions aren't changed by paying out, so computing this after the Ⓜ1 payments above is fine.
        leftover, weights = leftover_and_weights(db, market_id, outcome_id)
        total_weight = sum(weights.values())
        leftover_paid = 0.0
        if leftover >= 0.01 and total_weight > EPS:
            for uid, weight in weights.items():
                cut = math.floor(leftover * weight / total_weight * 100) / 100
                if cut >= 0.01:
                    db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (cut, uid))
                    log_activity(db, uid, "payout", cut, m,
                                 f"Bonus: your share of the Ⓜ{leftover:,.2f} left in the pot")
                    leftover_paid += cut
        for p in db.execute("SELECT p.user_id, p.shares, p.outcome_id FROM positions p JOIN outcomes o "
                            "ON o.id = p.outcome_id WHERE o.market_id = ? AND p.outcome_id != ? AND p.shares > 1e-9",
                            (market_id, outcome_id)).fetchall():
            log_activity(db, p["user_id"], "loss", 0, m,
                         f"Lost: {p['shares']:,.2f} “{outs[p['outcome_id']]}” shares paid nothing")
        db.execute("UPDATE markets SET status = 'resolved', resolved_outcome_id = ?, resolved_at = ?, "
                   "resolved_by = ?, leftover_paid = ? WHERE id = ?",
                   (outcome_id, now_str(), resolved_by, cents(leftover_paid), market_id))
        db.execute("UPDATE disputes SET handled_at = ? WHERE market_id = ? AND handled_at IS NULL",
                   (now_str(), market_id))
        db.execute("UPDATE limit_orders SET status = 'cancelled', updated_at = ? "
                   "WHERE market_id = ? AND status = 'open'", (now_str(), market_id))
        db.execute("COMMIT")
        return len(winners)
    except Exception:
        db.execute("ROLLBACK")
        raise


def propose_result(db, market_id, outcome_id, admin_id):
    """An admin names the winner. Nobody is paid yet: the result finalizes after the challenge window.
    Proposing again (e.g. after a dispute) replaces the proposal, restarts the window and clears disputes."""
    db.execute("BEGIN IMMEDIATE")
    try:
        m = db.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
        if m is None or m["status"] != "open":
            raise TradeError("Only unresolved markets can have a result proposed.")
        if datetime.now() < parse_dt(m["closes_at"]):
            raise TradeError("Betting hasn't closed yet. Propose a result once it has.")
        if outcome_id not in [o["id"] for o in get_outcomes(db, market_id)]:
            raise TradeError("Unknown outcome.")
        now = datetime.now()
        db.execute("UPDATE markets SET proposed_outcome_id = ?, proposed_by = ?, proposed_at = ?, finalizes_at = ? "
                   "WHERE id = ?", (outcome_id, admin_id, now.strftime("%Y-%m-%d %H:%M:%S"),
                                    (now + DISPUTE_WINDOW).strftime("%Y-%m-%d %H:%M:%S"), market_id))
        db.execute("UPDATE disputes SET handled_at = ? WHERE market_id = ? AND handled_at IS NULL",
                   (now_str(), market_id))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def retract_result(db, market_id):
    """Take back a proposed result before it finalizes."""
    db.execute("BEGIN IMMEDIATE")
    try:
        m = db.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
        if m is None or m["status"] != "open" or m["proposed_outcome_id"] is None:
            raise TradeError("There's no proposed result to withdraw.")
        db.execute("UPDATE markets SET proposed_outcome_id = NULL, proposed_by = NULL, proposed_at = NULL, "
                   "finalizes_at = NULL, ou_result = NULL WHERE id = ?", (market_id,))
        db.execute("UPDATE disputes SET handled_at = ? WHERE market_id = ? AND handled_at IS NULL",
                   (now_str(), market_id))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def finalize_due(db):
    """Pay out every market whose challenge window ended with no unhandled dispute. Called on each request,
    so results settle on time without a background job."""
    due = db.execute("SELECT id, proposed_outcome_id, proposed_by FROM markets WHERE status = 'open' "
                     "AND proposed_outcome_id IS NOT NULL AND finalizes_at <= ?", (now_str(),)).fetchall()
    for m in due:
        try:
            resolve_market(db, m["id"], m["proposed_outcome_id"], m["proposed_by"], automatic=True)
        except TradeError:
            pass  # a dispute arrived, or another request already finalized it


def add_dispute(db, user_id, market_id, reason):
    m = db.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    if m is None or m["status"] != "open" or m["proposed_outcome_id"] is None:
        raise TradeError("There's no proposed result to dispute.")
    if not db.execute("SELECT 1 FROM trades WHERE user_id = ? AND market_id = ? LIMIT 1",
                      (user_id, market_id)).fetchone():
        raise TradeError("Only people who traded in this market can dispute its result.")
    if not 10 <= len(reason) <= 500:
        raise TradeError("Explain the problem in 10–500 characters.")
    if db.execute("SELECT 1 FROM disputes WHERE market_id = ? AND user_id = ? AND handled_at IS NULL",
                  (market_id, user_id)).fetchone():
        raise TradeError("You already disputed this result.")
    db.execute("INSERT INTO disputes (market_id, user_id, reason, created_at) VALUES (?, ?, ?, ?)",
               (market_id, user_id, reason, now_str()))


def cancel_market(db, market_id):
    """Void a market: everyone gets back what they net-spent on it."""
    db.execute("BEGIN IMMEDIATE")
    try:
        m = db.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
        if m is None or m["status"] != "open":
            raise TradeError("Only unresolved markets can be cancelled.")
        rows = db.execute("SELECT user_id, SUM(amount) AS net FROM trades WHERE market_id = ? GROUP BY user_id",
                          (market_id,)).fetchall()
        for r in rows:
            if r["net"] > 0:
                db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (r["net"], r["user_id"]))
            log_activity(db, r["user_id"], "refund", max(r["net"], 0), m, "Refund: market cancelled")
        refund_entry_fees(db, market_id, reason="Entry fee refunded: market cancelled")
        # A proposed or disputed result is moot once the market is void.
        db.execute("UPDATE disputes SET handled_at = ? WHERE market_id = ? AND handled_at IS NULL",
                   (now_str(), market_id))
        db.execute("UPDATE markets SET status = 'cancelled', resolved_at = ?, proposed_outcome_id = NULL, "
                   "proposed_by = NULL, proposed_at = NULL, finalizes_at = NULL WHERE id = ?", (now_str(), market_id))
        db.execute("UPDATE limit_orders SET status = 'cancelled', updated_at = ? "
                   "WHERE market_id = ? AND status = 'open'", (now_str(), market_id))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def delete_market(db, market_id):
    """Remove a market entirely. If it was never settled, everyone's net spend is refunded first.
    Payouts from an already-resolved market are kept."""
    db.execute("BEGIN IMMEDIATE")
    try:
        m = db.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
        if m is None:
            raise TradeError("Market not found.")
        if m["status"] == "open":
            for r in db.execute("SELECT user_id, SUM(amount) AS net FROM trades WHERE market_id = ? GROUP BY user_id",
                                (market_id,)).fetchall():
                if r["net"] > 0:
                    db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?",
                               (r["net"], r["user_id"]))
                log_activity(db, r["user_id"], "refund", max(r["net"], 0), m, "Refund: market deleted")
            refund_entry_fees(db, market_id, reason="Entry fee refunded: market deleted")
        db.execute("DELETE FROM positions WHERE outcome_id IN (SELECT id FROM outcomes WHERE market_id = ?)",
                   (market_id,))
        db.execute("UPDATE proposals SET market_id = NULL WHERE market_id = ?", (market_id,))
        for table in ("limit_orders", "trades", "price_history", "comments", "market_blocks", "market_entries",
                      "disputes", "clarifications", "outcomes"):
            db.execute(f"DELETE FROM {table} WHERE market_id = ?", (market_id,))
        db.execute("DELETE FROM markets WHERE id = ?", (market_id,))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def create_market(db, question, description, category, closes_at, labels, b, created_by=None,
                  rules_source="", rules_otherwise="", ou_line=None, ou_unit="", charge_fee=False):
    db.execute("BEGIN IMMEDIATE")
    try:
        if charge_fee:  # an admin making a market directly pays the same fee as a proposer
            bal = db.execute("SELECT balance FROM users WHERE id = ?", (created_by,)).fetchone()["balance"]
            if bal + EPS < CREATION_FEE:
                raise TradeError(f"Making a market costs Ⓜ{CREATION_FEE:,.0f}. You have Ⓜ{bal:,.2f}.")
            db.execute("UPDATE users SET balance = ROUND(balance - ?, 2) WHERE id = ?", (CREATION_FEE, created_by))
            log_activity(db, created_by, "fee", -CREATION_FEE, None, f"Market creation fee: “{question}”")
        cur = db.execute(
            "INSERT INTO markets (question, description, category, b, closes_at, created_by, created_at, "
            "rules_source, rules_otherwise, ou_line, ou_unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (question, description, category, b, closes_at, created_by, now_str(), rules_source, rules_otherwise,
             ou_line, ou_unit or ""))
        mid = cur.lastrowid
        for i, label in enumerate(labels):
            db.execute("INSERT INTO outcomes (market_id, label, position) VALUES (?, ?, ?)", (mid, label, i))
        snapshot_prices(db, mid, b)
        db.execute("COMMIT")
        return mid
    except Exception:
        db.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- auth & csrf

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def load_user_and_check_csrf():
    g.user = None
    g.owner_id = owner_id(get_db())
    if session.get("uid"):
        db = get_db()
        g.user = db.execute("SELECT * FROM users WHERE id = ?", (session["uid"],)).fetchone()
        if g.user is None:
            session.pop("uid")
        elif session.get("gen", 0) != g.user["session_gen"]:
            log_event("session_expired", user=g.user, detail="password changed or reset since this session started")
            session.clear()  # password changed elsewhere: this session is no longer valid
            g.user = None
        elif g.user["id"] == g.owner_id and not g.user["is_admin"]:
            db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (g.user["id"],))  # owner is always admin
            g.user = db.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    if request.endpoint not in PWA_ENDPOINTS:
        finalize_due(get_db())
        collect_overdue_loans(get_db())
    if request.method == "POST":
        token = request.form.get("csrf_token", "")
        if not token or not secrets.compare_digest(token, session.get("_csrf", "")):
            log_event("csrf_failed", detail=f"{request.method} {request.endpoint}")
            abort(400, "Your form expired. Go back, reload the page and try again.")


EMAIL_EXEMPT = {"account_email", "logout", "static", "how_it_works", "manifest", "service_worker", "offline"}
# files the browser fetches on its own (app install, offline support): no per-request bookkeeping needed
PWA_ENDPOINTS = {"static", "manifest", "service_worker", "offline"}


@app.before_request
def require_confirmed_email():
    """Logged-in accounts without a confirmed email are sent to add one first (existing accounts after the switch
    to emails, and new accounts until they type in their code)."""
    if g.get("user") is not None and not g.user["email_verified"] and request.endpoint not in EMAIL_EXEMPT:
        if request.endpoint == "quote":
            return jsonify(ok=False, error="Confirm your email first."), 403
        return redirect(url_for("account_email"))


# ---------------------------------------------------------------- alt detection (see security.py)

DEVICE_COOKIE = "mams_did"
SIGHTING_EVERY = 1800  # seconds between re-checks of a logged-in visitor (sessions last 30 days)
HOLD_ALLOWED = {"logout", "account_email", "change_password"}  # the only POSTs a held account may make


@app.before_request
def device_cookie():
    """Give every browser a random ID cookie. Clearing cookies resets it, so it's only one clue among several."""
    if request.endpoint in PWA_ENDPOINTS:
        return
    did = request.cookies.get(DEVICE_COOKIE, "")
    g.device_id = did if re.fullmatch(r"[0-9a-f]{32}", did) else ""
    if not g.device_id:
        g.device_id = secrets.token_hex(16)
        g.new_device = True


@app.after_request
def set_device_cookie(resp):
    if g.get("new_device"):
        resp.set_cookie(DEVICE_COOKIE, g.device_id, max_age=2 * 365 * 86400, httponly=True,
                        secure=PRODUCTION, samesite="Lax")
    return resp


@app.before_request
def hold_check():
    """Held accounts (flagged as a likely alt, waiting for an admin) can read everything but change nothing.
    Default-deny: any POST outside HOLD_ALLOWED is refused, so a new route is covered without remembering."""
    if (g.get("user") is not None and g.user["held"] and not g.user["is_admin"] and request.method == "POST"
            and request.endpoint not in HOLD_ALLOWED):
        log_event("hold_blocked", detail=f"{request.method} {request.endpoint}")
        flash("Your account is waiting for an admin to review it. You can look around, but you can't bet, claim, "
              "lend or post until it's approved.", "error")
        return redirect(referrer_or(url_for("index")))


@app.before_request
def periodic_sighting():
    u = g.get("user")
    if u is None or request.method != "GET" or request.endpoint in PWA_ENDPOINTS:
        return
    if time.time() - session.get("_seen", 0) < SIGHTING_EVERY:
        return
    session["_seen"] = time.time()
    db = get_db()
    try:
        if record_sighting(db, u["id"], u["username"]):
            log_event("sighting_new", user=u, detail="a new IP or device was seen on a page view")
            apply_risk(db, u["id"])
    except Exception:
        app.logger.exception("alt detection failed on a page view")


def record_sighting(db, uid, username, event=None, fp_raw=None, extra=None):
    """Remember this visitor's IP, device and browser fingerprint against the account, and (for sign-up, log-in and
    email confirmation) add a row to the login history. Loopback and private IPs are ignored as clues. Returns how
    many values were new for this account."""
    t = time.time()
    ip = (request.remote_addr or "")[:64]
    ua = request.headers.get("User-Agent", "")[:400]
    did = g.get("device_id", "")
    digest = security.fp_digest(fp_raw) if fp_raw else None
    fresh = 0
    values = []
    if security.usable_ip(ip):
        values += [("ip", ip), ("net", security.net_of(ip))]
    if did:
        values.append(("device", did))
    if digest:
        if digest["token"] and digest["token"] != did:
            values.append(("device", digest["token"]))
        values += list(digest["parts"].items())
    for kind, value in values:
        fresh += bool(security.record(db, uid, username, kind, value, t))
    if event:
        db.execute("INSERT INTO login_events (user_id, username, event, ip, ua, device_id, fp_hash, fp_json, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (uid, username, event, ip, ua, did, digest["parts"].get("fp", "") if digest else "",
                    json.dumps({**(digest["meta"] if digest else {}), **(extra or {})}), t))
    return fresh


def risk_config(db):
    return {"common_nets": security.parse_network_list(get_setting(db, "common_networks")),
            "extra_blocked": security.parse_domain_list(get_setting(db, "email_blocked")),
            "extra_shady": security.parse_domain_list(get_setting(db, "email_shady")),
            "tz_name": os.environ.get("TZ", "America/New_York")}


def risk_pool(db):
    return security.email_pool(db)


def apply_risk(db, uid, pool=None, cfg=None):
    """Score an account and act on it. A new account (younger than NEW_ACCOUNT_DAYS) scoring at least HOLD_THRESHOLD,
    with evidence beyond IP/fingerprint, is held for admin approval. Anyone else over FLAG_THRESHOLD is only flagged.
    Held stays held until an admin decides; approved accounts are only re-flagged by a new kind of signal.
    Admins and the owner are never scored. Returns the new status (None if not scored)."""
    u = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if u is None:
        return None
    if u["is_admin"] or uid == owner_id(db):
        db.execute("DELETE FROM user_risk WHERE user_id = ?", (uid,))
        db.execute("UPDATE users SET held = 0 WHERE id = ? AND held = 1", (uid,))
        return None
    r = security.compute_risk(db, uid, pool, **(cfg or risk_config(db)))
    cur = db.execute("SELECT * FROM user_risk WHERE user_id = ?", (uid,)).fetchone()
    status = cur["status"] if cur else "clear"
    ack = set(json.loads(cur["ack"])) if cur else set()
    reviewed = bool(cur and cur["reviewed_at"])  # an admin has already decided on this account: never auto-hold it
    try:
        age_days = (datetime.now() - parse_dt(u["created_at"])).days
    except ValueError:
        age_days = 9999
    if status == "held":
        new = "held"
    elif status == "approved":
        new = "flagged" if (r["kinds"] - ack) and r["score"] >= security.FLAG_THRESHOLD else "approved"
    elif (age_days < security.NEW_ACCOUNT_DAYS and r["score"] >= security.HOLD_THRESHOLD and r["qualifies"]
          and not reviewed):
        new = "held"
    elif r["score"] >= security.FLAG_THRESHOLD:
        new = "flagged"
    else:
        new = "clear"
    links = {str(k): v for k, v in r["links"].items()}
    db.execute(
        "INSERT INTO user_risk (user_id, score, level, reasons, links, status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET score = excluded.score, level = excluded.level, reasons = excluded.reasons, "
        "links = excluded.links, status = excluded.status, updated_at = excluded.updated_at",
        (uid, r["score"], r["level"], json.dumps(r["reasons"]), json.dumps(links), new, time.time()))
    top = "; ".join(t for _, _, t in r["reasons"][:3])
    if new == "held" and not u["held"]:
        db.execute("UPDATE users SET held = 1 WHERE id = ?", (uid,))
        log_audit("user.auto_hold", "user", uid, u["username"], f"Risk {r['score']}/100: {top}", system=True)
        log_event("risk_held", user_id=uid, username=u["username"], detail=f"risk {r['score']}/100: {top}")
    elif new != status:
        kind = {"flagged": "risk_reflagged" if status == "approved" else "risk_flagged", "clear": "risk_cleared"}.get(new)
        if kind:
            log_event(kind, user_id=uid, username=u["username"], detail=f"risk {r['score']}/100: {top}")
    if new != status:
        rescore_credit(db, uid, reason="risk status")  # a hold caps the score; clearing it lifts the cap
    return new


def track(db, uid, username, event, fp_raw="", extra=None):
    """Record a sign-up / log-in / email confirmation against the account and re-score it. If anything in the alt
    detection fails it's logged and ignored: it must never stop someone signing up or logging in."""
    try:
        record_sighting(db, uid, username, event, fp_raw, extra)
        return apply_risk(db, uid)
    except Exception:
        app.logger.exception("alt detection failed for %s", event)
        return None


def rescore(db, *uids):
    """Re-score accounts after something that can reveal an alt (a loan, an entry fee). A problem in the risk engine
    is logged and ignored: it must never get in the way of a trade or a loan."""
    try:
        for uid in {u for u in uids if u}:
            apply_risk(db, uid)
    except Exception:
        app.logger.exception("risk re-score failed")


def set_risk_status(db, uid, status):
    """An admin's decision on an account: 'approved' (clears any hold, remembers which signals they accepted) or
    'held'. Returns False if there's nothing to do."""
    row = db.execute("SELECT * FROM user_risk WHERE user_id = ?", (uid,)).fetchone()
    if status == "approved":
        kinds = sorted({k for p, k, _ in json.loads(row["reasons"]) if p > 0}) if row else []
        db.execute("INSERT INTO user_risk (user_id, status, ack, reviewed_by, reviewed_at, updated_at) "
                   "VALUES (?, 'approved', ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET status = 'approved', "
                   "ack = excluded.ack, reviewed_by = excluded.reviewed_by, reviewed_at = excluded.reviewed_at",
                   (uid, json.dumps(kinds), g.user["username"], time.time(), time.time()))
        db.execute("UPDATE users SET held = 0 WHERE id = ?", (uid,))
    else:
        db.execute("INSERT INTO user_risk (user_id, status, reviewed_by, reviewed_at, updated_at) "
                   "VALUES (?, 'held', ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET status = 'held', "
                   "reviewed_by = excluded.reviewed_by, reviewed_at = excluded.reviewed_at",
                   (uid, g.user["username"], time.time(), time.time()))
        db.execute("UPDATE users SET held = 1 WHERE id = ?", (uid,))
    rescore_credit(db, uid, reason="review decision")
    return True


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Log in to do that.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def owner_email_ok(email, verified):
    """With MAMS_OWNER_EMAIL set, the owner's account must have confirmed that address (any alias of it)."""
    if not OWNER_EMAIL:
        return True
    return bool(verified and email) and security.normalize_email(email)[1] == security.normalize_email(OWNER_EMAIL)[1]


def owner_id(db):
    if OWNER_USERNAME:
        row = db.execute("SELECT id, email, email_verified FROM users WHERE username = ?", (OWNER_USERNAME,)).fetchone()
        if row is not None and not owner_email_ok(row["email"], row["email_verified"]):
            return None
    else:
        row = db.execute("SELECT MIN(id) AS id FROM users").fetchone()
    return row["id"] if row else None


def is_owner():
    return g.get("user") is not None and g.user["id"] == g.get("owner_id")


def can_manage(target_id):
    """Owner can manage anyone. Other admins can only manage regular (non-admin) users."""
    if is_owner():
        return True
    if target_id == g.owner_id:
        return False
    target = get_db().execute("SELECT is_admin FROM users WHERE id = ?", (target_id,)).fetchone()
    return target is not None and not target["is_admin"]


NOT_ALLOWED = "Only the site owner can do that to an admin."


def safe_path(url, default):
    """`url` if it's a path on this site, else `default`. Rejects //host and /\\host (browsers read a backslash as
    a slash), schemes and control characters, so `next` parameters can't send people to another site."""
    url = url or ""
    if (not url.startswith("/") or url.startswith("//") or "\\" in url
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in url)):
        return default
    parts = urlsplit(url)
    return url if not parts.scheme and not parts.netloc else default


def referrer_or(default):
    """The page the request came from, if it's on this site, else `default`."""
    parts = urlsplit(request.referrer or "")
    if parts.netloc and parts.netloc == request.host and parts.scheme in ("http", "https"):
        return safe_path(parts.path + (f"?{parts.query}" if parts.query else ""), default)
    return default


def back(default):
    """Redirect to the page a form came from (a same-site `next` path) or to `default`."""
    return redirect(safe_path(request.form.get("next", ""), default))

# Every admin action is written to audit_log (Admin -> Admin log). The key is stored; this map gives the label.
AUDIT_ACTIONS = {
    "market.create": "Created a market",
    "market.edit": "Edited a market",
    "market.cancel": "Cancelled a market (bets refunded)",
    "market.delete": "Deleted a market",
    "market.close_early": "Closed betting early",
    "market.reopen": "Reopened betting",
    "market.feature": "Featured a market",
    "market.unfeature": "Removed a market from featured",
    "market.clarify": "Added a rules clarification",
    "market.convert_ou": "Converted a market to Over/Under",
    "market.block": "Blocked a user from a market",
    "market.unblock": "Unblocked a user from a market",
    "market.cancel_trade": "Cancelled and refunded a share purchase",
    "result.propose": "Proposed a result",
    "result.retract": "Withdrew a proposed result",
    "result.confirm": "Confirmed a disputed result",
    "proposal.approve": "Approved a market proposal",
    "proposal.reject": "Rejected a market proposal",
    "comment.hide": "Hid a comment",
    "comment.unhide": "Unhid a comment",
    "comment.delete": "Deleted a comment",
    "user.ban": "Banned a user from trading",
    "user.unban": "Allowed a user to trade again",
    "user.comment_mute": "Muted a user from commenting",
    "user.comment_unmute": "Allowed a user to comment again",
    "user.grant": "Adjusted a balance",
    "user.admin_on": "Made a user an admin",
    "user.admin_off": "Removed a user's admin role",
    "user.password_reset": "Reset a password",
    "user.delete": "Deleted an account",
    "site.announcement": "Changed the site announcement",
    "user.auto_hold": "Automatically held a new account for review",
    "user.hold": "Held an account for review",
    "user.approve": "Approved an account",
    "user.bulk": "Bulk action on accounts",
    "security.rescan": "Re-scanned every account for alts",
    "security.lists": "Changed the email or network lists",
    "email.release": "Released a blocked email address",
    "loan.forgive": "Forgave a loan",
    "loan.extend": "Extended a loan's due date",
    "loan.cancel": "Cancelled a loan offer",
    "credit.adjust": "Adjusted a credit score",
    "credit.freeze": "Froze a user's borrowing",
    "credit.unfreeze": "Allowed a user to borrow again",
    "credit.limit": "Set or cleared a borrowing limit override",
    "credit.recompute": "Recomputed every credit score",
    "credit.config": "Changed the credit score model",
}


def log_audit(action, target_type, target_id, label="", detail="", system=False):
    """Record an admin action. Call it after the action succeeded (failed or forbidden attempts aren't logged).
    Names are copied in, with no foreign keys, so the log survives deleted accounts and markets. A logging
    failure never undoes the action. `system=True` records an automatic action (actor "system")."""
    assert action in AUDIT_ACTIONS, action
    try:
        actor_id, actor_name = (None, "system") if system or g.get("user") is None else (g.user["id"], g.user["username"])
        get_db().execute(
            "INSERT INTO audit_log (actor_id, actor_name, action, target_type, target_id, target_label, detail, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (actor_id, actor_name, action, target_type, str(target_id), str(label)[:200],
             str(detail)[:500], now_str()))
    except sqlite3.Error:
        app.logger.exception("could not write audit log entry %s", action)
    if not system:  # one timeline for everything: the Events page shows admin actions beside logins and failures
        by = g.user["username"] if g.get("user") is not None else "?"
        about_user = target_type == "user" and str(target_id).isdigit()
        log_event("admin_action", user_id=int(target_id) if about_user else None,
                  username=str(label)[:40] if about_user else None,
                  detail=f"{AUDIT_ACTIONS[action]} by {by}" + (f" · {label}" if label and not about_user else "")
                  + (f" · {detail}" if detail else ""))


def market_label(db, mid):
    row = db.execute("SELECT question FROM markets WHERE id = ?", (mid,)).fetchone()
    return row["question"] if row else f"Market #{mid}"


def throttled(view):
    """For expensive pages anyone can load: answer 429 instead of doing the work when one account (or, logged out,
    one IP) asks too often."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is not None:
            key, (limit, window) = ("heavy-user", g.user["id"]), HEAVY_PAGE_LIMITS["user"]
        else:
            key, (limit, window) = ("heavy-ip", request.remote_addr), HEAVY_PAGE_LIMITS["ip"]
        if rate_limited(key, limit, window):
            log_event("page_throttled", detail=f"{request.method} {request.path}")
            return "Too many requests. Wait a minute and try again.", 429, {"Retry-After": str(window)}
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None or not g.user["is_admin"]:
            log_event("admin_denied", detail=f"{request.method} {request.path}")
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    can_bonus = bool(g.user) and g.user["last_bonus"] != date.today().isoformat()
    pending = 0
    if g.user and g.user["is_admin"]:
        pending = get_db().execute("SELECT COUNT(*) FROM proposals WHERE status = 'pending'").fetchone()[0]
    loan_alerts = 0
    if g.user:  # offers made to you, loans of yours that are overdue, and loans you made that are in default
        grace_cutoff = (datetime.now() - timedelta(days=LOAN_GRACE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        loan_alerts = get_db().execute(
            "SELECT COUNT(*) FROM loans WHERE (status = 'offered' AND borrower_id = ?) "
            "OR (status = 'active' AND borrower_id = ? AND due_at <= ?) "
            "OR (status = 'active' AND lender_id = ? AND due_at <= ?)",
            (g.user["id"], g.user["id"], now_str(), g.user["id"], grace_cutoff)).fetchone()[0]
    return {"csrf_token": csrf_token, "user": g.user, "can_bonus": can_bonus, "pending_proposals": pending,
            "loan_alerts": loan_alerts,
            "SCHOOL_TZ": os.environ.get("TZ", "America/New_York"),
            "announcement": get_setting(get_db(), "announcement"),
            "celebrate": session.pop("celebrate", None),
            "owner_id": g.get("owner_id"), "is_owner": is_owner(),
            "CURRENCY": CURRENCY, "DAILY_BONUS": DAILY_BONUS, "CATEGORIES": CATEGORIES,
            "DISPUTE_HOURS": DISPUTE_HOURS, "CREATION_FEE": CREATION_FEE, "ENTRY_FEE": ENTRY_FEE,
            "CREATOR_MIN_BET": CREATOR_MIN_BET, "LATE_FEE_RATE": LATE_FEE_RATE,
            "LATE_SNIPE_RATE": LATE_SNIPE_RATE, "LOAN_LATE_FEE": LOAN_LATE_FEE}


@app.template_filter("ts")
def ts_filter(t):
    """Epoch seconds (the security tables) as the site's usual date text, which `when` and `ago` then format."""
    try:
        return datetime.fromtimestamp(float(t)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


@app.template_filter("slug")
def slug_filter(text):
    """'Very poor' -> 'very-poor', for CSS classes."""
    return str(text or "").lower().replace(" ", "-")


@app.template_filter("money")
def money_filter(x):
    return f"{x:,.2f}"


@app.template_filter("signed_money")
def signed_money_filter(x):
    """+Ⓜ12.00 / −Ⓜ3.50 (a real minus sign)."""
    return f"{'+' if x >= 0 else '−'}Ⓜ{abs(x):,.2f}"


@app.template_filter("cents_label")
def cents_label_filter(p):
    """A 0-1 price as cents: 0.42 -> 42¢."""
    return f"{round(p * 100):.0f}¢"


@app.template_filter("pct")
def pct_filter(p):
    v = p * 100
    if 0 < v < 1:
        return "<1%"
    if 99 < v < 100:
        return ">99%"
    return f"{v:.0f}%"


@app.template_filter("when")
def when_filter(s):
    dt = parse_dt(s)
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{dt.strftime('%b')} {dt.day}, {dt.year} {hour}:{dt.strftime('%M %p')}"


@app.template_filter("ago")
def ago_filter(s):
    secs = (datetime.now() - parse_dt(s)).total_seconds()
    if secs < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"


@app.template_filter("until")
def until_filter(s):
    secs = (parse_dt(s) - datetime.now()).total_seconds()
    if secs <= 0:
        return "closed"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} left"
    return "<1m left"


# ---------------------------------------------------------------- security headers

@app.template_global()
def csp_nonce():
    """A per-request random value. Inline <script> tags carry it, and the CSP only runs scripts that do."""
    if "csp_nonce" not in g:
        g.csp_nonce = secrets.token_urlsafe(16)
    return g.csp_nonce


# Google Fonts is the only outside origin the pages use. Styles allow 'unsafe-inline' because templates and
# app.js use style="" attributes; scripts don't.
# Cloudflare Turnstile (the "verify you're human" box on sign-up) loads its script and frame from this origin.
TURNSTILE_ORIGIN = "https://challenges.cloudflare.com"
CSP = ("default-src 'self'; script-src 'self' 'nonce-{nonce}' " + TURNSTILE_ORIGIN + "; frame-src " + TURNSTILE_ORIGIN + "; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
       "img-src 'self' data:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; object-src 'none'; "
       "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


@app.after_request
def security_headers(resp):
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("X-Frame-Options", "DENY")  # no framing, so nobody can trick clicks onto Buy buttons
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if PRODUCTION:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    if resp.mimetype == "text/html":
        h["Content-Security-Policy"] = CSP.format(nonce=csp_nonce())
        # personal pages (balances, admin screens, one-time passwords) aren't kept in the browser's cache
        if g.get("user") is not None and "Cache-Control" not in h:
            h["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- installable app (PWA)

def _asset_version():
    """A short hash of everything in static/. Static URLs carry it (?v=...), so a deploy gives every file a new URL
    and the service worker, which caches static files forever, picks up the new copies."""
    digest = hashlib.sha256()
    static_dir = os.path.join(BASE_DIR, "static")
    for root, _dirs, files in sorted(os.walk(static_dir)):
        for name in sorted(files):
            with open(os.path.join(root, name), "rb") as f:
                digest.update(name.encode() + f.read())
    return digest.hexdigest()[:10]


ASSET_VERSION = _asset_version()
# cached by the service worker when it installs, so the offline page has its styles and icon
PRECACHE_ASSETS = ["style.css", "app.js", "icons/icon-192.png", "icons/favicon-32.png"]


@app.template_global()
def asset(filename):
    return url_for("static", filename=filename, v=ASSET_VERSION)


@app.route("/manifest.webmanifest")
def manifest():
    icon = lambda f, size, purpose="any": {"src": asset(f"icons/{f}"), "sizes": size, "type": "image/png",
                                           "purpose": purpose}
    data = {
        "name": "MAMS Prediction Market",
        "short_name": "MAMS Markets",
        "description": f"Trade {CURRENCY} on what happens next at Mass Academy.",
        "id": "/",
        "start_url": "/?source=app",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#f4f5f6",
        "theme_color": "#ffffff",
        "categories": ["education", "games"],
        "icons": [icon("icon-192.png", "192x192"), icon("icon-512.png", "512x512"),
                  icon("maskable-512.png", "512x512", "maskable")],
        "shortcuts": [
            {"name": "Markets", "url": "/", "icons": [icon("icon-192.png", "192x192")]},
            {"name": "Portfolio", "url": "/portfolio", "icons": [icon("icon-192.png", "192x192")]},
            {"name": "Leaderboard", "url": "/leaderboard", "icons": [icon("icon-192.png", "192x192")]},
        ],
    }
    resp = make_response(json.dumps(data))
    resp.headers["Content-Type"] = "application/manifest+json"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/sw.js")
def service_worker():
    """Served from the site root so it can control every page. Its text includes ASSET_VERSION, so each deploy is a
    new service worker that installs itself and clears the old caches."""
    resp = make_response(render_template("sw.js", version=ASSET_VERSION, offline_url=url_for("offline"),
                                         precache=[asset(f) for f in PRECACHE_ASSETS]))
    resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/offline")
def offline():
    """Shown by the service worker when a page can't load. It's cached, so it must not contain anything personal
    (no username, balance or form tokens): it doesn't extend base.html for that reason."""
    return render_template("offline.html")


# ---------------------------------------------------------------- routes: accounts

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        if not USERNAME_RE.match(username):
            flash("Username must be 3–20 characters: letters, numbers, _ . -", "error")
        elif not EMAIL_RE.match(email) or security.normalize_email(email)[1] is None:
            flash("Enter a valid email address.", "error")
        elif problem := email_problem(get_db(), email, username=username):
            log_event("signup_refused_email", username=username, detail=f"{security.normalize_email(email)[1]}: {problem[:80]}")
            flash(problem, "error")
        elif not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
            flash(f"Password must be {PASSWORD_MIN}–{PASSWORD_MAX} characters.", "error")
        elif password != request.form.get("confirm", ""):
            flash("Passwords don't match.", "error")
        elif rate_limited(("register", request.remote_addr), 40, 3600) \
                or rate_limited(("register-device", g.device_id), 8, 3600):
            log_event("signup_rate_limited", username=username, detail="per IP or per device limit")
            flash("Too many new accounts from this network. Try again in a while.", "error")
        elif (captcha := turnstile_check(request.form.get("cf-turnstile-response", ""), request.remote_addr)) \
                in ("missing", "failed", "unreachable"):
            if captcha == "unreachable":
                log_event("captcha_unreachable", username=username,
                          detail="Cloudflare Turnstile couldn't be reached; sign-up rejected")
                flash("We couldn't run the “verify you're human” check just now. Try again in a minute.", "error")
            else:
                log_event("signup_captcha_failed", username=username, detail=captcha)
                flash("Please complete the “verify you're human” check and try again.", "error")
        else:
            db = get_db()
            first_user = db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            # with MAMS_OWNER_EMAIL set, the owner's name only becomes admin once that email is confirmed
            is_admin = int((first_user and not OWNER_USERNAME)
                           or (username.lower() == OWNER_USERNAME.lower() and not OWNER_EMAIL)
                           or username.lower() in ADMIN_USERNAMES)
            try:
                db.execute("BEGIN IMMEDIATE")
                cur = db.execute(
                    "INSERT INTO users (username, password_hash, balance, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                    (username, generate_password_hash(password), STARTING_BALANCE, is_admin, now_str()))
                log_activity(db, cur.lastrowid, "signup", STARTING_BALANCE, None, "Starting balance")
                db.execute("COMMIT")
            except sqlite3.IntegrityError:
                db.execute("ROLLBACK")
                flash("That username is taken.", "error")
            else:
                session.clear()
                session.permanent = True
                session["uid"] = cur.lastrowid
                session["gen"] = 0
                email = security.normalize_email(email)[0]  # tidied (lowercase domain); the code goes here
                flash(f"Welcome! You start with {STARTING_BALANCE:,.0f} {CURRENCY}."
                      + (" You're an admin, so you can create and resolve markets." if is_admin else "")
                      + " Confirm your email to start betting.", "success")
                wait = code_wait(cur.lastrowid, email)
                if wait:
                    # this address was just sent a code by another sign-up; still hold it for this account
                    db.execute("INSERT OR REPLACE INTO login_challenges (username, code_hash, expires_at, requested_at, "
                               "attempts, email) VALUES (?, '', 0, ?, 0, ?)", (username, time.time(), email))
                    flash(f"A code was just sent to {email}. Wait {wait} seconds, then ask for one below.", "error")
                else:
                    send_email_code(db, cur.lastrowid, username, email)
                log_event("signup_ok", user_id=cur.lastrowid, username=username,
                          detail=f"email {security.normalize_email(email)[1]}, human check: {captcha}")
                track(db, cur.lastrowid, username, "signup", request.form.get("fp", ""), extra={"captcha": captcha})
                return redirect(url_for("account_email"))
    return render_template("register.html", turnstile_key=TURNSTILE_SITE_KEY if turnstile_enabled() else "")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if rate_limited(("login-ip", request.remote_addr), 120, 60):
            log_event("login_rate_limited", detail="over 120 attempts a minute from this IP")
            flash("Too many log-in attempts from this network. Wait a minute and try again.", "error")
            return render_template("login.html")
        name = request.form.get("username", "").strip()
        fail_key = ("login-fail", name.lower())
        with _hits_lock:
            recent_fails = [t for t in _hits.get(fail_key, []) if time.time() - t < 300]
        if len(recent_fails) >= 10:
            log_event("login_locked", username=name if USERNAME_RE.match(name) else "", detail="10 wrong passwords in 5 minutes")
            flash("Too many wrong passwords for this account. Wait 5 minutes and try again.", "error")
            return render_template("login.html")
        u = get_db().execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
        # Unknown usernames still pay for a hash check, so response time doesn't reveal which accounts exist.
        ok = check_password_hash(u["password_hash"] if u else DUMMY_PASSWORD_HASH, request.form.get("password", ""))
        if u and ok:
            with _hits_lock:
                _hits.pop(fail_key, None)
            session.clear()
            session.permanent = True
            session["uid"] = u["id"]
            session["gen"] = u["session_gen"]
            db = get_db()
            log_event("login_ok", user=u)
            track(db, u["id"], u["username"], "login", request.form.get("fp", ""))
            return redirect(safe_path(request.args.get("next", ""), url_for("index")))
        rate_limited(fail_key, 10, 300)  # record the failure
        # An unknown name is only logged if it looks like a username (people sometimes paste a password into that box).
        log_event("login_failed" if u else "login_unknown_user", user=u,
                  username=None if u else (name if USERNAME_RE.match(name) else "(not a username)"),
                  detail="wrong password" if u else "no such account")
        if u:  # logged for admins, but never counted as a sighting: anyone can type someone else's username
            get_db().execute("INSERT INTO login_events (user_id, username, event, ip, ua, device_id, created_at) "
                             "VALUES (?, ?, 'login_fail', ?, ?, ?, ?)",
                             (u["id"], u["username"], (request.remote_addr or "")[:64],
                              request.headers.get("User-Agent", "")[:400], g.device_id, time.time()))
        flash("Wrong username or password.", "error")
    return render_template("login.html")


DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_hex(16))
PASSWORD_MIN, PASSWORD_MAX = 6, 128


# Cloudflare Turnstile on the sign-up form, to stop bots making accounts. Off unless both keys are set (get them
# free at dash.cloudflare.com -> Turnstile -> Add widget, with your site's domain).
TURNSTILE_SITE_KEY = os.environ.get("MAMS_TURNSTILE_SITE_KEY", "").strip()
TURNSTILE_SECRET = os.environ.get("MAMS_TURNSTILE_SECRET", "").strip()
TURNSTILE_VERIFY_URL = TURNSTILE_ORIGIN + "/turnstile/v0/siteverify"


def turnstile_enabled():
    return bool(TURNSTILE_SITE_KEY and TURNSTILE_SECRET)


def turnstile_check(token, remote_ip):
    """Ask Cloudflare whether this sign-up's Turnstile token is genuine (each token works once). Returns
    'off' (not configured), 'missing' (no token sent), 'failed' (Cloudflare says no), 'ok', or 'unreachable'.
    Sign-up only goes ahead on 'off' or 'ok': when Cloudflare can't be reached the sign-up is rejected (and logged),
    so bots can't slip through during an outage."""
    if not turnstile_enabled():
        return "off"
    if not token:
        return "missing"
    data = urlencode({"secret": TURNSTILE_SECRET, "response": token, "remoteip": remote_ip or ""}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TURNSTILE_VERIFY_URL, data=data), timeout=10) as resp:
            body = json.loads(resp.read().decode())
        if not body.get("success"):
            current_app.logger.info("turnstile rejected a token: %s", ",".join(body.get("error-codes", [])) or "no reason")
        return "ok" if body.get("success") else "failed"
    except (OSError, ValueError, urllib.error.URLError):
        current_app.logger.exception("Couldn't reach Cloudflare Turnstile; rejecting this sign-up.")
        return "unreachable"


def turnstile_passed(token, remote_ip):
    """True only if the check passed or Turnstile is off."""
    return turnstile_check(token, remote_ip) in ("off", "ok")


EMAIL_TAKEN_MSG = "That email is already used by another account. If it's yours, log in to that account instead."


EMAIL_BLOCKED_MSG = ("That kind of email address isn't accepted (temporary or disposable inboxes aren't allowed). "
                     "Use your regular email address.")
EMAIL_USED_MSG = "That email has been used before and can't be used again. Ask an admin if you think that's a mistake."


def domain_lists(db):
    """The admin-edited extra blocked and shady email domains (merged with the bundled lists in security_data/)."""
    return (security.parse_domain_list(get_setting(db, "email_blocked")),
            security.parse_domain_list(get_setting(db, "email_shady")))


def email_problem(db, email, except_id=None, username=None):
    """Why this email can't be used by this account, or None if it can. The one place every rule about emails lives
    (sign-up, asking for a code, and confirming it all call it). The address is normalized first, so dots, +tags and
    alias domains (googlemail.com) can't be used to make a second account from one mailbox. It's refused if:
      - it's invalid, or from a disposable-inbox service;
      - another account confirmed it (or the same mailbox);
      - another sign-up asked for a code to it in the last EMAIL_CLAIM_HOURS and hasn't confirmed yet;
      - it was used by an account that has since been deleted (until an admin releases it), or by someone else."""
    display, canon = security.normalize_email(email)
    if not canon:
        return "Enter a valid email address."
    blocked, shady = domain_lists(db)
    if security.email_provider_class(canon, blocked, shady)[0] == "blocked":
        return EMAIL_BLOCKED_MSG
    except_id = -1 if except_id is None else except_id
    if db.execute("SELECT 1 FROM users WHERE id != ? AND email_verified = 1 "
                  "AND (email_canonical = ? OR email = ? COLLATE NOCASE) LIMIT 1", (except_id, canon, email)).fetchone():
        return EMAIL_TAKEN_MSG
    for r in db.execute("SELECT email FROM users WHERE id != ? AND email_verified = 1 AND email_canonical IS NULL "
                        "AND email IS NOT NULL", (except_id,)):  # confirmed before canonical emails were stored
        if security.normalize_email(r["email"])[1] == canon:
            return EMAIL_TAKEN_MSG
    since = time.time() - EMAIL_CLAIM_HOURS * 3600
    for c in db.execute("SELECT c.email FROM login_challenges c JOIN users u ON u.username = c.username "
                        "WHERE u.id != ? AND c.requested_at >= ? AND c.email IS NOT NULL", (except_id, since)):
        if security.normalize_email(c["email"])[1] == canon:
            return EMAIL_TAKEN_MSG
    row = db.execute("SELECT status, first_username FROM email_ledger WHERE canonical = ?", (canon,)).fetchone()
    if row and row["status"] == "deleted":
        return EMAIL_USED_MSG
    if row and row["status"] == "active" and (username or "").casefold() != row["first_username"].casefold():
        return EMAIL_TAKEN_MSG
    return None


def claim_email(db, canon, username):
    """Record that `username` now owns this (canonical) email. Call inside the transaction that confirms it."""
    db.execute(
        "INSERT INTO email_ledger (canonical, first_username, status, created_at) VALUES (?, ?, 'active', ?) "
        "ON CONFLICT(canonical) DO UPDATE SET status = 'active', first_username = "
        "CASE WHEN email_ledger.status = 'released' THEN excluded.first_username ELSE email_ledger.first_username END",
        (canon, username, time.time()))


def code_wait(user_id, email):
    """Seconds until this account may get another code, and until this address may be sent one (whichever is
    later). 0 means go ahead, and starts the next wait."""
    keys = [("email-code-minute", user_id), ("email-code-to", security.normalize_email(email)[1] or email.casefold())]
    now = time.time()
    with _hits_lock:
        last = max((t for k in keys for t in _hits.get(k, [])), default=0.0)
        left = LOGIN_CODE_RESEND_DELAY - (now - last)
        if left > 0:
            return math.ceil(left)
        for k in keys:
            _hits[k] = [now]
    return 0


def send_email_code(db, user_id, username, email):
    """Email a 6-digit code to confirm `email` for this account. Returns True if it was sent."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    started = time.time()
    try:
        how = send_login_code_email(email, code)
    except EmailError as e:
        log_event("email_send_failed", user_id=user_id, username=username, detail=f"to {email}: {e.reason}")
        flash("We couldn't send that email. Check the address and try again in a minute." if "recipient" in e.reason
              else "We couldn't send an email just now. Try again in a minute.", "error")
        return False
    except (OSError, RuntimeError, smtplib.SMTPException, ValueError) as e:
        current_app.logger.exception("Unable to send an email confirmation code.")
        log_event("email_send_failed", user_id=user_id, username=username, detail=f"to {email}: {type(e).__name__}")
        flash("We couldn't send an email just now. Check the address and try again in a minute.", "error")
        return False
    db.execute("INSERT OR REPLACE INTO login_challenges (username, code_hash, expires_at, requested_at, attempts, email) "
               "VALUES (?, ?, ?, ?, 0, ?)",
               (username, login_code_hash(username, code), time.time() + LOGIN_CODE_LIFETIME, time.time(), email))
    log_event("email_code_sent", user_id=user_id, username=username,
              detail=f"to {email} ({how or 'sent'}, {int((time.time() - started) * 1000)} ms total)")
    flash(f"We sent a 6-digit code to {email}. It expires in 10 minutes.", "success")
    return True


@app.route("/profile", methods=["GET", "POST"])
@app.route("/account/email", methods=["GET", "POST"])
@login_required
def account_email():
    """Profile settings (and email confirmation gate for accounts without a confirmed email)."""
    db = get_db()
    u = g.user
    challenge = db.execute("SELECT * FROM login_challenges WHERE username = ?", (u["username"],)).fetchone()
    if request.method == "POST":
        action = request.form.get("action", "")
        if action in ("send", "resend"):
            email = request.form.get("email", "").strip() if action == "send" else (challenge["email"] if challenge else "")
            if not EMAIL_RE.match(email or "") or security.normalize_email(email)[1] is None:
                flash("Enter a valid email address.", "error")
            elif problem := email_problem(db, email, except_id=u["id"], username=u["username"]):
                log_event("email_refused", detail=f"{security.normalize_email(email)[1]}: {problem[:80]}")
                flash(problem, "error")
            elif wait := code_wait(u["id"], email):
                log_event("email_code_wait", detail=f"asked again with {wait}s to go")
                flash(f"We just sent a code. Wait {wait} seconds before asking for another.", "error")
            elif rate_limited(("email-code", u["id"]), 5, 3600) or rate_limited(("email-code-ip", request.remote_addr), 30, 3600):
                log_event("email_code_rate_limited", detail="5 an hour per account or 30 an hour per IP")
                flash("Too many codes requested. Try again later.", "error")
            else:
                send_email_code(db, u["id"], u["username"], security.normalize_email(email)[0])
            return redirect(url_for("account_email"))
        if action == "change":
            db.execute("DELETE FROM login_challenges WHERE username = ?", (u["username"],))
            return redirect(url_for("account_email"))
        if action == "verify":
            code = request.form.get("code", "").strip()
            if not challenge or not challenge["email"] or challenge["expires_at"] < time.time() \
                    or challenge["attempts"] >= LOGIN_CODE_MAX_ATTEMPTS:
                log_event("email_code_expired", detail="no code pending, expired, or too many wrong tries")
                db.execute("DELETE FROM login_challenges WHERE username = ?", (u["username"],))
                flash("That code expired. Enter your email to get a new one.", "error")
            elif not hmac.compare_digest(challenge["code_hash"], login_code_hash(u["username"], code)):
                db.execute("UPDATE login_challenges SET attempts = attempts + 1 WHERE username = ?", (u["username"],))
                log_event("email_code_wrong", detail=f"wrong try {challenge['attempts'] + 1} of {LOGIN_CODE_MAX_ATTEMPTS}")
                flash("That code isn't right. Check the email and try again.", "error")
            else:
                # Re-check and claim the address in one transaction, so two people can't both confirm one mailbox.
                db.execute("BEGIN IMMEDIATE")
                try:
                    problem = email_problem(db, challenge["email"], except_id=u["id"], username=u["username"])
                    if problem:
                        db.execute("DELETE FROM login_challenges WHERE username = ?", (u["username"],))
                        db.execute("COMMIT")
                        log_event("email_refused", detail=f"at confirmation: {problem[:80]}")
                        flash(problem, "error")
                        return redirect(url_for("account_email"))
                    canon = security.normalize_email(challenge["email"])[1]
                    db.execute("UPDATE users SET email = ?, email_canonical = ?, email_verified = 1 WHERE id = ?",
                               (challenge["email"], canon, u["id"]))
                    claim_email(db, canon, u["username"])
                    db.execute("DELETE FROM login_challenges WHERE username = ?", (u["username"],))
                    db.execute("COMMIT")
                except Exception:
                    db.execute("ROLLBACK")
                    raise
                try:
                    write_email_csv(db)
                except OSError:
                    current_app.logger.exception("Couldn't update people.csv.")
                log_event("email_verified", detail=f"{security.normalize_email(challenge['email'])[1]}")
                track(db, u["id"], u["username"], "verify")
                flash("Email confirmed. You're all set!", "success")
                return redirect(url_for("index"))
            return redirect(url_for("account_email"))
    pending = challenge["email"] if challenge and challenge["email"] and challenge["expires_at"] >= time.time() else None
    return render_template("account_email.html", pending=pending,
                           current=u["email"] if u["email_verified"] else None)


@app.post("/account/username")
@login_required
def change_username():
    new_name = request.form.get("username", "").strip()
    old_name = g.user["username"]
    if rate_limited(("change-username", g.user["id"]), 10, 3600):
        flash("You're changing your name too often. Try again later.", "error")
    elif not USERNAME_RE.match(new_name):
        flash("Name must be 3–20 characters: letters, numbers, _, - or .", "error")
    elif new_name == old_name:
        flash("That's already your name.", "info")
    elif OWNER_USERNAME and (
        (old_name.lower() == OWNER_USERNAME.lower() and new_name.lower() != OWNER_USERNAME.lower())
        or (old_name.lower() != OWNER_USERNAME.lower() and new_name.lower() == OWNER_USERNAME.lower())
    ):
        flash("That username is reserved for the site owner.", "error")
    else:
        db = get_db()
        taken = db.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE AND id != ?",
            (new_name, g.user["id"]),
        ).fetchone()
        if taken:
            flash("That name is already taken.", "error")
        else:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("UPDATE users SET username = ? WHERE id = ?", (new_name, g.user["id"]))
                db.execute("UPDATE sightings SET username = ? WHERE user_id = ?", (new_name, g.user["id"]))
                db.execute("DELETE FROM login_challenges WHERE username = ? COLLATE NOCASE", (old_name,))
                db.execute("COMMIT")
            except sqlite3.IntegrityError:
                db.execute("ROLLBACK")
                flash("That name is already taken.", "error")
                return redirect(url_for("account_email"))
            except Exception:
                db.execute("ROLLBACK")
                raise
            try:
                write_email_csv(db)
            except OSError:
                current_app.logger.exception("Couldn't update people.csv.")
            log_event("username_changed", detail=f"{old_name} -> {new_name}")
            flash(f"Your name is now {new_name}.", "success")
    return redirect(url_for("account_email"))


def set_temporary_password(db, user_id):
    """Give a user a new random password, sign them out everywhere and clear their login lockout.
    Returns the password (shown once to whoever reset it)."""
    pw = secrets.token_urlsafe(9)  # 12 URL-safe characters
    name = db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()["username"]
    db.execute("UPDATE users SET password_hash = ?, session_gen = session_gen + 1 WHERE id = ?",
               (generate_password_hash(pw), user_id))
    with _hits_lock:
        _hits.pop(("login-fail", name.lower()), None)
    return pw


@app.post("/account/password")
@login_required
def change_password():
    cur, new = request.form.get("current", ""), request.form.get("new", "")
    if rate_limited(("change-password", request.remote_addr), 30, 60):
        log_event("password_change_failed", detail="rate limited")
        flash("Too many attempts. Wait a minute and try again.", "error")
    elif not cur:
        flash("Enter your current password.", "error")
    elif not check_password_hash(g.user["password_hash"], cur):
        log_event("password_change_failed", detail="current password was wrong")
        flash("Your current password is wrong.", "error")
    elif not PASSWORD_MIN <= len(new) <= PASSWORD_MAX:
        flash(f"Password must be {PASSWORD_MIN}–{PASSWORD_MAX} characters.", "error")
    elif new != request.form.get("confirm", ""):
        flash("Passwords don't match.", "error")
    else:
        db = get_db()
        db.execute("UPDATE users SET password_hash = ?, session_gen = session_gen + 1 WHERE id = ?",
                   (generate_password_hash(new), g.user["id"]))
        session["gen"] = g.user["session_gen"] + 1  # keep this device signed in; every other one is signed out
        log_event("password_changed", detail="other devices were signed out")
        flash("Password changed.", "success")
    nxt = (request.form.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("portfolio") + "#account")


@app.post("/logout")
def logout():
    if g.get("user") is not None:
        log_event("logout", user=g.user)
    session.clear()
    return redirect(url_for("index"))


@app.post("/bonus")
@login_required
def bonus():
    today = date.today().isoformat()
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    cur = db.execute(
        "UPDATE users SET balance = ROUND(balance + ?, 2), last_bonus = ? "
        "WHERE id = ? AND (last_bonus IS NULL OR last_bonus != ?)",
        (DAILY_BONUS, today, g.user["id"], today))
    if cur.rowcount:
        log_activity(db, g.user["id"], "bonus", DAILY_BONUS, None, "Daily bonus")
    db.execute("COMMIT")
    if cur.rowcount:
        flash(f"Claimed your daily {DAILY_BONUS:.0f} {CURRENCY}!", "success")
        session["celebrate"] = {"kind": "bonus", "from": g.user["balance"]}
    else:
        flash("You already claimed today's bonus. Come back tomorrow.", "error")
    return redirect(referrer_or(url_for("index")))


# ---------------------------------------------------------------- routes: markets

STATE_ORDER = ["open", "closed", "proposed", "disputed", "resolved", "cancelled"]
STATUS_TABS = [("open", "Open"), ("closed", "Awaiting result"), ("resolved", "Resolved"),
               ("cancelled", "Cancelled"), ("all", "All")]


def matches_status(state, status):
    if status == "closed":  # "Awaiting result" also covers results that are proposed or disputed
        return state in ("closed", "proposed", "disputed")
    return status == "all" or state == status


@app.route("/")
def index():
    """Every market is rendered (in one fixed order) and the status/topic filters just hide cards: the server
    marks the ones that don't match the query string hidden, and static/app.js flips them on click with no
    request. Order: grouped by state; open by featured then volume; awaiting by closing time; settled newest first."""
    db = get_db()
    status = request.args.get("status", "open")
    status = status if status in dict(STATUS_TABS) else "open"
    cat = request.args.get("cat", "")
    cat = cat if cat in CATEGORIES else ""
    markets = bundle_many(db, db.execute("SELECT * FROM markets ORDER BY closes_at").fetchall())

    def sort_key(b):
        rank = STATE_ORDER.index(b["state"])
        if b["state"] == "open":
            return (rank, -b["m"]["featured"], -b["volume"], b["m"]["closes_at"])
        if b["state"] in ("resolved", "cancelled"):  # newest first; ISO strings sort as dates
            return (rank, 0, 0, "".join(chr(0x10FFFF - ord(ch)) for ch in (b["m"]["resolved_at"] or "")))
        return (rank, 0, 0, b["m"]["closes_at"])
    markets.sort(key=sort_key)
    visible = {b["m"]["id"] for b in markets
               if matches_status(b["state"], status) and (not cat or b["m"]["category"] == cat)}
    live = [b for b in markets if b["state"] == "open"]
    trends = price_trends(db, [b["m"]["id"] for b in markets if b["state"] in ("open", "closed", "proposed",
                                                                               "disputed")])
    for b in markets:
        b["trend"] = trends.get(b["m"]["id"])
    # Movers: the open markets whose price moved most in the last day (a binary market by its first outcome,
    # a multiple-choice one by whichever outcome moved most).
    movers = []
    for b in live:
        if not b["trend"]:
            continue
        idx = 0 if b["binary"] else max(range(len(b["outcomes"])), key=lambda i: abs(b["trend"]["delta"][i])
                                        if i < len(b["trend"]["delta"]) else 0)
        delta = b["trend"]["delta"][idx] if idx < len(b["trend"]["delta"]) else 0
        if abs(delta) >= 0.005:
            movers.append({"b": b, "o": b["outcomes"][idx], "idx": idx, "delta": delta})
    movers.sort(key=lambda x: -abs(x["delta"]))
    spotlight = [b for b in live if b["m"]["featured"]][:5]
    return render_template("index.html", markets=markets, visible=visible, status=status, cat=cat,
                           tabs=STATUS_TABS, movers=movers[:8], spotlight=spotlight,
                           default_view=(status == "open" and not cat))


@app.route("/market/<int:mid>")
def market(mid):
    db = get_db()
    m = db.execute("SELECT * FROM markets WHERE id = ?", (mid,)).fetchone()
    if m is None:
        abort(404)
    b = bundle(db, m)
    trend = price_trends(db, [mid]).get(mid) if b["state"] not in ("resolved", "cancelled") else None
    history = [{"t": r["created_at"], "p": json.loads(r["prices"])}
               for r in db.execute("SELECT * FROM price_history WHERE market_id = ? ORDER BY id", (mid,))]
    trades = db.execute(
        "SELECT t.*, u.username, u.is_admin, o.label FROM trades t JOIN users u ON u.id = t.user_id "
        "JOIN outcomes o ON o.id = t.outcome_id WHERE t.market_id = ? ORDER BY t.id DESC LIMIT 25", (mid,)).fetchall()
    clarifications = db.execute("SELECT * FROM clarifications WHERE market_id = ? ORDER BY id", (mid,)).fetchall()
    disputes, proposer, can_dispute = [], None, False
    if m["proposed_outcome_id"] is not None and m["status"] == "open":
        disputes = open_disputes(db, mid)
        row = db.execute("SELECT username FROM users WHERE id = ?", (m["proposed_by"],)).fetchone()
        proposer = row["username"] if row else None
        if g.user:
            traded = db.execute("SELECT 1 FROM trades WHERE user_id = ? AND market_id = ? LIMIT 1",
                                (g.user["id"], mid)).fetchone()
            can_dispute = bool(traded) and not any(d["user_id"] == g.user["id"] for d in disputes)
    is_admin = bool(g.user and g.user["is_admin"])
    comments = []
    for c in db.execute("SELECT c.*, u.username FROM comments c JOIN users u ON u.id = c.user_id "
                        "WHERE c.market_id = ? ORDER BY c.id DESC", (mid,)).fetchall():
        c = dict(c)
        if c["hidden"] and not is_admin:
            c["body"] = None  # a hidden comment's text is never sent to non-admins
        c["can_moderate"] = is_admin and (c["user_id"] == g.user["id"] or can_manage(c["user_id"]))
        comments.append(c)
    commenting_off = bool(g.user and db.execute("SELECT 1 FROM comment_mutes WHERE user_id = ?",
                                                (g.user["id"],)).fetchone())
    my_positions = []
    if g.user:
        by_id = {o["id"]: o for o in b["outcomes"]}
        q = [o["shares"] for o in b["outcomes"]]
        index = {o["id"]: i for i, o in enumerate(b["outcomes"])}
        for p in db.execute(
                "SELECT p.* FROM positions p JOIN outcomes o ON o.id = p.outcome_id "
                "WHERE p.user_id = ? AND o.market_id = ? ORDER BY o.position", (g.user["id"], mid)):
            o = by_id[p["outcome_id"]]
            # Resolved markets pay 1 per winning share; otherwise show what the shares would sell for now.
            value = (p["shares"] * o["price"] if b["m"]["status"] == "resolved"
                     else 0.0 if b["m"]["status"] == "cancelled"
                     else lmsr.sell_value(q, b["m"]["b"], index[p["outcome_id"]], p["shares"]))
            my_positions.append({"outcome": o, "shares": p["shares"], "cost": p["cost"], "value": value,
                                 "bonus": 0.0,
                                 "est_bonus": estimated_bonus(db, mid, p["outcome_id"], g.user["id"])
                                 if b["m"]["status"] == "open" else 0.0})
        if b["m"]["status"] == "resolved":  # the leftover-pot bonus belongs with the winning position
            bonus = leftover_bonus(db, g.user["id"], mid)
            for p in my_positions:
                if p["outcome"]["id"] == b["m"]["resolved_outcome_id"]:
                    p["bonus"] = bonus
                    p["value"] += bonus
    my_limit_orders = []
    if g.user:
        by_id = {o["id"]: o for o in b["outcomes"]}
        for lo in db.execute(
            "SELECT * FROM limit_orders WHERE user_id = ? AND market_id = ? AND status = 'open' ORDER BY id DESC",
            (g.user["id"], mid),
        ).fetchall():
            o = by_id.get(lo["outcome_id"])
            if o:
                my_limit_orders.append({**dict(lo), "label": o["label"], "current_price": o["price"]})
    block_reason = trade_block_reason(db, g.user["id"], mid) if g.user else None
    blocked, all_users = [], []
    if g.user and g.user["is_admin"]:
        blocked = db.execute("SELECT u.id, u.username FROM market_blocks mb JOIN users u ON u.id = mb.user_id "
                             "WHERE mb.market_id = ? ORDER BY u.username", (mid,)).fetchall()
        all_users = [r["username"] for r in db.execute("SELECT username FROM users ORDER BY username")]
    creator = creator_name(db, m)
    my_entry_fee = entry_fee_due(db, m, g.user["id"]) if g.user else (ENTRY_FEE if creator else 0.0)
    pick = request.args.get("pick", type=int)
    return render_template("market.html", b=b, history=history, trades=trades, comments=comments,
                           trend=trend, pick=pick if pick in {o["id"] for o in b["outcomes"]} else None,
                           my_positions=my_positions, my_limit_orders=my_limit_orders, block_reason=block_reason,
                           creator=creator, my_entry_fee=my_entry_fee,
                           late_t=late_multiplier(m) if b["state"] == "open" else 0.0,
                           blocked=blocked, all_users=all_users, clarifications=clarifications,
                           disputes=disputes, proposer=proposer, can_dispute=can_dispute,
                           commenting_off=commenting_off, comments_off_text=COMMENTS_OFF,
                           reopen_default=(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"))


@app.post("/market/<int:mid>/dispute")
@login_required
def dispute(mid):
    if rate_limited(("dispute", g.user["id"]), 10, 3600):
        flash("Too many disputes. Try again later.", "error")
        return redirect(url_for("market", mid=mid))
    try:
        add_dispute(get_db(), g.user["id"], mid, request.form.get("reason", "").strip())
        flash("Dispute sent. The result won't finalize until an admin reviews it.", "success")
    except TradeError as e:
        flash(str(e), "error")
    return redirect(url_for("market", mid=mid))


def late_fee_table():
    """LATE_FEE_TIERS as rows for the How it works page: [(time left, T), ...], longest time first."""
    def say(d, unit):
        mins = int(d.total_seconds() // 60)
        return f"{mins // 60:g}" if unit == "hours" else f"{mins}"

    def unit_for(d):
        return "hours" if d.total_seconds() >= 3600 and d.total_seconds() % 3600 == 0 else "minutes"

    def one(d):
        u = unit_for(d)
        n = say(d, u)
        return f"{n} {u[:-1] if n == '1' else u}"

    tiers = LATE_FEE_TIERS
    rows = [(f"More than {one(tiers[-1][0])}", 0.0)]
    for (lo, _), (hi, t) in reversed(list(zip(tiers, tiers[1:]))):
        u = unit_for(hi) if unit_for(lo) == unit_for(hi) else "minutes"
        rows.append((f"{say(lo, u)}–{say(hi, u)} {u}", t))
    rows.append((f"Less than {one(tiers[0][0])}", tiers[0][1]))
    return rows


@app.route("/how-it-works")
def how_it_works():
    top = LATE_FEE_TIERS[0][1]
    return render_template("how_it_works.html", window_hours=DISPUTE_HOURS, late_rows=late_fee_table(),
                           LOAN_MAX_ACTIVE=LOAN_MAX_ACTIVE, LOAN_GRACE_DAYS=LOAN_GRACE_DAYS,
                           late_top=top, late_example=cents(LATE_FEE_RATE * 100 * 0.25 * top),
                           late_first_limit=late_fee_table()[-1][0].replace("Less than ", ""),
                           snipe_example=cents(LATE_SNIPE_RATE * 100 * 0.95 * 0.95 * top),
                           snipe_old=cents(LATE_FEE_RATE * 100 * 0.95 * 0.05 * top))


@app.post("/market/<int:mid>/trade")
@login_required
def trade(mid):
    try:
        outcome_id = int(request.form["outcome_id"])
        value = float(request.form.get("value", ""))
        if not math.isfinite(value):
            raise ValueError
    except (KeyError, ValueError):
        flash("Pick an outcome and enter a valid number.", "error")
        return redirect(url_for("market", mid=mid))
    try:
        min_out = request.form.get("min_out", "").strip()
        min_out = float(min_out) if min_out else None
        if min_out is not None and (not math.isfinite(min_out) or min_out < 0):
            min_out = None
    except ValueError:
        min_out = None
    if any(rate_limited(("trade", g.user["id"], window), limit, window) for limit, window in TRADE_LIMITS):
        log_event("trade_throttled", detail=f"market {mid}")
        flash("You're trading too fast. Wait a minute and try again.", "error")
        return redirect(url_for("market", mid=mid))
    try:
        db = get_db()
        action = request.form.get("action")
        order_type = (request.form.get("order_type") or "market").strip().lower()
        mkt = db.execute("SELECT * FROM markets WHERE id = ?", (mid,)).fetchone()
        fee_due = bool(mkt) and entry_fee_due(db, mkt, g.user["id"]) > 0
        if order_type == "limit":
            try:
                limit_pct = float(request.form.get("limit_pct", ""))
                if not math.isfinite(limit_pct):
                    raise ValueError
            except ValueError:
                raise TradeError("Enter a valid limit percentage between 1% and 99%.")
            flash(place_limit_order(db, g.user["id"], mid, outcome_id, action, value, limit_pct / 100.0), "success")
            if fee_due and entry_fee_due(db, mkt, g.user["id"]) == 0:
                rescore(db, g.user["id"], mkt["created_by"])
        else:
            flash(execute_trade(db, g.user["id"], mid, outcome_id, action, value, min_out), "success")
            if fee_due:  # paid an entry fee to the market's creator: money moving between linked accounts is a signal
                rescore(db, g.user["id"], mkt["created_by"])
            # the next page plays a short animation: confetti in the outcome's colour, the balance ticking over
            m = db.execute("SELECT ou_line FROM markets WHERE id = ?", (mid,)).fetchone()
            labels = [o["label"] for o in get_outcomes(db, mid)]
            ids = [o["id"] for o in get_outcomes(db, mid)]
            idx = ids.index(outcome_id) if outcome_id in ids else 0
            binary = is_binary(labels) or (m is not None and m["ou_line"] is not None)
            session["celebrate"] = {"kind": "buy" if action == "buy" else "sell", "from": g.user["balance"],
                                    "color": ("--yes" if idx == 0 else "--no") if binary else f"--c{idx}"}
    except TradeError as e:
        flash(str(e), "error")
    return redirect(url_for("market", mid=mid))


COMMENTS_OFF = "An admin has turned off commenting for your account."


@app.post("/market/<int:mid>/comment")
@login_required
def comment(mid):
    body = request.form.get("body", "").strip()
    if not body:
        flash("Comment can't be empty.", "error")
    elif len(body) > 500:
        flash("Keep comments under 500 characters.", "error")
    elif get_db().execute("SELECT 1 FROM comment_mutes WHERE user_id = ?", (g.user["id"],)).fetchone():
        flash(COMMENTS_OFF, "error")
    elif rate_limited(("comment", g.user["id"]), 10, 60):
        flash("You're commenting too fast. Wait a minute and try again.", "error")
    elif get_db().execute("SELECT 1 FROM markets WHERE id = ?", (mid,)).fetchone():
        get_db().execute("INSERT INTO comments (market_id, user_id, body, created_at) VALUES (?, ?, ?, ?)",
                         (mid, g.user["id"], body, now_str()))
    return redirect(url_for("market", mid=mid) + "#discussion")


@app.get("/api/quote")
def quote():
    """Preview a trade without making it."""
    db = get_db()
    try:
        mid = int(request.args["market"])
        oid = int(request.args["outcome"])
        value = float(request.args["value"])
        action = request.args["action"]
        if not math.isfinite(value) or value <= 0:
            raise ValueError
    except (KeyError, ValueError):
        return jsonify(ok=False)
    m = db.execute("SELECT * FROM markets WHERE id = ?", (mid,)).fetchone()
    if m is None or market_state(m) != "open":
        return jsonify(ok=False, error="This market is no longer accepting bets.")
    outs = get_outcomes(db, mid)
    ids = [o["id"] for o in outs]
    if oid not in ids:
        return jsonify(ok=False, error="Unknown outcome.")
    i = ids.index(oid)
    balance = held = None  # logged-out visitors still get a price preview
    try:
        if g.user:
            block = trade_block_reason(db, g.user["id"], mid) or (overdue_block(db, g.user["id"]) if action == "buy" else None)
            if block:
                raise TradeError(block)
            pos = db.execute("SELECT shares FROM positions WHERE user_id = ? AND outcome_id = ?",
                             (g.user["id"], oid)).fetchone()
            balance, held = g.user["balance"], pos["shares"] if pos else 0.0
            fee = entry_fee_due(db, m, g.user["id"]) if action == "buy" else 0.0
        else:
            fee = ENTRY_FEE if action == "buy" and m["created_by"] is not None else 0.0
        shares, coins, late = check_trade(m, outs, i, action, value, balance, held, fee)
    except TradeError as e:
        return jsonify(ok=False, error=str(e))
    q = [o["shares"] for o in outs]
    q[i] += shares if action == "buy" else -shares
    late_to = creator_name(db, m) if m["created_by"] is not None and not (g.user and g.user["id"] == m["created_by"]) else None
    est_bonus = 0.0
    if action == "buy":  # the leftover-pot bonus this bet would get if its outcome won right after it
        who = g.user["id"] if g.user else "visitor"
        est_bonus = estimated_bonus(db, mid, oid, who, (who, oid, shares, coins))
    return jsonify(ok=True, shares=round(shares, 4), coins=coins, entry_fee=fee, creator=creator_name(db, m),
                   late_fee=late, late_multiplier=late_multiplier(m), late_fee_to=late_to, est_bonus=est_bonus,
                   avg_price=coins / shares if shares else 0,
                   new_price=lmsr.prices(q, m["b"])[i])


# ---------------------------------------------------------------- routes: people

@app.route("/portfolio")
@login_required
def portfolio():
    db = get_db()
    uid = g.user["id"]
    rows = db.execute(
        "SELECT p.*, o.label, o.market_id, m.question FROM positions p "
        "JOIN outcomes o ON o.id = p.outcome_id JOIN markets m ON m.id = o.market_id "
        "WHERE p.user_id = ? ORDER BY m.closes_at", (uid,)).fetchall()
    bundles = {}
    active, settled = [], []
    for r in rows:
        if r["market_id"] not in bundles:
            m = db.execute("SELECT * FROM markets WHERE id = ?", (r["market_id"],)).fetchone()
            bundles[r["market_id"]] = bundle(db, m)
        b = bundles[r["market_id"]]
        outs = b["outcomes"]
        i = next(k for k, o in enumerate(outs) if o["id"] == r["outcome_id"])
        price = outs[i]["price"]
        if b["m"]["status"] == "open":  # open or awaiting result: value = what the shares would sell for now
            value = lmsr.sell_value([o["shares"] for o in outs], b["m"]["b"], i, r["shares"])
        elif b["m"]["status"] == "cancelled":
            value = 0.0  # refunded when the market was cancelled
        else:
            value = r["shares"] * price
        bonus = leftover_bonus(db, uid, r["market_id"]) if b["m"]["status"] == "resolved" and price > 0 else 0.0
        refund = None
        if b["m"]["status"] == "cancelled":
            refund = cents(max(db.execute("SELECT COALESCE(SUM(amount), 0) FROM trades WHERE user_id = ? AND market_id = ?",
                                          (uid, r["market_id"])).fetchone()[0], 0.0))
        item = {"r": r, "b": b, "price": price, "value": value + bonus, "bonus": bonus, "refund": refund}
        (active if b["m"]["status"] == "open" else settled).append(item)
    settled.sort(key=lambda i: i["b"]["m"]["resolved_at"] or "", reverse=True)
    trades = db.execute(
        "SELECT t.*, o.label, m.question FROM trades t JOIN outcomes o ON o.id = t.outcome_id "
        "JOIN markets m ON m.id = t.market_id WHERE t.user_id = ? ORDER BY t.id DESC LIMIT 30", (uid,)).fetchall()
    worth = net_worths(db)[uid]
    # Joint per-market value, so the KPI matches net worth even when someone holds several outcomes of a market.
    invested = sum(open_book_values(db).get(uid, {}).values())
    loan_totals = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN status = 'active' AND borrower_id = ? THEN owed END), 0) AS owe, "
        "COALESCE(SUM(CASE WHEN status = 'active' AND lender_id = ? THEN owed END), 0) AS owed, "
        "COALESCE(SUM(CASE WHEN status = 'offered' AND lender_id = ? THEN principal END), 0) AS held "
        "FROM loans", (uid, uid, uid)).fetchone()
    lo_rows = db.execute(
        "SELECT lo.*, o.label, m.question FROM limit_orders lo "
        "JOIN outcomes o ON o.id = lo.outcome_id JOIN markets m ON m.id = lo.market_id "
        "WHERE lo.user_id = ? AND lo.status = 'open' ORDER BY lo.id DESC", (uid,)).fetchall()
    limit_orders = []
    for lo in lo_rows:
        if lo["market_id"] not in bundles:
            m = db.execute("SELECT * FROM markets WHERE id = ?", (lo["market_id"],)).fetchone()
            bundles[lo["market_id"]] = bundle(db, m)
        b = bundles[lo["market_id"]]
        o = next((x for x in b["outcomes"] if x["id"] == lo["outcome_id"]), None)
        limit_orders.append({
            **dict(lo),
            "current_price": o["price"] if o else 0.0,
            "state": b["state"],
        })
    return render_template("portfolio.html", active=active, settled=settled, trades=trades, worth=worth,
                           invested=invested, rows_total=sum(i["value"] for i in active),
                           loan_totals=loan_totals, limit_orders=limit_orders)


@app.post("/limit-orders/<int:oid>/cancel")
@login_required
def cancel_limit_order_route(oid):
    db = get_db()
    if cancel_limit_order(db, g.user["id"], oid):
        flash("Limit order cancelled.", "success")
    else:
        flash("Limit order not found or already closed.", "error")
    nxt = (request.form.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("portfolio"))


@app.post("/limit-orders/cancel-all")
@login_required
def cancel_all_limit_orders_route():
    db = get_db()
    n = cancel_all_limit_orders(db, g.user["id"])
    if n:
        flash(f"Cancelled {n} open limit order{'s' if n != 1 else ''}.", "success")
    else:
        flash("You have no open limit orders to cancel.", "info")
    return redirect(url_for("portfolio"))


ACTIVITY_FILTERS = {
    "": None,
    "trades": ("buy", "sell"),
    "results": ("payout", "loss", "refund"),
    "fees": ("fee", "earning"),
    "other": ("signup", "bonus", "admin", "loan"),
}


FEED_PAGE = 100


@app.route("/activity")
def activity():
    """Every trade anyone has made, newest first. Filter with ?show=buys|sells, ?trader=name, ?market=id;
    page back with ?before=<id>."""
    db = get_db()
    show = request.args.get("show", "")
    trader = request.args.get("trader", "").strip()
    try:
        market_id = int(request.args.get("market", "") or 0) or None
        before = int(request.args.get("before", "") or 0) or None
    except ValueError:
        abort(404)
    where, args = ["a.kind IN ('buy', 'sell')"], []
    if show in ("buys", "sells"):
        where.append("a.kind = ?")
        args.append(show[:-1])
    if trader:
        where.append("u.username = ?")
        args.append(trader)
    if market_id:
        where.append("a.market_id = ?")
        args.append(market_id)
    totals = db.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(ABS(a.amount)), 0) AS volume, COUNT(DISTINCT a.user_id) AS traders "
        f"FROM activity a JOIN users u ON u.id = a.user_id WHERE {' AND '.join(where)}", args).fetchone()
    if before:
        where.append("a.id < ?")
        args.append(before)
    rows = db.execute(
        "SELECT a.*, u.username, (m.id IS NOT NULL) AS market_exists FROM activity a "
        "JOIN users u ON u.id = a.user_id LEFT JOIN markets m ON m.id = a.market_id "
        f"WHERE {' AND '.join(where)} ORDER BY a.id DESC LIMIT ?", args + [FEED_PAGE + 1]).fetchall()
    more = len(rows) > FEED_PAGE
    rows = rows[:FEED_PAGE]
    market_title = None
    if market_id:
        hit = db.execute("SELECT market_title FROM activity WHERE market_id = ? LIMIT 1", (market_id,)).fetchone()
        market_title = hit["market_title"] if hit else f"market #{market_id}"
    return render_template("activity.html", rows=rows, totals=totals, show=show, trader=trader,
                           market_id=market_id, market_title=market_title,
                           older=rows[-1]["id"] if more and rows else None)


@app.route("/activity/mine")
@login_required
def balance_history():
    """Everything that changed a user's balance, newest first. Admins can view anyone's with ?user=name."""
    db = get_db()
    target = g.user
    name = request.args.get("user", "").strip()
    if name and g.user["is_admin"]:
        target = db.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
        if target is None:
            abort(404)
    show = request.args.get("show", "")
    kinds = ACTIVITY_FILTERS.get(show)
    where, args = "a.user_id = ?", [target["id"]]
    if kinds:
        where += f" AND a.kind IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    rows = db.execute(
        "SELECT a.*, (m.id IS NOT NULL) AS market_exists FROM activity a LEFT JOIN markets m ON m.id = a.market_id "
        f"WHERE {where} ORDER BY a.id DESC LIMIT 300", args).fetchall()
    totals = {r["kind"]: r["total"] for r in db.execute(
        "SELECT kind, SUM(amount) AS total FROM activity WHERE user_id = ? GROUP BY kind", (target["id"],))}
    return render_template("balance_history.html", rows=rows, target=target, show=show, totals=totals,
                           viewing_other=target["id"] != g.user["id"])


@app.route("/leaderboard")
@throttled
def leaderboard():
    db = get_db()
    worth = net_worths(db)
    counts = {r["user_id"]: r["n"] for r in db.execute("SELECT user_id, COUNT(*) AS n FROM trades GROUP BY user_id")}
    # accounts that never confirmed an email and never traded (abandoned sign-ups) aren't listed
    users = db.execute("SELECT u.id, u.username, u.balance, u.email_verified, c.score AS credit, c.tier AS credit_tier "
                       "FROM users u LEFT JOIN user_credit c ON c.user_id = u.id WHERE u.held = 0").fetchall()
    defaulters = {l["borrower_id"] for l in db.execute("SELECT * FROM loans WHERE status = 'active'")
                  if in_default(db, l)}
    board_by_name = {u["username"].casefold(): {"username": u["username"], "worth": worth[u["id"]],
                                                "credit": u["credit"], "credit_tier": u["credit_tier"],
                                                "defaulted": u["id"] in defaulters,
                                                "balance": u["balance"], "trades": counts.get(u["id"], 0),
                                                "is_me": g.user is not None and g.user["id"] == u["id"]}
                     for u in users if u["email_verified"] or counts.get(u["id"], 0)}
    board = sorted(board_by_name.values(), key=lambda x: -x["worth"])
    return render_template("leaderboard.html", board=board, LOAN_GRACE_DAYS=LOAN_GRACE_DAYS)


# ---------------------------------------------------------------- loans between players

def loan_owed_total(principal, rate):
    """What the borrower pays back in total: the amount plus flat interest."""
    return cents(principal * (1 + rate / 100))


def close_loan_offer(db, loan, status, detail):
    """Turn down or withdraw an offer that was never taken: the held money goes back to the lender. Call inside a
    transaction."""
    db.execute("UPDATE loans SET status = ?, closed_at = ? WHERE id = ?", (status, now_str(), loan["id"]))
    db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (loan["principal"], loan["lender_id"]))
    log_activity(db, loan["lender_id"], "loan", loan["principal"], None, detail)


def pay_loan(db, loan, amount, automatic=False):
    """Move `amount` from the borrower to the lender. Call inside a transaction, with amount <= both the borrower's
    cash and what they owe. Marks the loan repaid once nothing is left."""
    amount = cents(amount)
    if amount <= 0:
        return 0.0
    names = {r["id"]: r["username"] for r in db.execute(
        "SELECT id, username FROM users WHERE id IN (?, ?)", (loan["lender_id"], loan["borrower_id"]))}
    left = cents(loan["owed"] - amount)
    done = left < 0.005
    db.execute("UPDATE loans SET owed = ?, repaid = ROUND(repaid + ?, 2), status = ?, closed_at = ? WHERE id = ?",
               (0.0 if done else left, amount, "repaid" if done else "active", now_str() if done else None,
                loan["id"]))
    how = "Overdue loan collected" if automatic else "Loan repayment"
    db.execute("UPDATE users SET balance = ROUND(balance - ?, 2) WHERE id = ?", (amount, loan["borrower_id"]))
    log_activity(db, loan["borrower_id"], "loan", -amount, None,
                 f"{how} to {names.get(loan['lender_id'], '?')}" + (" (paid off)" if done else ""))
    db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (amount, loan["lender_id"]))
    log_activity(db, loan["lender_id"], "loan", amount, None,
                 f"{how} from {names.get(loan['borrower_id'], '?')}" + (" (paid off)" if done else ""))
    return amount


def credit_loan(db, loan, amount, detail_borrower, detail_lender, market=None):
    """Knock `amount` off a loan without any cash moving (the lender took something else in payment). Call inside a
    transaction, with amount <= what's owed. Marks the loan repaid once nothing is left."""
    amount = cents(amount)
    if amount <= 0:
        return 0.0
    left = cents(loan["owed"] - amount)
    done = left < 0.005
    db.execute("UPDATE loans SET owed = ?, repaid = ROUND(repaid + ?, 2), status = ?, closed_at = ? WHERE id = ?",
               (0.0 if done else left, amount, "repaid" if done else "active", now_str() if done else None,
                loan["id"]))
    suffix = " (paid off)" if done else ""
    log_activity(db, loan["borrower_id"], "loan", 0, market, detail_borrower + suffix)
    log_activity(db, loan["lender_id"], "loan", 0, market, detail_lender + suffix)
    return amount


def collect_for_borrower(db, borrower_id):
    """Take what's owed on this borrower's overdue loans out of their cash, oldest first, as much as they have.
    Call inside a transaction."""
    taken = 0.0
    for loan in db.execute("SELECT * FROM loans WHERE status = 'active' AND borrower_id = ? AND due_at <= ? "
                           "ORDER BY due_at, id", (borrower_id, now_str())).fetchall():
        cash = db.execute("SELECT balance FROM users WHERE id = ?", (borrower_id,)).fetchone()["balance"]
        taken += pay_loan(db, loan, min(cash, loan["owed"]), automatic=True)
    return taken


def collect_overdue_loans(db):
    """Take what's owed on overdue loans out of the borrower's cash, as much as they have. Runs on every request,
    so money a borrower gets later (sales, payouts, bonuses) goes to their lenders until the debt is cleared.
    Oldest loans are paid first. Loan offers an overdue borrower still has open are withdrawn first, so the money
    they hold goes to their lenders as well. Before that, overdue loans get their late fees: LOAN_LATE_FEE of what's
    still owed when the due date passes, then again at the start of each further day it's unpaid, each fee on the
    new total (so they compound), until the grace period ends and lenders may collect from bets."""
    if not db.execute(f"SELECT 1 FROM loans WHERE {LATE_FEE_DUE_SQL} LIMIT 1", (LOAN_GRACE_DAYS, now_str())).fetchone() and not db.execute("SELECT 1 FROM loans l JOIN users u ON u.id = l.borrower_id WHERE l.status = 'active' "
                      "AND l.due_at <= ? AND u.balance >= 0.01 LIMIT 1", (now_str(),)).fetchone() \
            and not db.execute("SELECT 1 FROM loans o WHERE o.status = 'offered' AND EXISTS (SELECT 1 FROM loans a "
                               "WHERE a.status = 'active' AND a.borrower_id = o.lender_id AND a.due_at <= ?) LIMIT 1",
                               (now_str(),)).fetchone():
        return
    paid_borrowers = []
    db.execute("BEGIN IMMEDIATE")
    try:
        for l in db.execute(f"SELECT * FROM loans WHERE {LATE_FEE_DUE_SQL}", (LOAN_GRACE_DAYS, now_str())).fetchall():
            add_late_fees(db, l)
        for offer in db.execute("SELECT * FROM loans o WHERE o.status = 'offered' AND EXISTS (SELECT 1 FROM loans a "
                                "WHERE a.status = 'active' AND a.borrower_id = o.lender_id AND a.due_at <= ?)",
                                (now_str(),)).fetchall():
            close_loan_offer(db, offer, "cancelled", "Loan offer withdrawn: you have an overdue loan")
        for row in db.execute("SELECT DISTINCT borrower_id FROM loans WHERE status = 'active' AND due_at <= ?",
                              (now_str(),)).fetchall():
            if collect_for_borrower(db, row["borrower_id"]) > 0:
                paid_borrowers.append(row["borrower_id"])
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    if paid_borrowers:  # only when money actually moved, so a borrower with nothing to take isn't re-scored every request
        rescore_credit(db, *paid_borrowers, reason="overdue loan collected")


# Active loans owed another late fee: the next one is due `late_fees` days after the due date, up to LOAN_GRACE_DAYS.
LATE_FEE_DUE_SQL = ("status = 'active' AND late_fees < ? "
                    "AND datetime(due_at, '+' || late_fees || ' days') <= ?")


def late_fees_due(loan, now=None):
    """How many late fees this loan should have had by now: one at the due date and one at the start of each further
    day it's unpaid, at most LOAN_GRACE_DAYS."""
    late = (now or datetime.now()) - parse_dt(loan["due_at"])
    if late < timedelta(0):
        return 0
    return min(LOAN_GRACE_DAYS, late.days + 1)


def add_late_fees(db, loan):
    """Add every late fee this loan is owed and hasn't had (more than one if nobody visited the site for a while).
    Each is LOAN_LATE_FEE of what's owed after the fees before it. Call inside a transaction."""
    owed, penalty, done = loan["owed"], loan["penalty"], loan["late_fees"]
    due = late_fees_due(loan)
    for k in range(done, due):
        fee = cents(owed * LOAN_LATE_FEE)
        owed, penalty = cents(owed + fee), cents(penalty + fee)
        if fee > 0:
            log_activity(db, loan["borrower_id"], "loan", 0, None,
                         f"Late fee of Ⓜ{fee:,.2f} added to an overdue loan ({LOAN_LATE_FEE:.0%} of what was owed, "
                         f"day {k + 1} of {LOAN_GRACE_DAYS})")
    if due > done:
        db.execute("UPDATE loans SET late_at = COALESCE(late_at, ?), penalty = ?, owed = ?, late_fees = ? WHERE id = ?",
                   (now_str(), penalty, owed, due, loan["id"]))


def has_overdue_loan(db, user_id):
    return db.execute("SELECT 1 FROM loans WHERE status = 'active' AND borrower_id = ? AND due_at <= ?",
                      (user_id, now_str())).fetchone() is not None


def overdue_block(db, user_id):
    """Why this user can't bet, create markets or lend right now (they have an overdue loan), or None."""
    if has_overdue_loan(db, user_id):
        return ("You have an overdue loan. Buying, lending and creating markets are paused until it's paid back; "
                "selling and repaying still work.")
    return None


def loan_grace_end(db, loan):
    """When the lender may start collecting from the borrower's bets: LOAN_GRACE_DAYS after the due date, or after
    the day these rules began if the loan was already overdue then."""
    start = get_setting(db, "loans_v2_at") or "1970-01-01 00:00:00"
    base = max(loan["due_at"], start)
    return (parse_dt(base) + timedelta(days=LOAN_GRACE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")


def in_default(db, loan):
    return loan["status"] == "active" and loan_grace_end(db, loan) <= now_str()


def borrower_record(db, user_id):
    """How a player has handled loans so far: {'ontime', 'late', 'default'} counts."""
    rec = {"ontime": 0, "late": 0, "default": 0}
    for l in db.execute("SELECT * FROM loans WHERE borrower_id = ? AND accepted_at IS NOT NULL "
                        "AND status IN ('repaid', 'active')", (user_id,)).fetchall():
        if l["status"] == "repaid":
            rec["ontime" if l["closed_at"] <= l["due_at"] else "late"] += 1
        elif in_default(db, l):
            rec["default"] += 1
        elif l["due_at"] <= now_str():
            rec["late"] += 1
    return rec


# ---------------------------------------------------------------- credit scores (see credit.py)

CREDIT_TTL = 3600          # a stored score older than this is recomputed when someone looks at it
CREDIT_HISTORY_GAP = 3600  # at most one history point an hour per player, unless the tier changed


def credit_config(db):
    """The scoring settings: credit.DEFAULTS, the admin's saved overrides, and the loan rules from this file."""
    cfg = credit.merged_config(get_setting(db, "credit_cfg"))
    cfg["grace_days"], cfg["limit_floor"], cfg["limit_ratio"] = LOAN_GRACE_DAYS, LOAN_CREDIT_FLOOR, LOAN_CREDIT_RATIO
    cfg["risk_floor"] = security.FLAG_THRESHOLD
    return cfg


def decode_credit(row):
    """A user_credit row as a dict with its JSON columns unpacked (None if there is no row)."""
    if row is None:
        return None
    d = dict(row)
    for key in ("factors", "reasons", "caps"):
        try:
            d[key] = json.loads(d.get(key) or "[]")
        except ValueError:
            d[key] = []
    return d


def credit_compute(db, uid, cfg, books, adjust=0):
    """credit.compute_credit with this site's inputs: the sell-now value of the player's bets and their alt-risk row."""
    rk = db.execute("SELECT score, status, links FROM user_risk WHERE user_id = ?", (uid,)).fetchone()
    risk = None
    if rk:
        try:
            links = json.loads(rk["links"] or "{}")
        except ValueError:
            links = {}
        risk = {"score": rk["score"], "status": rk["status"], "links": links}
    return credit.compute_credit(db, uid, cfg, bets=sum(books.get(uid, {}).values()), risk=risk, adjust=adjust,
                                 grace_start=get_setting(db, "loans_v2_at"))


def apply_credit(db, uid, reason="", cfg=None, books=None):
    """Score a player and store the result (plus a history point when it moved). Admin overrides are kept. Admins and
    the owner aren't scored. Don't call inside a transaction you might roll back. Returns the stored row as a dict."""
    u = db.execute("SELECT id, username, is_admin FROM users WHERE id = ?", (uid,)).fetchone()
    if u is None:
        return None
    if u["is_admin"] or uid == owner_id(db):
        db.execute("DELETE FROM user_credit WHERE user_id = ?", (uid,))
        return None
    cfg = cfg or credit_config(db)
    books = open_book_values(db) if books is None else books
    cur = db.execute("SELECT * FROM user_credit WHERE user_id = ?", (uid,)).fetchone()
    r = credit_compute(db, uid, cfg, books, cur["adjust"] if cur else 0)
    use = r["owe"] / r["base"] if r["base"] else 0.0
    now = now_str()
    db.execute(
        "INSERT INTO user_credit (user_id, score, tier, multiplier, factors, reasons, caps, thin, utilisation, "
        "ontime, late, defaulted, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET score = excluded.score, tier = excluded.tier, "
        "multiplier = excluded.multiplier, factors = excluded.factors, reasons = excluded.reasons, "
        "caps = excluded.caps, thin = excluded.thin, utilisation = excluded.utilisation, ontime = excluded.ontime, "
        "late = excluded.late, defaulted = excluded.defaulted, updated_at = excluded.updated_at",
        (uid, r["score"], r["tier"], r["multiplier"], json.dumps(r["factors"]), json.dumps(r["reasons"]),
         json.dumps(r["caps"]), int(r["thin"]), round(use, 4), r["record"]["ontime"], r["record"]["late"],
         r["record"]["default"], now))
    last = db.execute("SELECT score, tier, created_at FROM credit_history WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                      (uid,)).fetchone()
    tier_changed = last is not None and last["tier"] != r["tier"]
    if last is None or (last["score"] != r["score"] and (
            tier_changed or (datetime.now() - parse_dt(last["created_at"])).total_seconds() >= CREDIT_HISTORY_GAP)):
        db.execute("INSERT INTO credit_history (user_id, score, tier, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                   (uid, r["score"], r["tier"], reason, now))
    if tier_changed:
        log_event("credit_tier_change", user_id=uid, username=u["username"],
                  detail=f"{last['tier']} → {r['tier']} ({last['score']} → {r['score']})")
    return decode_credit(db.execute("SELECT * FROM user_credit WHERE user_id = ?", (uid,)).fetchone())


def get_credit(db, uid, max_age=CREDIT_TTL, cfg=None, books=None):
    """The stored credit row (a dict), recomputed first if it is missing or older than max_age seconds. None for
    admins, the owner and unknown users."""
    row = db.execute("SELECT * FROM user_credit WHERE user_id = ?", (uid,)).fetchone()
    if row is not None:
        try:
            if (datetime.now() - parse_dt(row["updated_at"])).total_seconds() <= max_age:
                return decode_credit(row)
        except ValueError:
            pass
    return apply_credit(db, uid, "refresh", cfg, books)


def rescore_credit(db, *uids, reason=""):
    """Re-score players after something that changes their record (a loan, a repayment, an admin action). A problem
    here is logged and ignored: it must never get in the way of a loan."""
    try:
        uids = {u for u in uids if u}
        if uids:
            cfg, books = credit_config(db), open_book_values(db)
            for uid in uids:
                apply_credit(db, uid, reason, cfg, books)
    except Exception:
        app.logger.exception("credit re-score failed")


def refresh_credit(db, limit=300):
    """Bring stored scores up to date before a page filters or counts on them: score players who have none, and
    re-score borrowers whose loan became overdue, or went into default, after their score was last computed. (Those
    moments don't trigger anything on their own when the borrower has no cash to collect.)"""
    n = score_missing(db, limit)
    grace = f"+{LOAN_GRACE_DAYS} days"
    ids = [r["borrower_id"] for r in db.execute(
        "SELECT DISTINCT l.borrower_id FROM loans l JOIN user_credit c ON c.user_id = l.borrower_id "
        "WHERE l.status = 'active' AND ((l.due_at <= ? AND c.updated_at < l.due_at) "
        "OR (datetime(l.due_at, ?) <= ? AND c.updated_at < datetime(l.due_at, ?))) LIMIT ?",
        (now_str(), grace, now_str(), grace, limit))]
    if ids:
        rescore_credit(db, *ids, reason="loan overdue")
    return n + len(ids)


def score_missing(db, limit=300):
    """Score players who have no stored score yet (new accounts, or a database from before credit scores)."""
    ids = [r["id"] for r in db.execute(
        "SELECT u.id FROM users u LEFT JOIN user_credit c ON c.user_id = u.id WHERE c.user_id IS NULL "
        "AND u.is_admin = 0 AND u.id != ? ORDER BY u.id LIMIT ?", (owner_id(db) or 0, limit))]
    if ids:
        rescore_credit(db, *ids, reason="first score")
    return len(ids)


def credit_limit_detail(db, user_id, credit_row=None, books=None):
    """How much this player may owe in total, and how that comes about: the old rule (the floor, or half of cash +
    the sell-now value of bets - debts, whichever is more) times a multiplier from their credit score, unless an
    admin fixed their limit. Money they're owed doesn't count, so lending to an alt can't raise it."""
    cfg = credit_config(db)
    cash = db.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()["balance"]
    books = open_book_values(db) if books is None else books
    owe = db.execute("SELECT COALESCE(SUM(owed), 0) FROM loans WHERE status = 'active' AND borrower_id = ?",
                     (user_id,)).fetchone()[0]
    base = credit.base_limit(cash, sum(books.get(user_id, {}).values()), owe, cfg)
    c = credit_row if credit_row is not None else get_credit(db, user_id, cfg=cfg, books=books)
    mult = c["multiplier"] if c else 1.0
    override = c["limit_override"] if c else None
    scaled = credit.scaled_limit(cash, sum(books.get(user_id, {}).values()), owe, mult, cfg)
    return {"limit": override if override is not None else scaled, "score_limit": scaled, "owe": owe, "base": base,
            "multiplier": mult, "score": c["score"] if c else None, "tier": c["tier"] if c else None,
            "override": override, "frozen": bool(c and c["frozen"])}


def credit_limit(db, user_id, credit_row=None, books=None):
    """(limit, owed): how much this player may owe in total, and what they owe now. See credit_limit_detail."""
    d = credit_limit_detail(db, user_id, credit_row, books)
    return d["limit"], d["owe"]


def offer_loan(db, lender_id, borrower_name, principal, rate, days, note="", min_score=None):
    """Hold the lender's money and post the offer. borrower_name '' means anyone can take it. Returns the loan id;
    raises TradeError with a user-facing message."""
    principal, rate = cents(principal), round(rate, 2)
    if not (principal >= LOAN_MIN):
        raise TradeError(f"Lend at least Ⓜ{LOAN_MIN:.0f}.")
    if not (0 <= rate <= LOAN_MAX_RATE):
        raise TradeError(f"The interest rate must be between 0% and {LOAN_MAX_RATE:.0f}%.")
    if not (1 <= days <= LOAN_MAX_DAYS):
        raise TradeError(f"A loan can last from 1 to {LOAN_MAX_DAYS} days.")
    if len(note) > 120:
        raise TradeError("Keep the note under 120 characters.")
    if min_score is not None and not (credit.MIN_SCORE <= min_score <= credit.MAX_SCORE):
        raise TradeError(f"A minimum credit score must be between {credit.MIN_SCORE} and {credit.MAX_SCORE}.")
    min_score = min_score or None
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("SELECT 1 FROM loans WHERE status = 'active' AND borrower_id = ? LIMIT 1",
                      (lender_id,)).fetchone():
            # otherwise borrowed money could be passed on to another account, out of reach of the lender
            raise TradeError("Pay off what you've borrowed before lending.")
        borrower_id = None
        if borrower_name:
            row = db.execute("SELECT id FROM users WHERE username = ?", (borrower_name,)).fetchone()
            if row is None:
                raise TradeError(f"There's no player called “{borrower_name}”.")
            if row["id"] == lender_id:
                raise TradeError("You can't lend money to yourself.")
            borrower_id = row["id"]
        cash = db.execute("SELECT balance FROM users WHERE id = ?", (lender_id,)).fetchone()["balance"]
        if principal > cash + EPS:
            raise TradeError(f"You only have Ⓜ{cash:,.2f}.")
        cur = db.execute("INSERT INTO loans (lender_id, borrower_id, principal, rate, days, note, created_at, "
                         "min_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (lender_id, borrower_id, principal, rate, days, note, now_str(), min_score))
        db.execute("UPDATE users SET balance = ROUND(balance - ?, 2) WHERE id = ?", (principal, lender_id))
        log_activity(db, lender_id, "loan", -principal, None,
                     f"Loan offer to {borrower_name or 'anyone'} at {rate:g}% (held until taken)"
                     + (f", needs a credit score of {min_score}" if min_score else ""))
        db.execute("COMMIT")
        return cur.lastrowid
    except Exception:
        db.execute("ROLLBACK")
        raise


def accept_loan(db, loan_id, user_id):
    # Scored before the transaction opens, so a refused borrow can't leave half of a re-score (or its event) behind.
    # Always the current score, never a stale one. Admins and the owner aren't scored (c is None): the credit rules
    # (frozen, minimum score) don't apply to them, only the ordinary limits.
    c = get_credit(db, user_id, max_age=0)
    db.execute("BEGIN IMMEDIATE")
    try:
        loan = db.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
        if loan is None or loan["status"] != "offered":
            raise TradeError("That offer isn't available any more.")
        if loan["lender_id"] == user_id:
            raise TradeError("You can't take your own offer.")
        if loan["borrower_id"] not in (None, user_id):
            raise TradeError("That offer was made to someone else.")
        if has_overdue_loan(db, user_id):
            raise TradeError("Pay back your overdue loan before borrowing more.")
        if db.execute("SELECT 1 FROM loans WHERE status = 'offered' AND lender_id = ? LIMIT 1", (user_id,)).fetchone():
            raise TradeError("Withdraw your loan offers before borrowing.")
        if db.execute("SELECT COUNT(*) FROM loans WHERE status = 'active' AND borrower_id = ?",
                      (user_id,)).fetchone()[0] >= LOAN_MAX_ACTIVE:
            raise TradeError(f"You can have at most {LOAN_MAX_ACTIVE} loans running at once. Pay one back first.")
        owed = loan_owed_total(loan["principal"], loan["rate"])
        if c and c["frozen"]:
            log_event("credit_blocked_borrow", user_id=user_id, detail=f"loan {loan_id}: borrowing frozen")
            raise TradeError("Borrowing is paused on your account by an admin.")
        if loan["min_score"] and c and c["score"] < loan["min_score"]:
            log_event("credit_blocked_borrow", user_id=user_id,
                      detail=f"loan {loan_id}: needs {loan['min_score']}, has {c['score']}")
            raise TradeError(f"This offer needs a credit score of {loan['min_score']}; yours is {c['score']}.")
        limit, owe = credit_limit(db, user_id, credit_row=c)
        if owe + owed > limit + EPS:
            raise TradeError(f"You can owe at most Ⓜ{limit:,.2f} in total and you already owe Ⓜ{owe:,.2f}. "
                             f"This loan would make it Ⓜ{owe + owed:,.2f}.")
        now = datetime.now()
        due = (now + timedelta(days=loan["days"])).strftime("%Y-%m-%d %H:%M:%S")
        db.execute("UPDATE loans SET status = 'active', borrower_id = ?, owed = ?, accepted_at = ?, due_at = ? "
                   "WHERE id = ?", (user_id, owed, now.strftime("%Y-%m-%d %H:%M:%S"), due, loan_id))
        lender = db.execute("SELECT username FROM users WHERE id = ?", (loan["lender_id"],)).fetchone()["username"]
        db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (loan["principal"], user_id))
        log_activity(db, user_id, "loan", loan["principal"], None,
                     f"Borrowed from {lender} at {loan['rate']:g}%, Ⓜ{owed:,.2f} due in {loan['days']} days")
        db.execute("COMMIT")
        return owed
    except Exception:
        db.execute("ROLLBACK")
        raise


def repay_loan(db, loan_id, user_id, amount=None):
    """Pay back part (amount) or all (None) of a loan. Returns the amount paid."""
    db.execute("BEGIN IMMEDIATE")
    try:
        loan = db.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
        if loan is None or loan["borrower_id"] != user_id or loan["status"] != "active":
            raise TradeError("You don't have that loan to pay back.")
        amount = loan["owed"] if amount is None else min(cents(amount), loan["owed"])
        if amount < 0.01:
            raise TradeError("Enter how much to pay back.")
        cash = db.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()["balance"]
        if amount > cash + EPS:
            raise TradeError(f"You only have Ⓜ{cash:,.2f}.")
        paid = pay_loan(db, loan, amount)
        db.execute("COMMIT")
        return paid
    except Exception:
        db.execute("ROLLBACK")
        raise


def shares_to_cover(q, b, idx, held, owed):
    """The fewest shares (of `held`) whose sale pays at least `owed`, or all of them if even that isn't enough.
    Searches on the value rounded down to cents, the way a sale pays out. Returns (shares, coins)."""
    def worth(n):
        return floor_cents(lmsr.sell_value(q, b, idx, n))
    if worth(held) <= owed:
        return held, worth(held)
    lo, hi = 0.0, held
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if worth(mid) < owed else (lo, mid)
    return hi, worth(hi)


def collectable_positions(db, loan, lender_id):
    """What a lender could collect on a loan in default: the borrower's bets in markets that are still open, with the
    shares it would take to cover what's owed and what those are worth now."""
    rows = db.execute("SELECT p.outcome_id, p.shares, o.label, o.market_id FROM positions p "
                      "JOIN outcomes o ON o.id = p.outcome_id JOIN markets m ON m.id = o.market_id "
                      "WHERE p.user_id = ? AND p.shares > 0 AND m.status = 'open'", (loan["borrower_id"],)).fetchall()
    out = []
    for r in rows:
        m = db.execute("SELECT * FROM markets WHERE id = ?", (r["market_id"],)).fetchone()
        if market_state(m) != "open":
            continue
        outs = get_outcomes(db, m["id"])
        idx = [o["id"] for o in outs].index(r["outcome_id"])
        n, coins = shares_to_cover(
            [o["shares"] for o in outs], m["b"], idx, r["shares"], loan["owed"])
        if coins < 0.01:
            continue
        can_take = (trade_block_reason(db, lender_id, m["id"]) is None and late_multiplier(m) == 0
                    and overdue_block(db, lender_id) is None)
        out.append({"outcome_id": r["outcome_id"], "market_id": m["id"], "question": m["question"],
                    "label": r["label"], "held": r["shares"], "shares": n, "value": coins, "can_take": can_take})
    return out


def seize_position(db, loan_id, lender_id, outcome_id, mode):
    """A lender collects on a loan in default from one of the borrower's bets in an open market, only up to what is
    owed. mode 'sell' sells the shares on the borrower's behalf and pays the lender the proceeds; mode 'take' hands
    the shares to the lender at their sell-now value, which comes off the debt. Returns a message for the lender."""
    if mode not in ("sell", "take"):
        raise TradeError("Choose force sell or take over.")
    db.execute("BEGIN IMMEDIATE")
    try:
        loan = db.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
        if loan is None or loan["lender_id"] != lender_id or loan["status"] != "active":
            raise TradeError("That isn't one of your running loans.")
        if loan_grace_end(db, loan) > now_str():
            raise TradeError(f"You can collect from their bets after {loan_grace_end(db, loan)}.")
        borrower_id = loan["borrower_id"]
        collect_for_borrower(db, borrower_id)  # cash first, the oldest loans first
        loan = db.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
        if loan["status"] != "active":
            db.execute("COMMIT")
            return "Their cash already covered the loan, so nothing was taken from their bets."
        o = db.execute("SELECT * FROM outcomes WHERE id = ?", (outcome_id,)).fetchone()
        m = db.execute("SELECT * FROM markets WHERE id = ?", (o["market_id"],)).fetchone() if o else None
        if m is None or market_state(m) != "open":
            raise TradeError("You can only collect from bets in markets that are still open. "
                             "Anything in a closed market is paid to lenders when it resolves.")
        pos = db.execute("SELECT * FROM positions WHERE user_id = ? AND outcome_id = ?",
                         (borrower_id, outcome_id)).fetchone()
        if pos is None or pos["shares"] <= EPS:
            raise TradeError("They don't hold that bet any more.")
        outs = get_outcomes(db, m["id"])
        idx = [x["id"] for x in outs].index(outcome_id)
        q = [x["shares"] for x in outs]
        n, coins = shares_to_cover(q, m["b"], idx, pos["shares"], loan["owed"])
        # whole hundredths of a share (rounded up, so the sale still covers what's owed), priced like any sale
        held_centi = lmsr.to_centi_shares(pos["shares"])
        n_centi = min(held_centi, max(1, math.ceil(n * lmsr.SHARES_SCALE - 1e-9)))
        n = lmsr.from_centi_shares(n_centi)
        coins = lmsr.from_cents(lmsr.quote_sell_cents([lmsr.to_centi_shares(x) for x in q], m["b"], idx, n_centi))
        if coins < 0.01:
            raise TradeError("That bet is worth less than Ⓜ0.01.")
        names = {r["id"]: r["username"] for r in db.execute(
            "SELECT id, username FROM users WHERE id IN (?, ?)", (borrower_id, lender_id))}
        label = o["label"]
        new_shares = lmsr.from_centi_shares(held_centi - n_centi)
        new_cost = pos["cost"] * (new_shares / pos["shares"])
        if mode == "sell":
            db.execute("UPDATE outcomes SET shares = ? WHERE id = ?",
                       (lmsr.from_centi_shares(lmsr.to_centi_shares(outs[idx]["shares"]) - n_centi), outcome_id))
            set_position(db, borrower_id, outcome_id, new_shares, new_cost)
            db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (coins, borrower_id))
            log_activity(db, borrower_id, "sell", coins, m,
                         f"Forced sale of {n:,.2f} shares of “{label}” to repay {names[lender_id]}")
            db.execute("INSERT INTO trades (user_id, market_id, outcome_id, shares, amount, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (borrower_id, m["id"], outcome_id, -n, -coins, now_str()))
            snapshot_prices(db, m["id"], m["b"])
            process_limit_orders(db, m["id"])
            paid = pay_loan(db, loan, min(coins, loan["owed"]), automatic=True)
            msg = f"Sold {n:,.2f} shares of “{label}” for Ⓜ{coins:,.2f}. Ⓜ{paid:,.2f} went to your loan."
        else:
            block = trade_block_reason(db, lender_id, m["id"]) or overdue_block(db, lender_id)
            if block:
                raise TradeError(block)
            if late_multiplier(m) > 0:
                raise TradeError("Betting in this market is about to close, so you can only force a sale here.")
            fee = entry_fee_due(db, m, lender_id)
            if fee:
                cash = db.execute("SELECT balance FROM users WHERE id = ?", (lender_id,)).fetchone()["balance"]
                if cash + EPS < fee:
                    raise TradeError(f"Taking over a first bet in this market costs the Ⓜ{fee:,.0f} entry fee "
                                     f"and you have Ⓜ{cash:,.2f}.")
                creator = creator_name(db, m)
                db.execute("UPDATE users SET balance = ROUND(balance - ?, 2) WHERE id = ?", (fee, lender_id))
                log_activity(db, lender_id, "fee", -fee, m, f"Entry fee (first bet), paid to {creator}")
                db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (fee, m["created_by"]))
                log_activity(db, m["created_by"], "earning", fee, m, f"Entry fee from {names[lender_id]}")
                db.execute("INSERT INTO market_entries (market_id, user_id, creator_id, fee, created_at) "
                           "VALUES (?, ?, ?, ?, ?)", (m["id"], lender_id, m["created_by"], fee, now_str()))
            mine = db.execute("SELECT * FROM positions WHERE user_id = ? AND outcome_id = ?",
                              (lender_id, outcome_id)).fetchone()
            set_position(db, borrower_id, outcome_id, new_shares, new_cost)
            set_position(db, lender_id, outcome_id, (mine["shares"] if mine else 0.0) + n,
                         (mine["cost"] if mine else 0.0) + coins)
            now = now_str()
            db.execute("INSERT INTO trades (user_id, market_id, outcome_id, shares, amount, created_at, via) "
                       "VALUES (?, ?, ?, ?, ?, ?, 'loan')", (borrower_id, m["id"], outcome_id, -n, -coins, now))
            db.execute("INSERT INTO trades (user_id, market_id, outcome_id, shares, amount, created_at, via) "
                       "VALUES (?, ?, ?, ?, ?, ?, 'loan')", (lender_id, m["id"], outcome_id, n, coins, now))
            credited = credit_loan(
                db, loan, min(coins, loan["owed"]),
                f"{names[lender_id]} took over {n:,.2f} shares of “{label}” (worth Ⓜ{coins:,.2f}) for your overdue loan",
                f"Took over {n:,.2f} shares of “{label}” from {names[borrower_id]} (worth Ⓜ{coins:,.2f}) "
                f"for their overdue loan", m)
            msg = f"Took over {n:,.2f} shares of “{label}” worth Ⓜ{coins:,.2f}. Ⓜ{credited:,.2f} came off the loan."
        db.execute("COMMIT")
        return msg
    except Exception:
        db.execute("ROLLBACK")
        raise


def end_loan_offer(db, loan_id, user_id, status):
    """status 'cancelled' (the lender withdraws) or 'declined' (the named borrower turns it down)."""
    db.execute("BEGIN IMMEDIATE")
    try:
        loan = db.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
        who = "lender_id" if status == "cancelled" else "borrower_id"
        if loan is None or loan["status"] != "offered" or loan[who] != user_id:
            raise TradeError("That offer isn't available any more.")
        if status == "cancelled":
            close_loan_offer(db, loan, status, "Loan offer withdrawn")
        else:
            name = db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()["username"]
            close_loan_offer(db, loan, status, f"Loan offer declined by {name}")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


@app.route("/loans")
@login_required
def loans():
    db = get_db()
    uid = g.user["id"]
    q = ("SELECT l.*, lu.username AS lender, bu.username AS borrower FROM loans l "
         "JOIN users lu ON lu.id = l.lender_id LEFT JOIN users bu ON bu.id = l.borrower_id ")
    for_me = db.execute(q + "WHERE l.status = 'offered' AND l.lender_id != ? AND (l.borrower_id = ? OR l.borrower_id IS NULL) "
                        "ORDER BY l.borrower_id IS NULL, l.rate, l.id DESC", (uid, uid)).fetchall()
    borrowing = db.execute(q + "WHERE l.status = 'active' AND l.borrower_id = ? ORDER BY l.due_at", (uid,)).fetchall()
    lending = db.execute(q + "WHERE l.status IN ('offered', 'active') AND l.lender_id = ? "
                         "ORDER BY l.status = 'offered', l.due_at, l.id DESC", (uid,)).fetchall()
    history = db.execute(q + "WHERE l.status IN ('repaid', 'declined', 'cancelled', 'forgiven') "
                         "AND (l.lender_id = ? OR l.borrower_id = ?) "
                         "ORDER BY l.closed_at DESC LIMIT 30", (uid, uid)).fetchall()
    grace_end = {l["id"]: loan_grace_end(db, l) for l in borrowing + lending if l["status"] == "active"}
    now = now_str()
    # loans in default where this player is the lender: what they could collect from the borrower's bets
    collect = {l["id"]: collectable_positions(db, l, uid) for l in lending
               if l["status"] == "active" and grace_end[l["id"]] <= now}
    # how each of your borrowers has handled loans before, shown beside their name
    records = {bid: borrower_record(db, bid) for bid in {l["borrower_id"] for l in lending if l["borrower_id"]}}
    books = open_book_values(db)
    scores = {bid: get_credit(db, bid, books=books) for bid in records}
    mine = get_credit(db, uid, books=books)
    detail = credit_limit_detail(db, uid, credit_row=mine, books=books)
    limit, owe_now = detail["limit"], detail["owe"]
    return render_template("loans.html", for_me=for_me, borrowing=borrowing, lending=lending, history=history,
                           scores=scores, my_credit=mine, limit_detail=detail, tier_options=tier_options(db),
                           prefill_to=request.args.get("to", "")[:20], grace_end=grace_end, collect=collect, records=records, credit_limit=limit,
                           overdue_message=overdue_block(db, uid), LOAN_GRACE_DAYS=LOAN_GRACE_DAYS,
                           LOAN_MAX_ACTIVE=LOAN_MAX_ACTIVE,
                           now=now_str(), owed_total=loan_owed_total, LOAN_MAX_RATE=LOAN_MAX_RATE,
                           LOAN_MAX_DAYS=LOAN_MAX_DAYS, LOAN_MIN=LOAN_MIN,
                           owe=sum(l["owed"] for l in borrowing), borrow_room=max(0.0, limit - owe_now),
                           owed_to_me=sum(l["owed"] for l in lending if l["status"] == "active"),
                           held=sum(l["principal"] for l in lending if l["status"] == "offered"))


def tier_options(db):
    """[(slug, name, floor)] for the tiers a lender can require, best first (the lowest tier means 'anyone')."""
    return [(name.lower().replace(" ", "-"), name, floor) for name, floor in credit_config(db)["tiers"]
            if floor > credit.MIN_SCORE]


def _min_score_from_form(db):
    """The minimum credit score a lender asked for: a typed number wins over the tier preset. None means anyone."""
    raw = request.form.get("min_score", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            raise TradeError("The minimum credit score must be a whole number.")
    slug = request.form.get("min_tier", "")
    return next((floor for s_, _, floor in tier_options(db) if s_ == slug), None)


@app.route("/loans/all")
@throttled
def all_loans():
    """Every loan that's running right now, for anyone to see (logged in or not): who lent to whom, at what rate, what's
    still owed, the borrower's credit score, and whether it's on time, overdue or in default. Most urgent first. It only
    reads stored scores, so a public page view never triggers any scoring."""
    db = get_db()
    rows = [dict(r) for r in db.execute(
        "SELECT l.*, lu.username AS lender, bu.username AS borrower, c.score AS borrower_credit, "
        "c.tier AS borrower_tier FROM loans l JOIN users lu ON lu.id = l.lender_id "
        "JOIN users bu ON bu.id = l.borrower_id LEFT JOIN user_credit c ON c.user_id = l.borrower_id "
        "WHERE l.status = 'active' ORDER BY l.due_at, l.id")]
    now = now_str()
    for r in rows:
        r["default"] = in_default(db, r)
        r["overdue"] = r["due_at"] <= now
    rows.sort(key=lambda r: (not r["default"], not r["overdue"], r["due_at"]))
    offers = db.execute("SELECT COUNT(*) AS n, COALESCE(SUM(principal), 0) AS total FROM loans "
                        "WHERE status = 'offered'").fetchone()
    return render_template("all_loans.html", rows=rows, now=now, open_offers=offers,
                           total_owed=sum(r["owed"] for r in rows), LOAN_GRACE_DAYS=LOAN_GRACE_DAYS,
                           overdue=sum(1 for r in rows if r["overdue"] and not r["default"]),
                           defaulted=sum(1 for r in rows if r["default"]))


def _loan_number(name, kind=float):
    try:
        return kind(request.form.get(name, "").strip())
    except ValueError:
        raise TradeError("Fill in the amount, interest rate and number of days with numbers.")


@app.post("/loans/offer")
@login_required
def loan_offer():
    if rate_limited(f"loan:{g.user['id']}", 20, 3600):
        flash("You've made a lot of offers. Try again later.", "error")
        return redirect(url_for("loans"))
    try:
        borrower = request.form.get("borrower", "").strip()
        lid = offer_loan(get_db(), g.user["id"], borrower, _loan_number("amount"), _loan_number("rate"),
                         _loan_number("days", int), request.form.get("note", "").strip(),
                         _min_score_from_form(get_db()))
        loan = get_db().execute("SELECT * FROM loans WHERE id = ?", (lid,)).fetchone()
        rescore(get_db(), g.user["id"], loan["borrower_id"])
        flash(f"Offer posted: Ⓜ{loan['principal']:,.2f} to {borrower or 'anyone'} at {loan['rate']:g}%. "
              "The money is held until someone takes it; withdraw the offer any time before then.", "success")
    except TradeError as e:
        flash(str(e), "error")
    return redirect(url_for("loans"))


@app.post("/loans/<int:lid>/accept")
@login_required
def loan_accept(lid):
    try:
        owed = accept_loan(get_db(), lid, g.user["id"])
        lender = get_db().execute("SELECT lender_id FROM loans WHERE id = ?", (lid,)).fetchone()
        rescore(get_db(), g.user["id"], lender["lender_id"] if lender else None)
        rescore_credit(get_db(), g.user["id"], lender["lender_id"] if lender else None, reason="loan taken")
        flash(f"Loan taken. You owe Ⓜ{owed:,.2f}. Once it's overdue, buying, lending and creating markets are "
              "paused and anything you receive goes to the lender. If it's still unpaid "
              f"{LOAN_GRACE_DAYS} days after the due date, the lender can sell or take over your bets to cover it.",
              "success")
    except TradeError as e:
        flash(str(e), "error")
    return redirect(url_for("loans"))


@app.post("/loans/<int:lid>/repay")
@login_required
def loan_repay(lid):
    try:
        raw = request.form.get("amount", "").strip()
        amount = None if raw in ("", "all") else _loan_number("amount")
        paid = repay_loan(get_db(), lid, g.user["id"], amount)
        row = get_db().execute("SELECT lender_id FROM loans WHERE id = ?", (lid,)).fetchone()
        rescore_credit(get_db(), g.user["id"], row["lender_id"] if row else None, reason="loan repayment")
        flash(f"Paid back Ⓜ{paid:,.2f}.", "success")
    except TradeError as e:
        flash(str(e), "error")
    return redirect(url_for("loans"))


@app.post("/loans/<int:lid>/collect")
@login_required
def loan_collect(lid):
    if rate_limited(f"collect:{g.user['id']}", 30, 3600):
        flash("You've done that a lot. Try again later.", "error")
        return redirect(url_for("loans"))
    try:
        try:
            outcome_id = int(request.form.get("outcome_id", ""))
        except ValueError:
            raise TradeError("Pick a bet to collect from.")
        msg = seize_position(get_db(), lid, g.user["id"], outcome_id, request.form.get("mode", ""))
        loan = get_db().execute("SELECT borrower_id FROM loans WHERE id = ?", (lid,)).fetchone()
        rescore(get_db(), g.user["id"], loan["borrower_id"] if loan else None)
        rescore_credit(get_db(), g.user["id"], loan["borrower_id"] if loan else None, reason="collected from bets")
        flash(msg, "success")
    except TradeError as e:
        flash(str(e), "error")
    return redirect(url_for("loans"))


@app.post("/loans/<int:lid>/cancel")
@login_required
def loan_cancel(lid):
    try:
        end_loan_offer(get_db(), lid, g.user["id"], "cancelled")
        flash("Offer withdrawn. The money is back in your balance.", "success")
    except TradeError as e:
        flash(str(e), "error")
    return redirect(url_for("loans"))


@app.post("/loans/<int:lid>/decline")
@login_required
def loan_decline(lid):
    try:
        end_loan_offer(get_db(), lid, g.user["id"], "declined")
        flash("Offer declined.", "success")
    except TradeError as e:
        flash(str(e), "error")
    return redirect(url_for("loans"))


# ---------------------------------------------------------------- routes: credit scores

def score_line_points(values, w=240, h=56):
    """SVG polyline points for a list of scores (300-850) in a w x h box, oldest first."""
    if not values:
        return ""
    if len(values) == 1:
        values = values * 2
    lo, hi = min(values), max(values)
    if hi - lo < 30:  # a nearly flat history stays nearly flat
        mid = (hi + lo) / 2
        lo, hi = mid - 15, mid + 15
    pad, n = 4, len(values) - 1
    return " ".join(f"{pad + k * (w - 2 * pad) / n:.1f},{pad + (hi - v) / (hi - lo) * (h - 2 * pad):.1f}"
                    for k, v in enumerate(values))


def credit_history_points(db, uid, days=90):
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.execute("SELECT score, tier, reason, created_at FROM credit_history WHERE user_id = ? AND created_at >= ? "
                      "ORDER BY id", (uid, since)).fetchall()
    return rows, score_line_points([r["score"] for r in rows])


def _credit_page(db, target, full):
    """Render a credit report for `target` (a users row). `full` shows the factor breakdown (the player themself, or
    an admin); lenders see the score, tier and loan record."""
    c = get_credit(db, target["id"], max_age=60)
    if c is None:
        abort(404)
    detail = credit_limit_detail(db, target["id"], credit_row=c)
    if not g.user["is_admin"]:
        c = credit.redact(c)  # nothing from the alt detector reaches a player, not even their own report
    hist, points = credit_history_points(db, target["id"])
    loans_ = db.execute("SELECT l.*, lu.username AS lender FROM loans l JOIN users lu ON lu.id = l.lender_id "
                        "WHERE l.borrower_id = ? AND l.accepted_at IS NOT NULL ORDER BY l.id DESC LIMIT 20",
                        (target["id"],)).fetchall()
    return render_template("credit.html", target=target, c=c, detail=detail, history=hist[-10:][::-1], points=points,
                           spark_n=len(hist), spark_first=hist[0]["score"] if hist else None,
                           spark_last=hist[-1]["score"] if hist else None, viewer_admin=bool(g.user["is_admin"]),
                           loans=loans_, full=full, mine=target["id"] == g.user["id"], cfg=credit_config(db),
                           CREDIT_MIN=credit.MIN_SCORE, CREDIT_MAX=credit.MAX_SCORE, now=now_str())


@app.get("/credit")
@login_required
@throttled
def my_credit():
    db = get_db()
    if g.user["is_admin"] or g.user["id"] == g.owner_id:
        return render_template("credit.html", target=g.user, c=None, mine=True, full=True)
    return _credit_page(db, g.user, True)


@app.get("/credit/<username>")
@login_required
@throttled
def user_credit(username):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
    if target is None or ((target["is_admin"] or target["id"] == g.owner_id) and not g.user["is_admin"]):
        abort(404)
    if target["id"] == g.user["id"]:
        return redirect(url_for("my_credit"))
    return _credit_page(db, target, bool(g.user["is_admin"]))


BORROWER_PAGE = 25
BORROWER_SORTS = {"score": "c.score DESC, u.username", "ontime": "c.ontime DESC, c.score DESC, u.username",
                  "recent": "(SELECT MAX(l.accepted_at) FROM loans l WHERE l.borrower_id = u.id) DESC, c.score DESC",
                  "new": "u.created_at DESC"}


def _int_arg(name, lo, hi):
    try:
        return max(lo, min(hi, int(request.args.get(name, ""))))
    except ValueError:
        return None


@app.get("/loans/borrowers")
@login_required
@throttled
def loan_borrowers():
    """Lenders browse players by credit score: filter by score range or tier, by whether they've repaid before and
    whether they're clear of overdue loans, and sort by score, on-time record or recent borrowing."""
    db = get_db()
    refresh_credit(db)
    q = request.args.get("q", "").strip()
    lo, hi = _int_arg("min", credit.MIN_SCORE, credit.MAX_SCORE), _int_arg("max", credit.MIN_SCORE, credit.MAX_SCORE)
    tier = request.args.get("tier", "")
    tiers = [name for name, _ in credit_config(db)["tiers"]]
    sort = request.args.get("sort", "score")
    sort = sort if sort in BORROWER_SORTS else "score"
    where, args = ["u.is_admin = 0", "u.id != ?", "u.id != ?"], [g.user["id"], g.owner_id or 0]
    if q:
        where.append("LOWER(u.username) LIKE ?")
        args.append(f"%{q.lower()}%")
    if lo is not None:
        where.append("c.score >= ?")
        args.append(lo)
    if hi is not None:
        where.append("c.score <= ?")
        args.append(hi)
    if tier in tiers:
        where.append("c.tier = ?")
        args.append(tier)
    if request.args.get("repaid"):
        where.append("c.ontime + c.late > 0")
    if request.args.get("clean"):
        where.append("NOT EXISTS (SELECT 1 FROM loans l WHERE l.borrower_id = u.id AND l.status = 'active' "
                     "AND l.due_at <= ?)")
        args.append(now_str())
    frm = "FROM users u JOIN user_credit c ON c.user_id = u.id WHERE " + " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) {frm}", args).fetchone()[0]
    pages = max(1, -(-total // BORROWER_PAGE))
    try:
        page = min(pages, max(1, int(request.args.get("page", 1))))
    except ValueError:
        page = 1
    rows = db.execute(
        f"SELECT u.id, u.username, u.created_at, u.held, c.score, c.tier, c.multiplier, c.thin, c.utilisation, "
        f"c.ontime, c.late, c.defaulted, c.frozen, c.updated_at, "
        f"(SELECT MAX(l.accepted_at) FROM loans l WHERE l.borrower_id = u.id) AS last_loan {frm} "
        f"ORDER BY {BORROWER_SORTS[sort]} LIMIT ? OFFSET ?", args + [BORROWER_PAGE, (page - 1) * BORROWER_PAGE]).fetchall()
    rows = [dict(r) for r in rows]
    stale = [r for r in rows if (datetime.now() - parse_dt(r["updated_at"])).total_seconds() > CREDIT_TTL]
    if stale:  # refresh just the page being shown, so the list never costs more than 25 re-scores
        books, cfg = open_book_values(db), credit_config(db)
        for r in stale:
            c = apply_credit(db, r["id"], "refresh", cfg, books)
            if c:
                r.update(score=c["score"], tier=c["tier"], multiplier=c["multiplier"], thin=c["thin"],
                         utilisation=c["utilisation"], ontime=c["ontime"], late=c["late"], defaulted=c["defaulted"])
    for r in rows:
        try:
            r["age_days"] = (datetime.now() - parse_dt(r["created_at"])).days
        except ValueError:
            r["age_days"] = 0
    return render_template("loan_borrowers.html", rows=rows, total=total, page=page, pages=pages, tiers=tiers,
                           view={"q": q, "min": lo, "max": hi, "tier": tier, "sort": sort,
                                 "repaid": bool(request.args.get("repaid")), "clean": bool(request.args.get("clean"))})


# ---------------------------------------------------------------- routes: admin

USER_PAGE = 25
USER_FILTERS = {"": "Everyone", "admins": "Admins", "banned": "Banned from trading",
                "muted": "Muted from commenting", "held": "Held for review", "flagged": "Flagged as suspicious",
                "high": "High risk", "approved": "Approved after review"}


def list_users(db):
    """The Admin → Users table: search by name, email or IP, one filter, 25 per page. Returns (rows, view-state).
    Each row has `peek`: whether this admin may see that person's email and IP (everyone except admins, who only the
    owner may look into)."""
    q = request.args.get("q", "").strip()
    flt = request.args.get("filter", "")
    flt = flt if flt in USER_FILTERS else ""
    try:
        page = max(1, int(request.args.get("upage", 1)))
    except ValueError:
        page = 1
    where, args = [], []
    if q:
        like = f"%{q.lower()}%"
        clauses, cargs = ["LOWER(u.username) LIKE ?"], [like]
        peek_sql, peek_args = ("1", []) if is_owner() else ("(u.is_admin = 0 AND u.id != ?)", [g.owner_id])
        clauses.append(f"({peek_sql} AND (LOWER(COALESCE(u.email, '')) LIKE ? "
                       "OR LOWER(COALESCE(u.email_canonical, '')) LIKE ? "
                       "OR u.id IN (SELECT user_id FROM login_events WHERE ip LIKE ?) "
                       "OR u.id IN (SELECT user_id FROM sightings WHERE kind = 'ip' AND value LIKE ?) "
                       "OR LOWER(COALESCE((SELECT email FROM login_challenges c WHERE c.username = u.username "
                       "COLLATE NOCASE), '')) LIKE ?))")
        cargs += peek_args + [like, like, f"{q}%", f"{q}%", like]
        where.append("(" + " OR ".join(clauses) + ")")
        args += cargs
    if flt == "admins":
        where.append("(u.is_admin = 1 OR u.id = ?)")
        args.append(g.owner_id)
    elif flt == "banned":
        where.append("tb.user_id IS NOT NULL")
    elif flt == "muted":
        where.append("cm.user_id IS NOT NULL")
    elif flt in ("held", "flagged", "approved"):
        where.append("r.status = ?")
        args.append(flt)
    elif flt == "high":
        where.append("r.score >= ?")
        args.append(security.HOLD_THRESHOLD)
    frm = ("FROM users u LEFT JOIN trade_bans tb ON tb.user_id = u.id "
           "LEFT JOIN comment_mutes cm ON cm.user_id = u.id LEFT JOIN user_risk r ON r.user_id = u.id "
           "LEFT JOIN user_credit uc ON uc.user_id = u.id "
           + ("WHERE " + " AND ".join(where) if where else ""))
    total = db.execute(f"SELECT COUNT(*) {frm}", args).fetchone()[0]
    pages = max(1, -(-total // USER_PAGE))
    page = min(page, pages)
    order = "r.score DESC, u.username" if flt in ("held", "flagged", "high") else "u.username"
    rows = db.execute(
        "SELECT u.*, (tb.user_id IS NOT NULL) AS banned, (cm.user_id IS NOT NULL) AS muted, "
        "r.score AS risk_score, r.status AS risk_status, uc.score AS credit_score, uc.tier AS credit_tier, "
        "(SELECT e.ip FROM login_events e WHERE e.user_id = u.id AND e.ip != '' ORDER BY e.id DESC LIMIT 1) AS last_ip, "
        "(SELECT c.email FROM login_challenges c WHERE c.username = u.username COLLATE NOCASE) AS pending_email "
        f"{frm} ORDER BY {order} LIMIT ? OFFSET ?", args + [USER_PAGE, (page - 1) * USER_PAGE]).fetchall()
    rows = [dict(r, peek=is_owner() or (not r["is_admin"] and r["id"] != g.owner_id)) for r in rows]
    return rows, {"q": q, "filter": flt, "page": page, "pages": pages, "total": total, "filters": USER_FILTERS}


def get_setting(db, key):
    row = db.execute("SELECT value FROM site_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    markets = bundle_many(db, db.execute("SELECT * FROM markets WHERE status = 'open' ORDER BY closes_at").fetchall())
    settled = bundle_many(db, db.execute("SELECT * FROM markets WHERE status != 'open' ORDER BY resolved_at DESC").fetchall())
    users, user_view = list_users(db)
    proposals =[dict(p, outcomes=json.loads(p["outcomes"])) for p in db.execute(
        "SELECT p.*, u.username FROM proposals p JOIN users u ON u.id = p.user_id "
        "WHERE p.status = 'pending' ORDER BY p.id")]
    default_close = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
    review_counts = {r["status"]: r["n"] for r in db.execute(
        "SELECT status, COUNT(*) AS n FROM user_risk WHERE status IN ('held', 'flagged') GROUP BY status")}
    return render_template("admin.html", markets=markets, settled=settled, users=users, uv=user_view,
                           proposals=proposals, default_close=default_close, review_counts=review_counts,
                           announcement=get_setting(db, "announcement"))


def read_market_form(f):
    """Validate a create/propose/review form. Returns (fields, error_message)."""
    question = f.get("question", "").strip()
    # "yesno" is the standard market; "overunder" bets on a number vs. a line;
    # "multi" (or no kind at all) takes the outcome list as typed.
    kind = f.get("kind")
    ou_line, ou_unit = None, ""
    if kind == "overunder":
        ou_line, ou_unit, error = parse_ou(f)
        if error:
            return None, error
        labels = ou_labels(ou_line, ou_unit)
    else:
        labels = ["Yes", "No"] if kind == "yesno" else parse_outcome_lines(f.get("outcomes", ""))
    description = f.get("description", "").strip()
    try:
        closes_at = datetime.fromisoformat(f.get("closes_at", ""))
    except ValueError:
        closes_at = None
    if not question or len(question) > 200:
        return None, "Question is required (max 200 characters)."
    if not 2 <= len(labels) <= 8:
        return None, "Give 2–8 distinct outcomes, one per line."
    if len(description) < MIN_RULES_LENGTH:
        return None, (f"Say exactly how the result will be decided "
                      f"(at least {MIN_RULES_LENGTH} characters in the first rules box).")
    if closes_at is None or closes_at <= datetime.now():
        return None, "Pick a closing time in the future."
    return {
        "question": question,
        "description": description[:2000],
        "rules_source": f.get("rules_source", "").strip()[:300],
        "rules_otherwise": f.get("rules_otherwise", "").strip()[:1000],
        "category": f.get("category") if f.get("category") in CATEGORIES else "Other",
        "closes_at": closes_at.strftime("%Y-%m-%d %H:%M:%S"),
        "labels": labels,
        "b": market_b(len(labels)),
        "ou_line": ou_line,
        "ou_unit": ou_unit,
    }, None


def read_stake(f, labels):
    """The creator's opening bet from a form. Returns (outcome_index, amount, error)."""
    try:
        amount = cents(float(str(f.get("stake", "")).replace(",", "").strip()))
        if not math.isfinite(amount):
            raise ValueError
    except ValueError:
        return None, 0.0, f"Enter your opening bet (at least Ⓜ{CREATOR_MIN_BET:,.0f})."
    if amount < CREATOR_MIN_BET:
        return None, 0.0, f"Your opening bet must be at least Ⓜ{CREATOR_MIN_BET:,.0f}."
    try:
        pick = int(f.get("stake_pick", ""))
    except ValueError:
        pick = -1
    if not 0 <= pick < len(labels):
        return None, 0.0, "Pick which outcome your opening bet is on."
    return pick, amount, None


def place_creator_stake(db, mid, user_id, pick, amount):
    """The creator's opening bet: a normal trade on their own market (no entry fee, since they're the creator)."""
    outcome_id = get_outcomes(db, mid)[pick]["id"]
    return execute_trade(db, user_id, mid, outcome_id, "buy", amount)


@app.post("/admin/markets")
@admin_required
def admin_create():
    fields, error = read_market_form(request.form)
    if not error:
        pick, stake, error = read_stake(request.form, fields["labels"])
    if not error:
        error = overdue_block(get_db(), g.user["id"])
    if not error and g.user["balance"] + EPS < CREATION_FEE + stake:
        error = (f"You need Ⓜ{CREATION_FEE + stake:,.2f} (Ⓜ{CREATION_FEE:,.0f} fee + Ⓜ{stake:,.2f} opening bet) "
                 f"but have Ⓜ{g.user['balance']:,.2f}.")
    if error:
        flash(error, "error")
        return redirect(url_for("admin"))
    db = get_db()
    try:
        mid = create_market(db, fields["question"], fields["description"], fields["category"],
                            fields["closes_at"], fields["labels"], fields["b"], g.user["id"],
                            fields["rules_source"], fields["rules_otherwise"], fields["ou_line"], fields["ou_unit"],
                            charge_fee=True)
    except TradeError as e:
        flash(str(e), "error")
        return redirect(url_for("admin"))
    try:
        place_creator_stake(db, mid, g.user["id"], pick, stake)
    except TradeError as e:  # e.g. balance changed in between; the market exists, the bet didn't go through
        log_audit("market.create", "market", mid, fields["question"])
        flash(f"Market created, but your opening bet failed: {e}", "error")
        return redirect(url_for("market", mid=mid))
    log_audit("market.create", "market", mid, fields["question"])
    flash(f"Market created. You paid the Ⓜ{CREATION_FEE:,.0f} creation fee and bet Ⓜ{stake:,.2f} on "
          f"“{fields['labels'][pick]}”.", "success")
    return redirect(url_for("market", mid=mid))


# ---------------------------------------------------------------- routes: proposals

MAX_PENDING_PROPOSALS = 5


@app.route("/propose", methods=["GET", "POST"])
@login_required
def propose():
    db = get_db()
    if request.method == "POST":
        pending = db.execute("SELECT COUNT(*) FROM proposals WHERE user_id = ? AND status = 'pending'",
                             (g.user["id"],)).fetchone()[0]
        fields, error = read_market_form(request.form)
        if pending >= MAX_PENDING_PROPOSALS:
            error = f"You already have {MAX_PENDING_PROPOSALS} proposals waiting. Wait for an admin to review them."
        elif rate_limited(("propose", g.user["id"]), *PROPOSE_LIMIT):
            error = "You've sent a lot of proposals in the last hour. Try again later."
        if not error:
            pick, stake, error = read_stake(request.form, fields["labels"])
        if not error:
            error = overdue_block(db, g.user["id"])
        if not error and g.user["balance"] + EPS < CREATION_FEE + stake:
            error = (f"Proposing costs Ⓜ{CREATION_FEE:,.0f} plus your Ⓜ{stake:,.2f} opening bet "
                     f"(Ⓜ{CREATION_FEE + stake:,.2f} total). You have Ⓜ{g.user['balance']:,.2f}.")
        if error:
            flash(error, "error")
            return render_template("propose.html", form=request.form, mine=my_proposals(db))
        db.execute("BEGIN IMMEDIATE")
        try:
            charged = db.execute("UPDATE users SET balance = ROUND(balance - ?, 2) WHERE id = ? AND balance >= ?",
                                 (CREATION_FEE + stake, g.user["id"], CREATION_FEE + stake - EPS)).rowcount
            if not charged:
                raise TradeError(f"Proposing costs Ⓜ{CREATION_FEE + stake:,.2f} including your opening bet.")
            db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (stake, g.user["id"]))
            log_activity(db, g.user["id"], "fee", -CREATION_FEE, None, f"Market proposal fee: “{fields['question']}”")
            db.execute("UPDATE users SET balance = ROUND(balance - ?, 2) WHERE id = ?", (stake, g.user["id"]))
            log_activity(db, g.user["id"], "stake", -stake, None,
                         f"Opening bet held: Ⓜ{stake:,.2f} on “{fields['labels'][pick]}” for “{fields['question']}”")
            db.execute(
                "INSERT INTO proposals (user_id, question, description, category, outcomes, closes_at, created_at, "
                "rules_source, rules_otherwise, ou_line, ou_unit, fee, stake, stake_pick, stake_label) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (g.user["id"], fields["question"], fields["description"], fields["category"],
                 json.dumps(fields["labels"]), fields["closes_at"], now_str(),
                 fields["rules_source"], fields["rules_otherwise"], fields["ou_line"], fields["ou_unit"], CREATION_FEE,
                 stake, pick, fields["labels"][pick]))
            db.execute("COMMIT")
        except TradeError as e:
            db.execute("ROLLBACK")
            flash(str(e), "error")
            return render_template("propose.html", form=request.form, mine=my_proposals(db))
        except Exception:
            db.execute("ROLLBACK")
            raise
        flash(f"Thanks! Your market was sent to the admins for approval. You paid the Ⓜ{CREATION_FEE:,.0f} fee, "
              f"and your Ⓜ{stake:,.2f} opening bet is held until it's approved.", "success")
        return redirect(url_for("propose"))
    return render_template("propose.html", form={}, mine=my_proposals(db))


def match_stake_pick(p, labels):
    """Which of the final outcomes the proposer's opening bet goes on, after any edits during review.
    Same name wins; otherwise a two-outcome market keeps the same side (Yes/Over first, No/Under second)."""
    lowered = [x.lower() for x in labels]
    if p["stake_label"].lower() in lowered:
        return lowered.index(p["stake_label"].lower())
    if len(labels) == 2 and len(json.loads(p["outcomes"])) == 2 and p["stake_pick"] in (0, 1):
        return p["stake_pick"]
    return None


def release_stake(db, p, reason):
    """Give a proposal's held opening bet back to the proposer. Must run inside a transaction."""
    if p["stake"] > 0:
        db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (p["stake"], p["user_id"]))
        log_activity(db, p["user_id"], "stake", p["stake"], None, f"{reason}: “{p['question']}”")


def my_proposals(db):
    return [dict(p, outcomes=json.loads(p["outcomes"])) for p in db.execute(
        "SELECT * FROM proposals WHERE user_id = ? ORDER BY id DESC LIMIT 20", (g.user["id"],))]


@app.post("/propose/<int:pid>/withdraw")
@login_required
def withdraw_proposal(pid):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    p = db.execute("SELECT * FROM proposals WHERE id = ? AND user_id = ? AND status = 'pending'",
                   (pid, g.user["id"])).fetchone()
    if p:
        db.execute("DELETE FROM proposals WHERE id = ?", (pid,))
        if p["fee"] > 0:
            db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (p["fee"], g.user["id"]))
            log_activity(db, g.user["id"], "refund", p["fee"], None, f"Proposal withdrawn, fee refunded: “{p['question']}”")
        release_stake(db, p, "Proposal withdrawn, opening bet refunded")
    db.execute("COMMIT")
    if p:
        back = p["fee"] + p["stake"]
        flash("Proposal withdrawn." + (f" Ⓜ{back:,.2f} was refunded." if back > 0 else ""), "success")
    else:
        flash("That proposal can't be withdrawn.", "error")
    return redirect(url_for("propose"))


@app.route("/admin/proposals/<int:pid>", methods=["GET", "POST"])
@admin_required
def admin_review(pid):
    """Review a proposal: the admin can tweak any field, then approve (creates the market) or reject."""
    db = get_db()
    p = db.execute("SELECT p.*, u.username FROM proposals p JOIN users u ON u.id = p.user_id WHERE p.id = ?",
                   (pid,)).fetchone()
    if p is None:
        abort(404)
    if request.method == "POST":
        if p["status"] != "pending":
            flash("This proposal was already reviewed.", "error")
            return redirect(url_for("admin"))
        note = request.form.get("review_note", "").strip()[:500]
        if request.form.get("decision") == "reject":
            db.execute("BEGIN IMMEDIATE")
            done = db.execute("UPDATE proposals SET status = 'rejected', review_note = ?, reviewed_by = ?, "
                              "reviewed_at = ? WHERE id = ? AND status = 'pending'",
                              (note, g.user["id"], now_str(), pid)).rowcount
            if done:
                release_stake(db, p, "Proposal rejected, opening bet refunded")
            db.execute("COMMIT")
            if done:
                log_audit("proposal.reject", "proposal", pid, p["question"], note)
            flash("Proposal rejected." + (f" {p['username']}'s Ⓜ{p['stake']:,.2f} opening bet was refunded; "
                                          f"the Ⓜ{p['fee']:,.0f} fee is kept." if done and p["stake"] else ""),
                  "success")
            return redirect(url_for("admin") + "#proposals")
        fields, error = read_market_form(request.form)
        stake_pick = None
        if not error and p["stake"] > 0:
            stake_pick = match_stake_pick(p, fields["labels"])
            if stake_pick is None:
                error = (f"{p['username']}'s opening bet is on “{p['stake_label']}”, which isn't one of the outcomes "
                         "any more. Keep that outcome, or reject the proposal.")
        if error:
            flash(error, "error")
            return render_template("review_proposal.html", p=p, form=request.form)
        claimed = db.execute("UPDATE proposals SET status = 'approved', review_note = ?, reviewed_by = ?, "
                             "reviewed_at = ? WHERE id = ? AND status = 'pending'",
                             (note, g.user["id"], now_str(), pid)).rowcount
        if not claimed:  # another admin got there first
            flash("This proposal was already reviewed.", "error")
            return redirect(url_for("admin"))
        mid = create_market(db, fields["question"], fields["description"], fields["category"],
                            fields["closes_at"], fields["labels"], fields["b"], p["user_id"],
                            fields["rules_source"], fields["rules_otherwise"], fields["ou_line"], fields["ou_unit"])
        db.execute("UPDATE proposals SET market_id = ? WHERE id = ?", (mid, pid))
        bet_note = ""
        if p["stake"] > 0:
            db.execute("BEGIN IMMEDIATE")
            release_stake(db, p, "Opening bet released to place it")
            db.execute("COMMIT")
            try:
                place_creator_stake(db, mid, p["user_id"], stake_pick, p["stake"])
                bet_note = f" {p['username']}'s Ⓜ{p['stake']:,.2f} opening bet on “{fields['labels'][stake_pick]}” is placed."
            except TradeError as e:  # e.g. the proposer was banned meanwhile; they keep the refunded stake
                bet_note = f" {p['username']}'s opening bet couldn't be placed ({e}), so it was refunded."
        log_audit("proposal.approve", "proposal", pid, fields["question"],
                  f"By {p['username']}; market #{mid}." + (f" Note: {note}" if note else ""))
        flash(f"Approved! The market is now live. (Proposed by {p['username']}.){bet_note}", "success")
        return redirect(url_for("market", mid=mid))

    form = {"question": p["question"], "description": p["description"], "category": p["category"],
            "rules_source": p["rules_source"], "rules_otherwise": p["rules_otherwise"],
            "outcomes": "\n".join(json.loads(p["outcomes"])),
            "closes_at": parse_dt(p["closes_at"]).strftime("%Y-%m-%dT%H:%M")}
    if p["ou_line"] is not None:
        form.update(kind="overunder", ou_line=fmt_number(p["ou_line"]).replace(",", ""), ou_unit=p["ou_unit"])
    return render_template("review_proposal.html", p=p, form=form)


@app.post("/admin/markets/<int:mid>/propose-result")
@admin_required
def admin_propose_result(mid):
    db = get_db()
    m = db.execute("SELECT * FROM markets WHERE id = ?", (mid,)).fetchone()
    if m is None:
        abort(404)
    try:
        actual = None
        if m["ou_line"] is not None:
            # Over/Under: the admin enters the measured number and the winner follows from the line.
            try:
                actual = float(request.form.get("actual", "").replace(",", "").strip())
                if not math.isfinite(actual):
                    raise ValueError
            except ValueError:
                raise TradeError("Enter the actual number, like 87.3.")
            if actual == m["ou_line"]:
                raise TradeError(f"{ou_text(actual, m['ou_unit'])} is exactly on the line, so neither side wins "
                                 "(a “push”). Use Cancel & refund to give everyone their money back.")
            over, under = get_outcomes(db, mid)
            outcome_id = over["id"] if actual > m["ou_line"] else under["id"]
        else:
            outcome_id = int(request.form.get("outcome_id", 0))
        propose_result(db, mid, outcome_id, g.user["id"])
        if actual is not None:
            db.execute("UPDATE markets SET ou_result = ? WHERE id = ?", (actual, mid))
        label = next((o["label"] for o in get_outcomes(db, mid) if o["id"] == outcome_id), "")
        log_audit("result.propose", "market", mid, m["question"], f"Proposed “{label}”.")
        flash(f"Result proposed. It finalizes in {DISPUTE_HOURS} hours unless someone disputes it.", "success")
    except (TradeError, ValueError) as e:
        flash(str(e) or "Pick a winning outcome.", "error")
    return redirect(referrer_or(url_for("market", mid=mid)))


def convert_to_over_under(db, market_id, line, unit, yes_means, admin_id):
    """Turn an open Yes/No market into Over/Under. Bets carry over: YES holders get the side that YES meant.
    Over becomes outcome 0 (swapping the price history to match if needed) and a dated clarification
    records the change."""
    db.execute("BEGIN IMMEDIATE")
    try:
        m = db.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
        if m is None or m["status"] != "open" or m["proposed_outcome_id"] is not None:
            raise TradeError("Only open markets without a proposed result can be converted.")
        if m["ou_line"] is not None:
            raise TradeError("This market is already Over/Under.")
        outs = get_outcomes(db, market_id)
        if not is_binary([o["label"] for o in outs]):
            raise TradeError("Only Yes/No markets can be converted to Over/Under.")
        yes, no = outs
        over, under = (yes, no) if yes_means == "over" else (no, yes)
        over_label, under_label = ou_labels(line, unit)
        db.execute("UPDATE outcomes SET label = ?, position = 0 WHERE id = ?", (over_label, over["id"]))
        db.execute("UPDATE outcomes SET label = ?, position = 1 WHERE id = ?", (under_label, under["id"]))
        if over["id"] != yes["id"]:  # outcome order flipped, so flip the stored price history too
            for r in db.execute("SELECT id, prices FROM price_history WHERE market_id = ?", (market_id,)).fetchall():
                db.execute("UPDATE price_history SET prices = ? WHERE id = ?",
                           (json.dumps(json.loads(r["prices"])[::-1]), r["id"]))
        db.execute("UPDATE markets SET ou_line = ?, ou_unit = ? WHERE id = ?", (line, unit, market_id))
        text = ou_text(line, unit)
        note = (f"Converted to Over/Under {text}. YES bets became {over_label if yes_means == 'over' else under_label}"
                f" and NO bets became {under_label if yes_means == 'over' else over_label}. Over wins if the result is "
                f"above {text}, Under if below. Exactly {text} is a push and everyone is refunded.")
        db.execute("INSERT INTO clarifications (market_id, admin_id, body, created_at) VALUES (?, ?, ?, ?)",
                   (market_id, admin_id, note, now_str()))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


@app.post("/admin/markets/<int:mid>/convert-ou")
@admin_required
def admin_convert_ou(mid):
    line, unit, error = parse_ou(request.form)
    yes_means = request.form.get("yes_means")
    if not error and yes_means not in ("over", "under"):
        error = "Choose whether YES meant Over or Under."
    if error:
        flash(error, "error")
    else:
        try:
            convert_to_over_under(get_db(), mid, line, unit, yes_means, g.user["id"])
            log_audit("market.convert_ou", "market", mid, market_label(get_db(), mid),
                      f"Line {ou_text(line, unit)}; YES meant {yes_means}.")
            flash(f"Converted to Over/Under {ou_text(line, unit)}. Everyone's bets carried over.", "success")
        except TradeError as e:
            flash(str(e), "error")
    return redirect(url_for("market", mid=mid))


@app.post("/admin/markets/<int:mid>/result-action")
@admin_required
def admin_result_action(mid):
    """Confirm a disputed result right away, or take a proposed result back."""
    db = get_db()
    m = db.execute("SELECT * FROM markets WHERE id = ?", (mid,)).fetchone()
    if m is None:
        abort(404)
    try:
        if request.form.get("decision") == "retract":
            retract_result(db, mid)
            log_audit("result.retract", "market", mid, m["question"])
            flash("Proposed result withdrawn. You can propose a different one.", "success")
        elif request.form.get("decision") == "confirm":
            if m["proposed_outcome_id"] is None or not open_disputes(db, mid):
                raise TradeError("Only a disputed result needs confirming; others finalize on their own.")
            n = resolve_market(db, mid, m["proposed_outcome_id"], g.user["id"])
            log_audit("result.confirm", "market", mid, m["question"], f"Paid {n} winning position(s).")
            flash(f"Result confirmed. Paid out {n} winning position(s).", "success")
        else:
            raise TradeError("Unknown action.")
    except TradeError as e:
        flash(str(e), "error")
    return redirect(url_for("market", mid=mid))


@app.post("/admin/markets/<int:mid>/clarify")
@admin_required
def admin_clarify(mid):
    db = get_db()
    if db.execute("SELECT 1 FROM markets WHERE id = ?", (mid,)).fetchone() is None:
        abort(404)
    body = request.form.get("body", "").strip()
    if not 5 <= len(body) <= 500:
        flash("A clarification needs 5–500 characters.", "error")
    else:
        db.execute("INSERT INTO clarifications (market_id, admin_id, body, created_at) VALUES (?, ?, ?, ?)",
                   (mid, g.user["id"], body, now_str()))
        log_audit("market.clarify", "market", mid, market_label(db, mid), body)
        flash("Clarification added to the rules.", "success")
    return redirect(referrer_or(url_for("market", mid=mid)))


@app.post("/admin/markets/<int:mid>/cancel")
@admin_required
def admin_cancel(mid):
    try:
        label = market_label(get_db(), mid)
        cancel_market(get_db(), mid)
        log_audit("market.cancel", "market", mid, label)
        flash("Market cancelled and bets refunded.", "success")
    except TradeError as e:
        flash(str(e), "error")
    return redirect(referrer_or(url_for("admin")))


def parse_outcome_lines(text):
    labels = []
    for line in text.splitlines():
        line = line.strip()[:60]
        if line and line.lower() not in [x.lower() for x in labels]:
            labels.append(line)
    return labels


@app.route("/admin/markets/<int:mid>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit(mid):
    """Edit a market. Text, category and closing time can always change. Once anyone has bet,
    outcomes can only be renamed (not added/removed) and liquidity is locked, so prices and payouts stay fair."""
    db = get_db()
    m = db.execute("SELECT * FROM markets WHERE id = ?", (mid,)).fetchone()
    if m is None:
        abort(404)
    outs = get_outcomes(db, mid)
    has_trades = db.execute("SELECT 1 FROM trades WHERE market_id = ? LIMIT 1", (mid,)).fetchone() is not None
    # The written rules lock once someone other than the creator has bet (the creator's own opening bet doesn't
    # count). Outcomes and the line lock once anyone has bet, since shares are tied to them.
    rules_locked = db.execute("SELECT 1 FROM trades WHERE market_id = ? AND user_id IS NOT ? LIMIT 1",
                              (mid, m["created_by"])).fetchone() is not None

    if request.method == "POST":
        f = request.form
        question = f.get("question", "").strip()
        try:
            closes_at = datetime.fromisoformat(f.get("closes_at", "")).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            closes_at = None
        is_ou = m["ou_line"] is not None
        ou_line, ou_unit, error = m["ou_line"], m["ou_unit"], None
        if is_ou and has_trades:  # the line is locked once anyone has bet; labels stay as they are
            renames = {o["id"]: o["label"] for o in outs}
            labels = list(renames.values())
        elif is_ou:
            ou_line, ou_unit, error = parse_ou(f)
            labels = ou_labels(ou_line, ou_unit) if not error else []
        elif has_trades:
            renames = {o["id"]: f.get(f"label_{o['id']}", "").strip()[:60] for o in outs}
            labels = list(renames.values())
        else:
            labels = parse_outcome_lines(f.get("outcomes", ""))
        if error:
            pass
        elif not question or len(question) > 200:
            error = "Question is required (max 200 characters)."
        elif closes_at is None:
            error = "Pick a valid closing time."
        elif (m["proposed_outcome_id"] is not None and m["status"] == "open"
              and datetime.fromisoformat(closes_at) > datetime.now()):
            error = "A result is proposed for this market. Withdraw it before moving the closing time later."
        elif not all(labels) or len({x.lower() for x in labels}) != len(labels):
            error = "Outcome names must be filled in and different from each other."
        elif not 2 <= len(labels) <= 8:
            error = "Give 2–8 distinct outcomes, one per line."
        if error:
            flash(error, "error")
            return redirect(url_for("admin_edit", mid=mid))

        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("UPDATE markets SET question = ?, category = ?, closes_at = ? WHERE id = ?",
                       (question, f.get("category") if f.get("category") in CATEGORIES else "Other", closes_at, mid))
            if not rules_locked:  # once others have bet the written rules are locked; use a clarification instead
                db.execute("UPDATE markets SET description = ?, rules_source = ?, rules_otherwise = ? WHERE id = ?",
                           (f.get("description", "").strip()[:2000], f.get("rules_source", "").strip()[:300],
                            f.get("rules_otherwise", "").strip()[:1000], mid))
            if has_trades:
                for oid, label in renames.items():
                    db.execute("UPDATE outcomes SET label = ? WHERE id = ?", (label, oid))
            else:
                b = market_b(len(labels))  # outcome count may have changed
                db.execute("UPDATE markets SET b = ?, ou_line = ?, ou_unit = ? WHERE id = ?",
                           (b, ou_line, ou_unit if is_ou else "", mid))
                if [o["label"] for o in outs] != labels:
                    db.execute("DELETE FROM outcomes WHERE market_id = ?", (mid,))
                    for i, label in enumerate(labels):
                        db.execute("INSERT INTO outcomes (market_id, label, position) VALUES (?, ?, ?)", (mid, label, i))
                if b != m["b"] or [o["label"] for o in outs] != labels:
                    db.execute("DELETE FROM price_history WHERE market_id = ?", (mid,))
                    snapshot_prices(db, mid, b)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        log_audit("market.edit", "market", mid, question)
        flash("Market updated.", "success")
        return redirect(url_for("market", mid=mid))

    clarifications = db.execute("SELECT * FROM clarifications WHERE market_id = ? ORDER BY id", (mid,)).fetchall()
    ou_line_value = "" if m["ou_line"] is None else fmt_number(m["ou_line"]).replace(",", "")
    return render_template("edit_market.html", m=m, outcomes=outs, has_trades=has_trades, rules_locked=rules_locked,
                           clarifications=clarifications, ou_line_value=ou_line_value,
                           closes_value=parse_dt(m["closes_at"]).strftime("%Y-%m-%dT%H:%M"))


@app.post("/admin/markets/<int:mid>/delete")
@admin_required
def admin_delete(mid):
    try:
        row = get_db().execute("SELECT question, status FROM markets WHERE id = ?", (mid,)).fetchone()
        delete_market(get_db(), mid)
        log_audit("market.delete", "market", mid, row["question"], f"Status was {row['status']}.")
        flash("Market deleted.", "success")
    except TradeError as e:
        flash(str(e), "error")
    return redirect(url_for("admin"))


@app.post("/admin/markets/<int:mid>/block")
@admin_required
def admin_block(mid):
    db = get_db()
    if db.execute("SELECT 1 FROM markets WHERE id = ?", (mid,)).fetchone() is None:
        abort(404)
    name = request.form.get("username", "").strip()
    u = db.execute("SELECT id, username FROM users WHERE username = ?", (name,)).fetchone()
    if u is None:
        flash(f"No user named “{name}”.", "error")
    elif not can_manage(u["id"]):
        flash(NOT_ALLOWED, "error")
    else:
        refund = block_user_in_market(db, u["id"], mid)
        log_audit("market.block", "user", u["id"], u["username"],
                  f"Market #{mid}." + (f" Refunded Ⓜ{refund:,.2f}." if refund else ""))
        flash(f"{u['username']} can no longer bet on this market."
              + (f" Refunded Ⓜ{refund:,.2f} and removed their bets." if refund else ""), "success")
    return redirect(url_for("market", mid=mid) + "#admin")


@app.post("/admin/trades/<int:tid>/cancel")
@admin_required
def admin_cancel_trade(tid):
    db = get_db()
    t = db.execute("SELECT t.user_id, t.market_id, u.username FROM trades t JOIN users u ON u.id = t.user_id "
                   "WHERE t.id = ?", (tid,)).fetchone()
    if t is None:
        flash("That trade no longer exists.", "error")
        return back(url_for("admin"))
    mid = t["market_id"]
    if t["user_id"] != g.user["id"] and not can_manage(t["user_id"]):
        flash(NOT_ALLOWED, "error")
        return redirect(url_for("market", mid=mid) + "#trades")
    try:
        refund, row = cancel_trade(db, tid, "Refund (purchase cancelled by an admin)")
    except TradeError as e:
        flash(str(e), "error")
        return redirect(url_for("market", mid=mid) + "#trades")
    log_audit("market.cancel_trade", "user", t["user_id"], t["username"],
              f"Market #{mid}, trade #{tid}: {row['shares']:,.2f} × {row['label']}. Refunded Ⓜ{refund:,.2f}.")
    flash(f"Cancelled {t['username']}'s purchase of {row['shares']:,.2f} × {row['label']} "
          f"and refunded Ⓜ{refund:,.2f}.", "success")
    return redirect(url_for("market", mid=mid) + "#trades")


@app.post("/admin/markets/<int:mid>/unblock/<int:uid>")
@admin_required
def admin_unblock(mid, uid):
    db = get_db()
    u = db.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()
    if u is not None and not can_manage(uid):
        flash(NOT_ALLOWED, "error")
        return redirect(url_for("market", mid=mid) + "#admin")
    if db.execute("DELETE FROM market_blocks WHERE market_id = ? AND user_id = ?", (mid, uid)).rowcount and u:
        log_audit("market.unblock", "user", uid, u["username"], f"Market #{mid}.")
    flash("They can bet on this market again.", "success")
    return redirect(url_for("market", mid=mid) + "#admin")


@app.post("/admin/users/<int:uid>/trading")
@admin_required
def admin_trading(uid):
    db = get_db()
    u = db.execute("SELECT id, username FROM users WHERE id = ?", (uid,)).fetchone()
    if u is None:
        abort(404)
    if not can_manage(uid):
        flash(NOT_ALLOWED, "error")
    elif uid == g.owner_id:
        flash("The owner can't be banned from trading.", "error")
    elif db.execute("SELECT 1 FROM trade_bans WHERE user_id = ?", (uid,)).fetchone():
        db.execute("DELETE FROM trade_bans WHERE user_id = ?", (uid,))
        log_audit("user.unban", "user", uid, u["username"])
        flash(f"{u['username']} can trade again.", "success")
    else:
        refund = ban_user_from_trading(db, uid)
        log_audit("user.ban", "user", uid, u["username"], f"Refunded Ⓜ{refund:,.2f} from open bets.")
        flash(f"Trading turned off for {u['username']}."
              + (f" Refunded Ⓜ{refund:,.2f} from their open bets." if refund else ""), "success")
    return back(url_for("admin") + "#users")


@app.post("/admin/users/<int:uid>/grant")
@admin_required
def admin_grant(uid):
    try:
        amount = cents(float(request.form.get("amount", "")))
        if not math.isfinite(amount) or amount == 0 or abs(amount) > 100000:
            raise ValueError
    except ValueError:
        flash("Enter an amount between -100,000 and 100,000.", "error")
        return back(url_for("admin") + "#users")
    if not can_manage(uid):
        flash(NOT_ALLOWED, "error")
        return back(url_for("admin") + "#users")
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    before = db.execute("SELECT balance FROM users WHERE id = ?", (uid,)).fetchone()
    if before is None:
        db.execute("ROLLBACK")
        abort(404)
    db.execute("UPDATE users SET balance = MAX(0, ROUND(balance + ?, 2)) WHERE id = ?", (amount, uid))
    after = db.execute("SELECT balance FROM users WHERE id = ?", (uid,)).fetchone()[0]
    change = cents(after - before["balance"])  # can be less than asked if it would have gone below zero
    log_activity(db, uid, "admin", change, None, f"Balance adjusted by an admin ({g.user['username']})")
    db.execute("COMMIT")
    target = db.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()["username"]
    log_audit("user.grant", "user", uid, target, f"{'+' if change >= 0 else '−'}Ⓜ{abs(change):,.2f}")
    flash(f"Adjusted balance by {change:,.2f}.", "success")
    return back(url_for("admin") + "#users")


@app.post("/admin/users/<int:uid>/toggle-admin")
@admin_required
def admin_toggle(uid):
    if not is_owner():
        flash("Only the site owner can add or remove admins.", "error")
    elif uid == g.user["id"]:
        flash("You can't remove your own admin access.", "error")
    else:
        db = get_db()
        u = db.execute("SELECT username, is_admin FROM users WHERE id = ?", (uid,)).fetchone()
        if u is None:
            abort(404)
        db.execute("UPDATE users SET is_admin = 1 - is_admin WHERE id = ?", (uid,))
        log_audit("user.admin_off" if u["is_admin"] else "user.admin_on", "user", uid, u["username"])
    return back(url_for("admin") + "#users")


def delete_account(db, user_id):
    """Remove an account. Their bets in unresolved markets are reversed and refunded first (so prices go back
    to what they'd have been); payouts from settled markets stay with whoever got them. Returns the amount
    reversed. Raises TradeError if the account can't be deleted safely."""
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("SELECT 1 FROM disputes WHERE user_id = ? AND handled_at IS NULL", (user_id,)).fetchone():
            raise TradeError("They have an open dispute on a proposed result. Confirm or change that result "
                             "first, then delete the account.")
        running = db.execute("SELECT id FROM markets WHERE created_by = ? AND status = 'open' ORDER BY id LIMIT 1",
                             (user_id,)).fetchone()
        if running:
            raise TradeError(f"They created market #{running['id']}, which is still running. "
                             "Resolve or cancel it first.")
        if db.execute("SELECT 1 FROM loans WHERE status = 'active' AND (lender_id = ? OR borrower_id = ?)",
                      (user_id, user_id)).fetchone():
            raise TradeError("They have a loan that hasn't been paid back yet (as lender or borrower). "
                             "Wait until it's repaid, then delete the account.")
        for l in db.execute("SELECT * FROM loans WHERE status = 'offered' AND (lender_id = ? OR borrower_id = ?)",
                            (user_id, user_id)).fetchall():
            close_loan_offer(db, l, "cancelled", "Loan offer cancelled (account deleted)")
        db.execute("DELETE FROM loans WHERE lender_id = ? OR borrower_id = ?", (user_id, user_id))
        reversed_total = 0.0
        for r in db.execute("SELECT DISTINCT market_id FROM trades WHERE user_id = ? ORDER BY market_id",
                            (user_id,)).fetchall():
            reversed_total += refund_user_in_market(db, user_id, r["market_id"], "Refund (account deleted)")
        for table in ("limit_orders", "positions", "trades", "comments", "disputes", "market_blocks", "market_entries",
                      "activity", "comment_mutes", "trade_bans", "proposals"):
            db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM market_entries WHERE creator_id = ?", (user_id,))
        db.execute("UPDATE proposals SET reviewed_by = NULL WHERE reviewed_by = ?", (user_id,))
        db.execute("UPDATE markets SET created_by = NULL WHERE created_by = ?", (user_id,))
        db.execute("UPDATE markets SET proposed_by = NULL WHERE proposed_by = ?", (user_id,))
        db.execute("UPDATE markets SET resolved_by = NULL WHERE resolved_by = ?", (user_id,))
        db.execute("UPDATE clarifications SET admin_id = NULL WHERE admin_id = ?", (user_id,))
        # their emails stay blocked (so the same inbox can't make a fresh account) until an admin releases them
        gone = db.execute("SELECT username, email_canonical FROM users WHERE id = ?", (user_id,)).fetchone()
        db.execute("UPDATE email_ledger SET status = 'deleted' WHERE status = 'active' "
                   "AND (first_username = ? COLLATE NOCASE OR canonical = ?)", (gone["username"], gone["email_canonical"]))
        db.execute("DELETE FROM user_risk WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM user_credit WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM credit_history WHERE user_id = ?", (user_id,))
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'credit_events'").fetchone():
            db.execute("DELETE FROM credit_events WHERE user_id = ?", (user_id,))  # left over from the older credit system
        db.execute("DELETE FROM login_challenges WHERE username = ? COLLATE NOCASE", (gone["username"],))
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        db.execute("COMMIT")
        return cents(reversed_total)
    except Exception:
        db.execute("ROLLBACK")
        raise


@app.get("/admin/users/<int:uid>")
@admin_required
def admin_user(uid):
    db = get_db()
    u = db.execute("SELECT u.*, (tb.user_id IS NOT NULL) AS banned, (cm.user_id IS NOT NULL) AS muted "
                   "FROM users u LEFT JOIN trade_bans tb ON tb.user_id = u.id "
                   "LEFT JOIN comment_mutes cm ON cm.user_id = u.id WHERE u.id = ?", (uid,)).fetchone()
    if u is None:
        abort(404)
    manageable = can_manage(uid)
    return render_template("admin_user.html", u=u, manageable=manageable, me=u["id"] == g.user["id"],
                           is_target_owner=u["id"] == g.owner_id,
                           ident=user_identity(db, u) if manageable else None, catalog=eventlog.EVENTS,
                           **admin_user_credit(db, u, manageable))


def admin_user_credit(db, u, manageable):
    """The credit card on a user's admin page: the stored score, its history and their loans on both sides."""
    if not manageable or u["is_admin"] or u["id"] == g.owner_id:
        return {"cred": None}
    c = get_credit(db, u["id"], max_age=60)
    hist, points = credit_history_points(db, u["id"])
    loans_ = db.execute("SELECT l.*, lu.username AS lender, bu.username AS borrower FROM loans l "
                        "JOIN users lu ON lu.id = l.lender_id LEFT JOIN users bu ON bu.id = l.borrower_id "
                        "WHERE (l.borrower_id = ? OR l.lender_id = ?) AND l.status IN ('active', 'repaid', 'forgiven') "
                        "ORDER BY l.id DESC LIMIT 15", (u["id"], u["id"])).fetchall()
    return {"cred": c, "cred_detail": credit_limit_detail(db, u["id"], credit_row=c), "cred_history": hist,
            "cred_points": points, "cred_loans": loans_, "CREDIT_MAX_ADJUST": credit_config(db)["max_adjust"]}


def user_action_error(db, uid, action):
    """The one set of rules for who an admin may act on. Returns (user row or None, reason it's refused or None).
    Used by the single-user pages and by bulk actions, so a bulk action can never do what the page wouldn't."""
    u = db.execute("SELECT id, username, is_admin FROM users WHERE id = ?", (uid,)).fetchone()
    if u is None:
        return None, "no such account"
    if uid == g.user["id"]:
        return u, "You can't delete your own account." if action == "delete" else "You can't do that to your own account."
    if uid == g.owner_id:
        return u, "The owner's account can't be deleted." if action == "delete" else "Nobody can do that to the owner."
    if not can_manage(uid):
        return u, NOT_ALLOWED
    if u["is_admin"] and action == "delete":
        return u, "Remove their admin role before deleting the account."
    if u["is_admin"] and action in ("hold", "approve", "rescan"):
        return u, "Admins aren't scored or held."
    return u, None


@app.post("/admin/users/<int:uid>/delete")
@admin_required
def admin_delete_user(uid):
    db = get_db()
    u, error = user_action_error(db, uid, "delete")
    if u is None:
        abort(404)
    if error:
        flash(error, "error")
    else:
        try:
            amount = delete_account(db, uid)
        except TradeError as e:
            flash(str(e), "error")
            return redirect(url_for("admin_user", uid=uid))
        log_audit("user.delete", "user", uid, u["username"], f"Open bets reversed (Ⓜ{amount:,.2f}).")
        flash(f"Deleted {u['username']}'s account." + (f" Reversed Ⓜ{amount:,.2f} of open bets." if amount else ""),
              "success")
        return redirect(url_for("admin") + "#users")
    return redirect(url_for("admin_user", uid=uid))


def _moderated_comment(cid):
    """Load a comment for moderation, or None (with a flash) if it's gone or the admin may not act on it."""
    c = get_db().execute("SELECT c.*, u.username FROM comments c JOIN users u ON u.id = c.user_id WHERE c.id = ?",
                         (cid,)).fetchone()
    if c is None:
        flash("That comment no longer exists.", "error")
    elif c["user_id"] != g.user["id"] and not can_manage(c["user_id"]):
        flash(NOT_ALLOWED, "error")
        return None
    return c


def _comment_detail(c):
    body = c["body"] if len(c["body"]) <= 80 else c["body"][:80] + "…"
    return f"Market #{c['market_id']}: “{body}”"


@app.post("/admin/comments/<int:cid>/hide")
@admin_required
def admin_hide_comment(cid):
    c = _moderated_comment(cid)
    if c is None:
        return redirect(url_for("index"))
    hide = not c["hidden"]
    get_db().execute("UPDATE comments SET hidden = ? WHERE id = ?", (int(hide), cid))
    log_audit("comment.hide" if hide else "comment.unhide", "comment", cid, c["username"], _comment_detail(c))
    flash("Comment hidden." if hide else "Comment restored.", "success")
    return redirect(url_for("market", mid=c["market_id"]) + "#discussion")


@app.post("/admin/comments/<int:cid>/delete")
@admin_required
def admin_delete_comment(cid):
    c = _moderated_comment(cid)
    if c:
        get_db().execute("DELETE FROM comments WHERE id = ?", (cid,))
        log_audit("comment.delete", "comment", cid, c["username"], _comment_detail(c))
        flash("Comment deleted.", "success")
        return redirect(url_for("market", mid=c["market_id"]) + "#discussion")
    return redirect(url_for("index"))


@app.post("/admin/markets/<int:mid>/close")
@admin_required
def admin_close_early(mid):
    db = get_db()
    m = db.execute("SELECT * FROM markets WHERE id = ?", (mid,)).fetchone()
    if m is None:
        abort(404)
    if m["status"] != "open":
        flash("Only unresolved markets can be closed.", "error")
    elif market_state(m) != "open":
        flash("Betting is already closed on this market.", "error")
    else:
        db.execute("UPDATE markets SET closes_at = ? WHERE id = ?", (now_str(), mid))
        log_audit("market.close_early", "market", mid, m["question"])
        flash("Betting closed. You can propose a result now.", "success")
    return redirect(url_for("market", mid=mid))


@app.post("/admin/markets/<int:mid>/reopen")
@admin_required
def admin_reopen(mid):
    db = get_db()
    m = db.execute("SELECT * FROM markets WHERE id = ?", (mid,)).fetchone()
    if m is None:
        abort(404)
    try:
        closes = datetime.fromisoformat(request.form.get("closes_at", ""))
    except ValueError:
        closes = None
    if closes is None:
        flash("Pick a valid closing time.", "error")
    elif closes <= datetime.now():
        flash("Pick a closing time in the future.", "error")
    elif m["status"] != "open":
        flash("Only unresolved markets can be reopened.", "error")
    elif m["proposed_outcome_id"] is not None:
        flash("A result is proposed for this market. Withdraw it before reopening betting.", "error")
    elif market_state(m) != "closed":
        flash("Betting is still open on this market.", "error")
    else:
        stamp = closes.strftime("%Y-%m-%d %H:%M:%S")
        db.execute("UPDATE markets SET closes_at = ? WHERE id = ?", (stamp, mid))
        log_audit("market.reopen", "market", mid, m["question"], f"New closing time {stamp}")
        flash("Betting reopened.", "success")
    return redirect(url_for("market", mid=mid))


@app.post("/admin/markets/<int:mid>/feature")
@admin_required
def admin_feature(mid):
    db = get_db()
    m = db.execute("SELECT * FROM markets WHERE id = ?", (mid,)).fetchone()
    if m is None:
        abort(404)
    now_featured = not m["featured"]
    db.execute("UPDATE markets SET featured = ? WHERE id = ?", (int(now_featured), mid))
    log_audit("market.feature" if now_featured else "market.unfeature", "market", mid, m["question"])
    flash("Market featured: it now appears first on the home page." if now_featured
          else "Market removed from featured.", "success")
    return redirect(url_for("market", mid=mid))


ANNOUNCEMENT_MAX = 300


@app.post("/admin/announcement")
@admin_required
def admin_announcement():
    db = get_db()
    text = "" if request.form.get("clear") else request.form.get("text", "").strip()
    if len(text) > ANNOUNCEMENT_MAX:
        flash(f"Keep the announcement under {ANNOUNCEMENT_MAX} characters.", "error")
    elif text:
        db.execute("INSERT INTO site_settings (key, value, updated_at) VALUES ('announcement', ?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                   (text, now_str()))
        log_audit("site.announcement", "site", "announcement", "Announcement", text)
        flash("Announcement published.", "success")
    else:
        db.execute("DELETE FROM site_settings WHERE key = 'announcement'")
        log_audit("site.announcement", "site", "announcement", "Announcement", "(cleared)")
        flash("Announcement cleared.", "success")
    return redirect(url_for("admin") + "#announcement")


@app.post("/admin/users/<int:uid>/comment-mute")
@admin_required
def admin_comment_mute(uid):
    db = get_db()
    u = db.execute("SELECT id, username FROM users WHERE id = ?", (uid,)).fetchone()
    if u is None:
        abort(404)
    if not can_manage(uid):
        flash(NOT_ALLOWED, "error")
    elif uid == g.owner_id:
        flash("The owner can't be muted.", "error")
    elif db.execute("DELETE FROM comment_mutes WHERE user_id = ?", (uid,)).rowcount:
        log_audit("user.comment_unmute", "user", uid, u["username"])
        flash(f"{u['username']} can comment again.", "success")
    else:
        db.execute("INSERT INTO comment_mutes (user_id, created_at) VALUES (?, ?)", (uid, now_str()))
        log_audit("user.comment_mute", "user", uid, u["username"])
        flash(f"{u['username']} can no longer comment.", "success")
    return back(url_for("admin_user", uid=uid))


AUDIT_PAGE = 50


@app.get("/admin/loans")
@admin_required
def admin_loans():
    db = get_db()
    rows = db.execute("SELECT l.*, lu.username AS lender, bu.username AS borrower FROM loans l "
                      "JOIN users lu ON lu.id = l.lender_id LEFT JOIN users bu ON bu.id = l.borrower_id "
                      "WHERE l.status IN ('offered', 'active') ORDER BY l.status = 'offered', l.due_at, l.id").fetchall()
    loans_ = [{"l": l, "default": in_default(db, l), "overdue": l["status"] == "active" and l["due_at"] <= now_str(),
               "grace_end": loan_grace_end(db, l) if l["status"] == "active" else None} for l in rows]
    return render_template("admin_loans.html", loans=loans_, now=now_str(), LOAN_MAX_DAYS=LOAN_MAX_DAYS)


def _admin_loan_action(lid, action):
    """Run `action(db, loan)` inside a transaction on a loan that's offered or running; it returns (audit key, detail)."""
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        loan = db.execute("SELECT l.*, lu.username AS lender, bu.username AS borrower FROM loans l "
                          "JOIN users lu ON lu.id = l.lender_id LEFT JOIN users bu ON bu.id = l.borrower_id "
                          "WHERE l.id = ?", (lid,)).fetchone()
        if loan is None or loan["status"] not in ("offered", "active"):
            raise TradeError("That loan isn't open any more.")
        result = action(db, loan)
        db.execute("COMMIT")
    except TradeError as e:
        db.execute("ROLLBACK")
        flash(str(e), "error")
        return back(url_for("admin_loans"))
    except Exception:
        db.execute("ROLLBACK")
        raise
    key, detail, message = result
    log_audit(key, "loan", lid, f"{loan['lender']} → {loan['borrower'] or 'anyone'}", detail)
    rescore_credit(db, loan["borrower_id"], loan["lender_id"], reason=key)
    flash(message, "success")
    return back(url_for("admin_loans"))


@app.post("/admin/loans/<int:lid>/forgive")
@admin_required
def admin_loan_forgive(lid):
    def do(db, loan):
        if loan["status"] != "active":
            raise TradeError("Only a running loan can be forgiven. Cancel the offer instead.")
        db.execute("UPDATE loans SET status = 'forgiven', closed_at = ? WHERE id = ?", (now_str(), lid))
        by = g.user["username"]
        log_activity(db, loan["borrower_id"], "loan", 0, None,
                     f"Loan from {loan['lender']} forgiven by an admin ({by}): Ⓜ{loan['owed']:,.2f} written off")
        log_activity(db, loan["lender_id"], "loan", 0, None,
                     f"Loan to {loan['borrower']} forgiven by an admin ({by}): Ⓜ{loan['owed']:,.2f} written off")
        return "loan.forgive", f"Ⓜ{loan['owed']:,.2f} written off", "Loan forgiven."
    return _admin_loan_action(lid, do)


@app.post("/admin/loans/<int:lid>/extend")
@admin_required
def admin_loan_extend(lid):
    try:
        days = int(request.form.get("days", ""))
        if not 1 <= days <= LOAN_MAX_DAYS:
            raise ValueError
    except ValueError:
        flash(f"Enter a number of days from 1 to {LOAN_MAX_DAYS}.", "error")
        return back(url_for("admin_loans"))

    def do(db, loan):
        if loan["status"] != "active":
            raise TradeError("Only a running loan can be extended.")
        # from the later of now and the old date, so an extension on an overdue loan gives the borrower real time
        start = max(parse_dt(loan["due_at"]), datetime.now())
        due = (start + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        db.execute("UPDATE loans SET due_at = ? WHERE id = ?", (due, lid))
        return "loan.extend", f"{days} more days, now due {due}", f"Due date moved to {due}."
    return _admin_loan_action(lid, do)


@app.post("/admin/loans/<int:lid>/cancel")
@admin_required
def admin_loan_cancel(lid):
    def do(db, loan):
        if loan["status"] != "offered":
            raise TradeError("Only an offer nobody has taken can be cancelled.")
        close_loan_offer(db, loan, "cancelled", f"Loan offer cancelled by an admin ({g.user['username']})")
        return "loan.cancel", f"Ⓜ{loan['principal']:,.2f} returned to {loan['lender']}", "Offer cancelled and refunded."
    return _admin_loan_action(lid, do)


# ---------------------------------------------------------------- routes: admin credit

CREDIT_FLAGS = {"": "Everyone", "default": "In default", "overdue": "Has an overdue loan", "thin": "New borrowers",
                "frozen": "Borrowing frozen", "adjusted": "Manually adjusted", "override": "Fixed limit",
                "held": "Held for review", "nearlimit": "Over 90% of limit"}
CREDIT_ADMIN_SORTS = {"score": "c.score DESC, u.username", "low": "c.score, u.username", "name": "u.username",
                      "use": "c.utilisation DESC", "updated": "c.updated_at DESC"}
CREDIT_PAGE = 25


def credit_form_config(form, base):
    """A credit config built from the settings form (weights as percentages), on top of `base`. Raises TradeError on
    garbage."""
    cfg = json.loads(json.dumps(base))
    try:
        cfg["weights"] = {k: float(form.get(f"w_{k}", "")) / 100 for k in cfg["weights"]}
        cfg["tiers"] = [[name, int(form.get(f"t_{i}", floor))] for i, (name, floor) in enumerate(cfg["tiers"])]
        cfg["multiplier"] = [[int(form.get(f"mx_{i}", x)), float(form.get(f"my_{i}", y))]
                             for i, (x, y) in enumerate(cfg["multiplier"])]
        for key in ("default_cap", "overdue_cap", "held_cap"):
            cfg[key] = int(form.get(key, cfg[key]))
        cfg["half_life"] = float(form.get("half_life", cfg["half_life"]))
    except ValueError:
        raise TradeError("Fill in every setting with a number.")
    if not 7 <= cfg["half_life"] <= 365:
        raise TradeError("The half-life must be between 7 and 365 days.")
    if any(not credit.MIN_SCORE <= cfg[k] <= credit.MAX_SCORE for k in ("default_cap", "overdue_cap", "held_cap")):
        raise TradeError(f"Caps must be between {credit.MIN_SCORE} and {credit.MAX_SCORE}.")
    errors = credit.validate_config(cfg)
    if errors:
        raise TradeError(" ".join(errors))
    return cfg


def recompute_all_credit(db, cfg=None, reason="recompute", dry=False):
    """Re-score every player. With dry=True nothing is stored: returns {uid: (score, tier)} under `cfg`."""
    cfg = cfg or credit_config(db)
    books = open_book_values(db)
    ids = [r["id"] for r in db.execute("SELECT id FROM users WHERE is_admin = 0 AND id != ?", (owner_id(db) or 0,))]
    out = {}
    for uid in ids:
        if dry:
            cur = db.execute("SELECT adjust FROM user_credit WHERE user_id = ?", (uid,)).fetchone()
            r = credit_compute(db, uid, cfg, books, cur["adjust"] if cur else 0)
            out[uid] = (r["score"], r["tier"])
        else:
            c = apply_credit(db, uid, reason, cfg, books)
            out[uid] = (c["score"], c["tier"]) if c else None
    return out


def _tier_counts(pairs, cfg):
    counts = {name: 0 for name, _ in cfg["tiers"]}
    for score, tier in pairs:
        if tier in counts:
            counts[tier] += 1
    return counts


def admin_credit_data(db):
    """Everything the credit console shows above the table: KPIs, tier counts and the three watchlists."""
    cfg = credit_config(db)
    now = now_str()
    scores = [r["score"] for r in db.execute("SELECT c.score FROM user_credit c JOIN users u ON u.id = c.user_id "
                                             "ORDER BY c.score")]
    n = len(scores)
    due = db.execute("SELECT * FROM loans WHERE accepted_at IS NOT NULL AND status IN ('active', 'repaid', 'forgiven') "
                     "AND due_at BETWEEN ? AND ?", ((datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
                                                     now)).fetchall()
    bad = sum(1 for l in due if l["status"] == "forgiven" or in_default(db, l))
    kpis = {
        "players": n, "avg": round(sum(scores) / n) if n else 0, "median": scores[n // 2] if n else 0,
        "outstanding": db.execute("SELECT COALESCE(SUM(owed), 0) FROM loans WHERE status = 'active'").fetchone()[0],
        "default_rate": (bad / len(due)) if due else 0.0, "due_30": len(due),
        "in_default": db.execute("SELECT COUNT(*) FROM user_credit WHERE defaulted > 0").fetchone()[0],
        "overdue": db.execute("SELECT COUNT(DISTINCT borrower_id) FROM loans WHERE status = 'active' AND due_at <= ?",
                              (now,)).fetchone()[0],
        "frozen": db.execute("SELECT COUNT(*) FROM user_credit WHERE frozen = 1").fetchone()[0],
        "thin": db.execute("SELECT COUNT(*) FROM user_credit WHERE thin = 1").fetchone()[0],
        "adjusted": db.execute("SELECT COUNT(*) FROM user_credit WHERE adjust != 0").fetchone()[0]}
    tiers = _tier_counts(((r["score"], r["tier"]) for r in db.execute("SELECT score, tier FROM user_credit")), cfg)
    # biggest drops in the last 7 days: the latest score against the last one before the window (or the first in it)
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    drops = []
    for r in db.execute("SELECT h.user_id, u.username, MIN(h.id) AS first_id, MAX(h.id) AS last_id FROM credit_history h "
                        "JOIN users u ON u.id = h.user_id WHERE h.created_at >= ? GROUP BY h.user_id", (cutoff,)):
        before = db.execute("SELECT score FROM credit_history WHERE user_id = ? AND created_at < ? ORDER BY id DESC LIMIT 1",
                            (r["user_id"], cutoff)).fetchone()
        start = before["score"] if before else db.execute("SELECT score FROM credit_history WHERE id = ?",
                                                          (r["first_id"],)).fetchone()["score"]
        end = db.execute("SELECT score, tier FROM credit_history WHERE id = ?", (r["last_id"],)).fetchone()
        if start - end["score"] >= 15:
            drops.append({"id": r["user_id"], "username": r["username"], "from": start, "to": end["score"],
                          "tier": end["tier"], "drop": start - end["score"]})
    drops = sorted(drops, key=lambda d: -d["drop"])[:10]
    # possible credit farming: a borrower whose repaid volume is mostly to lenders the alt detector links to them
    links = {}
    for r in db.execute("SELECT user_id, links FROM user_risk WHERE links != '{}'"):
        try:
            links[r["user_id"]] = {int(k) for k in json.loads(r["links"])}
        except ValueError:
            pass
    vol = {}
    for l in db.execute("SELECT borrower_id, lender_id, repaid FROM loans WHERE status = 'repaid' AND borrower_id IS NOT NULL"):
        b = l["borrower_id"]
        linked = l["lender_id"] in links.get(b, set()) or b in links.get(l["lender_id"], set())
        t, k = vol.get(b, (0.0, 0.0))
        vol[b] = (t + l["repaid"], k + (l["repaid"] if linked else 0.0))
    names = {r["id"]: r["username"] for r in db.execute("SELECT id, username FROM users")}
    farming = sorted(({"id": b, "username": names.get(b, "?"), "volume": t, "linked": k, "share": k / t}
                      for b, (t, k) in vol.items() if t >= 50 and k / t >= 0.5), key=lambda f: -f["linked"])[:10]
    near = db.execute("SELECT u.id, u.username, c.score, c.tier, c.utilisation, "
                      "(SELECT COALESCE(SUM(owed), 0) FROM loans l WHERE l.borrower_id = u.id AND l.status = 'active') AS owed "
                      "FROM user_credit c JOIN users u ON u.id = c.user_id WHERE c.utilisation >= 0.9 "
                      "ORDER BY c.utilisation DESC LIMIT 10").fetchall()
    return {"cfg": cfg, "kpis": kpis, "tiers": tiers, "drops": drops, "farming": farming, "near": near}


def credit_flag_sql(flag):
    return {"default": "c.defaulted > 0", "thin": "c.thin = 1", "frozen": "c.frozen = 1", "adjusted": "c.adjust != 0",
            "override": "c.limit_override IS NOT NULL", "nearlimit": "c.utilisation >= 0.9",
            "held": "u.held = 1",
            "overdue": "EXISTS (SELECT 1 FROM loans l WHERE l.borrower_id = u.id AND l.status = 'active' "
                       "AND l.due_at <= datetime('now', 'localtime'))"}.get(flag)


@app.get("/admin/credit")
@admin_required
def admin_credit():
    return render_admin_credit()


def render_admin_credit(preview=None, form=None):
    db = get_db()
    refresh_credit(db)
    data = admin_credit_data(db)
    flag = request.args.get("flag", "")
    flag = flag if flag in CREDIT_FLAGS else ""
    tier = request.args.get("tier", "")
    sort = request.args.get("sort", "low")
    sort = sort if sort in CREDIT_ADMIN_SORTS else "low"
    lo, hi = _int_arg("min", credit.MIN_SCORE, credit.MAX_SCORE), _int_arg("max", credit.MIN_SCORE, credit.MAX_SCORE)
    q = request.args.get("q", "").strip()
    where, args = [], []
    if q:
        where.append("LOWER(u.username) LIKE ?")
        args.append(f"%{q.lower()}%")
    if tier in data["tiers"]:
        where.append("c.tier = ?")
        args.append(tier)
    if lo is not None:
        where.append("c.score >= ?")
        args.append(lo)
    if hi is not None:
        where.append("c.score <= ?")
        args.append(hi)
    if credit_flag_sql(flag):
        where.append(credit_flag_sql(flag))
    frm = "FROM user_credit c JOIN users u ON u.id = c.user_id" + (" WHERE " + " AND ".join(where) if where else "")
    total = db.execute(f"SELECT COUNT(*) {frm}", args).fetchone()[0]
    pages = max(1, -(-total // CREDIT_PAGE))
    try:
        page = min(pages, max(1, int(request.args.get("page", 1))))
    except ValueError:
        page = 1
    rows = db.execute(
        "SELECT u.id, u.username, u.held, c.score, c.tier, c.multiplier, c.thin, c.utilisation, c.ontime, c.late, "
        "c.defaulted, c.adjust, c.frozen, c.limit_override, c.updated_at, "
        "(SELECT COALESCE(SUM(owed), 0) FROM loans l WHERE l.borrower_id = u.id AND l.status = 'active') AS owed "
        f"{frm} ORDER BY {CREDIT_ADMIN_SORTS[sort]} LIMIT ? OFFSET ?", args + [CREDIT_PAGE, (page - 1) * CREDIT_PAGE]).fetchall()
    return render_template("admin_credit.html", rows=rows, total=total, page=page, pages=pages, flags=CREDIT_FLAGS,
                           view={"flag": flag, "tier": tier, "sort": sort, "min": lo, "max": hi, "q": q},
                           preview=preview, form=form, FACTOR_LABELS=credit.FACTOR_LABELS, **data)


@app.post("/admin/credit/recompute")
@admin_required
def admin_credit_recompute():
    done = recompute_all_credit(get_db(), reason="admin recompute")
    log_audit("credit.recompute", "site", 0, "Credit scores", f"{len(done)} players re-scored")
    flash(f"Re-scored {len(done)} players.", "success")
    return back(url_for("admin_credit"))


@app.post("/admin/credit/config")
@admin_required
def admin_credit_config():
    """Owner only. `action` is 'preview' (recompute everyone in memory and show the tier counts before and after) or
    'save' (store the settings and re-score everyone) or 'reset' (back to the defaults)."""
    if not is_owner():
        flash("Only the owner can change the scoring model.", "error")
        return back(url_for("admin_credit"))
    db = get_db()
    action = request.form.get("action", "")
    base = credit_config(db)
    try:
        if action == "reset":
            set_setting(db, "credit_cfg", "")
            done = recompute_all_credit(db, reason="model reset")
            log_audit("credit.config", "site", 0, "Credit scores", f"Reset to the defaults; {len(done)} players re-scored")
            flash("Scoring model reset to the defaults.", "success")
            return redirect(url_for("admin_credit"))
        cfg = credit_form_config(request.form, base)
        if action == "save":
            overrides = {k: cfg[k] for k in ("weights", "tiers", "multiplier", "default_cap", "overdue_cap",
                                             "held_cap", "half_life")}
            set_setting(db, "credit_cfg", json.dumps(overrides))
            done = recompute_all_credit(db, reason="model changed")
            log_audit("credit.config", "site", 0, "Credit scores",
                      f"Weights {', '.join(f'{k} {v * 100:.0f}%' for k, v in cfg['weights'].items())}; "
                      f"{len(done)} players re-scored")
            flash(f"Scoring model saved and {len(done)} players re-scored.", "success")
            return redirect(url_for("admin_credit"))
        after = recompute_all_credit(db, cfg, dry=True)
        before = [(r["score"], r["tier"]) for r in db.execute("SELECT score, tier FROM user_credit")]
        preview = {"before": _tier_counts(before, base), "after": _tier_counts(after.values(), cfg),
                   "moved": sum(1 for r in db.execute("SELECT user_id, score FROM user_credit")
                                if after.get(r["user_id"]) and after[r["user_id"]][0] != r["score"]),
                   "cfg": cfg}
        return render_admin_credit(preview=preview, form=request.form)
    except TradeError as e:
        flash(str(e), "error")
        return back(url_for("admin_credit"))


def _credit_target(uid, action):
    """The user row an admin may change credit settings on, or None (after flashing why)."""
    db = get_db()
    u, error = user_action_error(db, uid, action)
    if u is None:
        abort(404)
    if error is None and u["is_admin"]:
        error = "Admins aren't scored."
    if error:
        flash(error, "error")
        return None
    return u


def _credit_done(uid):
    return back(url_for("admin_user", uid=uid) + "#credit")


@app.post("/admin/users/<int:uid>/credit/adjust")
@admin_required
def admin_credit_adjust(uid):
    u = _credit_target(uid, "credit")
    if u is None:
        return _credit_done(uid)
    db = get_db()
    try:
        points = int(request.form.get("points", ""))
    except ValueError:
        flash("Enter the points as a whole number (0 clears an adjustment).", "error")
        return _credit_done(uid)
    cap = credit_config(db)["max_adjust"]
    reason = request.form.get("reason", "").strip()[:200]
    if abs(points) > cap:
        flash(f"An adjustment can be at most ±{cap} points.", "error")
    elif points and not reason:
        flash("Say why: a reason is required for an adjustment.", "error")
    else:
        get_credit(db, uid, max_age=60)
        db.execute("UPDATE user_credit SET adjust = ?, adjust_reason = ?, reviewed_by = ?, reviewed_at = ? "
                   "WHERE user_id = ?", (points, reason if points else "", g.user["username"], now_str(), uid))
        apply_credit(db, uid, "admin adjustment")
        log_audit("credit.adjust", "user", uid, u["username"], f"{points:+d} points" + (f": {reason}" if points else " (cleared)"))
        flash("Adjustment cleared." if not points else f"Credit score adjusted by {points:+d}.", "success")
    return _credit_done(uid)


@app.post("/admin/users/<int:uid>/credit/freeze")
@admin_required
def admin_credit_freeze(uid):
    u = _credit_target(uid, "credit")
    if u is None:
        return _credit_done(uid)
    db = get_db()
    freeze = request.form.get("freeze") == "1"
    if get_credit(db, uid, max_age=60) is None:
        flash("This account has no score.", "error")
        return _credit_done(uid)
    db.execute("UPDATE user_credit SET frozen = ?, reviewed_by = ?, reviewed_at = ? WHERE user_id = ?",
               (int(freeze), g.user["username"], now_str(), uid))
    log_audit("credit.freeze" if freeze else "credit.unfreeze", "user", uid, u["username"],
              request.form.get("reason", "").strip()[:200])
    if freeze:
        log_event("credit_frozen", user_id=uid, username=u["username"], detail=f"by {g.user['username']}")
    flash(f"{u['username']} can't borrow until you unfreeze them." if freeze else f"{u['username']} can borrow again.",
          "success")
    return _credit_done(uid)


@app.post("/admin/users/<int:uid>/credit/limit")
@admin_required
def admin_credit_limit(uid):
    u = _credit_target(uid, "credit")
    if u is None:
        return _credit_done(uid)
    db = get_db()
    raw = request.form.get("limit", "").strip()
    try:
        value = float(raw) if raw else None
        if value is not None and not 0 <= value <= 1_000_000:
            raise ValueError
    except ValueError:
        flash("Enter a limit between 0 and 1,000,000, or leave it blank to use the score.", "error")
        return _credit_done(uid)
    get_credit(db, uid, max_age=60)
    db.execute("UPDATE user_credit SET limit_override = ?, reviewed_by = ?, reviewed_at = ? WHERE user_id = ?",
               (value, g.user["username"], now_str(), uid))
    log_audit("credit.limit", "user", uid, u["username"], f"Ⓜ{value:,.2f}" if value is not None else "cleared")
    flash(f"Borrowing limit fixed at Ⓜ{value:,.2f}." if value is not None else "Borrowing limit follows the score again.",
          "success")
    return _credit_done(uid)


@app.get("/admin/log")
@admin_required
def admin_log():
    db = get_db()
    q = request.args.get("q", "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    where, args = "", []
    if q:
        like = f"%{q.lower()}%"
        where = ("WHERE LOWER(actor_name) LIKE ? OR LOWER(action) LIKE ? OR LOWER(target_label) LIKE ? "
                 "OR LOWER(detail) LIKE ?")
        args = [like] * 4
        # also match the readable label ("Banned a user from trading") by finding keys whose label matches
        keys = [k for k, label in AUDIT_ACTIONS.items() if q.lower() in label.lower()]
        if keys:
            where += f" OR action IN ({','.join('?' * len(keys))})"
            args += keys
    total = db.execute(f"SELECT COUNT(*) FROM audit_log {where}", args).fetchone()[0]
    pages = max(1, -(-total // AUDIT_PAGE))
    page = min(page, pages)
    rows = db.execute(f"SELECT a.*, (m.id IS NOT NULL) AS market_exists FROM audit_log a "
                      "LEFT JOIN markets m ON a.target_type = 'market' AND m.id = CAST(a.target_id AS INTEGER) "
                      f"{where} ORDER BY a.id DESC LIMIT ? OFFSET ?", args + [AUDIT_PAGE, (page - 1) * AUDIT_PAGE]).fetchall()
    return render_template("admin_log.html", rows=rows, q=q, page=page, pages=pages, total=total,
                           labels=AUDIT_ACTIONS)


@app.post("/admin/users/<int:uid>/reset-password")
@admin_required
def admin_reset_password(uid):
    db = get_db()
    u = db.execute("SELECT id, username FROM users WHERE id = ?", (uid,)).fetchone()
    if u is None:
        abort(404)
    if not can_manage(uid):
        flash(NOT_ALLOWED, "error")
    else:
        pw = set_temporary_password(db, uid)
        log_audit("user.password_reset", "user", uid, u["username"])
        flash(f"Temporary password for {u['username']}: {pw}. Share it privately; they can change it on their "
              "Portfolio page.", "notice")
    return back(url_for("admin") + "#users")


# ---------------------------------------------------------------- admin: alts, risk, bulk actions

def set_setting(db, key, value):
    if value:
        db.execute("INSERT INTO site_settings (key, value, updated_at) VALUES (?, ?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                   (key, value, now_str()))
    else:
        db.execute("DELETE FROM site_settings WHERE key = ?", (key,))


def decode_risk(row):
    """A user_risk row with its JSON columns opened up (reasons as (points, kind, text), links as {id: info})."""
    if row is None:
        return None
    d = dict(row)
    d["reasons"] = [tuple(x) for x in json.loads(row["reasons"] or "[]")]
    d["links"] = {int(k): v for k, v in json.loads(row["links"] or "{}").items()}
    return d


def user_identity(db, u):
    """Everything an admin sees on a user's page under 'Identity & risk': email, devices, IPs, history, risk."""
    uid = u["id"]
    pending = db.execute("SELECT email FROM login_challenges WHERE username = ? COLLATE NOCASE", (u["username"],)).fetchone()
    email = u["email"] if u["email_verified"] else (pending["email"] if pending else u["email"])
    cls = security.email_provider_class(security.normalize_email(email)[1], *domain_lists(db)) if email and \
        security.normalize_email(email)[1] else ("normal", 0, "")
    events = []
    for e in db.execute("SELECT * FROM login_events WHERE user_id = ? ORDER BY id DESC LIMIT 50", (uid,)):
        try:
            meta = json.loads(e["fp_json"] or "{}")
        except ValueError:
            meta = {}
        events.append(dict(e, agent=security.parse_ua(e["ua"]), meta=meta))

    def shared(kind, value):
        return db.execute("SELECT DISTINCT user_id, username FROM sightings WHERE kind = ? AND value = ? AND user_id != ?",
                          (kind, value, uid)).fetchall()
    sightings = {}
    for kind in ("ip", "net", "device", "fp", "fp_parts"):
        rows = []
        for r in db.execute("SELECT * FROM sightings WHERE user_id = ? AND kind = ? ORDER BY last_seen DESC LIMIT 30",
                            (uid, kind)):
            others = shared(kind, r["value"])
            live = {x["id"] for x in db.execute(f"SELECT id FROM users WHERE id IN ({','.join('?' * len(others)) or 'NULL'})",
                                                [o["user_id"] for o in others])}
            rows.append(dict(r, others=[(o["username"], o["user_id"] in live, o["user_id"]) for o in others]))
        sightings[kind] = rows
    signup = next((e for e in reversed(events) if e["event"] == "signup"), None)
    log_rows = db.execute("SELECT * FROM security_events WHERE user_id = ? ORDER BY id DESC LIMIT 40", (uid,)).fetchall()
    return {"email": email, "email_verified": bool(u["email_verified"]), "provider": cls, "events": events,
            "log": log_rows,
            "sightings": sightings, "signup": signup, "risk": decode_risk(
                db.execute("SELECT * FROM user_risk WHERE user_id = ?", (uid,)).fetchone())}


@app.post("/admin/users/<int:uid>/risk")
@admin_required
def admin_risk(uid):
    db = get_db()
    action = request.form.get("action", "")
    if action not in ("approve", "hold", "rescan"):
        abort(400)
    u, error = user_action_error(db, uid, action)
    if u is None:
        abort(404)
    if error:
        flash(error, "error")
    elif action == "rescan":
        apply_risk(db, uid)
        flash(f"Re-scanned {u['username']}.", "success")
    elif action == "approve":
        set_risk_status(db, uid, "approved")
        log_audit("user.approve", "user", uid, u["username"])
        flash(f"Approved {u['username']}. They can use the site normally.", "success")
    else:
        set_risk_status(db, uid, "held")
        log_audit("user.hold", "user", uid, u["username"])
        flash(f"{u['username']} is on hold: they can look around but not bet, lend, comment or propose.", "success")
    return back(url_for("admin_user", uid=uid))


BULK_ACTIONS = {"delete": "Delete", "ban": "Ban from trading", "allow": "Allow trading", "mute": "Mute",
                "unmute": "Unmute", "hold": "Hold for review", "approve": "Approve", "rescan": "Re-scan"}
BULK_MAX = 200


@app.post("/admin/users/bulk")
@admin_required
def admin_users_bulk():
    """Apply one action to many accounts. Each account goes through the same checks as its own page
    (user_action_error), is handled on its own, and gets its own Admin log entry; refused ones are listed."""
    db = get_db()
    action = request.form.get("action", "")
    ids = []
    for raw in request.form.getlist("uids"):
        if raw.isdigit() and int(raw) not in ids:
            ids.append(int(raw))
    if action not in BULK_ACTIONS:
        flash("Pick an action.", "error")
    elif not ids:
        flash("Tick at least one account first.", "error")
    elif len(ids) > BULK_MAX:
        flash(f"Pick at most {BULK_MAX} accounts at a time.", "error")
    else:
        done, skipped = [], []
        pool = risk_pool(db) if action == "rescan" else None
        for uid in ids:
            u, error = user_action_error(db, uid, action)
            if error:
                skipped.append(f"{u['username'] if u else uid}: {error}")
                continue
            name = u["username"]
            try:
                if action == "delete":
                    amount = delete_account(db, uid)
                    log_audit("user.delete", "user", uid, name, f"Open bets reversed (Ⓜ{amount:,.2f}). (bulk)")
                elif action == "ban":
                    if db.execute("SELECT 1 FROM trade_bans WHERE user_id = ?", (uid,)).fetchone():
                        raise TradeError("already banned from trading")
                    refund = ban_user_from_trading(db, uid)
                    log_audit("user.ban", "user", uid, name, f"Refunded Ⓜ{refund:,.2f} from open bets. (bulk)")
                elif action == "allow":
                    if not db.execute("DELETE FROM trade_bans WHERE user_id = ?", (uid,)).rowcount:
                        raise TradeError("wasn't banned")
                    log_audit("user.unban", "user", uid, name, "(bulk)")
                elif action == "mute":
                    if db.execute("SELECT 1 FROM comment_mutes WHERE user_id = ?", (uid,)).fetchone():
                        raise TradeError("already muted")
                    db.execute("INSERT INTO comment_mutes (user_id, created_at) VALUES (?, ?)", (uid, now_str()))
                    log_audit("user.comment_mute", "user", uid, name, "(bulk)")
                elif action == "unmute":
                    if not db.execute("DELETE FROM comment_mutes WHERE user_id = ?", (uid,)).rowcount:
                        raise TradeError("wasn't muted")
                    log_audit("user.comment_unmute", "user", uid, name, "(bulk)")
                elif action == "hold":
                    set_risk_status(db, uid, "held")
                    log_audit("user.hold", "user", uid, name, "(bulk)")
                elif action == "approve":
                    set_risk_status(db, uid, "approved")
                    log_audit("user.approve", "user", uid, name, "(bulk)")
                else:
                    apply_risk(db, uid, pool)
            except TradeError as e:
                skipped.append(f"{name}: {e}")
                continue
            done.append(name)
        if done:
            log_audit("user.bulk", "user", "", f"{len(done)} account(s)",
                      f"{BULK_ACTIONS[action]}: {', '.join(done)[:380]}")
        msg = f"{BULK_ACTIONS[action]}: {len(done)} done" + (f", {len(skipped)} skipped" if skipped else "") + "."
        if skipped:
            msg += " Skipped: " + "; ".join(skipped)[:300]
        flash(msg, "success" if done else "error")
    return back(url_for("admin") + "#users")


EVENT_PAGE = 50


@app.get("/admin/events")
@admin_required
def admin_events():
    """The event log: sign-ups, log-ins, failures, emails, holds, errors and admin actions, newest first. Events about
    admins and the owner are only shown to the owner (their IPs and devices are private like everyone else's are
    to non-admins)."""
    db = get_db()
    sev = request.args.get("severity", "")
    kind = request.args.get("kind", "")
    q = request.args.get("q", "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    where, args = [], []
    if not is_owner():
        where.append("(e.user_id IS NULL OR u.id IS NULL OR (u.is_admin = 0 AND u.id != ?))")
        args.append(g.owner_id or -1)
    if sev in eventlog.SEVERITIES:
        where.append("e.severity = ?")
        args.append(sev)
    if kind in eventlog.EVENTS:
        where.append("e.kind = ?")
        args.append(kind)
    if q:
        like = f"%{q.lower()}%"
        where.append("(LOWER(e.username) LIKE ? OR e.ip LIKE ? OR LOWER(e.detail) LIKE ? OR e.request_id = ? "
                     "OR LOWER(e.path) LIKE ?)")
        args += [like, f"{q}%", like, q, like]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    frm = f"FROM security_events e LEFT JOIN users u ON u.id = e.user_id {clause}"
    total = db.execute(f"SELECT COUNT(*) {frm}", args).fetchone()[0]
    pages = max(1, -(-total // EVENT_PAGE))
    page = min(page, pages)
    rows = db.execute(f"SELECT e.* {frm} ORDER BY e.id DESC LIMIT ? OFFSET ?", args + [EVENT_PAGE, (page - 1) * EVENT_PAGE]).fetchall()
    day = time.time() - 86400
    summary = {r["severity"]: r["n"] for r in db.execute(
        "SELECT severity, COUNT(*) AS n FROM security_events WHERE created_at >= ? GROUP BY severity", (day,))}
    return render_template("admin_events.html", rows=rows, q=q, sev=sev, kind=kind, page=page, pages=pages, total=total,
                           catalog=eventlog.EVENTS, summary=summary)


def risk_clusters(db):
    """Groups of accounts tied together by shared devices, IPs, emails or loans (union-find over the saved links)."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    rows = {r["user_id"]: r for r in db.execute(
        "SELECT r.*, u.username FROM user_risk r JOIN users u ON u.id = r.user_id WHERE u.is_admin = 0")}
    for uid, r in rows.items():
        for other in json.loads(r["links"] or "{}"):
            find(uid)
            parent[find(int(other))] = find(uid)
    groups = {}
    for uid in rows:
        if uid in parent:
            groups.setdefault(find(uid), []).append(uid)
    out = []
    for members in groups.values():
        members = [m for m in members if m in rows]
        if len(members) >= 2 and any(rows[m]["status"] in ("held", "flagged") for m in members):
            out.append({"accounts": [{"id": m, "username": rows[m]["username"], "status": rows[m]["status"],
                                      "score": rows[m]["score"]} for m in sorted(members, key=lambda m: -rows[m]["score"])],
                        "top": max(rows[m]["score"] for m in members)})
    return sorted(out, key=lambda c: -c["top"])[:30]


@app.get("/admin/security")
@admin_required
def admin_security():
    db = get_db()
    queue = []
    for r in db.execute("SELECT r.*, u.username, u.created_at, u.email, u.email_verified, u.balance FROM user_risk r "
                        "JOIN users u ON u.id = r.user_id WHERE r.status IN ('held', 'flagged') AND u.is_admin = 0 "
                        "AND u.id != ? ORDER BY (r.status = 'held') DESC, r.score DESC LIMIT 200", (g.owner_id or -1,)):
        d = decode_risk(r)
        d.update(username=r["username"], created_at=r["created_at"], balance=r["balance"])
        queue.append(d)
    blocked_emails = db.execute("SELECT * FROM email_ledger WHERE status = 'deleted' ORDER BY created_at DESC LIMIT 200").fetchall()
    return render_template(
        "admin_security.html", queue=queue, clusters=risk_clusters(db), blocked_emails=blocked_emails,
        blocked_text="\n".join(security.parse_domain_list(get_setting(db, "email_blocked"))),
        shady_text="\n".join(security.parse_domain_list(get_setting(db, "email_shady"))),
        networks_text="\n".join(security.parse_network_list(get_setting(db, "common_networks"))),
        bundled_blocked=len(security.bundled_lists()[0]), bundled_shady=len(security.bundled_lists()[1]),
        school=sorted(security.school_domains()), thresholds={"hold": security.HOLD_THRESHOLD,
                                                              "flag": security.FLAG_THRESHOLD,
                                                              "days": security.NEW_ACCOUNT_DAYS,
                                                              "common": security.COMMON_THRESHOLD},
        bulk_actions=BULK_ACTIONS)


@app.post("/admin/security/lists")
@admin_required
def admin_security_lists():
    db = get_db()
    set_setting(db, "email_blocked", "\n".join(security.parse_domain_list(request.form.get("blocked", ""))))
    set_setting(db, "email_shady", "\n".join(security.parse_domain_list(request.form.get("shady", ""))))
    set_setting(db, "common_networks", "\n".join(security.parse_network_list(request.form.get("networks", ""))))
    log_audit("security.lists", "site", "security", "Security lists")
    flash("Saved. Re-scan to apply the new lists to existing accounts.", "success")
    return redirect(url_for("admin_security") + "#lists")


@app.post("/admin/security/rescan")
@admin_required
def admin_security_rescan():
    db = get_db()
    pool, cfg = risk_pool(db), risk_config(db)
    ids = [r["id"] for r in db.execute("SELECT id FROM users WHERE is_admin = 0 AND id != ?", (g.owner_id or -1,))]
    held = flagged = 0
    for uid in ids:
        status = apply_risk(db, uid, pool, cfg)
        held += status == "held"
        flagged += status == "flagged"
    log_audit("security.rescan", "site", "security", "All accounts", f"{len(ids)} scanned: {held} held, {flagged} flagged")
    flash(f"Scanned {len(ids)} accounts: {held} held, {flagged} flagged.", "success")
    return redirect(url_for("admin_security"))


@app.post("/admin/security/release-email")
@admin_required
def admin_release_email():
    db = get_db()
    canon = request.form.get("canonical", "")
    if db.execute("UPDATE email_ledger SET status = 'released', released_by = ?, released_at = ? "
                  "WHERE canonical = ? AND status = 'deleted'", (g.user["username"], time.time(), canon)).rowcount:
        log_audit("email.release", "site", "email", canon)
        flash(f"{canon} can be used to sign up again.", "success")
    else:
        flash("That email isn't blocked.", "error")
    return redirect(url_for("admin_security") + "#emails")


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
@app.errorhandler(500)
def error_page(e):
    if e.code == 500:
        orig = getattr(e, "original_exception", None)
        log_event("server_error", detail=f"{request.method} {request.path}: {type(orig).__name__ if orig else 'error'}")
    elif e.code == 413:
        log_event("too_large", detail=f"{request.method} {request.path}")
    return render_template("error.html", error=e, reference=g.get("request_id")), e.code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=os.environ.get("FLASK_DEBUG") == "1")
