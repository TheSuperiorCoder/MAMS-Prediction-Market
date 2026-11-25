"""Credit scores for player loans (300-850).

Pure scoring code: it reads loans, activity and users through the connection it is given and never touches Flask, so
it can be tested and previewed without a request. app.py wraps it (apply_credit, get_credit) and stores the result.

The score is 300 + 550 x the weighted average of six factors, each valued 0..1:

  payment      repayment record, recent and larger loans count more, a Bayesian prior stops one loan maxing it out
  utilisation  how much of their borrowing limit they have used
  history      how long they have been borrowing and have had an account
  experience   on-time repayments and volume, more lenders count extra, repaying alts counts for very little
  stability    cash cushion over debt, balance volatility and how active the account has been recently
  velocity     how many loans were taken recently (like hard inquiries)

then: new borrowers are clamped to a middle band, hard caps apply for default / overdue / held accounts, an alt-risk
penalty is subtracted, and finally an admin's manual adjustment. Every number below can be overridden by the
`credit_cfg` site setting (see merged_config).
"""
import copy
import json
import math
from datetime import datetime

MIN_SCORE, MAX_SCORE = 300, 850
SPAN = MAX_SCORE - MIN_SCORE

DEFAULTS = {
    "weights": {"payment": 0.35, "utilisation": 0.20, "history": 0.10, "experience": 0.10,
                "stability": 0.15, "velocity": 0.10},
    "half_life": 90.0,                  # days after which a loan counts half as much towards the payment record
    "tiers": [["Excellent", 760], ["Good", 680], ["Fair", 600], ["Poor", 500], ["Very poor", 300]],
    "multiplier": [[300, 0.25], [580, 0.75], [680, 1.0], [850, 1.5]],   # score -> borrowing limit multiplier
    "thin_range": [580, 680],           # score band for someone with no loan record yet
    "default_cap": 450, "overdue_cap": 560, "held_cap": 500,
    "risk_floor": 30, "risk_penalty": 0.8,   # alt-risk score at/above the floor costs penalty x risk points
    "grace_days": 2,                    # keep equal to LOAN_GRACE_DAYS
    "limit_floor": 500.0, "limit_ratio": 0.5,
    "max_adjust": 150,
}

# Reasons and caps that come from the alt detector. Players never see these (it would leak the risk score), so
# redact() drops them from anything shown outside the admin pages.
RISK_REASON = "Account activity looks linked to other accounts"
HELD_CAP_PREFIX = "Account held for review"

FACTOR_LABELS = {
    "payment": "Payment history", "utilisation": "Credit use", "history": "Length of history",
    "experience": "Experience", "stability": "Financial stability", "velocity": "Recent borrowing",
}
FACTOR_HINTS = {
    "payment": "Repay on time. Late and defaulted loans hurt most, and recent loans count more than old ones.",
    "utilisation": "Owing a small share of your limit looks safer than being maxed out.",
    "history": "Time helps: the longer you have been borrowing and playing, the better.",
    "experience": "Repaying several loans on time, to different lenders, builds trust.",
    "stability": "Keep a cushion of cash and bets above what you owe, and avoid wild swings in your balance.",
    "velocity": "Taking out many loans in a short time lowers the score.",
}


