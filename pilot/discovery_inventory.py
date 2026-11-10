"""Per-venue discovery funnel from real responses (read-only, PAPER ONLY, network guard):

  API listed -> active perps -> in the pilot funding matrix (parsed, with a rate) ->
  matched with another venue -> funding history recorded -> ranked candidates
  (whole list via /api/candidates, not the UI's top 40).

Filters are reported by name (category, status, symbol list, per-request budget) so a
whitelist, the workspace selection and the UI limit are never mistaken for discovery.
usage: python -m pilot.discovery_inventory OUT.json
"""
import json
import sys
import time
import urllib.parse

from pilot import opportunity_audit as oa  # installs the network guard
import variational_public as vp

PILOT = "http://127.0.0.1:18765"
VENUES = ["Hyperliquid", "LighterCore", "Lighter", "Extended", "RISEx", "Paradex", "TxFlow",
          "Arcus", "Variational"]


RAW = {}
# Same instrument names across venues that are NOT assumed equal (shown, never matched):
POSSIBLE_EQUIVALENTS = {"WTI": "CL", "XBR": "BZ"}




def listed():
    out = {}
    hl = oa.hl.load_markets()
    sw = [m for m in hl.values() if m.get("swap") and m.get("settle") == "USDC"]
    out["Hyperliquid"] = dict(listed=len(sw), active=sum(1 for m in sw if m.get("active") is not False),
                              note="ccxt swaps settled in USDC (HIP-3 deployments excluded by the engine)")
    for v, base in (("LighterCore", oa.LC), ("Lighter", "https://api.rh.lighter.xyz/api/v1/")):
        d = oa.get(base + "orderBookDetails")["order_book_details"]
        out[v] = dict(listed=len(d), active=sum(1 for x in d if x.get("status") == "active"
                                                 and x.get("market_type") == "perp"))
    e = oa.get(oa.EXT + "info/markets")["data"]
    RAW["Extended"] = e
    act = [x for x in e if x.get("status") == "ACTIVE" and x.get("type") == "PERPETUAL" and x.get("active")]
    out["Extended"] = dict(listed=len(e), active=len(act), rfq=sum(1 for x in act if x.get("isRfq")),
                           orderbook=sum(1 for x in act if not x.get("isRfq")),
                           categories=dict(sorted({c: sum(1 for x in act if x.get("category") == c)
                                                   for c in {x.get("category") for x in act}}.items())))
    r = oa.get(oa.RISE + "markets")["data"]["markets"]
    RAW["RISEx"] = r
    ra = [x for x in r if x.get("active") and x["config"].get("unlocked") and not x.get("reduce_only")]
    out["RISEx"] = dict(listed=len(r), active=len(ra), crypto=sum(1 for x in ra if x.get("category") == "crypto"),
                        rwa=sum(1 for x in ra if x.get("category") != "crypto"),
                        note="engine feed: crypto only (RWA: research layer, identity unverified)")
    v = oa.get(vp.URL)
    rows, _ = vp.parse(v, time.time())
    RAW["Variational"] = rows
    out["Variational"] = dict(listed=len(v.get("listings") or []), active=len(rows),
                              note="no engine connector; research adapter only")
    for x in ("Paradex", "TxFlow", "Arcus"):
        out[x] = dict(listed=None, active=None, note="listing counted from the pilot only (not re-queried here)")
    return out


def main():
    inv = listed()
    fm = oa.get(PILOT + "/api/funding-matrix", timeout=600)
    caps = oa.get(PILOT + "/api/capabilities", timeout=600)["venues"]
    cells = {v: set() for v in VENUES}
    for row in fm["assets"]:
        for v in row["cells"]:
            if v in cells:
                cells[v].add(row["asset"])
    matched = {v: {a for a in cells[v] if any(a in cells[o] for o in VENUES if o != v)} for v in VENUES}
    for v in VENUES:
        x = inv.setdefault(v, {})
        x["pilot_matrix"] = len(cells[v])
        x["pilot_matched_other_venue"] = len(matched[v])
        cap = caps.get(v) or {}
        x["pilot_discovery_method"] = ((cap.get("caps") or {}).get("market_discovery") or {}).get("method")
        x["pilot_blocked_funding"] = (cap.get("strategies") or {}).get("funding")
        for strat in ("funding", "convergence"):
            try:
                page = oa.get(PILOT + "/api/candidates?" + urllib.parse.urlencode(
                    dict(strategy=strat, venue=v, limit=1)), timeout=600)
                x["%s_candidates" % strat] = page["matched"]
                x["%s_total" % strat] = page["total"]
            except Exception as ex:
                x["%s_candidates" % strat] = "UNKNOWN (%s)" % type(ex).__name__
    research = research_universe(fm)
    out = dict(at=time.time(), venues=inv, research_universe=research,
               filters=dict(
                   RISEx="category == crypto (RWA excluded from the engine feed; research layer)",
                   TxFlow="legacy 15-symbol list (one request per market)",
                   Lighter="rates for the legacy 15 symbols (one request per market)",
                   Extended="all active markets; RFQ books only for streamed markets (12)",
                   Hyperliquid="selected rows: 15 symbols; universe rows: every perp (bulk funding)",
                   LighterCore="universe via native fundings, <= 40 new requests per scan",
                   UI="/api/state shows 40; /api/candidates pages the whole list"))
    json.dump(out, open(sys.argv[1], "w", encoding="utf8"), indent=1, default=str)
    print(json.dumps(out, indent=1, default=str)[:6000])


