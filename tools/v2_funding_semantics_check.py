"""Funding semantics check (Phase 15 P0-C). Offline, recorded public data only.

  python tools/v2_funding_semantics_check.py [--out DIR]

Three checks, each a piece of EVIDENCE (not a proof) for v2/funding_semantics:

1. Boundary convergence (rescued Phase 8 stream, ~20 s samples): for every funding
   boundary inside the stream, the last published value before the boundary vs the
   venue's settled rate at that boundary (tradeXYZ NATGAS, HL native INIT). TxFlow and
   Variational have no settled history: only their behaviour is reported.
2. Variational fields: `funding.last` vs `funding.next`, and how long `next_timestamp`
   stays in the past after a boundary.
3. Cross-venue sign / scale (05:12 snapshot, 7-day settled histories): for every asset
   with settled history on >= 2 venues, per venue pair over common hours: Pearson
   correlation, sign agreement and the median |rate| ratio. An inverted sign would show
   as a strongly negative correlation; a unit error (percent vs fraction) as a ratio
   near 100 or 0.01. Different funding formulas (premium multipliers, caps) also move
   the ratio, so a flag is a SUSPICION for review, never a verdict.

Runs under the socket guard (any network attempt -> exit 3). Writes only under the
benchmark data folder on C: (D:/E: refused).
"""
import argparse
import collections
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import v2_funding_persistence as tp  # noqa: E402
from v2 import funding_persistence as fp  # noqa: E402
from v2 import paths, storage  # noqa: E402
from v2.control import netguard  # noqa: E402

HOUR = 3600.0
MIN_COMMON = 48
INVERSION_CORR = -0.3
SCALE_HI, SCALE_LO = 20.0, 0.05


def hm(t):
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t))


