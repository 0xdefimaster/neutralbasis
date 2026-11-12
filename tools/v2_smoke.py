"""V2 live public smoke: one metadata read per reachable venue, under the V2 guard.

PAPER / research only; explicit opt-in (run by hand). Public reads, no keys, no
account fields. The process installs the V2 network guard first (NB_V2 scope), so
any request outside the eight venues' public read scope raises before it is sent;
the script also asserts that a CEX host and a Hyperliquid native query ARE refused.

Each response is saved gzip-compressed with its sha256 in a manifest; these files
are the fixtures for the offline discovery snapshot (tools/v2_discovery_snapshot.py).
Refuses to write on E:.

Usage: python -m tools.v2_smoke --out DIR
"""
import argparse
import datetime
import gzip
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

UA = {"User-Agent": "NeutralBasisV2Smoke/1.0 (paper research)", "Accept": "application/json"}

REQUESTS = (
    ("tradexyz", "meta_and_ctxs", "POST", "https://api.hyperliquid.xyz/info",
     {"type": "metaAndAssetCtxs", "dex": "xyz"}),
    ("tradexyz", "perp_dexs", "POST", "https://api.hyperliquid.xyz/info",
     {"type": "perpDexs"}),
    ("txflow", "perp_meta", "POST", "https://rpc.txflow.com/info",
     {"type": "perpMeta", "exchangeId": "all"}),
    ("risex", "markets", "GET", "https://api.rise.trade/v1/markets", None),
    ("arcus", "markets", "GET", "https://api.arcus.xyz/v1/markets", None),
    ("lighter_rh", "order_book_details", "GET",
     "https://api.rh.lighter.xyz/api/v1/orderBookDetails", None),
    ("variational", "stats_v2", "GET",
     "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats", None),
)
MUST_REFUSE = (
    ("cex", "GET", "https://fapi.binance.com/fapi/v1/premiumIndex", None),
    ("hyperliquid_native", "POST", "https://api.hyperliquid.xyz/info",
     {"type": "metaAndAssetCtxs"}),
    ("extended_legacy", "GET", "https://api.starknet.extended.exchange/api/v1/info/markets",
     None),
)


def call(method, url, body):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(UA)
    if data is not None:
        headers["Content-Type"] = "application/json"
    if "txflow" in url:
        headers["x-api-version"] = "2026-08-16"  # docs.txflow.com/api/
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read(40_000_000)


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
    manifest = dict(created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    label="OBSERVED public metadata (V2 smoke)", responses=[], refused=[])
    for xid, name, method, url, body in REQUESTS:
        row = dict(exchange_id=xid, name=name, method=method, url=url, body=body)
        t = time.time()
        try:
            status, raw = call(method, url, body)
            path = out / ("%s_%s.json.gz" % (xid, name))
            path.write_bytes(gzip.compress(raw))
            row.update(status=status, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                       file=path.name, seconds=round(time.time() - t, 2))
        except Exception as ex:
            row.update(error=(type(ex).__name__ + ": " + str(ex))[:300])
        manifest["responses"].append(row)
        print(xid, name, row.get("status"), row.get("bytes"), row.get("error", ""))
    for name, method, url, body in MUST_REFUSE:
        try:
            call(method, url, body)
            manifest["refused"].append(dict(name=name, refused=False))
        except network_guard.Refused as ex:
            manifest["refused"].append(dict(name=name, refused=True, why=str(ex)[:200]))
        except urllib.error.URLError as ex:
            # urllib wraps the guard's PermissionError in URLError(reason=...).
            ok = isinstance(ex.reason, network_guard.Refused)
            manifest["refused"].append(dict(name=name, refused=ok, why=str(ex.reason)[:200]))
        except Exception as ex:
            manifest["refused"].append(dict(name=name, refused=False,
                                            error=type(ex).__name__ + ": " + str(ex)[:200]))
        print("must refuse", name, manifest["refused"][-1].get("refused"))
    manifest["guard"] = dict(stats=guard.stats, by_venue=guard.by_venue)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    if not all(x["refused"] for x in manifest["refused"]):
        raise SystemExit("a request that must be refused was not refused")


if __name__ == "__main__":
    main()
