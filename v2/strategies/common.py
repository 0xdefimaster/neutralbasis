"""Inputs shared by the strategies: a leg (row + book + settled payments + hourly
closes) and the evidence checks on them."""
import math
import statistics
from dataclasses import dataclass, field

from v2 import economics
from v2.strategies import COST, DATA, EXECUTION, reason

DAY = 86400
HOUR = 3600
COVERAGE = 0.95  # as wide_paper.PAYMENT_COVERAGE


@dataclass
class Leg:
    row: dict                      # strategy row (v2.source.build), per-unit normalized
    book: dict = None              # normalized book, or None
    settled: list = None           # [dict(ts, rate fraction per payment)] or None
    closes: dict = None            # {hour ts: close} or None
    extra: dict = field(default_factory=dict)

    @property
    def interval(self):
        return self.row.get("interval")

    @property
    def hourly(self):
        """Published rate as a fraction per hour (positive = longs pay)."""
        return self.row.get("rate")


def priced(x):
    r = x.get("rate")
    return isinstance(r, (int, float)) and not isinstance(r, bool) and r == r


def settled_coverage(leg, at, days):
    """(have, expected) settled payments in (at - days, at]."""
    if not leg.settled or not leg.interval:
        return 0, int(days * DAY / (leg.interval or HOUR))
    # Only points with a numeric rate count (a payment row with an unverified rate
    # unit, rate None, proves a payment but not the market rate).
    have = sum(1 for x in leg.settled if at - days * DAY < x["ts"] <= at
               and priced(x))
    return have, int(days * DAY / leg.interval)


def settled_ok(leg, at, days=7):
    have, expected = settled_coverage(leg, at, days)
    return expected > 0 and have >= COVERAGE * expected


def daily_carry(long, short, at, days=7):
    """Realized carry per $1 for each of the last `days` days (index 0 = last 24h):
    what the short received minus what the long paid."""
    out = [0.0] * days
    for leg, sign in ((short, 1), (long, -1)):
        for x in leg.settled or []:
            if not priced(x):
                continue
            age = at - x["ts"]
            if 0 <= age < days * DAY:
                out[int(age // DAY)] += sign * x["rate"]
    return out


def realized_hourly(leg, at, hours=24):
    xs = [x["rate"] for x in leg.settled or [] if at - hours * HOUR < x["ts"] <= at
          and priced(x)]
    if not xs or not leg.interval:
        return None
    return statistics.mean(xs) / (leg.interval / HOUR)


def gap_series(long, short, at, days=7):
    """{hour: log(short/long)} over the hours both legs have closes."""
    if not long.closes or not short.closes:
        return {}
    start = at - days * DAY
    out = {}
    for t, lc in long.closes.items():
        sc = short.closes.get(t)
        if t >= start and lc and sc and lc > 0 and sc > 0:
            out[t] = math.log(sc / lc)
    return out


def price_history_ok(gaps, days=7, coverage=0.9):
    return len(gaps) >= coverage * days * 24


def static_reasons(long, short):
    """Checks that do not depend on the strategy: books present, fees known."""
    out = []
    for name, leg in (("long", long), ("short", short)):
        if leg.row.get("fee") is None:
            out.append(reason(DATA, economics.FEE_UNKNOWN, "%s %s taker fee UNKNOWN"
                              % (name, leg.row.get("venue"))))
        if leg.book is None:
            out.append(reason(DATA, economics.BOOK_MISSING, "%s %s book missing"
                              % (name, leg.row.get("venue"))))
        elif leg.book.get("model") == "rfq_indicative":
            out.append(reason(EXECUTION, "EXECUTION_MODEL_UNVERIFIED",
                              "%s %s: indicative RFQ quote is not a fill price"
                              % (name, leg.row.get("venue"))))
    return out


def costs(long, short, size):
    c, why = economics.round_trip(long.book, short.book, size, long.row.get("fee"),
                                  short.row.get("fee"))
    if why is None:
        return c, None
    cat = DATA if why in (economics.BOOK_MISSING, economics.FEE_UNKNOWN) else EXECUTION
    return None, reason(cat, why)


__all__ = ["Leg", "COST", "DATA", "EXECUTION", "reason"]


def add_stress(out, c, funding=0.0, basis=0.0, reserve=0.0):
    """Entry and exit costs apart, the stress scenarios, and a COST reason when the
    combined moderate scenario loses money (the exit is priced on today's book, which
    does not guarantee tomorrow's liquidity)."""
    out.update(entry_cost=c["entry_cost"], exit_cost=c["exit_cost"],
               exit_basis=c["exit_basis"],
               stress=economics.stress(c, funding, basis, reserve or 0.0))
    moderate = next(x["net"] for x in out["stress"] if x["name"] == "combined_moderate")
    if moderate < 0:
        out["reasons"].append(reason(COST, "STRESS_NEGATIVE",
                                     "combined moderate stress net %.2f < 0" % moderate))
