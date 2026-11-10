"""Offline walk-forward sweep of the convergence rule. RESEARCH ONLY: reads a pilot
database read-only; changes nothing in the engine.

For each pair direction and decision hour, the engine's own convergence_stats and
pair_stats run once on the 14 days of candles closed before the hour. Variants then differ
only in: the "widest" reference (max or a percentile of the deviations), the tier-1 entry
level, the exit (back to the normal gap, or half of the entry deviation closed), the
maximum hold, the correlation gate, the entry delay and the book cost.
Gates kept in every variant: 7-day and long-window crossings, baseline drift, the
walk-forward stop check, a move past the historical maximum is refused, tier-1 returns
(>= 3 back within the hold, <= 1 missed), the ex-ante $0.50-at-$1000 net (no funding).
Train = first half of the decision hours, test = second half. One open trade per pair
direction and variant. Realized funding of both legs over the hold is included.
"""
import argparse
import bisect
import itertools
import json
import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import wide_paper as wp  # noqa: E402

FEES = {"Hyperliquid": 0.00045, "Lighter": 0.00035, "RISEx": 0.0003,
        "LighterCore": 0.00028, "Extended": 0.00025}
LOOKBACK_DAYS = 14
MIN_NET = 0.0005

WIDEST = (("max", None), ("p99", 0.99), ("p95", 0.95), ("p90", 0.90), ("p80", 0.80))
LEVELS = (0.50, 0.35)
EXITS = ("normal", "half")
HOLDS = (24, 72)
CORR = (True, False)
DELAYS = (0, 1)
DRAGS = (0.0, 14.0)


def quantile(xs, q):
    i = (len(xs) - 1) * q
    lo = int(i)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def sqlite_ro(db):
    import sqlite3
    return sqlite3.connect("file:%s?mode=ro" % Path(db).as_posix(), uri=True, timeout=60)


def load(db):
    con = sqlite_ro(db)
    venues = sorted(FEES)
    q = ",".join("?" * len(venues))
    candles, funding = {}, {}
    for v, a, ts, p in con.execute(
            "select venue, asset, ts, price from hist_candles where venue in (%s)" % q, venues):
        if p and p > 0:
            candles.setdefault((v, a), {})[ts] = p
    for v, a, ts, r in con.execute(
            "select venue, asset, ts, rate from hist_funding where venue in (%s)" % q, venues):
        if r is not None:
            funding.setdefault((v, a), []).append((ts, r))
    for k in funding:
        funding[k].sort()
    return candles, funding


