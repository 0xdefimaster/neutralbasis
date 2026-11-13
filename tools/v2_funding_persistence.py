"""Phase 14 Slice D: funding persistence research (PAPER, offline, saved data only).

Reads (C: only, no network):
  v2-phase4-2026-11-12/answers.json.gz          05:12 snapshot, 7-day settled histories
                                                (tradeXYZ, Arcus, Lighter RH, RISEx)
  v2-aligned-2026-11-12/aligned.json.gz          90-day hourly settled history of
                                                tradeXYZ NATGAS and Hyperliquid INIT
  v2-phase8-rescue-2026-11-12/stream-*/stream.jsonl   TxFlow NATGAS activeAssetCtx
                                                (published nodeCtx.funding, percent) and
                                                tradeXYZ NATGAS published funding, 14:39-15:10
  v2-paper-data/natgas-history-20261113.json     later NATGAS pair observations (16:11-)

Writes v2-paper-data/phase14-funding-persistence/{report.json, report.md}.
Every number is research output (ESTIMATED); nothing here is a fill or a payment.
"""
import argparse
import datetime as dt
import gzip
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from v2 import funding_math, paths  # noqa: E402
from v2 import funding_persistence as fp  # noqa: E402

HOUR = 3600.0
TR = dt.timezone(dt.timedelta(hours=3))


def hm(t):
    return dt.datetime.fromtimestamp(t, TR).strftime("%m-%d %H:%M:%S")


def refuse_failing_disk(p):
    if str(Path(p).resolve()).lower().startswith(("d:", "e:")):
        raise SystemExit("refusing D:/E: (failing disk)")


def snap_hour(points, interval_s):
    """Settled rows -> [(hour_ts, rate per hour)]; a row whose time is more than 5 min
    away from a grid point is dropped (not moved)."""
    out = []
    for x in points or []:
        r, t = x.get("rate"), x.get("ts")
        if not isinstance(r, (int, float)) or t is None:
            continue
        g = round(t / interval_s) * interval_s
        if abs(t - g) <= 300:
            out.append((g, r / (interval_s / HOUR)))
    return fp.clean(out)


