"""STRICT vs ESTIMATED PAPER on recorded data, NATGAS/INIT across aligned bursts, RFQ
positions opened and closed on different quotes, opportunity lifecycle.

PAPER / research only; offline (reads saved answers only). Nothing is traded; the
strict gate is used unchanged; estimated results are labelled and kept apart.

Inputs:
  --phase4 DIR   the 05:12 snapshot (answers.json.gz) for the all-candidate comparison
  --bursts DIR.. folders written by tools.v2_aligned --observe (aligned.json.gz)
Outputs (--out): estimated_vs_strict.json, bursts.json, rfq_simulations.json,
lifecycle.json.

Usage: python -m tools.v2_estimated --out DIR --bursts D1 D2 ...
"""
import argparse
import gzip
import hashlib
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wide_paper  # noqa: E402
from v2 import economics, estimated, gates, opportunity_log, rfq_paper  # noqa: E402
from v2 import funding_evidence as fe  # noqa: E402
from v2 import funding_semantics as fs  # noqa: E402
from v2.venues import hyperliquid_obs as hlo  # noqa: E402
from v2.venues import tradexyz, txflow, variational  # noqa: E402

from v2 import paths as _paths  # noqa: E402

PILOT_DB = str(_paths.pilot_db())
SIZES = (1000.0, 5000.0, 10000.0)


def refuse_failing_disk(path):
    if str(Path(path).resolve()).lower().startswith(("e:", "d:")):
        raise SystemExit("must not read or write on the failing disk (D:/E:)")


def cfg_at(size):
    return dict(wide_paper.WIDE_DEFAULTS, wide_size=size)


# ------------------------------------------------------------------ 05:12 snapshot
def snapshot_compare(phase4):
    from tools import v2_forensics as vf

    r = vf.replay(Path(phase4))
    col, prov, at = r["col"], r["prov"], r["at"]
    recs = {x["instrument"].market_id: x for x in col.records.get("variational", [])}
    rows = prov.rows_by()

    def rfq_record(v, a):
        row = rows.get((v, a)) or {}
        return recs.get(row.get("market_id")) if row.get("exchange_id") == "variational" \
            else None

    cfg = dict(wide_paper.WIDE_DEFAULTS)
    ev = estimated.EstimatedEvaluator(r["gate"], cfg, rfq_record)
    results = []
    for c in wide_paper.rank(r["rows"], cfg, at):
        results.append(ev.evaluate(dict(c, strategy="funding"), at, "funding"))
    pairs = set()
    for c in wide_paper.rank_spreads(r["rows"], at, cfg):
        for lv, sv in ((c["long"], c["short"]), (c["short"], c["long"])):
            pairs.add((c["asset"], lv, sv))
            results.append(ev.evaluate(dict(asset=c["asset"], long=lv, short=sv,
                                            strategy="convergence"), at, "convergence"))
    for asset, lv, sv in sorted(pairs | {(x["asset"], x["long"], x["short"])
                                         for x in results if x["strategy"] == "funding"}):
        results.append(ev.evaluate(dict(asset=asset, long=lv, short=sv), at, "hybrid"))
    quote_ages = [rfq_paper.quote_age(x, at) for x in recs.values()]
    quote_ages = [q for q in quote_ages if q is not None]
    return dict(at=at, snapshot_sha=r["sha"], summary=estimated.summary(results),
                quote_age_s=dict(median=statistics.median(quote_ages),
                                 max=max(quote_ages)) if quote_ages else None,
                natgas=[x for x in results if x["asset"] == "NATGAS"],
                init=[x for x in results if x["asset"] == "INIT"],
                entries=[x for x in results if x["status"] == estimated.ENTERED])


