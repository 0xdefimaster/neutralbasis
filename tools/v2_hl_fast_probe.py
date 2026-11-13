r"""Hyperliquid l2Book `fast: false` vs `fast: true` (read-only, owner-approved test).

PAPER ONLY. Two read-only sockets run AT THE SAME TIME, one per mode, for at most 150 s
each (total socket time <= 300 s), subscribing INIT and xyz:NATGAS l2Book. REST l2Book
is read for both coins every `rest_every` s for comparison. One small JSON on C:.

Refuses to start unless NB_V2_READONLY_WS=1, --owner-approved AND --clock-verified
(the owner confirmed Windows Time is synchronized and the offset was re-measured).

Measured per mode and coin:
  update interval (median / P95 / max) from the venue's own `time`, receive times,
  data age = (local time + clock offset) - source time, sampled once per second
  (share of seconds the latest book was <= 2 s old: the INIT freshness gate),
  book depth per push (levels per side, USD within the book per side), compared
  5 vs 20 levels, integrity (stale / duplicate / crossed -> resync), reconnects,
  REST vs WS: REST book time minus the last WS book time (positive = REST newer),
  price agreement only where REST and WS carry the SAME book time (same state),
  HTTP 429 / errors, data loss (gaps > 3 x the median interval).
"""
import argparse
import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2 import observation, storage, ws_readonly  # noqa: E402

COINS = ("INIT", "xyz:NATGAS")
MAX_PER_MODE_S = 150.0


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def subs(fast):
    return [{"method": "subscribe", "subscription": {"type": "l2Book", "coin": c,
                                                     "fast": bool(fast)}} for c in COINS]


def run_mode(fast, seconds, connect=None, clock=time.time, sleep=time.sleep,
             offset_s=0.0, environ=None, on_tick=None):
    """One mode for `seconds` (<= 150). Returns raw per-coin observations."""
    seconds = min(float(seconds), MAX_PER_MODE_S)
    books = ws_readonly.Books(clock)
    ws = ws_readonly.ReadOnlyWS("hyperliquid", subs(fast), connect=connect, clock=clock,
                                sleep=sleep, books=books, allow_live=connect is None,
                                environ=environ)
    obs = {c: [] for c in COINS}
    ages = {c: [] for c in COINS}
    end, next_sample = clock() + seconds, clock() + 1.0
    while clock() < end:
        r = ws.step()
        if r.get("status") == "ok" and r.get("channel") == "l2Book":
            b = books.snap.get(r["market"])
            if b:
                obs[r["market"]].append(dict(
                    recv=r["recv_ts"], src=r["source_ts"], nb=len(b["bids"]), na=len(b["asks"]),
                    bid_usd=sum(p * q for p, q in b["bids"]), ask_usd=sum(p * q for p, q in b["asks"]),
                    bid=b["bids"][0][0] if b["bids"] else None,
                    ask=b["asks"][0][0] if b["asks"] else None))
        while clock() >= next_sample:
            for c in COINS:
                last = obs[c][-1]["src"] if obs[c] else None
                ages[c].append(None if last is None else clock() + offset_s - last)
            next_sample += 1.0
        if on_tick:
            on_tick(fast, books)
    try:
        if ws.conn is not None:
            ws.conn.close()
    except Exception:
        pass
    return dict(fast=fast, seconds=seconds, obs=obs, ages=ages,
                stats=dict(ws.stats), stale=books.total("hyperliquid", status="stale"),
                duplicates=books.total("hyperliquid", status="duplicate"),
                resyncs=books.total("hyperliquid", status="resync"),
                acks=books.total("hyperliquid", channel="subscriptionResponse"))


