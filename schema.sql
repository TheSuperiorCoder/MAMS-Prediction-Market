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
    created_at    TEXT NOT NULL
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
    resolved_by         INTEGER
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
    created_at TEXT NOT NULL
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
    admin_id   INTEGER NOT NULL REFERENCES users(id),
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
