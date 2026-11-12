"""NATGAS / INIT forensics and the opportunity funnel on recorded data.

PAPER / research only. Two steps:

offline (default): the 05:12 phase-4 answers (answers.json.gz, sha256 in its manifest)
are replayed through v2.collector, the rankers and v2.gates; every candidate becomes a
v2.funnel trace (funnel.json); NATGAS and INIT are traced from discovery to the gate
(forensics.json), with the radar on the recorded books.

--observe (explicit opt-in): a small set of public reads under the observation_v3
network guard (v2.observation; NOT the NB_V2 PAPER guard, which stays unchanged):
Hyperliquid native and EntropyIO (dex "io") metaAndAssetCtxs, INIT native funding
history (90 days, paged) and book, xyz:NATGAS funding history and book, TxFlow NATGAS
ctx and book, Variational stats and the Phoenix exchange snapshot. Saved with sha256
(observation.json.gz). Nothing is traded; no account is read.

Usage: python -m tools.v2_forensics --phase4 DIR --out DIR [--observe]
"""
import argparse
import collections
import gzip
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wide_paper  # noqa: E402
from v2 import funding_evidence as fe  # noqa: E402
from v2 import funnel, gates, observation  # noqa: E402
from v2.collector import Collector  # noqa: E402
from v2.venues import hyperliquid_obs as hlo  # noqa: E402

ASSETS = ("NATGAS", "INIT")
DAYS90_MS = 90 * 86400 * 1000


def refuse_failing_disk(path):
    if str(Path(path).resolve()).lower().startswith(("e:", "d:")):
        raise SystemExit("must not read or write on the failing disk (D:/E:)")


# ----------------------------------------------------------------------- offline
def replay(phase4):
    from tools.v2_phase4_eval import PILOT_DB, Provider, Replay, pilot_closes

    raw = (phase4 / "answers.json.gz").read_bytes()
    blob = json.loads(gzip.decompress(raw))
    snap_sha = hashlib.sha256(gzip.decompress(raw)).hexdigest()
    at = blob["at"]
    col = Collector(transport=Replay(blob["answers"]), clock=lambda: at)
    rows = col.refresh()
    closes, _ = pilot_closes(PILOT_DB)
    prov = Provider(col, closes)
    return dict(at=at, sha=snap_sha, col=col, rows=rows, prov=prov,
                gate=gates.Gate(prov, dict(wide_paper.WIDE_DEFAULTS)), blob=blob)


def evidence_of(r, cache, venue, asset):
    """Market-level funding evidence of one leg over the 7-day window (cached)."""
    k = (venue, asset)
    if k not in cache:
        row = r["prov"].rows_by().get(k) or {}
        pts = r["prov"].settled(venue, asset, r["at"])
        cache[k] = fe.market_evidence(row.get("exchange_id"), pts, r["at"],
                                      row.get("interval") or 3600, 7)
    return cache[k]


def all_traces(r, market_evidence=True):
    cfg = dict(wide_paper.WIDE_DEFAULTS)
    rows_by = r["prov"].rows_by()
    out, at, cache = [], r["at"], {}

    def tr(cc, strategy):
        evid = (dict(long=evidence_of(r, cache, cc["long"], cc["asset"]),
                     short=evidence_of(r, cache, cc["short"], cc["asset"]))
                if market_evidence else None)
        return funnel.trace(cc, r["gate"].evaluate(cc, at, strategy), rows_by,
                            r["col"].groups, r["sha"], strategy, evidence=evid)

    for c in wide_paper.rank(r["rows"], cfg, at):
        out.append(tr(dict(c, strategy="funding"), "funding"))
    pairs = set()
    for c in wide_paper.rank_spreads(r["rows"], at, cfg):
        for lv, sv in ((c["long"], c["short"]), (c["short"], c["long"])):
            pairs.add((c["asset"], lv, sv))
            out.append(tr(dict(asset=c["asset"], long=lv, short=sv,
                               strategy="convergence"), "convergence"))
    for asset, lv, sv in sorted(pairs | {(t["asset"], t["long"], t["short"]) for t in out
                                         if t["strategy"] == "funding"}):
        out.append(tr(dict(asset=asset, long=lv, short=sv), "hybrid"))
    return out


