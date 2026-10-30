"""Normalized record builders shared by the venue clients."""

from .base import UNKNOWN, DECLARED, OBSERVED, num

KINDS = (
    "market",
    "funding_snapshot",
    "settlement",
    "candle",
    "orderbook",
    "comparison",
)
# Fields every record of a kind must carry (values may be null where unknown).
REQUIRED = dict(
    market=(
        "venue",
        "asset",
        "market_id",
        "market_type",
        "contract_size",
        "contract_size_status",
        "min_order_size",
        "amount_step",
        "price_step",
        "max_leverage",
        "active",
        "funding_interval",
        "funding_interval_status",
        "observed_at",
        "raw_sha256",
    ),
    funding_snapshot=(
        "venue",
        "asset",
        "market_id",
        "published_rate_raw",
        "published_rate",
        "rate_unit",
        "rate_unit_status",
        "normalized_hourly_rate",
        "funding_interval",
        "next_funding_at",
        "next_funding_source",
        "mark_price",
        "index_price",
        "oracle_price",
        "api_timestamp",
        "received_at",
        "endpoint",
        "raw_sha256",
    ),
    settlement=(
        "venue",
        "asset",
        "market_id",
        "settled_at",
        "settled_rate_raw",
        "settled_rate",
        "settled_rate_status",
        "direction_raw",
        "direction_status",
        "value_raw",
        "received_at",
        "endpoint",
        "raw_sha256",
    ),
    candle=(
        "venue",
        "asset",
        "market_id",
        "open_time",
        "close_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "price_type",
        "price_type_status",
        "received_at",
        "raw_sha256",
    ),
    orderbook=(
        "venue",
        "asset",
        "market_id",
        "bids",
        "asks",
        "mid",
        "spread_bps",
        "valid",
        "exchange_ts",
        "received_at",
        "raw_sha256",
    ),
    comparison=(
        "venue",
        "asset",
        "market_id",
        "observed_at",
        "expected_settlement_at",
        "published_rate_raw",
        "settled_at",
        "settled_rate_raw",
        "comparison_status",
        "snapshot_raw_sha256",
        "settlement_raw_sha256",
    ),
)


def record_key(kind, r):
    """The idempotency key of a record within its kind and venue."""
    mid = str(r["market_id"])
    if kind == "market":
        return "%s|%d" % (mid, int(r["observed_at"]) // (6 * 3600))
    if kind == "funding_snapshot":
        return "%s|%d" % (mid, int(r["received_at"]))
    if kind == "settlement":
        return "%s|%d" % (mid, int(round(r["settled_at"])))
    if kind == "candle":
        return "%s|%d" % (mid, int(r["open_time"]))
    if kind == "orderbook":
        return "%s|%d" % (mid, int(r["received_at"]))
    if kind == "comparison":
        return "%s|%d" % (mid, int(round(r["settled_at"])))
    raise ValueError(kind)


def market(venue, asset, market_id, at, sha, **kw):
    out = dict(
        venue=venue,
        asset=asset,
        market_id=market_id,
        ccxt_symbol=None,
        base=None,
        quote=None,
        market_type=None,
        contract_size=None,
        contract_size_status=UNKNOWN,
        contract_size_source=None,
        contract_multiplier=None,
        min_order_size=None,
        min_quote=None,
        amount_step=None,
        price_step=None,
        max_leverage=None,
        active=None,
        funding_interval=None,
        funding_interval_status=UNKNOWN,
        extra={},
        observed_at=at,
        raw_sha256=sha,
    )
    out.update(kw)
    return out


def snapshot(client, asset, market_id, rate_raw, at, sha, endpoint, **kw):
    out = dict(
        venue=client.venue,
        asset=asset,
        market_id=market_id,
        published_rate_raw=rate_raw,
        published_rate=num(rate_raw),
        rate_unit=client.published_unit,
        rate_horizon_seconds=None,
        rate_unit_status=UNKNOWN,
        # Only filled once the unit is verified; never computed from a declaration.
        normalized_hourly_rate=None,
        funding_interval=client.declared_interval,
        funding_interval_status=DECLARED if client.declared_interval else UNKNOWN,
        next_funding_at=None,
        next_funding_source=None,
        mark_price=None,
        index_price=None,
        oracle_price=None,
        premium=None,
        api_timestamp=None,
        received_at=at,
        endpoint=endpoint,
        raw_sha256=sha,
    )
    out.update(kw)
    return out


def settlement(client, ctx, settled_at, rate_raw, at, sha, endpoint, **kw):
    out = dict(
        venue=client.venue,
        asset=ctx["asset"],
        market_id=ctx["market_id"],
        settled_at=settled_at,
        settled_rate_raw=rate_raw,
        settled_rate=num(rate_raw),
        settled_rate_status=DECLARED,
        rate_unit="PER_SETTLEMENT",
        direction_raw=None,
        direction_status=UNKNOWN,
        direction_convention=client.declared_direction,
        value_raw=None,
        currency=None,
        received_at=at,
        endpoint=endpoint,
        raw_sha256=sha,
    )
    out.update(kw)
    return out


def candle(client, ctx, open_time, o, h, l, c, v, at, sha, endpoint):
    return dict(
        venue=client.venue,
        asset=ctx["asset"],
        market_id=ctx["market_id"],
        open_time=open_time,
        close_time=open_time + 3600,
        open=num(o),
        high=num(h),
        low=num(l),
        close=num(c),
        volume=num(v),
        price_type=client.candle_price_type,
        price_type_status=DECLARED if client.candle_price_type else UNKNOWN,
        received_at=at,
        endpoint=endpoint,
        raw_sha256=sha,
    )


def series_stats(times, at):
    """Duplicates, out-of-order entries and implausible timestamps in one response."""
    seen, dup, disorder = set(), 0, 0
    for a, b in zip(times, times[1:]):
        if b < a:
            disorder += 1
    for t in times:
        if t in seen:
            dup += 1
        seen.add(t)
    return dict(
        duplicates=dup,
        out_of_order=disorder,
        future=sum(t > at + 300 for t in times),
        too_old=sum(t < 1_420_000_000 for t in times),
    )


def dedupe(records, field):
    """One record per timestamp, in time order (the first one seen wins)."""
    out, seen = [], set()
    for r in records:
        k = int(round(r[field]))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return sorted(out, key=lambda r: r[field])


__all__ = ["KINDS", "REQUIRED", "record_key", "OBSERVED"]
