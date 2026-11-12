"""Observation-only Hyperliquid readers: native perps (hyperliquid_native) and the HIP-3
dex "io" (entropy_io). READ ONLY; these venues are never PAPER legs
(v2.observation.assert_paper_venue).

Request shapes: Hyperliquid info-endpoint docs (read 2026-11-12): metaAndAssetCtxs
takes an optional "dex" ("" = the first perp dex, i.e. native); fundingHistory takes
coin, startTime (ms, inclusive), optional endTime; time-range answers hold at most 500
rows, so history is paged from the last returned time. HIP-3 coins are "dex:COIN".

Funding: same convention as tradexyz (v2/venues/tradexyz.py): hourly, positive = longs
pay. Native fees: tier-0 taker 0.045 % (fees docs, read 2026-11-12); io fees: that base
scaled by the dex's deployerFeeScale (fees.hip3_scale), as for xyz.
"""
from v2 import fees
from v2.identity import Instrument
from v2.venues import tradexyz

INFO_URL = tradexyz.INFO_URL
DEX = {"hyperliquid_native": "", "entropy_io": "io"}
PAGE = 500
HOUR_MS = 3600 * 1000


def meta_and_ctxs(exchange_id):
    dex = DEX[exchange_id]
    return {"type": "metaAndAssetCtxs", "dex": dex} if dex else {"type": "metaAndAssetCtxs"}


def coin(exchange_id, symbol):
    s = str(symbol)
    if exchange_id == "entropy_io":
        return s if s.startswith("io:") else "io:" + s
    if ":" in s:
        raise ValueError("native coin must have no dex prefix: %r" % s)
    return s


def l2_book(exchange_id, symbol):
    return {"type": "l2Book", "coin": coin(exchange_id, symbol)}


def funding_history(exchange_id, symbol, start_ms, end_ms=None):
    body = {"type": "fundingHistory", "coin": coin(exchange_id, symbol),
            "startTime": int(start_ms)}
    if end_ms is not None:
        body["endTime"] = int(end_ms)
    return body


def parse_markets(exchange_id, answer, cats=None):
    """Market records of one observation dex. Missing fields stay None."""
    universe, ctxs = answer[0]["universe"], answer[1]
    if len(universe) != len(ctxs):
        raise ValueError("metaAndAssetCtxs universe/ctx length mismatch")
    prefix = "io:" if exchange_id == "entropy_io" else None
    out = []
    for u, c in zip(universe, ctxs):
        name = u.get("name", "")
        if prefix and not name.startswith(prefix):
            raise ValueError("non-io market in an io answer: %r" % name)
        if not prefix and ":" in name:
            raise ValueError("HIP-3 market in a native answer: %r" % name)
        scale = u.get("deployerFeeScale")
        if exchange_id == "entropy_io":
            m = fees.hip3_scale(scale, u.get("growthMode") == "enabled") \
                if scale is not None else None
        else:
            m = 1.0
        cat = (cats or {}).get(name)
        rate = tradexyz.num(c.get("funding"))
        out.append(dict(
            instrument=Instrument(exchange_id, name, name, asset_class=cat or "unknown",
                                  quote_asset=None, price=tradexyz.num(c.get("markPx")),
                                  active=not u.get("isDelisted"),
                                  class_source="perpCategories=%s" % cat if cat else None),
            exchange_id=exchange_id, base=name.split(":", 1)[-1],
            mark=tradexyz.num(c.get("markPx")), oracle=tradexyz.num(c.get("oraclePx")),
            mid=tradexyz.num(c.get("midPx")),
            funding=None if rate is None else dict(
                raw=rate, unit="fraction", interval_s=3600, kind="observed_published",
                sign="positive: longs pay (Hyperliquid funding docs)"),
            fee_taker=None if m is None else tradexyz.BASE_TAKER * m,
            open_interest=tradexyz.num(c.get("openInterest")),
            day_notional=tradexyz.num(c.get("dayNtlVlm")),
            delisted=bool(u.get("isDelisted")), observation_only=True))
    return out


def parse_funding_history(answer):
    """[dict(ts seconds, rate fraction per hour, premium)] sorted by time."""
    out = []
    for x in answer or []:
        rate = tradexyz.num(x.get("fundingRate"))
        t = x.get("time")
        if rate is None or t is None:
            continue
        out.append(dict(ts=int(t) / 1000.0, rate=rate,
                        premium=tradexyz.num(x.get("premium"))))
    return sorted(out, key=lambda r: r["ts"])


def history(fetch, exchange_id, symbol, start_ms, end_ms):
    """All funding rows in [start_ms, end_ms], paged (500 rows per answer). `fetch(body)`
    returns the parsed JSON answer. Stops on a short or non-advancing page."""
    rows, start = [], int(start_ms)
    for _ in range(1000):
        page = fetch(funding_history(exchange_id, symbol, start, end_ms))
        rows.extend(page or [])
        if not page or len(page) < PAGE:
            break
        last = max(int(x["time"]) for x in page)
        if last + 1 <= start or last >= end_ms:
            break
        start = last + 1
    seen, out = set(), []
    for r in parse_funding_history(rows):
        if r["ts"] not in seen:
            seen.add(r["ts"])
            out.append(r)
    return out
