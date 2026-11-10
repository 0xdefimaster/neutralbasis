"""arb.sh reference pairs vs NeutralBasis: one verdict per pair, nothing dropped.

Read-only, PAPER ONLY, inside the pilot network guard (via pilot.opportunity_audit). Uses
the engine's own public endpoints, the documented Variational fields
(variational_public), the pilot's API (rank, decision, research reasons) and the
worktree's walk-forward model. Writes recovery/pair_verdicts.json.

Metric definitions are kept apart (never compared across definitions):
  m1 current_spread_8h      (short rate - long rate) x 8 h from each leg's CURRENT rate
                            field, with that field's semantics per venue
  m2 funding_realized_7d    settled payments over the last 168 h (both legs)
  m3 historical_funding_net m2 - 4 taker fees - round-trip book cost at the size
  m4 historical_total_arb   m2 + basis change (1 h closes, entry 7 d ago) - 4 taker fees
                            (arb.sh-like; no book cost)
  m5 historical_total_size  m4 - round-trip book cost at the size (counterfactual entry)
  m6 executable_cost        4 fees + round-trip book cost at the size, per price source
  m7 forward_h              funding contribution minus costs for 24/48/72/168 h
                            (1/3/7-day agreement, weakest window, 7-day basis-sd reserve)
  m8 convergence            price-gap entry vs history: needs synchronous executable
                            prices on both legs; UNKNOWN when a leg has no history
  m9 full_position_pnl      not modelled (basis risk) -> UNKNOWN
Missing values are null with a reason, never 0.
"""
import json
import math
import statistics
import sys
import time
import urllib.parse
from pathlib import Path

from pilot import opportunity_audit as oa  # installs the network guard
import variational_public as vp
from tools import shadow_ranking as sr

SIZES = (100, 500, 1000, 5000, 10000)
MIN_NET = 0.0005  # engine wide_min_net / wide_size
OUT = Path(__file__).resolve().parent.parent / "recovery" / "pair_verdicts.json"

# Each leg: (venue, venue market). Extended names are explicit (RWA tickers differ).
REFERENCES = [
    dict(kind="funding", asset="AERO", long=("Extended", "AERO-USD"), short=("RISEx", "AERO"),
         arb=dict(spread_8h=0.000826, hist_net_10k=35)),
    dict(kind="funding", asset="AERO", long=("Extended", "AERO-USD"), short=("Hyperliquid", "AERO"),
         arb=dict(spread_8h=0.000548, hist_net_10k=15)),
    dict(kind="funding", asset="SOL", long=("RISEx", "SOL"), short=("Extended", "SOL-USD"),
         arb=dict(spread_8h=0.000456, hist_net_10k=9)),
    dict(kind="funding", asset="XRP", long=("RISEx", "XRP"), short=("Extended", "XRP-USD"),
         arb=dict(spread_8h=0.000479, hist_net_10k=6)),
    dict(kind="funding", asset="SOXL", long=("Variational", "SOXL"), short=("Extended", "SOXL-USD"),
         arb=dict(spread_8h=0.000054, hist_net_10k=18)),
    dict(kind="funding", asset="CL/WTI", long=("Variational", "CL"), short=("Extended", "WTI-USD"),
         arb=dict(spread_8h=0.000276, hist_net_10k=15)),
    dict(kind="funding", asset="CL/WTI", long=("RISEx", "CL"), short=("Extended", "WTI-USD"),
         arb=dict(spread_8h=0.000061, hist_net_10k=12)),
    dict(kind="funding", asset="MSTR", long=("Extended", "MSTR_24_5-USD"), short=("RISEx", "MSTR"),
         arb=dict(spread_8h=0.000226, hist_net_10k=9)),
    dict(kind="funding", asset="SKHYNIX", long=("RISEx", "SKHYNIX"), short=("Extended", "SKHYNIX-USD"),
         arb=dict(spread_8h=0.000356, hist_net_10k=9)),
    dict(kind="price", asset="DRAM", long=("RISEx", "DRAM"), short=("Variational", "DRAM"),
         arb=dict(entry=0.00119, vs_7d=0.00302, net_10k=7)),
    dict(kind="price", asset="SOL", long=("RISEx", "SOL"), short=("Variational", "SOL"),
         arb=dict(entry=0.00093, vs_7d=0.00136, net_10k=2)),
    dict(kind="price", asset="BZ", long=("Variational", "BZ"), short=("RISEx", "BZ"),
         arb=dict(entry=0.00137, vs_7d=0.00058, net_10k=-11)),
    dict(kind="price", asset="VVV", long=("RISEx", "VVV"), short=("Variational", "VVV"),
         arb=dict(entry=0.00106, vs_7d=0.00197, net_10k=-48)),
    dict(kind="price", asset="XRP", long=("RISEx", "XRP"), short=("Variational", "XRP"),
         arb=dict(entry=0.00083, vs_7d=0.00122, net_10k=-3)),
]
SEMANTICS = dict(
    Extended="CURRENT_INDICATIVE (market stats: 'calculated every minute'); history = applied 1 h rates (REALIZED_SETTLEMENT)",
    Hyperliquid="CURRENT_INDICATIVE (ccxt current; 'predicted' NOT FOUND in docs); history = hourly payments (REALIZED_SETTLEMENT)",
    RISEx="LAST_SETTLED (current_funding_rate = last closed period; not a forecast); history = SettleFundingRate (REALIZED_SETTLEMENT)",
    Variational="CURRENT_INDICATIVE (docs: 'Current funding rate'; next = last); no public history",
)
FEE = dict(Extended=(0.00025, "docs taker 0.025 %"), Hyperliquid=(0.00045, "docs base-tier taker 0.045 %"),
           RISEx=(0.0003, "owner-entered 0.03 % (live engine wide_fees); not verified by a venue document"),
           Variational=(0.0, "docs: no trading fees (cost is the quoted spread)"))
