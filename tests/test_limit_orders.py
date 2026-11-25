import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MAMS_DB"] = os.path.join(tempfile.mkdtemp(), "test_limit.db")

import lmsr  # noqa: E402
import app as appmod  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    old_db = appmod.DB_PATH
    appmod.DB_PATH = str(tmp_path / "limit.db")
    monkeypatch.setattr(appmod, "OWNER_USERNAME", "")
    appmod.init_db()
    appmod.app.config["TESTING"] = True
    try:
        yield appmod.app.test_client()
    finally:
        appmod.DB_PATH = old_db


def future(days=2):
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def csrf(resp):
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', resp.get_data(as_text=True)).group(1)


def make_user(name, balance=1000.0, is_admin=0):
    db = appmod.connect()
    cur = db.execute(
        "INSERT INTO users (username, password_hash, balance, is_admin, created_at, email, email_verified) "
        "VALUES (?, ?, ?, ?, ?, ?, 1)",
        (name, appmod.generate_password_hash("secret1"), balance, is_admin, appmod.now_str(), f"{name}@example.com"),
    )
    uid = cur.lastrowid
    appmod.log_activity(db, uid, "signup", balance, None, "Starting balance")
    db.commit()
    db.close()
    return uid


def login_as(client, uid):
    with client.session_transaction() as sess:
        sess.permanent = True
        sess["uid"] = uid


def make_market(labels=("Yes", "No"), b=100.0, ou_line=None, created_by=None):
    db = appmod.connect()
    mid = appmod.create_market(
        db, "Will it snow?", "Resolves YES if it snows.", "Weather",
        future(), list(labels), b, created_by=created_by, ou_line=ou_line,
    )
    outs = appmod.get_outcomes(db, mid)
    db.close()
    return mid, [o["id"] for o in outs]


# ---------------------------------------------------------------- LMSR helpers

def test_lmsr_max_shares_respects_target_price():
    q_centi = [0, 0]  # 50% / 50%
    b = 100.0
    sc_buy = lmsr.max_buy_shares_to_price_centi(q_centi, b, 0, 0.60)
    assert sc_buy > 0
    p_after = lmsr.prices([(q_centi[0] + sc_buy) / 100.0, 0.0], b)[0]
    assert p_after <= 0.60 + 1e-12
    p_one_more = lmsr.prices([(q_centi[0] + sc_buy + 1) / 100.0, 0.0], b)[0]
    assert p_one_more > 0.60

    q_high = [5000, 0]  # ~62.2%
    sc_sell = lmsr.max_sell_shares_to_price_centi(q_high, b, 0, 0.55)
    assert sc_sell > 0
    p_sell_after = lmsr.prices([(q_high[0] - sc_sell) / 100.0, 0.0], b)[0]
    assert p_sell_after >= 0.55 - 1e-12


# ---------------------------------------------------------------- Limit order mechanics

def test_buy_limit_does_not_lock_cash_and_triggers_on_price_drop(env):
    alice = make_user("alice", 500.0)
    bob = make_user("bob", 1000.0)
    mid, (yes_id, no_id) = make_market(b=100.0)

    db = appmod.connect()
    # Market starts at 50%. Alice places a buy limit for Yes at 40% for 100 coins.
    msg = appmod.place_limit_order(db, alice, mid, yes_id, "buy", 100.0, 0.40)
    assert "Limit buy placed" in msg
    # Spendable balance is NOT reduced.
    assert db.execute("SELECT balance FROM users WHERE id = ?", (alice,)).fetchone()["balance"] == pytest.approx(500.0)
    lo = db.execute("SELECT * FROM limit_orders WHERE user_id = ?", (alice,)).fetchone()
    assert lo["status"] == "open"
    assert lo["remaining"] == pytest.approx(100.0)

    # Bob buys a lot of No, pushing Yes well below 40%.
    appmod.execute_trade(db, bob, mid, no_id, "buy", 150.0)

    # Alice's buy limit should have triggered and filled (fully or partially up to 40%).
    pos = db.execute("SELECT shares FROM positions WHERE user_id = ? AND outcome_id = ?", (alice, yes_id)).fetchone()
    assert pos is not None and pos["shares"] > 0
    alice_bal = db.execute("SELECT balance FROM users WHERE id = ?", (alice,)).fetchone()["balance"]
    assert alice_bal < 500.0
    # Post-fill Yes price must not exceed Alice's 40% limit.
    outs = appmod.get_outcomes(db, mid)
    yes_price = lmsr.prices([o["shares"] for o in outs], 100.0)[0]
    assert yes_price <= 0.40 + 1e-9
    # Trade recorded with via='limit'.
    tr = db.execute("SELECT via FROM trades WHERE user_id = ? AND outcome_id = ?", (alice, yes_id)).fetchone()
    assert tr["via"] == "limit"
    db.close()