def radar_all(r, traces):
    """The radar for every NOT_EVALUATED pair (research only)."""
    return funnel.radar_index(traces, lambda v, a: leg_input(r, v, a))


def leg_input(r, venue, asset):
    row = r["prov"].rows_by().get((venue, asset)) or {}
    return dict(venue=venue, exchange_id=row.get("exchange_id"), hourly=row.get("rate"),
                fee=row.get("fee"), book=r["prov"].book(venue, asset),
                interval=row.get("interval"))


def asset_story(r, asset, traces):
    """Discovery -> group -> rows -> candidates -> gate, for one asset."""
    groups = {k: dict(status=g.get("status"), venues=g.get("venues"), tier=g.get("tier"),
                      reason=g.get("reason"))
              for k, g in r["col"].groups.items()
              if k.split(":")[-1].split("-")[0] == asset or k.endswith(":" + asset)
              or ("variational:%s-PERP" % asset) in k}
    rows = [dict(venue=x["venue"], exchange_id=x["exchange_id"], market_id=x["market_id"],
                 mark=x["mark"], hourly=x["rate"], per_8h=fe.per_8h(x["rate"]),
                 interval=x["interval"], fine_class=x.get("fine_class"),
                 evidence=fe.venue_status(x["exchange_id"]))
            for x in r["rows"] if x["asset"] == asset]
    tr = [t for t in traces if t["asset"] == asset]
    radar = []
    venues = [x["venue"] for x in rows]
    for lv in venues:
        for sv in venues:
            if lv != sv:
                radar.append(funnel.radar(asset, leg_input(r, lv, asset),
                                          leg_input(r, sv, asset)))
    radar.sort(key=lambda x: -(x["carry_hourly"] or -1))
    first = tr[0]["first_rejection"] if tr else (
        "SINGLE_VENUE_ONLY" if any(g["status"] == "SINGLE_VENUE_ONLY" for g in groups.values())
        else "NOT_DISCOVERED")
    return dict(asset=asset, groups=groups, rows=rows, traces=tr, radar=radar,
                first_rejection=first,
                discovered=bool(groups) or bool(rows), matched=any(
                    g["status"] == "MULTI_VENUE" for g in groups.values()),
                in_candidates=bool(tr))


