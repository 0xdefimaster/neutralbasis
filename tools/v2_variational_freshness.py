"""Is a 2-second fresh Variational price possible with the public API? (offline analysis)

PAPER / research. Reads saved streams only (no network). For each followed listing:
  * update interval: gaps between DISTINCT quotes.updated_at values (median/P95/max)
  * quote age at read: receive time - updated_at (+ the measured clock offset)
  * share of reads whose quote was <= 2 s old
  * best case if one could read at any instant: the expected share of time a quote is
    <= 2 s old is sum(min(gap, 2)) / sum(gap) over the observed gaps
  * repeats: reads that returned an already-seen updated_at (venue cache)
  * HTTP 429 share of Variational reads
Docs (docs.variational.io/technical-documentation/api, read 2026-11-13): "The bid/ask
price may be cached for up to 600 seconds"; 10 requests / 10 s per IP, 1000 / min global;
no WebSocket; "The trading API is still in development, and is not yet available to any
users" -> no firm quote.

Usage: python -m tools.v2_variational_freshness --stream S.jsonl ... --clock-offset 0.27
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2 import rfq_paper, storage  # noqa: E402

GATE_S = 2.0


def pct(xs, q):
    xs = sorted(xs)
    return None if not xs else xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def analyse(streams, offset_s=0.0):
    reads = {}
    calls = failed = r429 = 0
    for s in streams:
        recs, rep = storage.read_chain(s)
        for r in recs:
            if r.get("kind") != "cycle":
                continue
            a = r["answers"].get("variational_stats")
            if not a:
                continue
            calls += 1
            if not a.get("ok"):
                failed += 1
                r429 += "429" in str(a.get("error"))
                continue
            for x in a["data"].get("listings", []):
                u = rfq_paper.ts((x.get("quotes") or {}).get("updated_at"))
                if u is not None:
                    reads.setdefault(x["ticker"], []).append((a["t1"] + offset_s, u, str(s)))
    out = {}
    for tk, rs in sorted(reads.items()):
        ups = sorted({u for _, u, _ in rs})
        gaps = []           # between distinct quotes WITHIN one stream (no cross-file gap)
        for src in sorted({x[2] for x in rs}):
            us = sorted({u for _, u, s_ in rs if s_ == src})
            gaps += [b - a for a, b in zip(us, us[1:])]
        ages = [t - u for t, u, _ in rs]
        seen, rep = set(), 0
        for _, u, _ in rs:
            rep += u in seen
            seen.add(u)
        best = (sum(min(g, GATE_S) for g in gaps) / sum(gaps)) if gaps else None
        out[tk] = dict(
            reads=len(rs), distinct_quotes=len(ups), repeated_reads=rep,
            update_interval_s=None if not gaps else dict(
                median=statistics.median(gaps), p95=pct(gaps, 0.95), max=max(gaps),
                min=min(gaps)),
            age_at_read_s=dict(min=min(ages), median=statistics.median(ages),
                               p95=pct(ages, 0.95), max=max(ages)),
            share_reads_age_le_2s=sum(1 for x in ages if x <= GATE_S) / len(ages),
            best_case_share_of_time_le_2s=best,
            negative_ages=sum(1 for x in ages if x < 0))
    return dict(listings=out, variational_calls=calls, failed=failed, http_429=r429,
                clock_offset_s=offset_s, gate_s=GATE_S,
                docs=dict(cache="bid/ask may be cached for up to 600 s",
                          websocket="none documented",
                          trading_api="not yet available to any users"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", nargs="+", required=True)
    ap.add_argument("--clock-offset", type=float, default=0.0)
    ap.add_argument("--out")
    a = ap.parse_args()
    r = analyse(a.stream, a.clock_offset)
    if a.out:
        storage.check_dir(Path(a.out).parent)
        Path(a.out).write_text(json.dumps(r, indent=1), encoding="utf-8")
    print(json.dumps(r, indent=1))


if __name__ == "__main__":
    main()