# ------------------------------------------------------------------ bursts
class BurstProvider:
    """rows/books/settled/closes of one aligned burst (NATGAS and INIT legs only)."""

    def __init__(self, folder, closes):
        raw = (Path(folder) / "aligned.json.gz").read_bytes()
        self.sha = hashlib.sha256(gzip.decompress(raw)).hexdigest()
        blob = json.loads(gzip.decompress(raw))
        A = blob["answers"]
        self.A = A
        t = lambda k: A[k]["t1"] if A.get(k, {}).get("ok") else None  # noqa: E731
        burst = [t(k) for k in ("variational_stats", "xyz_ctx", "native_ctx",
                                "txflow_ctx_NATGAS", "txflow_book_NATGAS",
                                "xyz_book_NATGAS", "native_book_INIT") if t(k)]
        self.at = statistics.median(burst)
        self.spread_s = max(burst) - min(burst)
        self.var = {r["instrument"].market_id: r
                    for r in variational.parse_markets(A["variational_stats"]["data"])}
        xyz = {m["instrument"].market_id: m
               for m in tradexyz.parse_markets(A["xyz_ctx"]["data"])}
        nat = {m["base"]: m for m in hlo.parse_markets("hyperliquid_native",
                                                        A["native_ctx"]["data"])}
        self.rows, self.books, self.times, self.hist, self.cl = {}, {}, {}, {}, closes
        base = dict(kind="perp", interval=3600)
        # NATGAS
        idx = blob.get("txflow_natgas_index")
        if idx and A.get("txflow_ctx_NATGAS", {}).get("ok"):
            meta = A["txflow_meta"]["data"]
            meta = meta if "data" in meta else dict(data=meta)
            tr = next(r for r in txflow.parse_markets(meta, None,
                                                      {idx: A["txflow_ctx_NATGAS"]["data"]})
                      if r["instrument"].market_id == idx)
            f = tr["funding"]
            self.rows[("TxFlow", "NATGAS")] = dict(
                base, venue="TxFlow", asset="NATGAS", exchange_id="txflow", market_id=idx,
                rate=fs.hourly(f, "txflow"),  # one unit path (Phase 15 P0-A)
                fee=tr["fee_taker"], mark=tr["mark"], fine_class="commodity")
            self.books["TxFlow", "NATGAS"] = dict(
                txflow.parse_book(A["txflow_book_NATGAS"]["data"]), received=self.at)
            self.times["TxFlow", "NATGAS"] = [t("txflow_ctx_NATGAS"), t("txflow_book_NATGAS")]
        xr = xyz["xyz:NATGAS"]
        self.rows[("tradeXYZ", "NATGAS")] = dict(
            base, venue="tradeXYZ", asset="NATGAS", exchange_id="tradexyz",
            market_id="xyz:NATGAS", rate=xr["funding"]["raw"], fee=xr["fee_taker"],
            mark=xr["mark"], fine_class="commodity")
        self.books["tradeXYZ", "NATGAS"] = self.l2(A["xyz_book_NATGAS"]["data"])
        self.times["tradeXYZ", "NATGAS"] = [t("xyz_ctx"), t("xyz_book_NATGAS")]
        if A.get("xyz_fh_NATGAS", {}).get("ok"):
            self.hist["tradeXYZ", "NATGAS"] = [dict(x, kind="settled_public")
                                               for x in A["xyz_fh_NATGAS"]["data"]]
        for mid, asset in (("NATGAS-PERP", "NATGAS"), ("INIT-PERP", "INIT")):
            r = self.var[mid]
            self.rows[("Variational", asset)] = dict(
                base, venue="Variational", asset=asset, exchange_id="variational",
                market_id=mid, rate=fe.per_hour(r["funding"]["raw"],
                                                r["funding"]["interval_s"]),
                interval=r["funding"]["interval_s"], fee=r["fee_taker"], mark=r["mark"],
                fine_class="commodity" if asset == "NATGAS" else "crypto")
            self.books["Variational", asset] = dict(
                variational.quote_book(r, "size_1k"), model="rfq_indicative",
                received=self.at)
            qa = rfq_paper.quote_age(r, t("variational_stats"))
            self.times["Variational", asset] = [t("variational_stats"),
                                                None if qa is None else
                                                t("variational_stats") - qa]
        nr = nat["INIT"]
        self.rows[("Hyperliquid", "INIT")] = dict(
            base, venue="Hyperliquid", asset="INIT", exchange_id="hyperliquid_native",
            market_id="INIT", rate=nr["funding"]["raw"], fee=nr["fee_taker"],
            mark=nr["mark"], fine_class="crypto")
        self.books["Hyperliquid", "INIT"] = self.l2(A["native_book_INIT"]["data"])
        self.times["Hyperliquid", "INIT"] = [t("native_ctx"), t("native_book_INIT")]
        if A.get("native_fh_INIT", {}).get("ok"):
            self.hist["Hyperliquid", "INIT"] = [dict(x, kind="settled_public")
                                                for x in A["native_fh_INIT"]["data"]]

    def l2(self, raw):
        lv = (raw or {}).get("levels") or [[], []]
        return dict(bids=[(float(x["px"]), float(x["sz"])) for x in lv[0]],
                    asks=[(float(x["px"]), float(x["sz"])) for x in lv[1]],
                    model="orderbook", empty=not lv[0] or not lv[1], crossed=False,
                    received=self.at)

    def rows_by(self):
        return dict(self.rows)

    def book(self, v, a):
        return self.books.get((v, a))

    def settled(self, v, a, at):
        return self.hist.get((v, a))

    def closes(self, v, a):
        return self.cl.get((v, a))

    def rth_open(self):
        return None

    def rfq_record(self, v, a):
        row = self.rows.get((v, a)) or {}
        return self.var.get(row.get("market_id")) if v == "Variational" else None

    def pair_time(self, lv, sv, asset):
        """(skew seconds, vol-based limit, basis): all leg times incl. quote time."""
        ts = [x for k in ((lv, asset), (sv, asset)) for x in self.times.get(k, [])
              if x is not None]
        skew = (max(ts) - min(ts)) if ts else None
        sig = [estimated.sigma_hourly(self.cl.get((v, asset))) for v in (lv, sv)]
        sig = [s for s in sig if s is not None]
        lim, basis = opportunity_log.freshness_limit(max(sig) if sig else None)
        return skew, lim, basis


