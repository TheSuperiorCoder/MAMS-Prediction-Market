"""MAMS Prediction Market — bet virtual MAMS Bucks on Mass Academy events."""

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import smtplib
import ssl
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for, current_app)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import lmsr

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
CREATION_FEE = 0.0
ENTRY_FEE = 0.0
# Whoever makes a market must also bet at least this much on it, so they have skin in the game.
CREATOR_MIN_BET = 0.0
CATEGORIES = ["Academics", "Competitions", "Sports", "Events", "Campus Life", "Weather", "Other"]
EPS = 1e-9
OWNER_USERNAME = os.environ.get("MAMS_OWNER", "").strip().lower()
LOGIN_CODE_LIFETIME = 10 * 60
LOGIN_CODE_RESEND_DELAY = 60
LOGIN_CODE_MAX_ATTEMPTS = 5
LOGIN_SETUP_LIFETIME = 15 * 60


def _secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(INSTANCE_DIR, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.config.update(SECRET_KEY=_secret_key(), PERMANENT_SESSION_LIFETIME=timedelta(days=30),
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=PRODUCTION)
if PRODUCTION:
    # Trust the hosting proxy's X-Forwarded-For / -Proto so we see real visitor IPs and HTTPS.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# Simple in-memory rate limiting (one server process is plenty for a school site).
# Limits are generous because a whole school can share one public IP address.
_hits = {}


def rate_limited(key, limit, window_seconds):
    now = time.time()
    recent = [t for t in _hits.get(key, []) if now - t < window_seconds]
    limited = len(recent) >= limit
    if not limited:
        recent.append(now)
    _hits[key] = recent
    if len(_hits) > 10000:
        _hits.clear()
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
        db.close()


# Columns added after the first release. schema.sql only creates missing tables, so databases
# created earlier get these through ALTER TABLE (see migrate).
NEW_COLUMNS = {
    "users": [("email", "TEXT"), ("email_verified", "INTEGER NOT NULL DEFAULT 0")],
    "markets": [("rules_source", "TEXT NOT NULL DEFAULT ''"), ("rules_otherwise", "TEXT NOT NULL DEFAULT ''"),
                ("proposed_outcome_id", "INTEGER"), ("proposed_by", "INTEGER"), ("proposed_at", "TEXT"),
                ("finalizes_at", "TEXT"), ("resolved_by", "INTEGER"),
                # Over/Under markets: the line, an optional unit ("%", "minutes"), and the measured result.
                ("ou_line", "REAL"), ("ou_unit", "TEXT NOT NULL DEFAULT ''"), ("ou_result", "REAL")],
    "proposals": [("rules_source", "TEXT NOT NULL DEFAULT ''"), ("rules_otherwise", "TEXT NOT NULL DEFAULT ''"),
                  ("ou_line", "REAL"), ("ou_unit", "TEXT NOT NULL DEFAULT ''"),
                  ("fee", "REAL NOT NULL DEFAULT 0"),  # creation fee paid, refunded on withdrawal
                  # the proposer's opening bet, held until approval (refunded if rejected or withdrawn)
                  ("stake", "REAL NOT NULL DEFAULT 0"), ("stake_pick", "INTEGER"),
                  ("stake_label", "TEXT NOT NULL DEFAULT ''")],
}


def migrate(db):
    """Add any columns an older database is missing. Safe to run repeatedly."""
    for table, columns in NEW_COLUMNS.items():
        have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


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


def init_db():
    db = connect()
    with open(os.path.join(BASE_DIR, "schema.sql")) as f:
        db.executescript(f.read())
    migrate(db)
    deepen_liquidity(db)
    backfill_activity(db)
    shorten_dispute_windows(db)
    db.close()


def load_login_directory():
    """Load private participant identity data from deployment config or the ignored instance directory."""
    raw = os.environ.get("MAMS_USER_DIRECTORY")
    if raw:
        entries = json.loads(raw)
    else:
        path = os.environ.get("MAMS_USER_DIRECTORY_FILE", os.path.join(INSTANCE_DIR, "login_roster.json"))
        with open(path, encoding="utf-8") as directory_file:
            entries = json.load(directory_file)
    if not isinstance(entries, list):
        raise ValueError("The user directory must be a JSON list.")
    directory = {}
    seen = set()
    emails_seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each user directory entry must contain a name and email.")
        name = entry.get("name")
        email = entry.get("email")
        if not isinstance(name, str) or not isinstance(email, str):
            raise ValueError("Each user directory entry must contain a name and email.")
        name = name.strip()
        email = email.strip()
        key = name.casefold()
        email_key = email.casefold()
        if (not name or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
                or key in seen or email_key in emails_seen):
            raise ValueError("The user directory contains a missing or duplicate name/email.")
        directory[key] = {"name": name, "email": email}
        seen.add(key)
        emails_seen.add(email_key)
    return directory


def login_code_hash(username, code):
    value = f"{username.casefold()}:{code}".encode()
    return hmac.new(app.config["SECRET_KEY"].encode(), value, hashlib.sha256).hexdigest()


def send_login_code_email(recipient, code):
    if app.config.get("TESTING"):
        raise RuntimeError("Email delivery is disabled during automated tests.")
    if os.environ.get("MAMS_EMAIL_BACKEND", "smtp").strip().lower() == "console":
        current_app.logger.warning("LOCAL VERIFICATION CODE for %s: %s", recipient, code)
        return
    host = os.environ.get("MAMS_SMTP_HOST", "").strip()
    sender = os.environ.get("MAMS_SMTP_FROM", os.environ.get("MAMS_SMTP_USERNAME", "")).strip()
    if not host or not sender:
        raise RuntimeError("SMTP host and sender must be configured before email can be sent.")
    port = int(os.environ.get("MAMS_SMTP_PORT", "587"))
    message = EmailMessage()
    message["Subject"] = "Your MAMS Prediction Market verification code"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(
        f"Your MAMS Prediction Market verification code is {code}. "
        "It expires in 10 minutes. If you did not request it, you can ignore this email."
    )
    with smtplib.SMTP(host, port, timeout=15) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        smtp_username = os.environ.get("MAMS_SMTP_USERNAME", "")
        smtp_password = os.environ.get("MAMS_SMTP_PASSWORD", "")
        if smtp_username:
            server.login(smtp_username, smtp_password)
        server.send_message(message)


init_db()


# ---------------------------------------------------------------- helpers

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(s):
    return datetime.fromisoformat(s)


def cents(x):
    return round(x + 0.0, 2)


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


def bundle(db, m):
    """A market plus its outcomes and current prices, ready for templates."""
    outs = get_outcomes(db, m["id"])
    if m["status"] == "resolved":
        ps = [1.0 if o["id"] == m["resolved_outcome_id"] else 0.0 for o in outs]
    else:
        ps = lmsr.prices([o["shares"] for o in outs], m["b"])
    stats = db.execute(
        "SELECT COALESCE(SUM(ABS(amount)), 0) AS volume, COUNT(DISTINCT user_id) AS traders "
        "FROM trades WHERE market_id = ?", (m["id"],)).fetchone()
    state = market_state(m)
    if state == "proposed" and open_disputes(db, m["id"]):
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
        "volume": stats["volume"],
        "traders": stats["traders"],
    }


