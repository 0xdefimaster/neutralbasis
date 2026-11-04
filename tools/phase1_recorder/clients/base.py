"""What every venue client offers, and the shared parsing helpers.

A client turns one public response into normalized records. Parsers are pure functions
of (response JSON, request context, receive time, raw sha256), so the same parser
replays a stored raw response offline (`--validate-only`).

Status words used in records and reports:
  VERIFIED  checked against recorded data by this tool;
  OBSERVED  present in or measured from the venue's own responses;
  DECLARED  stated by the connector / project code, not checked here;
  UNKNOWN   not known: stored as null, never filled with a default;
  CONFLICT  the data contradicts the declaration or itself.
"""

import math
from decimal import Decimal

VERIFIED, OBSERVED, DECLARED, UNKNOWN, CONFLICT = (
    "VERIFIED",
    "OBSERVED",
    "DECLARED",
    "UNKNOWN",
    "CONFLICT",
)
DOCS_UNREAD = "Borsa belgesi okunmadı; bağlayıcı beyanı (capabilities.FUNDING_MODELS)"


class SchemaError(ValueError):
    """A response without a field the parser needs."""


def num(x):
    """A finite float, or None. Never a default."""
    if x is None or isinstance(x, bool):
        return None
    try:
        n = float(x)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def need(d, *keys):
    """Nested field or SchemaError naming what is missing."""
    cur = d
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        elif isinstance(cur, list) and isinstance(k, int) and -len(cur) <= k < len(cur):
            cur = cur[k]
        else:
            raise SchemaError("Yanıtta alan yok: " + ".".join(map(str, keys)))
    return cur


def epoch(x):
    """Seconds from a timestamp in s, ms, us or ns (explicit thresholds, as in
    risk_scan.epoch); None when absent or not a number."""
    if x is None or isinstance(x, bool):
        return None
    try:
        n = Decimal(str(x))
    except Exception:
        return None
    if not n.is_finite():
        return None
    if n >= 10**17:
        return float(n / 10**9)
    if n >= 10**14:
        return float(n / 10**6)
    if n >= 10**11:
        return float(n / 10**3)
    return float(n)


def book_record(venue, asset, market_id, bids, asks, exchange_ts, at, sha, levels):
    """Top `levels` of a book, sorted, with mid, spread and depth; crossed or empty
    books are kept and flagged, never repaired."""
    bids = sorted(
        ((p, q) for p, q in bids if p and q and p > 0 and q > 0), reverse=True
    )
    asks = sorted((p, q) for p, q in asks if p and q and p > 0 and q > 0)
    bids, asks = bids[:levels], asks[:levels]
    mid = spread = None
    valid = bool(bids and asks) and bids[0][0] < asks[0][0]
    if valid:
        mid = (bids[0][0] + asks[0][0]) / 2
        spread = (asks[0][0] - bids[0][0]) / mid * 1e4
    return dict(
        venue=venue,
        asset=asset,
        market_id=market_id,
        bids=[[p, q] for p, q in bids],
        asks=[[p, q] for p, q in asks],
        levels=levels,
        mid=mid,
        spread_bps=spread,
        depth_bid=sum(q for _, q in bids),
        depth_ask=sum(q for _, q in asks),
        valid=valid,
        exchange_ts=exchange_ts,
        received_at=at,
        raw_sha256=sha,
    )


class Client:
    """One venue. Subclasses implement the fetch methods and their parsers."""

    venue = None
    # Declared by the project, not verified here (capabilities.FUNDING_MODELS).
    declared_interval = None
    declared_direction = None
    published_unit = UNKNOWN
    candle_price_type = None

    def __init__(self, http, cfg):
        self.http = http
        self.cfg = cfg

    def select(self, markets):
        """The markets whose per-market tasks run: configured assets, or all."""
        if self.cfg.get("all_markets"):
            return [m for m in markets if m["active"]]
        want = set(self.cfg["assets"])
        return [m for m in markets if m["active"] and m["asset"] in want]

    # Each returns normalized records (and parser statistics where noted).
    def markets(self, at):
        raise NotImplementedError

    def funding_snapshots(self, at):
        raise NotImplementedError

    def settlements(self, market, start, end, at):
        raise NotImplementedError

    def candles(self, market, start, end, at):
        raise NotImplementedError

    def orderbook(self, market, at):
        raise NotImplementedError

    def replay(self, endpoint, obj, ctx, at, sha):
        """Normalized records of a stored response (offline), by endpoint name."""
        raise NotImplementedError
