"""Hyperliquid public market data.

Hyperliquid serves its public data only as POST https://api.hyperliquid.xyz/info with
a JSON request type. The request bodies below are the ones CCXT's hyperliquid adapter
sends (the main project reads Hyperliquid through CCXT): metaAndAssetCtxs,
fundingHistory, candleSnapshot and l2Book. Any other type is refused.
"""

from . import records as R
from .base import (
    DECLARED,
    OBSERVED,
    UNKNOWN,
    Client,
    SchemaError,
    book_record,
    need,
    num,
)

HOST = "https://api.hyperliquid.xyz"
INFO = HOST + "/info"
ALLOWED_TYPES = ("metaAndAssetCtxs", "fundingHistory", "candleSnapshot", "l2Book")
HISTORY_PAGE = 500  # fundingHistory returns at most this many rows per call


class Hyperliquid(Client):
    venue = "Hyperliquid"
    declared_interval = 3600
    declared_direction = "Pozitif oran: long öder (beyan)"
    candle_price_type = "TRADE"

    def info(self, body, endpoint):
        if body.get("type") not in ALLOWED_TYPES:
            raise ValueError("İzin verilmeyen Hyperliquid istek türü")
        return self.http.request(endpoint, INFO, body=body, method="POST")

    # --- markets + funding (one response) ------------------------------------------
    def _meta(self, at):
        return self.info({"type": "metaAndAssetCtxs"}, "hyperliquid.metaAndAssetCtxs")

    def markets(self, at):
        obj, sha = self._meta(at)
        return self.parse_markets(obj, at, sha)

    def funding_snapshots(self, at):
        obj, sha = self._meta(at)
        return self.parse_snapshots(obj, at, sha)

    @staticmethod
    def _pairs(obj):
        if not isinstance(obj, list) or len(obj) < 2:
            raise SchemaError("metaAndAssetCtxs: [meta, ctxs] bekleniyordu")
        universe = need(obj[0], "universe")
        ctxs = obj[1]
        if not isinstance(ctxs, list) or len(ctxs) != len(universe):
            raise SchemaError("metaAndAssetCtxs: universe ve ctx sayısı uyuşmuyor")
        return list(zip(universe, ctxs))

    def parse_markets(self, obj, at, sha):
        out = []
        for u, _ in self._pairs(obj):
            name = need(u, "name")
            dec = num(u.get("szDecimals"))
            out.append(
                R.market(
                    self.venue,
                    name,
                    name,
                    at,
                    sha,
                    base=name,
                    market_type="perp",
                    # CCXT declares contractSize 1 for Hyperliquid perps; sizes are
                    # in base units (szDecimals).
                    contract_size=1.0,
                    contract_size_status=DECLARED,
                    contract_size_source="CCXT hyperliquid contractSize",
                    amount_step=10 ** -int(dec) if dec is not None else None,
                    max_leverage=num(u.get("maxLeverage")),
                    active=not bool(u.get("isDelisted")),
                    funding_interval=self.declared_interval,
                    funding_interval_status=DECLARED,
                    extra=dict(
                        szDecimals=u.get("szDecimals"),
                        onlyIsolated=u.get("onlyIsolated"),
                        isDelisted=u.get("isDelisted"),
                    ),
                )
            )
        return out

    def parse_snapshots(self, obj, at, sha):
        out = []
        for u, c in self._pairs(obj):
            if u.get("isDelisted"):
                continue
            name = need(u, "name")
            out.append(
                R.snapshot(
                    self,
                    name,
                    name,
                    c.get("funding"),
                    at,
                    sha,
                    "hyperliquid.metaAndAssetCtxs",
                    mark_price=num(c.get("markPx")),
                    oracle_price=num(c.get("oraclePx")),
                    premium=num(c.get("premium")),
                )
            )
        return out

    # --- settled funding -----------------------------------------------------------
    def settlements(self, market, start, end, at):
        ctx = dict(asset=market["asset"], market_id=market["market_id"])
        out, stats, cursor = [], {}, int(start * 1000)
        while cursor < end * 1000:
            body = {
                "type": "fundingHistory",
                "coin": market["market_id"],
                "startTime": cursor,
                "endTime": int(end * 1000),
            }
            obj, sha = self.info(body, "hyperliquid.fundingHistory")
            rows, st = self.parse_settlements(obj, ctx, at, sha)
            for k, v in st.items():
                stats[k] = stats.get(k, 0) + v
            out += rows
            if len(obj) < HISTORY_PAGE or not rows:
                break
            cursor = int(round(max(r["settled_at_api"] for r in rows) * 1000)) + 1
        rows, merged = R.dedupe_settlements(out)
        for k, v in merged.items():
            stats[k] = stats.get(k, 0) + v
        return rows, stats

    def parse_settlements(self, obj, ctx, at, sha):
        if not isinstance(obj, list):
            raise SchemaError("fundingHistory: liste bekleniyordu")
        rows = []
        for x in obj:
            t = num(need(x, "time"))
            if x.get("coin") not in (None, ctx["market_id"]):
                raise SchemaError("fundingHistory: başka piyasanın kaydı")
            rows.append(
                R.settlement(
                    self,
                    ctx,
                    t / 1000,
                    need(x, "fundingRate"),
                    at,
                    sha,
                    "hyperliquid.fundingHistory",
                    premium=num(x.get("premium")),
                )
            )
        return rows, R.series_stats([r["settled_at"] for r in rows], at)

    # --- candles ---------------------------------------------------------------------
    def candles(self, market, start, end, at):
        ctx = dict(asset=market["asset"], market_id=market["market_id"])
        body = {
            "type": "candleSnapshot",
            "req": {
                "coin": market["market_id"],
                "interval": "1h",
                "startTime": int(start * 1000),
                "endTime": int(end * 1000),
            },
        }
        obj, sha = self.info(body, "hyperliquid.candleSnapshot")
        return self.parse_candles(obj, ctx, at, sha)

    def parse_candles(self, obj, ctx, at, sha):
        if not isinstance(obj, list):
            raise SchemaError("candleSnapshot: liste bekleniyordu")
        rows = []
        for x in obj:
            if x.get("i") not in (None, "1h"):
                raise SchemaError("candleSnapshot: 1h olmayan mum")
            t = num(need(x, "t")) / 1000
            rows.append(
                R.candle(
                    self,
                    ctx,
                    t,
                    x.get("o"),
                    x.get("h"),
                    x.get("l"),
                    need(x, "c"),
                    x.get("v"),
                    at,
                    sha,
                    "hyperliquid.candleSnapshot",
                )
            )
        stats = R.series_stats([r["open_time"] for r in rows], at)
        return R.dedupe(rows, "open_time"), stats

    # --- book ------------------------------------------------------------------------
    def orderbook(self, market, at):
        body = {"type": "l2Book", "coin": market["market_id"]}
        obj, sha = self.info(body, "hyperliquid.l2Book")
        return self.parse_book(
            obj, dict(asset=market["asset"], market_id=market["market_id"]), at, sha
        )

    def parse_book(self, obj, ctx, at, sha):
        levels = need(obj, "levels")
        if not isinstance(levels, list) or len(levels) != 2:
            raise SchemaError("l2Book: iki taraf bekleniyordu")
        side = lambda xs: [(num(need(x, "px")), num(need(x, "sz"))) for x in xs]
        t = num(obj.get("time"))
        return book_record(
            self.venue,
            ctx["asset"],
            ctx["market_id"],
            side(levels[0]),
            side(levels[1]),
            t / 1000 if t else None,
            at,
            sha,
            self.cfg["book_levels"],
        )

    def replay(self, endpoint, obj, ctx, at, sha):
        body = ctx.get("body") or {}
        if endpoint == "hyperliquid.metaAndAssetCtxs":
            return dict(
                market=self.parse_markets(obj, at, sha),
                funding_snapshot=self.parse_snapshots(obj, at, sha),
            )
        req = body.get("req") or {}
        coin = body.get("coin") or req.get("coin")
        mctx = dict(asset=coin, market_id=coin)
        if endpoint == "hyperliquid.fundingHistory":
            return dict(settlement=self.parse_settlements(obj, mctx, at, sha)[0])
        if endpoint == "hyperliquid.candleSnapshot":
            return dict(candle=self.parse_candles(obj, mctx, at, sha)[0])
        if endpoint == "hyperliquid.l2Book":
            return dict(orderbook=[self.parse_book(obj, mctx, at, sha)])
        return {}


__all__ = ["Hyperliquid", "OBSERVED", "UNKNOWN"]
