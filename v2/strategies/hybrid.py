"""Hybrid: one position that earns the funding carry AND the convergence capture.

It is not the sum of two trades: one position pays one round trip and one risk
reserve. Funding is taken over the convergence's expected hold (not the funding
horizon), conservative and from SETTLED evidence as in the funding strategy; the basis
capture is the convergence's. When funding favours the position but the gap moves the
wrong way (or the reverse), the components are reported apart and the net shows it.

Evaluation only: the paper desk has no hybrid position type yet (a position with two
exit rules); a hybrid result is research until that exists (docs/v2/19).
"""
from v2 import economics
from v2.strategies import COST, reason
from v2.strategies import convergence, funding

STRATEGY = "hybrid"


def evaluate(long, short, size, cfg, at):
    f = funding.evaluate(long, short, size, cfg, at)
    c = convergence.evaluate(long, short, size, cfg, at)
    data = [r for r in f["reasons"] + c["reasons"] if r["category"] != COST]
    out = dict(strategy=STRATEGY, funding_part=f, convergence_part=c, research_only=True)
    if data:
        seen, uniq = set(), []
        for r in data:
            if r["code"] not in seen:
                seen.add(r["code"])
                uniq.append(r)
        out["reasons"] = uniq
        return out
    if "expected_hold_h" not in c or "economics" not in f:
        out["reasons"] = [r for r in f["reasons"] + c["reasons"]]
        return out
    hold = c["expected_hold_h"]
    funding_cash = f["hourly_carry"] * size * hold * cfg["wide_funding_realization"]
    reserve = max(f.get("reserve") or 0.0, c.get("reserve") or 0.0)
    out.update(**economics.net(c["costs"], funding=funding_cash, basis=c["basis"],
                               reserve=reserve), expected_hold_h=hold)
    out["reasons"] = []
    if out["net"] < cfg["wide_min_net"]:
        out["reasons"].append(reason(COST, "NET_NEGATIVE", "net %.2f < %.2f"
                                     % (out["net"], cfg["wide_min_net"])))
    return out
