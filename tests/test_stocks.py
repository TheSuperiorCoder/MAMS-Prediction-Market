import io
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAMS_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import app as appmod  # noqa: E402

RULES = "Settles on the official closing price of the stock."


def csrf(resp):
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', resp.get_data(as_text=True)).group(1)


def signup(client, name, admin=False):
    db = appmod.connect()
    cur = db.execute(
        "INSERT INTO users (username, password_hash, balance, is_admin, created_at, email, email_verified) "
        "VALUES (?, ?, ?, ?, ?, ?, 1)",
        (name, appmod.generate_password_hash("secret1"), appmod.STARTING_BALANCE, int(admin), appmod.now_str(),
         f"{name}@example.com"))
    with client.session_transaction() as s:
        s["uid"] = cur.lastrowid
    uid = cur.lastrowid
    db.close()
    return uid


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A fresh database, no real price lookups, and no background checks."""
    monkeypatch.setattr(appmod, "OWNER_USERNAME", "")
    monkeypatch.setattr(appmod, "STOCK_AUTO", False)
    prices = {}  # ticker -> list of (date, close, final); a missing ticker is unknown

    def fake_fetch(ticker, start, end):
        if ticker not in prices:
            raise appmod.StockNotFound(ticker)
        return [b for b in prices[ticker] if start <= b[0] <= end]

    monkeypatch.setattr(appmod, "fetch_stock_closes", fake_fetch)
    old = appmod.DB_PATH
    appmod.DB_PATH = str(tmp_path / "stocks.db")
    appmod._hits.clear()
    appmod.init_db()
    db = appmod.connect()
    yield db, prices
    db.close()
    appmod.DB_PATH = old


def stock_market(db, line=350, category="Stocks", ticker="AAPL", day=None, closed=True):
    """An Over/Under market on `ticker` whose betting closed yesterday at 9am (or closes tomorrow)."""
    closes = datetime.now() + (timedelta(days=-1) if closed else timedelta(days=1))
    closes = closes.replace(hour=9, minute=0, second=0, microsecond=0)
    day = day or closes.date().isoformat()
    mid = appmod.create_market(db, f"Will {ticker} close over {line}?", RULES, category,
                               closes.strftime("%Y-%m-%d %H:%M:%S"), appmod.ou_labels(line, ""), 1000,
                               ou_line=line, stock_ticker=ticker, stock_date=day)
    return mid, date.fromisoformat(day)


def row(db, mid):
    return db.execute("SELECT * FROM markets WHERE id = ?", (mid,)).fetchone()


def test_a_stock_over_under_proposes_its_own_result_and_pays_out(env):
    db, prices = env
    admin_id = signup(appmod.app.test_client(), "boss", admin=True)
    mid, day = stock_market(db, line=350)
    over, under = appmod.get_outcomes(db, mid)
    db.execute("UPDATE markets SET closes_at = ? WHERE id = ?",
               ((datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"), mid))
    appmod.execute_trade(db, admin_id, mid, over["id"], "buy", 100)
    db.execute("UPDATE markets SET closes_at = ? WHERE id = ?",
               (datetime.combine(day, datetime.min.time()).replace(hour=9).strftime("%Y-%m-%d %H:%M:%S"), mid))

    prices["AAPL"] = [(day, 360.25, True)]
    with appmod.app.app_context():
        assert appmod.check_stock_markets(db) == 1
    m = row(db, mid)
    assert m["proposed_outcome_id"] == over["id"] and m["proposed_by"] is None
    assert m["ou_result"] == 360.25 and "360.25" in m["stock_note"]
    assert db.execute("SELECT actor_name FROM audit_log WHERE action = 'result.propose'").fetchone()[0] == "system"
    with appmod.app.app_context():
        assert appmod.check_stock_markets(db) == 0  # already proposed: nothing more to do

    # the usual challenge window still applies, then winners are paid
    page = appmod.app.test_client().get(f"/market/{mid}").get_data(as_text=True)
    assert "Proposed automatically from the closing price" in page
    db.execute("UPDATE markets SET finalizes_at = ? WHERE id = ?", ("2000-01-01 00:00:00", mid))
    before = db.execute("SELECT balance FROM users WHERE id = ?", (admin_id,)).fetchone()[0]
    appmod.finalize_due(db)
    assert row(db, mid)["status"] == "resolved" and row(db, mid)["resolved_outcome_id"] == over["id"]
    assert db.execute("SELECT balance FROM users WHERE id = ?", (admin_id,)).fetchone()[0] > before


def test_under_wins_below_the_line_and_the_next_trading_day_counts(env):
    db, prices = env
    mid, day = stock_market(db, line=350)
    prices["AAPL"] = [(day + timedelta(days=1), 349.99, True)]  # the price date itself had no trading
    with appmod.app.app_context():
        appmod.check_stock_markets(db)
    assert row(db, mid)["proposed_outcome_id"] == appmod.get_outcomes(db, mid)[1]["id"]


def test_the_checker_waits_for_a_final_close_and_hands_odd_cases_to_admins(env):
    db, prices = env
    waiting, day = stock_market(db, ticker="AAPL")
    push, _ = stock_market(db, ticker="MSFT", line=400)
    unknown, _ = stock_market(db, ticker="NOPE")
    no_data, _ = stock_market(db, ticker="TSLA")
    prices.update(AAPL=[(day, 351.0, False)], MSFT=[(day, 400.0, True)], TSLA=[])
    with appmod.app.app_context():
        assert appmod.check_stock_markets(db) == 0
    assert row(db, waiting)["stock_status"] == "" and "Waiting" in row(db, waiting)["stock_note"]
    assert row(db, no_data)["stock_status"] == "" and "Waiting" in row(db, no_data)["stock_note"]
    assert row(db, push)["stock_status"] == "push" and "exactly" in row(db, push)["stock_note"]
    assert row(db, unknown)["stock_status"] == "error"
    assert all(row(db, m)["proposed_outcome_id"] is None for m in (waiting, push, unknown, no_data))

    # once the session is over the waiting one settles; the others are left for an admin
    prices["AAPL"] = [(day, 351.0, True)]
    with appmod.app.app_context():
        assert appmod.check_stock_markets(db) == 1
    assert row(db, push)["proposed_outcome_id"] is None


def test_only_closed_stocks_markets_settle_themselves(env):
    db, prices = env
    still_open, day = stock_market(db, closed=False)
    other_cat, _ = stock_market(db, category="Sports")
    reopened, rday = stock_market(db)
    db.execute("UPDATE markets SET closes_at = ? WHERE id = ?", (appmod.now_str(), reopened))  # reopened, closed today
    prices["AAPL"] = [(d, 999.0, True) for d in (day - timedelta(days=3), rday, day)]
    with appmod.app.app_context():
        assert appmod.check_stock_markets(db) == 0
    assert row(db, still_open)["proposed_outcome_id"] is None
    assert row(db, other_cat)["proposed_outcome_id"] is None and row(db, other_cat)["stock_note"] == ""
    # betting closed after the price date's close was known: hand it to the admins
    assert row(db, reopened)["stock_status"] == "manual"


def test_withdrawing_an_automatic_result_stops_the_checker(env):
    db, prices = env
    mid, day = stock_market(db)
    prices["AAPL"] = [(day, 360.0, True)]
    with appmod.app.app_context():
        appmod.check_stock_markets(db)
    appmod.retract_result(db, mid)
    with appmod.app.app_context():
        assert appmod.check_stock_markets(db) == 0
    assert row(db, mid)["proposed_outcome_id"] is None and row(db, mid)["stock_status"] == "manual"


def test_admins_can_move_an_existing_over_under_to_stocks(env):
    db, prices = env
    admin, joe = appmod.app.test_client(), appmod.app.test_client()
    signup(admin, "boss", admin=True)
    signup(joe, "joe")
    closes = (datetime.now() + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
    mid = appmod.create_market(db, "Will $NVDA close over 200?", RULES, "Other", closes.strftime("%Y-%m-%d %H:%M:%S"),
                               appmod.ou_labels(200, ""), 1000, ou_line=200)
    yesno = appmod.create_market(db, "Will it rain?", RULES, "Weather", closes.strftime("%Y-%m-%d %H:%M:%S"),
                                 ["Yes", "No"], 1000)
    prices["NVDA"] = []

    page = admin.get(f"/market/{mid}").get_data(as_text=True)
    assert "Move to Stocks" in page and 'value="NVDA"' in page  # the ticker is guessed from the question
    assert f'value="{closes.date().isoformat()}"' in page
    assert "Move to Stocks" not in admin.get(f"/market/{yesno}").get_data(as_text=True)
    tok = csrf(admin.get(f"/market/{mid}"))
    assert joe.post(f"/admin/markets/{mid}/stock", data={"csrf_token": csrf(joe.get(f"/market/{mid}")),
                                                         "stock_ticker": "NVDA"}).status_code == 403

    # a date before betting closes, an unknown ticker and a Yes/No market are all refused
    early = (closes.date() - timedelta(days=1)).isoformat()
    r = admin.post(f"/admin/markets/{mid}/stock", data={"csrf_token": tok, "stock_ticker": "NVDA",
                                                        "stock_date": early}, follow_redirects=True)
    assert "Betting has to close before" in r.get_data(as_text=True)
    r = admin.post(f"/admin/markets/{mid}/stock", data={"csrf_token": tok, "stock_ticker": "ZZZZ"},
                   follow_redirects=True)
    assert "doesn&#39;t know the ticker ZZZZ" in r.get_data(as_text=True)
    r = admin.post(f"/admin/markets/{yesno}/stock", data={"csrf_token": tok, "stock_ticker": "NVDA"},
                   follow_redirects=True)
    assert "Convert it to Over/Under first" in r.get_data(as_text=True)
    assert row(db, mid)["category"] == "Other" and row(db, yesno)["category"] == "Weather"

    r = admin.post(f"/admin/markets/{mid}/stock", data={"csrf_token": tok, "stock_ticker": "$nvda"},
                   follow_redirects=True)
    assert "Moved to Stocks" in r.get_data(as_text=True)
    m = row(db, mid)
    assert (m["category"], m["stock_ticker"], m["stock_date"]) == ("Stocks", "NVDA", closes.date().isoformat())
    assert "Moved to Stocks: Resolves automatically from NVDA" in db.execute(
        "SELECT body FROM clarifications WHERE market_id = ?", (mid,)).fetchone()[0]
    assert db.execute("SELECT 1 FROM audit_log WHERE action = 'market.stock'").fetchone()
    page = joe.get(f"/market/{mid}").get_data(as_text=True)
    assert "Resolves automatically from NVDA" in page
    assert "NVDA close" in joe.get("/").get_data(as_text=True)

    # and back off again
    r = admin.post(f"/admin/markets/{mid}/stock", data={"csrf_token": tok, "decision": "off"}, follow_redirects=True)
    assert "no longer resolves itself" in r.get_data(as_text=True)
    assert row(db, mid)["stock_ticker"] is None


def test_new_stocks_over_unders_need_a_ticker(env):
    closes = (datetime.now() + timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)
    form = {"kind": "overunder", "question": "Will AAPL close over 350?", "description": RULES,
            "category": "Stocks", "closes_at": closes.strftime("%Y-%m-%dT%H:%M"), "ou_line": "350"}
    fields, error = appmod.read_market_form(form)
    assert fields is None and "ticker" in error
    fields, error = appmod.read_market_form({**form, "stock_ticker": "aapl"})
    # betting closes after 4pm, so the price date defaults to the day after
    assert error is None and fields["stock_ticker"] == "AAPL"
    assert fields["stock_date"] == (closes.date() + timedelta(days=1)).isoformat()
    fields, error = appmod.read_market_form({**form, "stock_ticker": "aapl", "stock_date": closes.date().isoformat()})
    assert "Betting has to close before" in error
    # other categories and kinds ignore the ticker
    fields, _ = appmod.read_market_form({**form, "category": "Sports", "stock_ticker": "AAPL"})
    assert fields["stock_ticker"] is None


def test_a_proposal_keeps_its_ticker_through_approval(env):
    db, prices = env
    admin, sue = appmod.app.test_client(), appmod.app.test_client()
    signup(admin, "boss", admin=True)
    signup(sue, "sue")
    closes = (datetime.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    form = {"kind": "overunder", "question": "Will AAPL close over 350?", "description": RULES,
            "category": "Stocks", "closes_at": closes.strftime("%Y-%m-%dT%H:%M"), "ou_line": "350",
            "stock_ticker": "AAPL", "stake": "10", "stake_pick": "0"}
    sue.post("/propose", data={**form, "csrf_token": csrf(sue.get("/propose"))})
    p = db.execute("SELECT * FROM proposals").fetchone()
    assert (p["stock_ticker"], p["stock_date"]) == ("AAPL", closes.date().isoformat())
    page = admin.get(f"/admin/proposals/{p['id']}")
    assert 'value="AAPL"' in page.get_data(as_text=True)
    admin.post(f"/admin/proposals/{p['id']}", data={**form, "decision": "approve", "csrf_token": csrf(page)})
    m = db.execute("SELECT * FROM markets").fetchone()
    assert (m["category"], m["stock_ticker"], m["stock_date"]) == ("Stocks", "AAPL", closes.date().isoformat())


def test_background_check_only_runs_when_a_market_is_due(env, monkeypatch):
    db, prices = env
    started = []

    class FakeThread:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            started.append(1)

    monkeypatch.setattr(appmod.threading, "Thread", FakeThread)
    monkeypatch.setattr(appmod, "STOCK_AUTO", True)
    monkeypatch.setattr(appmod, "_stock_last_check", 0.0)
    appmod.maybe_check_stocks(db)
    assert started == []  # nothing due: no thread, no network
    stock_market(db)
    appmod.maybe_check_stocks(db)
    assert started == []  # checked a moment ago
    monkeypatch.setattr(appmod, "_stock_last_check", 0.0)
    appmod.maybe_check_stocks(db)
    assert started == [1]


def test_fetch_reads_closes_and_knows_when_a_day_is_final(monkeypatch):
    off = -5 * 3600
    today = appmod._epoch_day(time.time() + off)
    yesterday = today - timedelta(days=1)

    def stamp(d, hour):  # an exchange-local time as a Unix timestamp
        return int((datetime.combine(d, datetime.min.time()) - datetime(1970, 1, 1)).total_seconds()) + hour * 3600 - off

    def payload(session_end):
        return {"chart": {"result": [{
            "meta": {"gmtoffset": off, "currentTradingPeriod": {"regular": {"end": session_end}}},
            "timestamp": [stamp(yesterday, 9.5), stamp(today, 9.5)],
            "indicators": {"quote": [{"close": [101.239999, 102.5]}]}}]}}

    def serve(body):
        monkeypatch.setattr(appmod.urllib.request, "urlopen",
                            lambda req, timeout: io.BytesIO(json.dumps(body).encode()))

    serve(payload(int(time.time()) + 3600))  # today's session still running
    assert appmod.fetch_stock_closes("AAPL", yesterday, today) == [(yesterday, 101.24, True), (today, 102.5, False)]
    serve(payload(int(time.time()) - 3600))  # ended an hour ago
    assert appmod.fetch_stock_closes("AAPL", yesterday, today)[-1] == (today, 102.5, True)
    serve({"chart": {"result": None, "error": {"code": "Not Found"}}})
    with pytest.raises(appmod.StockNotFound):
        appmod.fetch_stock_closes("NOPE", yesterday, today)

    def boom(req, timeout):
        raise OSError("no network")
    monkeypatch.setattr(appmod.urllib.request, "urlopen", boom)
    with pytest.raises(appmod.StockFetchError):
        appmod.fetch_stock_closes("AAPL", yesterday, today)
