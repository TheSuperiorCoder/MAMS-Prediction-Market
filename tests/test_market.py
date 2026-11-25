import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MAMS_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

import lmsr  # noqa: E402
import app as appmod  # noqa: E402


@pytest.fixture(autouse=True)
def no_configured_owner(monkeypatch):
    """Tests assume the first account is the owner/admin, as on a fresh site with no OWNER_USERNAME set."""
    monkeypatch.setattr(appmod, "OWNER_USERNAME", "")


# ---------------------------------------------------------------- LMSR math

def test_prices_sum_to_one_and_start_equal():
    ps = lmsr.prices([0, 0, 0], 100)
    assert sum(ps) == pytest.approx(1)
    assert ps[0] == pytest.approx(1 / 3)


def test_shares_for_amount_inverts_cost():
    q, b = [10.0, -5.0, 3.0], 100
    s = lmsr.shares_for_amount(q, b, 1, 42.0)
    assert lmsr.cost_for_shares(q, b, 1, s) == pytest.approx(42.0)


def test_buy_then_sell_round_trips():
    q, b = [0.0, 0.0], 100
    s = lmsr.shares_for_amount(q, b, 0, 50)
    q[0] += s
    assert lmsr.sell_value(q, b, 0, s) == pytest.approx(50)


def test_buying_raises_price():
    q, b = [0.0, 0.0], 100
    s = lmsr.shares_for_amount(q, b, 0, 100)
    assert lmsr.prices([s, 0], b)[0] > 0.5


# ---------------------------------------------------------------- web flow

def past(hours=1):
    return (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def csrf(resp):
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', resp.get_data(as_text=True)).group(1)


def signup(client, name):
    db = appmod.connect()
    first_user = db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    is_admin = int((first_user and not appmod.OWNER_USERNAME) or name.lower() == appmod.OWNER_USERNAME
                   or name.lower() in appmod.ADMIN_USERNAMES)
    cur = db.execute(
        "INSERT INTO users (username, password_hash, balance, is_admin, created_at, email, email_verified) "
        "VALUES (?, ?, ?, ?, ?, ?, 1)",
        (name, appmod.generate_password_hash("secret1"), appmod.STARTING_BALANCE, is_admin, appmod.now_str(),
         f"{name}@example.com"))
    appmod.log_activity(db, cur.lastrowid, "signup", appmod.STARTING_BALANCE, None, "Starting balance")
    with client.session_transaction() as user_session:
        user_session.permanent = True
        user_session["uid"] = cur.lastrowid
    db.close()


def test_leaderboard_and_how_it_works_render_for_everyone(monkeypatch, tmp_path):
    old_db = appmod.DB_PATH
    appmod.DB_PATH = str(tmp_path / "public-pages.db")
    appmod.init_db()
    appmod._hits.clear()
    try:
        client = appmod.app.test_client()
        assert "Nobody has signed up yet" in client.get("/leaderboard").get_data(as_text=True)
        for i in range(5):
            signup(appmod.app.test_client(), f"student{i}")
        db = appmod.connect()
        db.execute("INSERT INTO users (username, password_hash, balance, created_at) VALUES ('nevercame', 'x', 1000, 'x')")
        db.close()
        page = client.get("/leaderboard")
        html = page.get_data(as_text=True)
        assert page.status_code == 200
        assert html.count('class="rank"') == 5 and "nevercame" not in html   # unconfirmed, never traded: hidden

        guide = client.get("/how-it-works")
        assert guide.status_code == 200
        assert "Prices are probabilities" in guide.get_data(as_text=True)
        assert 'How it works' in client.get("/").get_data(as_text=True)
    finally:
        appmod.DB_PATH = old_db


def test_full_flow(monkeypatch):
    appmod.app.config["TESTING"] = True
    admin, bob = appmod.app.test_client(), appmod.app.test_client()

    signup(admin, "teacher")  # first user -> admin
    signup(bob, "bob")
    db = appmod.connect()
    assert db.execute("SELECT is_admin FROM users WHERE username='teacher'").fetchone()[0] == 1
    assert db.execute("SELECT is_admin FROM users WHERE username='bob'").fetchone()[0] == 0

    # bob cannot reach admin
    assert bob.get("/admin").status_code == 403

    # admin creates a market (no creation fee, just Ⓜ10 opening bet)
    tok = csrf(admin.get("/admin"))
    closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
    r = admin.post("/admin/markets", data={"stake": "10", "stake_pick": "0", "csrf_token": tok, "question": "Snow day?", "outcomes": "Yes\nNo",
                                           "description": "Resolves YES if the school announces a snow day.", "rules_source": "Principal's email",
                                           "category": "Weather", "closes_at": closes, "liquidity": "medium"})
    assert r.status_code == 302
    mid = db.execute("SELECT id FROM markets WHERE question='Snow day?'").fetchone()[0]
    yes, no = [r[0] for r in db.execute("SELECT id FROM outcomes WHERE market_id=? ORDER BY position", (mid,))]

    # missing csrf is rejected
    assert bob.post(f"/market/{mid}/trade", data={"action": "buy", "outcome_id": yes, "value": 10}).status_code == 400

    # bob bets 100 on Yes (no entry fee)
    tok = csrf(bob.get(f"/market/{mid}"))
    r = bob.post(f"/market/{mid}/trade", data={"csrf_token": tok, "action": "buy", "outcome_id": yes, "value": 100},
                 follow_redirects=True)
    assert "You paid" in r.get_data(as_text=True)
    bal = db.execute("SELECT balance FROM users WHERE username='bob'").fetchone()[0]
    assert bal == pytest.approx(900)
    # admin bet Ⓜ10 opening stake, no creation or entry fee
    assert db.execute("SELECT balance FROM users WHERE username='teacher'").fetchone()[0] == pytest.approx(990)
    shares = db.execute("SELECT p.shares FROM positions p JOIN users u ON u.id = p.user_id "
                        "WHERE p.outcome_id=? AND u.username='bob'", (yes,)).fetchone()[0]
    assert shares > 100  # bought below 1 coin/share

    # can't overspend
    r = bob.post(f"/market/{mid}/trade", data={"csrf_token": tok, "action": "buy", "outcome_id": no, "value": 5000},
                 follow_redirects=True)
    assert "enough" in r.get_data(as_text=True)

    # quote endpoint works
    q = bob.get(f"/api/quote?market={mid}&outcome={no}&action=buy&value=10").get_json()
    assert q["ok"] and q["shares"] > 10

    # pages render
    for path in ["/", "/?status=all", f"/market/{mid}", "/portfolio", "/leaderboard"]:
        assert bob.get(path).status_code == 200, path

    # the result can't be proposed while betting is still open
    tok = csrf(admin.get(f"/market/{mid}"))
    r = admin.post(f"/admin/markets/{mid}/propose-result", data={"csrf_token": tok, "outcome_id": yes},
                   follow_redirects=True)
    assert "closed yet" in r.get_data(as_text=True)

    # after the close, an admin proposes Yes; nobody is paid until the window ends
    db.execute("UPDATE markets SET closes_at = ? WHERE id = ?", (past(), mid))
    admin.post(f"/admin/markets/{mid}/propose-result", data={"csrf_token": tok, "outcome_id": yes})
    assert db.execute("SELECT balance FROM users WHERE username='bob'").fetchone()[0] == pytest.approx(900)
    assert "Proposed result" in bob.get(f"/market/{mid}").get_data(as_text=True)

    # window ends with no dispute -> the next request finalizes and pays 1 per share
    db.execute("UPDATE markets SET finalizes_at = ? WHERE id = ?", (past(), mid))
    bob.get("/")
    bal = db.execute("SELECT balance FROM users WHERE username='bob'").fetchone()[0]
    assert bal == pytest.approx(900 + shares, abs=0.01)

    # no more trading after resolution
    tok = csrf(bob.get(f"/market/{mid}"))
    r = bob.post(f"/market/{mid}/trade", data={"csrf_token": tok, "action": "buy", "outcome_id": yes, "value": 10},
                 follow_redirects=True)
    assert "no longer accepting" in r.get_data(as_text=True)


def test_cancel_refunds():
    db = appmod.connect()
    uid = db.execute("INSERT INTO users (username, password_hash, balance, created_at) VALUES ('cara', 'x', 1000, ?)",
                     (appmod.now_str(),)).lastrowid
    closes = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    mid = appmod.create_market(db, "Cancel me?", "", "Other", closes, ["A", "B", "C"], 50)
    oid = db.execute("SELECT id FROM outcomes WHERE market_id=? AND label='B'", (mid,)).fetchone()[0]
    appmod.execute_trade(db, uid, mid, oid, "buy", 200)
    held = db.execute("SELECT shares FROM positions WHERE user_id=?", (uid,)).fetchone()[0]
    appmod.execute_trade(db, uid, mid, oid, "sell", held / 2)
    appmod.cancel_market(db, mid)
    assert db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0] == pytest.approx(1000, abs=0.02)