def test_buy_limit_partial_fill_leaves_remainder_open(env):
    alice = make_user("alice", 1000.0)
    bob = make_user("bob", 1000.0)
    mid, (yes_id, no_id) = make_market(b=100.0)

    db = appmod.connect()
    # Alice wants to spend 300 at <= 45%.
    appmod.place_limit_order(db, alice, mid, yes_id, "buy", 300.0, 0.45)
    # Bob buys enough No to push Yes just below 45% (~38%), so Alice's 300 cannot all fit under 45%.
    appmod.execute_trade(db, bob, mid, no_id, "buy", 50.0)

    lo = db.execute("SELECT * FROM limit_orders WHERE user_id = ?", (alice,)).fetchone()
    assert lo["status"] == "open"
    assert 0 < lo["remaining"] < 300.0
    outs = appmod.get_outcomes(db, mid)
    assert lmsr.prices([o["shares"] for o in outs], 100.0)[0] <= 0.45 + 1e-9
    db.close()


def test_buy_limit_never_puts_user_into_debt_and_stays_open(env):
    alice = make_user("alice", 20.0)
    bob = make_user("bob", 1000.0)
    mid, (yes_id, no_id) = make_market(b=100.0)

    db = appmod.connect()
    # Alice places a 100-coin buy limit at 45% despite only having 20 coins.
    appmod.place_limit_order(db, alice, mid, yes_id, "buy", 100.0, 0.45)
    # Alice spends all her 20 coins elsewhere so balance is 0.
    db.execute("UPDATE users SET balance = 0.0 WHERE id = ?", (alice,))
    db.commit()

    # Bob pushes Yes below 45%. Alice has 0 cash, so order must skip and stay open without debt.
    appmod.execute_trade(db, bob, mid, no_id, "buy", 100.0)
    assert db.execute("SELECT balance FROM users WHERE id = ?", (alice,)).fetchone()["balance"] == pytest.approx(0.0)
    lo = db.execute("SELECT * FROM limit_orders WHERE user_id = ?", (alice,)).fetchone()
    assert lo["status"] == "open"
    assert lo["remaining"] == pytest.approx(100.0)

    # Later Alice gets 15 coins; another small trade triggers a partial fill of up to 15 coins, still no debt.
    db.execute("UPDATE users SET balance = 15.0 WHERE id = ?", (alice,))
    db.commit()
    appmod.execute_trade(db, bob, mid, no_id, "buy", 5.0)
    bal_after = db.execute("SELECT balance FROM users WHERE id = ?", (alice,)).fetchone()["balance"]
    assert bal_after >= 0.0
    assert bal_after < 15.0
    lo = db.execute("SELECT * FROM limit_orders WHERE user_id = ?", (alice,)).fetchone()
    assert lo["status"] == "open"
    assert lo["remaining"] < 100.0
    db.close()


