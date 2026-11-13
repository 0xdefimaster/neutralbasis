"""V2 identity + strategy-row snapshot from the phase 3 smoke fixtures.

PAPER / research only. Offline by default: reads the fixtures of tools/v2_smoke_phase3
and prints, per venue, the markets, the markets verified in multi-venue groups, the
strategy rows produced, the venue pairs and every market's elimination reason.

--fetch-rh-fundings: demand-driven, read-only. Fetches Lighter RH's last settlements
only for the RH markets that sit in a verified multi-venue group (Standard accounts
have 60 requests/min; v2.ratelimit budget), under the V2 network guard, and stores the
answers next to the fixtures. Refuses to write on E:.

Usage: python -m tools.v2_identity_snapshot --fixtures DIR [--fetch-rh-fundings]
"""
import argparse
import gzip
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2 import source  # noqa: E402
from v2.venues import lighter_rh  # noqa: E402


def load(d, name):
    p = Path(d) / name
    return json.loads(gzip.decompress(p.read_bytes())) if p.exists() else None


def answers_from(d):
    ctx = load(d, "txflow_active_asset_ctx_all.json.gz") or {}
    ctxs = {}
    for coin, text in ctx.items():
        try:
            ctxs[coin] = json.loads(text)
        except ValueError:
            pass
    rh_f = {}
    for p in Path(d).glob("lighter_rh_fundings_m*.json.gz"):
        rh_f[p.name[len("lighter_rh_fundings_m"):-len(".json.gz")]] = load(d, p.name)
    btc = load(d, "lighter_rh_fundings_btc.json.gz")
    rh = load(d, "lighter_rh_order_book_details.json.gz")
    if btc and rh:
        m = next((x["market_id"] for x in rh["order_book_details"] if x["symbol"] == "BTC"),
                 None)
        if m is not None:
            rh_f.setdefault(str(m), btc)
    return dict(
        tradexyz=dict(meta=load(d, "tradexyz_meta_and_ctxs.json.gz"),
                      categories=load(d, "tradexyz_perp_categories.json.gz")),
        txflow=dict(meta=load(d, "txflow_perp_meta.json.gz"),
                    market_tags=load(d, "txflow_market_tags.json.gz"), ctxs=ctxs),
        risex=dict(markets=load(d, "risex_markets.json.gz")),
        arcus=dict(markets=load(d, "arcus_markets.json.gz")),
        lighter_rh=dict(markets=rh, fundings=rh_f),
        variational=dict(stats=load(d, "variational_stats_v2.json.gz")),
    )


def fetch_rh_fundings(d, records):
    from pilot import network_guard
    from v2.venues.base import http_json

    network_guard.install(str(Path(d) / "guard-rh.jsonl"), v2=True)
    wanted = [r["instrument"] for r in records.get("lighter_rh", [])
              if r["instrument"].reason is None and r["instrument"].status ==
              "IDENTITY_VERIFIED"]
    end = time.time()
    got = 0
    for inst in wanted:
        name = "lighter_rh_fundings_m%s.json.gz" % inst.market_id
        if (Path(d) / name).exists():
            continue
        _, url, _ = lighter_rh.fundings_request(inst.market_id, end - 6 * 3600, end)
        obj = http_json("lighter_rh", "GET", url)
        raw = json.dumps(obj).encode()
        (Path(d) / name).write_bytes(gzip.compress(raw))
        got += 1
    return got, len(wanted)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", required=True)
    ap.add_argument("--fetch-rh-fundings", action="store_true")
    a = ap.parse_args()
    d = Path(a.fixtures).resolve()
    if str(d).lower().startswith("e:"):
        raise SystemExit("fixtures must not be on E:")
    records = source.catalog_from(answers_from(d))
    # saved fixtures carry no fetch times: this IDENTITY report builds rows under the
    # legacy basis (labelled); the rows are never used for a time-sensitive decision
    from v2 import marketdata
    with marketdata.use_basis(marketdata.LEGACY):
        rows, groups, reasons = source.build(records)
    if a.fetch_rh_fundings:
        got, wanted = fetch_rh_fundings(d, records)
        print("Lighter RH fundings fetched: %d (wanted %d)" % (got, wanted))
        records = source.catalog_from(answers_from(d))
        with marketdata.use_basis(marketdata.LEGACY):
            rows, groups, reasons = source.build(records)
    table = source.venue_table(records, rows, groups, reasons)
    multi = {uid: g["venues"] for uid, g in groups.items() if g.get("status") == "MULTI_VENUE"}
    multi_a = {uid: g["venues_a"] for uid, g in groups.items()
               if g.get("status") == "MULTI_VENUE" and g.get("tier") == "A"}
    pairs, pairs_b = {}, {}
    for uid, venues in multi.items():
        for x, y in source.discovery.pairs(venues)[0]:
            target = pairs if uid in multi_a and x in multi_a[uid] and y in multi_a[uid] \
                else pairs_b
            target["%s~%s" % (x, y)] = target.get("%s~%s" % (x, y), 0) + 1
    shas = {p.name: hashlib.sha256(gzip.decompress(p.read_bytes())).hexdigest()
            for p in sorted(d.glob("*.json.gz"))}
    out = dict(label="OBSERVED public metadata; identity per v2.identity (fail-closed)",
               venues=table, multi_venue_assets=len(multi),
               tier_a_assets=len(multi_a), tier_b_only_assets=len(multi) - len(multi_a),
               by_class={c: sum(1 for u in multi_a if u.startswith(c + ":"))
                         for c in ("crypto", "rwa")},
               venue_pairs=dict(sorted(pairs.items(), key=lambda kv: -kv[1])),
               venue_pairs_tier_b=dict(sorted(pairs_b.items(), key=lambda kv: -kv[1])),
               strategy_rows=len(rows),
               strategy_assets=len({r["asset"] for r in rows}),
               assets=sorted(multi), fixtures_sha256=shas)
    (d / "identity_snapshot.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    for xid, t in table.items():
        print(xid, {k: t[k] for k in ("markets", "active", "verified_multi", "rows")},
              t["reasons"])
    print("multi-venue assets:", len(multi), "tier A:", len(multi_a), out["by_class"])
    print("tier A pairs:", out["venue_pairs"])
    print("tier B pairs:", out["venue_pairs_tier_b"])
    print("strategy rows:", len(rows), "assets:", out["strategy_assets"])


if __name__ == "__main__":
    main()
