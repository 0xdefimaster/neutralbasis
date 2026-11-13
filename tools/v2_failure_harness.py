"""Replay and failure-injection harness (Phase 15 P6). Offline; every input is MOCK
(labelled) and injected into the REAL modules; no network, no exchange, PAPER only.

  python tools/v2_failure_harness.py [--out DIR]

Each scenario returns dict(name, status PASS / FAIL, invariant, detail). The invariant
is what must hold for PAPER safety (never a wrong-price fill, never a silent loss,
never a stale value counted as fresh, never look-ahead). The test suite
(test_v2_phase15_p6.py) asserts every scenario PASSes; this CLI writes the report.
"""
import argparse
import gzip
import io
import json
import shutil
import sys
import tempfile
import time
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

T0 = 1_794_484_800.0


def result(name, ok, invariant, **detail):
    return dict(name=name, status="PASS" if ok else "FAIL", invariant=invariant,
                evidence="MOCK", detail=detail)


class Tmp:
    def __enter__(self):
        self.d = Path(tempfile.mkdtemp(prefix="nb-p6-"))
        return self.d

    def __exit__(self, *a):
        shutil.rmtree(self.d, ignore_errors=True)


# ------------------------------------------------------------------ market data path
class FakeConn:
    def __init__(self, frames, fail_after=None):
        self.frames, self.sent, self.fail_after, self.n = list(frames), [], fail_after, 0

    def send(self, op, data):
        self.sent.append(data)

    def recv(self):
        self.n += 1
        if self.fail_after is not None and self.n > self.fail_after:
            raise ConnectionResetError("MOCK disconnect")
        return self.frames.pop(0)

    def close(self):
        pass


def snap(seq, px=3.0, t=T0 * 1000):
    return json.dumps({"channel": "l2BookDepth", "data": {
        "coin": "7", "type": "snapshot", "seq": seq, "time": t,
        "levels": [[{"px": str(px - 0.01), "sz": "5"}], [{"px": str(px + 0.01), "sz": "5"}]]}})


def delta(seq, prev, px=3.0, t=T0 * 1000):
    return json.dumps({"channel": "l2BookDepth", "data": {
        "coin": "7", "type": "delta", "seq": seq, "prevSeq": prev, "time": t,
        "levels": [[{"px": str(px - 0.02), "sz": "1"}], []]}})


def sc_ws_disconnect_reconnect():
    from v2 import marketdata as md
    clk = [T0]
    conns = [FakeConn([snap(1), delta(2, 1)], fail_after=2), FakeConn([snap(10)])]
    s = md.TxFlowDepthStream(["7"], connect=lambda url: conns.pop(0),
                             clock=lambda: clk[0], sleep=lambda x: None)
    s.step(), s.step()
    before = s.book("7") is not None
    s.step()                                   # disconnect
    during = s.book("7")
    s.step()                                   # reconnect + fresh snapshot
    after = s.book("7")
    ok = before and during is None and after is not None and after["seq"] == 10
    return result("ws_disconnect_reconnect", ok, "no book is served between a disconnect "
                  "and the next snapshot", stats=s.stats, book_during_outage=during)


def sc_orderbook_gap():
    from v2 import marketdata as md
    b = md.SeqBook()
    r = [b.apply(json.loads(snap(1))["data"]), b.apply(json.loads(delta(2, 1))["data"]),
         b.apply(json.loads(delta(5, 4))["data"])]
    ok = r == ["ok", "ok", "resync"] and not b.valid and b.gaps == 1
    return result("orderbook_gap", ok, "a sequence gap invalidates the book (resync), "
                  "counted as a SUSPECTED gap, never priced", results=r, gaps=b.gaps)


def sc_missing_snapshot():
    from v2 import marketdata as md, ws_readonly
    b = md.SeqBook()
    r1 = b.apply(json.loads(delta(2, 1))["data"])
    books = ws_readonly.Books(clock=lambda: T0)
    r2 = books.handle("lighter_rh", json.dumps({
        "type": "update/order_book", "channel": "order_book:1",
        "order_book": {"nonce": 5, "begin_nonce": 4, "bids": [], "asks": []}}))
    ok = r1 == "resync" and not b.valid and r2["status"] == "resync"
    return result("missing_snapshot", ok, "a delta before any snapshot is never applied",
                  txflow=r1, lighter=r2["status"])


def sc_rest_429():
    from v2 import ratelimit
    from v2.venues import base
    err = urllib.error.HTTPError("https://api.arcus.xyz/v1/markets", 429, "busy",
                                 {"Retry-After": "0"}, io.BytesIO(b"{}"))
    b = ratelimit.budget("arcus")
    with mock.patch("urllib.request.urlopen", side_effect=err), \
            mock.patch.object(b, "acquire", lambda: None), \
            mock.patch.object(b, "rate_limited", lambda ra: None):
        try:
            base.http_json("arcus", "GET", "https://api.arcus.xyz/v1/markets", tries=3)
            raised = None
        except base.RateLimited as ex:
            raised = str(ex)
    ok = raised is not None and "429 after 3 tries" in raised
    return result("rest_429", ok, "repeated 429 ends in RateLimited (no silent empty "
                  "answer, no tight retry loop)", raised=raised)