# ----------------------------------------------------------------------- observe
def observe(out):
    from pilot import network_guard
    from v2.venues import base, txflow, variational

    guard = network_guard.install(str(out / "guard-observation.jsonl"), v2=False,
                                  mode=observation.MODE)
    answers, now = {}, time.time()
    end_ms = int(now * 1000)

    def get(name, xid, method, url, body=None):
        try:
            answers[name] = dict(ok=True, xid=xid, method=method, url=url, body=body,
                                 at=time.time(),
                                 data=base.http_json(xid, method, url, body))
        except Exception as ex:
            answers[name] = dict(ok=False, xid=xid, method=method, url=url, body=body,
                                 at=time.time(), error="%s: %s" % (type(ex).__name__,
                                                                    str(ex)[:300]))
        return answers[name].get("data")

    hl = hlo.INFO_URL
    get("hl_native_ctx", "hyperliquid_native", "POST", hl,
        hlo.meta_and_ctxs("hyperliquid_native"))
    get("io_ctx", "entropy_io", "POST", hl, hlo.meta_and_ctxs("entropy_io"))
    get("xyz_ctx", "tradexyz", "POST", hl, {"type": "metaAndAssetCtxs", "dex": "xyz"})
    get("hl_native_book_INIT", "hyperliquid_native", "POST", hl,
        hlo.l2_book("hyperliquid_native", "INIT"))
    get("xyz_book_NATGAS", "tradexyz", "POST", hl, {"type": "l2Book", "coin": "xyz:NATGAS"})
    for name, xid, coin in (("hl_native_fh_INIT", "hyperliquid_native", "INIT"),
                            ("xyz_fh_NATGAS", "tradexyz", "xyz:NATGAS")):
        pages = []

        def fetch(body, xid=xid):
            d = base.http_json(xid, "POST", hl, body)
            pages.append(dict(body=body, n=len(d or [])))
            return d

        try:
            if xid == "tradexyz":
                rows = []
                start = end_ms - DAYS90_MS
                while True:
                    page = fetch({"type": "fundingHistory", "coin": coin, "startTime": start,
                                  "endTime": end_ms})
                    rows.extend(page or [])
                    if not page or len(page) < hlo.PAGE:
                        break
                    start = max(int(x["time"]) for x in page) + 1
                hist = hlo.parse_funding_history(rows)
            else:
                hist = hlo.history(fetch, xid, coin, end_ms - DAYS90_MS, end_ms)
            answers[name] = dict(ok=True, xid=xid, pages=pages, at=time.time(), data=hist)
        except Exception as ex:
            answers[name] = dict(ok=False, xid=xid, pages=pages, at=time.time(),
                                 error="%s: %s" % (type(ex).__name__, str(ex)[:300]))
    m, u, b = variational.stats_request()
    get("variational_stats", "variational", m, u, b)
    meta = get("txflow_meta", "txflow", *txflow.meta_request())
    idx = None
    try:
        for x in (meta.get("data") or meta)["universe"]:
            if x.get("baseCurrency") == "NATGAS":
                idx = x.get("index")
    except Exception:
        pass
    if idx is not None:
        get("txflow_ctx_NATGAS", "txflow", *txflow.ctx_request(idx))
        get("txflow_book_NATGAS", "txflow", *txflow.book_request(idx))
    get("phoenix_snapshot", "phoenix_perps", "GET",
        "https://%s/v1/exchange/snapshot" % observation.PHOENIX_HOST)
    raw = json.dumps(dict(at=now, answers=answers), default=str).encode()
    (out / "observation.json.gz").write_bytes(gzip.compress(raw))
    man = dict(created=now, sha256=hashlib.sha256(raw).hexdigest(), mode=observation.MODE,
               label="OBSERVED public market data (observation_v3, read only)",
               reads={k: v.get("ok") for k, v in answers.items()},
               guard=dict(guard.stats), by_venue=dict(guard.by_venue))
    (out / "observation_manifest.json").write_text(json.dumps(man, indent=1),
                                                    encoding="utf-8")
    return answers, man


def hl_book(raw):
    lv = (raw or {}).get("levels") or [[], []]
    return dict(bids=[(float(x["px"]), float(x["sz"])) for x in lv[0]],
                asks=[(float(x["px"]), float(x["sz"])) for x in lv[1]], model="orderbook",
                empty=not lv[0] or not lv[1], crossed=False)


