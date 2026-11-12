"""Funding evidence: what kind of proof a funding number rests on, how much history
exists behind it, and the one sign convention every report uses.

Evidence statuses (strongest last; a weaker status never counts as a stronger one):
  MISSING                           no rate and no history from the venue
  RATE_PUBLISHED                    a current/predicted rate the venue publishes; not
                                    a payment (TxFlow, Variational)
  RATE_SAMPLED_AT_CUTOFF            our own sample of the published rate taken at the
                                    venue's funding time (tools/funding_observer.py);
                                    still not a payment
  SETTLEMENT_INFERRED_NOT_CONFIRMED a payment inferred from rate x interval; the venue
                                    did not publish it as applied
  MARKET_SETTLEMENT_CONFIRMED       the venue publishes the per-market rate it applied
                                    at each funding time (history endpoint)
  ACCOUNT_PAYMENT_CONFIRMED         an account's own payment record (read-only; needs an
                                    authorized test address; none is configured)
  REVISED                           the same funding time observed with two different
                                    rates (the venue changed history): needs review

Coverage windows are coverage-aware: a "90d" window over an asset listed 55 days ago is
reported as 55/90 days (PARTIAL), and its averages are over observed points only; it is
never stretched or zero-filled to 90 days.

Sign convention (one place): a published rate is a fraction per hour, positive = longs
pay shorts. The cash flow per $1 of notional for a leg is -rate for the long and +rate
for the short.
"""
import statistics

MISSING = "MISSING"
RATE_PUBLISHED = "RATE_PUBLISHED"
RATE_SAMPLED_AT_CUTOFF = "RATE_SAMPLED_AT_CUTOFF"
SETTLEMENT_INFERRED = "SETTLEMENT_INFERRED_NOT_CONFIRMED"
MARKET_SETTLEMENT = "MARKET_SETTLEMENT_CONFIRMED"
ACCOUNT_PAYMENT = "ACCOUNT_PAYMENT_CONFIRMED"
REVISED = "REVISED"
STATUSES = (MISSING, RATE_PUBLISHED, RATE_SAMPLED_AT_CUTOFF, SETTLEMENT_INFERRED,
            MARKET_SETTLEMENT, ACCOUNT_PAYMENT, REVISED)
RANK = {MISSING: 0, RATE_PUBLISHED: 1, RATE_SAMPLED_AT_CUTOFF: 2, SETTLEMENT_INFERRED: 3,
        MARKET_SETTLEMENT: 4, ACCOUNT_PAYMENT: 5, REVISED: -1}
# Statuses that count as settled evidence for a PAPER funding/hybrid entry.
SETTLED = (MARKET_SETTLEMENT, ACCOUNT_PAYMENT)

# What each venue's public API can give today (docs/v2/25_ELEVEN_VENUE_DATA_MATRIX.md).
VENUE_BEST = {
    "lighter_rh": MARKET_SETTLEMENT,        # /api/v1/fundings
    "risex": MARKET_SETTLEMENT,             # funding-rate-history (index price verified)
    "arcus": MARKET_SETTLEMENT,             # /v1/fundingRates
    "tradexyz": MARKET_SETTLEMENT,          # info fundingHistory (xyz:)
    "hyperliquid_native": MARKET_SETTLEMENT,  # info fundingHistory (native coin)
    "entropy_io": MARKET_SETTLEMENT,        # info fundingHistory (io:)
    "txflow": RATE_PUBLISHED,               # userFundings needs an authorized address
    "variational": RATE_PUBLISHED,          # stats only; observer samples at cutoff
    "popdex_perps": MISSING,                # no verified public API
    "veranta": MISSING,                     # no verified public API
    "phoenix_perps": MISSING,               # history route exists in the SDK, path UNKNOWN
    "qfex": MISSING,                        # REST paths UNKNOWN; WS funding is a rate only
}

DAY = 86400.0
HOUR = 3600.0
WINDOWS = (("24h", 1), ("3d", 3), ("7d", 7), ("30d", 30), ("90d", 90))
FULL = 0.95


def venue_status(exchange_id):
    return VENUE_BEST.get(exchange_id, MISSING)


def is_settled(status):
    return status in SETTLED


def strongest(statuses):
    xs = [s for s in statuses if s in RANK]
    if REVISED in xs:
        return REVISED
    return max(xs, key=RANK.get) if xs else MISSING


def per_hour(rate, interval_s):
    """A rate per funding interval as a fraction per hour."""
    if rate is None or not interval_s:
        return None
    return rate / (interval_s / HOUR)


