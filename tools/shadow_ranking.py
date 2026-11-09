"""Shadow economic ranking for delta-neutral pairs (ANALYSIS ONLY).

Not wired into the engine: it never changes a PAPER decision, gate or stream. It ranks a
pair (long venue L, short venue S, one asset) by what it can be expected to keep after
costs, using only information that was KNOWABLE at the decision time t, and then reads
what happened after t (walk-forward). Inputs are plain series:

  funding: [(settled_at, rate_per_hour)]  rate > 0 means the long pays (engine
           convention); a row is the payment settled at `settled_at`.
  closes:  [(candle_start, price)]         hourly trade candles keyed by their START.
  book:    dict(bids=[(p, q)], asks=[(p, q)])  read once; used only as a COST assumption.

Three times are kept apart (owner rule 2026-11-10):
  * a funding row is SETTLED at `settled_at`, PUBLISHED by the venue some time later and
    OBSERVED by us after that. It is usable at t only if settled_at + funding_lag <= t,
    where funding_lag is the measured publication + observation delay (never 0 by default).
  * a candle keyed by its start closes one hour later; its close is usable at t only if
    candle_start + 3600 + close_lag <= t. (If a venue keys candles by their end, this is
    one hour too conservative, never optimistic.)
  * hourly trade closes are NOT executable bid/ask prices: they are unsynchronised last
    trades. `price=False` (funding-only) credits no price convergence and reports the gap
    move only as risk.

`realized` reads only data after t and is never used by `score`.
"""
import math
import statistics

HORIZONS = (24, 48, 72, 168)
HOUR = 3600
# Conservative defaults until a venue's delay is measured: one full hour.
FUNDING_LAG = 3600
CLOSE_LAG = 60

KIND_FUNDING = "funding_contribution_minus_assumed_costs"
KIND_TOTAL = "estimate_with_price_from_trade_closes_NOT_EXECUTABLE"


def known(series, lag):
    """Re-key rows by the time they became usable (event time + lag)."""
    return [(ts + lag, v) for ts, v in series]


def window_mean(series, t, hours, min_coverage=0.9):
    """Mean of the values keyed in (t - hours, t] (keys = usable-at times); None when
    coverage is too thin."""
    rows = [v for k, v in series if t - hours * HOUR < k <= t]
    if len(rows) < min_coverage * hours:
        return None
    return sum(rows) / len(rows)


def last_value(series, t):
    return next((v for k, v in reversed(series) if k <= t), None)


def carry_series(funding_long, funding_short):
    """Hourly carry of long L / short S keyed by settlement time: the short receives its
    rate, the long pays its own."""
    long_ = dict(funding_long)
    return sorted((ts, r - long_[ts]) for ts, r in funding_short if ts in long_)


def gap_series(closes_long, closes_short):
    """log(P_short / P_long) for candles (keyed by start) that both venues closed."""
    long_ = dict(closes_long)
    return sorted((ts, math.log(p / long_[ts])) for ts, p in closes_short
                  if ts in long_ and p > 0 and long_[ts] > 0)


def round_trip_drag(book, notional):
    """Per-$ cost of crossing the book on entry AND exit at `notional` (buy at asks,
    sell at bids, VWAP); None when the book is too thin (no partial fills). A cost
    ASSUMPTION from a displayed book, not a guaranteed fill."""
    bids, asks = sorted(book["bids"], reverse=True), sorted(book["asks"])
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    qty = notional / mid

    def vwap(levels):
        left, cost = qty, 0.0
        for p, q in levels:
            take = min(left, q)
            cost += take * p
            left -= take
            if left <= 1e-12:
                return cost / qty
        return None

    buy, sell = vwap(asks), vwap(bids)
    if buy is None or sell is None:
        return None
    return (buy - sell) / mid


