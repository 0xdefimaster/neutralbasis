"""Variational Omni: read-only RESEARCH adapter (no trading, no account, not wired into the
PAPER engine).

Only fields whose meaning the official docs state are interpreted (docs.variational.io,
read 2026-11-10; quotes below). Everything else is UNKNOWN.

Endpoint: GET https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats
  "Returns platform-wide and per-listing market statistics for all perpetual and swap
  markets." Rate limit "Per IP: 10 requests per 10 seconds" (shared with v1).

Documented semantics used here:
  ticker         "Market ticker, suffixed with the contract type (e.g., BTC-PERP, XAU-SWAP)"
  contract_type  "perpetual or swap"
  mark_price     "Current mark price"; prices "denominated in USDC"
  funding.last   "Current funding rate"; funding.next "Predicted funding rate for the next
                 interval. Currently the same as last" -> CURRENT_INDICATIVE, never a
                 settled or verified forecast
  interval_rate  "Funding rate for one funding interval"; interval_seconds in seconds
  sign           "A positive rate means longs pay shorts"
  quotes         "Indicative quotes at several sizes" (base, size_1k, size_100k, size_1m),
                 "may be cached for up to 600 seconds", updated_at ISO 8601
  execution      RFQ against the OLP ("the only eligible maker"); public quotes are
                 "indicative", a firm quote is generated on order submission; "The trading
                 API is still in development, and is not yet available to any users."
  fees           "There are no trading fees on Omni." (the cost is the spread)
Not public: funding history, price history, firm quotes -> UNKNOWN.
"""
import datetime

URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats"
EXECUTION_MODEL = "RFQ_OLP_INDICATIVE_ONLY"  # PAPER execution: RESEARCH_ONLY
FUNDING_SEMANTICS = "CURRENT_INDICATIVE"
QUOTE_MAX_AGE = 600
# Instruments the docs define explicitly (tradfi-perpetuals page). Others: the docs' default
# rule ("you can assume the symbol matches the TradFi symbol") -> not a verified identity.
DOCUMENTED_UNDERLYING = {
    "CL": "WTI Crude Oil futures (two-contract roll)",
    "BZ": "Brent Crude Oil futures (two-contract roll)",
    "COPPER": "COMEX High Grade Copper futures",
    "NATGAS": "Henry Hub Natural Gas futures",
    "XAU": "Gold spot price",
    "XAG": "Silver spot price",
}


def num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def iso(x):
    """ISO 8601 with up to nanoseconds (as sent in quotes.updated_at) -> epoch seconds."""
    s = str(x or "").replace("Z", "+00:00")
    if not s:
        return None
    if "." in s:
        head, rest = s.split(".", 1)
        frac = "".join(ch for ch in rest if ch.isdigit())
        s = head + "." + frac[:6] + (rest[len(frac):] or "+00:00")
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def parse_listing(x, received):
    """One listing -> research row; a missing documented field stays None (UNKNOWN)."""
    ticker = x.get("ticker") or ""
    base, _, suffix = ticker.rpartition("-")
    f = x.get("funding") or {}
    last = f.get("last") or {}
    interval = num(f.get("interval_seconds"))
    rate = num(last.get("interval_rate"))
    q = x.get("quotes") or {}
    quoted_at = iso(q.get("updated_at"))
    quotes = {k: (num((q.get(k) or {}).get("bid")), num((q.get(k) or {}).get("ask")))
              for k in ("base", "size_1k", "size_100k", "size_1m") if k in q}
    ctype = x.get("contract_type")
    return dict(
        venue="Variational", ticker=ticker, asset=base or ticker, name=x.get("name"),
        contract_type=ctype, perpetual=ctype == "perpetual" and suffix == "PERP",
        underlying_documented=DOCUMENTED_UNDERLYING.get(base),
        mark=num(x.get("mark_price")),
        funding_semantics=FUNDING_SEMANTICS if ctype == "perpetual" else "SWAP_DAILY_UNSUPPORTED",
        interval=interval, rate_per_interval=rate,
        rate_hourly=None if rate is None or not interval else rate / (interval / 3600),
        next_funding=num(f.get("next_timestamp")),
        quotes=quotes, quoted_at=quoted_at,
        quote_age=None if quoted_at is None else received - quoted_at,
        quotes_fresh=quoted_at is not None and received - quoted_at <= QUOTE_MAX_AGE,
        volume_24h=num(x.get("volume_24h")),
        open_interest=sum(v for v in (num((x.get("open_interest") or {}).get("long_open_interest")),
                                      num((x.get("open_interest") or {}).get("short_open_interest")))
                          if v is not None) or None,
        fee_taker=0.0, fee_source="docs: 'There are no trading fees on Omni.' (spread is the cost)",
        execution_model=EXECUTION_MODEL, paper_execution="RESEARCH_ONLY",
        funding_history="UNKNOWN (not public)", price_history="UNKNOWN (not public)",
        received=received,
    )


def parse(obj, received):
    """{asset: row} for perpetual listings; (rows, skipped) with per-listing isolation."""
    rows, skipped = {}, {}
    for x in obj.get("listings") or []:
        try:
            r = parse_listing(x, received)
        except Exception as ex:  # one listing never drops the venue
            skipped[str(x.get("ticker"))] = type(ex).__name__
            continue
        if r["perpetual"]:
            rows[r["asset"]] = r
    return rows, skipped


def indicative_cost(row, side, notional):
    """Cost vs mark of taking the INDICATIVE quote for `notional` USD, using the smallest
    documented quote size that covers it (size_1k, size_100k, size_1m; `base` is the
    minimum order size, whose notional is undocumented, and is not used). None above
    the largest quoted size. Not a firm quote: research only."""
    if not row["mark"]:
        return None
    for key, size in (("size_1k", 1000), ("size_100k", 100000), ("size_1m", 1000000)):
        if notional <= size and key in row["quotes"]:
            bid, ask = row["quotes"][key]
            if bid is None or ask is None:
                return None
            return (ask / row["mark"] - 1) if side == "buy" else (1 - bid / row["mark"])
    return None
