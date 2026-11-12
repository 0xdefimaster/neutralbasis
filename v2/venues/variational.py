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


def stats(fetch=None):
    """The shared /metadata/v2/stats answer: (json, from_cache). `fetch` is for tests."""
    def get():
        if fetch is not None:
            return fetch()
        return http_json(XID, "GET", STATS_URL)

    return _CACHE.get(STATS_URL, get)


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
