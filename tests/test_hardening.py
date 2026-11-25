"""Throttling of trades, proposals and heavy pages; the net-worth cache; locking the owner to a confirmed email."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_market import _fresh_db, _market, csrf, signup, appmod  # noqa: E402


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(appmod, "OWNER_USERNAME", "")
    monkeypatch.setattr(appmod, "OWNER_EMAIL", "")
    old = appmod.DB_PATH
    appmod._net_worth_cache.clear()
    yield
    appmod.DB_PATH = old


def _trade(client, mid, oid, amount=1):
    token = csrf(client.get(f"/market/{mid}"))
    return client.post(f"/market/{mid}/trade", data={"csrf_token": token, "outcome_id": oid, "action": "buy",
                                                     "value": amount}, follow_redirects=True)


def test_trades_are_rate_limited_per_user(monkeypatch):
    monkeypatch.setattr(appmod, "TRADE_LIMITS", ((3, 60),))
    db = _fresh_db()
    mid, (yes, _) = _market(db)
    c = appmod.app.test_client()
    signup(c, "spammer")
    for _ in range(3):
        assert "too fast" not in _trade(c, mid, yes).get_data(as_text=True)
    assert "too fast" in _trade(c, mid, yes).get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3
    other = appmod.app.test_client()                     # someone else isn't affected
    signup(other, "normal")
    assert "too fast" not in _trade(other, mid, yes).get_data(as_text=True)


def test_proposals_are_rate_limited(monkeypatch):
    monkeypatch.setattr(appmod, "PROPOSE_LIMIT", (1, 3600))
    _fresh_db()
    c = appmod.app.test_client()
    signup(c, "proposer")
    form = lambda: {"csrf_token": csrf(c.get("/propose"))}
    c.post("/propose", data=form())                       # invalid form, but it counts towards the limit
    assert "a lot of proposals" in c.post("/propose", data=form()).get_data(as_text=True)


def test_heavy_pages_answer_429_when_hammered(monkeypatch):
    monkeypatch.setattr(appmod, "HEAVY_PAGE_LIMITS", {"user": (2, 60), "ip": (3, 60)})
    _fresh_db()
    anon = appmod.app.test_client()
    assert [anon.get("/leaderboard").status_code for _ in range(4)] == [200, 200, 200, 429]
    assert anon.get("/how-it-works").status_code == 200   # light pages aren't throttled
    c = appmod.app.test_client()
    signup(c, "reader")
    assert [c.get("/loans/all").status_code for _ in range(3)] == [200, 200, 429]


def test_net_worth_cache_is_reused_and_dropped_when_anything_changes(monkeypatch):
    db = _fresh_db()
    mid, (yes, _) = _market(db)
    c = appmod.app.test_client()
    signup(c, "ann")
    calls = []
    real = appmod._compute_net_worths
    monkeypatch.setattr(appmod, "_compute_net_worths", lambda d: calls.append(1) or real(d))
    first = appmod.net_worths(db)
    assert appmod.net_worths(db) == first and len(calls) == 1         # reused
    _trade(c, mid, yes, 50)
    appmod.net_worths(db)
    assert len(calls) == 2                                            # a trade invalidates it
    db.execute("UPDATE users SET balance = balance + 5")
    after = appmod.net_worths(db)
    assert len(calls) == 3 and after[1] == pytest.approx(first[1] + 5, abs=0.02)
    after[1] = -1                                                     # callers can't corrupt the cached copy
    assert appmod.net_worths(db)[1] != -1


def test_owner_needs_the_confirmed_owner_email_when_one_is_set(monkeypatch):
    monkeypatch.setattr(appmod, "OWNER_USERNAME", "Kaushik")
    monkeypatch.setattr(appmod, "OWNER_EMAIL", "real.owner@example.com")
    db = _fresh_db()
    db.execute("INSERT INTO users (username, password_hash, balance, created_at, email, email_verified) "
               "VALUES ('Kaushik', 'x', 1000, ?, 'imposter@example.com', 1)", (appmod.now_str(),))
    assert appmod.owner_id(db) is None                                # wrong address: no owner powers
    db.execute("UPDATE users SET email = 'Real.Owner+mams@example.com', email_verified = 0")
    assert appmod.owner_id(db) is None                                # right address, not confirmed yet
    db.execute("UPDATE users SET email_verified = 1")
    assert appmod.owner_id(db) is not None                            # confirmed (aliases count)


def test_owner_email_unset_keeps_the_username_rule(monkeypatch):
    monkeypatch.setattr(appmod, "OWNER_USERNAME", "Kaushik")
    db = _fresh_db()
    uid = db.execute("INSERT INTO users (username, password_hash, balance, created_at) "
                     "VALUES ('Kaushik', 'x', 1000, ?)", (appmod.now_str(),)).lastrowid
    assert appmod.owner_id(db) == uid


def test_app_starts_on_a_database_with_a_running_loan_from_before_the_loan_rules(tmp_path):
    """The live site crashed on start-up: init_db() runs while app.py is still loading, and with an active loan and no
    loans_v2_at setting it needed now_str() before it was defined. Import the app fresh, in its own process."""
    import subprocess
    db = _fresh_db()
    ann = db.execute("INSERT INTO users (username, password_hash, balance, created_at) VALUES ('ann', 'x', 0, ?)",
                     (appmod.now_str(),)).lastrowid
    bob = db.execute("INSERT INTO users (username, password_hash, balance, created_at) VALUES ('bob', 'x', 0, ?)",
                     (appmod.now_str(),)).lastrowid
    db.execute("INSERT INTO loans (lender_id, borrower_id, principal, rate, days, owed, status, due_at, created_at) "
               "VALUES (?, ?, 10, 0, 7, 10, 'active', '2099-01-01 00:00:00', ?)", (ann, bob, appmod.now_str()))
    db.execute("DELETE FROM site_settings WHERE key = 'loans_v2_at'")
    db.close()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, MAMS_DB=appmod.DB_PATH)
    r = subprocess.run([sys.executable, "-c", "import app"], cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    check = appmod.connect().execute("SELECT value FROM site_settings WHERE key = 'loans_v2_at'").fetchone()
    assert check and check[0] != "1970-01-01 00:00:00"