def test_delete_refunds_and_removes():
    db = appmod.connect()
    uid = db.execute("INSERT INTO users (username, password_hash, balance, created_at) VALUES ('dan', 'x', 1000, ?)",
                     (appmod.now_str(),)).lastrowid
    closes = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    mid = appmod.create_market(db, "Delete me?", "", "Other", closes, ["Yes", "No"], 100)
    oid = db.execute("SELECT id FROM outcomes WHERE market_id=? AND label='Yes'", (mid,)).fetchone()[0]
    appmod.execute_trade(db, uid, mid, oid, "buy", 300)
    appmod.delete_market(db, mid)
    # Radial LMSR unwind returns market-implied value without minting coins
    assert db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0] == pytest.approx(1000, abs=0.02)
    assert db.execute("SELECT COUNT(*) FROM markets WHERE id=?", (mid,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM positions WHERE user_id=?", (uid,)).fetchone()[0] == 0
    client = appmod.app.test_client()
    signup(client, "admin_check")
    for path in ["/admin", "/"]:
        assert client.get(path).status_code in (200, 403)


def test_admin_edit():
    path = os.path.join(tempfile.mkdtemp(), "edit.db")
    old = appmod.DB_PATH
    appmod.DB_PATH = path
    try:
        appmod.init_db()
        db = appmod.connect()
        closes = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        mid = appmod.create_market(db, "Old?", "", "Other", closes, ["Yes", "No"], 100)
        admin, bob = appmod.app.test_client(), appmod.app.test_client()
        signup(admin, "editor")
        signup(bob, "bettor")
        assert bob.get(f"/admin/markets/{mid}/edit").status_code == 403
        new_close = (datetime.now() + timedelta(days=9)).strftime("%Y-%m-%dT%H:%M")

        # no bets yet: outcomes and liquidity can be replaced
        tok = csrf(admin.get(f"/admin/markets/{mid}/edit"))
        r = admin.post(f"/admin/markets/{mid}/edit", data={
            "csrf_token": tok, "question": "New?", "description": "d", "category": "Sports",
            "closes_at": new_close, "outcomes": "A\nB\nC", "liquidity": "high"}, follow_redirects=True)
        assert "Market updated." in r.get_data(as_text=True)
        m = db.execute("SELECT * FROM markets WHERE id=?", (mid,)).fetchone()
        assert (m["question"], m["category"]) == ("New?", "Sports")
        assert m["b"] == pytest.approx(1000 * 1.5849625007)  # 3 outcomes
        labels = [o["label"] for o in appmod.get_outcomes(db, mid)]
        assert labels == ["A", "B", "C"]

        # after a bet: renames only, liquidity locked
        uid = db.execute("SELECT id FROM users WHERE username='bettor'").fetchone()[0]
        outs = appmod.get_outcomes(db, mid)
        appmod.execute_trade(db, uid, mid, outs[0]["id"], "buy", 50)
        tok = csrf(admin.get(f"/admin/markets/{mid}/edit"))
        data = {"csrf_token": tok, "question": "New?", "description": "", "category": "Sports",
                "closes_at": new_close, "liquidity": "low", "outcomes": "X\nY"}
        data.update({f"label_{o['id']}": l for o, l in zip(outs, ["Alpha", "B", "C"])})
        admin.post(f"/admin/markets/{mid}/edit", data=data)
        assert [o["label"] for o in appmod.get_outcomes(db, mid)] == ["Alpha", "B", "C"]
        assert db.execute("SELECT b FROM markets WHERE id=?", (mid,)).fetchone()[0] == pytest.approx(1000 * 1.5849625007)
        assert db.execute("SELECT COUNT(*) FROM positions WHERE user_id=?", (uid,)).fetchone()[0] == 1
        assert admin.get(f"/market/{mid}").status_code == 200
        assert admin.get("/admin").status_code == 200
    finally:
        appmod.DB_PATH = old


def test_login_lockout_after_repeated_failures():
    client = appmod.app.test_client()
    signup(client, "lockme")
    client = appmod.app.test_client()
    for _ in range(10):
        r = client.post("/login", data={"csrf_token": csrf(client.get("/login")), "username": "lockme",
                                        "password": "wrong"})
        assert "Wrong username or password." in r.get_data(as_text=True)
    r = client.post("/login", data={"csrf_token": csrf(client.get("/login")), "username": "lockme",
                                    "password": "secret1"})
    assert "Too many wrong passwords" in r.get_data(as_text=True)
    appmod._hits.clear()
    r = client.post("/login", data={"csrf_token": csrf(client.get("/login")), "username": "lockme",
                                    "password": "secret1"})
    assert r.status_code == 302


def test_email_is_disabled_during_automated_tests(monkeypatch):
    monkeypatch.setitem(appmod.app.config, "TESTING", True)
    with pytest.raises(RuntimeError, match="disabled during automated tests"):
        appmod.send_login_code_email("anyone@example.invalid", "123456")


def test_console_email_backend_prints_code_without_sending(monkeypatch, caplog):
    import logging

    monkeypatch.setitem(appmod.app.config, "TESTING", False)
    monkeypatch.setenv("MAMS_EMAIL_BACKEND", "console")
    monkeypatch.delenv("MAMS_SMTP_HOST", raising=False)
    with appmod.app.app_context(), caplog.at_level(logging.WARNING, logger="app"):
        appmod.send_login_code_email("anyone@example.invalid", "123456")
    assert "LOCAL VERIFICATION CODE for anyone@example.invalid: 123456" in caplog.text


def test_email_can_be_sent_to_any_configured_directory_recipient(monkeypatch):
    sent = []

    class SMTPStub:
        def __init__(self, _host, _port, timeout):
            assert timeout == 15

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def ehlo(self):
            pass

        def starttls(self, context):
            assert context is not None

        def send_message(self, message):
            sent.append(message)

    monkeypatch.setitem(appmod.app.config, "TESTING", False)
    monkeypatch.setenv("MAMS_SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("MAMS_SMTP_FROM", "noreply@example.invalid")
    monkeypatch.setattr(appmod.smtplib, "SMTP", SMTPStub)

    appmod.send_login_code_email("any-directory-user@example.invalid", "123456")
    assert len(sent) == 1
    assert sent[0]["To"] == "any-directory-user@example.invalid"


def test_uses_school_timezone():
    import time as _time
    assert _time.tzname[0] in ("EST", "EDT", "Eastern Standard Time", "Eastern Daylight Time")


def test_proposals_flow():
    path = os.path.join(tempfile.mkdtemp(), "proposals.db")
    old = appmod.DB_PATH
    appmod.DB_PATH = path
    appmod._hits.clear()
    try:
        appmod.init_db()
        db = appmod.connect()
        admin, sam = appmod.app.test_client(), appmod.app.test_client()
        signup(admin, "headadmin")
        signup(sam, "sam")
        closes = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M")

        def propose(question):
            tok = csrf(sam.get("/propose"))
            return sam.post("/propose", data={"stake": "10", "stake_pick": "0", "csrf_token": tok, "question": question, "description": "Resolves YES if a live band plays.",
                                              "outcomes": "Yes\nNo", "category": "Events", "closes_at": closes},
                            follow_redirects=True)

        assert "sent to the admins" in propose("Will the pep rally have a band?").get_data(as_text=True)
        assert sam.get("/admin").status_code == 403
        pid = db.execute("SELECT id FROM proposals WHERE question LIKE 'Will the pep%'").fetchone()[0]
        assert sam.get(f"/admin/proposals/{pid}").status_code == 403

        # admin sees it, with a badge, and approves with edited wording
        page = admin.get("/admin").get_data(as_text=True)
        assert "Will the pep rally have a band?" in page and 'class="badge"' in page
        tok = csrf(admin.get(f"/admin/proposals/{pid}"))
        r = admin.post(f"/admin/proposals/{pid}", data={
            "csrf_token": tok, "decision": "approve", "question": "Will the pep rally have a live band?",
            "description": "Resolves YES if a live band plays.", "outcomes": "Yes\nNo", "category": "Events",
            "closes_at": closes, "liquidity": "high", "review_note": "Clarified wording"}, follow_redirects=True)
        assert "Approved!" in r.get_data(as_text=True)
        p = db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        assert p["status"] == "approved" and p["market_id"]
        m = db.execute("SELECT * FROM markets WHERE id=?", (p["market_id"],)).fetchone()
        assert m["question"] == "Will the pep rally have a live band?" and m["b"] == 1000
        assert "Clarified wording" in sam.get("/propose").get_data(as_text=True)

        # approving twice does nothing
        tok = csrf(admin.get("/admin"))
        admin.post(f"/admin/proposals/{pid}", data={"csrf_token": tok, "decision": "approve", "question": "X",
                                                    "outcomes": "A\nB", "closes_at": closes})
        assert db.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 1

        # rejection with a note
        propose("Bad question?")
        pid2 = db.execute("SELECT id FROM proposals WHERE question='Bad question?'").fetchone()[0]
        tok = csrf(admin.get(f"/admin/proposals/{pid2}"))
        admin.post(f"/admin/proposals/{pid2}", data={"csrf_token": tok, "decision": "reject",
                                                     "review_note": "Too vague"})
        assert db.execute("SELECT status FROM proposals WHERE id=?", (pid2,)).fetchone()[0] == "rejected"
        assert "Too vague" in sam.get("/propose").get_data(as_text=True)

        # withdraw, and the 5-pending cap
        for i in range(5):
            propose(f"Q{i}?")
        assert "already have 5" in propose("One too many?").get_data(as_text=True)
        q0 = db.execute("SELECT id FROM proposals WHERE question='Q0?'").fetchone()[0]
        sam.post(f"/propose/{q0}/withdraw", data={"csrf_token": csrf(sam.get("/propose"))})
        assert db.execute("SELECT COUNT(*) FROM proposals WHERE id=?", (q0,)).fetchone()[0] == 0

        # deleting the approved market still works
        appmod.delete_market(db, p["market_id"])
        for path_ in ["/", "/propose", "/admin", f"/admin/proposals/{pid}"]:
            assert admin.get(path_).status_code == 200, path_
    finally:
        appmod.DB_PATH = old


def test_blocking_and_bans_refund_bets():
    path = os.path.join(tempfile.mkdtemp(), "blocks.db")
    old = appmod.DB_PATH
    appmod.DB_PATH = path
    appmod._hits.clear()
    try:
        appmod.init_db()
        db = appmod.connect()
        admin, eve, fay = (appmod.app.test_client() for _ in range(3))
        signup(admin, "boss")
        signup(eve, "eve")
        signup(fay, "fay")
        uid = lambda n: db.execute("SELECT id FROM users WHERE username=?", (n,)).fetchone()[0]
        bal = lambda n: db.execute("SELECT balance FROM users WHERE username=?", (n,)).fetchone()[0]
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        m1 = appmod.create_market(db, "M1?", "", "Other", closes, ["Yes", "No"], 100)
        m2 = appmod.create_market(db, "M2?", "", "Other", closes, ["Yes", "No"], 100)
        yes1 = appmod.get_outcomes(db, m1)[0]["id"]
        yes2 = appmod.get_outcomes(db, m2)[0]["id"]

        appmod.execute_trade(db, uid("eve"), m1, yes1, "buy", 200)
        appmod.execute_trade(db, uid("fay"), m1, yes1, "buy", 100)
        fay_shares = db.execute("SELECT shares FROM positions WHERE user_id=?", (uid("fay"),)).fetchone()[0]

        # per-market block: eve refunded, her shares leave the market, fay untouched
        tok = csrf(admin.get(f"/market/{m1}"))
        r = admin.post(f"/admin/markets/{m1}/block", data={"csrf_token": tok, "username": "eve"},
                       follow_redirects=True)
        assert "Refunded Ⓜ200.00" in r.get_data(as_text=True)
        assert bal("eve") == pytest.approx(1000)
        assert db.execute("SELECT COUNT(*) FROM trades WHERE user_id=?", (uid("eve"),)).fetchone()[0] == 0
        q = db.execute("SELECT shares FROM outcomes WHERE id=?", (yes1,)).fetchone()[0]
        assert q == pytest.approx(fay_shares)
        assert bal("fay") == pytest.approx(900)
        assert "blocked you from betting" in eve.get(f"/market/{m1}").get_data(as_text=True)
        with pytest.raises(appmod.TradeError):
            appmod.execute_trade(db, uid("eve"), m1, yes1, "buy", 10)
        appmod.execute_trade(db, uid("eve"), m2, yes2, "buy", 10)  # other markets still fine

        # unblock
        tok = csrf(admin.get(f"/market/{m1}"))
        admin.post(f"/admin/markets/{m1}/unblock/{uid('eve')}", data={"csrf_token": tok})
        appmod.execute_trade(db, uid("eve"), m1, yes1, "buy", 10)

        # site-wide ban refunds every open market
        tok = csrf(admin.get("/admin"))
        r = admin.post(f"/admin/users/{uid('eve')}/trading", data={"csrf_token": tok}, follow_redirects=True)
        assert "Trading turned off for eve" in r.get_data(as_text=True)
        assert bal("eve") == pytest.approx(1000)
        with pytest.raises(appmod.TradeError):
            appmod.execute_trade(db, uid("eve"), m2, yes2, "buy", 10)
        assert "Banned" in admin.get("/admin").get_data(as_text=True)

        # lift the ban
        tok = csrf(admin.get("/admin"))
        admin.post(f"/admin/users/{uid('eve')}/trading", data={"csrf_token": tok})
        appmod.execute_trade(db, uid("eve"), m2, yes2, "buy", 10)

        # deleting a market with blocks still works
        block_tok = csrf(admin.get(f"/market/{m2}"))
        admin.post(f"/admin/markets/{m2}/block", data={"csrf_token": block_tok, "username": "fay"})
        appmod.delete_market(db, m2)
        assert admin.get(f"/market/{m1}").status_code == 200
    finally:
        appmod.DB_PATH = old


# ---------------------------------------------------------------- pricing audit regressions

def _fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "pricing.db")
    appmod.DB_PATH = path
    appmod._hits.clear()
    appmod.init_db()
    return appmod.connect()


def _user(db, name, balance=1000):
    return db.execute("INSERT INTO users (username, password_hash, balance, created_at, email, email_verified) "
                      "VALUES (?, 'x', ?, ?, ?, 1)", (name, balance, appmod.now_str(), f"{name}@example.com")).lastrowid


def _market(db, n=2, b=300):
    closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    mid = appmod.create_market(db, f"Q{n}-{b}?", "", "Other", closes, [f"o{i}" for i in range(n)], b)
    return mid, [o["id"] for o in appmod.get_outcomes(db, mid)]


@pytest.mark.parametrize("b", [50, 150, 600])
@pytest.mark.parametrize("n", [2, 8])
@pytest.mark.parametrize("amount", [1e-12, 1.0, 1e5])
def test_shares_for_amount_inverts_cost_at_extremes(b, n, amount):
    q = [0.0] * n
    q[0] = 40 * b  # outcome 1 is priced near e^-40
    s = lmsr.shares_for_amount(q, b, 1, amount)
    assert s >= 0
    assert lmsr.cost_for_shares(q, b, 1, s) == pytest.approx(amount, rel=1e-6, abs=1e-9)


def test_buying_does_not_inflate_net_worth():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        uid = _user(db, "buyer")
        mid, (yes, no) = _market(db, 2, 100)
        appmod.execute_trade(db, uid, mid, yes, "buy", 100)
        assert appmod.net_worths(db)[uid] == pytest.approx(1000, abs=0.02)
    finally:
        appmod.DB_PATH = old


def test_holding_both_sides_is_worth_its_cost():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        uid = _user(db, "hedger")
        mid, (yes, no) = _market(db, 2, 50)
        appmod.execute_trade(db, uid, mid, yes, "buy", 400)
        appmod.execute_trade(db, uid, mid, no, "buy", 400)
        assert appmod.net_worths(db)[uid] == pytest.approx(1000, abs=0.05)
    finally:
        appmod.DB_PATH = old


def test_market_b_scales_with_outcomes():
    assert appmod.market_b(2) == 1000
    assert appmod.market_b(8) == 3000
    assert appmod.market_b(1) == 1000  # never below the yes/no level


def test_form_market_stores_scaled_b():
    old = appmod.DB_PATH
    try:
        _fresh_db()
        admin = appmod.app.test_client()
        signup(admin, "formboss")
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        admin.post("/admin/markets", data={"stake": "10", "stake_pick": "0", "csrf_token": csrf(admin.get("/admin")), "question": "Eight?",
                                           "outcomes": "\n".join("abcdefgh"), "category": "Other",
                                           "description": "The winner is the first letter drawn.",
                                           "closes_at": closes, "liquidity": "medium"})
        db = appmod.connect()
        mid, b = db.execute("SELECT id, b FROM markets WHERE question='Eight?'").fetchone()
        assert b == pytest.approx(3000)
        page = admin.get(f"/admin/markets/{mid}/edit").get_data(as_text=True)
        assert 'name="liquidity"' not in page  # no liquidity setting any more
    finally:
        appmod.DB_PATH = old


def test_quote_matches_trade_and_reports_errors():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        client = appmod.app.test_client()
        signup(client, "quoter")
        uid = db.execute("SELECT id FROM users WHERE username='quoter'").fetchone()[0]
        mid, (yes, no) = _market(db, 2, 100)
        url = lambda o, a, v: f"/api/quote?market={mid}&outcome={o}&action={a}&value={v}"

        assert "Minimum bet" in client.get(url(yes, "buy", 0.5)).get_json()["error"]
        assert "enough" in client.get(url(yes, "buy", 5000)).get_json()["error"]
        assert "only own" in client.get(url(yes, "sell", 5)).get_json()["error"]

        q = client.get(url(yes, "buy", 100)).get_json()
        assert q["ok"]
        bal0 = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]
        appmod.execute_trade(db, uid, mid, yes, "buy", 100)
        held = db.execute("SELECT shares FROM positions WHERE user_id=?", (uid,)).fetchone()[0]
        assert held == pytest.approx(q["shares"], abs=1e-3)
        assert db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0] == pytest.approx(bal0 - q["coins"])

        sq = client.get(url(yes, "sell", held)).get_json()
        assert sq["ok"]
        before = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]
        appmod.execute_trade(db, uid, mid, yes, "sell", held)
        after = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]
        assert after - before == pytest.approx(sq["coins"], abs=0.01)

        # blocked users see the reason; logged-out visitors still get a price preview
        db.execute("INSERT INTO market_blocks (market_id, user_id, created_at) VALUES (?, ?, ?)", (mid, uid, appmod.now_str()))
        assert "blocked" in client.get(url(yes, "buy", 10)).get_json()["error"]
        assert appmod.app.test_client().get(url(yes, "buy", 10)).get_json()["ok"]
    finally:
        appmod.DB_PATH = old