def per_8h(hourly):
    return None if hourly is None else hourly * 8


def leg_cashflow(side, hourly_rate):
    """Cash flow per $1 notional per hour for one leg (positive = received)."""
    if hourly_rate is None:
        return None
    if side not in ("long", "short"):
        raise ValueError("side must be long or short")
    return -hourly_rate if side == "long" else hourly_rate


def pair_carry(long_hourly, short_hourly):
    """Hourly carry per $1 of a long/short pair: what the short receives minus what the
    long pays. None when either rate is missing (never defaulted)."""
    a, b = leg_cashflow("long", long_hourly), leg_cashflow("short", short_hourly)
    return None if a is None or b is None else a + b


def revisions(observations):
    """Funding times seen with different rates. `observations` = [dict(ts, rate)] from
    several reads (oldest first). Returns [dict(ts, rates)]."""
    seen = {}
    for x in observations:
        seen.setdefault(x["ts"], [])
        if x["rate"] not in seen[x["ts"]]:
            seen[x["ts"]].append(x["rate"])
    return [dict(ts=t, rates=r) for t, r in sorted(seen.items()) if len(r) > 1]


def coverage(points, at, interval_s=3600, windows=WINDOWS):
    """Coverage-aware windows over a funding history.

    points: [dict(ts seconds, rate per interval)]. For each window: expected and
    observed points, the first observed time, coverage ratio, the days actually
    covered, a status (FULL >= 95 %, PARTIAL, EMPTY) and the mean hourly rate over
    observed points only (None when empty)."""
    out = {}
    pts = sorted(points or [], key=lambda x: x["ts"])
    first = pts[0]["ts"] if pts else None
    for name, days in windows:
        lo = at - days * DAY
        xs = [p for p in pts if lo < p["ts"] <= at]
        expected = int(round(days * DAY / interval_s))
        have = len(xs)
        span = (at - max(lo, xs[0]["ts"])) / DAY if xs else 0.0
        ratio = have / expected if expected else 0.0
        status = "EMPTY" if not have else ("FULL" if ratio >= FULL else "PARTIAL")
        hourly = [per_hour(p["rate"], interval_s) for p in xs]
        out[name] = dict(
            window_days=days, expected=expected, observed=have,
            coverage=round(ratio, 4), covered_days=round(min(span, days), 2),
            first_ts=xs[0]["ts"] if xs else None, status=status,
            label="%s: %d/%d points (%.1f/%d days)" % (name, have, expected,
                                                        min(span, days), days),
            mean_hourly=statistics.mean(hourly) if hourly else None)
    out["listed_since"] = first
    return out


# Anomaly thresholds (research labels only; they never make anything eligible).
EXTREME_HOURLY = 0.001     # 0.1 % per hour (~876 % APR) or more in either direction


def anomalies(current_hourly, history_hourly, cover=None):
    """Labels that ask for review before a funding number is trusted:
      EXTREME_RATE       |current| >= 0.1 %/h
      OUTSIDE_HISTORY    current outside the [min, max] of the history it is compared to
      SIGN_FLIPS         the history changes sign in more than 20 % of hours
      SHORT_HISTORY      the 90d window is PARTIAL or EMPTY (coverage-aware)
      NO_HISTORY         no history at all
      REGIME_SHIFT       the last 24h mean is at least 5x the 90d mean (and >= 0.01 %/h):
                         today's rate is not the asset's normal level"""
    out = []
    if current_hourly is not None and abs(current_hourly) >= EXTREME_HOURLY:
        out.append("EXTREME_RATE")
    hs = [h for h in history_hourly or [] if h is not None]
    if not hs:
        out.append("NO_HISTORY")
    else:
        if current_hourly is not None and not min(hs) <= current_hourly <= max(hs):
            out.append("OUTSIDE_HISTORY")
        flips = sum(1 for a, b in zip(hs, hs[1:]) if (a > 0) != (b > 0))
        if len(hs) > 10 and flips / (len(hs) - 1) > 0.2:
            out.append("SIGN_FLIPS")
    if cover is not None and cover.get("90d", {}).get("status") in ("PARTIAL", "EMPTY"):
        out.append("SHORT_HISTORY")
    if cover is not None:
        d1 = (cover.get("24h") or {}).get("mean_hourly")
        d90 = (cover.get("90d") or {}).get("mean_hourly")
        if d1 is not None and d90 is not None and abs(d1) >= 1e-4                 and abs(d1) >= 5 * abs(d90):
            out.append("REGIME_SHIFT")
    return out
