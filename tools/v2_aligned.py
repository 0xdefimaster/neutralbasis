"""Same-timestamp NATGAS / INIT analysis: every number in one calculation comes from one
burst of reads whose receive times are recorded and checked.

PAPER / research only.

--observe (explicit opt-in): one burst of public reads under the observation_v3 guard
(never NB_V2): Variational stats, xyz / native metaAndAssetCtxs, TxFlow NATGAS
activeAssetCtx and the three needed books first (time-critical), then funding
histories (anchored to the burst time). Each answer is saved with its request start
and receive time (aligned.json.gz + sha256 manifest).

offline (default): from that file only. For each pair, the legs' data times are the
receive times (and, for a Variational RFQ quote, its own updated_at). When the spread
of leg times exceeds MAX_SKEW_S the pair is TIME_MISALIGNED and no carry is computed:
rates from different moments are never combined. Funding evidence is computed per
market and window (funding_evidence.market_evidence); the NATGAS gate decision uses
the reference registry (v2.references).

Usage: python -m tools.v2_aligned --out DIR [--observe]
"""
import argparse
import gzip
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2 import economics, funnel, gates, observation, references  # noqa: E402
from v2 import funding_evidence as fe  # noqa: E402
from v2.venues import hyperliquid_obs as hlo  # noqa: E402
from v2.venues import tradexyz, txflow, variational  # noqa: E402

MAX_SKEW_S = 60.0
DAYS = 90
LABEL = "OBSERVED same-burst public reads (observation_v3); RESEARCH_ONLY"


def refuse_failing_disk(path):
    if str(Path(path).resolve()).lower().startswith(("e:", "d:")):
        raise SystemExit("must not read or write on the failing disk (D:/E:)")


