"""V2 evidence on a frozen pilot dataset (tools/v2_dataset.py): baseline, replayable
economic benchmark and observer comparison.

PAPER / research only. Reads the dataset file only (read-only); no network, no engine,
no pilot database. Same dataset -> same numbers: iteration is sorted, nothing random.

  baseline   every paper trade with its outcome class and PnL components, open
             positions at stop, periods and settings of each pilot
  benchmark  (a) the funding forecast the desk makes at entry, replayed on the
             recorded public funding payments and compared with what was paid next;
             (b) each pilot trade's entry estimate against its result
  observer   TxFlow and Variational observer rows against the pilot's own marks and
             against Hyperliquid's published payments (consistency and plausibility
             only: neither venue publishes settlements, so nothing here verifies a
             payment)

Usage: python -m tools.v2_evidence {baseline,benchmark,observer,all} --dataset DIR
Results are written to DIR/results/<name>.json.
"""
import argparse
import bisect
import collections
import json
import math
import re
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import capabilities  # noqa: E402
from wide_paper import OUTCOME_LEG_RECOVERY, outcome_of  # noqa: E402

DAY = 86400
HOUR = 3600
PERSIST_DAYS = 7  # as wide_paper
PAYMENT_COVERAGE = 0.95  # as wide_paper
HORIZONS = (24, 72, 168)
GRID_HOURS = 6


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % Path(path).as_posix(), uri=True)


def q(xs, p):
    """Plain percentile (nearest rank) of a non-empty list."""
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(math.ceil(p * len(xs))) - 1))]


def dist(xs, scale=1.0):
    if not xs:
        return None
    xs = [x * scale for x in xs]
    return dict(n=len(xs), mean=statistics.mean(xs), p10=q(xs, 0.10), p50=q(xs, 0.50),
                p90=q(xs, 0.90), min=min(xs), max=max(xs))


# ---------------------------------------------------------------- baseline ------------
USD = r"([+-]?)\$([\d.]+)"


def money(sign, value):
    return -float(value) if sign == "-" else float(value)


def parse_entry(reason):
    """Estimates printed in a pilot entry reason (PARSED_FROM_TEXT; the structured
    decision of closed LOOSE trades was not kept across period resets)."""
    out = {}
    if not reason:
        return out
    m = re.search(r"beklenen (\d+) saatlik funding " + USD, reason)
    if m:
        out["horizon_hours"] = int(m.group(1))
        out["expected_funding"] = money(m.group(2), m.group(3))
    m = re.search(r"\bnet " + USD, reason)
    if m:
        out["net"] = money(m.group(1), m.group(2))
    m = re.search(r"ortalama dönüş (\d+) saat", reason)
    if m:
        out["expected_hours"] = int(m.group(1))
    m = re.search(r"sapma ([\d.]+) bp", reason)
    if m:
        out["entry_gap_bp"] = float(m.group(1))
    return out