def snapshot_prices(db, market_id, b):
    outs = get_outcomes(db, market_id)
    ps = lmsr.prices([o["shares"] for o in outs], b)
    db.execute("INSERT INTO price_history (market_id, prices, created_at) VALUES (?, ?, ?)",
               (market_id, json.dumps([round(p, 5) for p in ps]), now_str()))


def open_book_values(db):
    """{user_id: {market_id: cash-out value}} for every unresolved market. Each value is what the user's
    whole holding in that market would sell for right now, so buying can't inflate it."""
    markets = {}
    for m in db.execute("SELECT * FROM markets WHERE status = 'open'").fetchall():
        outs = get_outcomes(db, m["id"])
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


def net_worths(db):
    worth = {u["id"]: u["balance"] for u in db.execute("SELECT id, balance FROM users")}
    for uid, per_market in open_book_values(db).items():
        worth[uid] += sum(per_market.values())
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
        db.execute("UPDATE outcomes SET shares = shares - ? WHERE id = ?", (p["shares"], p["outcome_id"]))
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


def check_trade(m, outs, idx, action, value, balance=None, held=None, fee=0.0):
    """Validate a trade and price it. Returns (shares, coins): shares bought/sold and coins spent/received.
    Raises TradeError with a user-facing message. balance/held may be None to skip those checks
    (used for logged-out quotes). Shared by execute_trade and the /api/quote preview so they always agree."""
    q = [o["shares"] for o in outs]
    label = outs[idx]["label"]
    if action == "buy":
        amount = cents(value)
        if amount < MIN_BET:
            raise TradeError(f"Minimum bet is {MIN_BET:.0f} {CURRENCY}.")
        if balance is not None and amount + fee > balance + EPS:
            if fee:
                raise TradeError(f"You need Ⓜ{amount + fee:,.2f} (Ⓜ{amount:,.2f} bet + Ⓜ{fee:,.0f} entry fee) "
                                 f"but have Ⓜ{balance:,.2f}.")
            raise TradeError("You don't have enough MAMS Bucks for that bet.")
        return lmsr.shares_for_amount(q, m["b"], idx, amount), amount
    if action == "sell":
        shares = value
        if held is not None and abs(shares - held) < 1e-6:
            shares = held
        if shares <= EPS:
            raise TradeError("Enter a number of shares to sell.")
        if held is not None and shares > held + EPS:
            raise TradeError(f"You only own {held:.2f} shares of “{label}”.")
        return shares, math.floor(lmsr.sell_value(q, m["b"], idx, shares) * 100) / 100
    raise TradeError("Unknown action.")


