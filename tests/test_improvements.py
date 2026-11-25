"""Tests for the admin controls, safeguards and polish ported from the Next.js branch."""
import os
import sqlite3
import tempfile
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_market import (RULES, _closed_market_with_bet, _fresh_db, _market, _post, _user, csrf, past,  # noqa: E402
                         signup, appmod)


@pytest.fixture(autouse=True)
def no_configured_owner(monkeypatch):
    monkeypatch.setattr(appmod, "OWNER_USERNAME", "")


@pytest.fixture(autouse=True)
def restore_db_path():
    old = appmod.DB_PATH
    yield
    appmod.DB_PATH = old


def _q(db, sql, *a):
    return db.execute(sql, a).fetchone()[0]


# ---------------------------------------------------------------- 1. fixes and safeguards

def test_delete_market_with_disputes_and_clarifications():
    old = appmod.DB_PATH
    try:
        db, mid, yes, no, admin, bob, eve = _closed_market_with_bet()
        _post(admin, f"/admin/markets/{mid}/propose-result", outcome_id=no)
        _post(bob, f"/market/{mid}/dispute", reason="Official page says Yes won")
        _post(admin, f"/admin/markets/{mid}/clarify", body="Clarifying how the source counts.")
        assert _q(db, "SELECT COUNT(*) FROM disputes") == 1 and _q(db, "SELECT COUNT(*) FROM clarifications") == 1
        r = _post(admin, f"/admin/markets/{mid}/delete")
        assert r.status_code == 200
        assert _q(db, "SELECT COUNT(*) FROM markets WHERE id=?", mid) == 0
        assert _q(db, "SELECT COUNT(*) FROM disputes") == 0 and _q(db, "SELECT COUNT(*) FROM clarifications") == 0
    finally:
        appmod.DB_PATH = old


def test_cancel_settles_open_disputes_and_clears_proposal():
    old = appmod.DB_PATH
    try:
        db, mid, yes, no, admin, bob, eve = _closed_market_with_bet()
        _post(admin, f"/admin/markets/{mid}/propose-result", outcome_id=no)
        _post(bob, f"/market/{mid}/dispute", reason="Official page says Yes won")
        _post(admin, f"/admin/markets/{mid}/cancel")
        m = db.execute("SELECT * FROM markets WHERE id=?", (mid,)).fetchone()
        assert m["status"] == "cancelled"
        assert m["proposed_outcome_id"] is None and m["finalizes_at"] is None
        assert _q(db, "SELECT COUNT(*) FROM disputes WHERE handled_at IS NULL") == 0
        # portfolio shows the radial LMSR refund (bounded by anti-arbitrage rounding)
        page = bob.get("/portfolio").get_data(as_text=True)
        assert "Refunded Ⓜ99.99" in page or "Refunded Ⓜ100.00" in page
    finally:
        appmod.DB_PATH = old


def test_slippage_guard_on_buy_and_sell():
    db = _fresh_db()
    a, b = _user(db, "a"), _user(db, "b")
    mid, (o0, o1) = _market(db)
    quote_shares = appmod.lmsr.shares_for_amount([0, 0], 300, 0, 50)
    appmod.execute_trade(db, b, mid, o0, "buy", 200)  # price moves after "the quote"
    with pytest.raises(appmod.TradeError, match="price moved"):
        appmod.execute_trade(db, a, mid, o0, "buy", 50, min_out=quote_shares * 0.98)
    assert _q(db, "SELECT COUNT(*) FROM trades WHERE user_id=?", a) == 0
    appmod.execute_trade(db, a, mid, o0, "buy", 50, min_out=1)  # a sane floor passes
    held = _q(db, "SELECT shares FROM positions WHERE user_id=? AND outcome_id=?", a, o0)
    stale_coins = 1000.0
    with pytest.raises(appmod.TradeError, match="price moved"):
        appmod.execute_trade(db, a, mid, o0, "sell", held, min_out=stale_coins)
    appmod.execute_trade(db, a, mid, o0, "sell", held, min_out=0)


def test_tiny_sell_is_refused_in_trade_and_quote():
    db = _fresh_db()
    a = _user(db, "a")
    mid, (o0, o1) = _market(db, b=100000)
    appmod.execute_trade(db, a, mid, o0, "buy", 1)
    held = _q(db, "SELECT shares FROM positions WHERE user_id=?", a)
    with pytest.raises(appmod.TradeError, match="less than Ⓜ0.01"):
        appmod.execute_trade(db, a, mid, o0, "sell", 0.01)
    assert held > 0
    outs = appmod.get_outcomes(db, mid)
    with pytest.raises(appmod.TradeError, match="less than Ⓜ0.01"):
        appmod.check_trade(appmod_market(db, mid), outs, 0, "sell", 0.01)


def appmod_market(db, mid):
    return db.execute("SELECT * FROM markets WHERE id=?", (mid,)).fetchone()


def test_unknown_result_decision_is_an_error():
    old = appmod.DB_PATH
    try:
        db, mid, yes, no, admin, bob, eve = _closed_market_with_bet()
        page = _post(admin, f"/admin/markets/{mid}/result-action", decision="bogus").get_data(as_text=True)
        assert "Unknown action." in page
    finally:
        appmod.DB_PATH = old


def test_min_bet_and_balance_messages():
    db = _fresh_db()
    a = _user(db, "a", balance=50)
    mid, (o0, o1) = _market(db)
    with pytest.raises(appmod.TradeError, match="Minimum bet is Ⓜ1"):
        appmod.execute_trade(db, a, mid, o0, "buy", 0.5)
    with pytest.raises(appmod.TradeError, match="Your balance is Ⓜ50.00"):
        appmod.execute_trade(db, a, mid, o0, "buy", 500)


# ---------------------------------------------------------------- 2. password tools

def _login(client, name, pw):
    return client.post("/login", data={"csrf_token": csrf(client.get("/login")), "username": name, "password": pw},
                       follow_redirects=True)


