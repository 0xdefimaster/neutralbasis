"""Bounded NATGAS / INIT observation stream for ESTIMATED PAPER replay (PAPER research).

Explicit opt-in (--observe), a FIXED number of cycles, then it stops: this is not the
24/7 recorder and not the engine. Public reads only, under the observation_v3 guard
(never NB_V2); nothing is traded, signed or authenticated.

Transport: REST polling. WebSocket is not used: the egress guard inspects HTTP only
(a WS client exists, extended_rfq.WebSocket, but its frames would bypass the guard);
enabling it needs a frame allowlist (docs/v2/35) and is a network-policy change that
needs the owner's review (docs/v2/31). Each cycle reads, time-critical first:
  Variational /metadata/v2/stats, xyz and native metaAndAssetCtxs, TxFlow NATGAS
  activeAssetCtx + l2Book, xyz NATGAS l2Book, native INIT l2Book
and stores every answer with its request start / receive time AND the source's own
timestamp where the venue publishes one (book `time`, TxFlow `blockTime`, Variational
quote `updated_at`; metaAndAssetCtxs has none -> receive time only, labelled).
Funding histories (xyz NATGAS, native INIT, last 3 days) are read once at the start and
once at the end, so payments that happened during the stream are known.

Robustness: a failed read is recorded (never retried in a tight loop); a venue that
fails twice in a row is skipped for an exponential back-off (v2.marketdata.Backoff);
HTTP 429 pauses the venue for the time it asks (v2.venues.base.http_json).

Phase 9:
  * --parallel: the seven time-critical reads start together (one thread each), so
    the receive times of one cycle are seconds closer; every answer still keeps its own
    request/receive/source times (nothing is given a shared timestamp).
  * Variational gets ONE try per cycle (TRIES): the Phase 8 stream lost ~5 s on each
    of 14 global-limit 429s by retrying three times; now a 429 just marks that cycle's
    answer failed and the venue pause applies.
  * Quote cache: a Variational answer whose quote `updated_at` equals the previous one
    is marked `quotes_unchanged` (cached at the venue: no new information).

Storage (v2.storage): one hash-chained JSON line per cycle in the dedicated data
folder (E:\\nb-v2-phase8 by default, owner decision 2026-11-12), plus a manifest.

Usage: python -m tools.v2_stream --observe --cycles 90 --every 20 --out E:\\nb-v2-phase8\\stream-X
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2 import observation, storage  # noqa: E402
from v2.marketdata import Backoff  # noqa: E402
from v2.venues import hyperliquid_obs as hlo  # noqa: E402
from v2.venues import tradexyz, txflow, variational  # noqa: E402

LABEL = "OBSERVED bounded public stream (observation_v3); RESEARCH / ESTIMATED PAPER only"
KEEP_VARIATIONAL = ("NATGAS-PERP", "INIT-PERP")
HISTORY_DAYS = 3


def source_time(name, data):
    """(seconds, field) of the venue's own timestamp in an answer, or (None, reason)."""
    try:
        if name in ("xyz_book_NATGAS", "native_book_INIT"):
            return data["time"] / 1000.0, "book.time"
        if name == "txflow_book_NATGAS":
            return data["data"]["time"] / 1000.0, "book.time"
        if name == "txflow_ctx_NATGAS":
            return data["data"]["blockTime"] / 1000.0, "ctx.blockTime"
    except (KeyError, TypeError):
        return None, "SOURCE_TIME_MISSING"
    return None, "SOURCE_TIME_NOT_PUBLISHED"


def slim_variational(data):
    """Keep the two listings this stream follows (the full answer is ~all markets)."""
    keep = [x for x in (data or {}).get("listings", []) if x.get("ticker") in KEEP_VARIATIONAL]
    return dict(listings=keep)


TRIES = dict(variational=1)


class Reader:
    def __init__(self, base_mod, clock=time.time, sleep=time.sleep):
        import threading

        self.base, self.clock, self.sleep = base_mod, clock, sleep
        self.backoff = {}
        self.skip_until = {}
        self.fail_streak = {}
        self.stats = dict(ok=0, failed=0, skipped=0)
        self.lock = threading.Lock()
        self.last_quote = {}

    def get(self, A, name, xid, method, url, body=None, headers=None, post=None):
        now = self.clock()
        if self.skip_until.get(xid, 0) > now:
            A[name] = dict(ok=False, xid=xid, skipped=True, t0=now, t1=now,
                           error="BACKOFF until %.0f" % self.skip_until[xid])
            self.stats["skipped"] += 1
            return None
        t0 = self.clock()
        try:
            kw = dict(tries=TRIES[xid]) if xid in TRIES else {}
            data = self.base.http_json(xid, method, url, body, headers, **kw)
            t1 = self.clock()
            if post:
                data = post(data)
            st, field = source_time(name, data)
            ans = dict(ok=True, xid=xid, t0=t0, t1=t1, source_ts=st,
                       source_field=field, data=data)
            if name == "variational_stats":
                ups = {x.get("ticker"): (x.get("quotes") or {}).get("updated_at")
                       for x in (data or {}).get("listings", [])}
                with self.lock:
                    ans["quotes_unchanged"] = sorted(
                        k for k, u in ups.items() if u and self.last_quote.get(k) == u)
                    self.last_quote.update(ups)
            A[name] = ans
            with self.lock:
                self.fail_streak[xid] = 0
                self.backoff.setdefault(xid, Backoff(2.0, 120.0)).reset()
                self.stats["ok"] += 1
            return data
        except Exception as ex:
            t1 = self.clock()
            A[name] = dict(ok=False, xid=xid, t0=t0, t1=t1,
                           error="%s: %s" % (type(ex).__name__, str(ex)[:300]))
            with self.lock:
                self.stats["failed"] += 1
                self.fail_streak[xid] = self.fail_streak.get(xid, 0) + 1
                if self.fail_streak[xid] >= 2:
                    d = self.backoff.setdefault(xid, Backoff(2.0, 120.0)).next()
                    self.skip_until[xid] = t1 + d
            return None


