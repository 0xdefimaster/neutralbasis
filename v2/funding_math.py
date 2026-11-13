"""Funding economics on real payment times (Phase 13.5). PAPER research.

Intervals are NOT reconciled by a plain multiplier. A leg pays or receives only at its
own payment times, and only if the position is OPEN at that instant
(open_at < T < close_at; a close exactly at T is not counted: conservative).

Sign (every V2 venue, positive rate = longs pay): long cash = -rate x notional,
short cash = +rate x notional, per payment, with the rate per PAYMENT PERIOD.

When a venue does not publish its next payment time the phase is UNKNOWN: the number
of payments in a hold can be floor(hold / interval) or one more, so the result is a
BAND (min, max), never a single invented number.

The common-horizon view (`per_horizon`) converts a per-period rate to 1 h / 4 h / 8 h
for COMPARISON only; money is always counted on the discrete schedule.

PnL classes (the names every report uses; none is a verified result):
  EXPECTED_AT_ENTRY           the entry-time expectation (published rates)
  ENTRY_STRESS_ESTIMATE       the worst modelled scenario at entry (a model output)
  SIMULATED_REALIZED_CLOSED   simulated fills of a closed position
  OPEN_MARK_TO_MARKET         open quantity at a fresh observed mid
  SETTLED_FUNDING_EVIDENCE    payments priced from a venue's public settled rates
  ACCOUNT_PAYMENT_CONFIRMED   an account's real payment (none in PAPER)
  UNVERIFIED_COMPONENTS       anything resting on a published / carried / assumed input
"""
import math

PNL_CLASSES = ("EXPECTED_AT_ENTRY", "ENTRY_STRESS_ESTIMATE", "SIMULATED_REALIZED_CLOSED",
               "OPEN_MARK_TO_MARKET", "SETTLED_FUNDING_EVIDENCE", "ACCOUNT_PAYMENT_CONFIRMED",
               "UNVERIFIED_COMPONENTS")
HOUR = 3600.0


def per_horizon(rate_per_period, interval_s, hours):
    """Comparison only: the per-period rate scaled to `hours` (not a payment)."""
    if rate_per_period is None or not interval_s:
        return None
    return rate_per_period * hours * HOUR / interval_s


def payment_times(next_ts, interval_s, open_at, close_at):
    """Payment instants T with open_at < T < close_at, or None if the phase is unknown."""
    if not next_ts or not interval_s:
        return None
    t = next_ts
    while t - interval_s > open_at:          # walk back to the first payment after open
        t -= interval_s
    while t <= open_at:
        t += interval_s
    out = []
    while t < close_at:
        out.append(t)
        t += interval_s
    return out


def possible_counts(hold_s, interval_s):
    """(min, max) payment instants of period `interval_s` strictly inside an open
    interval of length `hold_s` when the phase is unknown. With q = hold / interval:
    q not an integer -> floor(q) or floor(q)+1; q an integer k -> k-1 (phase aligned
    with both ends, which are excluded) or k."""
    if hold_s <= 0:
        return 0, 0
    q = hold_s / interval_s
    k = round(q)
    if abs(q - k) < 1e-9:
        return max(k - 1, 0), k
    return math.floor(q), math.floor(q) + 1


def leg_cash(side, leg, notional, open_at, close_at):
    """dict(payments [(T, cash)], total, band (min, max), phase KNOWN / UNKNOWN /
    BOUNDARY). leg: dict(rate_per_period, interval_s, next_ts or None).

    A payment exactly at open_at or close_at is UNCERTAIN (whether a position opened or
    closed at that instant is paid depends on venue timing we cannot observe): it is
    left out of `total` and the band covers both cases, so the band is conservative for
    a paying leg as well as for a receiving one."""
    sign = -1.0 if side == "long" else 1.0
    r, iv = leg.get("rate_per_period"), leg.get("interval_s")
    if r is None or not iv:
        return dict(payments=[], total=None, band=(None, None), phase="RATE_OR_INTERVAL_MISSING")
    per = sign * r * notional
    ts = payment_times(leg.get("next_ts"), iv, open_at, close_at)
    if ts is None:
        n_lo, n_hi = possible_counts(close_at - open_at, iv)
        lo, hi = sorted((per * n_lo, per * n_hi))
        return dict(payments=[], total=None, band=(lo, hi), phase="UNKNOWN",
                    payments_possible=(n_lo, n_hi))
    pays = [(t, per) for t in ts]
    tot = sum(c for _, c in pays)
    # payments landing exactly on the open or close instant
    edge = [t for t in payment_times(leg["next_ts"], iv, open_at - 1e-6, close_at + 1e-6)
            if abs(t - open_at) < 1e-6 or abs(t - close_at) < 1e-6]
    if edge:
        extra = per * len(edge)
        band = (tot + min(0.0, extra), tot + max(0.0, extra))
        return dict(payments=pays, total=None, band=band, phase="BOUNDARY",
                    certain_total=tot, boundary_payments=edge)
    return dict(payments=pays, total=tot, band=(tot, tot), phase="KNOWN")


def pair_cash(long_leg, short_leg, notional, open_at, close_at):
    lo = leg_cash("long", long_leg, notional, open_at, close_at)
    sh = leg_cash("short", short_leg, notional, open_at, close_at)
    if lo["band"][0] is None or sh["band"][0] is None:
        band = (None, None)
    else:
        band = (lo["band"][0] + sh["band"][0], lo["band"][1] + sh["band"][1])
    keys = [("long", t) for t, _ in lo["payments"]] + [("short", t) for t, _ in sh["payments"]]
    assert len(keys) == len(set(keys)), "a payment counted twice"
    return dict(long=lo, short=sh, band=band,
                total=band[0] if band[0] == band[1] else None,
                common_horizon_8h=dict(
                    long=per_horizon(long_leg.get("rate_per_period"),
                                     long_leg.get("interval_s"), 8),
                    short=per_horizon(short_leg.get("rate_per_period"),
                                      short_leg.get("interval_s"), 8)))


def min_hold_to_breakeven(long_leg, short_leg, notional, open_at, round_trip_cost,
                          max_hours=168.0):
    """Shortest hold (hours) after which the funding cash of the discrete schedule
    covers `round_trip_cost` (entry + exit, both legs). Uses the band's LOW end when a
    phase is unknown. None when it never does within max_hours."""
    if round_trip_cost is None:
        return None
    candidates = set()
    for leg in (long_leg, short_leg):
        ts = payment_times(leg.get("next_ts"), leg.get("interval_s"), open_at,
                           open_at + max_hours * HOUR)
        if ts:
            candidates |= set(ts)
        elif leg.get("interval_s"):
            k = 1
            while k * leg["interval_s"] <= max_hours * HOUR:
                candidates.add(open_at + k * leg["interval_s"])
                k += 1
    for t in sorted(candidates):
        res = pair_cash(long_leg, short_leg, notional, open_at, t + 1.0)
        low = res["band"][0]
        if low is not None and low >= round_trip_cost:
            return (t - open_at) / HOUR
    return None