def execute_trade(db, user_id, market_id, outcome_id, action, value):
    """Buy (value = coins to spend) or sell (value = shares to sell). Returns a status message."""
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
        shares, coins = check_trade(m, outs, idx, action, value, user["balance"], held, fee)
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
        if action == "buy":
            d_shares, d_cash = shares, -coins
            new_shares, new_cost = held + shares, held_cost + coins
            msg = (f"You paid Ⓜ{coins:,.2f} for {shares:,.2f} shares of “{label}”"
                   + (f" plus a Ⓜ{fee:,.0f} entry fee to {creator}" if fee else "") +
                   f". If it wins they pay Ⓜ{new_shares:,.2f} in total. Price now {now_pct:.0f}%.")
            detail = f"Bought {shares:,.2f} shares of “{label}”"
        else:
            d_shares, d_cash = -shares, coins
            new_shares = held - shares
            new_cost = held_cost * (new_shares / held) if held > 0 else 0.0
            msg = f"You received Ⓜ{coins:,.2f} for {shares:,.2f} shares of “{label}”. Price now {now_pct:.0f}%."
            detail = f"Sold {shares:,.2f} shares of “{label}”"

        db.execute("UPDATE outcomes SET shares = shares + ? WHERE id = ?", (d_shares, outcome_id))
        db.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE id = ?", (d_cash, user_id))
        log_activity(db, user_id, action, d_cash, m, detail)
        msg += f" Balance: Ⓜ{db.execute('SELECT balance FROM users WHERE id = ?', (user_id,)).fetchone()[0]:,.2f}."
        if new_shares <= 1e-9:
            db.execute("DELETE FROM positions WHERE user_id = ? AND outcome_id = ?", (user_id, outcome_id))
        else:
            db.execute(
                "INSERT INTO positions (user_id, outcome_id, shares, cost) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, outcome_id) DO UPDATE SET shares = excluded.shares, cost = excluded.cost",
                (user_id, outcome_id, new_shares, new_cost))
        db.execute("INSERT INTO trades (user_id, market_id, outcome_id, shares, amount, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?)", (user_id, market_id, outcome_id, d_shares, -d_cash, now_str()))
        snapshot_prices(db, market_id, m["b"])
        db.execute("COMMIT")
        return msg
    except Exception:
        db.execute("ROLLBACK")
        raise


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
        for p in db.execute("SELECT p.user_id, p.shares, p.outcome_id FROM positions p JOIN outcomes o "
                            "ON o.id = p.outcome_id WHERE o.market_id = ? AND p.outcome_id != ? AND p.shares > 1e-9",
                            (market_id, outcome_id)).fetchall():
            log_activity(db, p["user_id"], "loss", 0, m,
                         f"Lost: {p['shares']:,.2f} “{outs[p['outcome_id']]}” shares paid nothing")
        db.execute("UPDATE markets SET status = 'resolved', resolved_outcome_id = ?, resolved_at = ?, "
                   "resolved_by = ? WHERE id = ?", (outcome_id, now_str(), resolved_by, market_id))
        db.execute("UPDATE disputes SET handled_at = ? WHERE market_id = ? AND handled_at IS NULL",
                   (now_str(), market_id))
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
        db.execute("UPDATE markets SET status = 'cancelled', resolved_at = ? WHERE id = ?", (now_str(), market_id))
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
        for table in ("trades", "price_history", "comments", "market_blocks", "market_entries", "outcomes"):
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
        elif g.user["id"] == g.owner_id and not g.user["is_admin"]:
            db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (g.user["id"],))  # owner is always admin
            g.user = db.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    if request.endpoint != "static":
        finalize_due(get_db())
    if request.method == "POST":
        token = request.form.get("csrf_token", "")
        if not token or not secrets.compare_digest(token, session.get("_csrf", "")):
            abort(400, "Your form expired. Go back, reload the page and try again.")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Log in to do that.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def owner_id(db):
    if OWNER_USERNAME:
        row = db.execute("SELECT id FROM users WHERE username = ?", (OWNER_USERNAME,)).fetchone()
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


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None or not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    can_bonus = bool(g.user) and g.user["last_bonus"] != date.today().isoformat()
    pending = 0
    if g.user and g.user["is_admin"]:
        pending = get_db().execute("SELECT COUNT(*) FROM proposals WHERE status = 'pending'").fetchone()[0]
    return {"csrf_token": csrf_token, "user": g.user, "can_bonus": can_bonus, "pending_proposals": pending,
            "owner_id": g.get("owner_id"), "is_owner": is_owner(),
            "CURRENCY": CURRENCY, "DAILY_BONUS": DAILY_BONUS, "CATEGORIES": CATEGORIES,
            "DISPUTE_HOURS": DISPUTE_HOURS, "CREATION_FEE": CREATION_FEE, "ENTRY_FEE": ENTRY_FEE,
            "CREATOR_MIN_BET": CREATOR_MIN_BET}


