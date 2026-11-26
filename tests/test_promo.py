"""The Black Friday bonus: who qualifies, when, and that it's only paid once."""
import os
import sys
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_market import _fresh_db, _market, _user, signup, appmod  # noqa: E402

BONUS = appmod.BLACK_FRIDAY_BONUS


@pytest.fixture
def db(monkeypatch):
    """Promotions on, and today is Black Friday."""
    monkeypatch.setattr(appmod, "OWNER_USERNAME", "")
    monkeypatch.setattr(appmod, "PROMOS_ON", True)
    monkeypatch.setattr(appmod, "black_friday", lambda year: date.today())
    old = appmod.DB_PATH
    db = _fresh_db()
    yield db
    db.close()
    appmod.DB_PATH = old


def existing(db, name):
    uid = _user(db, name)
    db.execute("UPDATE users SET created_at = '2026-01-05 10:00:00' WHERE id = ?", (uid,))
    return uid


def balance(db, uid):
    return db.execute("SELECT balance FROM users WHERE id = ?", (uid,)).fetchone()[0]


def bonuses(db, uid):
    return db.execute("SELECT COUNT(*) FROM activity WHERE user_id = ? AND detail = 'Black Friday bonus'",
                      (uid,)).fetchone()[0]


def midnight(db, monkeypatch):
    """Black Friday ends: move everything that happened today back a day, then run the settlement."""
    db.execute("UPDATE trades SET created_at = datetime(created_at, '-1 day')")
    db.execute("UPDATE users SET created_at = datetime(created_at, '-1 day') WHERE created_at > '2026-06-01'")
    monkeypatch.setattr(appmod, "black_friday", lambda year: date.today() - timedelta(days=1))
    monkeypatch.setattr(appmod, "_bf_settled_year", None)
    appmod.settle_black_friday(db)


def test_new_signup_qualifies_by_betting_ten_on_one_market_and_is_paid_at_midnight(db, monkeypatch):
    uid = _user(db, "newbie")
    mid, (a, _) = _market(db)
    msg = appmod.execute_trade(db, uid, mid, a, "buy", 9)
    assert "Black Friday" not in msg
    msg = appmod.execute_trade(db, uid, mid, a, "buy", 1)  # Ⓜ10 in total on this market
    assert "qualified" in msg and bonuses(db, uid) == 0   # not paid yet
    before = balance(db, uid)
    midnight(db, monkeypatch)
    assert bonuses(db, uid) == 1 and balance(db, uid) == pytest.approx(before + BONUS)
    monkeypatch.setattr(appmod, "_bf_settled_year", None)
    appmod.settle_black_friday(db)  # settling again pays nobody twice
    appmod.execute_trade(db, uid, mid, a, "buy", 20)
    assert bonuses(db, uid) == 1


def test_existing_players_need_two_markets(db, monkeypatch):
    uid = existing(db, "veteran")
    loner = existing(db, "loner")
    m1, (a1, _) = _market(db, b=301)
    m2, (a2, _) = _market(db, b=302)
    appmod.execute_trade(db, uid, m1, a1, "buy", 10)
    appmod.execute_trade(db, uid, m2, a2, "buy", 10)
    appmod.execute_trade(db, loner, m1, a1, "buy", 10)
    appmod.execute_trade(db, loner, m1, a1, "buy", 10)  # Ⓜ20, but on one market
    midnight(db, monkeypatch)
    assert bonuses(db, uid) == 1 and bonuses(db, loner) == 0


def test_buying_and_selling_straight_back_earns_nothing(db, monkeypatch):
    flipper = _user(db, "flipper")
    keeper = _user(db, "keeper")
    mid, (a, _) = _market(db)
    appmod.execute_trade(db, flipper, mid, a, "buy", 10)
    shares = db.execute("SELECT shares FROM positions WHERE user_id = ?", (flipper,)).fetchone()[0]
    appmod.execute_trade(db, flipper, mid, a, "sell", shares)
    appmod.execute_trade(db, keeper, mid, a, "buy", 10)
    midnight(db, monkeypatch)
    assert bonuses(db, flipper) == 0 and bonuses(db, keeper) == 1


