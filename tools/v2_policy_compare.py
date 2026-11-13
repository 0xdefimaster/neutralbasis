"""STRICT_THRESHOLDS vs WORST_CASE_NONNEGATIVE on the SAME observations (Phase 10 item 4).

PAPER / research, offline. Both policies run the ESTIMATED engine on identical inputs;
the default policy is not changed. Per policy, asset and strategy:
  signals, entry success (positions with a fill / signals), simulated fills, closes,
  expected net at entry, entry stress estimate (net_min at entry: a model output, not a
  measured worst result), simulated realized PnL of closed positions, mark-to-market of
  open positions (fresh prices only; stale/missing counted apart), funding per evidence
  class, max unhedged seconds, rejections for missing data.

Usage: python -m tools.v2_policy_compare --out DIR --aligned D... --stream S...
"""
import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import v2_paper_replay as rp  # noqa: E402
from v2 import paper_engine, storage  # noqa: E402
from v2 import paper_ledger as L  # noqa: E402

POLICIES = ("STRICT_THRESHOLDS", "WORST_CASE_NONNEGATIVE")
MISSING_DATA = ("PRICE_HISTORY_MISSING", "STALE_QUOTE", "QUOTE_TIME_UNKNOWN", "BOOK_STALE",
                "BOOK_MISSING", "FUNDING_RATE_MISSING", "BETWEEN_TIERS_UNPRICED",
                "NO_QUOTE", "RFQ_RECORD_MISSING", "QUOTE_IN_FUTURE")


def mark_now(p, o, asset):
    """{side: mid} of each leg at the LAST observation, or None for a leg whose book is
    missing or older than its limit there (an older price is never used to value)."""
    from v2 import marketdata, rfq_paper

    out = {}
    for sd, lg in p["legs"].items():
        b = o.books.get((lg["venue"], asset))
        if not b or not b.get("bids") or not b.get("asks"):
            out[sd] = None
            continue
        if b.get("model") == "rfq_indicative":
            age = rfq_paper.quote_age(o.rfq_record(lg["venue"], asset), o.at)
            fresh = age is not None and age <= rfq_paper.MAX_QUOTE_AGE_S
        else:
            fresh = not marketdata.stale(b, lg.get("exchange_id"), o.at)
        out[sd] = (b["bids"][0][0] + b["asks"][0][0]) / 2 if fresh else None
    return out


def metrics(led, eng, obs, asset, strategy):
    """Phase 11: every PnL figure says what it is.
      expected_net_at_entry    the signals' expected net when they were made
      entry_stress_estimate    the signals' worst modelled scenario AT ENTRY (net_min);
                               a model output, never a measured result
      realized_closed_sim      closed positions: simulated realized PnL
      open_mark_to_market      open positions: realized parts so far + unrealized at the
                               LAST observation, valued only with fresh prices
      open_unvalued            open positions with a missing or stale price at the end
      funding_by_evidence      funding booked per evidence class (settled public rate,
                               account payment, published estimate, carried forward)"""
    ev = [e for e in eng.evaluations if e["asset"] == asset and e["strategy"] == strategy]
    sig = [s for s in led.signals if s["asset"] == asset and s["strategy"] == strategy]
    pos = [p for p in led.positions.values() if p["asset"] == asset and
           p["strategy"] == strategy]
    filled = [p for p in pos if any(l["entry_qty"] > L.EPS for l in p["legs"].values())]
    closed = [p for p in filled if p["state"] == L.CLOSED]
    open_ = [p for p in filled if p["state"] != L.CLOSED]
    last = obs[-1]
    realized_closed = sum(L.PaperLedger.pnl(p)["realized_total"] for p in closed)
    mtm, unvalued = 0.0, 0
    for p in open_:
        m = mark_now(p, last, asset)
        pn = L.PaperLedger.pnl(p, {k: v for k, v in m.items() if v is not None})
        if any(v is None for k, v in m.items() if p["legs"][k]["qty"] > L.EPS) or \
                pn["mark_to_market"] is None:
            unvalued += 1
            continue
        mtm += pn["realized_total"] + pn["mark_to_market"]
    fund = {k: sum(L.PaperLedger.pnl(p)["funding"][k] for p in filled)
            for k in L.FUNDING_KINDS}
    missing = collections.Counter(x["first"] for e in ev for x in e["sizes"]
                                  if x["first"] in MISSING_DATA)
    return dict(
        evaluations=len(ev), signals=len(sig), positions_filled=len(filled),
        entry_success=None if not sig else len(filled) / len(sig),
        simulated_fills=sum(len(l["fills"]) for p in pos for l in p["legs"].values()),
        verified_fills=sum(1 for p in pos for l in p["legs"].values() for f in l["fills"]
                           if f["verified"]),
        closed=len(closed),
        closed_reliable=sum(1 for p in closed if (p["reliability"] or [""])[-1] == "RELIABLE"),
        expected_net_at_entry=sum((s["expected"] or {}).get("net") or 0.0 for s in sig),
        entry_stress_estimate=sum((s["expected"] or {}).get("net_min") or 0.0 for s in sig),
        realized_closed_sim=realized_closed,
        open_positions=len(open_), open_mark_to_market=mtm, open_unvalued=unvalued,
        funding_by_evidence=fund,
        funding_account_verified=fund["ACCOUNT_PAYMENT_CONFIRMED"],
        max_unhedged_s=max([L.PaperLedger.unhedged(p, last.at)[0] for p in filled]
                           or [0.0]),
        missing_data_rejections=sum(missing.values()), missing_by_code=dict(missing),
        alt_gate_would_block=sum(1 for e in ev if e.get("alt_gate_blocks")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--aligned", nargs="*", default=[])
    ap.add_argument("--stream", nargs="*", default=[])
    a = ap.parse_args()
    out = storage.check_dir(a.out)
    out.mkdir(parents=True, exist_ok=True)
    obs, sources = rp.load(a.aligned, a.stream, rp.closes_db())
    result = dict(label="ESTIMATED PAPER policy comparison (simulated fills; default policy "
                  "unchanged)", sources=sources, observations=len(obs), policies={})
    for policy in POLICIES:
        res = {}
        for name, pairs, scen in (("natgas", rp.NATGAS_PAIRS, False),
                                  ("init_scenario", rp.INIT_PAIRS, True)):
            folder = out / policy / name
            if (folder / "ledger.jsonl").exists():
                raise SystemExit("%s exists: never append to an old ledger" % folder)
            led, eng, _ = rp.run(folder, obs, pairs, scen, entry_gate=policy)
            asset = pairs[0][0]
            res[name] = {s: metrics(led, eng, obs, asset, s)
                         for s in paper_engine.STRATEGIES}
        result["policies"][policy] = res
    (out / "policy_compare.json").write_text(json.dumps(result, indent=1, default=str),
                                             encoding="utf-8")
    for policy, res in result["policies"].items():
        for name, per in res.items():
            for s, m in per.items():
                print(policy, name, s, {k: (round(v, 3) if isinstance(v, float) else v)
                                        for k, v in m.items()
                                        if k not in ("missing_by_code", "funding_by_evidence")},
                      {k: round(v, 3) for k, v in m["funding_by_evidence"].items()})


if __name__ == "__main__":
    main()