@app.template_filter("money")
def money_filter(x):
    return f"{x:,.2f}"


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
    return parse_dt(s).strftime("%b %-d, %Y %-I:%M %p")


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


# ---------------------------------------------------------------- routes: accounts

@app.route("/register", methods=["GET", "POST"])
def register():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.form.get("next", "") if request.method == "POST" else request.args.get("next", "")
    try:
        directory = load_login_directory()
    except (OSError, ValueError):
        current_app.logger.exception("Unable to load the private login directory.")
        flash("Login is temporarily unavailable. Please contact the site administrator.", "error")
        return render_template("login.html", stage="select", names=[], next_url=next_url)

    names = [entry["name"] for entry in directory.values()]
    if request.method == "POST":
        stage = request.form.get("stage", "select")
    elif session.get("verified_login_name") and \
            time.time() - session.get("verified_login_at", 0) <= LOGIN_SETUP_LIFETIME:
        stage = "create_password"
    elif session.get("pending_login_name"):
        stage = "verify"
    else:
        stage = "select"
    selected_name = session.get("verified_login_name") if stage == "create_password" else \
        session.get("pending_login_name") if stage == "verify" else ""
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        entry = directory.get(name.casefold())
        if stage == "back_to_select":
            pending_name = session.pop("pending_login_name", "")
            if pending_name:
                get_db().execute("DELETE FROM login_challenges WHERE username = ?", (pending_name,))
            session.pop("verified_login_name", None)
            session.pop("verified_login_at", None)
            return render_template("login.html", stage="select", names=names, next_url=next_url)
        elif stage == "select":
            session.pop("pending_login_name", None)
            session.pop("verified_login_name", None)
            session.pop("verified_login_at", None)
            if entry is None:
                flash("Select a name from the list.", "error")
            elif rate_limited(("login-start-ip", request.remote_addr), 20, 3600) or \
                    rate_limited(("login-start", entry["name"].casefold()), 3, 900):
                flash("Too many login attempts. Please wait and try again.", "error")
            else:
                user = get_db().execute("SELECT * FROM users WHERE username = ?", (entry["name"],)).fetchone()
                if user:
                    return render_template("login.html", stage="password", names=names,
                                           selected_name=entry["name"], next_url=next_url)
                existing = get_db().execute(
                    "SELECT requested_at FROM login_challenges WHERE username = ?",
                    (entry["name"],)).fetchone()
                if existing and time.time() - existing["requested_at"] < LOGIN_CODE_RESEND_DELAY:
                    flash("A verification code was just sent. Check your email and try it below.", "success")
                    session["pending_login_name"] = entry["name"]
                    return render_template("login.html", stage="verify", names=names,
                                           selected_name=entry["name"], next_url=next_url)
                code = f"{secrets.randbelow(1_000_000):06d}"
                try:
                    send_login_code_email(entry["email"], code)
                except (OSError, RuntimeError, smtplib.SMTPException, ValueError):
                    current_app.logger.exception("Unable to send a first-login verification code.")
                    flash("Unable to send a verification email. Please try again later.", "error")
                else:
                    get_db().execute(
                        "INSERT OR REPLACE INTO login_challenges "
                        "(username, code_hash, expires_at, requested_at, attempts) VALUES (?, ?, ?, ?, 0)",
                        (entry["name"], login_code_hash(entry["name"], code),
                         time.time() + LOGIN_CODE_LIFETIME, time.time()))
                    session["pending_login_name"] = entry["name"]
                    session.pop("verified_login_name", None)
                    session.pop("verified_login_at", None)
                    flash("A verification code was sent to the email address on file.", "success")
                    return render_template("login.html", stage="verify", names=names,
                                           selected_name=entry["name"], next_url=next_url)
        elif stage == "password":
            user = get_db().execute("SELECT * FROM users WHERE username = ?", (entry["name"],)).fetchone() \
                if entry else None
            fail_key = ("login-fail", entry["name"].casefold() if entry else name.casefold())
            recent_fails = [t for t in _hits.get(fail_key, []) if time.time() - t < 300]
            if len(recent_fails) >= 10:
                flash("Too many wrong passwords for this account. Wait 5 minutes and try again.", "error")
                return render_template("login.html", stage="password", names=names,
                                       selected_name=entry["name"] if entry else "", next_url=next_url)
            elif user and check_password_hash(user["password_hash"], request.form.get("password", "")):
                _hits.pop(fail_key, None)
                session.clear()
                session.permanent = True
                session["uid"] = user["id"]
                return redirect(next_url if next_url.startswith("/") and not next_url.startswith("//")
                                else url_for("index"))
            else:
                rate_limited(fail_key, 10, 300)
                flash("Wrong username or password.", "error")
                return render_template("login.html", stage="password", names=names,
                                       selected_name=entry["name"] if entry else "", next_url=next_url)
        elif stage == "verify":
            pending_name = session.get("pending_login_name", "")
            entry = directory.get(pending_name.casefold())
            challenge = get_db().execute(
                "SELECT * FROM login_challenges WHERE username = ?", (pending_name,)).fetchone() if entry else None
            if request.form.get("action") == "resend":
                if not entry or not challenge:
                    session.pop("pending_login_name", None)
                    flash("Select your name to request a verification code.", "error")
                    return render_template("login.html", stage="select", names=names, next_url=next_url)
                if time.time() - challenge["requested_at"] < LOGIN_CODE_RESEND_DELAY:
                    flash("Please wait before requesting another verification code.", "error")
                    return render_template("login.html", stage="verify", names=names,
                                           selected_name=entry["name"], next_url=next_url)
                if rate_limited(("login-start-ip", request.remote_addr), 20, 3600) or \
                        rate_limited(("login-start", entry["name"].casefold()), 3, 900):
                    flash("Too many code requests. Please wait and try again.", "error")
                    return render_template("login.html", stage="verify", names=names,
                                           selected_name=entry["name"], next_url=next_url)
                code = f"{secrets.randbelow(1_000_000):06d}"
                try:
                    send_login_code_email(entry["email"], code)
                except (OSError, RuntimeError, smtplib.SMTPException, ValueError):
                    current_app.logger.exception("Unable to resend a first-login verification code.")
                    flash("Unable to send a verification email. Please try again later.", "error")
                else:
                    get_db().execute(
                        "UPDATE login_challenges SET code_hash = ?, expires_at = ?, requested_at = ?, attempts = 0 "
                        "WHERE username = ?",
                        (login_code_hash(entry["name"], code), time.time() + LOGIN_CODE_LIFETIME,
                         time.time(), entry["name"]))
                    flash("A new verification code was sent to the email address on file.", "success")
                return render_template("login.html", stage="verify", names=names,
                                       selected_name=entry["name"], next_url=next_url)
            if (not entry or not challenge or challenge["expires_at"] < time.time()
                    or challenge["attempts"] >= LOGIN_CODE_MAX_ATTEMPTS):
                flash("That verification code expired. Select your name to request another.", "error")
                session.pop("pending_login_name", None)
                return render_template("login.html", stage="select", names=names, next_url=next_url)
            code = request.form.get("code", "").strip()
            if not hmac.compare_digest(challenge["code_hash"], login_code_hash(entry["name"], code)):
                get_db().execute(
                    "UPDATE login_challenges SET attempts = attempts + 1 WHERE username = ?", (entry["name"],))
                flash("That verification code is incorrect.", "error")
                return render_template("login.html", stage="verify", names=names,
                                       selected_name=entry["name"], next_url=next_url)
            get_db().execute("DELETE FROM login_challenges WHERE username = ?", (entry["name"],))
            session.pop("pending_login_name", None)
            session["verified_login_name"] = entry["name"]
            session["verified_login_at"] = time.time()
            return render_template("login.html", stage="create_password", names=names,
                                   selected_name=entry["name"], next_url=next_url)
        elif stage == "create_password":
            verified_name = session.get("verified_login_name", "")
            entry = directory.get(verified_name.casefold())
            verified_at = session.get("verified_login_at", 0)
            if not entry or time.time() - verified_at > LOGIN_SETUP_LIFETIME:
                session.pop("verified_login_name", None)
                session.pop("verified_login_at", None)
                flash("Your verification session expired. Select your name to start again.", "error")
                return render_template("login.html", stage="select", names=names, next_url=next_url)
            password = request.form.get("password", "")
            if len(password) < 8:
                flash("Password must be at least 8 characters.", "error")
            elif password != request.form.get("confirm", ""):
                flash("Passwords don't match.", "error")
            elif get_db().execute("SELECT 1 FROM users WHERE username = ?", (entry["name"],)).fetchone():
                session.pop("verified_login_name", None)
                session.pop("verified_login_at", None)
                flash("An account already exists for that name. Log in with its password.", "error")
                return render_template("login.html", stage="select", names=names, next_url=next_url)
            else:
                db = get_db()
                first_user = db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
                is_admin = int((first_user and not OWNER_USERNAME) or entry["name"].casefold() == OWNER_USERNAME
                               or entry["name"].casefold() in ADMIN_USERNAMES)
                try:
                    db.execute("BEGIN IMMEDIATE")
                    cur = db.execute(
                        "INSERT INTO users (username, password_hash, email, email_verified, balance, is_admin, created_at) "
                        "VALUES (?, ?, ?, 1, ?, ?, ?)",
                        (entry["name"], generate_password_hash(password), entry["email"],
                         STARTING_BALANCE, is_admin, now_str()))
                    log_activity(db, cur.lastrowid, "signup", STARTING_BALANCE, None, "Starting balance")
                    db.execute("COMMIT")
                except sqlite3.IntegrityError:
                    db.execute("ROLLBACK")
                    flash("An account already exists for that name. Log in with its password.", "error")
                    return render_template("login.html", stage="select", names=names, next_url=next_url)
                session.clear()
                session.permanent = True
                session["uid"] = cur.lastrowid
                flash(f"Welcome! You start with {STARTING_BALANCE:,.0f} {CURRENCY}."
                      + (" You're an admin, so you can create and resolve markets." if is_admin else ""), "success")
                return redirect(next_url if next_url.startswith("/") and not next_url.startswith("//")
                                else url_for("index"))
        else:
            flash("That login step is no longer available. Start again.", "error")
            stage = "select"
    return render_template("login.html", stage=stage, names=names,
                           selected_name=selected_name, next_url=next_url)