def test_change_password_and_other_sessions_are_signed_out():
    _fresh_db()
    laptop, phone = appmod.app.test_client(), appmod.app.test_client()
    signup(laptop, "dana")
    _login(phone, "dana", "secret1")
    assert phone.get("/portfolio").status_code == 200

    def change(cur, new, conf=None):
        return _post(laptop, "/account/password", current=cur, new=new, confirm=conf or new).get_data(as_text=True)

    assert "current password is wrong" in change("nope", "newsecret")
    assert "at least 6" in change("secret1", "abc")
    assert "don&#39;t match" in change("secret1", "newsecret", "different")
    assert "Password changed." in change("secret1", "newsecret")
    assert laptop.get("/portfolio").status_code == 200       # this device stays signed in
    assert phone.get("/portfolio").status_code == 302        # the other one is signed out
    assert "Wrong username" in _login(appmod.app.test_client(), "dana", "secret1").get_data(as_text=True)
    assert "Wrong username" not in _login(appmod.app.test_client(), "dana", "newsecret").get_data(as_text=True)


def test_temporary_password_signs_out_and_clears_lockout(capsys):
    db = _fresh_db()
    u = _user(db, "erin")
    for _ in range(10):
        appmod.rate_limited(("login-fail", "erin"), 10, 300)
    pw = appmod.set_temporary_password(db, u)
    assert len(pw) == 12
    assert ("login-fail", "erin") not in appmod._hits
    assert appmod.check_password_hash(db.execute("SELECT password_hash FROM users WHERE id=?", (u,)).fetchone()[0], pw)
    assert _q(db, "SELECT session_gen FROM users WHERE id=?", u) == 1
    import reset_password
    assert reset_password.main(["reset_password.py", "erin"]) == 0
    assert "Temporary password for erin:" in capsys.readouterr().out
    assert reset_password.main(["reset_password.py", "nobody"]) == 1


# ---------------------------------------------------------------- 3. audit log

def _audit(db):
    return [(r["action"], r["actor_name"], r["target_label"], r["detail"])
            for r in db.execute("SELECT * FROM audit_log ORDER BY id")]


def test_admin_actions_are_audited_and_forbidden_ones_are_not():
    old = appmod.DB_PATH
    try:
        db, mid, yes, no, admin, bob, eve = _closed_market_with_bet()
        bob_id = _q(db, "SELECT id FROM users WHERE username='bob'")
        _post(admin, f"/admin/markets/{mid}/propose-result", outcome_id=yes)
        _post(admin, f"/admin/markets/{mid}/result-action", decision="retract")
        _post(admin, f"/admin/markets/{mid}/clarify", body="A clarification about the source.")
        _post(admin, f"/admin/users/{bob_id}/grant", amount="5")
        _post(admin, f"/admin/users/{bob_id}/trading")
        _post(admin, f"/admin/users/{bob_id}/trading")
        _post(admin, f"/admin/users/{bob_id}/toggle-admin")
        log = _audit(db)
        actions = [a[0] for a in log]
        assert actions == ["result.propose", "result.retract", "market.clarify", "user.grant", "user.ban",
                           "user.unban", "user.admin_on"]
        assert log[0][1] == "chief" and log[0][2] == "Win?" and "Proposed" in log[0][3]
        assert log[3][3] == "+Ⓜ5.00"
        # a regular user hitting an admin action is refused and leaves no entry
        _post(bob, f"/admin/users/{bob_id}/grant", amount="99")
        assert len(_audit(db)) == 7
        # delete keeps the entry after the market is gone
        _post(admin, f"/admin/markets/{mid}/delete")
        assert _audit(db)[-1][0] == "market.delete" and "Status was open" in _audit(db)[-1][3]
    finally:
        appmod.DB_PATH = old


def test_admin_log_page_search_pages_and_permissions():
    old = appmod.DB_PATH
    try:
        db, mid, yes, no, admin, bob, eve = _closed_market_with_bet()
        assert bob.get("/admin/log").status_code == 403
        assert "No admin activity yet." in admin.get("/admin/log").get_data(as_text=True)
        _post(admin, f"/admin/markets/{mid}/propose-result", outcome_id=yes)
        _post(admin, f"/admin/markets/{mid}/cancel")
        page = admin.get("/admin/log").get_data(as_text=True)
        assert "Proposed a result" in page and "Cancelled a market" in page and f"/market/{mid}" in page
        assert "Cancelled a market" not in admin.get("/admin/log?q=proposed").get_data(as_text=True)
        assert "Nothing matches" in admin.get("/admin/log?q=zzzz").get_data(as_text=True)
        for i in range(55):
            db.execute("INSERT INTO audit_log (actor_name, action, target_type, created_at) "
                       "VALUES ('chief', 'user.grant', 'user', ?)", (appmod.now_str(),))
        assert "Page 1 of 2" in admin.get("/admin/log").get_data(as_text=True)
        assert "Page 2 of 2" in admin.get("/admin/log?page=2").get_data(as_text=True)
    finally:
        appmod.DB_PATH = old


def test_admin_reset_password_rules():
    old = appmod.DB_PATH
    try:
        db = _fresh_db()
        owner, helper, bob = (appmod.app.test_client() for _ in range(3))
        signup(owner, "boss")
        signup(helper, "helper")
        signup(bob, "bob")
        uid = lambda n: _q(db, "SELECT id FROM users WHERE username=?", n)
        _post(owner, f"/admin/users/{uid('helper')}/toggle-admin")
        # an admin can reset a regular user, but not the owner
        page = _post(helper, f"/admin/users/{uid('bob')}/reset-password").get_data(as_text=True)
        assert "Temporary password for bob:" in page
        assert "Only the site owner" in _post(helper, f"/admin/users/{uid('boss')}/reset-password").get_data(as_text=True)
        assert bob.get("/portfolio").status_code == 302  # bob was signed out
        assert [a[0] for a in _audit(db)].count("user.password_reset") == 1
    finally:
        appmod.DB_PATH = old


# ---------------------------------------------------------------- 4. users admin and account deletion

def _site(*names):
    """A fresh DB where the first name is the owner. Returns (db, {name: client}, uid-lookup)."""
    db = _fresh_db()
    clients = {}
    for n in names:
        clients[n] = appmod.app.test_client()
        signup(clients[n], n)
    return db, clients, (lambda n: _q(db, "SELECT id FROM users WHERE username=?", n))


