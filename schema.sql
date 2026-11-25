PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    email         TEXT,
    email_verified INTEGER NOT NULL DEFAULT 0,
    balance       REAL NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    last_bonus    TEXT,
    created_at    TEXT NOT NULL,
    session_gen   INTEGER NOT NULL DEFAULT 0   -- bump to sign the user out everywhere
);

CREATE TABLE IF NOT EXISTS login_challenges (
    username     TEXT PRIMARY KEY COLLATE NOCASE,
    code_hash    TEXT NOT NULL,
    expires_at   REAL NOT NULL,
    requested_at REAL NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS markets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    question            TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    category            TEXT NOT NULL DEFAULT 'Other',
    b                   REAL NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open',   -- open | resolved | cancelled
    closes_at           TEXT NOT NULL,
    resolved_outcome_id INTEGER,
    resolved_at         TEXT,
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL,
    -- `description` is "Resolves YES if..."; these two finish the written rules.
    rules_source        TEXT NOT NULL DEFAULT '',
    rules_otherwise     TEXT NOT NULL DEFAULT '',
    -- Optimistic resolution: an admin proposes a result, then it finalizes after a challenge window.
    proposed_outcome_id INTEGER,
    proposed_by         INTEGER,
    proposed_at         TEXT,
    finalizes_at        TEXT,
    resolved_by         INTEGER,
    featured            INTEGER NOT NULL DEFAULT 0   -- admins can pin a market to the top of the home page
);

CREATE TABLE IF NOT EXISTS outcomes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER NOT NULL REFERENCES markets(id),
    label     TEXT NOT NULL,
    shares    REAL NOT NULL DEFAULT 0,
    position  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    user_id    INTEGER NOT NULL REFERENCES users(id),
    outcome_id INTEGER NOT NULL REFERENCES outcomes(id),
    shares     REAL NOT NULL DEFAULT 0,
    cost       REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, outcome_id)
);

-- shares > 0 for a buy, < 0 for a sell. amount = coins spent (negative when received).
CREATE TABLE IF NOT EXISTS trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    market_id  INTEGER NOT NULL REFERENCES markets(id),
    outcome_id INTEGER NOT NULL REFERENCES outcomes(id),
    shares     REAL NOT NULL,
    amount     REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id  INTEGER NOT NULL REFERENCES markets(id),
    prices     TEXT NOT NULL,   -- JSON list, same order as outcomes.position
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id  INTEGER NOT NULL REFERENCES markets(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    hidden     INTEGER NOT NULL DEFAULT 0   -- hidden by an admin: others see a placeholder
);

-- Markets suggested by users, waiting for an admin to approve or reject.
CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    question    TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT 'Other',
    outcomes    TEXT NOT NULL,                     -- JSON list of labels
    closes_at   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    review_note TEXT NOT NULL DEFAULT '',
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TEXT,
    market_id   INTEGER REFERENCES markets(id),
    created_at  TEXT NOT NULL,
    rules_source    TEXT NOT NULL DEFAULT '',
    rules_otherwise TEXT NOT NULL DEFAULT ''
);

-- Traders challenging a proposed result. An unhandled dispute stops it finalizing by itself.
CREATE TABLE IF NOT EXISTS disputes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id  INTEGER NOT NULL REFERENCES markets(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    handled_at TEXT
);

-- Admin notes added to a market's rules after betting started (the rules themselves are then locked).
CREATE TABLE IF NOT EXISTS clarifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id  INTEGER NOT NULL REFERENCES markets(id),
    admin_id   INTEGER REFERENCES users(id),   -- NULL once that admin's account is deleted
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Users an admin has barred from trading in one market (e.g. people who know the answer).
CREATE TABLE IF NOT EXISTS market_blocks (
    market_id  INTEGER NOT NULL REFERENCES markets(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (market_id, user_id)
);

-- Users an admin has barred from trading anywhere on the site.
CREATE TABLE IF NOT EXISTS trade_bans (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id),
    created_at TEXT NOT NULL
);

