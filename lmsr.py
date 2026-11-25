"""Logarithmic Market Scoring Rule (LMSR) automated market maker.

The market maker always quotes a price for every outcome, so traders never
need a counterparty. Prices across all outcomes of a market always sum to 1,
and each price can be read as the crowd's probability for that outcome.

    cost(q)  = b * ln( sum_i exp(q_i / b) )
    price_i  = exp(q_i / b) / sum_j exp(q_j / b)

q_i is the number of shares of outcome i sold so far and b is the
"liquidity" parameter: larger b means prices move less per trade.
A winning share pays out 1 coin when the market resolves.

Fixed-point helpers operate in integer cents (1 coin = 100 cents) and integer
centi-shares (1 share = 100 centi-shares) with asymmetric rounding (floor bought
shares, ceil buy cost, floor sell proceeds) so round-trip trades never mint coins.
"""

import math

SHARES_SCALE = 100
CENTS_PER_COIN = 100


def to_cents(x):
    """Convert a coin amount to integer cents."""
    return int(round(float(x) * CENTS_PER_COIN))


def from_cents(c):
    """Convert integer cents to a 2-decimal coin float."""
    return round(int(c) / CENTS_PER_COIN, 2)


def to_centi_shares(x):
    """Convert a share quantity to integer centi-shares (100 = 1.00 share)."""
    return int(round(float(x) * SHARES_SCALE))


def from_centi_shares(sc):
    """Convert integer centi-shares to a 2-decimal share float."""
    return round(int(sc) / SHARES_SCALE, 2)


def _logsumexp(xs):
    m = max(xs)
    return m + math.log(sum(math.exp(x - m) for x in xs))


def cost(q, b):
    return b * _logsumexp([x / b for x in q])


def prices(q, b):
    z = [x / b for x in q]
    m = max(z)
    e = [math.exp(v - m) for v in z]
    total = sum(e)
    return [v / total for v in e]


def shares_for_amount(q, b, i, amount):
    """How many shares of outcome i can be bought for exactly `amount` coins."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    # Solving cost_for_shares(s) = amount gives exp(s/b) = 1 + (exp(amount/b) - 1) / p_i. Evaluated in log
    # space so it stays accurate for tiny amounts and for outcomes priced near 0.
    x = amount / b
    neg_log_p = (cost(q, b) - q[i]) / b  # = -ln(p_i)
    a = neg_log_p + x + math.log(-math.expm1(-x))
    return b * (max(a, 0.0) + math.log1p(math.exp(-abs(a))))  # b * ln(1 + e^a)


def cost_for_shares(q, b, i, shares):
    """Coins needed to buy `shares` of outcome i."""
    q2 = list(q)
    q2[i] += shares
    return cost(q2, b) - cost(q, b)


def sell_value(q, b, i, shares):
    """Coins received for selling `shares` of outcome i back to the market maker."""
    q2 = list(q)
    q2[i] -= shares
    return cost(q, b) - cost(q2, b)


def liquidation_value(q, b, holdings):
    """Coins received for selling a whole set of holdings (one share count per outcome) at once.

    This is not the sum of per-outcome sell values: holding s of every outcome is worth exactly s."""
    return cost(q, b) - cost([x - h for x, h in zip(q, holdings)], b)


def quote_buy_cents(q_centi, b, i, spend_cents):
    """Fixed-point anti-arbitrage buy quote: returns (shares_centi, cost_cents).

    Floors bought shares to integer centi-shares and ceils exact LMSR cost to
    whole cents so the market maker never undercharges and round-trips cannot mint coins.
    """
    if not isinstance(spend_cents, int) or spend_cents <= 0:
        raise ValueError("spend_cents must be a positive integer")
    q = [x / SHARES_SCALE for x in q_centi]
    delta_q = shares_for_amount(q, b, i, spend_cents / CENTS_PER_COIN)
    shares_centi = math.floor(delta_q * SHARES_SCALE)
    if shares_centi <= 0:
        raise ValueError("Spend amount is too small to purchase shares")

    cost_cents = math.ceil(cost_for_shares(q, b, i, shares_centi / SHARES_SCALE) * CENTS_PER_COIN)
    while cost_cents > spend_cents and shares_centi > 0:
        shares_centi -= 1
        cost_cents = (
            math.ceil(cost_for_shares(q, b, i, shares_centi / SHARES_SCALE) * CENTS_PER_COIN)
            if shares_centi > 0
            else 0
        )

    if shares_centi <= 0 or cost_cents <= 0:
        raise ValueError("Spend amount is too small to purchase shares")
    return shares_centi, cost_cents


def quote_sell_cents(q_centi, b, i, sell_shares_centi):
    """Fixed-point anti-arbitrage sell quote: returns proceeds_cents (floored)."""
    if not isinstance(sell_shares_centi, int) or sell_shares_centi <= 0:
        raise ValueError("sell_shares_centi must be a positive integer")
    q = [x / SHARES_SCALE for x in q_centi]
    proceeds = sell_value(q, b, i, sell_shares_centi / SHARES_SCALE)
    return max(0, math.floor(proceeds * CENTS_PER_COIN))


def max_buy_shares_to_price_centi(q_centi, b, i, target_price):
    """Maximum integer centi-shares of outcome i buyable without pushing price_i above target_price."""
    if not (0.0 < target_price < 1.0):
        raise ValueError("target_price must be strictly between 0 and 1")
    q = [x / SHARES_SCALE for x in q_centi]
    p = prices(q, b)[i]
    if p >= target_price:
        return 0
    # In LMSR, p'/(1-p') = exp(dq/b) * p/(1-p), so dq = b * ln(p'(1-p) / (p(1-p'))).
    dq = b * (math.log(target_price / (1.0 - target_price)) - math.log(p / (1.0 - p)))
    sc = max(0, math.floor(dq * SHARES_SCALE))
    while sc > 0:
        q2 = list(q)
        q2[i] += sc / SHARES_SCALE
        if prices(q2, b)[i] <= target_price + 1e-12:
            break
        sc -= 1
    return sc


def max_sell_shares_to_price_centi(q_centi, b, i, target_price):
    """Maximum integer centi-shares of outcome i sellable without pushing price_i below target_price."""
    if not (0.0 < target_price < 1.0):
        raise ValueError("target_price must be strictly between 0 and 1")
    q = [x / SHARES_SCALE for x in q_centi]
    p = prices(q, b)[i]
    if p <= target_price:
        return 0
    dq = b * (math.log(p / (1.0 - p)) - math.log(target_price / (1.0 - target_price)))
    sc = min(int(q_centi[i]), max(0, math.floor(dq * SHARES_SCALE)))
    while sc > 0:
        q2 = list(q)
        q2[i] -= sc / SHARES_SCALE
        if prices(q2, b)[i] >= target_price - 1e-12:
            break
        sc -= 1
    return sc


