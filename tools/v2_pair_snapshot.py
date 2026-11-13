"""NATGAS TxFlow <-> tradeXYZ at one observed moment, in four SEPARATE parts (PAPER).

Offline: it reads one saved observation (a stream cycle or an aligned folder) and never
trades. The four parts are never merged into one number:
  1. prices      each leg's bid / ask / mid / spread, source time, the pair's time skew
                 against the market's freshness limit
  2. funding     evidence per leg: TxFlow = published rate (RATE_PUBLISHED; account
                 payments ACCOUNT_PAYMENT_UNVERIFIED); tradeXYZ = published rate plus its
                 public settled history (MARKET_SETTLEMENT_CONFIRMED)
  3. depth       visible USD per side within 10 / 25 / 50 bp of the mid, and how much of
                 $1k / $2.5k / $5k / $10k a 10 bp IOC would fill (simulation)
  4. simulated   the ESTIMATED evaluator (funding strategy, both directions, each size):
                 status, expected net, entry stress estimate, assumptions, the strict
                 gate's reasons. ESTIMATED - never a verified trade.

Usage: python -m tools.v2_pair_snapshot --stream S.jsonl [--cycle -1] --out F.json
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wide_paper  # noqa: E402
from v2 import economics, estimated, gates, paper_exec, paper_obs, paths, storage  # noqa: E402
from v2 import funding_evidence as fe  # noqa: E402

LEGS = ("TxFlow", "tradeXYZ")
ASSET = "NATGAS"
SIZES = (1000.0, 2500.0, 5000.0, 10000.0)


def depth(book, mid):
    out = {}
    for side in ("bids", "asks"):
        lv = book.get(side) or []
        out[side] = {"%dbp" % b: sum(p * q for p, q in lv if abs(p / mid - 1) <= b * 1e-4)
                     for b in (10, 25, 50)}
        out[side]["visible_total"] = sum(p * q for p, q in lv)
        out[side]["levels"] = len(lv)
    return out


def snapshot(o):
    if any((v, ASSET) not in o.rows for v in LEGS):
        return dict(error="a leg is missing in this observation", missing=[
            v for v in LEGS if (v, ASSET) not in o.rows])
    prices, funding, dep = {}, {}, {}
    for v in LEGS:
        row, b = o.rows[(v, ASSET)], o.book(v, ASSET)
        mid = economics.mid(b)
        prices[v] = dict(bid=b["bids"][0][0], ask=b["asks"][0][0], mid=mid,
                         spread_bp=1e4 * (b["asks"][0][0] - b["bids"][0][0]) / mid,
                         book_source_time=b.get("received"),
                         leg_times=o.times.get((v, ASSET)),
                         time_basis=o.time_basis.get((v, ASSET)))
        hist = o.settled(v, ASSET, o.at) or []
        funding[v] = dict(
            published_rate_per_hour=row.get("rate"), per_8h=fe.per_8h(row.get("rate")),
            next_funding=row.get("next_funding"),
            evidence=fe.VENUE_BEST.get(row.get("exchange_id")),
            settled_points=len(hist), last_settled=hist[-1] if hist else None,
            account_payment="ACCOUNT_PAYMENT_UNVERIFIED (no authorized address)"
            if v == "TxFlow" else "n/a (no account; public settled history used)")
        fills = {}
        for size in SIZES:
            for side, buy in (("buy", True), ("sell", False)):
                f = paper_exec.clob_ioc(b, buy, size / mid, paper_exec.DEFAULT_SLIPPAGE_BP)
                fills["%s_%d" % (side, size)] = dict(
                    filled_share=f["filled"] / (size / mid), price=f["price"],
                    slippage_bp=f.get("slippage_bp"), label=f["label"])
        dep[v] = dict(depth_usd=depth(b, mid), ioc_10bp=fills)
    skew, lim, basis = o.pair_time(*LEGS, ASSET)
    sim = []
    for lv, sv in (LEGS, LEGS[::-1]):
        for size in SIZES:
            cfg = dict(wide_paper.WIDE_DEFAULTS, wide_size=size)
            g = gates.Gate(o, cfg)
            strict = g.evaluate(dict(asset=ASSET, long=lv, short=sv, strategy="funding"),
                                o.at, "funding")
            r = estimated.EstimatedEvaluator(g, cfg, o.rfq_record).evaluate(
                dict(asset=ASSET, long=lv, short=sv, strategy="funding"), o.at, "funding")
            sim.append(dict(
                long=lv, short=sv, size=size, status=r["status"], first=r.get("first"),
                expected_net=r.get("net"),
                entry_stress_estimate=(r.get("uncertainty") or {}).get("net_min"),
                carry_8h=r.get("carry_8h"), breakeven_h=r.get("breakeven_h"),
                components=r.get("components"),
                assumptions=[a.get("label") for a in r.get("assumptions") or []],
                strict_reasons=[x["code"] for x in strict.get("reasons") or []],
                label="ESTIMATED (simulated on observed data; not a verified trade)"))
    return dict(
        observation=o.origin, at=o.at, asset=ASSET, legs=LEGS,
        part1_prices=dict(legs=prices, pair_skew_s=skew, freshness_limit_s=lim,
                          freshness_basis=basis,
                          aligned=skew is not None and lim is not None and skew <= lim),
        part2_funding=funding, part3_depth=dep, part4_simulated=sim,
        never="no verified execution; no account funding proof; strict gate unchanged")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream")
    ap.add_argument("--aligned")
    ap.add_argument("--cycle", type=int, default=-1)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    closes = paths.load_closes() if paths.closes_available() else None
    if a.stream:
        obs = paper_obs.from_stream(a.stream, closes)[0]
        o = obs[a.cycle]
    else:
        o = paper_obs.from_aligned(a.aligned, closes)
    r = snapshot(o)
    storage.check_dir(Path(a.out).parent)
    Path(a.out).write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
    print(json.dumps(r, indent=1, default=str)[:6000])


if __name__ == "__main__":
    main()
