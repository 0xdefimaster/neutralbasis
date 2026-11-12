"""V2 Phase 3 live public smoke: identity sources, aligned prices, sample books/funding.

PAPER / research only; explicit opt-in. Runs under the V2 network guard (eight-venue
scope, payload checks). Saves every answer gzip-compressed with sha256 in a manifest;
the files are the fixtures for the venue adapter tests and the identity snapshot
(tools/v2_identity_snapshot.py). Refuses to write on E:.

Usage: python -m tools.v2_smoke_phase3 --out DIR
"""
import argparse
import concurrent.futures
import datetime
import gzip
import hashlib
import json
import sys
import time
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.v2_smoke import call  # noqa: E402

HL = "https://api.hyperliquid.xyz/info"
TX = "https://rpc.txflow.com/info"
RH = "https://api.rh.lighter.xyz/api/v1/"
RISE = "https://api.rise.trade/v1/"
ARC = "https://api.arcus.xyz/v1/"
VAR = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats"


def save(out, manifest, xid, name, method, url, body=None):
    row = dict(exchange_id=xid, name=name, method=method, url=url, body=body)
    try:
        status, raw = call(method, url, body)
        p = out / ("%s_%s.json.gz" % (xid, name))
        p.write_bytes(gzip.compress(raw))
        row.update(status=status, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                   file=p.name, at=time.time())
        manifest["responses"].append(row)
        return json.loads(raw)
    except urllib.error.HTTPError as ex:
        row.update(error="HTTP %s" % ex.code)
    except Exception as ex:
        row.update(error=(type(ex).__name__ + ": " + str(ex))[:300])
    manifest["responses"].append(row)
    print("  !", xid, name, row["error"])
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out).resolve()
    if str(out).lower().startswith("e:"):
        raise SystemExit("smoke fixtures must not be written to E:")
    out.mkdir(parents=True, exist_ok=True)
    from pilot import network_guard

    guard = network_guard.install(str(out / "guard.jsonl"), v2=True)
    m = dict(created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
             label="OBSERVED public market data (V2 phase 3 smoke)", responses=[])
    now_ms = int(time.time() * 1000)
    day_ms = 86_400_000
    # Metadata and prices, close together in time.
    save(out, m, "tradexyz", "meta_and_ctxs", "POST", HL,
         {"type": "metaAndAssetCtxs", "dex": "xyz"})
    save(out, m, "tradexyz", "perp_categories", "POST", HL, {"type": "perpCategories"})
    save(out, m, "risex", "markets", "GET", RISE + "markets")
    save(out, m, "arcus", "markets", "GET", ARC + "markets")
    rh = save(out, m, "lighter_rh", "order_book_details", "GET", RH + "orderBookDetails")
    save(out, m, "variational", "stats_v2", "GET", VAR)
    meta = save(out, m, "txflow", "perp_meta", "POST", TX,
                {"type": "perpMeta", "exchangeId": "all"})
    save(out, m, "txflow", "tags", "POST", TX, {"type": "tags"})
    save(out, m, "txflow", "market_tags", "POST", TX, {"type": "marketTags"})
    ctxs = {}
    if meta:
        coins = [str(u["index"]) for u in meta["data"]["universe"]
                 if not u.get("delisted") and not u.get("haltTrading")]

        def one(c):
            try:
                return c, call("POST", TX, {"type": "activeAssetCtx", "coin": c})[1]
            except Exception as ex:
                return c, ("ERR " + type(ex).__name__).encode()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for c, raw in pool.map(one, coins):
                ctxs[c] = raw.decode("utf-8", "replace")
        raw = json.dumps(ctxs).encode()
        p = out / "txflow_active_asset_ctx_all.json.gz"
        p.write_bytes(gzip.compress(raw))
        m["responses"].append(dict(exchange_id="txflow", name="active_asset_ctx_all",
                                   method="POST", url=TX, coins=len(coins), file=p.name,
                                   sha256=hashlib.sha256(raw).hexdigest(), at=time.time()))
    # Sample books and funding (adapter fixtures).
    btc_rh = None
    if rh:
        btc_rh = next((x["market_id"] for x in rh["order_book_details"]
                       if x["symbol"] == "BTC"), None)
    if btc_rh is not None:
        save(out, m, "lighter_rh", "book_btc", "GET",
             RH + "orderBookOrders?market_id=%d&limit=20" % btc_rh)
        save(out, m, "lighter_rh", "fundings_btc", "GET",
             RH + "fundings?market_id=%d&resolution=1h&start_timestamp=%d&end_timestamp=%d"
             "&count_back=0" % (btc_rh, now_ms - day_ms, now_ms))
    save(out, m, "risex", "book_1", "GET", RISE + "orderbook?market_id=1&limit=20")
    save(out, m, "risex", "funding_history_1", "GET",
         RISE + "markets/id/1/funding-rate-history")
    save(out, m, "arcus", "book_btc", "GET", ARC + "l2OrderBook/BTC-USD?nLevels=20")
    save(out, m, "arcus", "funding_rates", "GET", ARC + "fundingRates")
    save(out, m, "txflow", "book_1", "POST", TX, {"type": "l2Book", "coin": "1"})
    save(out, m, "tradexyz", "book_xyz100", "POST", HL, {"type": "l2Book", "coin": "xyz:XYZ100"})
    save(out, m, "tradexyz", "funding_history_xyz100", "POST", HL,
         {"type": "fundingHistory", "coin": "xyz:XYZ100", "startTime": now_ms - day_ms})
    m["guard"] = dict(stats=guard.stats, by_venue=guard.by_venue)
    (out / "manifest.json").write_text(json.dumps(m, indent=1), encoding="utf-8")
    print(json.dumps(m["guard"]))
    for r in m["responses"]:
        print(r["exchange_id"], r["name"], r.get("status"), r.get("bytes") or r.get("coins"),
              r.get("error", ""))


if __name__ == "__main__":
    main()