def closes_db():
    from tools.v2_phase4_eval import pilot_closes

    return _paths.load_closes()


PAIRS = (("NATGAS", "TxFlow", "tradeXYZ"), ("NATGAS", "Variational", "tradeXYZ"),
         ("NATGAS", "TxFlow", "Variational"), ("INIT", "Variational", "Hyperliquid"))


def burst_eval(folders, closes):
    out = []
    for f in folders:
        bp = BurstProvider(f, closes)
        item = dict(folder=str(f), sha256=bp.sha, at=bp.at, burst_spread_s=bp.spread_s,
                    pairs=[])
        for asset, lv, sv in PAIRS:
            if (lv, asset) not in bp.rows or (sv, asset) not in bp.rows:
                continue
            skew, lim, basis = bp.pair_time(lv, sv, asset)
            c = dict(asset=asset, long=lv, short=sv)
            per = []
            for size in SIZES:
                gate = gates.Gate(bp, cfg_at(size))
                ev = estimated.EstimatedEvaluator(gate, cfg_at(size), bp.rfq_record)
                aligned = skew is not None and skew <= lim
                if "Hyperliquid" in (lv, sv):
                    r = ev.scenario(c, bp.at, "funding")
                else:
                    r = ev.evaluate(dict(c, strategy="funding"), bp.at, "funding")
                if not aligned and r.get("status") in (estimated.ENTERED, "SCENARIO_ONLY"):
                    r = dict(r, time_quality="TIME_MISALIGNED_FOR_VOLATILITY",
                             status_before_time_check=r["status"],
                             status=estimated.REJECTED if r["status"] == estimated.ENTERED
                             else r["status"])
                per.append(dict(size=size, **{k: r.get(k) for k in (
                    "status", "scenario_result", "first", "reasons", "strict_eligible",
                    "strict_reasons", "carry_8h", "net", "breakeven_h", "funding", "fees",
                    "spread", "reserve", "legs", "uncertainty", "assumptions",
                    "time_quality")}))
            carry = fe.pair_carry(bp.rows[(lv, asset)]["rate"], bp.rows[(sv, asset)]["rate"])
            item["pairs"].append(dict(asset=asset, long=lv, short=sv, skew_s=skew,
                                      freshness_limit_s=lim, freshness_basis=basis,
                                      time_quality="ALIGNED" if skew is not None and
                                      skew <= lim else "TIME_MISALIGNED_FOR_VOLATILITY",
                                      carry_8h_rows=fe.per_8h(carry),
                                      long_8h=fe.per_8h(bp.rows[(lv, asset)]["rate"]),
                                      short_8h=fe.per_8h(bp.rows[(sv, asset)]["rate"]),
                                      sizes=per))
        out.append(item)
    return out