@app.post("/logout")
def logout():
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
    else:
        flash("You already claimed today's bonus. Come back tomorrow.", "error")
    return redirect(request.referrer or url_for("index"))


# ---------------------------------------------------------------- routes: markets

@app.route("/")
def index():
    db = get_db()
    status = request.args.get("status", "open")
    cat = request.args.get("cat", "")
    markets = [bundle(db, m) for m in db.execute("SELECT * FROM markets ORDER BY closes_at").fetchall()]
    if status == "closed":  # "Awaiting result" also covers results that are proposed or disputed
        markets = [b for b in markets if b["state"] in ("closed", "proposed", "disputed")]
    elif status in ("open", "resolved", "cancelled"):
        markets = [b for b in markets if b["state"] == status]
    if cat:
        markets = [b for b in markets if b["m"]["category"] == cat]
    if status == "open":
        markets.sort(key=lambda b: -b["volume"])
    elif status in ("resolved", "cancelled"):
        markets.sort(key=lambda b: b["m"]["resolved_at"] or "", reverse=True)
    return render_template("index.html", markets=markets, status=status, cat=cat)


@app.route("/market/<int:mid>")
def market(mid):
    db = get_db()
    m = db.execute("SELECT * FROM markets WHERE id = ?", (mid,)).fetchone()
    if m is None:
        abort(404)
    b = bundle(db, m)
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
    comments = db.execute(
        "SELECT c.*, u.username FROM comments c JOIN users u ON u.id = c.user_id "
        "WHERE c.market_id = ? ORDER BY c.id DESC", (mid,)).fetchall()
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
            my_positions.append({"outcome": o, "shares": p["shares"], "cost": p["cost"], "value": value})
    block_reason = trade_block_reason(db, g.user["id"], mid) if g.user else None
    blocked, all_users = [], []
    if g.user and g.user["is_admin"]:
        blocked = db.execute("SELECT u.id, u.username FROM market_blocks mb JOIN users u ON u.id = mb.user_id "
                             "WHERE mb.market_id = ? ORDER BY u.username", (mid,)).fetchall()
        all_users = [r["username"] for r in db.execute("SELECT username FROM users ORDER BY username")]
    creator = creator_name(db, m)
    my_entry_fee = entry_fee_due(db, m, g.user["id"]) if g.user else (ENTRY_FEE if creator else 0.0)
    return render_template("market.html", b=b, history=history, trades=trades, comments=comments,
                           my_positions=my_positions, block_reason=block_reason,
                           creator=creator, my_entry_fee=my_entry_fee,
                           blocked=blocked, all_users=all_users, clarifications=clarifications,
                           disputes=disputes, proposer=proposer, can_dispute=can_dispute)