# ---------------------------------------------------------------- Yes/No, rules, optimistic resolution

RULES = "Resolves YES if the official results list MAMS first."


def test_migration_adds_missing_columns_and_is_repeatable():
    import sqlite3
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    old = appmod.DB_PATH
    try:
        raw = sqlite3.connect(path)  # a database from before rules/resolution columns existed
        raw.execute("CREATE TABLE markets (id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL, "
                    "description TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'Other', b REAL NOT NULL, "
                    "status TEXT NOT NULL DEFAULT 'open', closes_at TEXT NOT NULL, resolved_outcome_id INTEGER, "
                    "resolved_at TEXT, created_by INTEGER, created_at TEXT NOT NULL)")
        raw.execute("CREATE TABLE proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
                    "question TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'Other', "
                    "outcomes TEXT NOT NULL, closes_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
                    "review_note TEXT NOT NULL DEFAULT '', reviewed_by INTEGER, reviewed_at TEXT, market_id INTEGER, "
                    "created_at TEXT NOT NULL)")
        raw.execute("INSERT INTO markets (question, b, closes_at, created_at) VALUES ('Old?', 100, '2030-01-01 00:00:00', 'x')")
        raw.commit()
        raw.close()
        appmod.DB_PATH = path
        appmod.init_db()
        appmod.init_db()  # second run changes nothing
        db = appmod.connect()
        cols = {r["name"] for r in db.execute("PRAGMA table_info(markets)")}
        assert {"rules_source", "rules_otherwise", "proposed_outcome_id", "finalizes_at", "resolved_by"} <= cols
        assert "rules_source" in {r["name"] for r in db.execute("PRAGMA table_info(proposals)")}
        assert db.execute("SELECT question, rules_source FROM markets").fetchone()[:] == ("Old?", "")
        db.execute("SELECT * FROM disputes")
        db.execute("SELECT * FROM clarifications")
    finally:
        appmod.DB_PATH = old


def test_market_form_kinds_and_required_rules():
    base = {"question": "Q?", "description": RULES, "closes_at": (datetime.now() + timedelta(days=2)).isoformat(),
            "category": "Events"}
    fields, err = appmod.read_market_form({**base, "kind": "yesno", "outcomes": "ignored\nme"})
    assert err is None and fields["labels"] == ["Yes", "No"] and fields["b"] == 1000
    fields, err = appmod.read_market_form({**base, "kind": "multi", "outcomes": "A\nB\nC"})
    assert err is None and fields["labels"] == ["A", "B", "C"]
    _, err = appmod.read_market_form({**base, "kind": "multi", "outcomes": "Only"})
    assert "2–8" in err
    _, err = appmod.read_market_form({**base, "kind": "yesno", "description": "too short"})
    assert "decided" in err
    fields, _ = appmod.read_market_form({**base, "kind": "yesno", "rules_source": "Results page",
                                         "rules_otherwise": "Postponed means NO."})
    assert (fields["rules_source"], fields["rules_otherwise"]) == ("Results page", "Postponed means NO.")
    assert appmod.is_binary(["Yes", "No"]) and not appmod.is_binary(["Yes", "No", "Maybe"])


def _closed_market_with_bet():
    """A Yes/No market past its close date where `bob` holds Yes. Returns (db, mid, yes, no, admin, bob, eve)."""
    db = _fresh_db()
    admin, bob, eve = (appmod.app.test_client() for _ in range(3))
    signup(admin, "chief")
    signup(bob, "bob")
    signup(eve, "eve")  # never trades
    closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    mid = appmod.create_market(db, "Win?", RULES, "Events", closes, ["Yes", "No"], 100, None)
    yes, no = [o["id"] for o in appmod.get_outcomes(db, mid)]
    bob_id = db.execute("SELECT id FROM users WHERE username='bob'").fetchone()[0]
    appmod.execute_trade(db, bob_id, mid, yes, "buy", 100)
    db.execute("UPDATE markets SET closes_at = ? WHERE id = ?", (past(), mid))
    return db, mid, yes, no, admin, bob, eve


def _post(client, url, **data):
    return client.post(url, data={"csrf_token": csrf(client.get("/")), **data}, follow_redirects=True)


def test_undisputed_result_finalizes_once_and_pays_winners():
    old = appmod.DB_PATH
    try:
        db, mid, yes, no, admin, bob, eve = _closed_market_with_bet()
        shares = db.execute("SELECT p.shares FROM positions p JOIN users u ON u.id = p.user_id "
                        "WHERE p.outcome_id=? AND u.username='bob'", (yes,)).fetchone()[0]
        _post(admin, f"/admin/markets/{mid}/propose-result", outcome_id=yes)
        m = db.execute("SELECT * FROM markets WHERE id=?", (mid,)).fetchone()
        assert m["status"] == "open" and m["proposed_outcome_id"] == yes and m["finalizes_at"] > appmod.now_str()
        bob.get("/")  # window still running: nothing happens
        assert db.execute("SELECT status FROM markets WHERE id=?", (mid,)).fetchone()[0] == "open"
        assert appmod.market_state(m) == "proposed"

        db.execute("UPDATE markets SET finalizes_at = ? WHERE id = ?", (past(), mid))
        bob.get("/")
        bob.get("/")  # a second request must not pay again
        row = db.execute("SELECT status, resolved_outcome_id, resolved_by FROM markets WHERE id=?", (mid,)).fetchone()
        assert (row["status"], row["resolved_outcome_id"]) == ("resolved", yes) and row["resolved_by"]
        assert db.execute("SELECT balance FROM users WHERE username='bob'").fetchone()[0] == pytest.approx(900 + shares, abs=0.01)
    finally:
        appmod.DB_PATH = old


def test_dispute_pauses_finalizing_and_admin_can_confirm_or_change():
    old = appmod.DB_PATH
    try:
        db, mid, yes, no, admin, bob, eve = _closed_market_with_bet()
        bal = lambda: db.execute("SELECT balance FROM users WHERE username='bob'").fetchone()[0]
        _post(admin, f"/admin/markets/{mid}/propose-result", outcome_id=no)

        # people who never traded can't dispute; traders can, once
        assert "Only people who traded" in _post(eve, f"/market/{mid}/dispute", reason="I think it is wrong").get_data(as_text=True)
        assert "Explain the problem" in _post(bob, f"/market/{mid}/dispute", reason="no").get_data(as_text=True)
        page = _post(bob, f"/market/{mid}/dispute", reason="Official page says Yes won").get_data(as_text=True)
        assert "Dispute sent" in page and "Result disputed" in page
        assert "already disputed" in _post(bob, f"/market/{mid}/dispute", reason="Official page says Yes won").get_data(as_text=True)
        assert db.execute("SELECT COUNT(*) FROM disputes").fetchone()[0] == 1

        # window ends but the dispute keeps it from finalizing
        db.execute("UPDATE markets SET finalizes_at = ? WHERE id = ?", (past(), mid))
        bob.get("/")
        assert db.execute("SELECT status FROM markets WHERE id=?", (mid,)).fetchone()[0] == "open"

        # an ordinary user can't use the admin actions
        assert bob.post(f"/admin/markets/{mid}/result-action", data={"csrf_token": csrf(bob.get("/")), "decision": "confirm"}).status_code == 403

        # admin changes the outcome: window restarts, dispute is handled, still unpaid
        _post(admin, f"/admin/markets/{mid}/propose-result", outcome_id=yes)
        m = db.execute("SELECT * FROM markets WHERE id=?", (mid,)).fetchone()
        assert m["proposed_outcome_id"] == yes and m["finalizes_at"] > appmod.now_str()
        assert db.execute("SELECT COUNT(*) FROM disputes WHERE handled_at IS NULL").fetchone()[0] == 0
        assert bal() == pytest.approx(900)

        # a new dispute can be confirmed straight away
        _post(bob, f"/market/{mid}/dispute", reason="Still looks wrong to me")
        _post(admin, f"/admin/markets/{mid}/result-action", decision="confirm")
        assert db.execute("SELECT status FROM markets WHERE id=?", (mid,)).fetchone()[0] == "resolved"
        assert bal() > 900
    finally:
        appmod.DB_PATH = old


def test_confirm_needs_a_dispute_and_retract_removes_proposal():
    old = appmod.DB_PATH
    try:
        db, mid, yes, no, admin, bob, eve = _closed_market_with_bet()
        _post(admin, f"/admin/markets/{mid}/propose-result", outcome_id=yes)
        assert "Only a disputed result" in _post(admin, f"/admin/markets/{mid}/result-action", decision="confirm").get_data(as_text=True)
        assert db.execute("SELECT status FROM markets WHERE id=?", (mid,)).fetchone()[0] == "open"

        _post(admin, f"/admin/markets/{mid}/result-action", decision="retract")
        m = db.execute("SELECT * FROM markets WHERE id=?", (mid,)).fetchone()
        assert m["proposed_outcome_id"] is None and m["finalizes_at"] is None
        assert appmod.market_state(m) == "closed"
        bob.get("/")
        assert db.execute("SELECT status FROM markets WHERE id=?", (mid,)).fetchone()[0] == "open"
        # pages for every state render
        for state_page in ("/", "/?status=closed", f"/market/{mid}", "/portfolio", "/admin"):
            assert (admin if state_page == "/admin" else bob).get(state_page).status_code == 200
    finally:
        appmod.DB_PATH = old


