r"""Short, read-only live WebSocket check (Phase 11 item 5). NOT RUN without the owner.

PAPER ONLY. It opens read-only market-data sockets for at most 300 s, records counts
and timing only (no order, no account, no signature; every frame passes
v2.ws_readonly.check_frame), and writes one small JSON report.

Refuses to start unless BOTH:
  * NB_V2_READONLY_WS=1 in the environment, and
  * --owner-approved on the command line (the owner's explicit go-ahead for this run).
Under the observation_v3 egress guard (hosts only; frames are checked here).

Per venue it measures: connection success, subscription acknowledgement (HL
subscriptionResponse, Lighter "subscribed/..."; TxFlow /ws sends none by design, so the
first data frame counts), messages by status (ok/duplicate/stale/resync/ignored/tick),
source timestamps present, apparent latency (receive - source; includes the local clock
offset, which is UNKNOWN while Windows Time is not running), disconnects/reconnects,
snapshot/delta integrity (resyncs, gaps), REST vs WS top-of-book agreement (REST read
every `rest_every` s, compared only when <= 2 s apart), API errors and HTTP 429s.

Usage (after approval):
  set NB_V2_READONLY_WS=1
  python -m tools.v2_ws_probe --owner-approved --seconds 300 --out C:\...\ws-probe-X
"""
import argparse
import collections
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2 import observation, storage, ws_readonly  # noqa: E402

MAX_SECONDS = 300.0


def plan_subscriptions(txflow_coin="119", lighter_market=None):
    """The frames the probe would send (all pass check_frame)."""
    subs = dict(
        txflow=[{"method": "subscribe", "type": t, "subscription": {"type": t,
                                                                    "coin": txflow_coin}}
                for t in ("l2BookDepth", "activeAssetCtx")],
        hyperliquid=[{"method": "subscribe", "subscription": {"type": "l2Book", "coin": c}}
                     for c in ("INIT", "xyz:NATGAS")],
    )
    if lighter_market is not None:
        subs["lighter_rh"] = [{"type": "subscribe",
                               "channel": "order_book/%d" % int(lighter_market)}]
    return subs


def rest_top(venue, market, http_json):
    """(bid, ask, receive time) from the venue's REST book, or None."""
    from v2.venues import hyperliquid_obs as hlo
    from v2.venues import lighter_rh, tradexyz, txflow

    if venue == "txflow":
        b = txflow.parse_book(http_json("txflow", *txflow.book_request(market),
                                        headers=txflow.HEADERS))
    elif venue == "hyperliquid":
        body = tradexyz.l2_book(market.split(":", 1)[1]) if market.startswith("xyz:") \
            else hlo.l2_book("hyperliquid_native", market)
        raw = http_json("tradexyz", "POST", hlo.INFO_URL, body)
        lv = (raw or {}).get("levels") or [[], []]
        b = dict(bids=[(float(x["px"]), float(x["sz"])) for x in lv[0]],
                 asks=[(float(x["px"]), float(x["sz"])) for x in lv[1]])
    else:
        b = lighter_rh.parse_book(http_json("lighter_rh", *lighter_rh.book_request(market)))
    if not b or not b.get("bids") or not b.get("asks"):
        return None
    return dict(bids=b["bids"], asks=b["asks"]), time.time()


