"""Funding scenarios for an ESTIMATED funding position (PAPER research).

An estimated entry prices funding from the PUBLISHED rates of one moment and assumes
they stay CONSTANT over the horizon (assumption PUBLISHED_RATE_CONSTANT). That
assumption is reported, and these scenarios show what happens when it does not hold.
Each is a scenario, not a forecast; none is used to loosen an entry rule.

  constant_published     the assumption itself: carry x size x horizon
  history_mean_24h/_7d   each leg's published rate replaced by the mean of its own
                         SETTLED public history over 24 h / 7 d (legs without public
                         history keep the published rate and are listed); None when
                         no leg has history
  regime_shift_half      the carry halves from the entry onwards
  sign_flip_midway       the carry holds for half the horizon, then reverses
  sign_flip_at_entry     the carry reverses at once (worst plausible case)
  payment_schedule       discrete payments: only payment times inside (at, at+horizon]
                         pay. The next payment time is the venue's own (published
                         next_funding) or derived from the venue's settled history
                         timestamps; UNKNOWN when neither exists (then the worst case,
                         floor(horizon / interval) payments, is used and labelled)
Persistence (`persistence`): over the last 7 days of settled history, the share of
hours whose carry had the same sign as the current carry (both legs' history needed;
otherwise per-leg sign persistence of the leg's own rate, labelled PER_LEG_ONLY).

Rates are fractions per HOUR (positive = longs pay). Cash: the long pays rate x size,
the short receives it.
"""
import math
import statistics

HOUR = 3600.0


def per_hour(point, interval_s):
    r = point.get("rate")
    if not isinstance(r, (int, float)) or isinstance(r, bool) or r != r:
        return None
    if point.get("per_hour"):
        return r
    return r / ((interval_s or HOUR) / HOUR)


def history_mean(points, interval_s, at, hours):
    xs = [per_hour(p, interval_s) for p in points or []
          if at - hours * HOUR < p.get("ts", -1) <= at]
    xs = [x for x in xs if x is not None]
    return (statistics.mean(xs), len(xs)) if xs else (None, 0)


def next_payment(leg, at):
    """(ts of the next payment, basis) or (None, 'UNKNOWN')."""
    nxt = leg.get("next_funding")
    if nxt:
        while nxt <= at:
            nxt += leg.get("interval") or HOUR
        return nxt, "VENUE_PUBLISHED_NEXT_FUNDING"
    iv = leg.get("interval")
    ts = [p["ts"] for p in leg.get("history") or [] if p.get("ts")]
    if iv and len(ts) >= 3 and all(int(t) % int(iv) == 0 for t in ts[-24:]):
        return math.floor(at / iv) * iv + iv, "DERIVED_FROM_SETTLED_HISTORY_TIMES"
    return None, "UNKNOWN"


def payments_in(leg, at, horizon_h):
    iv = leg.get("interval")
    if not iv:
        return None, "INTERVAL_UNKNOWN"
    nxt, basis = next_payment(leg, at)
    end = at + horizon_h * HOUR
    if nxt is None:
        return int(horizon_h * HOUR // iv), "UNKNOWN_PHASE_WORST_CASE"
    if nxt > end:
        return 0, basis
    return 1 + int((end - nxt) // iv), basis


def persistence(long, short, at, carry_now):
    """Share of the last 7 days' hours with the same carry sign as now."""
    sign = 1 if carry_now > 0 else -1

    def hourly(leg):
        out = {}
        for p in leg.get("history") or []:
            if at - 7 * 86400 < p.get("ts", -1) <= at:
                r = per_hour(p, leg.get("interval"))
                if r is not None:
                    out[int(p["ts"] // HOUR)] = r
        return out

    hl, hs = hourly(long), hourly(short)
    both = sorted(set(hl) & set(hs))
    if len(both) >= 24:
        same = sum(1 for h in both if (hs[h] - hl[h]) * sign > 0)
        return dict(basis="BOTH_LEGS", hours=len(both), same_sign_share=same / len(both))
    per = {}
    for side, h, want in (("long", hl, -sign), ("short", hs, sign)):
        if len(h) >= 24:
            # the long helps the carry when its rate is negative (it is paid), the
            # short when its rate is positive
            per[side] = sum(1 for v in h.values() if v * want > 0) / len(h)
    return dict(basis="PER_LEG_ONLY" if per else "NO_HISTORY", hours=0, per_leg=per)


def scenarios(size, horizon_h, long, short, at, hurdle):
    """`long`/`short`: dict(rate per hour published, interval s, history [settled
    points], next_funding ts or None). `hurdle`: costs + reserve the funding must pay.
    Returns dict(assumption, scenarios [name, funding, net, basis], persistence)."""
    carry = short["rate"] - long["rate"]
    base = size * carry * horizon_h
    out = [dict(name="constant_published", funding=base, basis="PUBLISHED_RATE_CONSTANT")]
    for name, hours in (("history_mean_24h", 24), ("history_mean_7d", 168)):
        rl, nl = history_mean(long.get("history"), long.get("interval"), at, hours)
        rs, ns = history_mean(short.get("history"), short.get("interval"), at, hours)
        if nl == 0 and ns == 0:
            out.append(dict(name=name, funding=None, basis="NO_SETTLED_HISTORY"))
            continue
        c = (rs if ns else short["rate"]) - (rl if nl else long["rate"])
        missing = [s for s, n in (("long", nl), ("short", ns)) if n == 0]
        out.append(dict(name=name, funding=size * c * horizon_h,
                        basis="SETTLED_HISTORY_MEAN" + (" (published for %s)"
                                                        % ",".join(missing)
                                                        if missing else "")))
    out.append(dict(name="regime_shift_half", funding=base * 0.5, basis="SCENARIO"))
    out.append(dict(name="sign_flip_midway", funding=0.0, basis="SCENARIO"))
    out.append(dict(name="sign_flip_at_entry", funding=-base, basis="SCENARIO"))
    pays = {}
    for side, leg in (("long", long), ("short", short)):
        n, basis = payments_in(leg, at, horizon_h)
        pays[side] = dict(payments=n, basis=basis)
    if all(p["payments"] is not None for p in pays.values()):
        f = 0.0
        for side, leg, sign in (("long", long, -1), ("short", short, 1)):
            per_payment = leg["rate"] * (leg["interval"] / HOUR)
            f += sign * size * per_payment * pays[side]["payments"]
        out.append(dict(name="payment_schedule", funding=f, basis="DISCRETE_PAYMENTS",
                        payments=pays))
    else:
        out.append(dict(name="payment_schedule", funding=None, basis="INTERVAL_UNKNOWN",
                        payments=pays))
    for x in out:
        x["net"] = None if x["funding"] is None else x["funding"] - hurdle
    known = [x["net"] for x in out if x["net"] is not None]
    return dict(assumption="PUBLISHED_RATE_CONSTANT over %g h (not evidenced)" % horizon_h,
                carry_hourly=carry, scenarios=out, net_min=min(known) if known else None,
                persistence=persistence(long, short, at, carry))