_var = None


def variational(asset, now):
    global _var
    if _var is None:
        obj = oa.get(vp.URL)
        _var = vp.parse(obj, time.time())[0]
    r = _var.get(asset)
    if not r:
        return dict(listed=False, why="Variational metadata/v2/stats: no perpetual listing for " + asset)
    return dict(listed=True, id=r["ticker"], kind="RFQ_OLP_INDICATIVE", category=r["name"],
                underlying=r["underlying_documented"], funding=[], closes=[], var=r,
                current_rate_h=r["rate_hourly"], books={}, history="UNKNOWN (not public)")


def leg(venue, market, now):
    if venue == "Variational":
        return variational(market, now)
    if venue == "Extended":
        ms = {x["name"]: x for x in oa.get(oa.EXT + "info/markets")["data"]}
        x = ms.get(market)
        if not x or x.get("status") != "ACTIVE":
            return dict(listed=False, why="Extended %s: %s" % (market, "not listed" if not x else x.get("status")))
        r = oa.leg("Extended", x["assetName"], now)
        r.update(category=x.get("category"), current_rate_h=float(x["marketStats"]["fundingRate"]))
        return r
    if venue == "RISEx":
        m = oa.rise_market(market)
        if not m:
            return dict(listed=False, why="RISEx v1/markets: no market " + market)
        r = oa.leg("RISEx", market, now)
        r.update(category=m.get("category"),
                 current_rate_h=float(m["current_funding_rate"]) / (float(m["funding_interval"]) / 1e9 / 3600))
        return r
    if venue == "Hyperliquid":
        r = oa.leg("Hyperliquid", market, now)
        fr = oa.hl.fetch_funding_rate(r["id"]) if r.get("listed") else {}
        r.update(category="crypto", current_rate_h=fr.get("fundingRate"))
        return r
    return dict(listed=False, why="venue not covered")


def identity(ref, L, S):
    crypto = lambda x: str(x.get("category") or "").lower() in ("crypto",)
    gaps = sr.gap_series(L.get("closes") or [], S.get("closes") or [])
    med = statistics.median(v for _, v in gaps) if gaps else None
    if L.get("var") or S.get("var"):
        v = L.get("var") or S.get("var")
        o = S if L.get("var") else L
        gap = None if not (v["mark"] and o.get("closes")) else (o["closes"][-1][1] / v["mark"] - 1)
        med = gap
    same_symbol = ref["long"][1].split("-")[0].split("_")[0] == ref["short"][1].split("-")[0].split("_")[0]
    if crypto(L) and crypto(S) and same_symbol and med is not None and abs(med) < 0.05:
        st = "VERIFIED_SYMBOL_AND_PRICE"
    elif (L.get("var") or S.get("var")) and same_symbol and med is not None and abs(med) < 0.05 \
            and ref["asset"] in ("SOL", "XRP", "VVV", "AERO"):
        st = "VERIFIED_SYMBOL_AND_PRICE"
    else:
        st = "IDENTITY_UNVERIFIED"
    return dict(status=st, price_gap_bp=None if med is None else round(med * 1e4, 1),
                categories=dict(long=L.get("category"), short=S.get("category")),
                markets=dict(long=ref["long"][1], short=ref["short"][1]),
                note=None if st.startswith("VERIFIED") else
                "different venue tickers / RWA: contract, underlying and price source not verified on both sides")


