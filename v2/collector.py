"""Live V2 collection: the eight venues' public answers -> records -> strategy rows,
books and settled payments, on demand and cached.

PAPER / public reads only, every request through the venue's shared budget
(v2.venues.base.http_json) and inside v2.network_policy. Single-venue markets are kept
in the light catalog only: books, settled payments and closes are fetched only for
(venue, asset) pairs that are V2 strategy rows (tier A, verified multi-venue), so no
heavy subscription or backfill exists for them (`wanted()` says which).
"""
import threading
import time

from v2 import normalize, source
from v2.venues import arcus, base, lighter_rh, risex, tradexyz, txflow, variational

# Seconds each answer is reused before it is fetched again.
TTL = dict(meta=60.0, slow_meta=3600.0, book=5.0, settled=600.0)
def window(at, days=7):
    """(start, end) seconds of a settled-history window on whole hours, so the same
    request (and cache key) is used for a whole hour and a replay matches it."""
    end = (int(at) // 3600 + 1) * 3600
    return end - days * 86400, end


LEGACY_TO_ID = {"Lighter": "lighter_rh", "RISEx": "risex", "Arcus": "arcus",
                "TxFlow": "txflow", "Variational": "variational", "tradeXYZ": "tradexyz"}


class Collector:
    def __init__(self, transport=None, clock=time.time, mono=time.monotonic):
        # transport(exchange_id, method, url, body, headers) -> json
        self.transport = transport or (lambda x, m, u, b, h=None: base.http_json(x, m, u, b, h))
        # Only a live collector shares the process-wide Variational answer; a replay or
        # recording transport keeps its own (v2.venues.variational.stats).
        self.source = variational.LIVE if transport is None else "transport:%d" % id(transport)
        self.clock, self.mono = clock, mono
        self.lock = threading.Lock()
        self.cache = {}
        self.records = {}
        self.rows, self.groups, self.reasons = [], {}, {}
        self.by_key = {}
        # exchange_id -> stream with book(market_id) (e.g. marketdata.TxFlowDepthStream);
        # a fresh, valid stream book is used before a REST read.
        self.streams = {}
        self.sink = None  # recorder hook: sink(kind=..., exchange_id=..., ...)

    # -- cached fetch -----------------------------------------------------------------
    def get(self, key, ttl, fetch):
        with self.lock:
            hit = self.cache.get(key)
            if hit and self.clock() - hit[0] <= ttl:
                return hit[1]
        value = fetch()
        with self.lock:
            self.cache[key] = (self.clock(), value, self.mono())
        return value

    def fetched_at(self, key):
        """(wall, monotonic) time `key` was fetched, or (None, None) (Phase 15 P1:
        a cached answer keeps its own receive time; it is never re-stamped)."""
        with self.lock:
            hit = self.cache.get(key)
        return (hit[0], hit[2]) if hit else (None, None)

    # cache keys behind one market's rate (forced re-read; never a venue-wide refetch
    # for TxFlow, whose ctx answers are per market)
    RATE_KEYS = dict(tradexyz=lambda m: ["xyz:meta"], risex=lambda m: ["risex:markets"],
                     arcus=lambda m: ["arcus:markets"],
                     lighter_rh=lambda m: ["rh:markets", "rh:fund:" + str(m)],
                     txflow=lambda m: ["tx:ctx:" + str(m)])

    def invalidate_rate(self, xid, market_id):
        """Drop the cached answers behind one market's funding rate, so the next refresh
        really fetches them. Variational's shared stats answer (15 s TTL) is not
        dropped. Returns the keys dropped."""
        keys = self.RATE_KEYS.get(xid, lambda m: [])(market_id)
        with self.lock:
            return [k for k in keys if self.cache.pop(k, None) is not None]

    def venue_received(self):
        """{exchange_id: OLDEST fetch time of the answers its records come from}
        (conservative: a row is as old as its oldest input)."""
        with self.lock:
            items = list(self.cache.items())
        prefix = dict(tradexyz=("xyz:meta",), risex=("risex:markets",),
                      arcus=("arcus:markets",), lighter_rh=("rh:markets", "rh:fund:"),
                      txflow=("tx:ctx:",))
        out = {}
        for xid, pre in prefix.items():
            ts = [v[0] for k, v in items if any(k == p or (p.endswith(":") and
                                                           k.startswith(p)) for p in pre)]
            if ts:
                out[xid] = min(ts)
        age = variational.stats_age(source=self.source)
        if age is not None:
            out["variational"] = self.clock() - age
        return out

    def call(self, xid, req, headers=None):
        """One public read. With a `sink` (the recorder), every raw answer and every API
        error is handed over with its provenance: venue, method, URL, body hash,
        receive time and latency."""
        import hashlib
        import json as _json

        method, url, body = req
        started = self.clock()
        source = dict(method=method, url=url,
                      body_sha1=hashlib.sha1(_json.dumps(body, sort_keys=True).encode())
                      .hexdigest()[:16] if body is not None else None,
                      body=body)
        sink = getattr(self, "sink", None)
        try:
            obj = self.transport(xid, method, url, body, headers)
        except Exception as ex:
            if sink:
                sink(kind="api_error", exchange_id=xid, source=source, received=self.clock(),
                     latency_s=self.clock() - started,
                     payload=dict(error=type(ex).__name__, message=str(ex)[:300]))
            raise
        if sink:
            sink(kind="raw", exchange_id=xid, source=source, received=self.clock(),
                 latency_s=self.clock() - started, payload=obj)
        return obj

    # -- catalog ------------------------------------------------------------------------
    def answers(self):
        tx = txflow.HEADERS
        a = dict(
            tradexyz=dict(
                meta=self.get("xyz:meta", TTL["meta"], lambda: self.call(
                    "tradexyz", ("POST", tradexyz.INFO_URL, tradexyz.meta_and_ctxs()))),
                categories=self.get("xyz:cats", TTL["slow_meta"], lambda: self.call(
                    "tradexyz", ("POST", tradexyz.INFO_URL, tradexyz.categories_request())))),
            risex=dict(markets=self.get("risex:markets", TTL["meta"], lambda: self.call(
                "risex", risex.markets_request()))),
            arcus=dict(markets=self.get("arcus:markets", TTL["meta"], lambda: self.call(
                "arcus", arcus.markets_request()))),
            lighter_rh=dict(markets=self.get("rh:markets", TTL["meta"], lambda: self.call(
                "lighter_rh", lighter_rh.markets_request())), fundings={}),
            variational=dict(stats=variational.stats(
                lambda: self.call("variational", variational.stats_request()),
                source=self.source)[0]),
        )
        meta = self.get("tx:meta", TTL["slow_meta"], lambda: self.call(
            "txflow", txflow.meta_request(), tx))
        tags = self.get("tx:tags", TTL["slow_meta"], lambda: self.call(
            "txflow", txflow.tags_request(), tx))
        ctxs = {}
        for u in meta["data"]["universe"]:
            if u.get("delisted") or u.get("haltTrading"):
                continue
            coin = str(u["index"])
            try:
                ctxs[coin] = self.get("tx:ctx:" + coin, TTL["meta"], lambda c=coin: self.call(
                    "txflow", txflow.ctx_request(c), tx))
            except Exception:
                continue
        a["txflow"] = dict(meta=meta, market_tags=tags, ctxs=ctxs)
        return a

    def refresh(self, registry=None):
        """Fetch, verify and build rows; Lighter RH's current funding is read only for
        its verified multi-venue markets (demand-driven)."""
        answers = self.answers()
        records = source.catalog_from(answers)
        rows, groups, reasons = source.build(records, registry, at=self.clock())
        rh_wanted = [r["instrument"].market_id for r in records.get("lighter_rh", [])
                     if r["instrument"].reason is None]
        fundings = {}
        for mid in rh_wanted:
            try:
                fundings[mid] = self.get("rh:fund:" + mid, TTL["settled"], lambda m=mid: self.call(
                    "lighter_rh", lighter_rh.fundings_request(m, *window(self.clock()))))
            except Exception:
                continue
        answers["lighter_rh"]["fundings"] = fundings
        records = source.catalog_from(answers)
        rows, groups, reasons = source.build(records, registry, at=self.clock(),
                                             received=self.venue_received())
        with self.lock:
            self.records, self.rows, self.groups, self.reasons = records, rows, groups, reasons
            self.by_key = {(r["venue"], r["asset"]): r for r in rows}
        return rows

    # -- demand-driven reads (V2 rows only) -------------------------------------------
    def wanted(self):
        """The (venue, asset) pairs that may get books, settled payments or closes."""
        with self.lock:
            return set(self.by_key)

    def row(self, v, a):
        with self.lock:
            r = self.by_key.get((v, a))
        if r is None:
            raise ValueError("V2: %s %s is not a verified multi-venue row" % (v, a))
        return r

    def book(self, v, a):
        r = self.row(v, a)
        xid, mid = r["exchange_id"], r["market_id"]
        stream = self.streams.get(xid)
        if stream is not None:
            from v2 import marketdata

            sb = stream.book(mid)
            if sb is not None and not marketdata.stale(sb, xid, self.clock()):
                return normalize.book(sb, r.get("multiplier") or 1.0)
        if xid == "variational":
            rec = next((x for x in self.records.get("variational", [])
                        if x["instrument"].market_id == mid), None)
            b = variational.quote_book(rec) if rec else base.book([], [])
            b["model"] = "rfq_indicative"
            fetched = self.venue_received().get("variational")
            mono = None
        else:
            req, parse = {
                "lighter_rh": (lambda: lighter_rh.book_request(mid), lighter_rh.parse_book),
                "risex": (lambda: risex.book_request(mid), risex.parse_book),
                "arcus": (lambda: arcus.book_request(mid), arcus.parse_book),
                "txflow": (lambda: txflow.book_request(mid), txflow.parse_book),
                "tradexyz": (lambda: ("POST", tradexyz.INFO_URL, tradexyz.l2_book(mid)),
                             lambda raw: base.book(
                                 [(x["px"], x["sz"]) for x in raw["levels"][0]],
                                 [(x["px"], x["sz"]) for x in raw["levels"][1]],
                                 source="tradexyz")),
            }[xid]
            headers = txflow.HEADERS if xid == "txflow" else None
            key = "book:%s:%s" % (xid, mid)
            raw = self.get(key, TTL["book"], lambda: self.call(xid, req(), headers))
            b = dict(parse(raw), model="orderbook")
            fetched, mono = self.fetched_at(key)
        b = normalize.book(b, r.get("multiplier") or 1.0)
        # `answer_received` is the answer's own fetch time (P1). `received` is what the
        # gates read: the read time under the legacy basis (a cache hit looks up to
        # TTL["book"] younger than it is), the fetch time under the answer basis
        # (marketdata.TIME_BASIS; an unknown fetch time leaves it None -> stale).
        from v2 import marketdata as _md
        b["answer_received"] = fetched
        b["received"] = fetched if _md.TIME_BASIS == _md.ANSWER else self.clock()
        b["received_mono"] = mono
        b["audit_id"] = None
        return b

    def settled(self, v, a, at=None):
        """Settled public payments of a V2 row's market over 7 days, oldest first, or
        None when the venue publishes no settled history (TxFlow, Variational)."""
        r = self.row(v, a)
        xid, mid = r["exchange_id"], r["market_id"]
        at = at or self.clock()
        if xid == "lighter_rh":
            raw = self.get("rh:fund:" + mid, TTL["settled"], lambda: self.call(
                xid, lighter_rh.fundings_request(mid, *window(at))))
            return lighter_rh.parse_fundings(raw)
        if xid == "risex":
            raw = self.get("risex:fh:" + mid, TTL["settled"], lambda: self.call(
                xid, risex.funding_history_request(mid)))
            return risex.parse_funding_history(raw)
        if xid == "arcus":
            raw = self.get("arcus:fh:" + mid, TTL["settled"], lambda: self.call(
                xid, arcus.funding_history_request(mid, *window(at))))
            return arcus.parse_funding_history(raw)
        if xid == "tradexyz":
            raw = self.get("xyz:fh:" + mid, TTL["settled"], lambda: self.call(
                xid, ("POST", tradexyz.INFO_URL,
                      tradexyz.funding_history(mid, int(window(at)[0] * 1000)))))
            return [dict(ts=x["time"] / 1000, rate=float(x["fundingRate"]), unit="fraction",
                         kind="settled_public") for x in raw or []]
        return None

    def rth_open(self):
        """US regular session state as Arcus publishes it (any online equity market's
        `isOutsideRth`); None when no such market is known."""
        flags = [r.get("outside_rth") for r in self.records.get("arcus", [])
                 if r["instrument"].session == "RTH" and r["instrument"].active
                 and r.get("outside_rth") is not None]
        if not flags:
            return None
        return not any(flags)