def test_making_a_market_qualifies_anyone(db):
    for uid in (existing(db, "maker"), _user(db, "newmaker")):
        closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        mid = appmod.create_market(db, f"Mine {uid}?", "", "Other", closes, ["Yes", "No"], 300, created_by=uid)
        appmod.place_creator_stake(db, mid, uid, 0, appmod.CREATOR_MIN_BET)
        assert bonuses(db, uid) == 1


def test_a_black_friday_proposal_pays_when_approved_later(db, monkeypatch):
    uid = existing(db, "proposer")
    db.execute("INSERT INTO proposals (user_id, question, outcomes, closes_at, created_at) VALUES (?, 'Q?', '[]', "
               "'2099-01-01 00:00:00', ?)", (uid, appmod.now_str()))
    # approved the next day: the market's created_at is after Black Friday, but the proposal was made on it
    monkeypatch.setattr(appmod, "black_friday", lambda year: date.today() - timedelta(days=1))
    db.execute("UPDATE proposals SET created_at = ?", ((datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 12:00:00"),))
    closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    mid = appmod.create_market(db, "Q?", "", "Other", closes, ["Yes", "No"], 300, created_by=uid)
    db.execute("UPDATE proposals SET market_id = ?", (mid,))
    appmod.place_creator_stake(db, mid, uid, 0, 10)
    assert bonuses(db, uid) == 1


def test_nothing_on_other_days_or_for_later_signups(db, monkeypatch):
    uid = existing(db, "early")
    m1, (a1, _) = _market(db, b=301)
    m2, (a2, _) = _market(db, b=302)
    monkeypatch.setattr(appmod, "black_friday", lambda year: date.today() + timedelta(days=1))  # not yet
    appmod.execute_trade(db, uid, m1, a1, "buy", 10)
    appmod.execute_trade(db, uid, m2, a2, "buy", 10)
    assert bonuses(db, uid) == 0
    late = _user(db, "latecomer")  # signed up the day after Black Friday
    monkeypatch.setattr(appmod, "black_friday", lambda year: date.today() - timedelta(days=1))
    monkeypatch.setattr(appmod, "_bf_settled_year", None)
    appmod.settle_black_friday(db)
    appmod.execute_trade(db, late, m1, a1, "buy", 10)
    appmod.execute_trade(db, late, m2, a2, "buy", 10)
    assert bonuses(db, late) == 0 and bonuses(db, uid) == 0  # the earlier trades weren't on Black Friday either


def test_held_accounts_get_nothing(db):
    uid = _user(db, "held")
    db.execute("UPDATE users SET held = 1 WHERE id = ?", (uid,))
    mid, (a, _) = _market(db)
    appmod.execute_trade(db, uid, mid, a, "buy", 10)
    assert bonuses(db, uid) == 0


def test_banner_shows_the_offer_until_it_is_earned(db):
    mid, (a, _) = _market(db)
    m2, (a2, _) = _market(db, b=302)
    page = appmod.app.test_client().get("/").get_data(as_text=True)
    assert "sign up, then make a market" in page
    c = appmod.app.test_client()
    signup(c, "vet")
    uid = db.execute("SELECT id FROM users WHERE username = 'vet'").fetchone()[0]
    db.execute("UPDATE users SET created_at = '2026-01-05 10:00:00' WHERE id = ?", (uid,))
    assert "(0/2 so far)" in c.get("/").get_data(as_text=True)
    appmod.execute_trade(db, uid, mid, a, "buy", 10)
    assert "(1/2 so far)" in c.get("/").get_data(as_text=True)
    appmod.execute_trade(db, uid, m2, a2, "buy", 10)
    assert "paid after midnight" in c.get("/").get_data(as_text=True)
    appmod.award_black_friday(db, uid, final=True)
    assert "Black Friday" not in c.get("/").get_data(as_text=True)


def test_banner_is_hidden_on_ordinary_days(db, monkeypatch):
    monkeypatch.setattr(appmod, "black_friday", lambda year: date.today() + timedelta(days=1))
    assert "Black Friday" not in appmod.app.test_client().get("/").get_data(as_text=True)


def test_black_friday_dates():
    assert appmod.black_friday.__module__ == "app"
    assert [appmod.black_friday(y) for y in (2025, 2026, 2027, 2028)] == [
        date(2025, 11, 28), date(2026, 11, 27), date(2027, 11, 26), date(2028, 11, 24)]