def test_users_search_filter_and_paging():
    db, c, uid = _site("boss", "bob", "bobby", "carol")
    for i in range(30):
        _user(db, f"filler{i:02d}")
    page = c["boss"].get("/admin").get_data(as_text=True)
    assert "Page 1 of 2" in page and "Manage" in page
    assert "Page 2 of 2" in c["boss"].get("/admin?upage=2").get_data(as_text=True)
    found = c["boss"].get("/admin?q=bob").get_data(as_text=True)
    assert "bobby" in found and "carol" not in found
    assert "No users match." in c["boss"].get("/admin?q=zzz").get_data(as_text=True)
    _post(c["boss"], f"/admin/users/{uid('carol')}/trading")
    _post(c["boss"], f"/admin/users/{uid('bob')}/comment-mute")
    banned = c["boss"].get("/admin?filter=banned").get_data(as_text=True)
    assert "carol" in banned and "bobby" not in banned
    assert "bob</strong>" in c["boss"].get("/admin?filter=muted").get_data(as_text=True)
    admins = c["boss"].get("/admin?filter=admins").get_data(as_text=True)
    assert "boss" in admins and "carol" not in admins  # the owner counts even if the flag was never set


def test_manage_page_permissions():
    db, c, uid = _site("boss", "helper", "bob")
    _post(c["boss"], f"/admin/users/{uid('helper')}/toggle-admin")
    assert c["bob"].get(f"/admin/users/{uid('bob')}").status_code == 403
    page = c["helper"].get(f"/admin/users/{uid('boss')}").get_data(as_text=True)
    assert "Only the site owner can manage this account." in page
    assert "Delete account" in c["helper"].get(f"/admin/users/{uid('bob')}").get_data(as_text=True)
    # an admin can't mute the owner or another admin; the owner can't be muted by anyone
    assert "Only the site owner" in _post(c["helper"], f"/admin/users/{uid('boss')}/comment-mute").get_data(as_text=True)
    assert "can&#39;t be muted" in _post(c["boss"], f"/admin/users/{uid('boss')}/comment-mute").get_data(as_text=True)


def test_delete_account_refusals():
    db, c, uid = _site("boss", "helper", "bob")
    _post(c["boss"], f"/admin/users/{uid('helper')}/toggle-admin")
    say = lambda who, target: _post(c[who], f"/admin/users/{uid(target)}/delete").get_data(as_text=True)
    assert "can&#39;t delete your own account" in say("boss", "boss")
    assert "owner&#39;s account can&#39;t be deleted" in say("helper", "boss")
    assert "Remove their admin role" in say("boss", "helper")
    # an open dispute blocks deletion
    closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    mid = appmod.create_market(db, "Win?", RULES, "Events", closes, ["Yes", "No"], 100, None)
    yes, no = [o["id"] for o in appmod.get_outcomes(db, mid)]
    appmod.execute_trade(db, uid("bob"), mid, yes, "buy", 50)
    db.execute("UPDATE markets SET closes_at = ? WHERE id = ?", (past(), mid))
    _post(c["boss"], f"/admin/markets/{mid}/propose-result", outcome_id=no)
    _post(c["bob"], f"/market/{mid}/dispute", reason="The official page says Yes won")
    assert "open dispute" in say("boss", "bob")
    assert _q(db, "SELECT COUNT(*) FROM users WHERE username='bob'") == 1
    # a running market they created blocks it too
    db.execute("UPDATE disputes SET handled_at = ?", (appmod.now_str(),))
    appmod.create_market(db, "Bob's market?", RULES, "Events", closes, ["Yes", "No"], 100, uid("bob"))
    assert "still running" in say("boss", "bob")


def test_delete_account_reverses_open_bets_and_keeps_settled_payouts():
    db, c, uid = _site("boss", "alice", "bob")
    closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    open_m = appmod.create_market(db, "Open one?", RULES, "Events", closes, ["Yes", "No"], 100, None)
    done_m = appmod.create_market(db, "Done one?", RULES, "Events", closes, ["Yes", "No"], 100, None)
    oy = appmod.get_outcomes(db, open_m)[0]["id"]
    dy, dn = [o["id"] for o in appmod.get_outcomes(db, done_m)]
    price_before = appmod.lmsr.prices([o["shares"] for o in appmod.get_outcomes(db, open_m)], 100)[0]
    appmod.execute_trade(db, uid("bob"), open_m, oy, "buy", 100)
    appmod.execute_trade(db, uid("bob"), done_m, dn, "buy", 40)
    appmod.execute_trade(db, uid("alice"), done_m, dy, "buy", 60)
    db.execute("UPDATE markets SET closes_at = ? WHERE id = ?", (past(), done_m))
    appmod.propose_result(db, done_m, dy, uid("boss"))
    db.execute("UPDATE markets SET finalizes_at = ? WHERE id = ?", (past(), done_m))
    appmod.finalize_due(db)
    alice_before = _q(db, "SELECT balance FROM users WHERE username='alice'")
    db.execute("INSERT INTO comments (market_id, user_id, body, created_at) VALUES (?, ?, 'hi', ?)",
               (open_m, uid("bob"), appmod.now_str()))
    page = _post(c["boss"], f"/admin/users/{uid('bob')}/delete").get_data(as_text=True)
    assert "Deleted bob" in page and "Reversed" in page
    assert _q(db, "SELECT COUNT(*) FROM users WHERE username='bob'") == 0
    for table in ("trades", "positions", "comments", "activity"):
        assert _q(db, f"SELECT COUNT(*) FROM {table} WHERE user_id NOT IN (SELECT id FROM users)") == 0
    # bob's effect on the open market's price is gone, and nobody else's balance moved
    assert appmod.lmsr.prices([o["shares"] for o in appmod.get_outcomes(db, open_m)], 100)[0] == pytest.approx(price_before)
    assert _q(db, "SELECT balance FROM users WHERE username='alice'") == pytest.approx(alice_before)
    log = _audit(db)[-1]
    assert log[0] == "user.delete" and log[2] == "bob" and "Open bets reversed" in log[3]
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_delete_former_admin_who_wrote_clarifications():
    db, c, uid = _site("boss", "helper")
    _post(c["boss"], f"/admin/users/{uid('helper')}/toggle-admin")
    closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    mid = appmod.create_market(db, "Clarify?", RULES, "Events", closes, ["Yes", "No"], 100, None)
    _post(c["helper"], f"/admin/markets/{mid}/clarify", body="A note from the helper admin.")
    _post(c["boss"], f"/admin/users/{uid('helper')}/toggle-admin")  # demote first
    assert "Deleted helper" in _post(c["boss"], f"/admin/users/{uid('helper')}/delete").get_data(as_text=True)
    assert _q(db, "SELECT admin_id FROM clarifications") is None
    assert "A note from the helper admin." in c["boss"].get(f"/market/{mid}").get_data(as_text=True)