def requests_for(idx):
    hl = hlo.INFO_URL
    out = [("variational_stats", "variational", *variational.stats_request(), None,
            slim_variational),
           ("xyz_ctx", "tradexyz", "POST", hl, tradexyz.meta_and_ctxs(), None, None),
           ("native_ctx", "hyperliquid_native", "POST", hl,
            hlo.meta_and_ctxs("hyperliquid_native"), None, None)]
    if idx:
        out.append(("txflow_ctx_NATGAS", "txflow", *txflow.ctx_request(idx),
                    txflow.HEADERS, None))
        out.append(("txflow_book_NATGAS", "txflow", *txflow.book_request(idx),
                    txflow.HEADERS, None))
    out += [("xyz_book_NATGAS", "tradexyz", "POST", hl, tradexyz.l2_book("NATGAS"), None,
             None),
            ("native_book_INIT", "hyperliquid_native", "POST", hl,
             hlo.l2_book("hyperliquid_native", "INIT"), None, None)]
    return out


def cycle(reader, idx, parallel=False):
    """One cycle of time-critical reads, in sequence or all at once (threads)."""
    A = {}
    reqs = requests_for(idx)

    def one(r):
        name, xid, method, url, body, headers, post = r
        reader.get(A, name, xid, method, url, body=body, headers=headers, post=post)

    if parallel:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=len(reqs)) as ex:
            list(ex.map(one, reqs))
    else:
        for r in reqs:
            one(r)
    return A


def histories(reader, end_s):
    from v2.venues import base

    hl = hlo.INFO_URL
    end_ms = int(end_s * 1000)
    start_ms = end_ms - HISTORY_DAYS * 86400 * 1000
    A = {}
    for name, xid, sym in (("native_fh_INIT", "hyperliquid_native", "INIT"),
                           ("xyz_fh_NATGAS", "tradexyz", "NATGAS")):
        t0 = reader.clock()
        try:
            def fetch(b, xid=xid):
                return base.http_json(xid, "POST", hl, b)

            if xid == "tradexyz":
                rows = fetch(tradexyz.funding_history(sym, start_ms, end_ms)) or []
                hist = hlo.parse_funding_history(rows)
            else:
                hist = hlo.history(fetch, xid, sym, start_ms, end_ms)
            A[name] = dict(ok=True, xid=xid, t0=t0, t1=reader.clock(), data=hist)
        except Exception as ex:
            A[name] = dict(ok=False, xid=xid, t0=t0, t1=reader.clock(),
                           error="%s: %s" % (type(ex).__name__, str(ex)[:300]))
    return A


def observe(out, cycles, every, parallel=False):
    from pilot import network_guard
    from v2.venues import base

    out = storage.check_dir(out)
    out.mkdir(parents=True, exist_ok=True)
    guard = network_guard.install(str(out / "guard-stream.jsonl"), v2=False,
                                  mode=observation.MODE)
    log = storage.ChainLog(out / "stream.jsonl")
    reader = Reader(base)
    meta_A = {}
    meta = reader.get(meta_A, "txflow_meta", "txflow", *txflow.meta_request(),
                      headers=txflow.HEADERS)
    idx = next((str(u["index"]) for u in ((meta or {}).get("data") or meta or {})
                .get("universe", []) if u.get("baseCurrency") == "NATGAS"), None)
    started = time.time()
    log.append(dict(kind="start", at=started, label=LABEL, txflow_natgas_index=idx,
                    txflow_meta=meta_A["txflow_meta"], cycles=cycles, every_s=every,
                    histories=histories(reader, started)))
    for i in range(cycles):
        t = time.time()
        A = cycle(reader, idx, parallel)
        t1s = [a["t1"] for a in A.values() if a.get("ok")]
        log.append(dict(kind="cycle", n=i, at=t, burst_end=max(t1s) if t1s else t,
                        parallel=parallel, txflow_natgas_index=idx, answers=A))
        print("cycle %d/%d ok=%d failed=%d skipped=%d" % (i + 1, cycles, reader.stats["ok"],
                                                          reader.stats["failed"],
                                                          reader.stats["skipped"]),
              flush=True)
        if i + 1 < cycles:
            time.sleep(max(0.0, every - (time.time() - t)))
    ended = time.time()
    log.append(dict(kind="end", at=ended, histories=histories(reader, ended)))
    man = dict(created=ended, started=started, label=LABEL, mode=observation.MODE,
               parallel=parallel,
               cycles=cycles, every_s=every, reads=reader.stats, chain_head=log.last,
               lines=log.count, guard=dict(guard.stats), by_venue=dict(guard.by_venue))
    (out / "stream_manifest.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
    return man


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--observe", action="store_true")
    ap.add_argument("--cycles", type=int, default=30)
    ap.add_argument("--every", type=float, default=20.0)
    ap.add_argument("--parallel", action="store_true")
    a = ap.parse_args()
    if not a.observe:
        raise SystemExit("--observe is required (explicit opt-in to public reads)")
    if a.cycles > 240 or a.every < 10:
        raise SystemExit("bounded only: at most 240 cycles, at least 10 s apart")
    print(json.dumps(observe(Path(a.out), a.cycles, a.every, a.parallel), indent=1))


if __name__ == "__main__":
    main()