def baseline(db):
    trades = []
    ledger = collections.defaultdict(lambda: collections.Counter())
    for pilot, position, kind, amount in db.execute(
            "select pilot, position, kind, amount from ledger_events"):
        if amount is not None:
            ledger[(pilot, position)][kind] += amount
    opened = {}
    for pilot, body in db.execute("select pilot, body from events where kind='WIDE_OPEN'"):
        b = json.loads(body)
        opened[(pilot, b.get("position"))] = b.get("net")
    for row in db.execute(
            "select pilot, id, period_id, strategy, asset, long, short, opened, closed_at,"
            " size, price_pnl, funding, fees, net, reason, pending, body from trades"
            " order by opened"):
        (pilot, tid, period, strategy, asset, lv, sv, t0, t1, size, price, funding, fees,
         net, reason, pending, body) = row
        b = json.loads(body or "{}")
        led = ledger[(pilot, tid)]
        entry = parse_entry(b.get("entry_reason"))
        x = dict(pilot=pilot, id=tid, period=period, strategy=strategy, asset=asset,
                 long=lv, short=sv, opened=t0, closed_at=t1,
                 hold_hours=(t1 - t0) / HOUR if t1 and t0 else None,
                 size=size, price_pnl=price, funding=funding,
                 funding_settled=led.get("funding", 0.0) + led.get("funding_late", 0.0),
                 funding_estimated=b.get("estimated_funding") or 0.0,
                 fees=fees, slippage=b.get("slippage"), net=net, reason=reason,
                 pending_funding=bool(pending), fill_ratio=b.get("fill_ratio"),
                 predicted_net=opened.get((pilot, tid)), entry_parsed=entry,
                 entry_reason=b.get("entry_reason"))
        x["outcome"] = outcome_of(dict(x, venues=[v for v in (lv, sv) if v]))
        x["price_ex_slippage"] = (
            price + x["slippage"] if x["slippage"] is not None else None)
        trades.append(x)

    def total(xs):
        return dict(
            trades=len(xs), wins=sum(t["net"] > 0 and t["outcome"] != OUTCOME_LEG_RECOVERY
                                     for t in xs),
            net=sum(t["net"] for t in xs), price_pnl=sum(t["price_pnl"] for t in xs),
            funding_settled=sum(t["funding_settled"] for t in xs),
            funding_estimated=sum(t["funding_estimated"] for t in xs),
            fees=sum(t["fees"] for t in xs),
            slippage=sum(t["slippage"] or 0.0 for t in xs),
            hold_hours=sum(t["hold_hours"] or 0.0 for t in xs))

    groups = collections.defaultdict(list)
    for t in trades:
        groups[("ALL", "ALL")].append(t)
        groups[(t["strategy"], "ALL")].append(t)
        groups[(t["strategy"], t["outcome"])].append(t)
        groups[("pilot:" + t["pilot"], t["strategy"])].append(t)
    open_positions = []
    for pilot, v in db.execute("select pilot, v from kv where k='wide_positions'"):
        for p in json.loads(v):
            d = p.get("decision") or {}
            open_positions.append(dict(
                pilot=pilot, asset=p["asset"], strategy=p.get("strategy"),
                legs=[(l["venue"], l["side"]) for l in p.get("legs", [])],
                opened=p.get("opened"), net_if_closed=p.get("net_if_closed"),
                funding=p.get("funding"), fees=p.get("fees"),
                predicted=dict((k, d.get(k)) for k in (
                    "net", "size", "conservative_funding", "funding_day1", "funding_day3",
                    "entry_fee", "exit_fee", "execution_drag", "risk_reserve"))))
    periods = []
    for row in db.execute("select pilot, id, started, ended, capital, venues, workspace,"
                          " config, trades, net, source from periods order by pilot, id"):
        pilot, pid, s, e, cap, venues, ws, cfg, n, net, src = row
        cfg = json.loads(cfg or "null") or {}
        periods.append(dict(
            pilot=pilot, id=pid, started=s, ended=e, capital=cap,
            venues=sorted((json.loads(venues or "null") or {}).keys()),
            trades=n, net=net, source=src,
            config={k: cfg.get(k) for k in sorted(cfg) if k.startswith("wide_")}))
    boots = dict(db.execute(
        "select pilot, count(*) from events where kind='BOOT' group by pilot").fetchall())
    meta = dict(db.execute("select k, v from meta").fetchall())
    return dict(
        label="Paper trades are SIMULATED; prices and funding are OBSERVED public data",
        dataset_created_utc=meta.get("created_utc"),
        trades=trades,
        totals={"%s/%s" % k: total(v) for k, v in sorted(groups.items())},
        open_positions=open_positions,
        open_net_if_closed=sum(p["net_if_closed"] or 0.0 for p in open_positions),
        periods=periods, boots=boots,
        missing=[
            "Closed LOOSE trades: the structured entry decision (fees, drag, reserve) "
            "was dropped at each period reset (kv wide_closed is per period); only the "
            "WIDE_OPEN predicted net and the entry_reason text remain.",
        ])


# ---------------------------------------------------------------- benchmark -----------
def periodic_venues():
    return {v for v, m in capabilities.FUNDING_MODELS.items()
            if m.get("model") == capabilities.PERIODIC}


class Series:
    """One venue/asset's published payments, with prefix sums for window sums."""

    def __init__(self, rows):
        self.ts = [t for t, _ in rows]
        self.rate = [r for _, r in rows]
        self.pre = [0.0]
        for r in self.rate:
            self.pre.append(self.pre[-1] + r)
        gaps = [b - a for a, b in zip(self.ts, self.ts[1:])]
        self.interval = statistics.median(gaps) if gaps else None

    def idx(self, a, b):
        return bisect.bisect_right(self.ts, a), bisect.bisect_right(self.ts, b)

    def sum(self, a, b):
        i, j = self.idx(a, b)
        return self.pre[j] - self.pre[i]

    def count(self, a, b):
        i, j = self.idx(a, b)
        return j - i

    def last(self, t):
        i = bisect.bisect_right(self.ts, t)
        return self.rate[i - 1] if i else None

    def mean(self, a, b):
        n = self.count(a, b)
        return self.sum(a, b) / n if n else None

    def covered(self, a, b):
        if not self.interval:
            return False
        return self.count(a, b) >= PAYMENT_COVERAGE * (b - a) / self.interval