def test_migration_makes_clarification_admin_nullable_and_keeps_rows():
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    old = appmod.DB_PATH
    appmod.DB_PATH = path
    try:
        raw = sqlite3.connect(path)
        raw.executescript(open(os.path.join(appmod.BASE_DIR, "schema.sql")).read())
        raw.executescript("""
            DROP TABLE clarifications;
            CREATE TABLE clarifications (id INTEGER PRIMARY KEY AUTOINCREMENT,
              market_id INTEGER NOT NULL REFERENCES markets(id), admin_id INTEGER NOT NULL REFERENCES users(id),
              body TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX idx_clarifications_market ON clarifications(market_id);
            INSERT INTO users (username, password_hash, balance, created_at) VALUES ('a', 'x', 1, 'now');
            INSERT INTO markets (question, b, closes_at, created_at) VALUES ('Q', 100, 'later', 'now');
            INSERT INTO clarifications (market_id, admin_id, body, created_at) VALUES (1, 1, 'kept', 'now');""")
        raw.close()
        appmod.init_db()
        appmod.init_db()  # repeatable
        db = appmod.connect()
        cols = {r["name"]: r["notnull"] for r in db.execute("PRAGMA table_info(clarifications)")}
        assert cols["admin_id"] == 0
        assert _q(db, "SELECT body FROM clarifications") == "kept"
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='idx_clarifications_market'").fetchone()
    finally:
        appmod.DB_PATH = old


# ---------------------------------------------------------------- 5. comment moderation and market controls

def _open_market(db, question="Moderated?"):
    closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    return appmod.create_market(db, question, RULES, "Events", closes, ["Yes", "No"], 100, None)


def test_comment_hide_delete_and_visibility():
    db, c, uid = _site("boss", "helper", "bob", "eve")
    _post(c["boss"], f"/admin/users/{uid('helper')}/toggle-admin")
    mid = _open_market(db)
    _post(c["bob"], f"/market/{mid}/comment", body="SECRET-BOB-TEXT")
    cid = _q(db, "SELECT id FROM comments")
    # a regular user can't moderate
    assert c["eve"].post(f"/admin/comments/{cid}/hide", data={"csrf_token": csrf(c["eve"].get("/"))}).status_code == 403
    assert "Comment hidden." in _post(c["helper"], f"/admin/comments/{cid}/hide").get_data(as_text=True)
    page = c["eve"].get(f"/market/{mid}").get_data(as_text=True)
    assert "SECRET-BOB-TEXT" not in page and "This comment was hidden by an admin." in page
    assert "SECRET-BOB-TEXT" not in appmod.app.test_client().get(f"/market/{mid}").get_data(as_text=True)
    admin_page = c["helper"].get(f"/market/{mid}").get_data(as_text=True)
    assert "SECRET-BOB-TEXT" in admin_page and "hidden</span>" in admin_page
    assert "Comment restored." in _post(c["helper"], f"/admin/comments/{cid}/hide").get_data(as_text=True)
    # an admin can't touch the owner's comment; the owner can touch anyone's
    _post(c["boss"], f"/market/{mid}/comment", body="owner words")
    boss_cid = _q(db, "SELECT id FROM comments WHERE body='owner words'")
    assert "Only the site owner" in _post(c["helper"], f"/admin/comments/{boss_cid}/delete").get_data(as_text=True)
    assert _q(db, "SELECT COUNT(*) FROM comments WHERE id=?", boss_cid) == 1
    assert "Comment deleted." in _post(c["boss"], f"/admin/comments/{cid}/delete").get_data(as_text=True)
    assert "That comment no longer exists." in _post(c["boss"], f"/admin/comments/{cid}/delete").get_data(as_text=True)
    assert [a[0] for a in _audit(db)] == ["user.admin_on", "comment.hide", "comment.unhide", "comment.delete"]
    # admins can always moderate their own comments
    _post(c["helper"], f"/market/{mid}/comment", body="helper words")
    own = _q(db, "SELECT id FROM comments WHERE body='helper words'")
    assert "Comment hidden." in _post(c["helper"], f"/admin/comments/{own}/hide").get_data(as_text=True)


def test_muted_user_cannot_comment():
    db, c, uid = _site("boss", "bob")
    mid = _open_market(db)
    _post(c["boss"], f"/admin/users/{uid('bob')}/comment-mute")
    assert "turned off commenting" in c["bob"].get(f"/market/{mid}").get_data(as_text=True)
    assert "turned off commenting" in _post(c["bob"], f"/market/{mid}/comment", body="let me in").get_data(as_text=True)
    assert _q(db, "SELECT COUNT(*) FROM comments") == 0
    _post(c["boss"], f"/admin/users/{uid('bob')}/comment-mute")
    _post(c["bob"], f"/market/{mid}/comment", body="back again")
    assert _q(db, "SELECT COUNT(*) FROM comments") == 1
    assert [a[0] for a in _audit(db)] == ["user.comment_mute", "user.comment_unmute"]