def stream_records(bench):
    for f in sorted(bench.glob("v2-phase8-rescue-*/stream-*/stream.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            yield json.loads(line)["rec"]


def ctx_rate(ans, name, coin):
    c = ans.get(name) or {}
    if not c.get("ok"):
        return None
    names = [m["name"] for m in c["data"][0]["universe"]]
    if coin not in names:
        return None
    return float(c["data"][1][names.index(coin)]["funding"])


def boundary_convergence(bench):
    samples = collections.defaultdict(list)     # venue -> [(t, rate per hour)]
    settled = collections.defaultdict(dict)     # venue -> {boundary ts: rate}
    for rec in stream_records(bench):
        ans = rec.get("answers") or {}
        for venue, name, coin in (("tradexyz", "xyz_ctx", "xyz:NATGAS"),
                                  ("hyperliquid_native", "native_ctx", "INIT")):
            r = ctx_rate(ans, name, coin)
            t = (ans.get(name) or {}).get("t1")
            if r is not None and t:
                samples[venue].append((t, r))
        tx = ans.get("txflow_ctx_NATGAS") or {}
        if tx.get("ok"):
            n = ((tx.get("data") or {}).get("data") or {}).get("nodeCtx") or {}
            if n.get("funding") is not None:
                samples["txflow"].append((tx.get("t1"), float(n["funding"]) / 100.0))
        for venue, key in (("tradexyz", "xyz_fh_NATGAS"), ("hyperliquid_native", "native_fh_INIT")):
            for x in ((rec.get("histories") or {}).get(key) or {}).get("data") or []:
                settled[venue][round(x["ts"] / HOUR) * HOUR] = x["rate"]
    out = []
    for venue, xs in samples.items():
        xs.sort()
        bounds = sorted({int(t // HOUR + 1) * HOUR for t, _ in xs})
        for b in bounds:
            before = [(t, r) for t, r in xs if b - HOUR < t < b]
            after = [(t, r) for t, r in xs if b <= t < b + HOUR]
            if not before or not after:
                continue
            t_last, r_last = before[-1]
            s = settled[venue].get(b)
            out.append(dict(
                venue=venue, boundary=hm(b), samples_before=len(before),
                distinct_before=len({r for _, r in before}),
                last_before_at=hm(t_last), seconds_before=round(b - t_last, 1),
                last_before=r_last, first_after=after[0][1], settled=s,
                rel_diff=None if s in (None, 0) else abs(r_last - s) / abs(s),
                verdict=("NO_SETTLED_HISTORY" if s is None else
                         "CONVERGES" if abs(r_last - s) / max(abs(s), 1e-12) < 0.01 else
                         "DIFFERS")))
    return out


def variational_fields(bench):
    tot = neq = 0
    lags = []
    changes = collections.defaultdict(list)
    for rec in stream_records(bench):
        v = (rec.get("answers") or {}).get("variational_stats") or {}
        if not v.get("ok"):
            continue
        for x in v["data"].get("listings") or []:
            fu = x.get("funding") or {}
            if fu.get("last") is None:
                continue
            tot += 1
            neq += fu.get("last") != fu.get("next")
            nt = fu.get("next_timestamp")
            if nt and float(nt) <= v["t1"]:
                lags.append(v["t1"] - float(nt))
            if x["ticker"] in ("NATGAS-PERP", "INIT-PERP"):
                rate = (fu.get("last") or {}).get("interval_rate")
                c = changes[x["ticker"]]
                if not c or c[-1][1] != rate:
                    c.append((v["t1"], rate))
    gaps = {k: [round(b[0] - a[0]) for a, b in zip(c, c[1:])] for k, c in changes.items()}
    return dict(listings=tot, last_ne_next=neq, next_timestamp_past_reads=len(lags),
                next_timestamp_max_lag_s=round(max(lags), 1) if lags else None,
                seconds_between_value_changes={k: dict(n=len(g), median=statistics.median(g)
                                                       if g else None) for k, g in
                                               gaps.items()})


def compare(sa, sb):
    """Two settled series {hour ts: rate per hour} of the same asset on two venues ->
    dict(hours, corr, sign_agreement, median_abs_ratio_a_over_b, flags), or None when
    they share fewer than MIN_COMMON hours."""
    common = sorted(set(sa) & set(sb))
    if len(common) < MIN_COMMON:
        return None
    xa = [sa[t] for t in common]
    xb = [sb[t] for t in common]
    nz = [(p, q) for p, q in zip(xa, xb) if p != 0 and q != 0]
    ratio = statistics.median(abs(p) / abs(q) for p, q in nz) if nz else None
    agree = sum(1 for p, q in nz if (p > 0) == (q > 0)) / len(nz) if nz else None
    c = fp.corr(xa, xb)
    flags = []
    if c is not None and c < INVERSION_CORR and (agree or 0) < 0.3:
        flags.append("SIGN_INVERSION_SUSPECT")
    if ratio is not None and (ratio > SCALE_HI or ratio < SCALE_LO):
        flags.append("SCALE_SUSPECT")
    return dict(hours=len(common), corr=c, sign_agreement=agree,
                median_abs_ratio_a_over_b=ratio, flags=flags)


def cross_venue(bench):
    at, hist = tp.snapshot_histories(bench)
    by_asset = collections.defaultdict(dict)
    for h in hist:
        if h["series"]:
            by_asset[h["asset"]][h["exchange_id"] or h["venue"]] = dict(h["series"])
    pairs = []
    for asset, vs in sorted(by_asset.items()):
        names = sorted(vs)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                x = compare(vs[a], vs[b])
                if x is not None:
                    pairs.append(dict(asset=asset, a=a, b=b, **x))
    per_venue = collections.defaultdict(lambda: dict(pairs=0, corr=[], agree=[], flags=0))
    for p in pairs:
        for v in (p["a"], p["b"]):
            s = per_venue[v]
            s["pairs"] += 1
            if p["corr"] is not None:
                s["corr"].append(p["corr"])
            if p["sign_agreement"] is not None:
                s["agree"].append(p["sign_agreement"])
            s["flags"] += bool(p["flags"])
    summary = {v: dict(pairs=s["pairs"], median_corr=statistics.median(s["corr"]) if s["corr"]
                       else None, median_sign_agreement=statistics.median(s["agree"])
                       if s["agree"] else None, pairs_flagged=s["flags"])
               for v, s in sorted(per_venue.items())}
    return dict(snapshot_at=hm(at), assets_on_2plus=sum(1 for v in by_asset.values()
                                                        if len(v) > 1),
                pairs=pairs, per_venue=summary)


def build(bench):
    return dict(generated_at=hm(time.time()), evidence="OBSERVED public market data "
                "(recorded); results are evidence for review, not proofs",
                boundary_convergence=boundary_convergence(bench),
                variational=variational_fields(bench), cross_venue=cross_venue(bench))


def md(r):
    L = ["# Funding semantics check (Phase 15 P0-C)", "", r["evidence"], "",
         "## Boundary convergence", "",
         "| venue | boundary | samples before | distinct | last before (s) | last value | "
         "settled | rel. diff | verdict |", "|---|---|---|---|---|---|---|---|---|"]
    for x in r["boundary_convergence"]:
        L.append("| %s | %s | %d | %d | %.0f | %.6g | %s | %s | %s |" % (
            x["venue"], x["boundary"], x["samples_before"], x["distinct_before"],
            x["seconds_before"], x["last_before"],
            "-" if x["settled"] is None else "%.6g" % x["settled"],
            "-" if x["rel_diff"] is None else "%.4f %%" % (x["rel_diff"] * 100), x["verdict"]))
    v = r["variational"]
    L += ["", "## Variational fields", "",
          "- listings read: %d; `funding.last` != `funding.next`: %d" % (
              v["listings"], v["last_ne_next"]),
          "- reads with `next_timestamp` already past: %d (max %s s)" % (
              v["next_timestamp_past_reads"], v["next_timestamp_max_lag_s"]),
          "- seconds between value changes: %s" % json.dumps(
              v["seconds_between_value_changes"])]
    c = r["cross_venue"]
    L += ["", "## Cross-venue settled sign / scale (snapshot %s)" % c["snapshot_at"], "",
          "| venue | pairs | median corr | median sign agreement | pairs flagged |",
          "|---|---|---|---|---|"]
    for k, s in c["per_venue"].items():
        L.append("| %s | %d | %s | %s | %d |" % (
            k, s["pairs"], "-" if s["median_corr"] is None else "%.2f" % s["median_corr"],
            "-" if s["median_sign_agreement"] is None else "%.2f" % s["median_sign_agreement"],
            s["pairs_flagged"]))
    fl = [p for p in c["pairs"] if p["flags"]]
    L += ["", "Flagged pairs (suspicion for review): %d of %d" % (len(fl), len(c["pairs"])), ""]
    for p in fl:
        L.append("- %s %s/%s: corr %s, sign agreement %s, |a|/|b| %s -> %s" % (
            p["asset"], p["a"], p["b"],
            "-" if p["corr"] is None else "%.2f" % p["corr"],
            "-" if p["sign_agreement"] is None else "%.2f" % p["sign_agreement"],
            "-" if p["median_abs_ratio_a_over_b"] is None else "%.3g"
            % p["median_abs_ratio_a_over_b"], ", ".join(p["flags"])))
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    bench = paths.bench()
    out = Path(a.out) if a.out else bench / "v2-paper-data" / "phase15-funding-semantics"
    tp.refuse_failing_disk(out)
    with netguard.offline() as g:
        r = build(bench)
    if len(g):
        print("network attempt refused: %d" % len(g))
        return 3
    out = storage.check_dir(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(r, indent=1), encoding="utf-8")
    (out / "report.md").write_text(md(r), encoding="utf-8")
    print(md(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
