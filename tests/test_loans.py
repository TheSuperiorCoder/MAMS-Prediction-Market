"""Player-to-player loans."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_market import _fresh_db, _post, _user, past, signup, appmod  # noqa: E402


@pytest.fixture(autouse=True)
def restore_db_path():
    old = appmod.DB_PATH
    yield
    appmod.DB_PATH = old


def bal(db, uid):
    return db.execute("SELECT balance FROM users WHERE id = ?", (uid,)).fetchone()[0]


def loan(db, lid):
    return db.execute("SELECT * FROM loans WHERE id = ?", (lid,)).fetchone()


def test_offer_borrow_and_repay():
    db = _fresh_db()
    ann, bob = _user(db, "ann"), _user(db, "bob")
    before = appmod.net_worths(db)
    lid = appmod.offer_loan(db, ann, "", 100, 10, 7)
    assert bal(db, ann) == 900                                    # held while the offer is open
    assert appmod.net_worths(db)[ann] == pytest.approx(before[ann])

    owed = appmod.accept_loan(db, lid, bob)
    assert owed == 110 and bal(db, bob) == 1100
    l = loan(db, lid)
    assert l["status"] == "active" and l["borrower_id"] == bob and l["owed"] == 110 and l["due_at"] > appmod.now_str()
    worth = appmod.net_worths(db)
    assert worth[ann] == pytest.approx(1010) and worth[bob] == pytest.approx(990)  # interest counts right away
    assert worth[ann] + worth[bob] == pytest.approx(before[ann] + before[bob])     # zero-sum

    assert appmod.repay_loan(db, lid, bob, 40) == 40
    assert loan(db, lid)["owed"] == 70 and bal(db, ann) == 940
    assert appmod.repay_loan(db, lid, bob) == 70                   # None = the rest
    l = loan(db, lid)
    assert l["status"] == "repaid" and l["owed"] == 0 and l["repaid"] == 110
    assert bal(db, ann) == 1010 and bal(db, bob) == 990
    with pytest.raises(appmod.TradeError):
        appmod.repay_loan(db, lid, bob)
    kinds = [r[0] for r in db.execute("SELECT kind FROM activity WHERE user_id IN (?, ?)", (ann, bob))]
    assert kinds.count("loan") == 6  # offer, borrow, and each repayment on both sides


def test_repaying_more_than_owed_only_takes_what_is_owed():
    db = _fresh_db()
    ann, bob = _user(db, "ann"), _user(db, "bob")
    lid = appmod.offer_loan(db, ann, "bob", 50, 0, 3)
    appmod.accept_loan(db, lid, bob)
    assert appmod.repay_loan(db, lid, bob, 500) == 50
    assert loan(db, lid)["status"] == "repaid" and bal(db, bob) == 1000


def test_offer_rules():
    db = _fresh_db()
    ann, bob, cat = _user(db, "ann", balance=200), _user(db, "bob"), _user(db, "cat")
    for args, msg in [((0.5, 10, 7), "at least"), ((300, 10, 7), "only have"), ((50, -1, 7), "between"),
                      ((50, 101, 7), "between"), ((50, 10, 0), "1 to"), ((50, 10, 31), "1 to"),
                      ((float("nan"), 10, 7), "at least")]:
        with pytest.raises(appmod.TradeError, match=msg):
            appmod.offer_loan(db, ann, "", *args)
    with pytest.raises(appmod.TradeError, match="yourself"):
        appmod.offer_loan(db, ann, "ann", 50, 10, 7)
    with pytest.raises(appmod.TradeError, match="no player"):
        appmod.offer_loan(db, ann, "nobody", 50, 10, 7)
    assert bal(db, ann) == 200 and db.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 0

    lid = appmod.offer_loan(db, ann, "bob", 50, 10, 7)
    with pytest.raises(appmod.TradeError, match="someone else"):
        appmod.accept_loan(db, lid, cat)
    with pytest.raises(appmod.TradeError, match="your own"):
        appmod.accept_loan(db, lid, ann)
    with pytest.raises(appmod.TradeError):
        appmod.end_loan_offer(db, lid, cat, "declined")          # only bob can decline
    appmod.end_loan_offer(db, lid, bob, "declined")
    assert loan(db, lid)["status"] == "declined" and bal(db, ann) == 200
    with pytest.raises(appmod.TradeError, match="isn't available"):
        appmod.accept_loan(db, lid, bob)

    lid = appmod.offer_loan(db, ann, "", 50, 10, 7)
    with pytest.raises(appmod.TradeError):
        appmod.end_loan_offer(db, lid, bob, "cancelled")         # only the lender can withdraw
    appmod.end_loan_offer(db, lid, ann, "cancelled")
    assert loan(db, lid)["status"] == "cancelled" and bal(db, ann) == 200


def test_overdue_loans_are_collected_from_cash_including_later_money():
    db = _fresh_db()
    ann, bob = _user(db, "ann"), _user(db, "bob", balance=0)
    lid = appmod.offer_loan(db, ann, "bob", 100, 20, 7)
    appmod.accept_loan(db, lid, bob)                               # bob: 100 cash, owes 120
    db.execute("UPDATE users SET balance = 30 WHERE id = ?", (bob,))  # spent most of it
    appmod.collect_overdue_loans(db)
    assert loan(db, lid)["owed"] == 120                            # not due yet: nothing taken

    db.execute("UPDATE loans SET due_at = ? WHERE id = ?", (past(), lid))
    assert appmod.has_overdue_loan(db, bob)
    appmod.collect_overdue_loans(db)
    assert loan(db, lid)["penalty"] == 12                         # the 10% late fee on the 120 owed
    assert bal(db, bob) == 0 and loan(db, lid)["owed"] == 102 and bal(db, ann) == 930

    other = appmod.offer_loan(db, ann, "", 10, 0, 1)
    with pytest.raises(appmod.TradeError, match="overdue"):
        appmod.accept_loan(db, other, bob)

    db.execute("UPDATE users SET balance = 200 WHERE id = ?", (bob,))  # e.g. a payout later
    appmod.collect_overdue_loans(db)
    assert loan(db, lid)["status"] == "repaid" and bal(db, bob) == 98 and bal(db, ann) == 1022
    assert not appmod.has_overdue_loan(db, bob)
    detail = db.execute("SELECT detail FROM activity WHERE user_id = ? ORDER BY id DESC LIMIT 1", (bob,)).fetchone()[0]
    assert "Overdue loan collected" in detail and "paid off" in detail


def test_overdue_collection_runs_on_page_loads():
    db = _fresh_db()
    client = appmod.app.test_client()
    signup(client, "bob")
    bob = db.execute("SELECT id FROM users WHERE username = 'bob'").fetchone()[0]
    ann = _user(db, "ann")
    lid = appmod.offer_loan(db, ann, "bob", 100, 10, 7)
    appmod.accept_loan(db, lid, bob)
    db.execute("UPDATE loans SET due_at = ? WHERE id = ?", (past(), lid))
    client.get("/leaderboard")
    assert loan(db, lid)["status"] == "repaid"


def test_loans_page_and_routes():
    db = _fresh_db()
    ann_c, bob_c = appmod.app.test_client(), appmod.app.test_client()
    signup(ann_c, "ann")
    signup(bob_c, "bob")
    ann, bob = (db.execute("SELECT id FROM users WHERE username = ?", (n,)).fetchone()[0] for n in ("ann", "bob"))
    page = _post(ann_c, "/loans/offer", amount="100", rate="12.5", days="5", borrower="bob", note="good luck")
    html = page.get_data(as_text=True)
    assert "Offer posted" in html and "waiting to be taken" in html
    lid = db.execute("SELECT id FROM loans").fetchone()[0]

    html = bob_c.get("/loans").get_data(as_text=True)
    assert "just for you" in html and "good luck" in html and "Ⓜ112.50" in html
    assert 'class="badge"' in html                                  # nav badge for the offer
    assert "Loan taken" in _post(bob_c, f"/loans/{lid}/accept").get_data(as_text=True)
    html = bob_c.get("/loans").get_data(as_text=True)
    assert "Loans you&#39;re paying back" in html or "Loans you're paying back" in html
    assert "you owe Ⓜ112.50" in bob_c.get("/portfolio").get_data(as_text=True)
    assert "Paid back Ⓜ12.50" in _post(bob_c, f"/loans/{lid}/repay", amount="12.50").get_data(as_text=True)
    assert "Paid back Ⓜ100.00" in _post(bob_c, f"/loans/{lid}/repay", amount="").get_data(as_text=True)
    assert bal(db, ann) == pytest.approx(1012.5)
    assert "Loan" in ann_c.get("/activity/mine?show=other").get_data(as_text=True)

    assert "Fill in" in _post(ann_c, "/loans/offer", amount="lots", rate="1", days="1").get_data(as_text=True)
    assert appmod.app.test_client().get("/loans").status_code == 302   # logged out


def test_account_with_running_loan_cannot_be_deleted_and_offers_are_refunded():
    db = _fresh_db()
    ann, bob, cat = _user(db, "ann"), _user(db, "bob"), _user(db, "cat")
    lid = appmod.offer_loan(db, ann, "bob", 100, 10, 7)
    appmod.accept_loan(db, lid, bob)
    with pytest.raises(appmod.TradeError, match="loan"):
        appmod.delete_account(db, bob)
    appmod.repay_loan(db, lid, bob)
    pending = appmod.offer_loan(db, ann, "cat", 50, 10, 7)
    appmod.delete_account(db, cat)                                  # offer to cat goes back to ann
    assert bal(db, ann) == 1010 and loan(db, pending) is None
    appmod.delete_account(db, bob)
    assert db.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 0


# ---------------------------------------------------------------- limits, freezing and collecting from bets

from datetime import datetime, timedelta  # noqa: E402

from test_market import _market  # noqa: E402


def in_default_loan(db, lender, borrower, amount=100, rate=0, hours_overdue=72):
    """A running loan whose due date was `hours_overdue` ago (the grace period is 48 hours)."""
    lid = appmod.offer_loan(db, lender, "", amount, rate, 7)
    appmod.accept_loan(db, lid, borrower)
    db.execute("UPDATE loans SET due_at = ? WHERE id = ?", (past(hours_overdue), lid))
    return lid


def borrower_with_bet(db, bet, name="bob", market=None, **loan_args):
    """ann lends bob Ⓜ100; bob bets `bet` on 'o0'; his cash is then gone and the loan is long overdue."""
    ann, bob = _user(db, "ann"), _user(db, name)
    mid, (yes, no) = market or _market(db, 2, 300)
    lid = appmod.offer_loan(db, ann, "", 100, 0, 7)
    appmod.accept_loan(db, lid, bob)
    appmod.execute_trade(db, bob, mid, yes, "buy", bet)
    db.execute("UPDATE users SET balance = 0 WHERE id = ?", (bob,))
    db.execute("UPDATE loans SET due_at = ? WHERE id = ?", (past(72), lid))
    return ann, bob, lid, mid, yes


def shares(db, uid, outcome):
    row = db.execute("SELECT shares FROM positions WHERE user_id = ? AND outcome_id = ?", (uid, outcome)).fetchone()
    return row[0] if row else 0.0


def test_borrowing_limit_and_loan_count():
    db = _fresh_db()
    ann, bob, rich = _user(db, "ann", balance=100000), _user(db, "bob"), _user(db, "rich", balance=10000)
    first = appmod.offer_loan(db, ann, "", 400, 0, 7)
    appmod.accept_loan(db, first, bob)
    assert appmod.credit_limit(db, bob) == (500.0, 400.0)           # the floor
    too_much = appmod.offer_loan(db, ann, "", 200, 0, 7)
    with pytest.raises(appmod.TradeError, match="at most Ⓜ500.00"):
        appmod.accept_loan(db, too_much, bob)
    ok = appmod.offer_loan(db, ann, "", 100, 0, 7)
    appmod.accept_loan(db, ok, bob)                                  # exactly the limit

    appmod.accept_loan(db, too_much, rich)                           # half of 10,000 is well above the floor
    mult = appmod.credit_limit_detail(db, rich)["multiplier"]        # the credit score scales the share-of-net-worth part
    assert appmod.credit_limit(db, rich)[0] == pytest.approx(0.5 * (10200 - 200) * mult)

    carl = _user(db, "carl", balance=100000)
    for _ in range(appmod.LOAN_MAX_ACTIVE):
        appmod.accept_loan(db, appmod.offer_loan(db, ann, "", 10, 0, 7), carl)
    with pytest.raises(appmod.TradeError, match="at most 5 loans"):
        appmod.accept_loan(db, appmod.offer_loan(db, ann, "", 10, 0, 7), carl)


def test_the_limit_counts_bets_and_ignores_money_you_are_owed():
    db = _fresh_db()
    ann, bob = _user(db, "ann"), _user(db, "bob")
    lid = appmod.offer_loan(db, ann, "", 1000, 100, 7)               # all her cash, at 100% interest
    assert appmod.credit_limit(db, ann)[0] == 500.0                  # cash 0: the floor
    appmod.accept_loan(db, lid, _user(db, "rich", balance=10000))
    assert appmod.credit_limit(db, ann)[0] == 500.0                  # money owed to ann doesn't raise it...
    assert appmod.net_worths(db)[ann] == pytest.approx(2000)         # ...although her net worth jumped
    mid, (yes, no) = _market(db, 2, 300)                             # bets count, at their sell-now value
    appmod.execute_trade(db, bob, mid, yes, "buy", 900)
    worth_of_bets = sum(appmod.open_book_values(db)[bob].values())
    assert appmod.credit_limit(db, bob)[0] == pytest.approx(max(500, 0.5 * (100 + worth_of_bets)))


def test_cannot_lend_while_borrowing_or_borrow_with_offers_open():
    db = _fresh_db()
    ann, bob = _user(db, "ann"), _user(db, "bob")
    lid = appmod.offer_loan(db, ann, "", 100, 0, 7)
    appmod.accept_loan(db, lid, bob)
    with pytest.raises(appmod.TradeError, match="Pay off what you've borrowed"):
        appmod.offer_loan(db, bob, "ann", 50, 0, 7)                  # can't pass borrowed money to an alt
    cat = _user(db, "cat")
    offer = appmod.offer_loan(db, cat, "", 50, 0, 7)
    other = appmod.offer_loan(db, ann, "cat", 20, 0, 7)
    with pytest.raises(appmod.TradeError, match="Withdraw your loan offers"):
        appmod.accept_loan(db, other, cat)
    appmod.end_loan_offer(db, offer, cat, "cancelled")
    appmod.accept_loan(db, other, cat)
    appmod.repay_loan(db, lid, bob)
    appmod.offer_loan(db, bob, "ann", 50, 0, 7)                      # paid off: allowed again


def test_overdue_borrower_is_frozen_but_can_sell_and_repay():
    db = _fresh_db()
    ann, bob, lid, mid, yes = borrower_with_bet(db, 50)
    assert appmod.overdue_block(db, bob)
    with pytest.raises(appmod.TradeError, match="overdue"):
        appmod.execute_trade(db, bob, mid, yes, "buy", 10)
    with pytest.raises(appmod.TradeError, match="Pay off"):
        appmod.offer_loan(db, bob, "ann", 1, 0, 7)
    appmod.execute_trade(db, bob, mid, yes, "sell", shares(db, bob, yes))   # selling is fine...
    appmod.collect_overdue_loans(db)                                         # ...and the money goes to the lender
    l = loan(db, lid)
    assert l["repaid"] > 40 and l["owed"] == pytest.approx(110 - l["repaid"])          # the sale's proceeds, all of it
    assert l["penalty"] == 10                                                           # 10% late fee on the 100
    assert bal(db, bob) == 0 and bal(db, ann) == pytest.approx(900 + l["repaid"])


def test_overdue_borrowers_open_offers_are_withdrawn_and_collected():
    db = _fresh_db()
    ann, bob = _user(db, "ann"), _user(db, "bob")
    offer = appmod.offer_loan(db, bob, "", 80, 0, 7)                 # bob lends first, then (legacy) owes ann
    db.execute("INSERT INTO loans (lender_id, borrower_id, principal, rate, days, owed, status, due_at, created_at, "
               "accepted_at) VALUES (?, ?, 100, 0, 7, 100, 'active', ?, ?, ?)",
               (ann, bob, past(2), appmod.now_str(), appmod.now_str()))
    appmod.collect_overdue_loans(db)
    assert loan(db, offer)["status"] == "cancelled"
    assert bal(db, ann) == 1110 and bal(db, bob) == 890              # 100 owed + the 10 late fee went to ann


def test_overdue_borrower_cannot_create_or_propose_markets():
    db = _fresh_db()
    client = appmod.app.test_client()
    signup(client, "bob")
    bob = db.execute("SELECT id FROM users WHERE username = 'bob'").fetchone()[0]
    ann = _user(db, "ann")
    lid = in_default_loan(db, ann, bob)
    db.execute("UPDATE loans SET owed = 5000 WHERE id = ?", (lid,))                     # more than bob can ever cover
    form = dict(question="Will it rain?", category="Other", closes_at="2099-01-01T12:00", kind="yesno", stake="20",
                stake_pick="0", description="Resolves Yes if the airport records measurable rain on that day.")
    assert "overdue loan" in _post(client, "/propose", **form).get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (bob,))
    assert "overdue loan" in _post(client, "/admin/markets", **form).get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 0


def test_collection_needs_the_grace_period_and_an_open_market():
    db = _fresh_db()
    ann, bob, lid, mid, yes = borrower_with_bet(db, 50)
    db.execute("UPDATE loans SET due_at = ? WHERE id = ?", (past(24), lid))            # overdue but inside the grace
    with pytest.raises(appmod.TradeError, match="after"):
        appmod.seize_position(db, lid, ann, yes, "sell")
    db.execute("UPDATE loans SET due_at = ? WHERE id = ?", (past(72), lid))
    with pytest.raises(appmod.TradeError, match="isn't one of your"):
        appmod.seize_position(db, lid, bob, yes, "sell")                               # only the lender can
    db.execute("UPDATE markets SET closes_at = ? WHERE id = ?", (past(1), mid))        # betting has closed
    with pytest.raises(appmod.TradeError, match="still open"):
        appmod.seize_position(db, lid, ann, yes, "sell")
    assert shares(db, bob, yes) > 0 and loan(db, lid)["status"] == "active"


def test_force_sell_takes_only_what_covers_the_debt():
    db = _fresh_db()
    ann, bob, lid, mid, yes = borrower_with_bet(db, 300)
    held = shares(db, bob, yes)
    price_before = appmod.lmsr.prices([o["shares"] for o in appmod.get_outcomes(db, mid)], 300)[0]
    msg = appmod.seize_position(db, lid, ann, yes, "sell")
    left = shares(db, bob, yes)
    assert 0 < left < held                                                              # bob keeps the rest
    assert loan(db, lid)["status"] == "repaid" and loan(db, lid)["owed"] == 0
    assert bal(db, ann) == pytest.approx(1000)                                          # 900 + the 100 owed
    assert 0 <= bal(db, bob) < 1                                                        # at most rounding is left
    price_after = appmod.lmsr.prices([o["shares"] for o in appmod.get_outcomes(db, mid)], 300)[0]
    assert price_after < price_before and "Sold" in msg
    sold = db.execute("SELECT shares, amount FROM trades WHERE user_id = ? ORDER BY id DESC LIMIT 1", (bob,)).fetchone()
    assert sold["shares"] == pytest.approx(-(held - left)) and sold["amount"] < 0


def test_force_sell_of_a_small_position_sells_everything_and_leaves_the_rest_owed():
    db = _fresh_db()
    ann, bob, lid, mid, yes = borrower_with_bet(db, 20)
    appmod.seize_position(db, lid, ann, yes, "sell")
    assert shares(db, bob, yes) == 0
    l = loan(db, lid)
    assert l["status"] == "active" and 70 < l["owed"] < 100 and l["repaid"] > 0
    assert bal(db, ann) == pytest.approx(900 + l["repaid"])
    assert not appmod.collectable_positions(db, l, ann)                                  # nothing left to collect


def test_take_over_moves_shares_and_reduces_the_debt():
    db = _fresh_db()
    ann, bob, lid, mid, yes = borrower_with_bet(db, 300)
    held = shares(db, bob, yes)
    totals = [o["shares"] for o in appmod.get_outcomes(db, mid)]
    pot = db.execute("SELECT SUM(amount) FROM trades WHERE market_id = ?", (mid,)).fetchone()[0]
    cash = bal(db, ann)
    appmod.seize_position(db, lid, ann, yes, "take")
    taken = shares(db, ann, yes)
    assert 0 < taken < held and shares(db, bob, yes) == pytest.approx(held - taken)
    assert [o["shares"] for o in appmod.get_outcomes(db, mid)] == pytest.approx(totals)   # nothing was bought or sold
    assert loan(db, lid)["status"] == "repaid"
    assert bal(db, ann) == cash                                                            # no cash moved
    assert db.execute("SELECT SUM(amount) FROM trades WHERE market_id = ?", (mid,)).fetchone()[0] == pytest.approx(pot)
    assert [r[0] for r in db.execute("SELECT via FROM trades WHERE via IS NOT NULL")] == ["loan", "loan"]
    # the market feed names it
    html = appmod.app.test_client().get(f"/market/{mid}").get_data(as_text=True)
    assert "took over" in html and "handed over" in html


def test_take_over_survives_a_cancelled_market():
    db = _fresh_db()
    creator = _user(db, "maker")
    closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    mid = appmod.create_market(db, "Fee?", "", "Other", closes, ["a", "b"], 300, created_by=creator)
    yes = appmod.get_outcomes(db, mid)[0]["id"]
    ann, bob, lid, mid, yes = borrower_with_bet(db, 300, market=(mid, [yes, 0]))
    cash, maker = bal(db, ann), bal(db, creator)
    appmod.seize_position(db, lid, ann, yes, "take")
    assert bal(db, ann) == pytest.approx(cash) and bal(db, creator) == pytest.approx(maker)
    appmod.cancel_market(db, mid)
    # Radial LMSR unwinding prices all shares of Yes at the same average ray value,
    # conserving the 300-coin pool across ann's seized shares and bob's remainder.
    assert bal(db, ann) == pytest.approx(cash + 85.93, abs=0.05)
    assert bal(db, bob) == pytest.approx(214.07, abs=0.05)
    assert (bal(db, ann) - cash) + bal(db, bob) == pytest.approx(300.0, abs=0.05)


def test_take_over_is_refused_when_late_blocked_or_unaffordable_but_force_sell_works():
    db = _fresh_db()
    ann, bob, lid, mid, yes = borrower_with_bet(db, 300)
    db.execute("UPDATE markets SET closes_at = ? WHERE id = ?",
               ((datetime.now() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"), mid))
    with pytest.raises(appmod.TradeError, match="about to close"):
        appmod.seize_position(db, lid, ann, yes, "take")             # the late-trading fee would apply
    db.execute("UPDATE markets SET closes_at = ? WHERE id = ?",
               ((datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"), mid))
    db.execute("INSERT INTO market_blocks (market_id, user_id, created_at) VALUES (?, ?, ?)", (mid, ann, appmod.now_str()))
    with pytest.raises(appmod.TradeError, match="blocked"):
        appmod.seize_position(db, lid, ann, yes, "take")
    assert shares(db, ann, yes) == 0 and loan(db, lid)["status"] == "active"
    appmod.seize_position(db, lid, ann, yes, "sell")                 # a blocked lender can still force a sale
    assert loan(db, lid)["status"] == "repaid"


def test_cash_is_collected_before_bets_and_a_cleared_loan_takes_nothing():
    db = _fresh_db()
    ann, bob, lid, mid, yes = borrower_with_bet(db, 300)
    db.execute("UPDATE users SET balance = 500 WHERE id = ?", (bob,))
    held = shares(db, bob, yes)
    msg = appmod.seize_position(db, lid, ann, yes, "sell")
    assert "cash already covered" in msg and shares(db, bob, yes) == held
    assert loan(db, lid)["status"] == "repaid" and bal(db, bob) == 400


def test_loans_already_overdue_when_the_rules_shipped_get_a_fresh_grace_period():
    db = _fresh_db()
    assert appmod.get_setting(db, "loans_v2_at") == "1970-01-01 00:00:00"             # a new database has none
    ann, bob, lid, mid, yes = borrower_with_bet(db, 300)
    db.execute("DELETE FROM site_settings WHERE key = 'loans_v2_at'")
    appmod.mark_loan_rules_start(db)                                                   # an upgrade with a running loan
    start = appmod.get_setting(db, "loans_v2_at")
    assert start > "2000" and start <= appmod.now_str()
    with pytest.raises(appmod.TradeError, match="after"):
        appmod.seize_position(db, lid, ann, yes, "sell")
    assert appmod.loan_grace_end(db, loan(db, lid)) > appmod.now_str()
    assert not appmod.in_default(db, loan(db, lid))


def test_borrower_record_counts_on_time_late_and_default():
    db = _fresh_db()
    ann, bob = _user(db, "ann"), _user(db, "bob")
    a = appmod.offer_loan(db, ann, "", 10, 0, 7)
    appmod.accept_loan(db, a, bob)
    appmod.repay_loan(db, a, bob)
    b = appmod.offer_loan(db, ann, "", 10, 0, 7)
    appmod.accept_loan(db, b, bob)
    db.execute("UPDATE loans SET due_at = ? WHERE id = ?", (past(5), b))
    appmod.repay_loan(db, b, bob)                                                      # repaid after its due date
    c = appmod.offer_loan(db, ann, "", 10, 0, 7)
    appmod.accept_loan(db, c, bob)
    db.execute("UPDATE loans SET due_at = ? WHERE id = ?", (past(72), c))
    assert appmod.borrower_record(db, bob) == {"ontime": 1, "late": 1, "default": 1}


def test_collect_route_and_loans_page():
    db = _fresh_db()
    client = appmod.app.test_client()
    signup(client, "ann")
    ann = db.execute("SELECT id FROM users WHERE username = 'ann'").fetchone()[0]
    bob = _user(db, "bob")
    mid, (yes, no) = _market(db, 2, 300)
    lid = appmod.offer_loan(db, ann, "", 100, 0, 7)
    appmod.accept_loan(db, lid, bob)
    appmod.execute_trade(db, bob, mid, yes, "buy", 300)
    db.execute("UPDATE users SET balance = 0 WHERE id = ?", (bob,))
    db.execute("UPDATE loans SET due_at = ? WHERE id = ?", (past(72), lid))
    html = client.get("/loans").get_data(as_text=True)
    assert "Collect from bob" in html and "Force sell" in html and "Take over" in html and "0 on time" in html
    assert "Pick a bet" in _post(client, f"/loans/{lid}/collect", mode="sell").get_data(as_text=True)
    assert "Choose force sell" in _post(client, f"/loans/{lid}/collect", outcome_id=str(yes), mode="x").get_data(as_text=True)
    assert "Sold" in _post(client, f"/loans/{lid}/collect", outcome_id=str(yes), mode="sell").get_data(as_text=True)
    assert loan(db, lid)["status"] == "repaid"


def test_admin_loan_tools_forgive_extend_cancel():
    db = _fresh_db()
    admin, plain = appmod.app.test_client(), appmod.app.test_client()
    signup(admin, "mod")
    signup(plain, "pat")
    db.execute("UPDATE users SET is_admin = 1 WHERE username = 'mod'")
    ann, bob = _user(db, "ann"), _user(db, "bob")
    lid = in_default_loan(db, ann, bob, amount=100)
    db.execute("UPDATE users SET balance = 0 WHERE id = ?", (bob,))                     # or any request would collect it
    offer = appmod.offer_loan(db, ann, "", 30, 0, 7)
    assert "in default" in admin.get("/admin/loans").get_data(as_text=True)
    assert plain.get("/admin/loans").status_code == 403
    assert _post(plain, f"/admin/loans/{lid}/forgive").status_code == 403

    _post(admin, f"/admin/loans/{lid}/extend", days="400")
    assert loan(db, lid)["due_at"] < appmod.now_str()                                   # out of range: unchanged
    _post(admin, f"/admin/loans/{lid}/extend", days="10")
    assert loan(db, lid)["due_at"] > appmod.now_str() and not appmod.has_overdue_loan(db, bob)

    assert "Only a running loan" in _post(admin, f"/admin/loans/{offer}/forgive").get_data(as_text=True)
    _post(admin, f"/admin/loans/{offer}/cancel")
    assert loan(db, offer)["status"] == "cancelled" and bal(db, ann) == 900
    _post(admin, f"/admin/loans/{lid}/forgive")
    assert loan(db, lid)["status"] == "forgiven"
    assert appmod.net_worths(db)[ann] == pytest.approx(900) and appmod.net_worths(db)[bob] == pytest.approx(0)
    assert "open any more" in _post(admin, f"/admin/loans/{lid}/forgive").get_data(as_text=True)
    actions = [r[0] for r in db.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions == ["loan.extend", "loan.cancel", "loan.forgive"]


def test_borrower_sees_the_overdue_banner_and_the_borrowing_room():
    db = _fresh_db()
    client = appmod.app.test_client()
    signup(client, "bob")
    bob = db.execute("SELECT id FROM users WHERE username = 'bob'").fetchone()[0]
    ann = _user(db, "ann")
    html = client.get("/loans").get_data(as_text=True)
    assert "You can still borrow" in html and "Ⓜ500.00" in html and "Overdue loan." not in html
    lid = in_default_loan(db, ann, bob, amount=100)
    db.execute("UPDATE loans SET owed = 5000 WHERE id = ?", (lid,))                      # keeps the loan open after collection
    html = client.get("/loans").get_data(as_text=True)
    assert "Overdue loan." in html and "in default" in html and "can sell or take over your bets" in html
    quote = appmod.app.test_client()
    with quote.session_transaction() as s:
        s["uid"] = bob
    mid, (yes, no) = _market(db, 2, 300)
    db.execute("UPDATE users SET balance = 50 WHERE id = ?", (bob,))
    url = f"/api/quote?market={mid}&outcome={yes}&action=buy&value=10"
    assert "overdue loan" in quote.get(url).get_json()["error"]                           # the preview agrees with the trade


def test_overdue_proposer_gets_the_opening_bet_refunded_not_an_error():
    db = _fresh_db()
    admin, sam = appmod.app.test_client(), appmod.app.test_client()
    signup(admin, "headadmin")
    signup(sam, "sam")
    db.execute("UPDATE users SET is_admin = 1 WHERE username = 'headadmin'")
    closes = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M")
    _post(sam, "/propose", stake="10", stake_pick="0", question="Will there be a band?",
          description="Resolves YES if a live band plays.", outcomes="Yes\nNo", category="Events", closes_at=closes)
    pid = db.execute("SELECT id FROM proposals").fetchone()[0]
    sam_id = db.execute("SELECT id FROM users WHERE username = 'sam'").fetchone()[0]
    ann = _user(db, "ann")
    lid = in_default_loan(db, ann, sam_id)                                               # sam falls into default meanwhile
    db.execute("UPDATE loans SET owed = 5000 WHERE id = ?", (lid,))
    r = _post(admin, f"/admin/proposals/{pid}", decision="approve", question="Will there be a band?",
              description="Resolves YES if a live band plays.", outcomes="Yes\nNo", category="Events",
              closes_at=closes, liquidity="high", review_note="")
    html = r.get_data(as_text=True)
    assert "Approved!" in html and "couldn&#39;t be placed" in html and "overdue loan" in html
    assert db.execute("SELECT status FROM proposals WHERE id = ?", (pid,)).fetchone()[0] == "approved"
    assert db.execute("SELECT COUNT(*) FROM positions WHERE user_id = ?", (sam_id,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 1


def test_resolving_after_a_take_over_pays_the_same_total_split_by_shares():
    def scenario(take):
        db = _fresh_db()
        ann, bob, lid, mid, yes = borrower_with_bet(db, 300)
        other = _user(db, "other")
        appmod.execute_trade(db, other, mid, yes + 1, "buy", 100)                       # someone on the losing side
        held = shares(db, bob, yes)
        if take:
            appmod.seize_position(db, lid, ann, yes, "take")
        n = shares(db, ann, yes)
        before = (bal(db, ann), bal(db, bob))
        appmod.resolve_market(db, mid, yes)
        paid_ann, paid_bob = bal(db, ann) - before[0], bal(db, bob) - before[1]
        return held, n, paid_ann, paid_bob

    held, _, _, control = scenario(take=False)
    held2, n, paid_ann, paid_bob = scenario(take=True)
    assert held2 == pytest.approx(held) and 0 < n < held
    assert paid_ann == pytest.approx(n, abs=0.02) and paid_bob == pytest.approx(held - n, abs=0.02)                                       # each is paid for their own shares
    assert paid_ann + paid_bob == pytest.approx(control, abs=0.03)                        # the pot is split, not changed
    assert paid_ann / paid_bob == pytest.approx(n / (held - n), rel=0.02)                 # leftover follows the shares


def test_take_over_requires_no_entry_fee():
    db = _fresh_db()
    creator = _user(db, "maker")
    closes = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    mid = appmod.create_market(db, "No fee?", "", "Other", closes, ["a", "b"], 300, created_by=creator)
    yes = appmod.get_outcomes(db, mid)[0]["id"]
    ann, bob, lid, mid, yes = borrower_with_bet(db, 300, market=(mid, [yes, 0]))
    db.execute("UPDATE users SET balance = 5 WHERE id = ?", (ann,))
    appmod.seize_position(db, lid, ann, yes, "take")
    assert shares(db, ann, yes) > 0 and loan(db, lid)["status"] == "repaid" and bal(db, ann) == 5


def test_late_fee_is_added_once_when_a_loan_goes_overdue():
    db = _fresh_db()
    ann, bob = _user(db, "ann"), _user(db, "bob", balance=0)
    lid = appmod.offer_loan(db, ann, "bob", 100, 0, 7)
    appmod.accept_loan(db, lid, bob)
    db.execute("UPDATE users SET balance = 0 WHERE id = ?", (bob,))
    appmod.collect_overdue_loans(db)
    assert loan(db, lid)["late_at"] is None and loan(db, lid)["owed"] == 100     # not due yet: no fee
    db.execute("UPDATE loans SET due_at = ? WHERE id = ?", (past(), lid))
    appmod.collect_overdue_loans(db)
    appmod.collect_overdue_loans(db)                                            # runs every request, charged once
    l = loan(db, lid)
    assert l["late_at"] and l["penalty"] == 10 and l["owed"] == 110


def test_old_min_credit_offers_keep_their_requirement_after_migrating():
    db = _fresh_db()
    ann = _user(db, "ann")
    lid = appmod.offer_loan(db, ann, "", 50, 0, 7)
    db.execute("ALTER TABLE loans ADD COLUMN min_credit INTEGER NOT NULL DEFAULT 0")
    db.execute("UPDATE loans SET min_credit = 700, min_score = NULL WHERE id = ?", (lid,))
    appmod.migrate(db)
    assert loan(db, lid)["min_score"] == 700


def test_deleting_an_account_clears_the_old_credit_events_table():
    db = _fresh_db()
    bob = _user(db, "bob")
    db.execute("CREATE TABLE credit_events (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), "
               "loan_id INTEGER, change INTEGER, score_after INTEGER, reason TEXT, created_at TEXT)")
    db.execute("INSERT INTO credit_events (user_id, change, score_after, reason, created_at) "
               "VALUES (?, -40, 560, 'late', '2026-01-01 00:00:00')", (bob,))
    appmod.delete_account(db, bob)
    assert db.execute("SELECT COUNT(*) FROM credit_events").fetchone()[0] == 0