def test_close_early_and_reopen_preconditions():
    db, c, uid = _site("boss", "bob")
    mid = _open_market(db)
    boss = c["boss"]
    future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    assert "still open" in _post(boss, f"/admin/markets/{mid}/reopen", closes_at=future).get_data(as_text=True)
    assert "Betting closed." in _post(boss, f"/admin/markets/{mid}/close").get_data(as_text=True)
    assert "already closed" in _post(boss, f"/admin/markets/{mid}/close").get_data(as_text=True)
    assert appmod.market_state(db.execute("SELECT * FROM markets WHERE id=?", (mid,)).fetchone()) == "closed"
    assert "Pick a valid closing time." in _post(boss, f"/admin/markets/{mid}/reopen", closes_at="soon").get_data(as_text=True)
    assert "in the future" in _post(boss, f"/admin/markets/{mid}/reopen", closes_at="2001-01-01T00:00").get_data(as_text=True)
    yes, no = [o["id"] for o in appmod.get_outcomes(db, mid)]
    appmod.propose_result(db, mid, yes, uid("boss"))
    assert "Withdraw it before reopening" in _post(boss, f"/admin/markets/{mid}/reopen", closes_at=future).get_data(as_text=True)
    appmod.retract_result(db, mid)
    assert "Betting reopened." in _post(boss, f"/admin/markets/{mid}/reopen", closes_at=future).get_data(as_text=True)
    assert appmod.market_state(db.execute("SELECT * FROM markets WHERE id=?", (mid,)).fetchone()) == "open"
    # regular users can't
    assert c["bob"].post(f"/admin/markets/{mid}/close", data={"csrf_token": csrf(c["bob"].get("/"))}).status_code == 403
    assert [a[0] for a in _audit(db)] == ["market.close_early", "market.reopen"]


def test_featured_markets_sort_first_and_show_badge():
    db, c, uid = _site("boss")
    first = _open_market(db, "Plain popular market?")
    second = _open_market(db, "Featured quiet market?")
    appmod.execute_trade(db, uid("boss"), first, appmod.get_outcomes(db, first)[0]["id"], "buy", 50)
    page = c["boss"].get("/").get_data(as_text=True)
    assert page.index("Plain popular") < page.index("Featured quiet") and "★ Featured" not in page
    assert "Market featured" in _post(c["boss"], f"/admin/markets/{second}/feature").get_data(as_text=True)
    page = c["boss"].get("/").get_data(as_text=True)
    assert page.index("Featured quiet") < page.index("Plain popular") and "★ Featured" in page
    assert "removed from featured" in _post(c["boss"], f"/admin/markets/{second}/feature").get_data(as_text=True)
    assert [a[0] for a in _audit(db)] == ["market.feature", "market.unfeature"]


def test_announcement_banner_limit_and_clear():
    db, c, uid = _site("boss", "bob")
    post = lambda **kw: _post(c["boss"], "/admin/announcement", **kw).get_data(as_text=True)
    assert "under 300 characters" in post(text="x" * 301)
    assert 'class="announcement"' not in c["bob"].get("/").get_data(as_text=True)
    assert "Announcement published." in post(text="No trading during the assembly.")
    assert "No trading during the assembly." in appmod.app.test_client().get("/how-it-works").get_data(as_text=True)
    assert "Announcement cleared." in post(clear="1")
    assert 'class="announcement"' not in c["bob"].get("/").get_data(as_text=True)
    assert c["bob"].post("/admin/announcement", data={"csrf_token": csrf(c["bob"].get("/")), "text": "hi"}).status_code == 403
    assert [a[3] for a in _audit(db)] == ["No trading during the assembly.", "(cleared)"]


# ---------------------------------------------------------------- 6. front-end markup

def test_home_renders_every_market_and_hides_by_filter():
    db, c, uid = _site("boss")
    open_id = _open_market(db, "Still open market?")
    cancelled = _open_market(db, "Cancelled market?")
    appmod.cancel_market(db, cancelled)
    page = c["boss"].get("/").get_data(as_text=True)
    card = lambda text, html: next(x for x in html.split("class=\"card market-card")[1:] if text in x)
    assert " hidden" in card("Cancelled market?", page)[:300]
    assert " hidden" not in card("Still open market?", page)[:300]
    assert ">Cancelled</a>" in page  # the new tab
    page = c["boss"].get("/?status=cancelled").get_data(as_text=True)
    assert " hidden" not in card("Cancelled market?", page)[:300]
    assert " hidden" in card("Still open market?", page)[:300]
    # an unknown status or topic falls back to the defaults instead of hiding everything
    page = c["boss"].get("/?status=bogus&cat=nope").get_data(as_text=True)
    assert " hidden" not in card("Still open market?", page)[:300]
    # filtering to nothing shows the filter-aware empty state
    page = c["boss"].get("/?cat=Weather").get_data(as_text=True)
    assert "No markets match these filters." in page


def test_market_page_has_quote_guard_bet_bar_and_school_timezone():
    db, c, uid = _site("boss")
    mid = _open_market(db)
    page = c["boss"].get(f"/market/{mid}").get_data(as_text=True)
    assert 'name="min_out"' in page and 'id="bet-bar"' in page and 'class="market-head"' in page
    assert os.environ["TZ"] in page  # the chart is told the school time zone
    assert 'aria-pressed="true">Buy' in page and 'for="value"' in page
    # logged-out visitors get no trade form but still see the page and the menu
    anon = appmod.app.test_client().get(f"/market/{mid}").get_data(as_text=True)
    assert 'class="tabbar"' in anon and "Log in" in anon and 'name="min_out"' not in anon


def test_nav_marks_current_page_and_footer_links():
    db, c, uid = _site("boss")
    page = c["boss"].get("/leaderboard").get_data(as_text=True)
    assert 'aria-current="page">Leaderboard' in page and 'aria-current="page">Markets' not in page
    assert page.count("Signed in as") == 1 and "Leaderboard</a>\n" in page


def test_format_filters():
    env = appmod.app.jinja_env
    assert env.from_string("{{ 12|signed_money }} {{ -3.5|signed_money }}").render() == "+Ⓜ12.00 −Ⓜ3.50"
    assert env.from_string("{{ 0.424|cents_label }}").render() == "42¢"


# ---------------------------------------------------------------- confirmed emails

def _capture_codes(monkeypatch):
    sent = []
    monkeypatch.setattr(appmod, "send_login_code_email", lambda to, code: sent.append((to, code)))
    return sent


def _email_post(client, **data):
    return client.post("/account/email", data={"csrf_token": csrf(client.get("/account/email")), **data},
                       follow_redirects=True)


