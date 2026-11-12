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
    def __init__(self, transport=None, clock=time.time):
        # transport(exchange_id, method, url, body, headers) -> json
        self.transport = transport or (lambda x, m, u, b, h=None: base.http_json(x, m, u, b, h))
        self.clock = clock
        self.lock = threading.Lock()
        self.cache = {}
        self.records = {}
        self.rows, self.groups, self.reasons = [], {}, {}
        self.by_key = {}

    # -- cached fetch -----------------------------------------------------------------
    def get(self, key, ttl, fetch):
        with self.lock:
            hit = self.cache.get(key)
            if hit and self.clock() - hit[0] <= ttl:
                return hit[1]
        value = fetch()
        with self.lock:
            self.cache[key] = (self.clock(), value)
        return value

    def call(self, xid, req, headers=None):
        method, url, body = req
        return self.transport(xid, method, url, body, headers)

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
                lambda: self.call("variational", variational.stats_request()))[0]),
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
        rows, groups, reasons = source.build(records, registry, at=self.clock())
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
        if xid == "variational":
            rec = next((x for x in self.records.get("variational", [])
                        if x["instrument"].market_id == mid), None)
            b = variational.quote_book(rec) if rec else base.book([], [])
            b["model"] = "rfq_indicative"
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
            raw = self.get("book:%s:%s" % (xid, mid), TTL["book"],
                           lambda: self.call(xid, req(), headers))
            b = dict(parse(raw), model="orderbook")
        b = normalize.book(b, r.get("multiplier") or 1.0)
        b["received"] = self.clock()
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