def sc_wrong_timestamp_unit():
    from v2 import marketdata as md
    from v2.control import contract
    ms_book = dict(bids=[(1, 1)], asks=[(2, 1)], received=T0 * 1000)    # ms kept as s
    row = contract.normalize_book(dict(bids=[(1, 1)], asks=[(2, 1)], ts=T0 * 1000,
                                       received=T0), "arcus", "X", evidence="MOCK")
    ok = md.stale(ms_book, "arcus", T0) and "SOURCE_TS_UNIT_SUSPECT" in contract.validate(row)
    return result("wrong_timestamp_unit", ok, "a time in ms stored as seconds is never "
                  "fresh and is flagged by the contract",
                  stale=md.stale(ms_book, "arcus", T0), problems=contract.validate(row))


def sc_clock_drift():
    from v2 import marketdata as md
    from v2.control import contract
    row = contract.normalize_book(dict(bids=[(1, 1)], asks=[(2, 1)], ts=T0 + 30,
                                       received=T0), "arcus", "X", evidence="MOCK")
    ahead = dict(bids=[(1, 1)], asks=[(2, 1)], received=T0 + 60)       # local clock behind
    ok = "SOURCE_AHEAD_OF_RECEIPT" in contract.validate(row) and md.stale(ahead, "arcus", T0)
    return result("clock_drift", ok, "a source / receive time ahead of the local clock is "
                  "flagged and never counted as fresh", problems=contract.validate(row))


def sc_cache_stale_price():
    from v2 import marketdata as md
    b = dict(bids=[(1, 1)], asks=[(2, 1)], received=T0, answer_received=T0 - 20)
    v = md.stale_views(b, "arcus", T0)
    ok = v["verdict"] == "LEGACY_FRESH_ANSWER_STALE" and md.stale(b, "arcus", T0, md.ANSWER)
    return result("cache_stale_price", ok, "a cached answer older than the limit is seen "
                  "(shadow) and refused under the answer time basis", views=v)


def sc_stale_quote():
    from v2 import rfq_paper
    rec = dict(quotes={"size_1k": (2.99, 3.01)},
               quotes_updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(T0 - 600)))
    f = rfq_paper.fill(rec, True, 1000.0, T0)
    ok = f.get("reason") == "STALE_QUOTE"
    return result("stale_quote", ok, "an RFQ quote older than its limit is never filled",
                  reason=f.get("reason"), quote_age_s=f.get("quote_age_s"))


def sc_slow_venue():
    from v2 import opportunity
    legs = [dict(price_age_s=1.0, price_time=T0), dict(price_age_s=9.0, price_time=T0 - 8)]
    fast = opportunity.discovery_freshness("price_gap", legs, sigma_hourly=0.02)
    calm = opportunity.discovery_freshness("price_gap", legs, sigma_hourly=None)
    ok = fast["status"] == "ASYNCHRONOUS" and calm["status"] == "FRESH"
    return result("slow_venue", ok, "legs read further apart than the market's volatility "
                  "allows are ASYNCHRONOUS, never a price-gap signal (unknown volatility: "
                  "the 60 s cap applies)", volatile=fast, vol_unknown=calm)


# ------------------------------------------------------------------ storage path
def sc_corrupted_jsonl():
    from v2 import storage
    with Tmp() as d:
        log = storage.ChainLog(d / "ledger.jsonl")
        for i in range(5):
            log.append(dict(kind="x", i=i))
        lines = (d / "ledger.jsonl").read_text().splitlines()
        lines[2] = lines[2].replace('"i":2', '"i":7')                  # tampered middle
        (d / "ledger.jsonl").write_text("\n".join(lines) + "\n")
        recs, rep = storage.read_chain(d / "ledger.jsonl")
        try:
            storage.ChainLog(d / "ledger.jsonl")
            refused = False
        except storage.StorageError:
            refused = True
    ok = rep["status"] == "CORRUPT" and len(recs) == 2 and refused
    return result("corrupted_jsonl", ok, "a corrupted ledger line stops reading at the "
                  "last good record and appending is refused", report=rep)