-- Every change to a user's balance, newest last: the activity log.
-- market_id has no foreign key so the log survives a market being deleted; market_title keeps its name.
CREATE TABLE IF NOT EXISTS activity (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    kind          TEXT NOT NULL,      -- signup | bonus | buy | sell | payout | loss | refund | admin
    amount        REAL NOT NULL,      -- + money in, - money out
    balance_after REAL,               -- NULL for entries backfilled from before the log existed
    market_id     INTEGER,
    market_title  TEXT NOT NULL DEFAULT '',
    detail        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

-- Who has "entered" each market. Your first bet in a market costs ENTRY_FEE, paid to the market's creator.
CREATE TABLE IF NOT EXISTS market_entries (
    market_id  INTEGER NOT NULL REFERENCES markets(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    creator_id INTEGER NOT NULL REFERENCES users(id),
    fee        REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (market_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_activity_user ON activity(user_id, id);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_outcomes_market ON outcomes(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_market   ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_user     ON trades(user_id);
CREATE INDEX IF NOT EXISTS idx_history_market  ON price_history(market_id);
CREATE INDEX IF NOT EXISTS idx_comments_market ON comments(market_id);
CREATE INDEX IF NOT EXISTS idx_disputes_market ON disputes(market_id);
CREATE INDEX IF NOT EXISTS idx_clarifications_market ON clarifications(market_id);

-- Every admin action (Admin -> Admin log). Names are copied in with no foreign keys so the log survives
-- deleted accounts and markets; nothing in the app edits or deletes these rows.
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id     INTEGER,
    actor_name   TEXT NOT NULL,
    action       TEXT NOT NULL,       -- key in AUDIT_ACTIONS
    target_type  TEXT NOT NULL,       -- user | market | comment | proposal | site
    target_id    TEXT NOT NULL DEFAULT '',
    target_label TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);

-- Users an admin has barred from commenting.
CREATE TABLE IF NOT EXISTS comment_mutes (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id),
    created_at TEXT NOT NULL
);

-- Small site-wide settings, e.g. the announcement banner.
CREATE TABLE IF NOT EXISTS site_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Player-to-player loans. The lender's money is held when they offer; the borrower gets it on accepting and owes
-- principal x (1 + rate/100) by due_at. Overdue loans are collected from the borrower's cash automatically, and after
-- a grace period the lender can sell or take over the borrower's bets (see seize_position in app.py).
CREATE TABLE IF NOT EXISTS loans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lender_id   INTEGER NOT NULL REFERENCES users(id),
    borrower_id INTEGER REFERENCES users(id),   -- NULL on an offer anyone can take
    principal   REAL NOT NULL,
    rate        REAL NOT NULL,                  -- flat interest, percent of principal
    days        INTEGER NOT NULL,               -- loan length, counted from acceptance
    note        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'offered', -- offered | active | repaid | declined | cancelled | forgiven
    owed        REAL NOT NULL DEFAULT 0,        -- still to pay back (active loans)
    repaid      REAL NOT NULL DEFAULT 0,
    due_at      TEXT,
    created_at  TEXT NOT NULL,
    accepted_at TEXT,
    closed_at   TEXT,
    min_score   INTEGER,                        -- only borrowers with at least this credit score may take it
    late_at     TEXT,                           -- when the first late fee was added
    penalty     REAL NOT NULL DEFAULT 0,        -- all late fees added so far
    late_fees   INTEGER NOT NULL DEFAULT 0      -- how many daily late fees have been added (at most LOAN_GRACE_DAYS)
);
CREATE INDEX IF NOT EXISTS idx_loans_due ON loans(status, due_at);
CREATE INDEX IF NOT EXISTS idx_loans_lender ON loans(lender_id);
CREATE INDEX IF NOT EXISTS idx_loans_borrower ON loans(borrower_id);

-- ---- Alt detection (see security.py). None of these tables has a foreign key to users: names are copied in and rows
-- survive an account being deleted, so a deleted alt's device and IP keep catching the next alt. Times are epoch seconds.

-- Every IP / device / fingerprint value an account has been seen with. kind: ip | net | device | fp | fp_parts |
-- fp_canvas | fp_webgl | fp_audio | fp_fonts
CREATE TABLE IF NOT EXISTS sightings (
    user_id    INTEGER NOT NULL,
    username   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_sightings_value ON sightings(kind, value);

-- Sign-ups, log-ins and email confirmations, with what the browser looked like.
CREATE TABLE IF NOT EXISTS login_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    username   TEXT NOT NULL,
    event      TEXT NOT NULL,         -- signup | login | login_fail | verify
    ip         TEXT NOT NULL DEFAULT '',
    ua         TEXT NOT NULL DEFAULT '',
    device_id  TEXT NOT NULL DEFAULT '',
    fp_hash    TEXT NOT NULL DEFAULT '',
    fp_json    TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_user ON login_events(user_id, id);
CREATE INDEX IF NOT EXISTS idx_events_ip ON login_events(event, ip, created_at);
CREATE INDEX IF NOT EXISTS idx_events_device ON login_events(event, device_id, created_at);

-- The latest risk assessment per account. status: clear | flagged | held | approved.
-- `ack` lists the signal kinds an admin had seen when approving, so only new kinds re-flag an approved account.
CREATE TABLE IF NOT EXISTS user_risk (
    user_id     INTEGER PRIMARY KEY,
    score       INTEGER NOT NULL DEFAULT 0,
    level       TEXT NOT NULL DEFAULT 'low',
    reasons     TEXT NOT NULL DEFAULT '[]',
    links       TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'clear',
    ack         TEXT NOT NULL DEFAULT '[]',
    reviewed_by TEXT,
    reviewed_at REAL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_status ON user_risk(status, score);

-- Every canonical email ever used. An email can only ever belong to one account; a deleted account's email stays
-- blocked (status 'deleted') until an admin releases it. status: active | deleted | released
CREATE TABLE IF NOT EXISTS email_ledger (
    canonical      TEXT PRIMARY KEY,
    first_username TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    created_at     REAL NOT NULL,
    released_by    TEXT,
    released_at    REAL
);

-- The event log (Admin -> Events): one row per notable thing that happened. No foreign keys, so it survives deleted
-- accounts. severity: info | warn | alert. Never holds passwords, codes or tokens.
CREATE TABLE IF NOT EXISTS security_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    kind       TEXT NOT NULL,
    severity   TEXT NOT NULL DEFAULT 'info',
    user_id    INTEGER,
    username   TEXT NOT NULL DEFAULT '',
    ip         TEXT NOT NULL DEFAULT '',
    ua         TEXT NOT NULL DEFAULT '',
    device_id  TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    path       TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sevents_time ON security_events(created_at);
CREATE INDEX IF NOT EXISTS idx_sevents_kind ON security_events(kind, created_at);
CREATE INDEX IF NOT EXISTS idx_sevents_user ON security_events(user_id, id);
CREATE INDEX IF NOT EXISTS idx_sevents_ip ON security_events(ip);

-- ---- Credit scores (see credit.py). One row per user with the latest score, plus the admin's overrides, and a
-- thin history of how the score moved. No foreign keys, so rows survive account deletion like the event log.
CREATE TABLE IF NOT EXISTS user_credit (
    user_id        INTEGER PRIMARY KEY,
    score          INTEGER NOT NULL,
    tier           TEXT NOT NULL,
    multiplier     REAL NOT NULL DEFAULT 1,
    factors        TEXT NOT NULL DEFAULT '[]',   -- JSON, from credit.compute_credit
    reasons        TEXT NOT NULL DEFAULT '[]',
    caps           TEXT NOT NULL DEFAULT '[]',
    thin           INTEGER NOT NULL DEFAULT 1,
    utilisation    REAL NOT NULL DEFAULT 0,      -- owed / base limit, for the directory and admin filters
    ontime         INTEGER NOT NULL DEFAULT 0,
    late           INTEGER NOT NULL DEFAULT 0,
    defaulted      INTEGER NOT NULL DEFAULT 0,
    adjust         INTEGER NOT NULL DEFAULT 0,   -- admin: manual points added to the score
    adjust_reason  TEXT NOT NULL DEFAULT '',
    frozen         INTEGER NOT NULL DEFAULT 0,   -- admin: may not borrow
    limit_override REAL,                         -- admin: fixed borrowing limit
    reviewed_by    TEXT,
    reviewed_at    TEXT,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_score ON user_credit(score);

CREATE TABLE IF NOT EXISTS credit_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    score      INTEGER NOT NULL,
    tier       TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_history_user ON credit_history(user_id, created_at);

-- Standing buy/sell limit orders. Cash is NOT locked upfront; orders fill up to limit_price when triggered
-- and are skipped (staying open) if filling would put the user into debt or exceed held shares.
CREATE TABLE IF NOT EXISTS limit_orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    market_id   INTEGER NOT NULL REFERENCES markets(id),
    outcome_id  INTEGER NOT NULL REFERENCES outcomes(id),
    action      TEXT NOT NULL,                  -- buy | sell
    limit_price REAL NOT NULL,                  -- target probability (0.01..0.99)
    amount      REAL NOT NULL,                  -- original coins (buy) or shares (sell)
    remaining   REAL NOT NULL,                  -- unfilled coins (buy) or shares (sell)
    status      TEXT NOT NULL DEFAULT 'open',   -- open | filled | cancelled
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_limit_orders_market ON limit_orders(market_id, status);
CREATE INDEX IF NOT EXISTS idx_limit_orders_user ON limit_orders(user_id, status);

