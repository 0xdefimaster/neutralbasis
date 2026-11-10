"""Offline walk-forward sweep of funding carry with the price gap: for each pair direction
and decision hour, enter when the forecast funding over the hold, plus an optional
price-gap condition, beats 4 taker fees + book cost; hold H hours; book
price capture + realized funding - 4 fees - book cost.

RESEARCH ONLY. Reads a pilot database read-only; changes nothing in the engine.
Lookahead-safe: a decision at hour T sees candles that closed by T and settlements paid
by T. Parameters are chosen on the first half of the decision hours (train) and
reported on the second half (test). One open trade per pair direction (no overlap).

Usage: python tools/joint_sweep.py --db PATH --pairs pairs.json --out out.json
"""
import argparse
import bisect
import itertools
import json
import math
import statistics
import time
from pathlib import Path

FEES = {"Hyperliquid": 0.00045, "Lighter": 0.00035, "RISEx": 0.0003,
        "LighterCore": 0.00028, "Extended": 0.00025}
SIGNAL_DAYS = 7

HOLDS = (24, 48, 72, 168)
FORECASTS = ("last", "avg24", "avg72", "avg168", "min_all")
THRESH_BP = (5, 15, 30)
GAP_RULES = ("any", "gap_ge_median", "gap_ge_p75")
DRAGS = (0.0, 14.0)
DELAYS = (0, 1)


def sqlite_ro(db):
    import sqlite3
    return sqlite3.connect("file:%s?mode=ro" % Path(db).as_posix(), uri=True, timeout=60)


def load(db, venues):
    con = sqlite_ro(db)
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


class Paid:
    """Prefix sums of settled rates for fast window sums (start < ts <= end)."""

    def __init__(self, rows):
        self.ts = [r[0] for r in rows]
        self.cum = [0.0]
        for r in rows:
            self.cum.append(self.cum[-1] + r[1])

    def sum(self, start, end):
        i, j = bisect.bisect_right(self.ts, start), bisect.bisect_right(self.ts, end)
        return self.cum[j] - self.cum[i]

    def count(self, start, end):
        return bisect.bisect_right(self.ts, end) - bisect.bisect_right(self.ts, start)

    def last(self, end):
        j = bisect.bisect_right(self.ts, end)
        if not j:
            return None
        return self.ts[j - 1], self.cum[j] - self.cum[j - 1]


def per_hour(p, end, hours):
    """Average settled rate per hour over the last `hours` (None if payments are missing:
    hourly venues need >= 90 % of the hours covered)."""
    n = p.count(end - hours * 3600, end)
    if n < 0.9 * hours:
        return None
    return p.sum(end - hours * 3600, end) / hours


