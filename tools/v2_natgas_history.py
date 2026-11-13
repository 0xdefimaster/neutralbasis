r"""NATGAS TxFlow <-> tradeXYZ over every recorded observation (Phase 13.7). Offline.

For each observation, in time order, the four separate parts of tools.v2_pair_snapshot
(prices, funding evidence, depth, simulated ESTIMATED economics) for both directions and
four sizes. Each decision uses ONLY that observation (no look-ahead). Later observations
are used only AFTERWARDS, to measure what happened after a positive signal (carry,
price gap, expected net at the next observations). Nothing is a fill.

Usage: python -m tools.v2_natgas_history --aligned D... --stream S... --out F.json
"""
import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import v2_pair_snapshot as ps  # noqa: E402
from v2 import paper_obs, paths, references, storage  # noqa: E402


def timeline(observations):
    pts = []
    for o in sorted(observations, key=lambda x: x.at):
        r = ps.snapshot(o)
        if "error" in r:
            pts.append(dict(at=o.at, missing=r["missing"]))
            continue
        p = r["part1_prices"]
        gap = 1e4 * (p["legs"]["tradeXYZ"]["mid"] / p["legs"]["TxFlow"]["mid"] - 1)
        rows = {(s["long"], int(s["size"])): s for s in r["part4_simulated"]}
        pts.append(dict(at=o.at, aligned=p["aligned"], skew_s=p["pair_skew_s"], gap_bp=gap,
                        txflow_rate_h=r["part2_funding"]["TxFlow"]["published_rate_per_hour"],
                        xyz_rate_h=r["part2_funding"]["tradeXYZ"]["published_rate_per_hour"],
                        sim={"%s|%d" % k: {x: v[x] for x in (
                            "status", "first", "expected_net", "entry_stress_estimate",
                            "entry_cost", "exit_cost", "carry_8h", "breakeven_h")}
                             for k, v in rows.items()}))
    return pts


def after_signal(pts, key, horizon=5):
    """For every point where `key` has expected net > 0: what the next observations
    show (evaluation only, never part of the decision)."""
    out = []
    for i, p in enumerate(pts):
        s = (p.get("sim") or {}).get(key)
        if not s or (s["expected_net"] or 0) <= 0:
            continue
        nxt = [q for q in pts[i + 1:i + 1 + horizon] if q.get("sim")]
        out.append(dict(at=p["at"], expected_net=s["expected_net"],
                        stress=s["entry_stress_estimate"], status=s["status"],
                        gap_bp=p["gap_bp"],
                        next=[dict(dt_s=q["at"] - p["at"], gap_change_bp=q["gap_bp"] - p["gap_bp"],
                                   carry_8h=q["sim"][key]["carry_8h"],
                                   expected_net=q["sim"][key]["expected_net"]) for q in nxt]))
    return out


def summarize(pts):
    keys = sorted({k for p in pts for k in (p.get("sim") or {})})
    out = {}
    for k in keys:
        xs = [p["sim"][k] for p in pts if p.get("sim") and k in p["sim"]]
        out[k] = dict(
            points=len(xs),
            expected_positive=sum(1 for x in xs if (x["expected_net"] or 0) > 0),
            stress_positive=sum(1 for x in xs if (x["entry_stress_estimate"] or 0) > 0),
            entry_signals=sum(1 for x in xs if x["status"] == "ESTIMATED_ENTRY_SIGNAL"),
            first_rejection=dict(collections.Counter(x["first"] or "-" for x in xs)
                                 .most_common(5)),
            entry_cost_median=sorted(x["entry_cost"] for x in xs if x["entry_cost"])[
                len(xs) // 2] if any(x["entry_cost"] for x in xs) else None,
            exit_cost_median=sorted(x["exit_cost"] for x in xs if x["exit_cost"])[
                len(xs) // 2] if any(x["exit_cost"] for x in xs) else None)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--aligned", nargs="*", default=[])
    ap.add_argument("--stream", nargs="*", default=[])
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    closes = paths.load_closes() if paths.closes_available() else None
    obs = [paper_obs.from_aligned(f, closes) for f in a.aligned]
    for s in a.stream:
        obs += paper_obs.from_stream(s, closes)[0]
    pts = timeline(obs)
    ref = references.classify("txflow", "tradexyz", "NATGAS")
    rep = dict(observations=len(pts), first=pts[0]["at"], last=pts[-1]["at"],
               aligned_share=sum(1 for p in pts if p.get("aligned")) / len(pts),
               reference=dict(cls=ref["cls"], unknown=ref["unknown"],
                              different=ref["different"], hedge_ok=ref["hedge_ok"]),
               account_funding="ACCOUNT_PAYMENT_UNVERIFIED (no authorized TxFlow address)",
               funding_times="TxFlow: published next payment, hourly; tradeXYZ: hourly "
                             "on the hour (from its settled history times)",
               summary=summarize(pts),
               after_positive={k: after_signal(pts, k) for k in (
                   "TxFlow|1000", "tradeXYZ|1000")},
               points=pts, label="ESTIMATED research; no fill; no look-ahead in decisions")
    storage.check_dir(Path(a.out).parent)
    Path(a.out).write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in rep.items() if k not in ("points", "after_positive")},
                     indent=1, default=str))


if __name__ == "__main__":
    main()