def summarize(mode, rest):
    out = {}
    for c in COINS:
        o = mode["obs"][c]
        srcs = [x["src"] for x in o if x["src"] is not None]
        iv = [b - a for a, b in zip(srcs, srcs[1:])]
        med = statistics.median(iv) if iv else None
        ag = [a for a in mode["ages"][c] if a is not None]
        rc = [r for r in rest if r["coin"] == c]
        # REST newer (+) or older (-) than the latest WS book at REST time
        lead, agree = [], []
        for r in rc:
            prior = [x for x in o if x["src"] is not None and x["recv"] <= r["recv"]]
            if not prior:
                continue
            w = prior[-1]
            lead.append(r["src"] - w["src"])
            same = [x for x in o if x["src"] == r["src"]]
            if same and same[0]["bid"] and r["bid"]:
                wm = (same[0]["bid"] + same[0]["ask"]) / 2
                rm = (r["bid"] + r["ask"]) / 2
                agree.append(abs(wm - rm) / rm * 1e4)
        out[c] = dict(
            pushes=len(o),
            interval_s=None if not iv else dict(median=med, p95=pct(iv, 0.95), max=max(iv)),
            data_loss_gaps=0 if not iv else sum(1 for x in iv if x > 3 * med),
            age_s=None if not ag else dict(median=pct(ag, 0.5), p95=pct(ag, 0.95),
                                           max=max(ag)),
            share_age_le_2s=None if not ag else sum(1 for a in ag if a <= 2.0) / len(ag),
            levels=None if not o else dict(
                bids_median=statistics.median(x["nb"] for x in o),
                asks_median=statistics.median(x["na"] for x in o)),
            depth_usd=None if not o else dict(
                bid_median=statistics.median(x["bid_usd"] for x in o),
                ask_median=statistics.median(x["ask_usd"] for x in o)),
            rest_samples=len(rc),
            rest_newer_than_ws_s=None if not lead else dict(
                median=statistics.median(lead), max=max(lead), min=min(lead),
                share_rest_newer=sum(1 for x in lead if x > 0) / len(lead)),
            same_time_price_diff_bp=agree)
    return dict(fast=mode["fast"], seconds=mode["seconds"], acks=mode["acks"],
                reconnects=mode["stats"]["reconnects"], errors=mode["stats"]["errors"],
                refused_frames=mode["stats"]["refused"], stale=mode["stale"],
                duplicates=mode["duplicates"], resyncs=mode["resyncs"], coins=out)


def rest_loop(stop, http_json, every, out, clock=time.time):
    from v2.venues import hyperliquid_obs as hlo
    from v2.venues import tradexyz

    errs = dict(errors=0, http_429=0)
    while not stop.is_set():
        for c in COINS:
            body = tradexyz.l2_book(c.split(":", 1)[1]) if c.startswith("xyz:") \
                else hlo.l2_book("hyperliquid_native", c)
            try:
                raw = http_json("tradexyz", "POST", hlo.INFO_URL, body)
                lv = (raw or {}).get("levels") or [[], []]
                out.append(dict(coin=c, recv=clock(), src=(raw or {}).get("time", 0) / 1000.0,
                                bid=float(lv[0][0]["px"]) if lv[0] else None,
                                ask=float(lv[1][0]["px"]) if lv[1] else None))
            except Exception as ex:
                errs["errors"] += 1
                errs["http_429"] += "429" in str(ex)
        stop.wait(every)
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=float, default=150.0)
    ap.add_argument("--clock-offset", type=float, required=True,
                    help="measured seconds the local clock is BEHIND NTP (0 after sync)")
    ap.add_argument("--rest-every", type=float, default=10.0)
    ap.add_argument("--owner-approved", action="store_true")
    ap.add_argument("--clock-verified", action="store_true")
    a = ap.parse_args()
    if not (a.owner_approved and a.clock_verified) or \
            (os.environ.get(ws_readonly.ENV) or "") != "1":
        raise SystemExit("refused: needs --owner-approved, --clock-verified and %s=1"
                         % ws_readonly.ENV)
    out = storage.check_dir(a.out)
    out.mkdir(parents=True, exist_ok=True)
    from pilot import network_guard
    from v2.venues import base

    guard = network_guard.install(str(out / "guard-hl-fast.jsonl"), v2=False,
                                  mode=observation.MODE)
    res, rest, stop = {}, [], threading.Event()
    rest_errs = {}

    def go(fast):
        try:
            res[fast] = run_mode(fast, a.seconds, offset_s=a.clock_offset)
        except Exception as ex:
            res[fast] = dict(error="%s: %s" % (type(ex).__name__, str(ex)[:300]))

    def rest_go():
        rest_errs.update(rest_loop(stop, base.http_json, a.rest_every, rest))

    ts = [threading.Thread(target=go, args=(f,)) for f in (False, True)]
    rt = threading.Thread(target=rest_go)
    for t in ts + [rt]:
        t.start()
    for t in ts:
        t.join(MAX_PER_MODE_S + 60)
    stop.set()
    rt.join(30)
    rep = dict(created=time.time(), clock_offset_s=a.clock_offset,
               per_mode_seconds=min(a.seconds, MAX_PER_MODE_S), rest_errors=rest_errs,
               modes={("fast" if k else "slow"): (summarize(v, rest) if "obs" in v else v)
                      for k, v in res.items()},
               guard=dict(guard.stats), by_venue=dict(guard.by_venue))
    (out / "hl_fast_report.json").write_text(json.dumps(rep, indent=1, default=str),
                                             encoding="utf-8")
    print(json.dumps(rep, indent=1, default=str))


if __name__ == "__main__":
    main()
