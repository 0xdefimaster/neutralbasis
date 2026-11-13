"""Old vs new funding classification on the same recorded data (Phase 15 §3).
Offline; the result is a REPORT, not an entry rule.

  python tools/v2_funding_class_compare.py [--out DIR]

Old: an ESTIMATED entry signal = net > 0 with the published rates held for 24 h.
New: v2.funding_expectation classes (DISCOVERED / BASELINE / PERSISTENCE / ADVERSE
POSITIVE, INSUFFICIENT_HISTORY) and the profit label (never VERIFIED).

A. Every unique ESTIMATED replay entry (phase9-replays stories) re-evaluated at its own
   signal time with only data at or before it. Published samples of TxFlow and
   Variational are the stream's values up to that time; tradeXYZ uses its 90-day
   settled history cut at that time.
B. Every stream cycle and every NATGAS venue direction: the CARRY CASH class (before
   costs; costs exist only where the evaluator ran) under the three models.
C. The 05:12 scanner snapshot classes (from tools/v2_opportunity_scan.py).
Outcomes: whether a signal was a FALSE POSITIVE needs the PAID funding. tradeXYZ's is
public; TxFlow's and Variational's are not (no account evidence): those outcomes stay
UNKNOWN and are never reported as realized profit or loss.
"""
import argparse
import bisect
import collections
import glob
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import v2_funding_persistence as tp  # noqa: E402
from v2 import funding_expectation as fx  # noqa: E402
from v2 import paths, storage  # noqa: E402
from v2.control import netguard  # noqa: E402

HOUR = 3600.0
VENUE = {"TxFlow": "txflow", "tradeXYZ": "tradexyz", "Variational": "variational"}


