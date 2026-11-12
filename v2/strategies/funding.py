"""Funding arbitrage: long the leg that pays less, short the leg that receives more.

Income is funding only (no basis capture is credited). The expected funding over the
horizon is conservative per leg (the long pays the higher of its published and its
last-24h settled hourly rate, the short receives the lower) and is cut by the
realization factor. It needs SETTLED public payments on both legs: a venue that only
publishes a current rate (TxFlow, Variational) cannot prove the carry
(SETTLEMENT_EVIDENCE_MISSING). Costs and the break-even hold come from v2.economics
and wide_paper.funding_economics (default 24 h, unchanged).
"""
import statistics

from v2 import economics
from v2.strategies import COST, DATA, reason
from v2.strategies import common as cm

STRATEGY = "funding"


def evaluate(long, short, size, cfg, at):
    from wide_paper import breakeven_refusal, funding_economics

    out = dict(strategy=STRATEGY, reasons=list(cm.static_reasons(long, short)))
    for name, leg in (("long", long), ("short", short)):
        if not cm.settled_ok(leg, at, 7):
            have, expected = cm.settled_coverage(leg, at, 7)
            out["reasons"].append(reason(
                DATA, "SETTLEMENT_EVIDENCE_MISSING",
                "%s %s: settled payments %d/%d over 7 days (published rate only is not "
                "realized funding)" % (name, leg.row.get("venue"), have, expected)))
    gaps = cm.gap_series(long, short, at)
    if not cm.price_history_ok(gaps):
        out["reasons"].append(reason(DATA, "PRICE_HISTORY_MISSING",
                                     "%d/168 matched hourly closes" % len(gaps)))
    if out["reasons"]:
        return out
    days = cm.daily_carry(long, short, at, 7)
    if sum(days[:3]) <= 0 or sum(days) <= 0 or sum(days) - max(days) <= 0:
        out["reasons"].append(reason(COST, "CARRY_NOT_PERSISTENT",
                                     "realized carry 3d %.2e, 7d %.2e" % (sum(days[:3]),
                                                                          sum(days))))
    r_long, r_short = cm.realized_hourly(long, at), cm.realized_hourly(short, at)
    pay = max(long.hourly, r_long if r_long is not None else long.hourly)
    get = min(short.hourly, r_short if r_short is not None else short.hourly)
    hourly = get - pay
    horizon = cfg["wide_horizon"]
    expected = size * hourly * horizon
    sd = statistics.pstdev(gaps.values()) if len(gaps) > 1 else None
    reserve = size * sd if sd is not None else None
    c, why = cm.costs(long, short, size)
    if why:
        out["reasons"].append(why)
        return out
    econ = funding_economics(expected, horizon, c["long"]["fees"] + c["short"]["fees"], 0.0,
                             c["spread"], reserve, cfg["wide_funding_realization"])
    out.update(costs=c, economics=econ, hourly_carry=hourly,
               **economics.net(c, funding=econ["expected_funding"], reserve=reserve))
    cm.add_stress(out, c, funding=econ["expected_funding"], reserve=reserve)
    why = breakeven_refusal(econ, cfg)
    if why:
        out["reasons"].append(reason(COST, "BREAKEVEN", why))
    if out["net"] < cfg["wide_min_net"]:
        out["reasons"].append(reason(COST, "NET_BELOW_MIN", "net %.2f < wide_min_net %.2f"
                                     % (out["net"], cfg["wide_min_net"])))
    return out