def test_sell_limit_triggers_when_price_rises(env):
    alice = make_user("alice", 1000.0)
    bob = make_user("bob", 1000.0)
    mid, (yes_id, no_id) = make_market(b=100.0)

    db = appmod.connect()
    # Alice buys Yes at 50%.
    appmod.execute_trade(db, alice, mid, yes_id, "buy", 20.0)
    held_before = db.execute("SELECT shares FROM positions WHERE user_id = ? AND outcome_id = ?",
                             (alice, yes_id)).fetchone()["shares"]
    bal_before = db.execute("SELECT balance FROM users WHERE id = ?", (alice,)).fetchone()["balance"]

    # Alice places a sell limit at 65% for all her shares.
    msg = appmod.place_limit_order(db, alice, mid, yes_id, "sell", held_before, 0.65)
    assert "Limit sell placed" in msg
    assert db.execute("SELECT status FROM limit_orders WHERE user_id = ?", (alice,)).fetchone()["status"] == "open"

    # Bob buys lots of Yes, pushing Yes above 65%.
    appmod.execute_trade(db, bob, mid, yes_id, "buy", 120.0)

    bal_after = db.execute("SELECT balance FROM users WHERE id = ?", (alice,)).fetchone()["balance"]
    assert bal_after > bal_before
    lo = db.execute("SELECT * FROM limit_orders WHERE user_id = ?", (alice,)).fetchone()
    assert lo["remaining"] < held_before
    db.close()


def test_marketable_limit_executes_immediately(env):
    alice = make_user("alice", 1000.0)
    mid, (yes_id, _) = make_market(b=100.0)

    db = appmod.connect()
    # Current Yes price is 50%; placing a buy limit at 60% for 20 coins fills immediately.
    msg = appmod.place_limit_order(db, alice, mid, yes_id, "buy", 20.0, 0.60)
    assert "filled immediately" in msg
    lo = db.execute("SELECT * FROM limit_orders WHERE user_id = ?", (alice,)).fetchone()
    assert lo["status"] == "filled"
    assert lo["remaining"] == pytest.approx(0.0)
    db.close()


def test_limit_orders_on_multichoice_and_over_under(env):
    alice = make_user("alice", 1000.0)
    bob = make_user("bob", 1000.0)
    # Multi-choice (3 outcomes, start at 33.3% each)
    mid_mc, (o1, o2, o3) = make_market(labels=("A", "B", "C"), b=100.0)
    # Over/Under (2 outcomes with ou_line)
    mid_ou, (over_id, under_id) = make_market(labels=("Over 85", "Under 85"), b=100.0, ou_line=85.0)

    db = appmod.connect()
    appmod.place_limit_order(db, alice, mid_mc, o1, "buy", 25.0, 0.25)
    appmod.execute_trade(db, bob, mid_mc, o2, "buy", 150.0)
    pos_mc = db.execute("SELECT shares FROM positions WHERE user_id = ? AND outcome_id = ?", (alice, o1)).fetchone()
    assert pos_mc is not None and pos_mc["shares"] > 0

    appmod.place_limit_order(db, alice, mid_ou, over_id, "buy", 25.0, 0.40)
    appmod.execute_trade(db, bob, mid_ou, under_id, "buy", 120.0)
    pos_ou = db.execute("SELECT shares FROM positions WHERE user_id = ? AND outcome_id = ?", (alice, over_id)).fetchone()
    assert pos_ou is not None and pos_ou["shares"] > 0
    db.close()


# ---------------------------------------------------------------- Web UI & cancellation

def test_web_ui_place_and_cancel_all_in_portfolio(env):
    uid = make_user("alice", 1000.0)
    mid1, (yes1, no1) = make_market(b=100.0)
    mid2, (yes2, no2) = make_market(b=100.0)
    login_as(env, uid)

    # Market page renders Market/Limit switch and hover (i) info tooltip.
    r = env.get(f"/market/{mid1}")
    html = r.get_data(as_text=True)
    assert 'data-order-type="market"' in html
    assert 'data-order-type="limit"' in html
    assert "Market vs Limit orders info" in html
    tok = csrf(r)

    # Place two resting buy limit orders on two markets via POST /market/<mid>/trade.
    r1 = env.post(f"/market/{mid1}/trade", data={
        "csrf_token": tok, "action": "buy", "order_type": "limit",
        "outcome_id": yes1, "value": "50", "limit_pct": "35",
    }, follow_redirects=True)
    assert r1.status_code == 200
    assert "Limit buy placed" in r1.get_data(as_text=True)

    r2 = env.post(f"/market/{mid2}/trade", data={
        "csrf_token": tok, "action": "buy", "order_type": "limit",
        "outcome_id": no2, "value": "40", "limit_pct": "30",
    }, follow_redirects=True)
    assert r2.status_code == 200
    assert "Limit buy placed" in r2.get_data(as_text=True)

    # Portfolio shows both open limit orders and a Cancel all button.
    rp = env.get("/portfolio")
    phtml = rp.get_data(as_text=True)
    assert "Open limit orders" in phtml
    assert "Cancel all" in phtml
    assert "35.0%" in phtml
    assert "30.0%" in phtml
    tok = csrf(rp)

    # Cancel all from portfolio cancels both.
    rc = env.post("/limit-orders/cancel-all", data={"csrf_token": tok}, follow_redirects=True)
    assert rc.status_code == 200
    assert "Cancelled 2 open limit orders" in rc.get_data(as_text=True)

    db = appmod.connect()
    open_cnt = db.execute("SELECT COUNT(*) FROM limit_orders WHERE user_id = ? AND status = 'open'", (uid,)).fetchone()[0]
    assert open_cnt == 0
    db.close()