def series(bench):
    """Per venue: sorted [(t, rate per hour, next payment ts or None)] for NATGAS."""
    out = collections.defaultdict(list)
    for f in sorted(bench.glob("v2-phase8-rescue-*/stream-*/stream.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            A = (json.loads(line)["rec"].get("answers") or {})
            c = A.get("txflow_ctx_NATGAS") or {}
            if c.get("ok"):
                n = ((c.get("data") or {}).get("data") or {}).get("nodeCtx") or {}
                if n.get("funding") is not None:
                    nx = n.get("nextFundingTime")
                    out["txflow"].append((c["t1"], float(n["funding"]) / 100.0,
                                          nx / 1000.0 if nx else None))
            x = A.get("xyz_ctx") or {}
            if x.get("ok"):
                names = [m["name"] for m in x["data"][0]["universe"]]
                if "xyz:NATGAS" in names:
                    out["tradexyz"].append((x["t1"], float(x["data"][1][names.index(
                        "xyz:NATGAS")]["funding"]), (int(x["t1"] // HOUR) + 1) * HOUR))
            v = A.get("variational_stats") or {}
            if v.get("ok"):
                for L in v["data"].get("listings") or []:
                    if L.get("ticker") == "NATGAS-PERP":
                        fu = L.get("funding") or {}
                        iv = float(fu.get("interval_seconds") or 0)
                        r = (fu.get("last") or {}).get("interval_rate")
                        nt = fu.get("next_timestamp")
                        if iv and r is not None:
                            out["variational"].append((v["t1"], float(r) / (iv / HOUR),
                                                       float(nt) if nt else None))
    return {k: sorted(v) for k, v in out.items()}


def leg_at(ser, xyz_hist, xid, t, side):
    xs = ser.get(xid) or []
    i = bisect.bisect_right([p[0] for p in xs], t)
    if i == 0:
        return None
    cur, nxt = xs[i - 1][1], xs[i - 1][2]
    if xid == "tradexyz":
        return fx.leg(xid, cur, t, next_ts=nxt, side=side,
                      fit_override=fx.fit([(a, b) for a, b in xyz_hist if a <= t]))
    return fx.leg(xid, cur, t, next_ts=nxt, side=side,
                  samples=[p[1] for p in xs[:i]])


def replay_entries(bench, ser, xyz_hist):
    seen, out = set(), []
    for f in sorted(glob.glob(str(bench / "v2-paper-data" / "phase9-replays" / "*" / "*" /
                                  "stories.json"))):
        for s in json.loads(Path(f).read_text(encoding="utf-8")):
            k = (round(s["signal_at"], 3), s["long"], s["short"], s["size"])
            if k in seen or s.get("strategy") != "funding":
                continue
            seen.add(k)
            lx, sx = VENUE.get(s["long"]), VENUE.get(s["short"])
            lo = leg_at(ser, xyz_hist, lx, s["signal_at"], "long")
            sh = leg_at(ser, xyz_hist, sx, s["signal_at"], "short")
            if lo is None or sh is None:
                # e.g. the 07:36 aligned-snapshot signals: before the stream started, so
                # no published samples exist at their time (not re-evaluated, not guessed)
                out.append(dict(entry=k, signal_at=tp.hm(s["signal_at"]), long=s["long"],
                                short=s["short"], status="SIGNAL_OUTSIDE_RECORDED_STREAM"))
                continue
            r = fx.expect(lo, sh, s["size"], 24)
            exp = s["expected"]
            nets = fx.model_nets(r, exp["net"], exp["components"]["funding"])
            cls = fx.classify(True, nets, r["legs"], "ESTIMATED_CANDIDATE")
            out.append(dict(
                signal_at=tp.hm(s["signal_at"]), asset=s["asset"], long=s["long"],
                short=s["short"], size=s["size"],
                old=dict(signal="ESTIMATED_ENTRY", net=exp["net"], net_min=exp["net_min"]),
                new=dict(nets=nets, classes=cls["classes"], highest=cls["highest"],
                         profit_label=cls["profit_label"], confidence=r["confidence"],
                         flags=r["flags"]),
                outcome="UNKNOWN: a %s leg's paid funding is not observable" % ", ".join(
                    v for v in (s["long"], s["short"]) if v in ("TxFlow", "Variational"))
                if {"TxFlow", "Variational"} & {s["long"], s["short"]} else "see settled"))
    return out


def cycles(ser, xyz_hist):
    """Per stream cycle and NATGAS direction: carry CASH class before costs."""
    times = [p[0] for p in ser.get("txflow") or []]
    counts = collections.defaultdict(collections.Counter)
    venues = [v for v in ("txflow", "tradexyz", "variational") if ser.get(v)]
    for t in times:
        for a in venues:
            for b in venues:
                if a == b:
                    continue
                lo, sh = leg_at(ser, xyz_hist, a, t, "long"), leg_at(ser, xyz_hist, b, t,
                                                                    "short")
                if lo is None or sh is None:
                    continue
                r = fx.expect(lo, sh, 1000.0, 24)
                cash = {m: x["cash"] for m, x in r["models"].items()}
                c = counts["%s->%s" % (a, b)]
                c["cycles"] += 1
                for m in ("baseline", "persistence", "adverse"):
                    if cash[m] is None:
                        c[m + "_unknown"] += 1
                    elif cash[m] > 0:
                        c[m + "_positive"] += 1
                if cash["baseline"] and cash["baseline"] > 0 and not (
                        cash["persistence"] and cash["persistence"] > 0):
                    c["baseline_only"] += 1
    return {k: dict(v) for k, v in counts.items()}


def build(bench):
    ser = series(bench)
    xyz_hist = tp.aligned_histories(bench)["tradexyz NATGAS (90d)"]
    scan = json.loads(paths.bench("v2-paper-data", "phase15-scan-0512",
                                  "opportunity_report.json").read_text(encoding="utf-8"))
    return dict(generated_at=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                evidence="OBSERVED recorded data; every model result is ESTIMATED",
                stream_samples={k: len(v) for k, v in ser.items()},
                replay_entries=replay_entries(bench, ser, xyz_hist),
                cycles_carry_cash=cycles(ser, xyz_hist),
                scanner_0512=scan["ECONOMIC"]["funding_models"])


def md(r):
    L = ["# Old vs new funding classification", "", r["evidence"], "",
         "## A. ESTIMATED replay entries (old: all entered on baseline net > 0)", "",
         "| signal (UTC+3) | pair | size | old net | baseline / persistence / adverse net | "
         "highest class | profit label | outcome |", "|---|---|---|---|---|---|---|---|"]
    f = lambda v: "UNKNOWN" if v is None else "%+.2f" % v  # noqa: E731
    for e in r["replay_entries"]:
        if "new" not in e:
            L.append("| %s | %s long / %s short | | | not re-evaluated | | | %s |" % (
                e["signal_at"], e["long"], e["short"], e["status"]))
            continue
        n = e["new"]["nets"]
        L.append("| %s | %s long / %s short | %.0f | %+.2f | %s / %s / %s | %s | %s | %s |" % (
            e["signal_at"], e["long"], e["short"], e["size"], e["old"]["net"],
            f(n["baseline"]), f(n["persistence"]), f(n["adverse"]), e["new"]["highest"],
            e["new"]["profit_label"], e["outcome"]))
    L += ["", "## B. Stream cycles, NATGAS directions, 24 h carry cash before costs ($1k)", "",
          "| direction | cycles | baseline > 0 | persistence > 0 | adverse > 0 | "
          "baseline only |", "|---|---|---|---|---|---|"]
    for k, c in sorted(r["cycles_carry_cash"].items()):
        L.append("| %s | %d | %d | %d | %d | %d |" % (
            k, c.get("cycles", 0), c.get("baseline_positive", 0),
            c.get("persistence_positive", 0), c.get("adverse_positive", 0),
            c.get("baseline_only", 0)))
    s = r["scanner_0512"]
    L += ["", "## C. 05:12 scanner snapshot", "", "classes: " + json.dumps(s.get("classes")),
          "", "profit label: " + json.dumps(s.get("profit_label"))]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    bench = paths.bench()
    out = Path(a.out) if a.out else bench / "v2-paper-data" / "phase15-funding-classes"
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
