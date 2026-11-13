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

    src = None
    if venue == "txflow":
        raw = http_json("txflow", *txflow.book_request(market), headers=txflow.HEADERS)
        b = txflow.parse_book(raw)
        t = ((raw or {}).get("data") or {}).get("time")
        src = t / 1000.0 if t else None
    elif venue == "hyperliquid":
        body = tradexyz.l2_book(market.split(":", 1)[1]) if market.startswith("xyz:") \
            else hlo.l2_book("hyperliquid_native", market)
        raw = http_json("tradexyz", "POST", hlo.INFO_URL, body)
        src = raw["time"] / 1000.0 if (raw or {}).get("time") else None
        lv = (raw or {}).get("levels") or [[], []]
        b = dict(bids=[(float(x["px"]), float(x["sz"])) for x in lv[0]],
                 asks=[(float(x["px"]), float(x["sz"])) for x in lv[1]])
    else:
        b = lighter_rh.parse_book(http_json("lighter_rh", *lighter_rh.book_request(market)))
    if not b or not b.get("bids") or not b.get("asks"):
        return None
    # the REST book's own source time when the venue publishes one, else receive time
    return dict(bids=b["bids"], asks=b["asks"]), (src if src else time.time()), bool(src)


def run_venue(venue, subs, seconds, connect=None, clock=time.time, http_json=None,
              rest_every=60.0, environ=None, sleep=time.sleep, clock_offset_s=None):
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
                    top = books.top(venue, m)
                    basis = "SOURCE_TIME" if got[2] else "REST_RECEIVE_TIME"
                    if top is not None and top[2] is None:
                        # Lighter: the time unit of last_updated_at is NOT STATED, so the
                        # WS side is timed by its receive time (labelled)
                        last = max((x["recv_ts"] for x in books.records
                                    if x.get("market") == str(m) and x.get("status") == "ok"),
                                   default=None)
                        top, basis = (top[0], top[1], last), "WS_RECEIVE_TIME"
                    if top is not None and top[2] is not None and got[2] and \
                            abs(top[2] - got[1]) < 1e-6:
                        basis += " (same source time: same book state)"
                    comps.append(dict(market=m, time_basis=basis,
                                      **ws_readonly.compare(top, got[0], got[1])))
    try:
        if ws.conn is not None:
            ws.conn.close()
    except Exception:
        pass
    recs = books.records
    acks = books.total(venue, channel="subscriptionResponse") + books.total(
        venue, kind="snapshot", status="ok")
    per_market = {}
    for m in markets:
        pm = books.per_market.get((venue, str(m)), dict(gaps=[], lat=[], ok=0))
        gaps = pm["gaps"]
        lat = sorted(pm["lat"])
        per_market[str(m)] = dict(
            messages=books.total(venue, market=m), ok=pm["ok"],
            channels={c: n for (v, c, mm, s, k), n in books.totals.items()
                      if v == venue and mm == str(m)},
            interarrival_s=None if not gaps else dict(
                median=sorted(gaps)[len(gaps) // 2], max=max(gaps)),
            apparent_latency_s=None if not lat else dict(
                min=lat[0], median=lat[len(lat) // 2], max=lat[-1]),
            corrected_latency_s=None if not lat or clock_offset_s is None else dict(
                min=lat[0] + clock_offset_s, median=lat[len(lat) // 2] + clock_offset_s,
                max=lat[-1] + clock_offset_s))
    return dict(
        venue=venue, seconds=seconds, connected=ws.stats["connects"] > 0,
        connects=ws.stats["connects"], reconnects=ws.stats["reconnects"],
        errors=ws.stats["errors"], refused_frames=ws.stats["refused"],
        subscription_ack_or_first_snapshot=acks, messages=dict(status),
        with_source_time=sum(len(pm["lat"]) for (v, _), pm in books.per_market.items()
                             if v == venue),
        apparent_latency=books.latency_stats(venue),
        resyncs=books.total(venue, status="resync"),
        stale=books.total(venue, status="stale"),
        duplicates=books.total(venue, status="duplicate"),
        unattributed=sorted({(c, mm, s) for (v, c, mm, s, k) in books.totals
                             if v == venue and mm not in [str(x) for x in markets]
                             and c not in ("pong", "subscriptionResponse", "PONG")}),
        txflow_coin_aliases=dict(books.aliases),
        raw_samples={"%s/%s" % k: v for k, v in books.samples.items() if k[0] == venue},
        rest_vs_ws=dict(collections.Counter(c["status"] for c in comps)),
        rest_vs_ws_samples=comps[:10], api_errors=api_err, http_429=rate_limited,
        per_market=per_market, clock_offset_s=clock_offset_s,
        ingest=books.health(),     # Phase 15 P4: callback errors, metric bounds, state
        clock_note="apparent latency = receive - source; corrected = apparent + measured "
                   "offset (local clock behind NTP by clock_offset_s; Windows Time NOT "
                   "synchronized, offset measured with w32tm /stripchart)"
        if clock_offset_s is not None else "clock offset UNKNOWN: latency not reliable")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=float, default=300.0)
    ap.add_argument("--venues", default="txflow,hyperliquid,lighter_rh")
    ap.add_argument("--owner-approved", action="store_true")
    ap.add_argument("--clock-offset", type=float, default=None,
                    help="seconds the local clock is BEHIND the reference (measured)")
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
                results[v] = run_venue(v, subs[v], a.seconds, http_json=base.http_json,
                                       clock_offset_s=a.clock_offset)
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