def normals(gaps_known, hours=168):
    """{k: median of the gaps usable in (k - hours, k)} for each usable-at key k."""
    out, vals = {}, list(gaps_known)
    for i, (k, _) in enumerate(vals):
        rows = [v for s, v in vals[max(0, i - hours - 1):i] if k - hours * HOUR < s < k]
        if len(rows) >= hours * 0.5:
            out[k] = statistics.median(rows)
    return out


def reversion(gaps_known, t, hours, normal_map, lookback_days=14):
    """Mean reversion of the gap over `hours`, fitted only on pairs (s, s + hours) that
    were both usable by t: slope of (g_s - g_{s+h}) on (g_s - normal_s), clipped to
    [0, 1]; plus AR(1) half-life (hours). None when too little history."""
    g = dict(gaps_known)
    pts = []
    lo = t - lookback_days * 86400
    for s, gs in gaps_known:
        if s < lo or s + hours * HOUR > t or (s + hours * HOUR) not in g:
            continue
        normal = normal_map.get(s)
        if normal is None:
            continue
        pts.append((gs - normal, gs - g[s + hours * HOUR]))
    if len(pts) < 48:
        return None
    mx = sum(x for x, _ in pts) / len(pts)
    my = sum(y for _, y in pts) / len(pts)
    vx = sum((x - mx) ** 2 for x, _ in pts)
    slope = 0.0 if vx == 0 else sum((x - mx) * (y - my) for x, y in pts) / vx
    seq = [v for s, v in gaps_known if lo <= s <= t]
    phi = None
    if len(seq) > 48:
        a, b = seq[:-1], seq[1:]
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        va = sum((x - ma) ** 2 for x in a)
        phi = None if va == 0 else sum((x - ma) * (y - mb) for x, y in zip(a, b)) / va
    half_life = (math.log(0.5) / math.log(phi)) if phi and 0 < phi < 1 else None
    return dict(theta=min(1.0, max(0.0, slope)), half_life_h=half_life, points=len(pts))


def score(carry, gaps, t, costs, horizons=HORIZONS, min_net=0.0005, normal_map=None,
          price=True, funding_lag=FUNDING_LAG, close_lag=CLOSE_LAG, _known=None):
    """Expected per-$ value for each horizon at decision time t, from usable data only.

    carry: oriented carry keyed by settlement time; gaps: gap series keyed by candle
    start; costs: dict(fees=round-trip fees per $, drag=round-trip book cost per $ or
    None, optional reserve=fixed risk reserve per $; default 7-day sd of the gap, as the
    engine). price=False credits no price convergence (funding-only)."""
    ck, gk = _known or (known(carry, funding_lag), known(gaps, HOUR + close_lag))
    c1, c3, c7 = (window_mean(ck, t, h) for h in (24, 72, 168))
    out = dict(t=t, carry_1d=c1, carry_3d=c3, carry_7d=c7, horizons={},
               kind=KIND_TOTAL if price else KIND_FUNDING,
               funding_lag=funding_lag, close_lag=close_lag)
    if None in (c1, c3, c7) or costs.get("drag") is None:
        out["status"] = "UNKNOWN"
        return out
    # Funding is credited only when the last 1, 3 and 7 days all point the same way,
    # and then at the weakest of the three (no projection of today's level).
    agree = c1 > 0 and c3 > 0 and c7 > 0
    carry_h = min(c1, c3, c7) if agree else 0.0
    week = [v for k, v in ck if t - 168 * HOUR < k <= t]
    out["flip_share_7d"] = sum(v < 0 for v in week) / len(week)
    gap_week = [v for k, v in gk if t - 168 * HOUR < k <= t]
    sd = statistics.pstdev(gap_week) if gap_week else 0.0
    normal = statistics.median(gap_week) if len(gap_week) >= 84 else None
    now_gap = last_value(gk, t)
    out.update(gap=now_gap, normal=normal, gap_sd_7d=sd,
               deviation=None if normal is None or now_gap is None else now_gap - normal)
    reserve = costs.get("reserve", sd)
    if price and normal_map is None:
        normal_map = normals(gk)
    best = None
    for h in horizons:
        rev = (reversion(gk, t, h, normal_map)
               if price and out["deviation"] is not None else None)
        # Expected gap change: the fitted share of today's deviation from normal. Above
        # normal it is a gain (the short leg is the expensive one); below, a cost.
        gain = rev["theta"] * out["deviation"] if rev is not None else 0.0
        value = carry_h * h + gain - costs["fees"] - costs["drag"] - reserve
        out["horizons"][h] = dict(funding=carry_h * h, price=gain, reserve=reserve,
                                  value=value, reversion=rev)
        if best is None or value > out["horizons"][best]["value"]:
            best = h
    out["best_h"] = best
    out["best_value"] = out["horizons"][best]["value"]
    out["status"] = "PASS" if out["best_value"] >= min_net else "FAIL"
    return out


