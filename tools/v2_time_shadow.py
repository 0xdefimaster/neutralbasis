"""Time-basis SHADOW evaluation (Phase 15, docs/v2/45). Offline, recorded data only.

  python tools/v2_time_shadow.py [--out DIR]

Four times are kept apart for every book used in an evaluation:
  source      the venue's own book time (where published)
  fetch       when the answer was received (`t1`, = answer_received)
  read        when the engine read it; the collector re-stamps a cache hit here
              (legacy `received`)
  evaluation  the decision time (`at`)

A. Recorded streams (rescued Phase 8 stream, Phase 9 parallel probe): for every cycle
   and book, the age under the legacy basis (a collector cache hit is re-stamped at
   read, so its age at the decision is about 0), the answer basis and the source basis,
   against the venue limit (v2.marketdata.MAX_AGE). The ESTIMATED replays already
   measure from source time (else receive time): their decisions are read back.
B. The 05:12 scanner snapshot replays with a constant clock: every basis is equal by
   construction; it cannot show a difference (reported as NOT_MEASURABLE).
C. Live collector path (no saved run exists): the bounds that follow from the cache
   TTLs and the refresh cadence, stated as bounds, not measurements.
"""
import argparse
import collections
import glob
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import v2_funding_persistence as tp  # noqa: E402
from v2 import collector, marketdata, paths, storage  # noqa: E402
from v2.control import netguard  # noqa: E402
from v2.paper_obs import CORE, source_time  # noqa: E402

BOOKS = dict(txflow_book_NATGAS="txflow", xyz_book_NATGAS="tradexyz",
             native_book_INIT="hyperliquid_native")


def streams(bench):
    return sorted(list(bench.glob("v2-phase8-rescue-*/stream-*/stream.jsonl")) +
                  list(bench.glob("v2-paper-data/probe-parallel-*/stream.jsonl")))


def historical(bench):
    out = {}
    for f in streams(bench):
        per = collections.defaultdict(lambda: dict(n=0, verdicts=collections.Counter(),
                                                   answer_age=[], source_age=[],
                                                   source_unknown=0))
        cycles = 0
        for line in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)["rec"]
            A = rec.get("answers") or {}
            t1s = [A[k]["t1"] for k in CORE if (A.get(k) or {}).get("ok")]
            if not t1s:
                continue
            at = max(t1s)                     # the replay's decision time (paper_obs)
            cycles += 1
            for name, xid in BOOKS.items():
                a = A.get(name) or {}
                if not a.get("ok"):
                    continue
                st = a.get("source_ts") or source_time(name, a.get("data"))
                book = dict(received=at, answer_received=a["t1"], ts=st)  # legacy = read
                v = marketdata.stale_views(book, xid, at)
                p = per[xid]
                p["n"] += 1
                p["verdicts"][v["verdict"]] += 1
                p["answer_age"].append(v["answer_age_s"])
                if st is None:
                    p["source_unknown"] += 1
                else:
                    p["source_age"].append(v["source_age_s"])
                    if v["source_age_s"] > v["limit_s"]:
                        p["verdicts"]["SOURCE_BASIS_STALE"] += 1

        def q(xs, p):
            return sorted(xs)[int(p * (len(xs) - 1))] if xs else None
        out[f.parent.name] = dict(cycles=cycles, books={x: dict(
            n=p["n"], limit_s=marketdata.MAX_AGE.get(x, 15.0),
            verdicts=dict(p["verdicts"]),
            answer_age_s=dict(median=statistics.median(p["answer_age"]),
                              p95=q(p["answer_age"], .95), max=max(p["answer_age"])),
            source_age_s=dict(median=statistics.median(p["source_age"]) if p["source_age"]
                              else None, p95=q(p["source_age"], .95),
                              max=max(p["source_age"]) if p["source_age"] else None,
                              unknown=p["source_unknown"]))
            for x, p in sorted(per.items())})
    return out


