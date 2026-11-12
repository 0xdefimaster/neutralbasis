"""Old 46-hour pilot (2026-11-10..12) against the V2 rules and the V2 cost benchmark.

PAPER / research; offline. Inputs (read-only): the frozen V2-0 dataset (pilot trades)
and the phase 4 evaluation answers (tools/v2_phase4_eval). Output: for each pilot
trade, the first V2 rule that would refuse it today, and the round-trip cost the pilot
paid against the cost V2 prices on real books for its candidates.

Usage: python -m tools.v2_compare_pilot --dataset DIR --phase4 DIR
"""
import argparse
import gzip
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2 import policy  # noqa: E402

FEE_UNKNOWN_VENUES = ("RISEx",)
PUBLISHED_ONLY = ("TxFlow", "Variational")
BREAKEVEN_MAX_H = 24.0


def v2_verdict(t, pilot_cost_bp):
    """First V2 rule that refuses this pilot trade (or None)."""
    legs = [v for v in (t["long"], t["short"]) if v]
    if len(legs) < 2:
        return "LEG_RECOVERY (one leg never filled; never a win)"
    out = [v for v in legs if policy.exchange_id(v) is None]
    if out:
        return "OUT_OF_V2_SCOPE (%s)" % ", ".join(out)
    unknown = [v for v in legs if v in FEE_UNKNOWN_VENUES]
    if unknown:
        return "FEE_UNKNOWN (%s)" % ", ".join(unknown)
    if t["strategy"] == "funding":
        pub = [v for v in legs if v in PUBLISHED_ONLY]
        if pub:
            return "SETTLEMENT_EVIDENCE_MISSING (%s)" % ", ".join(pub)
        e = t.get("entry") or {}
        if e.get("expected_funding") and e.get("horizon_hours"):
            hourly = e["expected_funding"] / e["horizon_hours"]
            be = (pilot_cost_bp / 1e4 * (t["size"] or 1000)) / hourly if hourly > 0 else None
            if be is None or be > BREAKEVEN_MAX_H:
                return "BREAKEVEN (%.0f h > 24 h at the measured cost)" % (be or float("inf"))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--phase4", required=True)
    a = ap.parse_args()
    from tools.v2_evidence import baseline, ro, trade_benchmark

    db = ro(Path(a.dataset) / "dataset.sqlite3")
    base = baseline(db)
    tb = trade_benchmark(base)
    pilot_cost = tb["funding_two_leg"]["round_trip_cost_bp"]["p50"]
    rows = []
    for t in base["trades"]:
        x = dict(asset=t["asset"], strategy=t["strategy"], long=t["long"], short=t["short"],
                 net=round(t["net"], 2), size=t["size"], entry=t["entry_parsed"],
                 outcome=t["outcome"])
        x["v2"] = v2_verdict(x, pilot_cost) or "would be evaluated (identity, books, net)"
        rows.append(x)
    # V2 costs on real books (phase 4 answers, replayed).
    import wide_paper
    from tools.v2_phase4_eval import Provider, Replay, pilot_closes, PILOT_DB
    from v2 import gates
    from v2.collector import Collector

    blob = json.loads(gzip.decompress((Path(a.phase4) / "answers.json.gz").read_bytes()))
    at = blob["at"]
    col = Collector(transport=Replay(blob["answers"]), clock=lambda: at)
    v2rows = col.refresh()
    closes, _ = pilot_closes(PILOT_DB)
    cfg = dict(wide_paper.WIDE_DEFAULTS)
    prov = Provider(col, closes, True)
    costs = []
    from v2 import economics
    for c in wide_paper.rank(v2rows, cfg, at):
        lb, sb = prov.book(c["long"], c["asset"]), prov.book(c["short"], c["asset"])
        r = prov.rows_by()
        cc, why = economics.round_trip(lb, sb, cfg["wide_size"], r[(c["long"], c["asset"])]["fee"],
                                       r[(c["short"], c["asset"])]["fee"])
        if cc:
            costs.append(dict(pair="%s~%s" % tuple(sorted((c["long"], c["short"]))),
                              bp=cc["total"] / cfg["wide_size"] * 1e4,
                              fees_bp=cc["fees"] / cfg["wide_size"] * 1e4))
    bps = [x["bp"] for x in costs]
    by_pair = {}
    for x in costs:
        by_pair.setdefault(x["pair"], []).append(x["bp"])
    out = dict(
        pilot=dict(trades=len(rows), net=round(sum(r["net"] for r in rows), 2),
                   realized_round_trip_bp_p50=round(pilot_cost, 2)),
        verdicts=rows,
        v2_cost_bp=dict(n=len(bps), p10=round(sorted(bps)[len(bps) // 10], 2) if bps else None,
                        p50=round(statistics.median(bps), 2) if bps else None,
                        p90=round(sorted(bps)[int(len(bps) * 0.9)], 2) if bps else None,
                        by_pair={k: round(statistics.median(v), 2) for k, v in
                                 sorted(by_pair.items())}),
        label="Pilot trades SIMULATED; V2 costs on OBSERVED books of 2026-11-12 05:12")
    (Path(a.phase4) / "pilot_vs_v2.json").write_text(json.dumps(out, indent=1, default=str),
                                                     encoding="utf-8")
    print(json.dumps(dict(pilot=out["pilot"], v2_cost_bp=out["v2_cost_bp"]), indent=1))
    for r in rows:
        print(r["asset"], r["strategy"], r["long"], r["short"], r["net"], "->", r["v2"])


if __name__ == "__main__":
    main()
