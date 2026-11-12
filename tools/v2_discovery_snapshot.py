"""Offline discovery snapshot from the V2 smoke fixtures (tools/v2_smoke.py).

PAPER / research only; no network. Parses each venue's saved metadata into
v2.identity.Instruments, then reports:
  strict      the V2 identity gate (v2.discovery.intersect): only positive evidence
  ticker_only what a naive base-ticker match would pair (NOT used for decisions; it
              measures how much identity work is left)
Asset class comes only from a venue's own metadata field (RISEx `category`, Arcus
`category`/`type`); venues without such a field stay "unknown" and therefore unverified.

Usage: python -m tools.v2_discovery_snapshot --fixtures DIR
"""
import argparse
import collections
import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2 import discovery, fees, identity  # noqa: E402
from v2.identity import Instrument  # noqa: E402


def load(d, name):
    p = Path(d) / name
    return json.loads(gzip.decompress(p.read_bytes())) if p.exists() else None


def tradexyz(d):
    x = load(d, "tradexyz_meta_and_ctxs.json.gz")
    out = []
    for u, ctx in zip(x[0]["universe"], x[1]):
        if u.get("isDelisted"):
            continue
        out.append(Instrument("tradexyz", u["name"], u["name"], asset_class="unknown",
                              quote_asset="USDC"))
    return out


def txflow(d):
    t = load(d, "txflow_perp_meta.json.gz")
    return [Instrument("txflow", str(u["index"]), u["baseCurrency"], asset_class="unknown",
                       quote_asset=u.get("quoteCurrency"))
            for u in t["data"]["universe"] if not u.get("delisted") and not u.get("haltTrading")]


def risex(d):
    out = []
    raw = load(d, "risex_markets.json.gz")
    rows = raw.get("data", raw)
    rows = rows.get("markets", rows) if isinstance(rows, dict) else rows
    for m in rows:
        if not m.get("active", True):
            continue
        cat = (m.get("category") or "unknown").lower()
        out.append(Instrument("risex", str(m["market_id"]), m["config"]["name"].split("/")[0],
                              asset_class=identity.CRYPTO if cat == "crypto" else cat,
                              quote_asset=m.get("quote_asset_symbol")))
    return out


def arcus(d):
    raw = load(d, "arcus_markets.json.gz")
    rows = raw if isinstance(raw, list) else raw.get("markets", raw.get("data", []))
    out = []
    for m in rows:
        if m.get("status") != "ONLINE":
            continue
        cat = (m.get("category") or "unknown").lower()
        ctype = identity.PERP if m.get("type") == "PERPETUAL" else (m.get("type") or "?").lower()
        out.append(Instrument("arcus", str(m["marketId"]), m["baseAsset"],
                              asset_class=identity.CRYPTO if cat == "crypto" else cat,
                              contract_type=ctype, quote_asset=m.get("quoteAsset")))
    return out


def lighter_rh(d):
    raw = load(d, "lighter_rh_order_book_details.json.gz")
    out = []
    for m in raw.get("order_book_details", []):
        if m.get("status") != "active" or m.get("market_type") != "perp":
            continue
        out.append(Instrument("lighter_rh", str(m["market_id"]), m["symbol"],
                              asset_class="unknown"))
    return out


def variational(d):
    raw = load(d, "variational_stats_v2.json.gz")
    out = []
    for m in raw.get("listings", []):
        ct = identity.PERP if m.get("contract_type") == "perpetual" else m.get("contract_type")
        out.append(Instrument("variational", m["ticker"], m["ticker"], asset_class="unknown",
                              contract_type=ct))
    return out


PARSERS = dict(tradexyz=tradexyz, txflow=txflow, risex=risex, arcus=arcus,
               lighter_rh=lighter_rh, variational=variational)


def xyz_fees(d):
    """Effective tier-0 xyz fees per asset from deployerFeeScale and growthMode."""
    x = load(d, "tradexyz_meta_and_ctxs.json.gz")
    out = {}
    for u in x[0]["universe"]:
        scale = u.get("deployerFeeScale")
        m = fees.hip3_scale(scale, u.get("growthMode") == "enabled") if scale else None
        out[u["name"]] = None if m is None else dict(taker=0.00045 * m, maker=0.00015 * m,
                                                     scale=scale, growth=u.get("growthMode"))
    return out


def snapshot(d):
    catalog = {xid: fn(d) for xid, fn in PARSERS.items()}
    groups = discovery.intersect(catalog)
    strict = discovery.summary(groups)
    multi = sorted((uid, g["venues"]) for uid, g in groups.items()
                   if g["status"] == discovery.MULTI)
    by_ticker = collections.defaultdict(set)
    for xid, rows in catalog.items():
        for i in rows:
            by_ticker[i.base].add(xid)
    naive = {b: sorted(v) for b, v in by_ticker.items() if len(v) >= 2}
    naive_pairs = set()
    for v in naive.values():
        naive_pairs.update(discovery.pairs(v)[0])
    unverified = collections.Counter(
        g["members"][0].exchange_id for g in groups.values()
        if g["status"] == identity.UNVERIFIED)
    xf = xyz_fees(d)
    takers = sorted(v["taker"] for v in xf.values() if v)
    return dict(
        label="OBSERVED public metadata; identity per v2.identity (fail-closed)",
        markets_per_venue={x: len(r) for x, r in catalog.items()},
        not_fetched=["popdex_perps (no official API found)",
                     "veranta (SDK/API contract not verified yet)"],
        strict=dict(strict, multi_venue=multi[:200]),
        unverified_per_venue=dict(unverified),
        ticker_only=dict(assets_on_2plus=len(naive), venue_pairs=len(naive_pairs),
                         examples=sorted(naive.items())[:40]),
        tradexyz_fees=dict(assets=len(xf), taker_min=takers[0] if takers else None,
                           taker_max=takers[-1] if takers else None),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", required=True)
    a = ap.parse_args()
    res = snapshot(a.fixtures)
    manifest = json.loads((Path(a.fixtures) / "manifest.json").read_text(encoding="utf-8"))
    res["fixtures_sha256"] = {r["file"]: r["sha256"] for r in manifest["responses"]
                              if r.get("file")}
    out = Path(a.fixtures) / "discovery_snapshot.json"
    out.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("markets_per_venue", "unverified_per_venue",
                                          "tradexyz_fees")}, indent=1))
    print("strict:", {k: v for k, v in res["strict"].items() if k != "multi_venue"})
    print("multi:", res["strict"]["multi_venue"][:30])
    print("ticker_only:", {k: v for k, v in res["ticker_only"].items() if k != "examples"})


if __name__ == "__main__":
    main()
