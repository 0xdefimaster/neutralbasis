"""Price convergence: long the cheap leg, short the dear leg while their log price gap
sits far from its normal level, close when it returns.

Statistics are robust (median and MAD of the last 7 days of matched hourly closes). The
credited capture is the move back to the median times the share of past episodes (at
least the entry threshold away) that did return within the maximum hold; with no such episode
nothing is credited (NO_EPISODES). The gap today comes from the two books' mids. Funding
during the expected hold is a cost from the PUBLISHED rates (labelled estimated);
settled evidence is not required because the position is not opened for carry.
"""
import math
import statistics

from v2 import economics
from v2.strategies import COST, DATA, reason
from v2.strategies import common as cm

STRATEGY = "convergence"
MAD_SCALE = 1.4826


def episodes(gaps, baseline, deviation, max_hold_h):
    """(returned, total, hours to return) of past moves at least `deviation` above the
    baseline, each counted once from its first hour."""
    ts = sorted(gaps)
    total = returned = 0
    hours, i = [], 0
    while i < len(ts):
        if gaps[ts[i]] - baseline >= deviation:
            total += 1
            j = i
            while j < len(ts) and gaps[ts[j]] - baseline > 0:
                j += 1
            if j < len(ts) and (ts[j] - ts[i]) / 3600 <= max_hold_h:
                returned += 1
                hours.append((ts[j] - ts[i]) / 3600)
            i = j + 1
        else:
            i += 1
    return returned, total, hours


def evaluate(long, short, size, cfg, at, k_entry=2.0):
    out = dict(strategy=STRATEGY, reasons=list(cm.static_reasons(long, short)))
    gaps = cm.gap_series(long, short, at)
    if not cm.price_history_ok(gaps):
        out["reasons"].append(reason(DATA, "PRICE_HISTORY_MISSING",
                                     "%d/168 matched hourly closes" % len(gaps)))
    if out["reasons"]:
        return out
    c, why = cm.costs(long, short, size)
    if why:
        out["reasons"].append(why)
        return out
    gap_now = math.log(c["short"]["mid"] / c["long"]["mid"])
    values = list(gaps.values())
    baseline = statistics.median(values)
    mad = statistics.median(abs(v - baseline) for v in values) * MAD_SCALE
    deviation = gap_now - baseline
    out.update(gap_now=gap_now, baseline=baseline, mad=mad, deviation=deviation)
    if mad <= 0 or deviation < k_entry * mad:
        out["reasons"].append(reason(COST, "GAP_NOT_EXTREME",
                                     "deviation %.1f bp < %.1f x MAD %.1f bp"
                                     % (deviation * 1e4, k_entry, mad * 1e4)))
        return out
    max_hold = cfg["wide_conv_max_hold_hours"]
    # Past moves at least as far as the entry threshold: how often such a state
    # returned to the median within the maximum hold.
    returned, total, hours = episodes(gaps, baseline, k_entry * mad, max_hold)
    if total == 0 or returned == 0:
        out["reasons"].append(reason(DATA, "NO_EPISODES",
                                     "no past move this large returned (%d/%d)"
                                     % (returned, total)))
        return out
    share = returned / total
    hold = statistics.median(hours)
    capture = size * deviation * share
    carry = size * (short.hourly - long.hourly) * hold  # published: estimated
    reserve = size * mad * 2
    out.update(costs=c, capture_share=share, expected_hold_h=hold, carry_estimated=carry,
               **economics.net(c, funding=carry, basis=capture, reserve=reserve))
    cm.add_stress(out, c, funding=carry, basis=capture, reserve=reserve)
    if out["net"] < cfg["wide_min_net"]:
        out["reasons"].append(reason(COST, "NET_BELOW_MIN", "net %.2f < wide_min_net %.2f"
                                     % (out["net"], cfg["wide_min_net"])))
    return out