def realized(carry, gaps, t, h, costs, price=True, close_lag=CLOSE_LAG):
    """What happened from t to t + h (per $). Funding: settlements in (t, t + h]. Gap
    move: last close usable at t to last close usable at t + h (trade closes, NOT
    executable prices). `value` = funding + (gap move if price) - fees - drag, labelled
    by `kind`. None when the outcome window is not complete (never extrapolated)."""
    pays = [v for ts, v in carry if t < ts <= t + h * HOUR]
    if len(pays) < 0.9 * h:
        return None
    gk = known(gaps, HOUR + close_lag)
    g0, g1 = last_value(gk, t), last_value(gk, t + h * HOUR)
    if g0 is None or g1 is None or costs.get("drag") is None:
        return None
    funding = sum(pays)
    move = g0 - g1
    return dict(funding=funding, price_move=move, kind=KIND_TOTAL if price else KIND_FUNDING,
                value=funding + (move if price else 0.0) - costs["fees"] - costs["drag"])


def screen_proxy(carry, gaps, t, fees, horizon=48, funding_lag=FUNDING_LAG, close_lag=CLOSE_LAG):
    """The engine-style screen (full price gap counted as profit + latest carry over the
    horizon - fees), with usable data only. For comparison only."""
    last_c = last_value(known(carry, funding_lag), t)
    g0 = last_value(known(gaps, HOUR + close_lag), t)
    if last_c is None or g0 is None:
        return None
    return g0 + last_c * horizon - fees


def walk_forward(carry, gaps, costs, start, end, step_h=6, horizons=HORIZONS, min_net=0.0005,
                 price=True, funding_lag=FUNDING_LAG, close_lag=CLOSE_LAG):
    """Decision times start, start+step, ... while the shortest outcome window fits;
    a horizon whose outcome window is not complete is left None."""
    rows = []
    ck, gk = known(carry, funding_lag), known(gaps, HOUR + close_lag)
    normal_map = normals(gk) if price else {}
    t = start
    while t + min(horizons) * HOUR <= end:
        s = score(carry, gaps, t, costs, horizons, min_net, normal_map, price,
                  funding_lag, close_lag, _known=(ck, gk))
        r = {h: realized(carry, gaps, t, h, costs, price, close_lag) for h in horizons}
        rows.append(dict(t=t, score=s, realized=r,
                         proxy=screen_proxy(carry, gaps, t, costs["fees"], 48,
                                            funding_lag, close_lag)))
        t += step_h * HOUR
    return rows


def independent(selections):
    """Overlapping selections are one position, not several wins: per asset (the engine
    holds one position per asset) keep a selection only after the previous kept one's
    holding window has ended. `selections`: dicts with asset, t, h; sorted by t."""
    end, kept = {}, []
    for x in sorted(selections, key=lambda x: x["t"]):
        if x["t"] >= end.get(x["asset"], -math.inf):
            kept.append(x)
            end[x["asset"]] = x["t"] + x["h"] * HOUR
    return kept
