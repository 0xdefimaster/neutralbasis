"""Normalized record builders shared by the venue clients."""

from .base import UNKNOWN, DECLARED, OBSERVED, num

KINDS = (
    "market",
    "funding_snapshot",
    "settlement",
    "candle",
    "orderbook",
    "comparison",
    "settlement_correction",
    "settlement_withdrawn",
    "schema",
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
    schema=(
        "venue",
        "market_id",
        "endpoint",
        "schema_sha256",
        "paths",
        "listing_count",
        "first_seen_at",
        "raw_sha256",
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
        return revised("%s|%d" % (mid, int(round(r["settled_at"]))), r)
    if kind == "candle":
        return "%s|%d" % (mid, int(r["open_time"]))
    if kind == "orderbook":
        return "%s|%d" % (mid, int(r["received_at"]))
    if kind == "comparison":
        return revised(
            "%s|%d" % (mid, int(round(r["settled_at"]))),
            r,
            r.get("settlement_revision"),
        )
    if kind == "schema":
        return "%s|%s" % (mid, r["schema_sha256"])
    if kind in ("settlement_correction", "settlement_withdrawn"):
        return "%s|%d|r%d" % (mid, int(round(r["settled_at"])), r["revision"])
    raise ValueError(kind)


def revised(key, r, revision=None):
    """A first version keeps the plain key; a corrected one adds its revision."""
    n = revision if revision is not None else r.get("revision", 1)
    return key if (n or 1) <= 1 else "%s|r%d" % (key, n)


# Two settlement times closer than the market's tolerance may be one payment (a venue
# may report the same payment with a different sub-second part). The tolerance is
# set per venue or market (config `same_event_seconds`) and is never more than 60 s
# nor 1% of the market's payment interval (records.same_event_tolerance).
SAME_EVENT = 60
# Within the tolerance, times this close with different values are one payment
# reported two ways (CONFLICT); further apart they are two possible payments that
# cannot be told apart (AMBIGUOUS).
SAME_INSTANT = 1
SETTLED_FIELDS = ("settled_rate_raw", "direction_raw", "value_raw")


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
    """`settled_at` is whole UTC epoch seconds for every venue; the time exactly as
    the API gave it (in seconds) is kept in `settled_at_api`."""
    out = dict(
        venue=client.venue,
        asset=ctx["asset"],
        market_id=ctx["market_id"],
        settled_at=int(round(settled_at)),
        settled_at_api=settled_at,
        revision=1,
        identity_status="UNIQUE",
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
    """Duplicates, out-of-order entries and implausible timestamps in one response.

    A response listed consistently newest-first (Extended) is ordered, not disordered:
    only steps against the response's own dominant direction count."""
    seen, dup = set(), 0
    steps = [b - a for a, b in zip(times, times[1:]) if b != a]
    down = sum(d < 0 for d in steps)
    disorder = min(down, len(steps) - down)
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


def configured_tolerance(cfg, venue, asset):
    """`same_event_seconds` for a market: "Venue|ASSET", else the venue, else 60."""
    conf = cfg.get("same_event_seconds")
    if isinstance(conf, dict):
        return conf.get("%s|%s" % (venue, asset), conf.get(venue, SAME_EVENT))
    return SAME_EVENT if conf is None else conf


def same_event_tolerance(configured, interval):
    """The seconds within which two settlement times may be one payment: the smallest
    of the configured value, 1% of the payment interval and 60 s. With no known
    interval only SAME_INSTANT is allowed."""
    limits = [SAME_EVENT, configured if configured is not None else SAME_EVENT]
    limits.append(0.01 * interval if interval else SAME_INSTANT)
    return max(0.0, min(limits))


def dedupe_settlements(rows, tolerance=SAME_EVENT):
    """One record per payment of one response. Entries within `tolerance` seconds:
    * identical values: one payment (a duplicate, or a sub-second difference);
    * different values within SAME_INSTANT: one payment reported two ways, kept as
      one CONFLICT record whose settled rate is unknown (never a picked value);
    * different values further apart: both kept, both AMBIGUOUS (excluded from
      counts and comparisons until resolved)."""
    out = []
    stats = dict(conflicting_duplicates=0, jitter_merged=0, ambiguous=0)
    for r in sorted(rows, key=lambda r: r["settled_at_api"]):
        # Sorted by time: only the latest kept entry can be the same payment.
        same = (
            out[-1]
            if out and r["settled_at_api"] - out[-1]["settled_at_api"] <= tolerance
            else None
        )
        if same is None:
            out.append(r)
            continue
        differ = any(same[f] != r[f] for f in SETTLED_FIELDS)
        apart = abs(r["settled_at_api"] - same["settled_at_api"]) > SAME_INSTANT
        if differ and apart:
            stats["ambiguous"] += 1
            same["identity_status"] = "AMBIGUOUS"
            out.append(dict(r, identity_status="AMBIGUOUS"))
            continue
        if int(round(same["settled_at_api"])) != int(round(r["settled_at_api"])):
            stats["jitter_merged"] += 1
        if differ:
            stats["conflicting_duplicates"] += 1
            same.setdefault("conflict_values", [{f: same[f] for f in SETTLED_FIELDS}])
            same["conflict_values"].append({f: r[f] for f in SETTLED_FIELDS})
            same["settled_rate"] = None
            same["settled_rate_status"] = "CONFLICT"
    return out, stats


def counted(rows):
    """Settlements that may be counted and compared: not AMBIGUOUS."""
    return [r for r in rows if r.get("identity_status", "UNIQUE") != "AMBIGUOUS"]


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
