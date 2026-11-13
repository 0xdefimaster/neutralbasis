"""Market-data contract audit (Phase 15 P1). Offline, recorded public data only.

  python tools/v2_contract_audit.py [--out DIR]

1. Every market record of the 05:12 snapshot (Collector replay, identity verified) ->
   contract v2 (`normalize_market`), validated; field coverage and problems per venue.
2. Every REST answer of the rescued Phase 8 stream -> contract (`normalize_answer`),
   validated: source-vs-receipt skew, unit suspicion, failures.
3. Fake-signal guards: what v2.identity refused (PRICE_MISMATCH, MULTIPLIER_UNKNOWN,
   ...) and, for every verified multi-venue group, the spread of per-unit prices across
   venues (a scale / multiplier error would show as a ratio far from 1).

Socket-guarded (exit 3 on any network attempt). Writes only under the benchmark data
folder on C: (D:/E: refused). HISTORICAL / OBSERVED data; no MOCK.
"""
import argparse
import collections
import gzip
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import v2_funding_persistence as tp  # noqa: E402
from v2 import paths, storage  # noqa: E402
from v2.control import contract, netguard  # noqa: E402


def hm(t):
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t))


def snapshot_rows(bench):
    from tools.v2_phase4_eval import Replay
    from v2.collector import Collector

    blob = json.loads(gzip.decompress((bench / "v2-phase4-2026-11-12" / "answers.json.gz")
                                      .read_bytes()))
    at = blob["at"]
    col = Collector(transport=Replay(blob["answers"]), clock=lambda: at)
    col.refresh()
    rec_t = col.venue_received()
    rows = []
    for xid, recs in sorted(col.records.items()):
        for r in recs:
            rows.append((r, contract.normalize_market(
                r, rec_t.get(xid, at), evidence="HISTORICAL",
                raw_ref="v2-phase4-2026-11-12/answers.json.gz")))
    return at, col, rows


def market_audit(rows):
    by = collections.defaultdict(list)
    for _, c in rows:
        by[c["exchange_id"]].append(c)
    out = {}
    for xid, cs in sorted(by.items()):
        probs = collections.Counter(p for c in cs for p in contract.validate(c))
        warn = collections.Counter(w for c in cs for w in (c["funding_evidence"] or {})
                                   .get("warnings") or [])
        cov = contract.coverage(cs)
        out[xid] = dict(markets=len(cs), problems=dict(probs),
                        funding_warnings=dict(warn.most_common()),
                        rate_kind=collections.Counter(
                            (c["funding_evidence"] or {}).get("rate_kind") for c in cs)
                        .most_common(1)[0][0],
                        coverage={k: round(v, 3) for k, v in cov.items()
                                  if k in ("canonical_underlying_id", "received_ts",
                                           "monotonic_receive_ts", "source_ts", "multiplier",
                                           "funding_evidence")},
                        funding_rate_known=round(sum(
                            1 for c in cs if (c["funding_evidence"] or {}).get(
                                "rate_per_hour") is not None) / len(cs), 3))
    return out


def stream_audit(bench):
    by = collections.defaultdict(lambda: dict(answers=0, problems=collections.Counter(),
                                              failed=0, skew=[]))
    n = 0
    for f in sorted(bench.glob("v2-phase8-rescue-*/stream-*/stream.jsonl")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines()):
            rec = json.loads(line)["rec"]
            for name, ans in (rec.get("answers") or {}).items():
                c = contract.normalize_answer(name, ans, raw_ref="%s#%d" % (f.name, i),
                                              evidence="OBSERVED")
                s = by[c["exchange_id"] or "?"]
                s["answers"] += 1
                n += 1
                if c["evidence"] == "FAILED":
                    s["failed"] += 1
                for p in contract.validate(c):
                    s["problems"][p] += 1
                if c["source_ts"] is not None and c["received_ts"] is not None:
                    s["skew"].append(c["received_ts"] - c["source_ts"])
    out = {}
    for xid, s in sorted(by.items()):
        sk = s["skew"]
        out[xid] = dict(answers=s["answers"], failed=s["failed"],
                        problems=dict(s["problems"]),
                        receive_minus_source_s=dict(
                            n=len(sk), median=statistics.median(sk) if sk else None,
                            min=min(sk) if sk else None, max=max(sk) if sk else None,
                            basis="APPARENT: includes local clock offset (UNVERIFIED)"))
    return dict(answers=n, by_venue=out)