def test_resolve_and_cancel_market_cancel_open_limits(env):
    alice = make_user("alice", 1000.0)
    mid1, (yes1, _) = make_market(b=100.0)
    mid2, (yes2, _) = make_market(b=100.0)

    db = appmod.connect()
    appmod.place_limit_order(db, alice, mid1, yes1, "buy", 50.0, 0.30)
    appmod.place_limit_order(db, alice, mid2, yes2, "buy", 50.0, 0.30)
    appmod.resolve_market(db, mid1, yes1)
    appmod.cancel_market(db, mid2)
    open_cnt = db.execute("SELECT COUNT(*) FROM limit_orders WHERE status = 'open'").fetchone()[0]
    assert open_cnt == 0
    db.close()


def test_entry_fee_and_late_fee_apply_on_limit_orders(env):
    creator = make_user("creator", 1000.0)
    alice = make_user("alice", 25.0)
    bob = make_user("bob", 1000.0)

    # Market closing in 15 minutes -> max late trading fee tier (x6) + creator entry fee (M10).
    db = appmod.connect()
    soon = (datetime.now() + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    mid = appmod.create_market(
        db, "Closing soon?", "Resolves YES.", "Events", soon,
        ["Yes", "No"], 100.0, created_by=creator,
    )
    yes_id, no_id = [o["id"] for o in appmod.get_outcomes(db, mid)]

    # Alice has M25 and places a M25 limit buy at 60% (immediately marketable).
    # Entry fee (M10) + late fee must be deducted from available cash so Alice never goes negative.
    appmod.place_limit_order(db, alice, mid, yes_id, "buy", 25.0, 0.60)
    alice_bal = db.execute("SELECT balance FROM users WHERE id = ?", (alice,)).fetchone()["balance"]
    assert 0.0 <= alice_bal < 25.0
    fees_alice = db.execute(
        "SELECT SUM(amount) FROM activity WHERE user_id = ? AND kind = 'fee'", (alice,)
    ).fetchone()[0]
    assert fees_alice < -10.0  # M10 entry fee + late fee
    assert db.execute(
        "SELECT fee FROM market_entries WHERE market_id = ? AND user_id = ?", (mid, alice)
    ).fetchone()["fee"] == pytest.approx(appmod.ENTRY_FEE)

    # Creator received both the entry fee and Alice's late trading fee.
    creator_earnings = db.execute(
        "SELECT SUM(amount) FROM activity WHERE user_id = ? AND kind = 'earning'", (creator,)
    ).fetchone()[0]
    assert creator_earnings > appmod.ENTRY_FEE

    # Limit sell also charges late trading fee.
    held = db.execute("SELECT shares FROM positions WHERE user_id = ? AND outcome_id = ?", (alice, yes_id)).fetchone()["shares"]
    appmod.place_limit_order(db, alice, mid, yes_id, "sell", held, 0.40)
    sell_fees = db.execute(
        "SELECT COUNT(*) FROM activity WHERE user_id = ? AND kind = 'fee'", (alice,)
    ).fetchone()[0]
    assert sell_fees >= 3  # entry fee + buy late fee + sell late fee (and any subsequent fill)
    db.close()

