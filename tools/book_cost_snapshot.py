"""Today's round-trip book cost at a notional for every leg of a pair list.

RESEARCH ONLY. Public order-book reads (GET; Hyperliquid through ccxt's public
endpoint), inside the pilot network guard. The cost is buying the notional at the asks and
selling it at the bids (VWAP, no partial fills) per $ of notional: an ASSUMPTION from a
displayed book at one moment, not a guaranteed fill and not a historical cost. Extended
RFQ markets have no firm book: they are reported as RFQ and get no cost (UNKNOWN).

Usage: python tools/book_cost_snapshot.py --pairs pairs.json --out costs.json [--usd 1000]
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pilot import opportunity_audit as oa  # noqa: E402  (installs the network guard)
from tools import shadow_ranking as sr  # noqa: E402

LIGHTER = {"Lighter": "https://api.rh.lighter.xyz/api/v1/",
           "LighterCore": "https://mainnet.zklighter.elliot.ai/api/v1/"}
_lighter_ids = {}


def lighter_id(venue, asset):
    if venue not in _lighter_ids:
        o = oa.get(LIGHTER[venue] + "orderBookDetails")
        _lighter_ids[venue] = {d.get("symbol"): d.get("market_id")
                               for d in o.get("order_book_details") or []}
    return _lighter_ids[venue].get(asset)


def book(venue, asset):
    if venue == "Extended":
        m = oa.ext_market(asset)
        if not m:
            return None, "not listed"
        if m.get("isRfq"):
            return None, "RFQ (no firm book)"
        ob = oa.get(oa.EXT + "info/markets/%s/orderbook" % m["name"])["data"]
        return dict(bids=[(float(x["price"]), float(x["qty"])) for x in ob.get("bid") or []],
                    asks=[(float(x["price"]), float(x["qty"])) for x in ob.get("ask") or []]), None
    if venue == "RISEx":
        m = oa.rise_market(asset)
        if not m:
            return None, "not listed"
        ob = oa.get(oa.RISE + "orderbook?market_id=%s&limit=250" % m["market_id"])["data"]
        return dict(bids=[(float(x["price"]), float(x["quantity"])) for x in ob["bids"]],
                    asks=[(float(x["price"]), float(x["quantity"])) for x in ob["asks"]]), None
    if venue == "Hyperliquid":
        sym = next((s for s, m in oa.hl.load_markets().items() if m.get("swap")
                    and m.get("base") == asset and m.get("settle") == "USDC"), None)
        if not sym:
            return None, "not listed"
        b = oa.hl.fetch_order_book(sym, 100)
        return dict(bids=[(p, q) for p, q, *_ in b["bids"]],
                    asks=[(p, q) for p, q, *_ in b["asks"]]), None
    if venue in LIGHTER:
        mid = lighter_id(venue, asset)
        if mid is None:
            return None, "not listed"
        o = oa.get(LIGHTER[venue] + "orderBookOrders?market_id=%s&limit=250" % mid)
        lv = lambda k: [(float(x["price"]), float(x["remaining_base_amount"]))
                        for x in o.get(k) or [] if float(x["remaining_base_amount"]) > 0]
        return dict(bids=lv("bids"), asks=lv("asks")), None
    return None, "venue not covered"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--usd", type=float, default=1000.0)
    a = ap.parse_args()
    pairs = json.loads(Path(a.pairs).read_text(encoding="utf8"))
    legs = sorted({(v, p[0]) for p in pairs for v in p[1:]})
    out = {}
    for v, asset in legs:
        try:
            b, why = book(v, asset)
            cost = None if b is None else sr.round_trip_drag(b, a.usd)
            if b is not None and cost is None:
                why = "book does not cover the size"
            out["%s|%s" % (v, asset)] = dict(round_trip_bp=None if cost is None else round(cost * 1e4, 2),
                                             why=why, at=time.time())
        except Exception as ex:
            out["%s|%s" % (v, asset)] = dict(round_trip_bp=None, why="%s: %s" % (type(ex).__name__, str(ex)[:100]),
                                             at=time.time())
        time.sleep(0.15)
    Path(a.out).write_text(json.dumps(dict(
        label="RESEARCH ONLY - one-moment public book snapshot; cost assumption, not a fill",
        usd=a.usd, legs=out), indent=1), encoding="utf8")
    known = [x["round_trip_bp"] for x in out.values() if x["round_trip_bp"] is not None]
    print(json.dumps(dict(legs=len(out), known=len(known),
                          unknown={k: x["why"] for k, x in out.items() if x["round_trip_bp"] is None}),
                     indent=1)[:3000])


if __name__ == "__main__":
    main()