def test_everyone_is_logged_out_once_when_emails_arrive():
    db = _fresh_db()
    client = appmod.app.test_client()
    signup(client, "oldtimer")
    assert client.get("/portfolio").status_code == 200
    db.execute("DELETE FROM site_settings WHERE key = 'emails_logout_done'")   # as on the live site before the update
    appmod.require_emails_once(db)
    assert client.get("/portfolio").status_code == 302                           # signed out
    assert _login(client, "oldtimer", "secret1").status_code == 200
    gen = db.execute("SELECT session_gen FROM users WHERE username='oldtimer'").fetchone()[0]
    appmod.require_emails_once(db)                                               # only ever once
    assert db.execute("SELECT session_gen FROM users WHERE username='oldtimer'").fetchone()[0] == gen
    assert client.get("/portfolio").status_code == 200


def test_existing_user_without_email_must_confirm_one(monkeypatch):
    db = _fresh_db()
    sent = _capture_codes(monkeypatch)
    client = appmod.app.test_client()
    signup(client, "oldtimer")
    db.execute("UPDATE users SET email = NULL, email_verified = 0 WHERE username = 'oldtimer'")   # a pre-email account
    for path in ("/", "/portfolio", "/leaderboard"):
        r = client.get(path)
        assert r.status_code == 302 and r.headers["Location"].endswith("/account/email")
    assert client.get("/api/quote?market=1&outcome=1&action=buy&value=5").status_code == 403
    assert "Add your email" in client.get("/account/email").get_data(as_text=True)

    assert "valid email" in _email_post(client, action="send", email="nope").get_data(as_text=True)
    page = _email_post(client, action="send", email="Old.Timer@example.com").get_data(as_text=True)
    assert "Enter the 6-digit code" in page and sent[-1][0] == "Old.Timer@example.com"
    code = sent[-1][1]
    assert "That code isn" in _email_post(client, action="verify", code="000000" if code != "000000" else "111111").get_data(as_text=True)
    assert "Email confirmed" in _email_post(client, action="verify", code=code).get_data(as_text=True)
    row = db.execute("SELECT email, email_verified FROM users WHERE username='oldtimer'").fetchone()
    assert tuple(row) == ("Old.Timer@example.com", 1)
    assert client.get("/portfolio").status_code == 200
    assert "Old.Timer@example.com" in client.get("/portfolio").get_data(as_text=True)

    # the email lands in people.csv next to the database
    csv_text = open(appmod.email_csv_path(), encoding="utf-8-sig").read().splitlines()
    assert csv_text[0] == "name,email" and "oldtimer,Old.Timer@example.com" in csv_text


def test_new_user_signs_up_with_email_and_confirms(monkeypatch):
    db = _fresh_db()
    sent = _capture_codes(monkeypatch)
    signup(appmod.app.test_client(), "taken")             # already has taken@example.com
    c = appmod.app.test_client()
    form = lambda **kw: {"csrf_token": csrf(c.get("/register")), "username": "newbie", "email": "newbie@example.com",
                         "password": "secret1", "confirm": "secret1", **kw}
    assert "already used" in c.post("/register", data=form(email="TAKEN@example.com"),
                                    follow_redirects=True).get_data(as_text=True)
    assert "valid email" in c.post("/register", data=form(email="x"), follow_redirects=True).get_data(as_text=True)
    page = c.post("/register", data=form(), follow_redirects=True).get_data(as_text=True)
    assert "Enter the 6-digit code" in page and sent[-1][0] == "newbie@example.com"
    assert c.get("/").status_code == 302                  # can't use the site until confirmed
    lb = appmod.app.test_client().get("/leaderboard").get_data(as_text=True)
    assert "newbie" not in lb                             # unconfirmed sign-ups aren't listed
    assert "Email confirmed" in _email_post(c, action="verify", code=sent[-1][1]).get_data(as_text=True)
    assert c.get("/").status_code == 200
    assert "newbie" in appmod.app.test_client().get("/leaderboard").get_data(as_text=True)
    assert "newbie,newbie@example.com" in open(appmod.email_csv_path(), encoding="utf-8-sig").read()


def test_code_expiry_attempt_limit_and_duplicate_at_confirm(monkeypatch):
    db = _fresh_db()
    sent = _capture_codes(monkeypatch)
    a, b = appmod.app.test_client(), appmod.app.test_client()
    signup(a, "alpha")
    signup(b, "bravo")
    db.execute("UPDATE users SET email = NULL, email_verified = 0")
    _email_post(a, action="send", email="shared@example.com")
    # someone else is already confirming that address: bravo can't claim it too
    assert "already used" in _email_post(b, action="send", email="Shared@example.com").get_data(as_text=True)
    assert len(sent) == 1
    # if two claims do overlap (e.g. one lapsed), whoever confirms second is turned away
    db.execute("INSERT INTO login_challenges (username, code_hash, expires_at, requested_at, attempts, email) "
               "VALUES ('bravo', ?, ?, 0, 0, 'shared@example.com')",
               (appmod.login_code_hash("bravo", "123456"), appmod.time.time() + 600))
    assert "Email confirmed" in _email_post(a, action="verify", code=sent[0][1]).get_data(as_text=True)
    assert "already used" in _email_post(b, action="verify", code="123456").get_data(as_text=True)
    # too many wrong codes: the code stops working
    _email_post(b, action="send", email="bravo@example.com")
    wrong = "000000" if sent[-1][1] != "000000" else "111111"
    for _ in range(appmod.LOGIN_CODE_MAX_ATTEMPTS):
        _email_post(b, action="verify", code=wrong)
    assert "expired" in _email_post(b, action="verify", code=sent[-1][1]).get_data(as_text=True)


def test_kaushik_is_still_the_owner(monkeypatch):
    import re
    src = open(os.path.join(os.path.dirname(appmod.__file__), "app.py")).read()
    assert re.search(r'^OWNER_USERNAME = os.environ.get\("MAMS_OWNER", "Kaushik"\)', src, re.M)
    monkeypatch.setattr(appmod, "OWNER_USERNAME", "Kaushik")
    _capture_codes(monkeypatch)
    db = _fresh_db()
    signup(appmod.app.test_client(), "someone")
    c = appmod.app.test_client()
    c.post("/register", data={"csrf_token": csrf(c.get("/register")), "username": "kaushik", "email": "k@example.com",
                              "password": "secret1", "confirm": "secret1"})
    assert db.execute("SELECT is_admin FROM users WHERE username = 'kaushik'").fetchone()[0] == 1