def forecast(long_s, short_s, t, hours):
    """The desk's funding forecast for one direction at time t, per $1 notional over
    `hours` (assess_funding): min(day1, day3), with its persistence gates. The live
    quote is not in the record; the last published payment stands in for it
    (APPROXIMATION, labelled in the result)."""
    if not (long_s.covered(t - PERSIST_DAYS * DAY, t)
            and short_s.covered(t - PERSIST_DAYS * DAY, t)):
        return None
    raw = {}
    for side, s in (("long", long_s), ("short", short_s)):
        cur, r24 = s.last(t), s.mean(t - DAY, t)
        if cur is None or r24 is None:
            return None
        raw[side] = max(cur, r24) if side == "long" else min(cur, r24)
    day1 = (raw["short"] * hours * HOUR / short_s.interval
            - raw["long"] * hours * HOUR / long_s.interval)
    days = [short_s.sum(t - (k + 1) * DAY, t - k * DAY)
            - long_s.sum(t - (k + 1) * DAY, t - k * DAY) for k in range(PERSIST_DAYS)]
    day3 = sum(days[:3]) / 3 * hours / 24
    week = sum(days)
    eligible = day1 > 0 and sum(days[:3]) > 0 and week > 0 and week - max(days) > 0
    return dict(forecast=min(day1, day3), day1=day1, day3=day3, eligible=eligible)


def replay(db, pilot="LOOSE", horizons=HORIZONS, grid_hours=GRID_HOURS):
    allowed = periodic_venues()
    rows = collections.defaultdict(list)
    excluded = collections.Counter()
    for v, a, t, r in db.execute(
            "select venue, asset, ts, rate from hist_funding where pilot=?"
            " order by venue, asset, ts", (pilot,)):
        if v in allowed:
            rows[(v, a)].append((t, r))
        else:
            excluded[v] += 1
    series = {k: Series(v) for k, v in rows.items() if len(v) > 24}
    by_asset = collections.defaultdict(list)
    for (v, a) in sorted(series):
        by_asset[a].append(v)
    lo = min(s.ts[0] for s in series.values())
    hi = max(s.ts[-1] for s in series.values())
    out = dict(pilot=pilot, venues=sorted({v for v, _ in series}),
               excluded_rows=dict(excluded),
               excluded_reason="funding model is not PERIODIC in capabilities."
                               "FUNDING_MODELS (e.g. Paradex: rate samples, not payments)",
               span=[lo, hi], approximation="live quote = last published payment",
               horizons={})
    for hours in horizons:
        samples = []
        t = math.ceil((lo + PERSIST_DAYS * DAY) / (grid_hours * HOUR)) * grid_hours * HOUR
        while t + hours * HOUR <= hi:
            for a in sorted(by_asset):
                vs = by_asset[a]
                for lv in vs:
                    for sv in vs:
                        if lv == sv:
                            continue
                        L, S = series[(lv, a)], series[(sv, a)]
                        if not (L.covered(t, t + hours * HOUR)
                                and S.covered(t, t + hours * HOUR)):
                            continue
                        f = forecast(L, S, t, hours)
                        if not f or not f["eligible"] or f["forecast"] <= 0:
                            continue
                        realized = S.sum(t, t + hours * HOUR) - L.sum(t, t + hours * HOUR)
                        samples.append(dict(t=t, asset=a, long=lv, short=sv,
                                            forecast=f["forecast"], realized=realized))
            t += grid_hours * HOUR
        out["horizons"][str(hours)] = summarize(samples)
    return out


def summarize(samples):
    if not samples:
        return dict(n=0)
    best = {}
    for s in samples:
        k = (s["t"], s["asset"])
        if k not in best or s["forecast"] > best[k]["forecast"]:
            best[k] = s
    top = {}
    for s in best.values():
        if s["t"] not in top or s["forecast"] > top[s["t"]]["forecast"]:
            top[s["t"]] = s

    def stats(xs):
        f = sum(x["forecast"] for x in xs)
        r = sum(x["realized"] for x in xs)
        return dict(
            n=len(xs), assets=len({x["asset"] for x in xs}),
            forecast_bp=dist([x["forecast"] for x in xs], 1e4),
            realized_bp=dist([x["realized"] for x in xs], 1e4),
            realization_ratio=r / f if f else None,
            realized_positive=sum(x["realized"] > 0 for x in xs) / len(xs),
            realized_at_least_forecast=sum(x["realized"] >= x["forecast"] for x in xs)
            / len(xs),
            pairs=collections.Counter(
                "%s>%s" % (x["long"], x["short"]) for x in xs).most_common(8))

    return dict(all_eligible=stats(samples), best_per_asset=stats(list(best.values())),
                top_per_time=stats(list(top.values())), samples=samples)