def costs_at(L, S, size, fl, fs):
    """{price source: dict(drag, total)} at `size` USD per leg; None when too thin."""
    out = {}
    lbooks = L.get("books") or {}
    sbooks = S.get("books") or {}
    if L.get("var"):
        lbooks = {"variational_indicative_quote": None}
    if S.get("var"):
        sbooks = {"variational_indicative_quote": None}
    for ln, lb in lbooks.items():
        for sn, sb in sbooks.items():
            def rt(book, var, side_first):
                if var is not None:
                    a = vp.indicative_cost(var, "buy", size)
                    b = vp.indicative_cost(var, "sell", size)
                    return None if a is None or b is None else a + b
                return sr.round_trip_drag(book, size)
            dl = rt(lb, L.get("var"), "buy")
            ds = rt(sb, S.get("var"), "sell")
            key = "%s + %s" % (ln, sn)
            out[key] = None if dl is None or ds is None else dict(
                drag=dl + ds, total=dl + ds + 2 * (fl + fs))
    return out


def main():
    now = int(time.time()) // 3600 * 3600
    pilot_rank = {}
    verdicts = []
    for ref in REFERENCES:
        (lv, lm), (sv, sm) = ref["long"], ref["short"]
        L, S = leg(lv, lm, now), leg(sv, sm, now)
        v = dict(asset=ref["asset"], strategy=ref["kind"], long_venue=lv, short_venue=sv,
                 markets=dict(long=lm, short=sm), arb_sh_reference=ref["arb"],
                 timestamp=now, source_provenance=dict(
                     long="%s public API (%s)" % (lv, L.get("id")), short="%s public API (%s)" % (sv, S.get("id")),
                     tool="pilot/pair_parity.py"))
        v["discovery_status"] = dict(long="LISTED" if L.get("listed") else "NOT_LISTED: " + str(L.get("why")),
                                     short="LISTED" if S.get("listed") else "NOT_LISTED: " + str(S.get("why")))
        if not (L.get("listed") and S.get("listed")):
            v.update(instrument_identity="UNKNOWN", normalization_status="UNKNOWN",
                     first_blocker="NOT_LISTED", paper_eligible=False, paper_position_id=None)
            verdicts.append(v)
            continue
        idn = identity(ref, L, S)
        v["instrument_identity"] = idn
        v["normalization_status"] = "NORMALIZED" if idn["status"].startswith("VERIFIED") else "IDENTITY_UNVERIFIED"
        v["funding_semantics"] = dict(long=SEMANTICS[lv], short=SEMANTICS[sv])
        rl, rs = L.get("current_rate_h"), S.get("current_rate_h")
        v["funding_current"] = dict(long_h=rl, short_h=rs, spread_8h=None if rl is None or rs is None
                                    else round((rs - rl) * 8, 7))
        fl, fs = FEE[lv][0], FEE[sv][0]
        v["fee_source"] = dict(long=FEE[lv], short=FEE[sv])
        hist = None
        if L.get("funding") and S.get("funding"):
            hist = oa.historical_7d(L, S, fl, fs, now)
            # The basis term uses unsynchronised 1 h trade closes: show how much the same
            # metric moves when the 7-day window ends 0..5 hours earlier.
            ends = [oa.historical_7d(L, S, fl, fs, now - k * 3600) for k in range(6)]
            tot = [e["net_bp"] for e in ends if e.get("net_bp") is not None]
            if tot:
                v["historical_total_window_range_bp"] = dict(
                    min=min(tot), max=max(tot), median=statistics.median(tot), windows=len(tot),
                    basis_noisy=max(tot) - min(tot) > 20)
        v["funding_realized_7d"] = None if not hist or hist.get("funding_bp") is None else hist["funding_bp"] / 1e4
        v["historical_basis_change"] = None if not hist or hist.get("price_bp") is None else hist["price_bp"] / 1e4
        v["historical_total_estimate"] = None if not hist or hist.get("net_bp") is None else hist["net_bp"] / 1e4
        v["historical_note"] = (hist or {}).get("why") or ("m4: realized funding + basis change - 4 taker fees; backward-looking"
                                                           if hist else "UNKNOWN: a leg has no public funding/price history")
        # m3 / m5 / m6 / m7 per size and price source.
        per_size = {}
        carry = sr.carry_series(L.get("funding") or [], S.get("funding") or [])
        gaps = sr.gap_series(L.get("closes") or [], S.get("closes") or [])
        for size in SIZES:
            cs = costs_at(L, S, size, fl, fs)
            entry = {}
            for src, c in cs.items():
                if c is None:
                    entry[src] = "INSUFFICIENT_LIQUIDITY (book/quote does not cover the size)"
                    continue
                e = dict(executable_cost=round(c["total"], 6), slippage=round(c["drag"], 6))
                if v["funding_realized_7d"] is not None:
                    e["historical_funding_net"] = round(v["funding_realized_7d"] - c["total"], 6)
                    e["historical_total_at_size"] = round(v["historical_total_estimate"] - c["drag"], 6)
                if carry and gaps:
                    s = sr.score(carry, gaps, now, dict(fees=2 * (fl + fs), drag=c["drag"]), price=False,
                                 funding_lag=3600)
                    if s["status"] != "UNKNOWN":
                        e["forward"] = {h: round(x["value"], 6) for h, x in s["horizons"].items()}
                        e["basis_risk_sd_7d"] = round(s["gap_sd_7d"], 6)
                        e["flip_share_7d"] = round(s["flip_share_7d"], 2)
                        e["carry_h"] = dict(d1=s["carry_1d"], d3=s["carry_3d"], d7=s["carry_7d"])
                    else:
                        e["forward"] = "UNKNOWN (1/3/7-day funding windows incomplete)"
                else:
                    e["forward"] = "UNKNOWN (a leg has no public funding history)"
                entry[src] = e
            per_size[size] = entry
        v["by_size"] = per_size
        v["notional_usd"] = list(SIZES)
        v["execution_price_source"] = sorted({k for x in per_size.values() for k in x})
        main_src = next((k for k in v["execution_price_source"] if "real_RFQ" not in k), None)
        m1000 = per_size[1000].get(main_src) if main_src else None
        fw = m1000.get("forward") if isinstance(m1000, dict) else None
        for h in (24, 48, 72, 168):
            v["forward_%dh" % h] = fw.get(h) if isinstance(fw, dict) else None
        v["slippage"] = m1000.get("slippage") if isinstance(m1000, dict) else None
        v["basis_risk"] = m1000.get("basis_risk_sd_7d") if isinstance(m1000, dict) else None
        if ref["kind"] == "price":
            conv = dict(last_trade_close_vs_mark_bp_NOT_EXECUTABLE=idn["price_gap_bp"],
                        vs_7d_avg="UNKNOWN: Variational publishes no price history (metadata/v2/stats only)",
                        note="m8 needs synchronous executable prices on both legs; research only")
            # Executable-side entry at $1000: buy the long leg, sell the short leg. RISEx: book
            # VWAP; Variational: INDICATIVE quote (not firm). Round-trip cost: 4 fees + both
            # legs' spread crossing at the same size.
            def side_px(X, side):
                if X.get("var"):
                    c = vp.indicative_cost(X["var"], side, 1000)
                    return None if c is None else X["var"]["mark"] * (1 + c if side == "buy" else 1 - c)
                b = (X.get("books") or {}).get("orderbook")
                if not b:
                    return None
                lv = sorted(b["asks"]) if side == "buy" else sorted(b["bids"], reverse=True)
                mid = (max(p for p, q in b["bids"]) + min(p for p, q in b["asks"])) / 2
                qty, left, cost = 1000 / mid, 1000 / mid, 0.0
                for p_, q_ in lv:
                    t = min(left, q_); cost += t * p_; left -= t
                    if left <= 1e-12:
                        return cost / qty
                return None
            buy, sell = side_px(L, "buy"), side_px(S, "sell")
            c1000 = per_size[1000]
            src = next(iter(c1000)) if c1000 else None
            cost = c1000[src]["executable_cost"] if src and isinstance(c1000[src], dict) else None
            if buy and sell:
                entry = math.log(sell / buy)
                conv.update(entry_executable_spread_bp=round(entry * 1e4, 1),
                            round_trip_cost_bp=None if cost is None else round(cost * 1e4, 1),
                            net_if_gap_fully_closes_bp=None if cost is None else round((entry - cost) * 1e4, 1),
                            caveat="upper bound: assumes the gap closes to zero; the normal gap is UNKNOWN")
            v["convergence"] = conv
        v["full_position_pnl"] = "UNKNOWN (basis risk not modelled; funding-only values above)"
        verdicts.append(v)
    # Engine view: rank and blockers from the pilot (whole list, not the top 40).
    for v in verdicts:
        strat = "convergence" if v["strategy"] == "price" else "funding"
        asset = v["markets"]["long"].split("-")[0] if v["long_venue"] != "RISEx" else v["markets"]["long"]
        page = None
        try:
            page = oa.get("http://127.0.0.1:18765/api/candidates?" + urllib.parse.urlencode(dict(
                strategy=strat, asset=asset, long=v["long_venue"], short=v["short_venue"], limit=5)), timeout=600)
        except Exception as ex:
            v["candidate_rank"] = None
            v["engine"] = "UNKNOWN (pilot /api/candidates unavailable: %s)" % type(ex).__name__
        if page is not None:
            row = (page.get("rows") or [None])[0]
            v["candidate_rank"] = row["rank"] if row else None
            v["engine"] = dict(total=page.get("total"), row=row, not_ranked=page.get("not_ranked"))
        v.update(first_blocker=None, secondary_blockers=[])
        blockers = []
        if "NOT_LISTED" in json.dumps(v.get("discovery_status")):
            blockers.append("NOT_LISTED")
        if v.get("normalization_status") == "IDENTITY_UNVERIFIED":
            blockers.append("IDENTITY_UNVERIFIED")
        if "Variational" in (v["long_venue"], v["short_venue"]):
            blockers.append("NO_ENGINE_CONNECTOR (Variational research adapter only; trading API unavailable)")
        if v.get("candidate_rank") is None and v.get("normalization_status") == "NORMALIZED" \
                and "Variational" not in (v["long_venue"], v["short_venue"]):
            nr = (v.get("engine") or {}).get("not_ranked") if isinstance(v.get("engine"), dict) else None
            blockers.append("NOT_RANKED_BY_ENGINE: " + "; ".join((nr or {}).get("why") or ["UNKNOWN"]))
        row = (v.get("engine") or {}).get("row") if isinstance(v.get("engine"), dict) else None
        if row and row.get("research"):
            blockers.append("RESEARCH: " + "; ".join(row["research"][:3]))
        if any("RFQ" in s for s in v.get("execution_price_source") or []) or \
                "RFQ" in json.dumps(v.get("markets")):
            pass
        if any("indicative" in s for s in v.get("execution_price_source") or []):
            blockers.append("EXECUTION_MODEL_UNVERIFIED (RFQ indicative price is not firm)")
        if row and row.get("first_code"):
            blockers.append("DEEP_CHECK: " + row["first_code"])
        f48 = v.get("forward_48h")
        if isinstance(f48, (int, float)) and f48 < MIN_NET:
            blockers.append("ECONOMICS: forward 48 h at $1000 = %.1f bp < 5 bp" % (f48 * 1e4))
        v["first_blocker"] = blockers[0] if blockers else "NONE"
        v["secondary_blockers"] = blockers[1:]
        v["evidence_confidence"] = ("LOW" if any(b.startswith(("IDENTITY", "NO_ENGINE", "EXECUTION")) for b in blockers)
                                    else "MEDIUM" if blockers else "HIGH")
        v["paper_eligible"] = bool(row and row.get("cls") in ("PAPER_EXECUTABLE", "OPENED"))
        v["paper_position_id"] = None
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(dict(generated_at=now, pilot="127.0.0.1:18765", pairs=verdicts),
                              indent=1, default=str), encoding="utf8")
    for v in verdicts:
        print("%-8s %-26s disc=%s ident=%s hist=%s f48=%s f168=%s rank=%s first=%s paper=%s" % (
            v["asset"], "%s>%s" % (v["long_venue"], v["short_venue"]),
            "OK" if "NOT_LISTED" not in json.dumps(v["discovery_status"]) else "NO",
            (v.get("instrument_identity") or {}).get("status") if isinstance(v.get("instrument_identity"), dict) else v.get("instrument_identity"),
            None if v.get("historical_total_estimate") is None else round(v["historical_total_estimate"] * 1e4, 1),
            None if not isinstance(v.get("forward_48h"), (int, float)) else round(v["forward_48h"] * 1e4, 1),
            None if not isinstance(v.get("forward_168h"), (int, float)) else round(v["forward_168h"] * 1e4, 1),
            v.get("candidate_rank"), v["first_blocker"][:60], v["paper_eligible"]))


if __name__ == "__main__":
    main()