def run_venue(venue, subs, seconds, connect=None, clock=time.time, http_json=None,
              rest_every=60.0, environ=None, sleep=time.sleep):
    """One venue for `seconds` (<= 300). Returns a metrics dict. `connect`/`http_json`
    are injected in tests (MOCK); None means the live client (needs the switches)."""
    seconds = min(float(seconds), MAX_SECONDS)
    books = ws_readonly.Books(clock)
    ws = ws_readonly.ReadOnlyWS(venue, subs, connect=connect, clock=clock, books=books,
                                allow_live=connect is None, environ=environ, sleep=sleep)
    end = clock() + seconds
    status = collections.Counter()
    comps, api_err, rate_limited = [], 0, 0
    next_rest = clock() + 5.0
    markets = sorted({(s.get("subscription") or {}).get("coin") or
                      str(s.get("channel", "")).split("/")[-1] for s in subs})
    while clock() < end:
        r = ws.step()
        status[r.get("status")] += 1
        if http_json is not None and clock() >= next_rest:
            next_rest = clock() + rest_every
            for m in markets:
                if venue == "txflow" and m is None:
                    continue
                try:
                    got = rest_top(venue, m, http_json)
                except Exception as ex:
                    api_err += 1
                    rate_limited += "429" in str(ex)
                    continue
                if got:
                    comps.append(dict(market=m, **ws_readonly.compare(
                        books.top(venue, m), got[0], got[1])))
    try:
        if ws.conn is not None:
            ws.conn.close()
    except Exception:
        pass
    recs = books.records
    acks = sum(1 for x in recs if x.get("channel") == "subscriptionResponse" or
               str(x.get("kind")) == "snapshot")
    return dict(
        venue=venue, seconds=seconds, connected=ws.stats["connects"] > 0,
        connects=ws.stats["connects"], reconnects=ws.stats["reconnects"],
        errors=ws.stats["errors"], refused_frames=ws.stats["refused"],
        subscription_ack_or_first_snapshot=acks, messages=dict(status),
        with_source_time=sum(1 for x in recs if x.get("source_ts") is not None),
        apparent_latency=books.latency_stats(venue),
        resyncs=sum(1 for x in recs if x.get("status") == "resync"),
        stale=sum(1 for x in recs if x.get("status") == "stale"),
        duplicates=sum(1 for x in recs if x.get("status") == "duplicate"),
        rest_vs_ws=dict(collections.Counter(c["status"] for c in comps)),
        rest_vs_ws_samples=comps[:10], api_errors=api_err, http_429=rate_limited,
        clock_note="apparent latency includes the local clock offset (Windows Time "
                   "service not running on 2026-11-13: offset UNKNOWN)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=float, default=300.0)
    ap.add_argument("--venues", default="txflow,hyperliquid,lighter_rh")
    ap.add_argument("--owner-approved", action="store_true")
    a = ap.parse_args()
    if not a.owner_approved or (os.environ.get(ws_readonly.ENV) or "") != "1":
        raise SystemExit("refused: needs --owner-approved AND %s=1 (owner decision)"
                         % ws_readonly.ENV)
    out = storage.check_dir(a.out)
    out.mkdir(parents=True, exist_ok=True)
    from pilot import network_guard
    from v2.venues import base, lighter_rh

    guard = network_guard.install(str(out / "guard-ws-probe.jsonl"), v2=False,
                                  mode=observation.MODE)
    lm = None
    if "lighter_rh" in a.venues:
        mk = lighter_rh.parse_markets(base.http_json("lighter_rh",
                                                     *lighter_rh.markets_request()))
        lm = next((int(m["instrument"].market_id) for m in mk
                   if (m["instrument"].base or m["instrument"].symbol) == "BTC"), None)
    subs = plan_subscriptions(lighter_market=lm)
    results, threads = {}, []
    for v in a.venues.split(","):
        if v not in subs:
            results[v] = dict(venue=v, skipped="no subscription planned")
            continue

        def go(v=v):
            try:
                results[v] = run_venue(v, subs[v], a.seconds, http_json=base.http_json)
            except Exception as ex:
                results[v] = dict(venue=v, error="%s: %s" % (type(ex).__name__,
                                                              str(ex)[:300]))
        t = threading.Thread(target=go)     # one socket per thread (single owner)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(MAX_SECONDS + 60)
    rep = dict(created=time.time(), seconds=min(a.seconds, MAX_SECONDS), results=results,
               guard=dict(guard.stats), by_venue=dict(guard.by_venue))
    (out / "ws_probe_report.json").write_text(json.dumps(rep, indent=1, default=str),
                                              encoding="utf-8")
    print(json.dumps(rep, indent=1, default=str))


if __name__ == "__main__":
    main()