# ------------------------------------------------------------------ RFQ simulations
def rfq_simulations(folders, closes):
    """Open on burst i, close on burst j > i: Variational leg on its two quotes, the
    other leg on its two books; funding from the published rates of the OPEN burst
    (averaged with the close burst's rate), labelled estimated."""
    bps = [BurstProvider(f, closes) for f in folders]
    out = []
    for asset, other in (("NATGAS", "tradeXYZ"), ("INIT", "Hyperliquid")):
        for i in range(len(bps)):
            for j in range(i + 1, len(bps)):
                o, c = bps[i], bps[j]
                for size in SIZES:
                    vo, vc = o.rfq_record("Variational", asset), c.rfq_record(
                        "Variational", asset)
                    rv = [o.rows[("Variational", asset)]["rate"],
                          c.rows[("Variational", asset)]["rate"]]
                    sim = rfq_paper.simulate(vo, vc, True, size, o.at, c.at,
                                             statistics.mean(rv))
                    if sim.get("reason"):
                        out.append(dict(asset=asset, open=o.at, close=c.at, size=size,
                                        status="NOT_SIMULATED", reason=sim["reason"]))
                        continue
                    ob, cb = o.book(other, asset), c.book(other, asset)
                    qty = sim["qty"]
                    m0, m1 = economics.mid(ob), economics.mid(cb)
                    tol = 0.001
                    e = economics.fill(ob["bids"], qty, False, m0 * (1 - tol))
                    x = economics.fill(cb["asks"], qty, True, m1 * (1 + tol))
                    if e is None or x is None:
                        out.append(dict(asset=asset, open=o.at, close=c.at, size=size,
                                        status="NOT_SIMULATED",
                                        reason="OTHER_LEG_INSUFFICIENT_LIQUIDITY",
                                        variational=sim))
                        continue
                    fee = o.rows[(other, asset)]["fee"]
                    ro = [o.rows[(other, asset)]["rate"], c.rows[(other, asset)]["rate"]]
                    hours = sim["hours"]
                    other_pnl = qty * (e - x)
                    other_fund = statistics.mean(ro) * size * hours
                    other_fees = qty * (e + x) * fee
                    net = sim["net"] + other_pnl + other_fund - other_fees
                    tq = []
                    for bp in (o, c):
                        skew, lim, _ = bp.pair_time("Variational", other, asset)
                        tq.append("ALIGNED" if skew is not None and skew <= lim
                                  else "TIME_MISALIGNED_FOR_VOLATILITY")
                    out.append(dict(
                        time_quality=dict(open=tq[0], close=tq[1]),
                        trustworthy=tq == ["ALIGNED", "ALIGNED"],
                        asset=asset, long="Variational", short=other, open=o.at,
                        close=c.at, hours=hours, size=size, status="SIMULATED",
                        label=rfq_paper.LABEL + (" + SCENARIO_ONLY (observation leg)"
                                                 if other == "Hyperliquid" else ""),
                        variational=dict(open_price=sim["open"]["price"],
                                         close_price=sim["close"]["price"],
                                         open_tier=sim["open"]["tier"],
                                         tier_status=sim["open"]["status"],
                                         price_pnl=sim["price_pnl"],
                                         funding_estimated=sim["funding_estimated"],
                                         quote_age_open_s=sim["open"]["quote_age_s"],
                                         quote_age_close_s=sim["close"]["quote_age_s"]),
                        other=dict(open_price=e, close_price=x, price_pnl=other_pnl,
                                   funding_estimated=other_fund, fees=other_fees),
                        net=net, verified=False))
    return out


# ------------------------------------------------------------------ lifecycle
def lifecycle(bursts):
    obs = []
    for b in bursts:
        for p in b["pairs"]:
            one = next((s for s in p["sizes"] if s["size"] == 1000.0), {})
            carry = p.get("carry_8h_rows")
            obs.append(dict(at=b["at"], asset=p["asset"], long=p["long"], short=p["short"],
                            strategy="funding", present=carry is not None and carry > 0,
                            advantage=carry, liquid=one.get("net") is not None,
                            time_quality=p.get("time_quality"),
                            estimated_return=one.get("net"), verified_return=None))
    return opportunity_log.lifecycle(obs), obs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--phase4", default=str(_paths.bench("v2-phase4-2026-11-12")))
    ap.add_argument("--bursts", nargs="*", default=[])
    a = ap.parse_args()
    out = Path(a.out).resolve()
    for p in [out, Path(a.phase4)] + [Path(b) for b in a.bursts]:
        refuse_failing_disk(p)
    out.mkdir(parents=True, exist_ok=True)
    snap = snapshot_compare(a.phase4)
    (out / "estimated_vs_strict.json").write_text(json.dumps(snap, indent=1, default=str),
                                                  encoding="utf-8")
    print(json.dumps(dict(summary=snap["summary"], quote_age_s=snap["quote_age_s"]),
                     indent=1, default=str))
    if a.bursts:
        closes = closes_db()
        bursts = burst_eval(a.bursts, closes)
        (out / "bursts.json").write_text(json.dumps(bursts, indent=1, default=str),
                                         encoding="utf-8")
        sims = rfq_simulations(a.bursts, closes)
        (out / "rfq_simulations.json").write_text(json.dumps(sims, indent=1, default=str),
                                                  encoding="utf-8")
        life, obs = lifecycle(bursts)
        (out / "lifecycle.json").write_text(json.dumps(dict(summary=life, observations=obs),
                                                       indent=1, default=str),
                                            encoding="utf-8")
        print(json.dumps(life, indent=1, default=str))


if __name__ == "__main__":
    main()
