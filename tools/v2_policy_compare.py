"""STRICT_THRESHOLDS vs WORST_CASE_NONNEGATIVE on the SAME observations (Phase 10 item 4).

PAPER / research, offline. Both policies run the ESTIMATED engine on identical inputs;
the default policy is not changed. Per policy, asset and strategy:
  signals, entry success (positions with a fill / signals), simulated fills, closes,
  estimated net (realized closed + mark-to-market open), worst modelled PnL (sum of the
  entered signals' net_min), max unhedged seconds, rejections for missing data.

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


def metrics(led, eng, obs, asset, strategy):
    ev = [e for e in eng.evaluations if e["asset"] == asset and e["strategy"] == strategy]
    sig = [s for s in led.signals if s["asset"] == asset and s["strategy"] == strategy]
    pos = [p for p in led.positions.values() if p["asset"] == asset and
           p["strategy"] == strategy]
    filled = [p for p in pos if any(l["entry_qty"] > L.EPS for l in p["legs"].values())]
    closed = [p for p in filled if p["state"] == L.CLOSED]
    last = rp.last_mids(obs, [(asset, p["legs"]["long"]["venue"], p["legs"]["short"]["venue"])
                              for p in filled]) if filled else {}
    est = 0.0
    for p in filled:
        mids = {}
        for o in obs:
            for sd, lg in p["legs"].items():
                b = o.books.get((lg["venue"], asset))
                if b and b.get("bids") and b.get("asks"):
                    mids[sd] = (b["bids"][0][0] + b["asks"][0][0]) / 2
        pn = L.PaperLedger.pnl(p, mids)
        est += pn["realized_total"] + (pn["mark_to_market"] or 0.0)
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
        estimated_net=est,
        worst_modelled=sum((s["expected"] or {}).get("net_min") or 0.0 for s in sig),
        max_unhedged_s=max([L.PaperLedger.unhedged(p, obs[-1].at)[0] for p in filled]
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
                                        for k, v in m.items() if k != "missing_by_code"})


if __name__ == "__main__":
    main()
