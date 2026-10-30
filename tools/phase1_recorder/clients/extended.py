"""Extended public market data (https://api.starknet.extended.exchange/api/v1/, bot.E).

Endpoints, as the project already reads them (bot.Feed):

  info/markets                     markets, mark/index price, current funding rate and
                                   next funding time (marketStats)
  info/<market>/funding            settled funding (T, f)
  info/candles/<market>/trades     1h trade candles (interval=PT1H)
  info/markets/<market>/orderbook  book levels

The settled history carries no payer direction: it is stored as UNKNOWN, never derived.
"""

from . import records as R
from .base import (
    DECLARED,
    OBSERVED,
    UNKNOWN,
    Client,
    SchemaError,
    book_record,
    epoch,
    need,
    num,
)

HOST = "https://api.starknet.extended.exchange/api/v1/"
CANDLE_LIMIT = 750


class Extended(Client):
    venue = "Extended"
    declared_interval = 3600
    declared_direction = "Pozitif oran: long öder (beyan)"
    candle_price_type = "TRADE"

    def get(self, path, endpoint, params=None):
        return self.http.request(endpoint, HOST + path, params=params)

    @staticmethod
    def _data(obj, what):
        if not isinstance(obj, dict):
            raise SchemaError(what + ": nesne bekleniyordu")
        if obj.get("status", "OK") != "OK":
            raise SchemaError(what + ": API durumu %s" % obj.get("status"))
        return need(obj, "data")

    def _markets_response(self):
        return self.get("info/markets", "extended.info/markets")

    def markets(self, at):
        obj, sha = self._markets_response()
        return self.parse_markets(obj, at, sha)

    def funding_snapshots(self, at):
        obj, sha = self._markets_response()
        return self.parse_snapshots(obj, at, sha)

    def parse_markets(self, obj, at, sha):
        out = []
        for d in self._data(obj, "info/markets"):
            c = d.get("tradingConfig") or {}
            out.append(
                R.market(
                    self.venue,
                    need(d, "assetName"),
                    need(d, "name"),
                    at,
                    sha,
                    base=d.get("assetName"),
                    quote=d.get("collateralAssetName"),
                    market_type=d.get("type"),
                    contract_size=1.0,
                    contract_size_status=DECLARED,
                    contract_size_source="connectors.NativeConnector: miktar taban birimde",
                    min_order_size=num(c.get("minOrderSize")),
                    amount_step=num(c.get("minOrderSizeChange")),
                    price_step=num(c.get("minPriceChange")),
                    max_leverage=num(c.get("maxLeverage")),
                    active=bool(d.get("active"))
                    and d.get("status") == "ACTIVE"
                    and d.get("type") == "PERPETUAL",
                    funding_interval=self.declared_interval,
                    funding_interval_status=DECLARED,
                    extra=dict(
                        status=d.get("status"),
                        category=d.get("category"),
                        isRfq=d.get("isRfq"),
                        maxMarketOrderValue=c.get("maxMarketOrderValue"),
                    ),
                )
            )
        return out

    def parse_snapshots(self, obj, at, sha):
        out = []
        for d in self._data(obj, "info/markets"):
            s = d.get("marketStats") or {}
            if "fundingRate" not in s:
                continue
            nxt = epoch(s.get("nextFundingRate"))
            out.append(
                R.snapshot(
                    self,
                    need(d, "assetName"),
                    need(d, "name"),
                    s.get("fundingRate"),
                    at,
                    sha,
                    "extended.info/markets",
                    mark_price=num(s.get("markPrice")),
                    index_price=num(s.get("indexPrice")),
                    next_funding_at=nxt,
                    next_funding_source=(
                        "API marketStats.nextFundingRate" if nxt else None
                    ),
                )
            )
        return out

    def settlements(self, market, start, end, at):
        ctx = dict(asset=market["asset"], market_id=market["market_id"])
        obj, sha = self.get(
            "info/%s/funding" % market["market_id"],
            "extended.info/funding",
            dict(startTime=int(start * 1000), endTime=int(end * 1000), limit=1000),
        )
        rows, stats = self.parse_settlements(obj, ctx, at, sha)
        # One payment per time is decided by the recorder (records.dedupe_settlements),
        # with the market's own tolerance.
        return sorted(rows, key=lambda r: r["settled_at_api"]), stats

    def parse_settlements(self, obj, ctx, at, sha):
        rows = []
        for x in self._data(obj, "info/funding"):
            if x.get("m") not in (None, ctx["market_id"]):
                raise SchemaError("info/funding: başka piyasanın kaydı")
            rows.append(
                R.settlement(
                    self,
                    ctx,
                    epoch(need(x, "T")),
                    need(x, "f"),
                    at,
                    sha,
                    "extended.info/funding",
                )
            )
        return rows, R.series_stats([r["settled_at"] for r in rows], at)

    def candles(self, market, start, end, at):
        ctx = dict(asset=market["asset"], market_id=market["market_id"])
        obj, sha = self.get(
            "info/candles/%s/trades" % market["market_id"],
            "extended.info/candles/trades",
            dict(interval="PT1H", limit=CANDLE_LIMIT, endTime=int(end * 1000)),
        )
        rows, stats = self.parse_candles(obj, ctx, at, sha)
        rows = [r for r in rows if start <= r["open_time"] < end]
        return rows, stats

    def parse_candles(self, obj, ctx, at, sha):
        rows = [
            R.candle(
                self,
                ctx,
                epoch(need(x, "T")),
                x.get("o"),
                x.get("h"),
                x.get("l"),
                need(x, "c"),
                x.get("v"),
                at,
                sha,
                "extended.info/candles/trades",
            )
            for x in self._data(obj, "info/candles")
        ]
        stats = R.series_stats([r["open_time"] for r in rows], at)
        return R.dedupe(rows, "open_time"), stats

    def orderbook(self, market, at):
        obj, sha = self.get(
            "info/markets/%s/orderbook" % market["market_id"], "extended.info/orderbook"
        )
        return self.parse_book(
            obj, dict(asset=market["asset"], market_id=market["market_id"]), at, sha
        )

    def parse_book(self, obj, ctx, at, sha):
        o = self._data(obj, "info/orderbook")
        side = lambda xs: [(num(need(x, "price")), num(need(x, "qty"))) for x in xs]
        return book_record(
            self.venue,
            ctx["asset"],
            ctx["market_id"],
            side(need(o, "bid")),
            side(need(o, "ask")),
            None,
            at,
            sha,
            self.cfg["book_levels"],
        )

    def replay(self, endpoint, obj, ctx, at, sha):
        if endpoint == "extended.info/markets":
            return dict(
                market=self.parse_markets(obj, at, sha),
                funding_snapshot=self.parse_snapshots(obj, at, sha),
            )
        if endpoint == "extended.info/funding":
            return dict(settlement=self.parse_settlements(obj, ctx, at, sha)[0])
        if endpoint == "extended.info/candles/trades":
            return dict(candle=self.parse_candles(obj, ctx, at, sha)[0])
        if endpoint == "extended.info/orderbook":
            return dict(orderbook=[self.parse_book(obj, ctx, at, sha)])
        return {}


__all__ = ["Extended", "OBSERVED", "UNKNOWN"]