# ----------------------------------------------------------------------- observe
def observe(out):
    from pilot import network_guard
    from v2.venues import base

    guard = network_guard.install(str(out / "guard-aligned.jsonl"), v2=False,
                                  mode=observation.MODE)
    A = {}

    def get(name, xid, method, url, body=None, headers=None):
        t0 = time.time()
        try:
            data = base.http_json(xid, method, url, body, headers)
            A[name] = dict(ok=True, xid=xid, method=method, url=url, body=body, t0=t0,
                           t1=time.time(), data=data)
        except Exception as ex:
            A[name] = dict(ok=False, xid=xid, method=method, url=url, body=body, t0=t0,
                           t1=time.time(), error="%s: %s" % (type(ex).__name__,
                                                              str(ex)[:300]))
        return A[name].get("data")

    hl = hlo.INFO_URL
    tx = txflow.HEADERS
    meta = get("txflow_meta", "txflow", *txflow.meta_request(), headers=tx)
    idx = next((str(u["index"]) for u in ((meta or {}).get("data") or meta or {})
                .get("universe", []) if u.get("baseCurrency") == "NATGAS"), None)
    # time-critical burst
    get("variational_stats", "variational", *variational.stats_request())
    get("xyz_ctx", "tradexyz", "POST", hl, tradexyz.meta_and_ctxs())
    get("native_ctx", "hyperliquid_native", "POST", hl,
        hlo.meta_and_ctxs("hyperliquid_native"))
    if idx:
        get("txflow_ctx_NATGAS", "txflow", *txflow.ctx_request(idx), headers=tx)
        get("txflow_book_NATGAS", "txflow", *txflow.book_request(idx), headers=tx)
    get("xyz_book_NATGAS", "tradexyz", "POST", hl, tradexyz.l2_book("NATGAS"))
    get("native_book_INIT", "hyperliquid_native", "POST", hl,
        hlo.l2_book("hyperliquid_native", "INIT"))
    burst_end = time.time()
    # histories, anchored to the burst
    end_ms = int(burst_end * 1000)
    start_ms = end_ms - DAYS * 86400 * 1000
    for name, xid, sym in (("native_fh_INIT", "hyperliquid_native", "INIT"),
                           ("xyz_fh_NATGAS", "tradexyz", "NATGAS")):
        t0 = time.time()
        pages = []

        def fetch(b, xid=xid):
            d = base.http_json(xid, "POST", hl, b)
            pages.append(len(d or []))
            return d

        try:
            if xid == "tradexyz":
                rows, s = [], start_ms
                while True:
                    page = fetch(tradexyz.funding_history(sym, s, end_ms))
                    rows.extend(page or [])
                    if not page or len(page) < hlo.PAGE:
                        break
                    s = max(int(x["time"]) for x in page) + 1
                hist = hlo.parse_funding_history(rows)
            else:
                hist = hlo.history(fetch, xid, sym, start_ms, end_ms)
            A[name] = dict(ok=True, xid=xid, t0=t0, t1=time.time(), pages=pages,
                           data=hist)
        except Exception as ex:
            A[name] = dict(ok=False, xid=xid, t0=t0, t1=time.time(), pages=pages,
                           error="%s: %s" % (type(ex).__name__, str(ex)[:300]))
    raw = json.dumps(dict(burst_end=burst_end, txflow_natgas_index=idx, answers=A),
                     default=str).encode()
    (out / "aligned.json.gz").write_bytes(gzip.compress(raw))
    man = dict(created=time.time(), sha256=hashlib.sha256(raw).hexdigest(),
               mode=observation.MODE, label=LABEL,
               reads={k: v.get("ok") for k, v in A.items()},
               guard=dict(guard.stats), by_venue=dict(guard.by_venue))
    (out / "aligned_manifest.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
    return man


# ----------------------------------------------------------------------- analyse
def book_of(raw, parser=None):
    if raw is None:
        return None
    if parser:
        return parser(raw)
    lv = raw.get("levels") or [[], []]
    return dict(bids=[(float(x["px"]), float(x["sz"])) for x in lv[0]],
                asks=[(float(x["px"]), float(x["sz"])) for x in lv[1]],
                model="orderbook", empty=not lv[0] or not lv[1], crossed=False)


def legs_time(legs):
    times = [t for leg in legs for t in leg["times"] if t is not None]
    return (max(times) - min(times)) if times else None


def pair(asset, long, short, at):
    """Carry and the size ladder for one pair, only if its legs are time-aligned."""
    skew = legs_time([long, short])
    res = dict(asset=asset, long=long["venue"], short=short["venue"], at=at,
               leg_times={long["venue"]: long["times"], short["venue"]: short["times"]},
               skew_s=skew, max_skew_s=MAX_SKEW_S)
    if skew is None or skew > MAX_SKEW_S:
        res.update(status="TIME_MISALIGNED", carry_8h=None, ladder=None)
        return res
    r = funnel.radar(asset, long, short)
    res.update(status="ALIGNED", long_hourly=long["hourly"], short_hourly=short["hourly"],
               long_8h=fe.per_8h(long["hourly"]), short_8h=fe.per_8h(short["hourly"]),
               long_cash_h=fe.leg_cashflow("long", long["hourly"]),
               short_cash_h=fe.leg_cashflow("short", short["hourly"]),
               carry_8h=r["carry_8h"], gap_bp=r["gap_bp"], label=r["label"],
               ladder=r["sizes"], eligible=False)
    return res


def analyse(out):
    raw = (out / "aligned.json.gz").read_bytes()
    blob = json.loads(gzip.decompress(raw))
    A = blob["answers"]
    sha = hashlib.sha256(gzip.decompress(raw)).hexdigest()
    burst = [A[k]["t1"] for k in ("variational_stats", "xyz_ctx", "native_ctx",
                                  "txflow_ctx_NATGAS", "txflow_book_NATGAS",
                                  "xyz_book_NATGAS", "native_book_INIT") if k in A]
    at = statistics.median(burst)
    var = {r["instrument"].market_id: r
           for r in variational.parse_markets(A["variational_stats"]["data"])}
    xyz = {m["instrument"].market_id: m for m in tradexyz.parse_markets(A["xyz_ctx"]["data"])}
    nat = {m["base"]: m for m in hlo.parse_markets("hyperliquid_native",
                                                    A["native_ctx"]["data"])}
    t = lambda k: A[k]["t1"] if A.get(k, {}).get("ok") else None  # noqa: E731

    def var_leg(mid, size_bucket=1000.0):
        r = var[mid]
        age = variational.quote_age(r, t("variational_stats"))
        qt = None if age is None else t("variational_stats") - age
        book = dict(variational.quote_book_for(r, size_bucket), model="rfq_indicative")
        return dict(venue="Variational", exchange_id="variational",
                    hourly=fe.per_hour(r["funding"]["raw"], r["funding"]["interval_s"]),
                    fee=r["fee_taker"], book=book, record=r,
                    times=[t("variational_stats"), qt], quote_age_s=age)

    out_ = dict(label=LABEL, sha256=sha, at=at, burst_spread_s=max(burst) - min(burst),
                rfq_model=variational.rfq_model_status())
    # NATGAS: TxFlow / tradeXYZ (and the Variational alternative)
    ctx = (A.get("txflow_ctx_NATGAS") or {}).get("data")
    tx_rec = None
    if ctx is not None and blob.get("txflow_natgas_index"):
        meta = A["txflow_meta"]["data"]
        meta = meta if "data" in meta else dict(data=meta)
        recs = txflow.parse_markets(meta, None, {blob["txflow_natgas_index"]: ctx})
        tx_rec = next(r for r in recs if r["instrument"].market_id ==
                      blob["txflow_natgas_index"])
    xr = xyz["xyz:NATGAS"]
    xyz_leg = dict(venue="tradeXYZ", exchange_id="tradexyz", hourly=xr["funding"]["raw"],
                   fee=xr["fee_taker"], book=book_of(A["xyz_book_NATGAS"]["data"]),
                   times=[t("xyz_ctx"), t("xyz_book_NATGAS")])
    nat_res = dict(reference=references.compare("txflow", "tradexyz", "NATGAS"),
                   reference_xyz_vs_variational=references.compare("tradexyz",
                                                                   "variational", "NATGAS"))
    if tx_rec is not None:
        f = tx_rec["funding"]
        tx_h = fe.per_hour(f["raw"] / 100.0 if f["unit"] == "percent" else f["raw"],
                           f["interval_s"] or 3600)
        tx_leg = dict(venue="TxFlow", exchange_id="txflow", hourly=tx_h,
                      fee=tx_rec["fee_taker"],
                      book=book_of(A["txflow_book_NATGAS"]["data"], txflow.parse_book),
                      times=[t("txflow_ctx_NATGAS"), t("txflow_book_NATGAS")],
                      next_funding=f.get("next_ts"))
        nat_res["txflow_xyz"] = pair("NATGAS", tx_leg, xyz_leg, at)
    else:
        nat_res["txflow_xyz"] = dict(status="TXFLOW_NOT_READ")
    nat_res["variational_xyz"] = pair("NATGAS", var_leg("NATGAS-PERP"), xyz_leg, at)
    fh = A["xyz_fh_NATGAS"]["data"] if A.get("xyz_fh_NATGAS", {}).get("ok") else None
    nat_res["evidence"] = dict(
        tradexyz=fe.market_evidence("tradexyz", [dict(x, kind="settled_public")
                                                 for x in fh] if fh is not None else None,
                                    at, 3600, 7),
        txflow=fe.market_evidence("txflow", None, at, 3600, 7),
        variational=fe.market_evidence("variational", None, at, 14400, 7))
    out_["NATGAS"] = nat_res
    # INIT: Variational / Hyperliquid native
    nr = nat["INIT"]
    hl_leg = dict(venue="Hyperliquid", exchange_id="hyperliquid_native",
                  hourly=nr["funding"]["raw"], fee=nr["fee_taker"],
                  book=book_of(A["native_book_INIT"]["data"]),
                  times=[t("native_ctx"), t("native_book_INIT")])
    init_res = dict()
    p1 = pair("INIT", var_leg("INIT-PERP", 1000.0), hl_leg, at)
    # 5k/10k use the 100k quote bucket (the RFQ price for that size)
    big = pair("INIT", var_leg("INIT-PERP", 5000.0), hl_leg, at)
    if p1.get("ladder") and big.get("ladder"):
        p1["ladder"] = [p1["ladder"][0]] + big["ladder"][1:]
    init_res["variational_hyperliquid"] = p1
    ih = A["native_fh_INIT"]["data"] if A.get("native_fh_INIT", {}).get("ok") else None
    cov = fe.coverage(ih or [], at)
    init_res["hl_history"] = {k: cov[k]["label"] for k, _ in fe.WINDOWS}
    init_res["hl_means_hourly"] = {k: cov[k]["mean_hourly"] for k, _ in fe.WINDOWS}
    init_res["hl_anomalies"] = fe.anomalies(nr["funding"]["raw"],
                                            [x["rate"] for x in ih or []], cov)
    init_res["evidence"] = dict(
        hyperliquid_native=fe.market_evidence(
            "hyperliquid_native", [dict(x, kind="settled_public") for x in ih]
            if ih is not None else None, at, 3600, 7),
        variational=fe.market_evidence("variational", None, at, 3600, 7))
    init_res["paper_universe"] = dict(
        variational=observation.is_paper_venue("variational"),
        hyperliquid_native=observation.is_paper_venue("hyperliquid_native"))
    out_["INIT"] = init_res
    return out_


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--observe", action="store_true")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    refuse_failing_disk(out)
    out.mkdir(parents=True, exist_ok=True)
    if a.observe:
        print(json.dumps(observe(out), indent=1))
    res = analyse(out)
    (out / "aligned_analysis.json").write_text(json.dumps(res, indent=1, default=str),
                                               encoding="utf-8")
    print(json.dumps(res, indent=1, default=str)[:6000])


if __name__ == "__main__":
    main()