@app.post("/market/<int:mid>/dispute")
@login_required
def dispute(mid):
    try:
        add_dispute(get_db(), g.user["id"], mid, request.form.get("reason", "").strip())
        flash("Dispute sent. The result won't finalize until an admin reviews it.", "success")
    except TradeError as e:
        flash(str(e), "error")
    return redirect(url_for("market", mid=mid))


@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html", window_hours=DISPUTE_HOURS)


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
        flash(execute_trade(get_db(), g.user["id"], mid, outcome_id, request.form.get("action"), value), "success")
    except TradeError as e:
        flash(str(e), "error")
    return redirect(url_for("market", mid=mid))


@app.post("/market/<int:mid>/comment")
@login_required
def comment(mid):
    body = request.form.get("body", "").strip()
    if not body:
        flash("Comment can't be empty.", "error")
    elif len(body) > 500:
        flash("Keep comments under 500 characters.", "error")
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
            block = trade_block_reason(db, g.user["id"], mid)
            if block:
                raise TradeError(block)
            pos = db.execute("SELECT shares FROM positions WHERE user_id = ? AND outcome_id = ?",
                             (g.user["id"], oid)).fetchone()
            balance, held = g.user["balance"], pos["shares"] if pos else 0.0
            fee = entry_fee_due(db, m, g.user["id"]) if action == "buy" else 0.0
        else:
            fee = ENTRY_FEE if action == "buy" and m["created_by"] is not None else 0.0
        shares, coins = check_trade(m, outs, i, action, value, balance, held, fee)
    except TradeError as e:
        return jsonify(ok=False, error=str(e))
    q = [o["shares"] for o in outs]
    q[i] += shares if action == "buy" else -shares
    return jsonify(ok=True, shares=round(shares, 4), coins=coins, entry_fee=fee, creator=creator_name(db, m),
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
        item = {"r": r, "b": b, "price": price, "value": value}
        (active if b["m"]["status"] == "open" else settled).append(item)
    trades = db.execute(
        "SELECT t.*, o.label, m.question FROM trades t JOIN outcomes o ON o.id = t.outcome_id "
        "JOIN markets m ON m.id = t.market_id WHERE t.user_id = ? ORDER BY t.id DESC LIMIT 30", (uid,)).fetchall()
    worth = net_worths(db)[uid]
    # Joint per-market value, so the KPI matches net worth even when someone holds several outcomes of a market.
    invested = sum(open_book_values(db).get(uid, {}).values())
    return render_template("portfolio.html", active=active, settled=settled, trades=trades, worth=worth,
                           invested=invested, rows_total=sum(i["value"] for i in active))


ACTIVITY_FILTERS = {
    "": None,
    "trades": ("buy", "sell"),
    "results": ("payout", "loss", "refund"),
    "fees": ("fee", "earning"),
    "other": ("signup", "bonus", "admin"),
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
def leaderboard():
    db = get_db()
    worth = net_worths(db)
    counts = {r["user_id"]: r["n"] for r in db.execute("SELECT user_id, COUNT(*) AS n FROM trades GROUP BY user_id")}
    users = db.execute("SELECT id, username, balance FROM users").fetchall()
    try:
        roster = load_login_directory()
    except (OSError, ValueError, json.JSONDecodeError):
        roster = {}
    by_name = {u["username"].casefold(): u for u in users}
    board_by_name = {}
    for key, entry in roster.items():
        u = by_name.get(key)
        if u:
            board_by_name[key] = {"username": u["username"], "worth": worth[u["id"]],
                                  "balance": u["balance"], "trades": counts.get(u["id"], 0),
                                  "is_me": g.user is not None and g.user["id"] == u["id"]}
        else:
            board_by_name[key] = {"username": entry["name"], "worth": STARTING_BALANCE,
                                  "balance": STARTING_BALANCE, "trades": 0,
                                  "is_me": False}
    for u in users:
        key = u["username"].casefold()
        if key not in board_by_name:
            board_by_name[key] = {"username": u["username"], "worth": worth[u["id"]],
                                  "balance": u["balance"], "trades": counts.get(u["id"], 0),
                                  "is_me": g.user is not None and g.user["id"] == u["id"]}
    board = sorted(board_by_name.values(), key=lambda x: -x["worth"])
    return render_template("leaderboard.html", board=board)


# ---------------------------------------------------------------- routes: admin

@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    markets = [bundle(db, m) for m in db.execute("SELECT * FROM markets WHERE status = 'open' ORDER BY closes_at")]
    settled = [bundle(db, m) for m in db.execute(
        "SELECT * FROM markets WHERE status != 'open' ORDER BY resolved_at DESC")]
    users = db.execute("SELECT u.*, (tb.user_id IS NOT NULL) AS banned FROM users u "
                       "LEFT JOIN trade_bans tb ON tb.user_id = u.id ORDER BY u.username").fetchall()
    proposals =[dict(p, outcomes=json.loads(p["outcomes"])) for p in db.execute(
        "SELECT p.*, u.username FROM proposals p JOIN users u ON u.id = p.user_id "
        "WHERE p.status = 'pending' ORDER BY p.id")]
    default_close = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
    return render_template("admin.html", markets=markets, settled=settled, users=users,
                           proposals=proposals, default_close=default_close)


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
        flash(f"Market created, but your opening bet failed: {e}", "error")
        return redirect(url_for("market", mid=mid))
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
        if not error:
            pick, stake, error = read_stake(request.form, fields["labels"])
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
        flash(f"Result proposed. It finalizes in {DISPUTE_HOURS} hours unless someone disputes it.", "success")
    except (TradeError, ValueError) as e:
        flash(str(e) or "Pick a winning outcome.", "error")
    return redirect(request.referrer or url_for("market", mid=mid))


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
            flash("Proposed result withdrawn. You can propose a different one.", "success")
        elif request.form.get("decision") == "confirm":
            if m["proposed_outcome_id"] is None or not open_disputes(db, mid):
                raise TradeError("Only a disputed result needs confirming; others finalize on their own.")
            n = resolve_market(db, mid, m["proposed_outcome_id"], g.user["id"])
            flash(f"Result confirmed. Paid out {n} winning position(s).", "success")
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
        flash("Clarification added to the rules.", "success")
    return redirect(request.referrer or url_for("market", mid=mid))


@app.post("/admin/markets/<int:mid>/cancel")
@admin_required
def admin_cancel(mid):
    try:
        cancel_market(get_db(), mid)
        flash("Market cancelled and bets refunded.", "success")
    except TradeError as e:
        flash(str(e), "error")
    return redirect(request.referrer or url_for("admin"))


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
        delete_market(get_db(), mid)
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
        flash(f"{u['username']} can no longer bet on this market."
              + (f" Refunded Ⓜ{refund:,.2f} and removed their bets." if refund else ""), "success")
    return redirect(url_for("market", mid=mid) + "#admin")


@app.post("/admin/markets/<int:mid>/unblock/<int:uid>")
@admin_required
def admin_unblock(mid, uid):
    get_db().execute("DELETE FROM market_blocks WHERE market_id = ? AND user_id = ?", (mid, uid))
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
        flash(f"{u['username']} can trade again.", "success")
    else:
        refund = ban_user_from_trading(db, uid)
        flash(f"Trading turned off for {u['username']}."
              + (f" Refunded Ⓜ{refund:,.2f} from their open bets." if refund else ""), "success")
    return redirect(url_for("admin") + "#users")


@app.post("/admin/users/<int:uid>/grant")
@admin_required
def admin_grant(uid):
    try:
        amount = cents(float(request.form.get("amount", "")))
        if not math.isfinite(amount) or amount == 0 or abs(amount) > 100000:
            raise ValueError
    except ValueError:
        flash("Enter an amount between -100,000 and 100,000.", "error")
        return redirect(url_for("admin"))
    if not can_manage(uid):
        flash(NOT_ALLOWED, "error")
        return redirect(url_for("admin") + "#users")
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
    flash(f"Adjusted balance by {change:,.2f}.", "success")
    return redirect(url_for("admin") + "#users")


@app.post("/admin/users/<int:uid>/toggle-admin")
@admin_required
def admin_toggle(uid):
    if not is_owner():
        flash("Only the site owner can add or remove admins.", "error")
    elif uid == g.user["id"]:
        flash("You can't remove your own admin access.", "error")
    else:
        get_db().execute("UPDATE users SET is_admin = 1 - is_admin WHERE id = ?", (uid,))
    return redirect(url_for("admin") + "#users")


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def error_page(e):
    return render_template("error.html", error=e), e.code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=os.environ.get("FLASK_DEBUG") == "1")
