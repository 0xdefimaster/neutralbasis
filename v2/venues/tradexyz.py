"""tradexyz: trade.xyz, the HIP-3 dex "xyz" on Hyperliquid, as its own venue.

PAPER / public reads only. Every request this module builds is scoped to the xyz dex
(v2.network_policy enforces the same rule at the socket). CCXT's hyperliquid loader is
NOT used in V2: it asks `spotMeta` first, which is outside the xyz scope (V2 guard
refused it, 2026-11-12), so the old CCXT tradeXYZ path is fail-closed under NB_V2.

Fields read from the live `metaAndAssetCtxs` (dex "xyz") answer, 2026-11-12:
universe[i] = {name "xyz:SYM", szDecimals, maxLeverage, growthMode, deployerFeeScale,
isDelisted?}, ctxs[i] = {funding, openInterest, oraclePx, markPx, midPx, impactPxs,
premium, dayNtlVlm}. Funding sign and formula for HIP-3 were not found in the docs read
(docs/v2/16_BLOCKERS.md B11): the rate is kept as published, sign UNVERIFIED_DOC.
"""
from v2 import fees, identity
from v2.identity import Instrument

INFO_URL = "https://api.hyperliquid.xyz/info"
DEX = "xyz"
PREFIX = "xyz:"
# Hyperliquid perps tier 0 (fees docs, read 2026-11-12); scaled per asset.
BASE_TAKER = 0.00045
BASE_MAKER = 0.00015


def coin(symbol):
    s = str(symbol)
    return s if s.startswith(PREFIX) else PREFIX + s


def meta_and_ctxs():
    return {"type": "metaAndAssetCtxs", "dex": DEX}


def l2_book(symbol):
    return {"type": "l2Book", "coin": coin(symbol)}


def funding_history(symbol, start_ms, end_ms=None):
    body = {"type": "fundingHistory", "coin": coin(symbol), "startTime": int(start_ms)}
    if end_ms is not None:
        body["endTime"] = int(end_ms)
    return body


def num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def parse_markets(answer):
    """Markets of the xyz dex: list of dicts with instrument, prices, funding, fees.
    A missing field stays None; nothing is defaulted."""
    universe, ctxs = answer[0]["universe"], answer[1]
    if len(universe) != len(ctxs):
        raise ValueError("metaAndAssetCtxs universe/ctx length mismatch")
    out = []
    for u, c in zip(universe, ctxs):
        name = u.get("name", "")
        if not name.startswith(PREFIX):
            raise ValueError("non-xyz market in an xyz answer: %r" % name)
        scale = u.get("deployerFeeScale")
        growth = u.get("growthMode") == "enabled"
        m = fees.hip3_scale(scale, growth) if scale is not None else None
        out.append(dict(
            instrument=Instrument("tradexyz", name, name, asset_class="unknown",
                                  quote_asset=None),
            delisted=bool(u.get("isDelisted")),
            max_leverage=u.get("maxLeverage"),
            sz_decimals=u.get("szDecimals"),
            mark=num(c.get("markPx")), oracle=num(c.get("oraclePx")),
            mid=num(c.get("midPx")),
            funding_raw=num(c.get("funding")), funding_unit="fraction",
            funding_sign="UNVERIFIED_DOC",
            open_interest=num(c.get("openInterest")),
            day_notional=num(c.get("dayNtlVlm")),
            fee_scale=scale, growth_mode=growth,
            taker=None if m is None else BASE_TAKER * m,
            maker=None if m is None else BASE_MAKER * m,
            fee_status="VERIFIED_LIVE_DERIVED" if m is not None else fees.UNKNOWN))
    return out


def funding_multipliers(perp_dexs_answer):
    """{coin: multiplier} for the xyz dex only, from a perpDexs answer (BROAD_METADATA:
    every other dex's entry is ignored)."""
    xyz = [d for d in perp_dexs_answer if d and d.get("name") == DEX]
    if len(xyz) != 1:
        raise ValueError("perpDexs answer has %d xyz entries" % len(xyz))
    return {c: num(m) for c, m in xyz[0].get("assetToFundingMultiplier") or []}


def base(symbol):
    return identity.parse_symbol(symbol)[0]