def observed_story(out):
    """INIT and NATGAS from the observation_v3 reads: rates, coverage-aware funding
    history, anomalies and the radar ladder. Research only."""
    from v2.venues import variational

    raw = (out / "observation.json.gz").read_bytes()
    d = json.loads(gzip.decompress(raw))
    A, at = d["answers"], d["at"]
    res = dict(at=at, sha256=hashlib.sha256(gzip.decompress(raw)).hexdigest(),
               label="OBSERVED observation_v3 public reads; RESEARCH_ONLY")
    native = {m["base"]: m for m in hlo.parse_markets("hyperliquid_native",
                                                       A["hl_native_ctx"]["data"])}
    io = {m["base"]: m for m in hlo.parse_markets("entropy_io", A["io_ctx"]["data"])}
    var = {r["instrument"].market_id: r
           for r in variational.parse_markets(A["variational_stats"]["data"])}
    res["listings"] = dict(
        hyperliquid_native=sorted(k for k in native if k in ASSETS or k == "GAS"),
        entropy_io=sorted(k for k in io if k in ASSETS),
        variational=sorted(k for k in var if k.split("-")[0] in ASSETS),
        phoenix_perps=sorted(m["symbol"] for m in A["phoenix_snapshot"]["data"]["markets"]
                             if m["symbol"] in ASSETS) if A["phoenix_snapshot"].get("ok")
        else "READ_FAILED")
    # INIT: long Variational / short Hyperliquid native (the arb.sh direction).
    fh = A["hl_native_fh_INIT"]["data"]
    cov = fe.coverage(fh, at)
    hl_rate = native["INIT"]["funding"]["raw"]
    v = var["INIT-PERP"]
    v_rate = fe.per_hour(v["funding"]["raw"], v["funding"]["interval_s"])
    ladder = []
    for size in funnel.SIZES:
        vb = dict(variational.quote_book_for(v, size), model="rfq_indicative")
        ladder.extend(funnel.radar(
            "INIT", dict(venue="Variational", exchange_id="variational", hourly=v_rate,
                         fee=v["fee_taker"], book=vb),
            dict(venue="Hyperliquid", exchange_id="hyperliquid_native", hourly=hl_rate,
                 fee=native["INIT"]["fee_taker"],
                 book=hl_book(A["hl_native_book_INIT"]["data"])),
            size_list=(size,))["sizes"])
    res["INIT"] = dict(
        variational_hourly=v_rate, variational_8h=fe.per_8h(v_rate),
        hyperliquid_hourly=hl_rate, hyperliquid_8h=fe.per_8h(hl_rate),
        long_variational_receives_h=fe.leg_cashflow("long", v_rate),
        short_hyperliquid_receives_h=fe.leg_cashflow("short", hl_rate),
        carry_8h=fe.per_8h(fe.pair_carry(v_rate, hl_rate)),
        hl_history_coverage={k: cov[k]["label"] for k, _ in fe.WINDOWS},
        hl_history_means={k: cov[k]["mean_hourly"] for k, _ in fe.WINDOWS},
        hl_anomalies=fe.anomalies(hl_rate, [x["rate"] for x in fh], cov),
        variational_history=fe.venue_status("variational"),
        ladder=ladder, evidence=dict(variational=fe.venue_status("variational"),
                                     hyperliquid_native=fe.venue_status(
                                         "hyperliquid_native")))
    fx = A["xyz_fh_NATGAS"]["data"]
    cx = fe.coverage(fx, at)
    res["NATGAS"] = dict(
        xyz_history_coverage={k: cx[k]["label"] for k, _ in fe.WINDOWS},
        xyz_history_means={k: cx[k]["mean_hourly"] for k, _ in fe.WINDOWS},
        variational_hourly=fe.per_hour(var["NATGAS-PERP"]["funding"]["raw"],
                                       var["NATGAS-PERP"]["funding"]["interval_s"]),
        hyperliquid_native_GAS_is_not_NATGAS="native GAS has no commodity anchor; "
                                             "never matched to NATGAS (CONFLICT risk)")
    return res


def pct(x, k=4):
    return "UNKNOWN" if x is None else ("%+." + str(k) + "f%%") % (100 * x)