# ---------------------------------------------------------------- installable app (PWA)

def test_manifest_lists_working_icons_and_start_url():
    _fresh_db()
    client = appmod.app.test_client()
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["Content-Type"].startswith("application/manifest+json")
    data = r.get_json(force=True)
    assert data["display"] == "standalone" and data["start_url"].startswith("/") and data["scope"] == "/"
    sizes = {(i["sizes"], i["purpose"]) for i in data["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]


def test_service_worker_is_served_from_root_and_precaches_real_files():
    _fresh_db()
    client = appmod.app.test_client()
    r = client.get("/sw.js")
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "javascript" in r.headers["Content-Type"] and r.headers["Cache-Control"] == "no-cache"
    assert appmod.ASSET_VERSION in body and '"/offline"' in body
    for name in appmod.PRECACHE_ASSETS:
        url = f"/static/{name}?v={appmod.ASSET_VERSION}"
        assert url in body and client.get(url).status_code == 200
    # pages link the manifest and use versioned static URLs, so a deploy never serves stale cached CSS/JS
    page = client.get("/").get_data(as_text=True)
    assert 'rel="manifest"' in page and f"style.css?v={appmod.ASSET_VERSION}" in page


def test_offline_page_is_never_personal_and_needs_no_confirmed_email():
    db, c, uid = _site("boss", "newbie")
    db.execute("UPDATE users SET email_verified = 0 WHERE username = 'newbie'")
    db.commit()
    for path in ("/offline", "/sw.js", "/manifest.webmanifest"):
        assert c["newbie"].get(path).status_code == 200, path  # not redirected to the email page
    page = c["boss"].get("/offline").get_data(as_text=True)
    assert "You're offline" in page and "boss" not in page and "csrf_token" not in page and "Ⓜ" not in page


# ---------------------------------------------------------------- security hardening

def test_next_links_only_go_to_pages_on_this_site():
    for bad in ("/\\evil.com", "//evil.com", "https://evil.com", "/\tevil", "javascript:alert(1)", ""):
        with appmod.app.test_request_context():
            assert appmod.safe_path(bad, "/home") == "/home", bad
    with appmod.app.test_request_context():
        assert appmod.safe_path("/portfolio?x=1#a", "/home") == "/portfolio?x=1#a"
    _fresh_db()
    appmod.app.test_client()
    c = appmod.app.test_client()
    signup(appmod.app.test_client(), "walker")
    db = appmod.connect()
    db.execute("UPDATE users SET password_hash = ? WHERE username = 'walker'",
               (appmod.generate_password_hash("secret1"),))
    db.close()
    for nxt, where in (("/\\evil.com", "/"), ("https://evil.com", "/"), ("/portfolio", "/portfolio")):
        r = c.post(f"/login?next={nxt}", data={"csrf_token": csrf(c.get("/login")), "username": "walker",
                                               "password": "secret1"})
        assert r.status_code == 302 and r.headers["Location"] == where, nxt


def test_admin_back_link_and_referrer_stay_on_site():
    db, c, uid = _site("boss", "bob")
    r = c["boss"].post(f"/admin/users/{uid('bob')}/grant", data={"csrf_token": csrf(c["boss"].get("/")), "amount": "5",
                                                                 "next": "/\\evil.com"})
    assert r.status_code == 302 and "evil" not in r.headers["Location"]
    r = c["bob"].post("/bonus", data={"csrf_token": csrf(c["bob"].get("/"))},
                      headers={"Referer": "https://evil.com/x"})
    assert r.status_code == 302 and "evil" not in r.headers["Location"]
    r = c["bob"].post("/bonus", data={"csrf_token": csrf(c["bob"].get("/"))},
                      headers={"Referer": "http://localhost/leaderboard"})
    assert r.headers["Location"] == "/leaderboard"


def test_flooding_rate_limiter_keys_keeps_existing_lockouts():
    appmod._hits.clear()
    for _ in range(10):
        appmod.rate_limited(("login-fail", "victim"), 10, 300)
    for i in range(appmod.HITS_MAX_KEYS + 50):
        appmod.rate_limited(("login-fail", f"junk{i}"), 10, 300)
    assert len(appmod._hits) <= appmod.HITS_MAX_KEYS
    assert appmod.rate_limited(("login-fail", "victim"), 10, 300)  # still locked out
    appmod._hits.clear()


def test_security_headers_and_csp_nonce():
    db, c, uid = _site("boss")
    r = appmod.app.test_client().get("/")
    csp = r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    nonce = csp.split("'nonce-")[1].split("'")[0]
    html = r.get_data(as_text=True)
    assert html.count("<script") == html.count(f'nonce="{nonce}"') + html.count("<script src=")
    assert "Cache-Control" not in r.headers or "no-store" not in r.headers["Cache-Control"]
    mid, _ = _market(db)
    page = c["boss"].get(f"/market/{mid}")
    assert page.headers["Cache-Control"] == "no-store"
    nonce = page.headers["Content-Security-Policy"].split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">\n  drawChart' in page.get_data(as_text=True)
    sw = c["boss"].get("/sw.js")
    assert "Content-Security-Policy" not in sw.headers and sw.headers["Cache-Control"] == "no-cache"
    assert 'nonce="' in c["boss"].get("/offline").get_data(as_text=True)


def test_oversized_posts_and_long_passwords_are_refused(monkeypatch):
    _fresh_db()
    _capture_codes(monkeypatch)
    c = appmod.app.test_client()
    tok = csrf(c.get("/register"))
    assert c.post("/register", data={"csrf_token": tok, "body": "x" * 300_000}).status_code == 413
    long_pw = "p" * 129
    page = c.post("/register", data={"csrf_token": tok, "username": "longpw", "email": "l@example.com",
                                     "password": long_pw, "confirm": long_pw}, follow_redirects=True)
    assert "6–128 characters" in page.get_data(as_text=True)
    assert appmod.connect().execute("SELECT 1 FROM users WHERE username = 'longpw'").fetchone() is None


def test_comment_rate_limit():
    db, c, uid = _site("boss", "chatty")
    mid, _ = _market(db)
    for i in range(10):
        _post(c["chatty"], f"/market/{mid}/comment", body=f"comment {i}")
    page = _post(c["chatty"], f"/market/{mid}/comment", body="one too many").get_data(as_text=True)
    assert "commenting too fast" in page
    assert _q(db, "SELECT COUNT(*) FROM comments WHERE market_id = ?", mid) == 10


def test_admins_cannot_unblock_people_they_cannot_manage():
    db, c, uid = _site("boss", "mod", "bob")
    db.execute("UPDATE users SET is_admin = 1 WHERE username = 'mod'")
    mid, _ = _market(db)
    db.execute("INSERT INTO market_blocks (market_id, user_id, created_at) VALUES (?, ?, ?)",
               (mid, uid("boss"), appmod.now_str()))
    _post(c["mod"], f"/admin/markets/{mid}/unblock/{uid('boss')}")
    assert _q(db, "SELECT COUNT(*) FROM market_blocks WHERE market_id = ?", mid) == 1


def test_people_csv_neutralises_formulas_and_is_private():
    db = _fresh_db()
    _user(db, "-cmd")
    db.execute("UPDATE users SET email = '=HYPERLINK(1)@x.com' WHERE username = '-cmd'")
    appmod.write_email_csv(db)
    text = open(appmod.email_csv_path(), encoding="utf-8-sig").read()
    assert "'-cmd,'=HYPERLINK(1)@x.com" in text
    if os.name != "nt":
        assert os.stat(appmod.email_csv_path()).st_mode & 0o777 == 0o600


def test_signup_rejects_an_email_another_signup_is_confirming(monkeypatch):
    db = _fresh_db()
    sent = _capture_codes(monkeypatch)
    c1, c2 = appmod.app.test_client(), appmod.app.test_client()
    form = lambda c, name, email: {"csrf_token": csrf(c.get("/register")), "username": name, "email": email,
                                   "password": "secret1", "confirm": "secret1"}
    c1.post("/register", data=form(c1, "first", "same@example.com"))
    page = c2.post("/register", data=form(c2, "second", "SAME@example.com"), follow_redirects=True).get_data(as_text=True)
    assert "already used" in page and _q(db, "SELECT COUNT(*) FROM users WHERE username = 'second'") == 0
    assert len(sent) == 1
    # an unconfirmed claim lapses after EMAIL_CLAIM_HOURS
    db.execute("UPDATE login_challenges SET requested_at = ?", (appmod.time.time() - 25 * 3600,))
    appmod._hits.clear()
    c2.post("/register", data=form(c2, "second", "same@example.com"))
    assert _q(db, "SELECT COUNT(*) FROM users WHERE username = 'second'") == 1 and len(sent) == 2


def test_codes_at_most_once_a_minute_per_account_and_per_address(monkeypatch):
    db = _fresh_db()
    sent = _capture_codes(monkeypatch)
    a, b = appmod.app.test_client(), appmod.app.test_client()
    signup(a, "alpha")
    signup(b, "bravo")
    db.execute("UPDATE users SET email = NULL, email_verified = 0")
    _email_post(a, action="send", email="one@example.com")
    assert "Wait" in _email_post(a, action="resend").get_data(as_text=True)
    _email_post(a, action="change")
    # a different address doesn't skip the wait
    assert "Wait" in _email_post(a, action="send", email="two@example.com").get_data(as_text=True)
    assert len(sent) == 1
    # another account can't make the site email the same address again straight away
    assert "Wait" in _email_post(b, action="send", email="one@example.com").get_data(as_text=True)
    assert len(sent) == 1
    # a minute later it's fine
    for k in list(appmod._hits):
        appmod._hits[k] = [t - 61 for t in appmod._hits[k]]
    _email_post(a, action="send", email="two@example.com")
    assert len(sent) == 2 and sent[-1][0] == "two@example.com"


def test_turnstile_on_signup(monkeypatch):
    import io
    import json as _json
    db = _fresh_db()
    _capture_codes(monkeypatch)
    c = appmod.app.test_client()
    form = lambda name, **kw: {"csrf_token": csrf(c.get("/register")), "username": name, "email": f"{name}@example.com",
                               "password": "secret1", "confirm": "secret1", **kw}
    # off by default: no widget, sign-up works
    assert "cf-turnstile" not in c.get("/register").get_data(as_text=True)

    monkeypatch.setattr(appmod, "TURNSTILE_SITE_KEY", "site-key")
    monkeypatch.setattr(appmod, "TURNSTILE_SECRET", "secret-key")
    seen = []

    def fake_urlopen(req, timeout):
        body = dict(x.split("=") for x in req.data.decode().split("&"))
        seen.append(body)
        return io.BytesIO(_json.dumps({"success": body["response"] == "good"}).encode())
    monkeypatch.setattr(appmod.urllib.request, "urlopen", fake_urlopen)

    r = c.get("/register")
    html = r.get_data(as_text=True)
    assert 'data-sitekey="site-key"' in html and "challenges.cloudflare.com/turnstile/v0/api.js" in html
    csp = r.headers["Content-Security-Policy"]
    assert "frame-src https://challenges.cloudflare.com" in csp and "script-src 'self'" in csp

    assert "verify you" in c.post("/register", data=form("botty"), follow_redirects=True).get_data(as_text=True)
    assert "verify you" in c.post("/register", data=form("botty", **{"cf-turnstile-response": "fake"}),
                                  follow_redirects=True).get_data(as_text=True)
    assert _q(db, "SELECT COUNT(*) FROM users WHERE username = 'botty'") == 0
    assert seen[-1]["secret"] == "secret-key"
    c.post("/register", data=form("human", **{"cf-turnstile-response": "good"}))
    assert _q(db, "SELECT COUNT(*) FROM users WHERE username = 'human'") == 1

    # Cloudflare unreachable: reject the sign-up rather than let bots through
    def down(req, timeout):
        raise OSError("network unreachable")
    monkeypatch.setattr(appmod.urllib.request, "urlopen", down)
    c2 = appmod.app.test_client()
    c2.post("/register", data={"csrf_token": csrf(c2.get("/register")), "username": "later", "email": "later@example.com",
                               "password": "secret1", "confirm": "secret1", "cf-turnstile-response": "x"})
    assert _q(db, "SELECT COUNT(*) FROM users WHERE username = 'later'") == 0