def sc_incomplete_write():
    from v2 import storage
    with Tmp() as d:
        log = storage.ChainLog(d / "ledger.jsonl")
        for i in range(3):
            log.append(dict(kind="x", i=i))
        with open(d / "ledger.jsonl", "a") as f:
            f.write('{"prev": "abc", "h": "de')                          # crash mid-write
        recs, rep = storage.read_chain(d / "ledger.jsonl")
        log2 = storage.ChainLog(d / "ledger.jsonl")                    # restart
        log2.append(dict(kind="x", i=3))
        recs2, rep2 = storage.read_chain(d / "ledger.jsonl")
    ok = rep["status"] == "TORN_TAIL" and len(recs) == 3 and rep2["status"] == "OK" and \
        len(recs2) == 4 and rep2["torn_lines"]
    return result("incomplete_write", ok, "a torn last line is isolated on restart, nothing "
                  "is deleted, the chain continues", before=rep, after=rep2)


def sc_recorder_restart():
    from v2 import recorder
    with Tmp() as d:
        seg = d / "rec" / "segments" / "20261112"
        seg.mkdir(parents=True)
        good = gzip.compress(b'{"received": 1, "x": 1}\n')
        (seg / "000000.part.jsonl.gz").write_bytes(good[:-6])         # torn gzip tail
        r = recorder.Recorder(d / "rec", wanted=lambda: {("V", "A")},
                              disk_usage=lambda p: mock.Mock(free=10 ** 12))
        man = [json.loads(x) for x in (d / "rec" / "manifest.jsonl").read_text().splitlines()]
        r.record("book", "V", "A", {"x": 1})
        r.close()
        sealed = sorted(str(p.relative_to(d / "rec" / "segments"))
                        for p in (d / "rec" / "segments").glob("*/*.jsonl.gz"))
        man = [json.loads(x) for x in (d / "rec" / "manifest.jsonl").read_text().splitlines()]
    ok = r.stats["recovered"] == 1 and man[0]["recovered"] and not man[1]["recovered"] and \
        not any(".part." in s for s in sealed) and len(sealed) == 2
    return result("recorder_restart", ok, "a leftover open segment is sealed as recovered "
                  "(never deleted), writing continues in a new segment",
                  manifest=man, sealed=sealed)


def sc_disk_full():
    from v2 import recorder
    with Tmp() as d:
        r = recorder.Recorder(d / "rec", wanted=lambda: {("V", "A")},
                              min_free_bytes=10 ** 9,
                              disk_usage=lambda p: mock.Mock(free=10 ** 6))
        ok_write = r.record("book", "V", "A", {"x": 1})
        gaps = (d / "rec" / "gaps.jsonl").read_text().splitlines()
    ok = ok_write is False and r.stats["refused_disk"] == 1 and len(gaps) == 1
    return result("disk_full", ok, "below the free-space floor nothing is written and a "
                  "gap is recorded (the disk is never filled)", stats=r.stats, gaps=gaps)


def sc_queue_overflow():
    from v2 import ws_readonly
    b = ws_readonly.Books(clock=lambda: T0, max_metric_markets=50, max_total_keys=200)
    for i in range(20000):
        b.record(venue="txflow", channel="probe", market="m%d" % i, status="ok")
    ok = len(b.records) <= 5000 and len(b.per_market) <= 50 and \
        b.total("txflow") == 20000
    return result("queue_overflow", ok, "every in-memory buffer is bounded; counts are "
                  "folded, not lost", records=len(b.records), per_market=len(b.per_market),
                  venue_total=b.total("txflow"))


# ------------------------------------------------------------------ economics path
def sc_duplicate_funding():
    from v2 import funding_expectation as fx
    from v2 import paper_ledger as L
    with Tmp() as d:
        led = L.PaperLedger(d / "led")
        led.emit("open_attempt", pos="p1", at=T0, strategy="funding", asset="X", size=1000,
                 signal=None, legs={s: dict(venue="V", model="CLOB") for s in ("long",
                                                                              "short")})
        for _ in range(3):
            led.emit("funding", pos="p1", key="funding:short:%d" % int(T0 + 3600),
                     side="short", ts=T0 + 3600, rate=1e-4, units=1, price=1, amount=0.1,
                     evidence="SETTLED_PUBLIC_RATE", basis="MOCK")
        booked = len(led.positions["p1"]["funding"])
    pts = [dict(ts=T0 - 3600 * k, rate=1e-4, kind="settled_public") for k in range(3)]
    ser = fx.settled_series(pts + pts[:2], T0)
    ok = booked == 1 and len(ser) == 3
    return result("duplicate_funding", ok, "the same funding payment is booked once; a "
                  "duplicated settled point counts once", booked=booked, series=len(ser))


def sc_look_ahead():
    from v2 import funding_expectation as fx
    past = [dict(ts=T0 - 3600 * k, rate=1e-4 + 1e-6 * (k % 5), kind="settled_public")
            for k in range(1, 120)]
    future = [dict(ts=T0 + 3600 * k, rate=5e-2, kind="settled_public") for k in range(1, 50)]
    a = fx.fit(fx.settled_series(past, T0))
    b = fx.fit(fx.settled_series(past + future, T0))
    ok = a == b
    return result("look_ahead", ok, "data after the decision time never changes a fit",
                  fit_points=a.get("points"))