def replay_decisions(bench):
    """ESTIMATED replays: how many evaluations a time check blocked (they measure from
    source time, else receive time: the honest basis already)."""
    out = {}
    for f in sorted(glob.glob(str(bench / "v2-paper-data" / "phase9-replays" / "*" /
                                  "*" / "evaluations.json"))):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        rel = str(Path(f).relative_to(bench / "v2-paper-data"))
        out[rel] = dict(evaluations=len(d),
                        blocked_by_time=sum(1 for e in d if e.get("blocked_by_time")),
                        time_quality=dict(collections.Counter(e.get("time_quality")
                                                              for e in d)))
    return out


def bounds():
    from v2.engine_source import V2EngineSource as V2Source
    import inspect
    refresh = inspect.signature(V2Source.__init__).parameters["refresh_every"].default
    ttl = collector.TTL
    import wide_paper
    return dict(
        book=dict(cache_ttl_s=ttl["book"], strict_limit_min_s=min(marketdata.MAX_AGE.values()),
                  legacy_understatement_max_s=ttl["book"],
                  note="a cache hit is served only while <= TTL old, so the answer age at "
                       "read is < %.0f s; a STRICT decision flips only if the gate runs "
                       "more than (limit - TTL) s after the read" % ttl["book"]),
        funding_row=dict(meta_ttl_s=ttl["meta"], refresh_every_s=refresh,
                         legacy_desk_entry_limit_s=wide_paper.ENTRY_FUNDING_MAX_AGE,
                         answer_age_bound_s=ttl["meta"] + refresh,
                         note="legacy age < refresh_every; answer age < meta TTL + "
                              "refresh_every = the 120 s limit itself: at the boundary"),
        reread_finding="EngineSource.current_funding 're-reads' by refreshing the "
                       "collector, which serves the SAME cached answer for up to "
                       "TTL['meta'] s and re-stamps it: the legacy desk's stale-quote "
                       "re-read does not fetch anything new within the TTL",
        settled=dict(ttl_s=ttl["settled"], note="histories; no age gate reads them"))


def build(bench):
    return dict(generated_at=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                evidence="OBSERVED recorded streams (A); bounds from code constants (C)",
                historical=historical(bench), replay_decisions=replay_decisions(bench),
                scanner_snapshot=dict(status="NOT_MEASURABLE", why="the 05:12 snapshot "
                                      "replays with a constant clock: read = fetch = "
                                      "evaluation for every answer"),
                live_collector_bounds=bounds())


def md(r):
    L = ["# Time-basis shadow evaluation", "", r["evidence"], "",
         "## A. Recorded streams (decision time = last core answer of the cycle)", ""]
    for name, s in r["historical"].items():
        L += ["### %s (%d cycles)" % (name, s["cycles"]), "",
              "| venue | books | limit | answer age median / p95 / max | source age median / "
              "p95 / max | verdicts |", "|---|---|---|---|---|---|"]
        for x, b in s["books"].items():
            a, so = b["answer_age_s"], b["source_age_s"]
            L.append("| %s | %d | %.0f s | %.2f / %.2f / %.2f s | %s | %s |" % (
                x, b["n"], b["limit_s"], a["median"], a["p95"], a["max"],
                "-" if so["median"] is None else "%.2f / %.2f / %.2f s" % (
                    so["median"], so["p95"], so["max"]),
                ", ".join("%s %d" % kv for kv in sorted(b["verdicts"].items()))))
        L.append("")
    L += ["## ESTIMATED replay decisions blocked by time", "",
          "| replay | evaluations | blocked by time |", "|---|---|---|"]
    for k, v in r["replay_decisions"].items():
        L.append("| %s | %d | %d |" % (k, v["evaluations"], v["blocked_by_time"]))
    b = r["live_collector_bounds"]
    L += ["", "## B. Scanner snapshot: %s (%s)" % (r["scanner_snapshot"]["status"],
                                                   r["scanner_snapshot"]["why"]),
          "", "## C. Live collector path (bounds, not measurements)", "",
          "- book: " + b["book"]["note"],
          "- funding row: " + b["funding_row"]["note"],
          "- finding: " + b["reread_finding"]]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    bench = paths.bench()
    out = Path(a.out) if a.out else bench / "v2-paper-data" / "phase15-time-shadow"
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