def paid(rows, start, end):
    if not rows:
        return None
    ts = [r[0] for r in rows]
    return sum(r[1] for r in rows[bisect.bisect_right(ts, start):bisect.bisect_right(ts, end)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--pairs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--drag-file", help="book_cost_snapshot output: each pair's own round-trip "
                    "book cost replaces the fixed scenarios; unknown cost = pair left out")
    a = ap.parse_args()
    t0 = time.time()
    cfg = dict(wp.WIDE_DEFAULTS)
    cfg["wide_min_history_days"] = 7
    exit_bps = cfg["wide_conv_exit_bps"] / 10000
    pairs = json.loads(Path(a.pairs).read_text(encoding="utf8"))
    candles, funding = load(a.db)
    legs = json.loads(Path(a.drag_file).read_text(encoding="utf8"))["legs"] if a.drag_file else None
    drags = ("pair",) if legs else DRAGS
    combos = list(itertools.product(WIDEST, LEVELS, EXITS, HOLDS, CORR, DELAYS, drags))
    names = ["%s|L%.2f|%s|H%d|corr%s|d%d|drag%s" % (w[0], lv, ex, h, c, d, g)
             for w, lv, ex, h, c, d, g in combos]
    skipped_unknown_cost = 0
    trades = {n: [] for n in names}
    evaluated = 0
    span = [None, None]
    for asset, va, vb in pairs:
        A, B = candles.get((va, asset)), candles.get((vb, asset))
        if not A or not B or va not in FEES or vb not in FEES:
            continue
        common = sorted(set(A) & set(B))
        if len(common) < (LOOKBACK_DAYS + 4) * 24:
            continue
        pair_drag = None
        if legs:
            ca = (legs.get("%s|%s" % (va, asset)) or {}).get("round_trip_bp")
            cb = (legs.get("%s|%s" % (vb, asset)) or {}).get("round_trip_bp")
            if ca is None or cb is None:
                skipped_unknown_cost += 1
                continue
            pair_drag = ca + cb
        start = wp.hour(common[0]) + LOOKBACK_DAYS * 86400 + 3600
        end = common[-1] - 72 * 3600
        for lv, sv, L, S in ((va, vb, A, B), (vb, va, B, A)):
            fl, fs = funding.get((lv, asset)), funding.get((sv, asset))
            fee4 = 2 * (FEES[lv] + FEES[sv])
            busy = dict.fromkeys(names, 0)
            for at in range(start, end, 3600):
                last = at - 3600
                if last not in L or last not in S:
                    continue
                lo = at - LOOKBACK_DAYS * 86400
                i0, i1 = bisect.bisect_left(common, lo), bisect.bisect_left(common, at)
                win = common[i0:i1]
                gaps = [math.log(S[t] / L[t]) for t in win]
                if len(gaps) < 24 * 7:
                    continue
                med = statistics.median(gaps)
                devs = sorted(x - med for x in gaps)
                live_dev = math.log(S[last] / L[last]) - med
                p80 = quantile(devs, 0.80)
                if p80 <= 0 or live_dev < 0.35 * p80:
                    continue  # no variant can enter below 0.35 x p80
                lw = {t: L[t] for t in win}
                sw = {t: S[t] for t in win}
                left = dict(prices=lw, price_type="trade")
                right = dict(prices=sw, price_type="trade")
                try:
                    hist = wp.convergence_stats(left, right, at, cfg)
                    stats = wp.pair_stats(left, right, at, 1, cfg["wide_min_history_days"])
                    days = hist["history_days"]
                    times = wp.matched(left, right, at, days * 24)
                except ValueError:
                    continue
                evaluated += 1
                span[0] = at if span[0] is None else min(span[0], at)
                span[1] = at if span[1] is None else max(span[1], at)
                if hist["cross7"] < cfg["wide_conv_min_cross_7"] or hist["two_sided7"] < 4:
                    continue
                if hist["cross30"] < math.ceil(cfg["wide_conv_min_cross_30"] * days / 30) or \
                        hist["two_sided30"] < math.ceil(15 * days / 30):
                    continue
                if abs(hist["baseline7"] - hist["baseline"]) > max(0.0002, hist["sd30"] * 0.5):
                    continue
                if hist["breaches"] > wp.CONV_MAX_BREACHES:
                    continue
                base = hist["baseline"]
                dev = [(t, math.log(sw[t] / lw[t]) - base) for t in times]
                dsorted = sorted(x for t, x in dev)
                live = math.log(S[last] / L[last])
                move = live - base
                if move > hist["widest"]:
                    continue
                corr_ok = min(stats["corr"], stats["corr30"]) >= cfg["wide_min_corr"]
                target = max(hist["baseline7"], base)
                qual_cache = {}
                for (wname, q), level, ex, hold, corr_gate, delay, drag in combos:
                    n = "%s|L%.2f|%s|H%d|corr%s|d%d|drag%s" % (wname, level, ex, hold,
                                                              corr_gate, delay, drag)
                    if drag == "pair":
                        drag = pair_drag
                    if busy[n] > at or (corr_gate and not corr_ok):
                        continue
                    widest = hist["widest"] if q is None else quantile(dsorted, q)
                    if widest <= 0 or move < level * widest:
                        continue
                    key = (wname, level, hold)
                    if key not in qual_cache:
                        back, missed = wp.excursions(dev, level * widest, exit_bps, hold)
                        qual_cache[key] = (len(back) >= cfg["wide_conv_min_episodes"]
                                           and missed <= wp.CONV_MAX_MISSES)
                    if not qual_cache[key]:
                        continue
                    if (live - target) - fee4 - drag / 1e4 < MIN_NET:
                        continue
                    et = last + delay * 3600
                    if et not in L or et not in S:
                        continue
                    eg = math.log(S[et] / L[et])
                    stop = base + widest * (1 + cfg["wide_conv_stop_pct"] / 100)
                    goal = base + exit_bps if ex == "normal" else live - move / 2
                    xt, xg, how = None, None, "max_hold"
                    for k in range(1, hold + 1):
                        t = et + k * 3600
                        if t not in L or t not in S:
                            continue
                        g = math.log(S[t] / L[t])
                        xt, xg = t, g
                        if g <= goal:
                            how = "exit"
                            break
                        if g >= stop:
                            how = "stop"
                            break
                    if xt is None:
                        continue
                    fs_ = paid(fs, et, xt + 3600)
                    fl_ = paid(fl, et, xt + 3600)
                    fund = (fs_ - fl_) if fs_ is not None and fl_ is not None else 0.0
                    net = (eg - xg) + fund - fee4 - drag / 1e4
                    trades[n].append((at, asset, lv, sv, how, round(net * 1e4, 2),
                                      round((eg - xg) * 1e4, 2), round(fund * 1e4, 2)))
                    busy[n] = xt + 3600
    mid = (span[0] + span[1]) / 2 if span[0] else 0

    def stat(sub):
        nets = [x[5] for x in sub]
        return dict(n=len(sub), pairs=len({x[1:4] for x in sub}),
                    days=len({x[0] // 86400 for x in sub}),
                    win=round(sum(v > 0 for v in nets) / len(nets), 2) if nets else None,
                    mean_bp=round(statistics.mean(nets), 2) if nets else None,
                    median_bp=round(statistics.median(nets), 2) if nets else None,
                    worst_bp=round(min(nets), 1) if nets else None,
                    capture_bp=round(statistics.mean(x[6] for x in sub), 2) if sub else None,
                    funding_bp=round(statistics.mean(x[7] for x in sub), 2) if sub else None,
                    stops=sum(x[4] == "stop" for x in sub))
    rows = [dict(variant=n, train=stat([x for x in ts if x[0] < mid]),
                 test=stat([x for x in ts if x[0] >= mid])) for n, ts in trades.items()]
    Path(a.out).write_text(json.dumps(dict(
        label="RESEARCH ONLY - offline walk-forward on stored public candles; not a forecast",
        fees=FEES, drag_file=a.drag_file, skipped_unknown_cost=skipped_unknown_cost,
        lookback_days=LOOKBACK_DAYS, split_at=mid, evaluated_hours=evaluated,
        seconds=round(time.time() - t0), rows=rows,
        trades={n: ts for n, ts in trades.items() if ts}), default=str), encoding="utf8")
    print(json.dumps(dict(seconds=round(time.time() - t0), evaluated_hours=evaluated,
                          variants=len(rows)), indent=1))


if __name__ == "__main__":
    main()