def merged_config(raw):
    """DEFAULTS with a stored JSON override (a string or dict) laid on top. Bad or partial overrides fall back."""
    cfg = copy.deepcopy(DEFAULTS)
    try:
        over = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    except ValueError:
        over = {}
    if isinstance(over, dict):
        for k, v in over.items():
            if k in cfg:
                if isinstance(cfg[k], dict) and isinstance(v, dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
    return cfg


def validate_config(cfg):
    """Return a list of problems with a config (empty if it is usable)."""
    errors = []
    w = cfg.get("weights", {})
    if set(w) != set(DEFAULTS["weights"]):
        errors.append("Every factor needs a weight.")
    else:
        if any((not isinstance(x, (int, float))) or x < 0 for x in w.values()):
            errors.append("Weights can't be negative.")
        elif abs(sum(w.values()) - 1) > 0.005:
            errors.append(f"Weights must add up to 100% (they add up to {sum(w.values()) * 100:.1f}%).")
    tiers = cfg.get("tiers", [])
    mins = [t[1] for t in tiers]
    if len(tiers) < 2 or mins != sorted(mins, reverse=True) or len(set(mins)) != len(mins):
        errors.append("Tier cutoffs must go from high to low with no repeats.")
    elif mins[-1] > MIN_SCORE or mins[0] > MAX_SCORE:
        errors.append(f"The lowest tier must start at or below {MIN_SCORE}.")
    pts = cfg.get("multiplier", [])
    xs = [p[0] for p in pts]
    if len(pts) < 2 or xs != sorted(xs) or len(set(xs)) != len(xs):
        errors.append("Multiplier points must go from low to high score.")
    elif any(p[1] <= 0 or p[1] > 5 for p in pts):
        errors.append("Multipliers must be between 0 and 5.")
    lo, hi = cfg.get("thin_range", [0, 0])
    if not (MIN_SCORE <= lo <= hi <= MAX_SCORE):
        errors.append("The new-borrower range is out of order.")
    return errors


def interp(x, points):
    """Piecewise-linear value of x over [(x0, y0), (x1, y1), ...] sorted by x, flat outside the ends."""
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def tier_for(score, cfg):
    for name, floor in cfg["tiers"]:
        if score >= floor:
            return name
    return cfg["tiers"][-1][0]


def multiplier_for(score, cfg):
    return round(interp(score, [tuple(p) for p in cfg["multiplier"]]), 3)


def _dt(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _days(a, b):
    return (a - b).total_seconds() / 86400.0


def base_limit(cash, bets, owe, cfg):
    """The borrowing limit before the score multiplier (the rule loans had before credit scores)."""
    return max(cfg["limit_floor"], cfg["limit_ratio"] * max(0.0, cash + bets - owe))


def scaled_limit(cash, bets, owe, mult, cfg):
    """The borrowing limit with a credit multiplier applied. The share-of-net-worth part scales with the multiplier;
    the floor is kept in full for new and average borrowers (multiplier 0.75 or more) and only shrinks for poor
    scores, so a new player can always borrow the floor."""
    floor = cfg["limit_floor"] * min(1.0, mult / 0.75)
    return max(floor, cfg["limit_ratio"] * max(0.0, cash + bets - owe) * mult)


def redact(c):
    """A stored credit dict without anything derived from the alt detector, for pages players can see. `redacted` is
    set when something was dropped, so the page can say that other account checks also apply."""
    c = dict(c)
    reasons = [r for r in c["reasons"] if r[1] != RISK_REASON and not r[1].startswith(HELD_CAP_PREFIX)]
    caps = [t for t in c["caps"] if not t.startswith(HELD_CAP_PREFIX)]
    c["redacted"] = len(reasons) != len(c["reasons"]) or len(caps) != len(c["caps"])
    c["reasons"], c["caps"] = reasons, caps
    return c


def compute_credit(db, uid, cfg, now=None, bets=0.0, risk=None, adjust=0, grace_start=None):
    """Score one player. `bets` is the sell-now value of their open bets; `risk` is their user_risk row as a dict
    ({'score', 'status', 'links'}) or None; `adjust` an admin's manual points; `grace_start` the day the loan rules
    began (a loan already overdue then gets its grace counted from it, like loan_grace_end in app.py). Returns a dict with score, tier,
    multiplier, factors, reasons, caps, thin, owe and base (the limit before the multiplier)."""
    now = now or datetime.now()
    w = cfg["weights"]
    user = db.execute("SELECT balance, created_at FROM users WHERE id = ?", (uid,)).fetchone()
    cash = user["balance"] if user else 0.0
    acct_age = _days(now, _dt(user["created_at"]) or now) if user else 0.0
    links = set()
    if risk and risk.get("links"):
        links = {str(k) for k in risk["links"]}

    loans = db.execute("SELECT * FROM loans WHERE borrower_id = ? AND accepted_at IS NOT NULL "
                       "AND status IN ('active', 'repaid', 'forgiven')", (uid,)).fetchall()
    grace = cfg["grace_days"]
    start_dt = _dt(grace_start) if grace_start else None
    if start_dt is None or start_dt.year <= 1970:
        start_ts = 0.0
    else:
        try:
            start_ts = start_dt.timestamp()
        except (OSError, OverflowError, ValueError):
            start_ts = 0.0

    def grace_end(loan):
        due_ts = (_dt(loan["due_at"]) or now).timestamp()
        return max(due_ts, start_ts) + grace * 86400

    # ---- payment history
    prior, prior_weight = 0.6, 2.0
    num = den = 0.0
    obs = ontime = 0
    late_n = default_n = 0
    repaid_vol = ontime_eff = 0.0
    lenders = set()
    owe = 0.0
    recent = running = 0
    for l in loans:
        accepted = _dt(l["accepted_at"])
        due = _dt(l["due_at"]) or now
        if l["status"] == "active":
            owe += l["owed"]
            running += 1
        if accepted and _days(now, accepted) <= 14:
            recent += 1
        size = min(1.5, math.log1p(l["principal"]) / math.log1p(100))
        if l["status"] == "repaid":
            closed = _dt(l["closed_at"]) or due
            late = max(0.0, _days(closed, due))
            credit = 1.0 if late <= 0 else max(0.2, 1 - 0.1 * late)
            when = closed
            if late <= 0:
                ontime += 1
            else:
                late_n += 1
            linked = str(l["lender_id"]) in links
            repaid_vol += l["repaid"] * (0.2 if linked else 1.0)
            ontime_eff += (0.2 if linked else 1.0) if late <= 0 else 0.0
            if not linked:
                lenders.add(l["lender_id"])
        elif l["status"] == "forgiven":
            credit, when = 0.0, _dt(l["closed_at"]) or due
            default_n += 1
        else:  # running
            if now.timestamp() >= grace_end(l):
                credit, when = 0.0, due
                default_n += 1
            elif due <= now:
                credit, when = 0.15, due
                late_n += 1
            else:
                continue  # not due yet: nothing to judge
        weight = (0.5 ** (max(0.0, _days(now, when)) / cfg["half_life"])) * size
        num += weight * credit
        den += weight
        obs += 1
    payment = (prior * prior_weight + num) / (prior_weight + den)
    payment_detail = (f"{ontime} on time, {late_n} late, {default_n} in default" if obs else "No loans judged yet")

    # ---- utilisation
    base = base_limit(cash, bets, owe, cfg)
    use = owe / base if base else 0.0
    utilisation = interp(use, [(0.1, 1.0), (0.5, 0.6), (0.9, 0.2), (1.0, 0.0)])
    util_detail = f"Using {use * 100:.0f}% of your base limit"

    # ---- history length
    first = min((_dt(l["accepted_at"]) for l in loans if _dt(l["accepted_at"])), default=None)
    loan_age = _days(now, first) if first else 0.0
    history = 0.6 * min(1.0, loan_age / 60) + 0.4 * min(1.0, acct_age / 90)
    hist_detail = (f"First loan {loan_age:.0f} days ago, " if first else "No loans yet, ") + f"account {acct_age:.0f} days old"

    # ---- experience & diversity
    experience = (0.4 * min(1.0, math.log1p(ontime_eff) / math.log1p(8))
                  + 0.3 * min(1.0, math.log1p(repaid_vol) / math.log1p(1000))
                  + 0.3 * min(1.0, len(lenders) / 4))
    exp_detail = f"{ontime} on-time repayments to {len(lenders)} lender(s)"
    if links and any(str(l["lender_id"]) in links for l in loans if l["status"] == "repaid"):
        exp_detail += " (repaying linked accounts counts for little)"

    # ---- stability
    since = datetime.fromtimestamp(now.timestamp() - 30 * 86400).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.execute("SELECT balance_after, created_at FROM activity WHERE user_id = ? AND created_at >= ? "
                      "AND balance_after IS NOT NULL ORDER BY id", (uid, since)).fetchall()
    cushion = 1.0 if owe <= 0 else interp((cash + bets) / owe, [(1.0, 0.0), (1.5, 0.5), (3.0, 1.0)])
    series = [r["balance_after"] for r in rows]
    if len(series) >= 3:
        mean = sum(series) / len(series)
        sd = math.sqrt(sum((x - mean) ** 2 for x in series) / len(series))
        cv = sd / mean if mean > 1 else 2.0
        volatility = interp(cv, [(0.0, 1.0), (0.5, 0.5), (1.5, 0.0)])
    else:
        cv, volatility = 0.0, 0.5
    active_days = len({r["created_at"][:10] for r in rows})
    engagement = min(1.0, active_days / 10)
    stability = 0.5 * cushion + 0.25 * volatility + 0.25 * engagement
    stab_detail = (f"Cash and bets cover {((cash + bets) / owe):.1f}x your debt, " if owe > 0 else "No debt, ") \
        + f"active on {active_days} of the last 30 days"

    # ---- recent borrowing
    x = recent + 0.5 * max(0, running - recent)
    velocity = interp(x, [(0, 1.0), (1, 0.9), (2, 0.65), (3, 0.4), (5, 0.0)])
    vel_detail = f"{recent} loan(s) in the last 14 days, {running} running"

    values = {"payment": payment, "utilisation": utilisation, "history": history, "experience": experience,
              "stability": stability, "velocity": velocity}
    details = {"payment": payment_detail, "utilisation": util_detail, "history": hist_detail,
               "experience": exp_detail, "stability": stab_detail, "velocity": vel_detail}
    factors = []
    reasons = []
    total = 0.0
    for key in ("payment", "utilisation", "history", "experience", "stability", "velocity"):
        pts = w[key] * values[key] * SPAN
        total += pts
        factors.append({"key": key, "label": FACTOR_LABELS[key], "weight": w[key], "value": round(values[key], 3),
                        "points": round(pts, 1), "detail": details[key], "hint": FACTOR_HINTS[key]})
        swing = pts - w[key] * 0.5 * SPAN   # against a middling value
        if abs(swing) >= 4:
            reasons.append((round(swing), f"{FACTOR_LABELS[key]}: {details[key]}"))
    score = MIN_SCORE + total

    # ---- post-adjustments
    caps = []
    thin = obs == 0
    if thin:
        lo, hi = cfg["thin_range"]
        score = min(hi, max(lo, score))
        caps.append("New borrower: no loan record yet, so the score stays in a middle band")
    def cap(limit, text):
        nonlocal score
        if score > limit:
            reasons.append((round(limit - score), text))
            score = limit
        caps.append(text)
    if any(l["status"] == "active" and now.timestamp() >= grace_end(l) for l in loans):
        cap(cfg["default_cap"], f"A loan is in default: score capped at {cfg['default_cap']}")
    elif any(l["status"] == "active" and (_dt(l["due_at"]) or now) <= now for l in loans):
        cap(cfg["overdue_cap"], f"A loan is overdue: score capped at {cfg['overdue_cap']}")
    if risk and risk.get("status") == "held":
        cap(cfg["held_cap"], f"{HELD_CAP_PREFIX}: score capped at {cfg['held_cap']}")
    if risk and risk.get("status") != "approved" and risk.get("score", 0) >= cfg["risk_floor"]:
        # an admin who approved the account has already looked at these signals, so they don't count against it
        pen = cfg["risk_penalty"] * risk["score"]
        score -= pen
        reasons.append((-round(pen), RISK_REASON))
    if adjust:
        score += adjust
        reasons.append((int(adjust), "Adjusted by an admin"))
    score = int(round(max(MIN_SCORE, min(MAX_SCORE, score))))
    reasons.sort(key=lambda r: -abs(r[0]))
    return {"score": score, "tier": tier_for(score, cfg), "multiplier": multiplier_for(score, cfg),
            "factors": factors, "reasons": reasons[:8], "caps": caps, "thin": thin, "owe": round(owe, 2),
            "base": round(base, 2), "record": {"ontime": ontime, "late": late_n, "default": default_n}}
