"""Observed published funding recorder for venues with no public funding history.

PAPER / research only. Public market data, read-only, under the pilot network guard.
No account, no keys, no orders.

TxFlow (docs.txflow.com, read 2026-11-11) publishes the current funding rate per market
(activeAssetCtx nodeCtx.funding, in percent) but no public market-wide funding history.
Variational (docs.variational.io, read 2026-11-10) publishes the current funding rate,
mark price and indicative size quotes (/metadata/v2/stats) and no funding or price history.
Without a history neither venue can pass the desk's realized-funding checks, so this
tool records what they publish, every round, in its OWN database. Each row is labelled
OBSERVED_PUBLISHED: a published rate as seen at a time, never a settled payment. Nothing
here is written to a pilot or engine database.

Usage: python -m tools.funding_observer [--db PATH] [--every SECONDS] [--once]
"""
import argparse
import concurrent.futures
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_DB = Path(r"C:\Users\W10\nb-observer\observed_funding.sqlite3")
TXFLOW_INFO = "https://rpc.txflow.com/info"
VARIATIONAL_STATS = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats"
LABEL = "OBSERVED_PUBLISHED"
SCHEMA = """
create table if not exists observations(
  venue text not null, asset text not null, observed real not null,
  source_ts real, rate_raw real, rate_unit text, interval_s real, next_ts real,
  mark real, index_px real, bid_1k real, ask_1k real, label text not null,
  primary key(venue, asset, observed));
create table if not exists rounds(
  started real primary key, venue text, markets integer, errors integer,
  seconds real, note text);
"""


def post(url, body, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "NeutralBasisObserver/1.0",
                                          # The same API version the engine's feed sends.
                                          "x-api-version": "2026-08-16",
                                          "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        o = json.loads(r.read(15_000_000))
    if not o.get("success"):
        raise ValueError("TxFlow public query failed: %s" % str(o)[:200])
    return o


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "NeutralBasisObserver/1.0",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(20_000_000))


def epoch(x):
    """TxFlow times: ISO string or ms/ns number -> seconds; None if unreadable."""
    if x is None:
        return None
    try:
        v = float(x)
        while v > 1e11:
            v /= 1000.0
        return v
    except (TypeError, ValueError):
        pass
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def num(x):
    try:
        v = float(x)
        return v if v == v and abs(v) != float("inf") else None
    except (TypeError, ValueError):
        return None


def txflow_round():
    meta = post(TXFLOW_INFO, {"type": "perpMeta", "exchangeId": "all"})
    universe = [d for d in meta["data"]["universe"]
                if not d.get("haltTrading") and not d.get("delisted")]

    def one(d):
        x = post(TXFLOW_INFO, {"type": "activeAssetCtx", "coin": str(d["index"])})
        ctx = x["data"].get("nodeCtx", {})
        prev, nxt = epoch(ctx.get("prevFundingTime")), epoch(ctx.get("nextFundingTime"))
        return dict(
            venue="TxFlow", asset=d["baseCurrency"], observed=time.time(),
            source_ts=epoch(ctx.get("blockTime")),
            # Published in percent (docs: api/info/activeAssetCtx, marketTicker).
            rate_raw=num(ctx.get("funding")), rate_unit="percent per payment",
            interval_s=(nxt - prev) if prev and nxt else None, next_ts=nxt,
            mark=num(ctx.get("markPx")), index_px=num(ctx.get("oraclePx")),
            bid_1k=None, ask_1k=None)

    rows, errors = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for f in [pool.submit(one, d) for d in universe]:
            try:
                rows.append(f.result())
            except Exception:
                errors += 1
    return rows, errors


def variational_round():
    import variational_public as vp

    received = time.time()
    obj = get(VARIATIONAL_STATS)
    parsed, skipped = vp.parse(obj, received)
    rows = []
    for a, r in parsed.items():
        q = (r.get("quotes") or {}).get("size_1k") or (None, None)
        rows.append(dict(
            venue="Variational", asset=a, observed=received, source_ts=r.get("quoted_at"),
            rate_raw=r.get("rate_per_interval"), rate_unit="fraction per interval",
            interval_s=r.get("interval"), next_ts=r.get("next_funding"),
            mark=r.get("mark"), index_px=None, bid_1k=q[0], ask_1k=q[1]))
    return rows, len(skipped)


def store(db, rows):
    with db:
        db.executemany(
            "insert or ignore into observations values(:venue,:asset,:observed,:source_ts,"
            ":rate_raw,:rate_unit,:interval_s,:next_ts,:mark,:index_px,:bid_1k,:ask_1k,:label)",
            [dict(r, label=LABEL) for r in rows])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--every", type=float, default=300.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    db_path = Path(args.db).resolve()
    if str(db_path).lower().startswith("e:"):
        raise SystemExit("observer database must not be on E:")
    if not 60 <= args.every <= 3600:
        raise SystemExit("--every must be 60..3600 seconds")
    from pilot import network_guard

    db_path.parent.mkdir(parents=True, exist_ok=True)
    network_guard.install(str(db_path.parent / "network-guard.jsonl"))
    db = sqlite3.connect(str(db_path), timeout=30)
    db.executescript(SCHEMA)
    print("observer PAPER/research, public reads only ->", db_path, flush=True)
    while True:
        for venue, fn in (("TxFlow", txflow_round), ("Variational", variational_round)):
            t = time.time()
            try:
                rows, errors = fn()
                store(db, rows)
                note = None
            except Exception as ex:
                rows, errors, note = [], -1, (type(ex).__name__ + ": " + str(ex))[:300]
            with db:
                db.execute("insert or replace into rounds values(?,?,?,?,?,?)",
                           (t, venue, len(rows), errors, time.time() - t, note))
            print("%s %s markets=%d errors=%d %.1fs %s" % (
                time.strftime("%H:%M:%S"), venue, len(rows), errors, time.time() - t,
                note or ""), flush=True)
        if args.once:
            break
        time.sleep(max(5.0, args.every - (time.time() % args.every)))


if __name__ == "__main__":
    main()