def test_rules_lock_after_first_bet_and_clarifications():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        admin = appmod.app.test_client()
        signup(admin, "ruler")
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        mid = appmod.create_market(db, "Rules?", RULES, "Other", closes, ["Yes", "No"], 100, None, "Results page", "Tie means NO.")
        new_close = (datetime.now() + timedelta(days=4)).strftime("%Y-%m-%dT%H:%M")
        edit = lambda **kw: _post(admin, f"/admin/markets/{mid}/edit", question="Rules?", category="Other",
                                  closes_at=new_close, outcomes="Yes\nNo", liquidity="medium", **kw)

        edit(description="Resolves YES if it rains on Tuesday.", rules_source="Weather app", rules_otherwise="")
        m = db.execute("SELECT * FROM markets WHERE id=?", (mid,)).fetchone()
        assert (m["description"], m["rules_source"], m["rules_otherwise"]) == ("Resolves YES if it rains on Tuesday.", "Weather app", "")

        uid = _user(db, "better")
        appmod.execute_trade(db, uid, mid, appmod.get_outcomes(db, mid)[0]["id"], "buy", 10)
        edit(description="Totally different rules now!!", rules_source="Other", rules_otherwise="Changed")
        m = db.execute("SELECT * FROM markets WHERE id=?", (mid,)).fetchone()
        assert (m["description"], m["rules_source"]) == ("Resolves YES if it rains on Tuesday.", "Weather app")

        _post(admin, f"/admin/markets/{mid}/clarify", body="Drizzle counts as rain.")
        assert "5–500" in _post(admin, f"/admin/markets/{mid}/clarify", body="x").get_data(as_text=True)
        page = admin.get(f"/market/{mid}").get_data(as_text=True)
        assert "Drizzle counts as rain." in page and "Resolves YES if it rains on Tuesday." in page
        assert "Weather app" in page and "chance of Yes" in page
    finally:
        appmod.DB_PATH = old


def test_trade_labels_for_admins_and_creators_and_yesno_layout():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        admin, maker = appmod.app.test_client(), appmod.app.test_client()
        signup(admin, "boss")
        signup(maker, "maker")
        maker_id = db.execute("SELECT id FROM users WHERE username='maker'").fetchone()[0]
        admin_id = db.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        mid = appmod.create_market(db, "Labels?", RULES, "Other", closes, ["Yes", "No"], 100, maker_id)
        yes = appmod.get_outcomes(db, mid)[0]["id"]
        appmod.execute_trade(db, maker_id, mid, yes, "buy", 10)
        appmod.execute_trade(db, admin_id, mid, yes, "buy", 10)
        page = maker.get(f"/market/{mid}").get_data(as_text=True)
        assert ">creator<" in page and ">admin<" in page
        assert "¢" in page and "binary" in page  # Yes/No buttons show cents
        assert "chance" in maker.get("/").get_data(as_text=True)
        multi, _ = _market(db, 3, 100)
        assert "binary" not in maker.get(f"/market/{multi}").get_data(as_text=True).split('class="outcome-pick')[1][:10]
    finally:
        appmod.DB_PATH = old


def test_how_it_works_and_propose_pages_render_logged_out_and_in():
    old = appmod.DB_PATH
    try:
        _fresh_db()
        anon, user = appmod.app.test_client(), appmod.app.test_client()
        assert "Prices are probabilities" in anon.get("/how-it-works").get_data(as_text=True)
        signup(user, "writer")
        page = user.get("/propose").get_data(as_text=True)
        assert 'value="yesno"' in page and "Resolves YES if" in page and "market-preview" in page
    finally:
        appmod.DB_PATH = old


# ---------------------------------------------------------------- owner role

def test_owner_cannot_be_overridden_by_other_admins():
    path = os.path.join(tempfile.mkdtemp(), "owner.db")
    old = appmod.DB_PATH
    appmod.DB_PATH = path
    appmod._hits.clear()
    try:
        appmod.init_db()
        db = appmod.connect()
        owner, helper, kid = (appmod.app.test_client() for _ in range(3))
        signup(owner, "founder")
        signup(helper, "helper")
        signup(kid, "kid")
        uid = lambda n: db.execute("SELECT id FROM users WHERE username=?", (n,)).fetchone()[0]
        row = lambda n: db.execute("SELECT * FROM users WHERE username=?", (n,)).fetchone()
        banned = lambda n: db.execute("SELECT 1 FROM trade_bans WHERE user_id=?", (uid(n),)).fetchone() is not None

        # owner promotes helper
        owner.post(f"/admin/users/{uid('helper')}/toggle-admin", data={"csrf_token": csrf(owner.get("/admin"))})
        assert row("helper")["is_admin"] == 1
        page = owner.get("/admin").get_data(as_text=True)
        assert ">Owner<" in page and "only you can add or remove admins" in page

        # helper can't touch the owner in any way
        tok = csrf(helper.get("/admin"))
        r = helper.post(f"/admin/users/{uid('founder')}/toggle-admin", data={"csrf_token": tok}, follow_redirects=True)
        assert "Only the site owner" in r.get_data(as_text=True)
        assert row("founder")["is_admin"] == 1
        helper.post(f"/admin/users/{uid('founder')}/trading", data={"csrf_token": tok})
        assert not banned("founder")
        helper.post(f"/admin/users/{uid('founder')}/grant", data={"csrf_token": tok, "amount": -1000})
        assert row("founder")["balance"] == pytest.approx(1000)
        closes = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        mid = appmod.create_market(db, "Owner test?", "", "Other", closes, ["Yes", "No"], 100)
        helper.post(f"/admin/markets/{mid}/block", data={"csrf_token": tok, "username": "founder"})
        assert db.execute("SELECT COUNT(*) FROM market_blocks").fetchone()[0] == 0

        # helper can't make new admins
        helper.post(f"/admin/users/{uid('kid')}/toggle-admin", data={"csrf_token": tok})
        assert row("kid")["is_admin"] == 0

        # helper CAN manage regular users
        helper.post(f"/admin/users/{uid('kid')}/trading", data={"csrf_token": tok})
        assert banned("kid")
        helper.post(f"/admin/users/{uid('kid')}/grant", data={"csrf_token": tok, "amount": 50})
        assert row("kid")["balance"] == pytest.approx(1050)

        # owner can demote the helper; owner can't be banned even by themself
        tok = csrf(owner.get("/admin"))
        owner.post(f"/admin/users/{uid('helper')}/toggle-admin", data={"csrf_token": tok})
        assert row("helper")["is_admin"] == 0
        assert helper.get("/admin").status_code == 403
        owner.post(f"/admin/users/{uid('founder')}/trading", data={"csrf_token": tok})
        assert not banned("founder")
    finally:
        appmod.DB_PATH = old


def test_owner_from_env_var(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "owner_env.db")
    old = appmod.DB_PATH
    appmod.DB_PATH = path
    appmod._hits.clear()
    monkeypatch.setattr(appmod, "OWNER_USERNAME", "realowner")
    try:
        appmod.init_db()
        db = appmod.connect()
        first, real = appmod.app.test_client(), appmod.app.test_client()
        signup(first, "earlybird")      # first account, but not the configured owner
        signup(real, "RealOwner")       # matches MAMS_OWNER case-insensitively
        admin_flag = lambda n: db.execute("SELECT is_admin FROM users WHERE username=?", (n,)).fetchone()[0]
        assert admin_flag("earlybird") == 0
        assert admin_flag("RealOwner") == 1
        assert ">Owner<" in real.get("/admin").get_data(as_text=True)
    finally:
        appmod.DB_PATH = old


def test_existing_markets_upgraded_to_deeper_liquidity_without_moving_prices():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        uid = db.execute("INSERT INTO users (username, password_hash, balance, created_at) VALUES ('olduser', 'x', 1000, ?)",
                         (appmod.now_str(),)).lastrowid
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        mid = appmod.create_market(db, "Old shallow?", "Resolves YES if the old market says so.", "Other",
                                   closes, ["Yes", "No", "Maybe"], 300)  # created under the old "medium" setting
        done = appmod.create_market(db, "Already settled?", "Resolves YES if the old market says so.", "Other",
                                    closes, ["Yes", "No"], 300)
        db.execute("UPDATE markets SET status = 'resolved' WHERE id = ?", (done,))
        yes = appmod.get_outcomes(db, mid)[0]["id"]
        appmod.execute_trade(db, uid, mid, yes, "buy", 200)
        q_before = [o["shares"] for o in appmod.get_outcomes(db, mid)]
        prices_before = appmod.lmsr.prices(q_before, 300)
        held = db.execute("SELECT shares FROM positions WHERE user_id=?", (uid,)).fetchone()[0]
        bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]

        appmod.deepen_liquidity(db)
        appmod.deepen_liquidity(db)  # running again changes nothing

        b = db.execute("SELECT b FROM markets WHERE id=?", (mid,)).fetchone()[0]
        assert b == pytest.approx(appmod.market_b(3))
        q_after = [o["shares"] for o in appmod.get_outcomes(db, mid)]
        assert appmod.lmsr.prices(q_after, b) == pytest.approx(prices_before)          # prices unchanged
        assert db.execute("SELECT shares FROM positions WHERE user_id=?", (uid,)).fetchone()[0] == held
        assert db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0] == bal
        assert db.execute("SELECT b FROM markets WHERE id=?", (done,)).fetchone()[0] == 300  # settled: untouched

        # the same bet now moves the price less than it would have before
        shallow = appmod.lmsr.prices([q_before[0] + appmod.lmsr.shares_for_amount(q_before, 300, 0, 100)] + q_before[1:], 300)[0]
        deep = appmod.lmsr.prices([q_after[0] + appmod.lmsr.shares_for_amount(q_after, b, 0, 100)] + q_after[1:], b)[0]
        assert deep - prices_before[0] < (shallow - prices_before[0]) / 2
    finally:
        appmod.DB_PATH = old


# ---------------------------------------------------------------- over/under

def test_ou_labels_and_text():
    assert appmod.ou_labels(85, "%") == ["Over 85%", "Under 85%"]
    assert appmod.ou_labels(90, "minutes") == ["Over 90 minutes", "Under 90 minutes"]
    assert appmod.ou_labels(50.5, "") == ["Over 50.5", "Under 50.5"]
    assert appmod.ou_text(1200, "pts") == "1,200 pts"
    assert appmod.parse_ou({"ou_line": "abc"})[2]
    assert appmod.parse_ou({"ou_line": "nan"})[2]
    assert appmod.parse_ou({"ou_line": " 1,250.5 ", "ou_unit": " points "})[:2] == (1250.5, "points")