def guard_audit(col, rows):
    refused = collections.defaultdict(collections.Counter)
    for r, _ in rows:
        inst = r["instrument"]
        if inst.reason:
            refused[inst.exchange_id][inst.reason] += 1
    spreads = []
    by_u = collections.defaultdict(list)
    for r, _ in rows:
        inst = r["instrument"]
        if inst.underlying_id and inst.reason is None and inst.unit_price:
            by_u[inst.underlying_id].append((inst.exchange_id, inst.unit_price))
    for uid, xs in sorted(by_u.items()):
        if len({x for x, _ in xs}) < 2:
            continue
        ps = [p for _, p in xs]
        spreads.append(dict(underlying=uid, venues=len(xs), max_over_min=max(ps) / min(ps)))
    ratios = [s["max_over_min"] for s in spreads]
    return dict(refused_by_venue={k: dict(v.most_common()) for k, v in sorted(refused.items())},
                verified_groups=len(spreads),
                unit_price_max_over_min=dict(
                    median=statistics.median(ratios) if ratios else None,
                    max=max(ratios) if ratios else None,
                    above_1_5x=sum(1 for x in ratios if x > 1.5)),
                widest=sorted(spreads, key=lambda s: -s["max_over_min"])[:5])


def build(bench):
    at, col, rows = snapshot_rows(bench)
    return dict(generated_at=hm(time.time()), schema_version=contract.SCHEMA_VERSION,
                evidence="HISTORICAL snapshot + OBSERVED stream; validation never repairs",
                snapshot_at=hm(at), markets=len(rows), market_audit=market_audit(rows),
                stream_audit=stream_audit(bench), guards=guard_audit(col, rows))


def md(r):
    L = ["# Market-data contract audit (%s)" % r["schema_version"], "", r["evidence"], "",
         "## Snapshot markets (%s, %d markets)" % (r["snapshot_at"], r["markets"]), "",
         "| venue | markets | rate kind | funding rate known | underlying id | problems | "
         "top funding warnings |", "|---|---|---|---|---|---|---|"]
    for x, a in r["market_audit"].items():
        L.append("| %s | %d | %s | %.0f %% | %.0f %% | %s | %s |" % (
            x, a["markets"], a["rate_kind"], a["funding_rate_known"] * 100,
            a["coverage"]["canonical_underlying_id"] * 100,
            ", ".join("%s %d" % kv for kv in a["problems"].items()) or "none",
            ", ".join("%s %d" % kv for kv in list(a["funding_warnings"].items())[:3])))
    s = r["stream_audit"]
    L += ["", "## Phase 8 stream answers (%d)" % s["answers"], "",
          "| venue | answers | failed | problems | receive - source (median, min) |",
          "|---|---|---|---|---|"]
    for x, a in s["by_venue"].items():
        k = a["receive_minus_source_s"]
        L.append("| %s | %d | %d | %s | %s |" % (
            x, a["answers"], a["failed"],
            ", ".join("%s %d" % kv for kv in a["problems"].items()) or "none",
            "-" if k["median"] is None else "%.2f s, %.2f s" % (k["median"], k["min"])))
    g = r["guards"]
    L += ["", "## Fake-signal guards", "",
          "Refused by identity (never a strategy row): " + json.dumps(g["refused_by_venue"]),
          "", "Verified multi-venue groups: %d; per-unit price max/min median %.4f, max %.4f, "
          "groups above 1.5x: %d" % (g["verified_groups"],
                                     g["unit_price_max_over_min"]["median"] or 0,
                                     g["unit_price_max_over_min"]["max"] or 0,
                                     g["unit_price_max_over_min"]["above_1_5x"])]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    bench = paths.bench()
    out = Path(a.out) if a.out else bench / "v2-paper-data" / "phase15-contract-audit"
    tp.refuse_failing_disk(out)
    with netguard.offline() as g:
        r = build(bench)
    if len(g):
        print("network attempt refused: %d" % len(g))
        return 3
    out = storage.check_dir(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
    (out / "report.md").write_text(md(r), encoding="utf-8")
    print(md(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