def coverage_of_cost(samples, cost_bp):
    """Share of samples whose realized carry pays the round-trip cost."""
    if not samples:
        return None
    return sum(s["realized"] * 1e4 >= cost_bp for s in samples) / len(samples)


def trade_benchmark(base):
    out = []
    for t in base["trades"]:
        e = t["entry_parsed"]
        x = dict(pilot=t["pilot"], asset=t["asset"], strategy=t["strategy"],
                 outcome=t["outcome"], hold_hours=t["hold_hours"],
                 predicted_net=t["predicted_net"], realized_net=t["net"],
                 cost_realized=t["fees"] + (t["slippage"] or 0.0),
                 cost_realized_bp=(t["fees"] + (t["slippage"] or 0.0)) / t["size"] * 1e4
                 if t["size"] else None)
        if t["strategy"] == "funding" and e.get("expected_funding") is not None:
            per_hour = e["expected_funding"] / e["horizon_hours"]
            x.update(expected_funding_horizon=e["expected_funding"],
                     horizon_hours=e["horizon_hours"],
                     expected_funding_for_hold=per_hour * (t["hold_hours"] or 0.0),
                     realized_funding=t["funding"])
        out.append(x)
    two_leg_funding = [x for x in out if x["strategy"] == "funding"
                       and x["outcome"] != OUTCOME_LEG_RECOVERY and x["cost_realized_bp"]]
    exp = sum(x.get("expected_funding_for_hold") or 0.0 for x in two_leg_funding)
    got = sum(x.get("realized_funding") or 0.0 for x in two_leg_funding)
    return dict(
        trades=out,
        funding_two_leg=dict(
            n=len(two_leg_funding),
            expected_funding_for_actual_holds=exp, realized_funding=got,
            realization_ratio=got / exp if exp else None,
            round_trip_cost_bp=dist([x["cost_realized_bp"] for x in two_leg_funding]),
            predicted_net=sum(x["predicted_net"] or 0.0 for x in two_leg_funding),
            realized_net=sum(x["realized_net"] for x in two_leg_funding)),
        convergence_two_leg=dict(
            n=sum(x["strategy"] == "convergence" and x["outcome"] != OUTCOME_LEG_RECOVERY
                  for x in out),
            predicted_net=sum(x["predicted_net"] or 0.0 for x in out
                              if x["strategy"] == "convergence"
                              and x["outcome"] != OUTCOME_LEG_RECOVERY),
            realized_net=sum(x["realized_net"] for x in out
                             if x["strategy"] == "convergence"
                             and x["outcome"] != OUTCOME_LEG_RECOVERY)))


def benchmark(db, base):
    rep = replay(db)
    trades = trade_benchmark(base)
    cost = trades["funding_two_leg"]["round_trip_cost_bp"]
    for hours, h in rep["horizons"].items():
        samples = h.pop("samples", [])
        if cost and samples:
            best, top = {}, {}
            for s in samples:
                k = (s["t"], s["asset"])
                if k not in best or s["forecast"] > best[k]["forecast"]:
                    best[k] = s
                if s["t"] not in top or s["forecast"] > top[s["t"]]["forecast"]:
                    top[s["t"]] = s
            c = cost["p50"]
            h["pays_measured_round_trip"] = dict(
                cost_bp_p50=c,
                all_eligible=coverage_of_cost(samples, c),
                best_per_asset=coverage_of_cost(list(best.values()), c),
                top_per_time=coverage_of_cost(list(top.values()), c),
                top_per_time_forecast_said=coverage_of_cost(
                    [dict(s, realized=s["forecast"]) for s in top.values()], c))
            # Break-even hours of the top opportunity at its realized hourly carry.
            h["top_per_time_realized_breakeven_hours"] = dist(
                [c / (s["realized"] * 1e4 / int(hours)) for s in top.values()
                 if s["realized"] > 0])
    return dict(label="Replay on recorded public funding payments (OBSERVED); the "
                      "trades are SIMULATED paper results", funding_forecast=rep,
                trades=trades)