def regularity(series, interval_s):
    if len(series) < 2:
        return None
    gaps = [b[0] - a[0] for a, b in zip(series, series[1:])]
    return dict(expected_interval_s=interval_s,
                on_schedule_share=sum(1 for g in gaps if g == interval_s) / len(gaps),
                missing_payments=sum(int(g // interval_s) - 1 for g in gaps if g > interval_s))


def analyse(series, interval_s):
    m = fp.metrics(series)
    v = fp.validate(series) if interval_s == HOUR else dict(
        status="NOT_EVALUATED", reason="validation needs an hourly grid")
    return dict(metrics=m, validation=v, regularity=regularity(series, interval_s),
                confidence=fp.confidence(m, v))


# ----------------------------------------------------------------------------- inputs
def snapshot_histories(bench):
    from tools.v2_phase4_eval import Replay
    from v2.collector import Collector

    p = bench / "v2-phase4-2026-11-12" / "answers.json.gz"
    blob = json.loads(gzip.decompress(p.read_bytes()))
    at = blob["at"]
    col = Collector(transport=Replay(blob["answers"]), clock=lambda: at)
    col.refresh()
    out = []
    for (v, a), row in sorted(col.by_key.items()):
        try:
            pts = col.settled(v, a, at)
        except Exception as ex:  # replay miss: the snapshot did not hold this history
            pts, err = None, str(ex)[:80]
        else:
            err = None
        if pts is None:
            continue
        iv = row.get("interval") or HOUR
        out.append(dict(venue=v, exchange_id=row.get("exchange_id"), asset=a,
                        interval_s=iv, series=snap_hour(pts, iv), error=err,
                        published_rate_h=row.get("rate")))
    return at, out


def aligned_histories(bench):
    b = json.loads(gzip.decompress((bench / "v2-aligned-2026-11-12" / "aligned.json.gz")
                                   .read_bytes()))
    a = b["answers"]
    return {"tradexyz NATGAS (90d)": snap_hour(a["xyz_fh_NATGAS"]["data"], HOUR),
            "hyperliquid_native INIT (90d)": snap_hour(a["native_fh_INIT"]["data"], HOUR)}


def published_natgas(bench):
    """[(t, txflow_rate_h, prev_ft, next_ft, xyz_published_rate_h, block_time)] and the
    tradeXYZ settled rows seen in the same stream."""
    rows, settled = [], {}
    for f in sorted(bench.glob("v2-phase8-rescue-*/stream-*/stream.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)["rec"]
            for x in ((rec.get("histories") or {}).get("xyz_fh_NATGAS") or {}).get("data") or []:
                settled[round(x["ts"] / HOUR) * HOUR] = x["rate"]
            ans = rec.get("answers") or {}
            c = ans.get("txflow_ctx_NATGAS") or {}
            if not c.get("ok"):
                continue
            n = ((c.get("data") or {}).get("data") or {}).get("nodeCtx") or {}
            if n.get("funding") is None:
                continue
            xyz = None
            xc = (ans.get("xyz_ctx") or {}).get("data")
            if xc:
                names = [m["name"] for m in xc[0]["universe"]]
                if "xyz:NATGAS" in names:
                    xyz = float(xc[1][names.index("xyz:NATGAS")]["funding"])
            rows.append(dict(t=c.get("t1") or rec.get("at"),
                             txflow_rate_h=float(n["funding"]) / 100.0,
                             prev_ft=(n.get("prevFundingTime") or 0) / 1000 or None,
                             next_ft=(n.get("nextFundingTime") or 0) / 1000 or None,
                             xyz_rate_h=xyz, block_time=(n.get("blockTime") or 0) / 1000))
    later = []
    nh = bench / "v2-paper-data" / "natgas-history-20261113.json"
    if nh.exists():
        d = json.loads(nh.read_text(encoding="utf-8"))
        later = [dict(t=p["at"], txflow_rate_h=p.get("txflow_rate_h"),
                      xyz_rate_h=p.get("xyz_rate_h")) for p in d.get("points") or []]
    return rows, later, sorted(settled.items())


# ------------------------------------------------------------------------- NATGAS case
def natgas_case(rows, later, xyz_settled, xyz_fit, notional=1000.0, hours=24, ref_hour_local=15):
    """Long TxFlow / short tradeXYZ (the 15:00-15:09 direction), three models."""
    win = [r for r in rows if r["xyz_rate_h"] is not None]
    ref = None
    for r in win:  # the first positive-carry sample after the 15:00 boundary
        if r["prev_ft"] and r["t"] >= r["prev_ft"] and r["xyz_rate_h"] - r["txflow_rate_h"] > 0 \
                and dt.datetime.fromtimestamp(r["t"], TR).hour == ref_hour_local:
            ref = r
            break
    if ref is None:
        return dict(status="NO_REFERENCE_POINT")
    past =[r["txflow_rate_h"] for r in rows if r["t"] <= ref["t"]]
    models = {}
    for name in ("baseline", "persistence", "adverse"):
        lp = fp.leg_path(ref["txflow_rate_h"], name, hours, fit=None,
                         samples=past if name != "baseline" else None)
        if name == "adverse":
            # long pays: the adverse case is a HIGH TxFlow rate (q90 of what was seen up
            # to the reference time) after the current period
            lp = [ref["txflow_rate_h"]] + [sorted(past)[int(0.9 * (len(past) - 1))]] * (hours - 1)
        sf = dict(xyz_fit)
        if name == "adverse":  # short receives: adverse = rate falls by train q10
            sf["adverse"] = xyz_fit["adverse_q10"]
        sp = fp.leg_path(ref["xyz_rate_h"], name, hours, fit=sf)
        cash, partial = fp.pair_cash(lp, sp, notional)
        models[name] = dict(cash_24h=cash, partial=partial, long_txflow_path_head=lp[:3],
                            short_xyz_path_head=sp[:3],
                            carry_8h_pct=None if cash is None else cash / notional / 3 * 100)
    # what the next observations showed (published values only; TxFlow payments UNKNOWN)
    after = sorted({round(r["t"], 1): r for r in rows + later
                    if r["t"] > ref["t"]}.values(), key=lambda r: r["t"])
    pick = {0, len(after) - 1} if after else set()
    for i in range(1, len(after)):  # first sample of each new hour, and each hour's last
        if int(after[i]["t"] // HOUR) != int(after[i - 1]["t"] // HOUR):
            pick |= {i - 1, i}
    nxt = [dict(at=hm(r["t"]), txflow_rate_h=r["txflow_rate_h"], xyz_rate_h=r["xyz_rate_h"],
                carry_8h_pct=None if r["xyz_rate_h"] is None or r["txflow_rate_h"] is None
                else (r["xyz_rate_h"] - r["txflow_rate_h"]) * 8 * 100)
           for r in (after[i] for i in sorted(pick))]
    xyz_paid = [dict(hour=hm(t), rate_h=rt) for t, rt in xyz_settled if ref["t"] - HOUR <= t
                <= ref["t"] + 3 * HOUR]
    pays = funding_math.payment_times(ref["next_ft"], HOUR, ref["t"], ref["t"] + hours * HOUR)
    return dict(reference=dict(at=hm(ref["t"]), txflow_rate_h=ref["txflow_rate_h"],
                               xyz_rate_h=ref["xyz_rate_h"], prev_funding=hm(ref["prev_ft"]),
                               next_funding=hm(ref["next_ft"]),
                               carry_8h_pct=(ref["xyz_rate_h"] - ref["txflow_rate_h"]) * 800),
                notional_usd=notional, hold_h=hours, payments_in_hold=None if pays is None else len(pays),
                models=models, next_observations=nxt, samples_after_reference=len(after),
                xyz_settled_around=xyz_paid,
                txflow_payment_evidence="ACCOUNT_PAYMENT_UNVERIFIED (no authorized address); "
                "the published value is an intra-period estimate, not a paid rate")


def txflow_intra_period(rows):
    """The published TxFlow value inside each funding period: does it reset at the
    boundary? Grouped by prevFundingTime."""
    per = {}
    for r in rows:
        per.setdefault(r["prev_ft"], []).append(r)
    out = []
    for p, rs in sorted(per.items(), key=lambda kv: kv[0] or 0):
        xs = [x["txflow_rate_h"] for x in rs]
        out.append(dict(period_start=hm(p) if p else None, samples=len(xs),
                        first=dict(at=hm(rs[0]["t"]), rate_h=xs[0]),
                        last=dict(at=hm(rs[-1]["t"]), rate_h=xs[-1]),
                        min=min(xs), max=max(xs),
                        range_over_mean_abs=(max(xs) - min(xs)) /
                        (statistics.mean(abs(x) for x in xs) or 1)))
    series = [dict(at=hm(r["t"]), rate_h=r["txflow_rate_h"], next=hm(r["next_ft"]))
              for r in rows]
    return dict(periods=out, series=series)


# ------------------------------------------------------------------------------ main
def build(bench):
    at, snap = snapshot_histories(bench)
    markets = []
    for h in snap:
        a = analyse(h["series"], h["interval_s"])
        markets.append(dict(venue=h["venue"], exchange_id=h["exchange_id"], asset=h["asset"],
                            interval_s=h["interval_s"], **a))
    long_ = {k: analyse(s, HOUR) for k, s in aligned_histories(bench).items()}
    rows, later, xyz_settled = published_natgas(bench)
    v = long_["tradexyz NATGAS (90d)"]["validation"]
    xyz_fit = dict(mean=v.get("train_mean"), phi=v.get("train_phi"),
                   adverse_q10=v.get("adverse_q10"))
    agg = {}
    for m in markets:
        g = agg.setdefault(m["exchange_id"], dict(markets=0, validated=0,
                                                  persistence_better=0, conf={}))
        g["markets"] += 1
        g["conf"][m["confidence"]] = g["conf"].get(m["confidence"], 0) + 1
        if m["validation"].get("status") == "OK":
            g["validated"] += 1
            g["persistence_better"] += bool(m["validation"]["persistence_better"])
            b = m["validation"]["best_model"]
            g.setdefault("best_model", {})[b] = g.setdefault("best_model", {}).get(b, 0) + 1
            for k in ("sign_kept_24h", "coverage_long_receives"):
                g.setdefault(k, []).append(m["validation"][k])
    for g in agg.values():
        for k in ("sign_kept_24h", "coverage_long_receives"):
            if g.get(k):
                g[k] = statistics.median(g[k])
    return dict(
        label="ESTIMATED research (PAPER). Settled = public market settlement history; "
              "TxFlow/Variational = published values only, payments UNKNOWN.",
        snapshot_at=at, snapshot_at_local=hm(at), by_venue=agg, markets=markets,
        long_history=long_, txflow_natgas=txflow_intra_period(rows),
        natgas_case=natgas_case(rows, later, xyz_settled, xyz_fit))


def md(r):
    L = ["# Phase 14 Slice D - funding persistence (ESTIMATED research)", "",
         r["label"], "", "Snapshot: %s (UTC+3)" % r["snapshot_at_local"], "",
         "## Settled histories in the 05:12 snapshot", "",
         "| venue | markets | validated | AR(1) beats baseline | best model (count) | "
         "median sign kept 24h | median adverse coverage | confidence |",
         "|---|---|---|---|---|---|---|---|"]
    for v, g in sorted(r["by_venue"].items()):
        L.append("| %s | %d | %d | %d | %s | %s | %s | %s |" % (
            v, g["markets"], g["validated"], g["persistence_better"], g.get("best_model"),
            "%.2f" % g["sign_kept_24h"] if g.get("sign_kept_24h") else "-",
            "%.2f" % g["coverage_long_receives"] if g.get("coverage_long_receives") else "-",
            g["conf"]))
    L += ["", "## 90-day hourly histories", ""]
    for k, a in r["long_history"].items():
        m, v = a["metrics"], a["validation"]
        L.append("- **%s**: %d pts, mean %.2e/h, phi %.3f, half-life %s h, sign kept "
                 "1h/8h/24h %s, reversals/day %.2f; validation MAE baseline %.2e vs "
                 "persistence %.2e (%s); MAE all %s; adverse q10 %.2e, coverage %.2f; "
                 "confidence %s" % (
                     k, m["points"], m["mean"], m["ar1_phi"],
                     "%.1f" % m["half_life_h"] if m["half_life_h"] not in (None,) else "-",
                     {h: round(x, 2) if x is not None else None
                      for h, x in m["sign_persistence"].items() if h in ("1h", "8h", "24h")},
                     m["reversals_per_day"] or 0, v.get("mae_baseline", 0),
                     v.get("mae_persistence", 0),
                     "persistence better" if v.get("persistence_better") else "baseline better",
                     {k: "%.2e" % x for k, x in (v.get("mae") or {}).items()},
                     v.get("adverse_q10") or 0, v.get("coverage_long_receives") or 0,
                     a["confidence"]))
    c = r["natgas_case"]
    L += ["", "## NATGAS 15:00-15:09: TxFlow published funding inside each period", "",
          "| period start | samples | first | last | min | max |", "|---|---|---|---|---|---|"]
    for p in r["txflow_natgas"]["periods"]:
        L.append("| %s | %d | %s %.2e | %s %.2e | %.2e | %.2e |" % (
            p["period_start"], p["samples"], p["first"]["at"], p["first"]["rate_h"],
            p["last"]["at"], p["last"]["rate_h"], p["min"], p["max"]))
    if c.get("reference"):
        ref = c["reference"]
        L += ["", "Reference (long TxFlow / short tradeXYZ): %s TxFlow %.2e/h, xyz %.2e/h, "
              "carry %+.3f %%/8h" % (ref["at"], ref["txflow_rate_h"], ref["xyz_rate_h"],
                                     ref["carry_8h_pct"]), "",
              "| model ($%d, %d h) | 24h carry cash | carry %%/8h |" % (
                  c["notional_usd"], c["hold_h"]), "|---|---|---|"]
        for k, m in c["models"].items():
            L.append("| %s | %s | %s |" % (k, "%.2f" % m["cash_24h"] if m["cash_24h"] is not None
                                          else "UNKNOWN %s" % m["partial"],
                                          "%.3f" % m["carry_8h_pct"] if m["carry_8h_pct"]
                                          is not None else "-"))
        L += ["", "Next observations (published, not paid):", ""]
        for n in c["next_observations"]:
            L.append("- %s TxFlow %s xyz %s carry %s %%/8h" % (
                n["at"], n["txflow_rate_h"], n["xyz_rate_h"],
                "%.3f" % n["carry_8h_pct"] if n["carry_8h_pct"] is not None else "-"))
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    bench = paths.bench()
    refuse_failing_disk(bench)
    out = Path(a.out) if a.out else bench / "v2-paper-data" / "phase14-funding-persistence"
    refuse_failing_disk(out)
    from v2.control import netguard
    with netguard.offline() as g:
        r = build(bench)
    r["network_attempts"] = len(g)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
    (out / "report.md").write_text(md(r), encoding="utf-8")
    print(md(r))
    print("written:", out)


if __name__ == "__main__":
    main()