def research_universe(fm):
    """Cross-venue research pairs incl. RISEx / Extended RWA and Variational (listings
    already fetched; no extra requests). Current funding per hour with each leg's
    semantics; identity VERIFIED only for crypto with the same ticker; RWA and possible
    equivalents IDENTITY_UNVERIFIED. PAPER execution is never implied here."""
    legs = {}  # key -> {venue: dict(rate_h, semantics, kind)}
    for row in fm["assets"]:  # engine-discovered (crypto, pilot)
        for v, c in row["cells"].items():
            if v in VENUES and c[0] is not None:
                legs.setdefault(row["asset"], {})[v] = dict(rate_h=c[0], kind="crypto (engine)",
                                                            semantics="engine row")
    for m in RAW.get("RISEx", []):
        if m.get("active") and m["config"].get("unlocked") and m.get("category") != "crypto":
            a = m["underlying"].split("/")[0]
            iv = float(m["funding_interval"]) / 1e9
            legs.setdefault(a, {})["RISEx"] = dict(rate_h=float(m["current_funding_rate"]) / (iv / 3600),
                                                   kind="RWA:" + m["category"], semantics="LAST_SETTLED")
    for m in RAW.get("Extended", []):
        if m.get("status") == "ACTIVE" and m.get("category") == "RWA":
            legs.setdefault(m["assetName"], {})["Extended"] = dict(
                rate_h=float(m["marketStats"]["fundingRate"]), kind="RWA" + (" RFQ" if m.get("isRfq") else ""),
                semantics="CURRENT_INDICATIVE")
    for a, r in RAW.get("Variational", {}).items():
        if r["rate_hourly"] is not None:
            legs.setdefault(a, {})["Variational"] = dict(rate_h=r["rate_hourly"], kind="Variational " + (r["name"] or ""),
                                                         semantics=r["funding_semantics"])
    pairs = []
    for a, vs in legs.items():
        names = [(a, v) for v in vs]
        alias = POSSIBLE_EQUIVALENTS.get(a)
        if alias and alias in legs:
            names += [(alias, v) for v in legs[alias]]
        for (a1, v1), (a2, v2) in [(x, y) for i, x in enumerate(names) for y in names[i + 1:]]:
            if v1 == v2:
                continue
            l1, l2 = legs[a1][v1], legs[a2][v2]
            lo, hi = ((a1, v1, l1), (a2, v2, l2)) if l1["rate_h"] <= l2["rate_h"] else ((a2, v2, l2), (a1, v1, l1))
            rwa = any(x["kind"].startswith(("RWA", "Variational")) and "crypto" not in x["kind"] for x in (l1, l2))
            ident = ("IDENTITY_UNVERIFIED" if a1 != a2 or rwa else "SYMBOL_MATCH (crypto)")
            pairs.append(dict(asset=a1 if a1 == a2 else "%s/%s" % (a1, a2), long=lo[1], short=hi[1],
                              spread_8h=round((hi[2]["rate_h"] - lo[2]["rate_h"]) * 8, 7),
                              semantics=dict(long=lo[2]["semantics"], short=hi[2]["semantics"]),
                              kinds=dict(long=lo[2]["kind"], short=hi[2]["kind"]), identity=ident,
                              status="RESEARCH_CANDIDATE",
                              paper_execution="CLOSED" if (rwa or "Variational" in (v1, v2)) else "ENGINE_GATES"))
    pairs.sort(key=lambda x: -x["spread_8h"])
    return dict(pairs=len(pairs), rwa_or_variational=sum(1 for p in pairs if p["paper_execution"] == "CLOSED"),
                top=pairs[:60],
                references=[p for p in pairs if p["asset"].split("/")[0] in
                            ("AERO", "SOL", "XRP", "SOXL", "CL", "WTI", "MSTR", "SKHYNIX", "DRAM", "BZ", "VVV")
                            and {p["long"], p["short"]} & {"RISEx", "Extended", "Variational", "Hyperliquid"}][:80])


if __name__ == "__main__":
    main()