def test_over_under_create_trade_and_resolve():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        admin, kid = appmod.app.test_client(), appmod.app.test_client()
        signup(admin, "ouboss")
        signup(kid, "oukid")
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        r = admin.post("/admin/markets", data={"stake": "10", "stake_pick": "0", 
            "csrf_token": csrf(admin.get("/admin")), "kind": "overunder", "ou_line": "85", "ou_unit": "%",
            "question": "Physics test average?", "description": "The class average as announced by the teacher.",
            "category": "Academics", "closes_at": closes, "outcomes": "should be ignored"}, follow_redirects=True)
        assert "Market created" in r.get_data(as_text=True)
        m = db.execute("SELECT * FROM markets WHERE question='Physics test average?'").fetchone()
        assert (m["ou_line"], m["ou_unit"], m["b"]) == (85, "%", 1000)
        over, under = appmod.get_outcomes(db, m["id"])
        assert (over["label"], under["label"]) == ("Over 85%", "Under 85%")

        page = kid.get(f"/market/{m['id']}").get_data(as_text=True)
        assert "Over / Under 85%" in page and "chance of Over 85%" in page
        assert "Over / Under 85%" in kid.get("/").get_data(as_text=True)

        kid_id = db.execute("SELECT id FROM users WHERE username='oukid'").fetchone()[0]
        appmod.execute_trade(db, kid_id, m["id"], over["id"], "buy", 100)
        db.execute("UPDATE markets SET closes_at = ? WHERE id = ?",
                   ((datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"), m["id"]))

        url = f"/admin/markets/{m['id']}/propose-result"
        r = admin.post(url, data={"csrf_token": csrf(admin.get("/admin")), "actual": "85"}, follow_redirects=True)
        assert "exactly on the line" in r.get_data(as_text=True)
        r = admin.post(url, data={"csrf_token": csrf(admin.get("/admin")), "actual": "abc"}, follow_redirects=True)
        assert "Enter the actual number" in r.get_data(as_text=True)

        admin.post(url, data={"csrf_token": csrf(admin.get("/admin")), "actual": "87.3"})
        m = db.execute("SELECT * FROM markets WHERE id=?", (m["id"],)).fetchone()
        assert m["proposed_outcome_id"] == over["id"] and m["ou_result"] == 87.3
        assert "Result: <b>87.3%</b>" in kid.get(f"/market/{m['id']}").get_data(as_text=True)

        # withdrawing clears the number; a lower number picks Under
        admin.post(f"/admin/markets/{m['id']}/result-action",
                   data={"csrf_token": csrf(admin.get("/admin")), "decision": "retract"})
        assert db.execute("SELECT ou_result FROM markets WHERE id=?", (m["id"],)).fetchone()[0] is None
        admin.post(url, data={"csrf_token": csrf(admin.get("/admin")), "actual": "80"})
        assert db.execute("SELECT proposed_outcome_id FROM markets WHERE id=?", (m["id"],)).fetchone()[0] == under["id"]
    finally:
        appmod.DB_PATH = old


def test_over_under_proposal_and_edit():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        admin, kid = appmod.app.test_client(), appmod.app.test_client()
        signup(admin, "ouadmin")
        signup(kid, "ouproposer")
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        assert 'value="overunder"' in kid.get("/propose").get_data(as_text=True)
        kid.post("/propose", data={"stake": "10", "stake_pick": "0", "csrf_token": csrf(kid.get("/propose")), "kind": "overunder", "ou_line": "90",
                                   "ou_unit": "minutes", "question": "How long is the assembly?",
                                   "description": "From scheduled start to dismissal, timed by the admins.",
                                   "category": "Events", "closes_at": closes})
        p = db.execute("SELECT * FROM proposals WHERE question='How long is the assembly?'").fetchone()
        assert (p["ou_line"], p["ou_unit"]) == (90, "minutes")
        review = admin.get(f"/admin/proposals/{p['id']}").get_data(as_text=True)
        assert re.search(r'value="overunder"\s+checked', review) and 'value="90"' in review

        admin.post(f"/admin/proposals/{p['id']}", data={
            "csrf_token": csrf(admin.get(f"/admin/proposals/{p['id']}")), "decision": "approve", "kind": "overunder",
            "ou_line": "90", "ou_unit": "minutes", "question": "How long is the assembly?",
            "description": "From scheduled start to dismissal, timed by the admins.", "category": "Events",
            "closes_at": closes})
        mid = db.execute("SELECT market_id FROM proposals WHERE id=?", (p["id"],)).fetchone()[0]
        assert [o["label"] for o in appmod.get_outcomes(db, mid)] == ["Over 90 minutes", "Under 90 minutes"]

        # the proposer's opening bet (Ⓜ10 on "Over 90 minutes") was placed when it was approved
        kid_id = db.execute("SELECT id FROM users WHERE username='ouproposer'").fetchone()[0]
        pos = db.execute("SELECT o.label FROM positions p JOIN outcomes o ON o.id = p.outcome_id WHERE p.user_id=?",
                         (kid_id,)).fetchall()
        assert [r[0] for r in pos] == ["Over 90 minutes"]

        def edit(market, line, description):
            return admin.post(f"/admin/markets/{market}/edit", data={
                "csrf_token": csrf(admin.get(f"/admin/markets/{market}/edit")), "question": "How long is the assembly?",
                "description": description, "category": "Events", "closes_at": closes, "ou_line": line, "ou_unit": "min"})

        # with only the creator's bet: the line is locked (shares depend on it) but the rules can still be fixed
        rules = lambda market: db.execute("SELECT description FROM markets WHERE id=?", (market,)).fetchone()[0]
        edit(mid, "120", "Timed from the first bell to dismissal by the admins.")
        assert db.execute("SELECT ou_line FROM markets WHERE id=?", (mid,)).fetchone()[0] == 90
        assert rules(mid) == "Timed from the first bell to dismissal by the admins."
        # once someone else bets, the rules lock too
        other = db.execute("INSERT INTO users (username, password_hash, balance, created_at) VALUES ('ouother', 'x', 500, ?)",
                           (appmod.now_str(),)).lastrowid
        appmod.execute_trade(db, other, mid, appmod.get_outcomes(db, mid)[1]["id"], "buy", 50)
        edit(mid, "120", "Changed after betting?")
        assert rules(mid) == "Timed from the first bell to dismissal by the admins."

        # a market with no bets at all can still have its line changed
        fresh = appmod.create_market(db, "How long is the assembly?", RULES, "Events", closes.replace("T", " ") + ":00",
                                     appmod.ou_labels(90, "min"), 1000, None, "", "", 90, "min")
        edit(fresh, "75.5", RULES)
        assert [o["label"] for o in appmod.get_outcomes(db, fresh)] == ["Over 75.5 min", "Under 75.5 min"]
    finally:
        appmod.DB_PATH = old


@pytest.mark.parametrize("yes_means", ["over", "under"])
def test_convert_yes_no_to_over_under_keeps_bets(yes_means):
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        admin = appmod.app.test_client()
        signup(admin, "convboss")
        uid = db.execute("SELECT id FROM users WHERE username='convboss'").fetchone()[0]
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        mid = appmod.create_market(db, "Physics Average +- 85%", RULES, "Academics", closes, ["YES", "NO"], 1000)
        yes, no = appmod.get_outcomes(db, mid)
        appmod.execute_trade(db, uid, mid, yes["id"], "buy", 200)
        yes_price = appmod.bundle(db, db.execute("SELECT * FROM markets WHERE id=?", (mid,)).fetchone())["outcomes"][0]["price"]
        history_before = [json.loads(r[0]) for r in db.execute("SELECT prices FROM price_history WHERE market_id=? ORDER BY id", (mid,))]
        bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]

        r = admin.post(f"/admin/markets/{mid}/convert-ou", data={
            "csrf_token": csrf(admin.get(f"/market/{mid}")), "ou_line": "85", "ou_unit": "%", "yes_means": yes_means},
            follow_redirects=True)
        assert "bets carried over" in r.get_data(as_text=True)

        m = db.execute("SELECT * FROM markets WHERE id=?", (mid,)).fetchone()
        b = appmod.bundle(db, m)
        assert b["ou"]["text"] == "85%" and b["binary"]
        assert [o["label"] for o in b["outcomes"]] == ["Over 85%", "Under 85%"]
        held_label = db.execute("SELECT o.label FROM positions p JOIN outcomes o ON o.id = p.outcome_id "
                                "WHERE p.user_id=?", (uid,)).fetchone()[0]
        assert held_label == ("Over 85%" if yes_means == "over" else "Under 85%")
        side = 0 if yes_means == "over" else 1
        assert b["outcomes"][side]["price"] == pytest.approx(yes_price)      # YES's price moved with its bets
        history_after = [json.loads(r[0]) for r in db.execute("SELECT prices FROM price_history WHERE market_id=? ORDER BY id", (mid,))]
        assert history_after == (history_before if yes_means == "over" else [h[::-1] for h in history_before])
        assert db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0] == bal
        note = db.execute("SELECT body FROM clarifications WHERE market_id=?", (mid,)).fetchone()[0]
        assert "Converted to Over/Under 85%" in note
        assert admin.get(f"/market/{mid}").status_code == 200

        # can't convert twice, or convert multiple-choice markets
        r = admin.post(f"/admin/markets/{mid}/convert-ou", data={
            "csrf_token": csrf(admin.get(f"/market/{mid}")), "ou_line": "90", "yes_means": "over"}, follow_redirects=True)
        assert "already Over/Under" in r.get_data(as_text=True)
        multi = appmod.create_market(db, "Which month?", RULES, "Other", closes, ["Dec", "Jan", "Feb"], 1585)
        r = admin.post(f"/admin/markets/{multi}/convert-ou", data={
            "csrf_token": csrf(admin.get(f"/market/{multi}")), "ou_line": "1", "yes_means": "over"}, follow_redirects=True)
        assert "Only Yes/No markets" in r.get_data(as_text=True)
    finally:
        appmod.DB_PATH = old


# ---------------------------------------------------------------- activity log

def test_activity_log_records_every_balance_change():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        admin, ann, ben = (appmod.app.test_client() for _ in range(3))
        signup(admin, "logboss")
        signup(ann, "ann")
        signup(ben, "ben")
        uid = lambda n: db.execute("SELECT id FROM users WHERE username=?", (n,)).fetchone()[0]
        bal = lambda n: db.execute("SELECT balance FROM users WHERE username=?", (n,)).fetchone()[0]
        log = lambda n: db.execute("SELECT kind, amount, balance_after, detail FROM activity WHERE user_id=? ORDER BY id",
                                   (uid(n),)).fetchall()
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")

        assert [tuple(r)[:3] for r in log("ann")] == [("signup", 1000, 1000)]
        ann.post("/bonus", data={"csrf_token": csrf(ann.get("/"))})
        assert log("ann")[-1]["kind"] == "bonus" and log("ann")[-1]["balance_after"] == 1100

        # buy / sell
        m1 = appmod.create_market(db, "Win?", RULES, "Other", closes, ["Yes", "No"], 1000)
        yes, no = appmod.get_outcomes(db, m1)
        appmod.execute_trade(db, uid("ann"), m1, yes["id"], "buy", 200)
        appmod.execute_trade(db, uid("ben"), m1, no["id"], "buy", 50)
        buy = log("ann")[-1]
        assert buy["kind"] == "buy" and buy["amount"] == -200 and buy["balance_after"] == 900
        assert "Bought" in buy["detail"] and "“Yes”" in buy["detail"]
        held = db.execute("SELECT shares FROM positions WHERE user_id=?", (uid("ann"),)).fetchone()[0]
        msg = appmod.execute_trade(db, uid("ann"), m1, yes["id"], "sell", held / 4)
        sell = log("ann")[-1]
        assert sell["kind"] == "sell" and sell["amount"] > 0 and sell["balance_after"] == pytest.approx(bal("ann"))
        assert "You received" in msg and "Balance:" in msg

        # resolution: winner paid, loser told they got nothing
        db.execute("UPDATE markets SET closes_at = ? WHERE id = ?",
                   ((datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"), m1))
        appmod.resolve_market(db, m1, yes["id"])
        payout = log("ann")[-1]
        assert payout["kind"] == "payout" and payout["amount"] == pytest.approx(held * 0.75, abs=0.01)
        assert payout["balance_after"] == pytest.approx(bal("ann"))
        loss = log("ben")[-1]
        assert loss["kind"] == "loss" and loss["amount"] == 0 and "“No”" in loss["detail"]

        # refunds: cancelled market, and being blocked
        m2 = appmod.create_market(db, "Cancel?", RULES, "Other", closes, ["Yes", "No"], 1000)
        appmod.execute_trade(db, uid("ben"), m2, appmod.get_outcomes(db, m2)[0]["id"], "buy", 40)
        appmod.cancel_market(db, m2)
        assert log("ben")[-1]["kind"] == "refund" and log("ben")[-1]["amount"] == pytest.approx(40, abs=0.02) and "cancelled" in log("ben")[-1]["detail"]
        m3 = appmod.create_market(db, "Block?", RULES, "Other", closes, ["Yes", "No"], 1000)
        appmod.execute_trade(db, uid("ben"), m3, appmod.get_outcomes(db, m3)[0]["id"], "buy", 30)
        appmod.block_user_in_market(db, uid("ben"), m3)
        assert tuple(log("ben")[-1])[:2] == ("refund", 30) and "blocked" in log("ben")[-1]["detail"]

        # admin adjustment records what actually changed (can't go below zero)
        admin.post(f"/admin/users/{uid('ben')}/grant", data={"csrf_token": csrf(admin.get("/admin")), "amount": -99999})
        adj = log("ben")[-1]
        assert adj["kind"] == "admin" and adj["balance_after"] == 0 and adj["amount"] < 0 and "logboss" in adj["detail"]

        # every balance_after matches a running total of the amounts
        for name in ("ann", "ben"):
            running = 0
            for r in log(name):
                running = round(running + r["amount"], 2)
                assert r["balance_after"] == pytest.approx(running, abs=0.02), (name, tuple(r))

        # pages: own log, admin can view others, non-admins can't
        page = ann.get("/activity/mine").get_data(as_text=True)
        assert "Your balance history" in page and "Daily bonus" in page and "Win?" in page
        assert "Your balance history" in ann.get("/activity/mine?user=ben").get_data(as_text=True)   # ignored for non-admins
        other = admin.get("/activity/mine?user=ben").get_data(as_text=True)
        assert "ben&#39;s balance history" in other and "Refund: market cancelled" in other
        assert admin.get("/activity/mine?user=nobody").status_code == 404
        assert "Bought" in ann.get("/activity/mine?show=trades").get_data(as_text=True)
        assert "Daily bonus" not in ann.get("/activity/mine?show=trades").get_data(as_text=True)

        # deleting a market keeps the history
        appmod.delete_market(db, m3)
        assert "Block? (deleted)" in admin.get("/activity/mine?user=ben").get_data(as_text=True)
    finally:
        appmod.DB_PATH = old


def test_activity_backfilled_from_old_trades():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        uid = db.execute("INSERT INTO users (username, password_hash, balance, created_at) VALUES ('vet', 'x', 1000, ?)",
                         (appmod.now_str(),)).lastrowid
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        mid = appmod.create_market(db, "Old trades?", RULES, "Other", closes, ["Yes", "No"], 1000)
        appmod.execute_trade(db, uid, mid, appmod.get_outcomes(db, mid)[0]["id"], "buy", 25)
        db.execute("DELETE FROM activity")           # simulate a database from before the log existed
        appmod.backfill_activity(db)
        rows = db.execute("SELECT kind, amount, balance_after, detail FROM activity WHERE user_id=?", (uid,)).fetchall()
        assert [(r["kind"], r["amount"], r["balance_after"]) for r in rows] == [("buy", -25, None)]
        assert rows[0]["detail"].startswith("Bought ")
        appmod.backfill_activity(db)                 # only runs once
        assert db.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == 1
    finally:
        appmod.DB_PATH = old


def test_activity_feed_shows_every_trade():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        ann, ben, visitor = (appmod.app.test_client() for _ in range(3))
        signup(ann, "feedann")
        signup(ben, "feedben")
        uid = lambda n: db.execute("SELECT id FROM users WHERE username=?", (n,)).fetchone()[0]
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        m1 = appmod.create_market(db, "Feed market one?", RULES, "Other", closes, ["Yes", "No"], 1000)
        m2 = appmod.create_market(db, "Feed market two?", RULES, "Other", closes, ["Yes", "No"], 1000)
        y1, y2 = appmod.get_outcomes(db, m1)[0]["id"], appmod.get_outcomes(db, m2)[0]["id"]
        appmod.execute_trade(db, uid("feedann"), m1, y1, "buy", 60)
        appmod.execute_trade(db, uid("feedben"), m2, y2, "buy", 40)
        held = db.execute("SELECT shares FROM positions WHERE user_id=?", (uid("feedann"),)).fetchone()[0]
        appmod.execute_trade(db, uid("feedann"), m1, y1, "sell", held / 2)
        ann.post("/bonus", data={"csrf_token": csrf(ann.get("/"))})   # not a trade: must not appear

        page = visitor.get("/activity").get_data(as_text=True)          # public, no login needed
        assert page.count('class="kind kind-') == 3
        assert "feedann" in page and "feedben" in page and "Feed market two?" in page
        assert "Daily bonus" not in page and "Starting balance" not in page
        assert ">3<" in page                                              # trade count

        only_ann = visitor.get("/activity?trader=feedann").get_data(as_text=True)
        assert only_ann.count('class="kind kind-') == 2 and "Feed market two?" not in only_ann.split("Showing only")[1]
        only_m2 = visitor.get(f"/activity?market={m2}").get_data(as_text=True)
        assert only_m2.count('class="kind kind-') == 1
        assert visitor.get("/activity?show=sells").get_data(as_text=True).count('class="kind kind-sell"') == 1
        assert visitor.get("/activity?show=buys").get_data(as_text=True).count('class="kind kind-') == 2

        # trades stay in the feed after their market is deleted
        appmod.delete_market(db, m2)
        assert "Feed market two? (deleted)" in visitor.get("/activity").get_data(as_text=True)
        assert visitor.get("/activity?market=abc").status_code == 404
    finally:
        appmod.DB_PATH = old


def test_activity_feed_pages_back_through_history():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        uid = db.execute("INSERT INTO users (username, password_hash, balance, created_at) VALUES ('pager', 'x', 100000, ?)",
                         (appmod.now_str(),)).lastrowid
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        mid = appmod.create_market(db, "Busy?", RULES, "Other", closes, ["Yes", "No"], 1000)
        yes = appmod.get_outcomes(db, mid)[0]["id"]
        for _ in range(appmod.FEED_PAGE + 5):
            appmod.execute_trade(db, uid, mid, yes, "buy", 1)
        client = appmod.app.test_client()
        first = client.get("/activity").get_data(as_text=True)
        assert first.count('class="kind kind-') == appmod.FEED_PAGE and "Older trades" in first
        before = re.search(r"before=(\d+)", first).group(1)
        second = client.get(f"/activity?before={before}").get_data(as_text=True)
        assert second.count('class="kind kind-') == 5 and "Older trades" not in second
    finally:
        appmod.DB_PATH = old


# ---------------------------------------------------------------- fees & dispute window

def test_dispute_window_is_two_hours():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        assert appmod.DISPUTE_WINDOW == timedelta(hours=2) and appmod.DISPUTE_HOURS == 2
        aid = db.execute("INSERT INTO users (username, password_hash, balance, is_admin, created_at) VALUES ('w', 'x', 0, 1, ?)",
                         (appmod.now_str(),)).lastrowid
        past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        mid = appmod.create_market(db, "Window?", RULES, "Other", past, ["Yes", "No"], 1000)
        appmod.propose_result(db, mid, appmod.get_outcomes(db, mid)[0]["id"], aid)
        fin = appmod.parse_dt(db.execute("SELECT finalizes_at FROM markets WHERE id=?", (mid,)).fetchone()[0])
        assert timedelta(minutes=119) < fin - datetime.now() <= timedelta(hours=2)
        assert "2 hours" in appmod.app.test_client().get("/how-it-works").get_data(as_text=True)
    finally:
        appmod.DB_PATH = old


def test_no_creation_fee_for_proposals_and_admin_markets():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        admin, kid = appmod.app.test_client(), appmod.app.test_client()
        signup(admin, "feeboss")
        signup(kid, "feekid")
        bal = lambda n: db.execute("SELECT balance FROM users WHERE username=?", (n,)).fetchone()[0]
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        form = lambda q: {"kind": "yesno", "question": q, "description": RULES, "category": "Events", "closes_at": closes}

        def propose(q):
            return kid.post("/propose", data={"stake": "10", "stake_pick": "0", "csrf_token": csrf(kid.get("/propose")), **form(q)}, follow_redirects=True)

        assert "No proposal fee" in kid.get("/propose").get_data(as_text=True)
        assert "opening bet is held" in propose("Kept?").get_data(as_text=True)
        assert bal("feekid") == 990          # only Ⓜ10 opening bet held, zero fee
        propose("Withdrawn?")
        assert bal("feekid") == 980

        # withdrawing or rejecting refunds the held opening bet in full
        pid = db.execute("SELECT id FROM proposals WHERE question='Withdrawn?'").fetchone()[0]
        r = kid.post(f"/propose/{pid}/withdraw", data={"csrf_token": csrf(kid.get("/propose"))}, follow_redirects=True)
        assert "Ⓜ10.00 was refunded" in r.get_data(as_text=True) and bal("feekid") == 990
        pid = db.execute("SELECT id FROM proposals WHERE question='Kept?'").fetchone()[0]
        admin.post(f"/admin/proposals/{pid}", data={"csrf_token": csrf(admin.get(f"/admin/proposals/{pid}")),
                                                     "decision": "reject", "review_note": "Too vague"})
        assert bal("feekid") == 1000

        # approving places the held opening bet
        propose("Approved?")
        pid = db.execute("SELECT id FROM proposals WHERE question='Approved?'").fetchone()[0]
        admin.post(f"/admin/proposals/{pid}", data={"csrf_token": csrf(admin.get(f"/admin/proposals/{pid}")),
                                                     "decision": "approve", **form("Approved?")})
        assert bal("feekid") == 990 and bal("feeboss") == 1000
        mid = db.execute("SELECT market_id FROM proposals WHERE id=?", (pid,)).fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM trades WHERE market_id=?", (mid,)).fetchone()[0] == 1

        # can't propose without enough for the opening bet
        db.execute("UPDATE users SET balance = 5 WHERE username='feekid'")
        assert "requires a Ⓜ10.00 opening bet" in propose("Broke?").get_data(as_text=True)
        assert db.execute("SELECT COUNT(*) FROM proposals WHERE question='Broke?'").fetchone()[0] == 0
        assert bal("feekid") == 5

        # admins pay no creation fee either, just their opening bet
        r = admin.post("/admin/markets", data={"stake": "10", "stake_pick": "0", "csrf_token": csrf(admin.get("/admin")), **form("Admin made?")},
                       follow_redirects=True)
        assert "opening bet on “Yes”" in r.get_data(as_text=True) and bal("feeboss") == 990
        db.execute("UPDATE users SET balance = 5 WHERE username='feeboss'")
        r = admin.post("/admin/markets", data={"stake": "10", "stake_pick": "0", "csrf_token": csrf(admin.get("/admin")), **form("Too poor?")},
                       follow_redirects=True)
        assert "You need Ⓜ10.00" in r.get_data(as_text=True)
        assert db.execute("SELECT COUNT(*) FROM markets WHERE question='Too poor?'").fetchone()[0] == 0
    finally:
        appmod.DB_PATH = old


def test_no_entry_fee_on_trades():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        maker, trader = appmod.app.test_client(), appmod.app.test_client()
        signup(maker, "qmaker")
        signup(trader, "qtrader")
        uid = lambda n: db.execute("SELECT id FROM users WHERE username=?", (n,)).fetchone()[0]
        bal = lambda n: db.execute("SELECT balance FROM users WHERE username=?", (n,)).fetchone()[0]
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        mid = appmod.create_market(db, "No entry fee?", RULES, "Other", closes, ["Yes", "No"], 1000, uid("qmaker"))
        yes = appmod.get_outcomes(db, mid)[0]["id"]

        q = trader.get(f"/api/quote?market={mid}&outcome={yes}&action=buy&value=50").get_json()
        assert q["ok"] and q["entry_fee"] == 0 and q["coins"] == 50
        appmod.execute_trade(db, uid("qtrader"), mid, yes, "buy", 50)
        assert (bal("qtrader"), bal("qmaker")) == (950, 1000)

        appmod.cancel_market(db, mid)
        assert bal("qtrader") == pytest.approx(1000, abs=0.02)
    finally:
        appmod.DB_PATH = old


def test_pending_results_moved_onto_shorter_dispute_window():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        aid = db.execute("INSERT INTO users (username, password_hash, balance, is_admin, created_at) VALUES ('sw', 'x', 0, 1, ?)",
                         (appmod.now_str(),)).lastrowid
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")
        now = datetime.now()

        def market(q, proposed_ago, deadline_in):
            mid = appmod.create_market(db, q, RULES, "Other", past, ["Yes", "No"], 1000)
            db.execute("UPDATE markets SET proposed_outcome_id = ?, proposed_by = ?, proposed_at = ?, finalizes_at = ? "
                       "WHERE id = ?", (appmod.get_outcomes(db, mid)[0]["id"], aid, fmt(now - proposed_ago),
                                        fmt(now + deadline_in), mid))
            return mid

        recent = market("Proposed 30 min ago?", timedelta(minutes=30), timedelta(hours=23, minutes=30))
        stale = market("Proposed 5 hours ago?", timedelta(hours=5), timedelta(hours=19))
        already = market("Already short?", timedelta(minutes=10), timedelta(minutes=50))  # earlier than 2h: untouched
        before_already = db.execute("SELECT finalizes_at FROM markets WHERE id=?", (already,)).fetchone()[0]

        appmod.shorten_dispute_windows(db)
        appmod.shorten_dispute_windows(db)  # running again changes nothing

        fin = lambda mid: appmod.parse_dt(db.execute("SELECT finalizes_at FROM markets WHERE id=?", (mid,)).fetchone()[0])
        assert abs((fin(recent) - (now + timedelta(minutes=90))).total_seconds()) < 2
        assert fin(stale) < datetime.now()
        assert db.execute("SELECT finalizes_at FROM markets WHERE id=?", (already,)).fetchone()[0] == before_already

        # the overdue one finalizes on the next page load; the others keep waiting
        appmod.app.test_client().get("/")
        status = lambda mid: db.execute("SELECT status FROM markets WHERE id=?", (mid,)).fetchone()[0]
        assert (status(stale), status(recent), status(already)) == ("resolved", "open", "open")
    finally:
        appmod.DB_PATH = old


# ---------------------------------------------------------------- creator's opening bet

def test_read_stake_rules():
    labels = ["A", "B", "C"]
    assert appmod.read_stake({"stake": "10", "stake_pick": "2"}, labels) == (2, 10, None)
    assert "at least Ⓜ10" in appmod.read_stake({"stake": "9.99", "stake_pick": "0"}, labels)[2]
    assert "at least Ⓜ10" in appmod.read_stake({"stake": "", "stake_pick": "0"}, labels)[2]
    assert "at least Ⓜ10" in appmod.read_stake({"stake": "inf", "stake_pick": "0"}, labels)[2]
    assert "Pick which outcome" in appmod.read_stake({"stake": "10", "stake_pick": "3"}, labels)[2]
    assert "Pick which outcome" in appmod.read_stake({"stake": "10"}, labels)[2]


def test_opening_bet_on_proposals_and_admin_markets():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        admin, kid = appmod.app.test_client(), appmod.app.test_client()
        signup(admin, "stakeboss")
        signup(kid, "stakekid")
        uid = lambda n: db.execute("SELECT id FROM users WHERE username=?", (n,)).fetchone()[0]
        bal = lambda n: db.execute("SELECT balance FROM users WHERE username=?", (n,)).fetchone()[0]
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        multi = {"kind": "multi", "outcomes": "December\nJanuary\nFebruary", "question": "First snow day?",
                 "description": RULES, "category": "Weather", "closes_at": closes}

        page = kid.get("/propose").get_data(as_text=True)
        assert 'name="stake"' in page and 'name="stake_pick"' in page and "opening bet" in page

        # below the minimum is refused and nothing is charged
        r = kid.post("/propose", data={"csrf_token": csrf(kid.get("/propose")), **multi, "stake": "5", "stake_pick": "1"},
                     follow_redirects=True)
        assert "at least Ⓜ10" in r.get_data(as_text=True) and bal("stakekid") == 1000

        # a Ⓜ25 opening bet on January is held, then placed on approval
        kid.post("/propose", data={"csrf_token": csrf(kid.get("/propose")), **multi, "stake": "25", "stake_pick": "1"})
        p = db.execute("SELECT * FROM proposals WHERE question='First snow day?'").fetchone()
        assert (p["stake"], p["stake_pick"], p["stake_label"]) == (25, 1, "January") and bal("stakekid") == 975
        assert "Opening bet: Ⓜ25.00 on “January” (held)" in kid.get("/propose").get_data(as_text=True)
        review = admin.get(f"/admin/proposals/{p['id']}").get_data(as_text=True)
        assert "Ⓜ25.00 on “January”" in review

        # if the admin removes January while reviewing, approval is refused
        approve = lambda outcomes: admin.post(f"/admin/proposals/{p['id']}", data={
            "csrf_token": csrf(admin.get(f"/admin/proposals/{p['id']}")), "decision": "approve",
            **{**multi, "outcomes": outcomes}}, follow_redirects=True)
        assert "one of the outcomes any more" in approve("December\nFebruary\nMarch").get_data(as_text=True)
        assert db.execute("SELECT status FROM proposals WHERE id=?", (p["id"],)).fetchone()[0] == "pending"

        # reordering is fine: the bet follows the name
        r = approve("February\njanuary\nDecember")
        assert "opening bet on “january” is placed" in r.get_data(as_text=True)
        mid = db.execute("SELECT market_id FROM proposals WHERE id=?", (p["id"],)).fetchone()[0]
        held = db.execute("SELECT o.label, p.cost FROM positions p JOIN outcomes o ON o.id = p.outcome_id "
                          "WHERE p.user_id=?", (uid("stakekid"),)).fetchall()
        assert [tuple(h) for h in held] == [("january", 25)] and bal("stakekid") == 975
        assert db.execute("SELECT created_by FROM markets WHERE id=?", (mid,)).fetchone()[0] == uid("stakekid")

        # yes/no: renaming the side keeps the bet on the same side
        yn = {"kind": "yesno", "question": "Snow day?", "description": RULES, "category": "Weather", "closes_at": closes}
        kid.post("/propose", data={"csrf_token": csrf(kid.get("/propose")), **yn, "stake": "10", "stake_pick": "1"})
        p2 = db.execute("SELECT id FROM proposals WHERE question='Snow day?'").fetchone()[0]
        assert appmod.match_stake_pick(db.execute("SELECT * FROM proposals WHERE id=?", (p2,)).fetchone(),
                                       ["Over 2.5", "Under 2.5"]) == 1

        # proposer banned before approval: the market goes live and the held bet is refunded instead
        db.execute("INSERT INTO trade_bans (user_id, created_at) VALUES (?, ?)", (uid("stakekid"), appmod.now_str()))
        before = bal("stakekid")
        r = admin.post(f"/admin/proposals/{p2}", data={"csrf_token": csrf(admin.get(f"/admin/proposals/{p2}")),
                                                       "decision": "approve", **yn}, follow_redirects=True)
        assert "be placed (" in r.get_data(as_text=True) and bal("stakekid") == before + 10

        # admins bet when creating directly (no creation fee)
        r = admin.post("/admin/markets", data={"csrf_token": csrf(admin.get("/admin")), **yn, "question": "Admin snow?",
                                               "stake": "40", "stake_pick": "0"}, follow_redirects=True)
        assert "opening bet on “Yes”" in r.get_data(as_text=True) and bal("stakeboss") == 1000 - 40
        r = admin.post("/admin/markets", data={"csrf_token": csrf(admin.get("/admin")), **yn, "question": "No stake?",
                                               "stake": "0", "stake_pick": "0"}, follow_redirects=True)
        assert "at least Ⓜ10" in r.get_data(as_text=True)
        assert db.execute("SELECT COUNT(*) FROM markets WHERE question='No stake?'").fetchone()[0] == 0

        # running balances in everyone's history still add up
        for name in ("stakekid", "stakeboss"):
            running = 0
            for r in db.execute("SELECT amount, balance_after FROM activity WHERE user_id=? ORDER BY id", (uid(name),)):
                running = round(running + r[0], 2)
                assert r[1] == pytest.approx(running, abs=0.02)
    finally:
        appmod.DB_PATH = old


# ---------------------------------------------------------------- late trading fee

def test_late_multiplier_tiers_and_formula(monkeypatch):
    monkeypatch.setattr(appmod, "LATE_FEE_TIERS", [(timedelta(minutes=30), 3.0), (timedelta(hours=1), 2.0), (timedelta(hours=2), 1.5)])  # values this test was written for
    now = datetime(2026, 11, 22, 12, 0, 0)
    m = lambda left: {"closes_at": (now + left).strftime("%Y-%m-%d %H:%M:%S")}
    T = lambda left: appmod.late_multiplier(m(left), now)
    assert T(timedelta(hours=5)) == 0
    assert T(timedelta(hours=2)) == 0              # "more than 2 hours" starts at exactly 2h
    assert T(timedelta(hours=1, minutes=59)) == 1.5
    assert T(timedelta(hours=1)) == 1.5
    assert T(timedelta(minutes=59)) == 2
    assert T(timedelta(minutes=30)) == 2
    assert T(timedelta(minutes=29)) == 3
    assert T(timedelta(seconds=1)) == 3
    # F = 0.07 x C x P(1-P) x T
    assert appmod.late_fee(100, 0.5, 3) == 5.25
    assert appmod.late_fee(100, 0.9, 1.5, buy=False) == pytest.approx(0.945, abs=0.006)  # 0.07 x 100 x 0.09 x 1.5
    assert appmod.late_fee(100, 0.5, 0) == 0
    assert appmod.late_fee(0, 0.5, 3) == 0


def test_late_fee_buys_pay_the_larger_of_both_formulas():
    # cheap or middling shares: the P(1 - P) formula is larger
    assert appmod.late_fee(100, 0.5, 6) == pytest.approx(0.07 * 100 * 0.25 * 6, abs=0.006)
    assert appmod.late_fee(100, 0.2, 6) == pytest.approx(0.07 * 100 * 0.16 * 6, abs=0.006)
    # near-certain shares: the P^2 formula takes over, so the fee doesn't shrink to nothing
    assert appmod.late_fee(100, 0.95, 6) == pytest.approx(0.017 * 100 * 0.9025 * 6, abs=0.006)
    assert appmod.late_fee(100, 0.95, 6) > 0.07 * 100 * 0.95 * 0.05 * 6
    # sniping a sure thing at 95¢ in the last 30 minutes no longer pays: fee > the 5¢ profit per share
    assert appmod.late_fee(100, 0.95, 6) > 100 * 0.05
    # sells only pay the original formula
    assert appmod.late_fee(100, 0.95, 6, buy=False) == pytest.approx(0.07 * 100 * 0.95 * 0.05 * 6, abs=0.006)
    # never higher than the larger of the two, and still nothing with time to spare
    for p in (0.05, 0.3, 0.5, 0.7, 0.81, 0.9, 0.99):
        both = max(0.07 * p * (1 - p), 0.017 * p * p) * 100 * 4
        assert appmod.late_fee(100, p, 4) == pytest.approx(both, abs=0.006)
        assert appmod.late_fee(100, p, 4) >= appmod.late_fee(100, p, 4, buy=False)
    assert appmod.late_fee(100, 0.95, 0) == 0


def test_late_fee_charged_on_buys_and_sells_near_the_deadline(monkeypatch):
    monkeypatch.setattr(appmod, "LATE_FEE_TIERS", [(timedelta(minutes=30), 3.0), (timedelta(hours=1), 2.0), (timedelta(hours=2), 1.5)])  # values this test was written for
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        client = appmod.app.test_client()
        signup(client, "latecomer")
        uid = db.execute("SELECT id FROM users WHERE username='latecomer'").fetchone()[0]
        bal = lambda: db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]
        closes = lambda left: (datetime.now() + left).strftime("%Y-%m-%d %H:%M:%S")
        mid = appmod.create_market(db, "Late?", RULES, "Other", closes(timedelta(days=1)), ["Yes", "No"], 1000)
        yes = appmod.get_outcomes(db, mid)[0]["id"]
        url = lambda a, v: f"/api/quote?market={mid}&outcome={yes}&action={a}&value={v}"

        # far from the deadline: no fee, no warning
        q = client.get(url("buy", 100)).get_json()
        assert q["late_fee"] == 0 and q["late_multiplier"] == 0
        assert "Late trading fee is on" not in client.get(f"/market/{mid}").get_data(as_text=True)

        # 20 minutes left: T = 3
        db.execute("UPDATE markets SET closes_at = ? WHERE id = ?", (closes(timedelta(minutes=20)), mid))
        assert "Late trading fee is on (×3)" in client.get(f"/market/{mid}").get_data(as_text=True)
        q = client.get(url("buy", 100)).get_json()
        expected = appmod.late_fee(q["shares"], 100 / q["shares"], 3)
        assert q["late_multiplier"] == 3 and q["late_fee"] == pytest.approx(expected) and q["late_fee"] > 0
        msg = appmod.execute_trade(db, uid, mid, yes, "buy", 100)
        assert "Late trading fee" in msg
        assert bal() == pytest.approx(1000 - 100 - q["late_fee"])
        fee_row = db.execute("SELECT amount, detail FROM activity WHERE user_id=? AND kind='fee'", (uid,)).fetchone()
        assert fee_row["amount"] == pytest.approx(-q["late_fee"]) and "×3" in fee_row["detail"]

        # selling 45 minutes out: T = 2, taken out of what you receive
        db.execute("UPDATE markets SET closes_at = ? WHERE id = ?", (closes(timedelta(minutes=45)), mid))
        held = db.execute("SELECT shares FROM positions WHERE user_id=?", (uid,)).fetchone()[0]
        sq = client.get(url("sell", held / 2)).get_json()
        assert sq["late_multiplier"] == 2 and sq["late_fee"] > 0
        before = bal()
        appmod.execute_trade(db, uid, mid, yes, "sell", held / 2)
        assert bal() == pytest.approx(before + sq["coins"] - sq["late_fee"], abs=0.01)

        # the fee counts towards what you can afford
        db.execute("UPDATE users SET balance = 100 WHERE id = ?", (uid,))
        with pytest.raises(appmod.TradeError, match="late trading fee"):
            appmod.execute_trade(db, uid, mid, yes, "buy", 100)
        assert bal() == 100

        # history still adds up
        running = 0
        for r in db.execute("SELECT amount, balance_after, kind FROM activity WHERE user_id=? ORDER BY id", (uid,)):
            running = round(running + r[0], 2)
            if r[2] != "admin":
                assert r[1] == pytest.approx(running, abs=0.02)
            running = r[1]
        assert "Late trading fee" in client.get("/how-it-works").get_data(as_text=True)
    finally:
        appmod.DB_PATH = old


def test_how_it_works_late_fee_table_follows_settings(monkeypatch):
    rows = lambda: appmod.late_fee_table()
    monkeypatch.setattr(appmod, "LATE_FEE_TIERS",
                        [(timedelta(minutes=30), 8.0), (timedelta(hours=1), 4.0), (timedelta(hours=2), 2.0)])
    assert rows() == [("More than 2 hours", 0.0), ("1–2 hours", 2.0), ("30–60 minutes", 4.0),
                      ("Less than 30 minutes", 8.0)]
    page = appmod.app.test_client().get("/how-it-works").get_data(as_text=True)
    assert "<td>30–60 minutes</td><td class=\"num\">4</td>" in page
    assert "0.07 × 100 × 0.25 × 8 = <b>Ⓜ14.00</b>" in page
    monkeypatch.setattr(appmod, "LATE_FEE_TIERS", [(timedelta(minutes=15), 5.0), (timedelta(hours=3), 1.0)])
    assert rows() == [("More than 3 hours", 0.0), ("15–180 minutes", 1.0), ("Less than 15 minutes", 5.0)]


def test_late_fee_paid_to_house(monkeypatch):
    monkeypatch.setattr(appmod, "LATE_FEE_TIERS",
                        [(timedelta(minutes=30), 6.0), (timedelta(hours=1), 4.0), (timedelta(hours=2), 2.0)])
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        ids = {n: db.execute("INSERT INTO users (username, password_hash, balance, created_at) VALUES (?, 'x', 1000, ?)",
                             (n, appmod.now_str())).lastrowid for n in ("maker", "trader")}
        bal = lambda n: db.execute("SELECT balance FROM users WHERE id=?", (ids[n],)).fetchone()[0]
        soon = (datetime.now() + timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
        mid = appmod.create_market(db, "Late paid?", RULES, "Other", soon, ["Yes", "No"], 1000, ids["maker"])
        yes = appmod.get_outcomes(db, mid)[0]["id"]

        # trader's late bet pays late fee to the house, not to the market creator
        appmod.execute_trade(db, ids["trader"], mid, yes, "buy", 100)
        late = -db.execute("SELECT amount FROM activity WHERE user_id=? AND kind='fee' AND detail LIKE 'Late%'",
                           (ids["trader"],)).fetchone()[0]
        assert late > 0
        assert bal("trader") == pytest.approx(1000 - 100 - late)
        assert bal("maker") == 1000

        # selling late also pays the house
        held = db.execute("SELECT shares FROM positions WHERE user_id=?", (ids["trader"],)).fetchone()[0]
        appmod.execute_trade(db, ids["trader"], mid, yes, "sell", held / 2)
        assert bal("maker") == 1000

        client = appmod.app.test_client()
        q = client.get(f"/api/quote?market={mid}&outcome={yes}&action=buy&value=10").get_json()
        assert q["late_fee_to"] is None and q["late_fee"] > 0
    finally:
        appmod.DB_PATH = old


# ---------------------------------------------------------------- leftover pot -> creator (capped at 1000)

def test_leftover_pot_paid_to_creator_and_capped_at_1000():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        ids = {n: db.execute("INSERT INTO users (username, password_hash, balance, created_at) VALUES (?, 'x', ?, ?)",
                             (n, 5000 if n == "whale" else 1000, appmod.now_str())).lastrowid
               for n in ("maker", "ann", "ben", "whale")}
        bal = lambda n: db.execute("SELECT balance FROM users WHERE id=?", (ids[n],)).fetchone()[0]
        later = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")

        # Uncapped (< 1000): Ann 100 on Yes, Ben 100 on No, Yes wins -> Ann gets 190.90 shares, maker gets 9.10
        m1 = appmod.create_market(db, "Ann vs Ben?", RULES, "Other", later, ["Yes", "No"], 1000, ids["maker"])
        yes, no = [o["id"] for o in appmod.get_outcomes(db, m1)]
        appmod.execute_trade(db, ids["ann"], m1, yes, "buy", 100)
        appmod.execute_trade(db, ids["ben"], m1, no, "buy", 100)
        ann_shares = db.execute("SELECT shares FROM positions WHERE user_id=? AND outcome_id=?", (ids["ann"], yes)).fetchone()[0]
        appmod.resolve_market(db, m1, yes)
        assert bal("ann") == pytest.approx(900 + ann_shares, abs=0.01)
        assert bal("ben") == 900
        expected_left = round(200 - ann_shares, 2)
        assert bal("maker") == pytest.approx(1000 + expected_left, abs=0.01)
        assert db.execute("SELECT leftover_paid FROM markets WHERE id=?", (m1,)).fetchone()[0] == pytest.approx(expected_left, abs=0.01)
        page = appmod.app.test_client().get(f"/market/{m1}").get_data(as_text=True)
        assert "went to the market creator" in page

        # Capped at 1000: Whale bets 1500 on No, Yes wins with 0 shares -> leftover is 1500, maker gets 1000 max
        m2 = appmod.create_market(db, "Big pot?", RULES, "Other", later, ["Yes", "No"], 1000, ids["maker"])
        yes2, no2 = [o["id"] for o in appmod.get_outcomes(db, m2)]
        appmod.execute_trade(db, ids["whale"], m2, no2, "buy", 1500)
        maker_before = bal("maker")
        appmod.resolve_market(db, m2, yes2)
        assert bal("maker") == pytest.approx(maker_before + 1000.0, abs=0.01)
        assert db.execute("SELECT leftover_paid FROM markets WHERE id=?", (m2,)).fetchone()[0] == pytest.approx(1000.0)

        # No maker / shortfall: creator gets 0
        m3 = appmod.create_market(db, "Shortfall?", RULES, "Other", later, ["Yes", "No"], 1000, ids["maker"])
        yes3 = appmod.get_outcomes(db, m3)[0]["id"]
        appmod.execute_trade(db, ids["ann"], m3, yes3, "buy", 100)
        maker_before = bal("maker")
        appmod.resolve_market(db, m3, yes3)
        assert bal("maker") == maker_before
        assert db.execute("SELECT leftover_paid FROM markets WHERE id=?", (m3,)).fetchone()[0] == 0
    finally:
        appmod.DB_PATH = old


def test_fixed_point_anti_arbitrage_and_radial_unwind():
    for b in (50, 100, 1000):
        for spend_cents in (100, 1234, 5000):
            q = [0, 0, 0]
            sc, cost_c = appmod.lmsr.quote_buy_cents(q, b, 0, spend_cents)
            assert cost_c <= spend_cents and sc > 0
            q[0] += sc
            proceeds_c = appmod.lmsr.quote_sell_cents(q, b, 0, sc)
            assert proceeds_c <= cost_c

    # Radial unwind gives identical holders identical refunds and unwinds outcomes.shares to 0
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        u1, u2 = [db.execute("INSERT INTO users (username, password_hash, balance, created_at) VALUES (?, 'x', 1000, ?)",
                             (n, appmod.now_str())).lastrowid for n in ("u1", "u2")]
        later = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        mid = appmod.create_market(db, "Radial?", RULES, "Other", later, ["A", "B", "C"], 100)
        a, b_out, _ = [o["id"] for o in appmod.get_outcomes(db, mid)]
        appmod.execute_trade(db, u1, mid, a, "buy", 50)
        appmod.execute_trade(db, u2, mid, b_out, "buy", 80)
        appmod.cancel_market(db, mid)
        assert all(o["shares"] == 0 for o in appmod.get_outcomes(db, mid))
        assert db.execute("SELECT pot_cents FROM markets WHERE id=?", (mid,)).fetchone()[0] == 0
    finally:
        appmod.DB_PATH = old


def test_bundle_many_matches_bundle():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        later = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        m1 = appmod.create_market(db, "M1?", RULES, "Other", later, ["Yes", "No"], 1000)
        m2 = appmod.create_market(db, "M2?", RULES, "Other", later, ["A", "B", "C"], 1585)
        rows = db.execute("SELECT * FROM markets ORDER BY id").fetchall()
        batched = appmod.bundle_many(db, rows)
        singles = [appmod.bundle(db, m) for m in rows]
        assert len(batched) == len(singles) == 2
        for b_item, s_item in zip(batched, singles):
            assert b_item["state"] == s_item["state"]
            assert b_item["volume"] == s_item["volume"]
            assert b_item["traders"] == s_item["traders"]
            assert b_item["outcomes"] == s_item["outcomes"]
    finally:
        appmod.DB_PATH = old


def test_admin_can_cancel_and_refund_one_purchase():
    path = os.path.join(tempfile.mkdtemp(), "cancel-trade.db")
    old = appmod.DB_PATH
    appmod.DB_PATH = path
    appmod._hits.clear()
    try:
        appmod.init_db()
        db = appmod.connect()
        admin, gus, hal = (appmod.app.test_client() for _ in range(3))
        signup(admin, "boss")
        signup(gus, "gus")
        signup(hal, "hal")
        uid = lambda n: db.execute("SELECT id FROM users WHERE username=?", (n,)).fetchone()[0]
        bal = lambda n: db.execute("SELECT balance FROM users WHERE username=?", (n,)).fetchone()[0]
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        mid = appmod.create_market(db, "Cancel one?", "", "Other", closes, ["Yes", "No"], 100,
                                   created_by=uid("boss"))
        yes, no = (o["id"] for o in appmod.get_outcomes(db, mid))

        appmod.execute_trade(db, uid("gus"), mid, yes, "buy", 50)   # + Ⓜ10 entry fee
        appmod.execute_trade(db, uid("gus"), mid, yes, "buy", 30)
        appmod.execute_trade(db, uid("hal"), mid, no, "buy", 40)
        first, second = [r[0] for r in db.execute(
            "SELECT id FROM trades WHERE user_id=? ORDER BY id", (uid("gus"),))]
        second_shares = db.execute("SELECT shares FROM trades WHERE id=?", (second,)).fetchone()[0]
        held = db.execute("SELECT shares FROM positions WHERE user_id=? AND outcome_id=?",
                          (uid("gus"), yes)).fetchone()[0]
        q_yes = db.execute("SELECT shares FROM outcomes WHERE id=?", (yes,)).fetchone()[0]
        before = bal("gus")

        # regular users can't
        page = admin.get(f"/market/{mid}")
        assert f"/admin/trades/{second}/cancel" in page.get_data(as_text=True)
        assert f"/admin/trades/{second}/cancel" not in gus.get(f"/market/{mid}").get_data(as_text=True)
        tok = csrf(gus.get(f"/market/{mid}"))
        assert gus.post(f"/admin/trades/{second}/cancel", data={"csrf_token": tok}).status_code == 403

        # the admin cancels the second purchase: Ⓜ30 back, shares out of the position and the market
        tok = csrf(page)
        r = admin.post(f"/admin/trades/{second}/cancel", data={"csrf_token": tok}, follow_redirects=True)
        assert "refunded Ⓜ30.00" in r.get_data(as_text=True)
        assert bal("gus") == pytest.approx(before + 30)
        assert db.execute("SELECT 1 FROM trades WHERE id=?", (second,)).fetchone() is None
        assert db.execute("SELECT shares FROM positions WHERE user_id=? AND outcome_id=?",
                          (uid("gus"), yes)).fetchone()[0] == pytest.approx(held - second_shares)
        assert db.execute("SELECT shares FROM outcomes WHERE id=?", (yes,)).fetchone()[0] == pytest.approx(
            q_yes - second_shares)
        assert db.execute("SELECT 1 FROM audit_log WHERE action='market.cancel_trade'").fetchone()
        assert bal("hal") == pytest.approx(1000 - 40 - appmod.ENTRY_FEE)  # other traders untouched

        # once his last purchase is gone, his entry fee comes back too
        admin.post(f"/admin/trades/{first}/cancel", data={"csrf_token": tok})
        assert bal("gus") == pytest.approx(1000)
        assert db.execute("SELECT COUNT(*) FROM positions WHERE user_id=?", (uid("gus"),)).fetchone()[0] == 0

        # can't cancel a purchase whose shares have since been sold
        appmod.execute_trade(db, uid("hal"), mid, no, "sell", 5)
        hal_buy = db.execute("SELECT id FROM trades WHERE user_id=? AND shares > 0", (uid("hal"),)).fetchone()[0]
        with pytest.raises(appmod.TradeError, match="sold"):
            appmod.cancel_trade(db, hal_buy)
        hal_sell = db.execute("SELECT id FROM trades WHERE user_id=? AND shares < 0", (uid("hal"),)).fetchone()[0]
        with pytest.raises(appmod.TradeError, match="purchases"):
            appmod.cancel_trade(db, hal_sell)

        # nor in a settled market
        appmod.cancel_market(db, mid)
        with pytest.raises(appmod.TradeError, match="unresolved"):
            appmod.cancel_trade(db, hal_buy)
    finally:
        appmod.DB_PATH = old
