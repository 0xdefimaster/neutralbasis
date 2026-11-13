"""variational: Variational Omni (RFQ; the OLP is the only maker). Public read-only
GET /metadata/v2/stats (docs.variational.io/technical-documentation/api, read
2026-11-12). The trading API is not live; nothing here could trade.

Rate limit: 10 requests / 10 s per IP and a global 1,000 / min; HTTP 429 carries
`retry_after_seconds`. Every caller in the process goes through `stats()`: one shared
budget (v2.ratelimit "variational") and one shared answer cached for STATS_TTL seconds,
so the engine, the observer and health checks never send parallel requests.

Asset class: the listing has no class field. The TradFi perpetuals page lists the
non-crypto perps with their underlying (read 2026-11-12); those get a documented class
(an anchor). Every other listing stays unknown: the docs do not say that unlisted
tickers are crypto, so such a market can only join a group anchored by another venue
(price and name checks). `name` is the asset's full name. Funding: `interval_rate` a
fraction per `interval_seconds` (1 h to 8 h per market), positive = longs pay. Quotes:
indicative bid/ask per size bucket (may be cached up to 600 s); trading fee 0 (docs),
the spread is the cost.
"""
import threading

from v2 import ratelimit
from v2.identity import Instrument
from v2.venues.base import book, funding, http_json, num

XID = "variational"
LEGACY = "Variational"
STATS_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats"
STATS_TTL = 15.0
# docs.variational.io/omni/trading/tradfi-perpetuals.md (perps table, 2026-11-12).
DOC_TRADFI = {
    "BZ": "commodity", "CL": "commodity", "COPPER": "commodity", "NATGAS": "commodity",
    "XAG": "commodity", "XAU": "commodity", "XPD": "commodity", "XPT": "commodity",
    "US500": "etf", "ANTHROPIC": "preipo", "OPENAI": "preipo",
}
DOC_SOURCE = "docs tradfi-perpetuals table"

_CACHE = ratelimit.SharedCache(STATS_TTL)
_LOCK = threading.Lock()


LIVE = "live"


def stats(fetch=None, source=None):
    """The shared /metadata/v2/stats answer: (json, from_cache).

    The cache key is the answer's SOURCE, not only the URL: live readers (source
    "live", the default without `fetch`) share one answer per TTL, so the venue sees one
    client; a replay, a recording or a test fetch (any other source; a `fetch` without
    a source gets its own key) never receives an answer from another source or another
    moment (fixed 2026-11-12: a test's MOCK stats leaked into a later replay)."""
    if source is None:
        source = LIVE if fetch is None else "fetch:%d" % id(fetch)

    def get():
        if fetch is not None:
            return fetch()
        return http_json(XID, "GET", STATS_URL)

    return _CACHE.get((STATS_URL, source), get)


def stats_age(fetch=None, source=None):
    """Seconds since the shared stats answer of this source was fetched, or None."""
    if source is None:
        source = LIVE if fetch is None else "fetch:%d" % id(fetch)
    return _CACHE.age((STATS_URL, source))


def stats_request():
    return ("GET", STATS_URL, None)


def parse_markets(raw):
    out = []
    for x in raw.get("listings") or []:
        ticker = x["ticker"]
        ct = "perp" if x.get("contract_type") == "perpetual" else str(x.get("contract_type"))
        base = ticker.rsplit("-", 1)[0]
        doc_class = DOC_TRADFI.get(base) if ct == "perp" else None
        inst = Instrument(XID, ticker, ticker, asset_class=doc_class or "unknown",
                          contract_type=ct, name=x.get("name"), price=num(x.get("mark_price")),
                          class_source=DOC_SOURCE if doc_class else None)
        f = x.get("funding") or {}
        q = x.get("quotes") or {}
        out.append(dict(
            instrument=inst, legacy_venue=LEGACY, mark=num(x.get("mark_price")), index=None,
            funding=funding(num((f.get("last") or {}).get("interval_rate")), "fraction",
                            num(f.get("interval_seconds")), num(f.get("next_timestamp")),
                            sign="positive: longs pay", source="variational interval_rate"),
            fee_taker=0.0, fee_source="docs: no trading fees; cost is the OLP quote spread",
            quotes={k: (num((q.get(k) or {}).get("bid")), num((q.get(k) or {}).get("ask")))
                    for k in ("base", "size_1k", "size_100k", "size_1m") if q.get(k)},
            quotes_updated=q.get("updated_at"),
            base_spread_bps=num(x.get("base_spread_bps")),
        ))
    return out