def research_snapshot(r, summ, stories, obs):
    """Dashboard snapshot: funnel, 11-venue matrix, NATGAS/INIT cards, radar."""
    nat, ini = stories["NATGAS"], stories["INIT"]
    top = next((t for t in nat["traces"] if t["strategy"] == "funding"), None)
    cards = [dict(asset="NATGAS", facts=[
        ("05:12 kayıt", "keşfedildi; kimlik Tier A (tradexyz+txflow+variational), "
                        "denklik: %s" % (top or {}).get("equivalence")),
        ("Aday", "funding sıralamasında; %d iz" % len(nat["traces"])),
        ("İlk ret", (top or {}).get("first_rejection")),
        ("Tüm retler", ", ".join((top or {}).get("canonical_codes") or [])),
        ("Sonuç", "%s — ekonomi hesaplanmadı" % (top or {}).get("outcome")),
        ("Sonraki adım", (top or {}).get("next_action"))])]
    facts = [("05:12 kayıt", "V2 evreninde yalnız Variational (SINGLE_VENUE_ONLY)"),
             ("Hyperliquid native", "PAPER evreni dışında: CONFIG_EXCLUDED (gözlem: evet)"),
             ("Variational bacağı", "RFQ kotasyonu: EXECUTION_MODEL_UNVERIFIED; funding "
                                    "yalnız yayımlanan oran")]
    if obs:
        i = obs["INIT"]
        facts += [("Gözlem (%s UTC)" % time.strftime("%H:%M", time.gmtime(obs["at"])),
                   "Variational %s/8s, Hyperliquid %s/8s, carry %s/8s"
                   % (pct(i["variational_8h"]), pct(i["hyperliquid_8h"]),
                      pct(i["carry_8h"]))),
                  ("HL funding geçmişi", i["hl_history_coverage"]["90d"]),
                  ("Anomali", ", ".join(i["hl_anomalies"]) or "yok")]
    cards.append(dict(asset="INIT", facts=facts))
    radar = list(nat["radar"])
    if obs:
        radar.append(dict(asset="INIT", long="Variational", short="Hyperliquid",
                          carry_8h=obs["INIT"]["carry_8h"], label="RESEARCH_ONLY",
                          sizes=obs["INIT"]["ladder"]))
    return dict(at=r["at"], rows=len(r["rows"]), safe=True, strategies={}, top={},
                open_hybrids=[], reasons_by_venue={}, funnel=summ,
                venues_v3=observation.VENUE_MATRIX, cards=cards, radar=radar)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase4", default=str(__import__("v2.paths").paths.bench("v2-phase4-2026-11-12")))
    ap.add_argument("--out", required=True)
    ap.add_argument("--observe", action="store_true")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    for p in (out, Path(a.phase4)):
        refuse_failing_disk(p)
    out.mkdir(parents=True, exist_ok=True)
    if a.observe:
        _, man = observe(out)
        print(json.dumps(man, indent=1))
        return
    r = replay(Path(a.phase4))
    traces = all_traces(r)
    summ = funnel.summary(traces)
    radar = radar_all(r, traces)
    (out / "radar_not_evaluated.json").write_text(json.dumps(dict(
        at=r["at"], snapshot_sha=r["sha"], label=funnel.RADAR_LABEL, entries=radar),
        indent=1, default=str), encoding="utf-8")
    summ["radar"] = dict(pairs=len(radar), potential=sum(1 for x in radar
                                                         if x["label"] == "POTENTIAL"),
                         traces_covered=sum(len(x["trace_ids"]) for x in radar))
    (out / "funnel.json").write_text(json.dumps(dict(
        at=r["at"], snapshot_sha=r["sha"], label="OBSERVED 05:12 snapshot replay; PAPER",
        summary=summ, traces=traces), indent=1, default=str), encoding="utf-8")
    stories = {x: asset_story(r, x, traces) for x in ASSETS}
    if (out / "observation.json.gz").exists():
        obs = observed_story(out)
        (out / "forensics_observed.json").write_text(json.dumps(obs, indent=1, default=str),
                                                     encoding="utf-8")
        print(json.dumps({k: obs[k] for k in ("listings", "INIT")}, indent=1,
                         default=str)[:4000])
    else:
        obs = None
    snap = research_snapshot(r, summ, stories, obs)
    from v2 import dashboard

    (out / "dashboard_research.json").write_text(json.dumps(snap, indent=1, default=str),
                                                 encoding="utf-8")
    (out / "dashboard_research.html").write_text(dashboard.render(snap), encoding="utf-8")
    (out / "forensics.json").write_text(json.dumps(dict(
        at=r["at"], snapshot_sha=r["sha"], stories=stories), indent=1, default=str),
        encoding="utf-8")
    print(json.dumps(summ, indent=1))
    for x in ASSETS:
        s = stories[x]
        print(x, "first:", s["first_rejection"], "traces:", len(s["traces"]),
              "groups:", s["groups"])


if __name__ == "__main__":
    main()