def forecast(pl, ps, end, rule):
    """Short-minus-long funding per hour that the rule expects to continue."""
    def spread(h):
        a, b = per_hour(ps, end, h), per_hour(pl, end, h)
        return None if a is None or b is None else a - b
    if rule == "last":
        a, b = ps.last(end), pl.last(end)
        if not a or not b or end - a[0] > 7200 or end - b[0] > 7200:
            return None
        return a[1] - b[1]
    if rule == "avg24":
        return spread(24)
    if rule == "avg72":
        return spread(72)
    if rule == "avg168":
        return spread(168)
    vals = [forecast(pl, ps, end, r) for r in ("last", "avg24", "avg72", "avg168")]
    return None if any(v is None for v in vals) else min(vals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--pairs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fee-scale", type=float, default=1.0,
                    help="scenario: 1 = 4 taker fees; 0.5 = half (e.g. one leg at a 0 maker fee, "
                         "UNVERIFIED); 0 = no fees (theoretical upper bound)")
    ap.add_argument("--drag-file", help="book_cost_snapshot output: each pair's own "
                    "round-trip book cost (today's books, an assumption) replaces the fixed "
                    "scenarios; pairs with an unknown cost are left out, never priced at 0")
    a = ap.parse_args()
    t0 = time.time()
    pairs = json.loads(Path(a.pairs).read_text(encoding="utf8"))
    candles, funding = load(a.db, sorted(FEES))
    legs = json.loads(Path(a.drag_file).read_text(encoding="utf8"))["legs"] if a.drag_file else None
    drags = ("pair",) if legs else DRAGS
    combos = list(itertools.product(HOLDS, FORECASTS, THRESH_BP, GAP_RULES, drags, DELAYS))
    skipped_unknown_cost = 0
    trades = {c: [] for c in combos}
    span_lo, span_hi = None, None
    for asset, va, vb in pairs:
        A, B = candles.get((va, asset)), candles.get((vb, asset))
        fa, fb = funding.get((va, asset)), funding.get((vb, asset))
        if not A or not B or not fa or not fb:
            continue
        common = sorted(set(A) & set(B))
        if len(common) < (SIGNAL_DAYS + 2) * 24:
            continue
        pair_drag = None
        if legs:
            ca = (legs.get("%s|%s" % (va, asset)) or {}).get("round_trip_bp")
            cb = (legs.get("%s|%s" % (vb, asset)) or {}).get("round_trip_bp")
            if ca is None or cb is None:
                skipped_unknown_cost += 1
                continue
            pair_drag = ca + cb
        gaps_all = {t: math.log(B[t] / A[t]) for t in common}
        for lv, sv, L, S, fl, fs in ((va, vb, A, B, fa, fb), (vb, va, B, A, fb, fa)):
            sign = 1 if sv == vb else -1  # gap = log(short / long)
            pl, ps = Paid(fl), Paid(fs)
            fee4 = 2 * (FEES[lv] + FEES[sv]) * a.fee_scale
            start = common[0] + SIGNAL_DAYS * 86400 + 3600
            stop = common[-1]
            # Per combo: when the pair direction is free again.
            busy = {c: 0 for c in combos}
            for at in range(start - start % 3600, stop, 3600):
                last = at - 3600
                if last not in gaps_all:
                    continue
                g_now = sign * gaps_all[last]
                i0 = bisect.bisect_left(common, at - SIGNAL_DAYS * 86400)
                i1 = bisect.bisect_left(common, at)
                past = [sign * gaps_all[t] for t in common[i0:i1]]
                if len(past) < 0.9 * SIGNAL_DAYS * 24:
                    continue
                past.sort()
                med, p75 = past[len(past) // 2], past[int(len(past) * 0.75)]
                fc = {r: forecast(pl, ps, at, r) for r in FORECASTS}
                span_lo = at if span_lo is None else min(span_lo, at)
                span_hi = at if span_hi is None else max(span_hi, at)
                outcome = {}

                def result(hold, delay):
                    key = (hold, delay)
                    if key not in outcome:
                        et = last + delay * 3600
                        xt = et + hold * 3600
                        if et not in gaps_all or xt not in gaps_all or \
                                pl.count(et + 3600, xt + 3600) < 0.9 * hold or \
                                ps.count(et + 3600, xt + 3600) < 0.9 * hold:
                            outcome[key] = None  # never assume a missing payment
                        else:
                            # Payments while held: after the entry close, to the exit close.
                            outcome[key] = (
                                sign * (gaps_all[et] - gaps_all[xt]),
                                ps.sum(et + 3600, xt + 3600) - pl.sum(et + 3600, xt + 3600),
                                xt + 3600)
                    return outcome[key]

                for c in combos:
                    hold, rule, thr, gap_rule, drag, delay = c
                    if drag == "pair":
                        drag = pair_drag
                    if busy[c] > at or fc[rule] is None:
                        continue
                    if gap_rule == "gap_ge_median" and g_now < med:
                        continue
                    if gap_rule == "gap_ge_p75" and g_now < p75:
                        continue
                    if fc[rule] * hold - fee4 - drag / 1e4 < thr / 1e4:
                        continue
                    r = result(hold, delay)
                    if r is None:
                        continue
                    capture, fund, free = r
                    net = capture + fund - fee4 - drag / 1e4
                    trades[c].append((at, asset, lv, sv, net * 1e4, capture * 1e4, fund * 1e4))
                    busy[c] = free
    mid = (span_lo + span_hi) / 2 if span_lo else 0
    rows = []
    for c, ts in trades.items():
        hold, rule, thr, gap_rule, drag, delay = c

        def stat(sub):
            nets = [x[4] for x in sub]
            return dict(n=len(sub), pairs=len({(x[1], x[2], x[3]) for x in sub}),
                        days=len({x[0] // 86400 for x in sub}),
                        win=round(sum(n > 0 for n in nets) / len(nets), 2) if nets else None,
                        mean_bp=round(statistics.mean(nets), 2) if nets else None,
                        median_bp=round(statistics.median(nets), 2) if nets else None,
                        total_bp=round(sum(nets), 1),
                        capture_bp=round(statistics.mean(x[5] for x in sub), 2) if sub else None,
                        funding_bp=round(statistics.mean(x[6] for x in sub), 2) if sub else None)
        rows.append(dict(hold=hold, forecast=rule, thresh_bp=thr, gap_rule=gap_rule,
                         drag_bp=drag, delay=delay,
                         train=stat([x for x in ts if x[0] < mid]),
                         test=stat([x for x in ts if x[0] >= mid])))
    out = dict(label="RESEARCH ONLY - offline walk-forward on stored public data; not a forecast",
               fees=FEES, fee_scale=a.fee_scale, drag_file=a.drag_file,
               skipped_unknown_cost=skipped_unknown_cost, signal_days=SIGNAL_DAYS, split_at=mid, seconds=round(time.time() - t0),
               combos=rows,
               trades={"|".join(map(str, c)): ts for c, ts in trades.items() if ts})
    Path(a.out).write_text(json.dumps(out, default=str), encoding="utf8")
    good = [r for r in rows if r["train"]["n"] >= 10 and (r["train"]["mean_bp"] or 0) > 0]
    good.sort(key=lambda r: -(r["train"]["mean_bp"] or 0))
    print(json.dumps(dict(seconds=out["seconds"], combos=len(rows),
                          train_positive=len(good), top=good[:10]), indent=1))


if __name__ == "__main__":
    main()