def quote_book(record, bucket="size_1k"):
    """The indicative quote of one size bucket as a one-level book (RFQ, not a book)."""
    bid, ask = (record.get("quotes") or {}).get(bucket, (None, None))
    if not bid or not ask:
        return book([], [], source=XID + ":rfq")
    size = {"size_1k": 1000.0, "size_100k": 100000.0, "size_1m": 1e6}.get(bucket)
    mid = (bid + ask) / 2
    q = size / mid if size else None
    return book([(bid, q)] if q else [], [(ask, q)] if q else [], source=XID + ":rfq:" + bucket)


BUCKETS = (("size_1k", 1000.0), ("size_100k", 100000.0), ("size_1m", 1e6))


def quote_book_for(record, size_usd):
    """The indicative quote of the smallest published bucket that covers `size_usd`
    (5k and 10k use the 100k bucket: wider, conservative). Still RFQ, not a fill."""
    for name, cap in BUCKETS:
        if size_usd <= cap and (record.get("quotes") or {}).get(name):
            return quote_book(record, name)
    return book([], [], source=XID + ":rfq")



# What a PAPER fill model for Omni RFQ needs, and whether public data gives it
# (docs.variational.io, read 2026-11-12). Any requirement not MET keeps the gate's
# EXECUTION_MODEL_UNVERIFIED: an indicative quote is "not a commitment to execute".
RFQ_DOC = "https://docs.variational.io/omni/trading/quoted-index-and-mark-prices.md"
RFQ_FILL_MODEL_REQUIREMENTS = (
    dict(id="indicative_tiers", need="bid/ask at the trade size",
         status="MET", source="api.md quotes: base, size_1k, size_100k, size_1m"),
    dict(id="quote_age", need="age of the quote at decision time",
         status="MET", source="api.md: quotes.updated_at; cached up to 600 s"),
    dict(id="firm_vs_indicative", need="distribution of firm price minus indicative "
         "price per size and market", status="NOT_AVAILABLE",
         source=RFQ_DOC + " (firm quote only on order entry; trading API 'not yet "
         "available to any users', api.md)"),
    dict(id="last_look_rejects", need="rate of OLP last-look rejections",
         status="NOT_AVAILABLE", source="trading-via-rfq.md (no public data)"),
    dict(id="quote_expiry", need="firm quote validity window", status="UNKNOWN",
         source="not stated in the docs"),
    dict(id="partial_fills", need="partial-fill behaviour of market orders",
         status="UNKNOWN", source="order-types.md: limit orders all-or-nothing; market "
         "orders not stated"),
    dict(id="public_fills", need="executed trades to validate any model",
         status="NOT_AVAILABLE", source="no public trades/fills endpoint; only the "
         "user's own CSV export (trade-and-transfer-history.md)"),
    dict(id="funding_settlement", need="settled funding per market",
         status="NOT_AVAILABLE", source="funding history only in the user's own CSV"),
)


def rfq_model_status():
    """('VERIFIED' | 'BLOCKED', [requirement ids not met])."""
    gaps = [r["id"] for r in RFQ_FILL_MODEL_REQUIREMENTS if r["status"] != "MET"]
    return ("BLOCKED" if gaps else "VERIFIED"), gaps


def quote_age(record, at):
    """Seconds between the quotes' updated_at and `at`, or None when not published."""
    import datetime

    u = (record or {}).get("quotes_updated")
    if not u:
        return None
    try:
        s = str(u).rstrip("Z")
        head, _, frac = s.partition(".")
        t = datetime.datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=datetime.timezone.utc).timestamp()
        t += float("0." + frac) if frac else 0.0
    except ValueError:
        return None
    return at - t