# ---------------------------------------------------------------- observer ------------
def nearest(ts_list, values, t, tol):
    i = bisect.bisect_left(ts_list, t)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(ts_list) and abs(ts_list[j] - t) <= tol:
            if best is None or abs(ts_list[j] - t) < abs(ts_list[best] - t):
                best = j
    return values[best] if best is not None else None


def observer(db):
    marks = collections.defaultdict(list)
    for v, a, t, p in db.execute(
            "select venue, asset, ts, price from marks_window order by venue, asset, ts"):
        marks[(v, a)].append((t, p))
    marks = {k: ([t for t, _ in v], [p for _, p in v]) for k, v in marks.items()}
    hl = collections.defaultdict(list)
    for a, t, r in db.execute(
            "select asset, ts, rate from hist_funding where pilot='LOOSE' and"
            " venue='Hyperliquid' order by asset, ts"):
        hl[a].append((t, r))
    hl = {a: ([t for t, _ in v], [r for _, r in v]) for a, v in hl.items()}
    out = {}
    for venue, ref_venues in (("TxFlow", ("TxFlow",)),
                              ("Variational", ("Extended", "Hyperliquid", "Lighter"))):
        rows = db.execute(
            "select asset, observed, rate_raw, rate_unit, interval_s, mark from"
            " observations where venue=? order by asset, observed", (venue,)).fetchall()
        assets = {r[0] for r in rows}
        mark_diff, rate_pairs, units, intervals = [], [], collections.Counter(), []
        for a, t, raw, unit, iv, mark in rows:
            units[unit] += 1
            if iv:
                intervals.append(iv)
            for rv in ref_venues:
                m = marks.get((rv, a))
                ref = nearest(m[0], m[1], t, 60) if m else None
                if ref and mark:
                    mark_diff.append(abs(mark / ref - 1) * 1e4)
                    break
            if raw is None or not iv or a not in hl:
                continue
            # Per hour, as a fraction: TxFlow publishes percent per payment (adapter
            # divides by 100), Variational a fraction per interval.
            per_payment = raw / 100 if unit == "percent per payment" else raw
            mine = per_payment * HOUR / iv
            ref = nearest(hl[a][0], hl[a][1], t, 2 * HOUR)
            if ref is not None:
                rate_pairs.append((mine, ref))
        rounds = db.execute(
            "select count(*), sum(errors < 0), sum(case when errors > 0 then errors"
            " else 0 end) from observer_rounds where venue=?", (venue,)).fetchone()
        notes = collections.Counter(
            (n or "")[:120] for (n,) in db.execute(
                "select note from observer_rounds where venue=? and errors < 0", (venue,)))
        same_sign = [math.copysign(1, a) == math.copysign(1, b) for a, b in rate_pairs
                     if a and b]
        out[venue] = dict(
            label="OBSERVED_PUBLISHED; settlement UNVERIFIED (no public settlement record)",
            rows=len(rows), assets=len(assets), units=dict(units),
            interval_hours=dist(intervals, 1 / HOUR),
            rounds=dict(total=rounds[0], failed=rounds[1] or 0, market_errors=rounds[2] or 0,
                        failure_notes=notes.most_common(5)),
            mark_vs_reference=dict(reference=list(ref_venues),
                                   diff_bp=dist(mark_diff)),
            rate_vs_hyperliquid=dict(
                pairs=len(rate_pairs),
                same_sign=sum(same_sign) / len(same_sign) if same_sign else None,
                abs_diff_bp_per_hour=dist([abs(a - b) for a, b in rate_pairs], 1e4),
                correlation=pearson(rate_pairs)))
    return out


def pearson(pairs):
    if len(pairs) < 3:
        return None
    xs, ys = [a for a, _ in pairs], [b for _, b in pairs]
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None


# ---------------------------------------------------------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=("baseline", "benchmark", "observer", "all"))
    ap.add_argument("--dataset", required=True)
    a = ap.parse_args()
    d = Path(a.dataset)
    db = ro(d / "dataset.sqlite3")
    res = d / "results"
    res.mkdir(exist_ok=True)
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    stamp = dict(dataset_sha256=manifest["sha256"])
    base = baseline(db)
    todo = ("baseline", "benchmark", "observer") if a.what == "all" else (a.what,)
    for name in todo:
        result = dict(base if name == "baseline" else
                      benchmark(db, base) if name == "benchmark" else observer(db), **stamp)
        (res / (name + ".json")).write_text(
            json.dumps(result, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        print("wrote", res / (name + ".json"))


if __name__ == "__main__":
    main()