class FakeObs:
    def __init__(self, at, books):
        self.at, self.books, self.times, self.rows = at, books, {}, {}

    def book(self, v, a):
        return self.books.get((v, a))

    def rfq_record(self, v, a):
        return None


def book(px, received=T0, crossed=False):
    b = dict(bids=[(px * (1.002 if crossed else 0.999), 1000.0)],
             asks=[(px * (0.998 if crossed else 1.001), 1000.0)], model="orderbook",
             received=received, empty=False, crossed=crossed)
    return b


def partial_position(led):
    legs = {"long": dict(venue="TxFlow", side="long", exchange_id="txflow", market_id="7",
                         model="CLOB", fee_rate=0.0, interval=3600),
            "short": dict(venue="tradeXYZ", side="short", exchange_id="tradexyz",
                          market_id="xyz:X", model="CLOB", fee_rate=0.0, interval=3600)}
    led.emit("open_attempt", pos="p1", at=T0 - 10, strategy="funding", asset="X",
             size=30.0, signal=None, legs=legs)
    led.emit("fill", pos="p1", side="long", at=T0 - 10, action="open", qty=10.0, price=3.0,
             fee=0.0, label="ESTIMATED_CLOB_FILL", model="CLOB_IOC")
    from v2 import paper_ledger as L
    led.emit("state", pos="p1", at=T0 - 10, state=L.PARTIAL, reason="MOCK", retries=0)
    return led.positions["p1"]


def sc_wrong_price_fill():
    """The critical one: an open position's retry must never fill from a crossed, stale
    or wrongly scaled book. Also run WITHOUT the guard, to show the old behaviour."""
    import wide_paper
    from v2 import paper_engine, paper_ledger as L
    cases = dict(crossed=book(3.0, crossed=True), stale=book(3.0, received=T0 - 120),
                 scale_x1000=book(3000.0), control_ok=book(3.0))
    out = {}
    for guarded in (True, False):
        for name, bk in cases.items():
            with Tmp() as d:
                led = L.PaperLedger(d / "led")
                p = partial_position(led)
                eng = paper_engine.PaperEngine(led, dict(wide_paper.WIDE_DEFAULTS), [])
                if not guarded:
                    eng.fill_guard = lambda *a, **k: None
                eng.retry(p, FakeObs(T0, {("tradeXYZ", "X"): bk,
                                          ("TxFlow", "X"): book(3.0)}))
                fills = [f for f in p["legs"]["short"]["fills"]]
                out["%s/%s" % ("guarded" if guarded else "no_guard", name)] = dict(
                    filled=bool(fills), price=fills[0]["price"] if fills else None)
    ok = (not any(out["guarded/" + k]["filled"] for k in ("crossed", "stale", "scale_x1000"))
          and out["guarded/control_ok"]["filled"]
          and out["no_guard/scale_x1000"]["filled"])
    return result("wrong_price_fill", ok, "an open position never fills at a crossed, "
                  "stale or wrongly scaled price (the unguarded engine did)", cases=out)


SCENARIOS = (sc_ws_disconnect_reconnect, sc_orderbook_gap, sc_missing_snapshot, sc_rest_429,
             sc_wrong_timestamp_unit, sc_clock_drift, sc_cache_stale_price, sc_stale_quote,
             sc_slow_venue, sc_corrupted_jsonl, sc_incomplete_write, sc_recorder_restart,
             sc_disk_full, sc_queue_overflow, sc_duplicate_funding, sc_look_ahead,
             sc_wrong_price_fill)


def run_all():
    out = []
    for f in SCENARIOS:
        try:
            out.append(f())
        except Exception as ex:
            out.append(result(f.__name__[3:], False, "scenario crashed",
                              error="%s: %s" % (type(ex).__name__, str(ex)[:300])))
    return out


def main(argv=None):
    from v2 import paths, storage
    from v2.control import netguard
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    out = Path(a.out) if a.out else paths.bench("v2-paper-data", "phase15-failure-harness")
    with netguard.offline() as g:
        res = run_all()
    rep = dict(generated_at=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
               evidence="MOCK inputs injected into the real modules", network_attempts=len(g),
               passed=sum(1 for r in res if r["status"] == "PASS"), total=len(res),
               scenarios=res)
    out = storage.check_dir(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    for r in res:
        print("%-26s %s  %s" % (r["name"], r["status"], r["invariant"]))
    print("%d/%d PASS, network attempts %d" % (rep["passed"], rep["total"], len(g)))
    return 0 if rep["passed"] == rep["total"] and not len(g) else 1


if __name__ == "__main__":
    sys.exit(main())
