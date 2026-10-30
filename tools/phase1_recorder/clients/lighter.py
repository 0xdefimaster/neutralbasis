"""Lighter public market data, for two separate venues with separate hosts, markets and
market ids:

  * Lighter RH   (venue "Lighter")     https://api.rh.lighter.xyz/api/v1/  (risk_scan.RH)
  * Lighter Core (venue "LighterCore") https://mainnet.zklighter.elliot.ai/api/v1/ (bot.L)

Both run the same Lighter API, so one client class serves both; nothing is shared
between the two instances. Endpoints, as the project already reads them:

  orderBookDetails   markets, mark and index price          (bot.Feed.markets)
  fundings           settled funding with direction, value  (bot.Feed.funding_history)
  candles            1h trade candles                       (bot.Feed.candles)
  orderBookOrders    book levels                            (bot.Feed.book)
  funding-rates      current published rates; from CCXT's lighter adapter
                     (fetchFundingRates). Its rate unit is not documented to the
                     project: stored raw, unit UNKNOWN. The response also lists other
                     exchanges' rates; only entries with exchange == "lighter" are kept.

The project's parser reads the settled rate as a percentage whose sign is given by
the `direction` field ("long" -> +, "short" -> -); that reading is stored as DECLARED
next to the raw values, which are kept unchanged.
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

RH = "https://api.rh.lighter.xyz/api/v1/"
CORE = "https://mainnet.zklighter.elliot.ai/api/v1/"
CANDLE_WINDOW = 360 * 3600  # bot.Feed.candles fetches 30 days in two 360h windows
FUNDING_WINDOW = 15 * 86400
DIRECTION_SIGN = {"long": 1, "short": -1}


class Lighter(Client):
    declared_interval = 3600
    declared_direction = (
        "direction alanı: long -> pozitif, short -> negatif (bot.py ayrıştırıcısı)"
    )
    candle_price_type = "TRADE"

    def __init__(self, http, cfg, venue, host):
        super().__init__(http, cfg)
        self.venue = venue
        self.host = host
        self.tag = "lighter_rh" if venue == "Lighter" else "lighter_core"

    def get(self, path, params=None):
        return self.http.request(self.tag + "." + path, self.host + path, params=params)

    @staticmethod
    def _ok(obj, what):
        if not isinstance(obj, dict):
            raise SchemaError(what + ": nesne bekleniyordu")
        if obj.get("code", 200) != 200:
            raise SchemaError(what + ": API hata kodu %s" % obj.get("code"))
        return obj

    # --- markets -------------------------------------------------------------------
    def markets(self, at):
        obj, sha = self.get("orderBookDetails")
        return self.parse_markets(obj, at, sha)

    def parse_markets(self, obj, at, sha):
        out = []
        for d in need(self._ok(obj, "orderBookDetails"), "order_book_details"):
            sd, pd = num(d.get("supported_size_decimals")), num(
                d.get("supported_price_decimals")
            )
            imf = num(d.get("default_initial_margin_fraction"))
            out.append(
                R.market(
                    self.venue,
                    need(d, "symbol"),
                    need(d, "market_id"),
                    at,
                    sha,
                    base=d.get("symbol"),
                    market_type=d.get("market_type"),
                    contract_size=1.0,
                    contract_size_status=DECLARED,
                    contract_size_source="connectors.NativeConnector: miktar taban birimde",
                    min_order_size=num(d.get("min_base_amount")),
                    min_quote=num(d.get("min_quote_amount")),
                    amount_step=10 ** -int(sd) if sd is not None else None,
                    price_step=10 ** -int(pd) if pd is not None else None,
                    max_leverage=10000 / imf if imf else None,
                    active=d.get("status") == "active"
                    and d.get("market_type") == "perp"
                    and not d.get("is_frozen"),
                    funding_interval=self.declared_interval,
                    funding_interval_status=DECLARED,
                    extra=dict(
                        status=d.get("status"),
                        is_frozen=d.get("is_frozen"),
                        maintenance_margin_fraction=d.get(
                            "maintenance_margin_fraction"
                        ),
                        mark_price=d.get("mark_price"),
                        index_price=d.get("index_price"),
                    ),
                )
            )
        return out

    # --- current funding -------------------------------------------------------------
    def funding_snapshots(self, at):
        rates, rsha = self.get("funding-rates")
        details, dsha = self.get("orderBookDetails")
        return self.parse_snapshots(rates, details, at, rsha, dsha)

    def parse_snapshots(self, rates, details, at, sha, details_sha=None):
        prices = {}
        if details is not None:
            for d in need(self._ok(details, "orderBookDetails"), "order_book_details"):
                prices[str(d.get("market_id"))] = d
        out = []
        for x in need(self._ok(rates, "funding-rates"), "funding_rates"):
            if x.get("exchange") != "lighter":
                continue
            mid = need(x, "market_id")
            d = prices.get(str(mid), {})
            out.append(
                R.snapshot(
                    self,
                    x.get("symbol") or d.get("symbol"),
                    mid,
                    x.get("rate"),
                    at,
                    sha,
                    self.tag + ".funding-rates",
                    mark_price=num(d.get("mark_price")),
                    index_price=num(d.get("index_price")),
                    prices_raw_sha256=details_sha,
                )
            )
        return out

    # --- settled funding -------------------------------------------------------------
    def settlements(self, market, start, end, at):
        ctx = dict(asset=market["asset"], market_id=market["market_id"])
        out, stats, left = [], {}, int(start)
        end = int(end)
        while left < end:
            right = min(end, left + FUNDING_WINDOW)
            obj, sha = self.get(
                "fundings",
                dict(
                    market_id=market["market_id"],
                    resolution="1h",
                    start_timestamp=left,
                    end_timestamp=right,
                    count_back=750,
                ),
            )
            rows, st = self.parse_settlements(obj, ctx, at, sha)
            for k, v in st.items():
                stats[k] = stats.get(k, 0) + v
            out += rows
            left = right
        # One payment per time is decided by the recorder (records.dedupe_settlements),
        # with the market's own tolerance.
        return sorted(out, key=lambda r: r["settled_at_api"]), stats

    def parse_settlements(self, obj, ctx, at, sha):
        rows = []
        for x in need(self._ok(obj, "fundings"), "fundings"):
            t = epoch(need(x, "timestamp"))
            direction = x.get("direction")
            sign = DIRECTION_SIGN.get(direction)
            rate = num(x.get("rate"))
            rows.append(
                R.settlement(
                    self,
                    ctx,
                    t,
                    x.get("rate"),
                    at,
                    sha,
                    self.tag + ".fundings",
                    # The project's reading; None when the direction is not one it knows.
                    settled_rate=(
                        abs(rate) / 100 * sign if rate is not None and sign else None
                    ),
                    settled_rate_status=DECLARED if sign else UNKNOWN,
                    direction_raw=direction,
                    direction_status=OBSERVED if direction is not None else UNKNOWN,
                    value_raw=x.get("value"),
                )
            )
        return rows, R.series_stats([r["settled_at"] for r in rows], at)

    # --- candles ---------------------------------------------------------------------
    def candles(self, market, start, end, at):
        ctx = dict(asset=market["asset"], market_id=market["market_id"])
        out, stats, left = [], {}, int(start)
        end = int(end)
        while left < end:
            right = min(end, left + CANDLE_WINDOW)
            obj, sha = self.get(
                "candles",
                dict(
                    market_id=market["market_id"],
                    resolution="1h",
                    start_timestamp=left,
                    end_timestamp=right,
                    count_back=500,
                ),
            )
            rows, st = self.parse_candles(obj, ctx, at, sha)
            for k, v in st.items():
                stats[k] = stats.get(k, 0) + v
            out += rows
            left = right
        return R.dedupe(out, "open_time"), stats

    def parse_candles(self, obj, ctx, at, sha):
        rows = []
        for x in need(self._ok(obj, "candles"), "c"):
            if x.get("c") is None:
                continue
            rows.append(
                R.candle(
                    self,
                    ctx,
                    epoch(need(x, "t")),
                    x.get("o"),
                    x.get("h"),
                    x.get("l"),
                    x.get("c"),
                    x.get("v"),
                    at,
                    sha,
                    self.tag + ".candles",
                )
            )
        stats = R.series_stats([r["open_time"] for r in rows], at)
        return R.dedupe(rows, "open_time"), stats

    # --- book ------------------------------------------------------------------------
    def orderbook(self, market, at):
        obj, sha = self.get(
            "orderBookOrders", dict(market_id=market["market_id"], limit=250)
        )
        return self.parse_book(
            obj, dict(asset=market["asset"], market_id=market["market_id"]), at, sha
        )

    def parse_book(self, obj, ctx, at, sha):
        o = self._ok(obj, "orderBookOrders")
        side = lambda xs: [
            (num(need(x, "price")), num(need(x, "remaining_base_amount"))) for x in xs
        ]
        return book_record(
            self.venue,
            ctx["asset"],
            ctx["market_id"],
            side(need(o, "bids")),
            side(need(o, "asks")),
            None,
            at,
            sha,
            self.cfg["book_levels"],
        )

    def replay(self, endpoint, obj, ctx, at, sha):
        name = endpoint.split(".", 1)[1]
        if name == "orderBookDetails":
            return dict(market=self.parse_markets(obj, at, sha))
        if name == "funding-rates":
            return dict(funding_snapshot=self.parse_snapshots(obj, None, at, sha))
        if name == "fundings":
            return dict(settlement=self.parse_settlements(obj, ctx, at, sha)[0])
        if name == "candles":
            return dict(candle=self.parse_candles(obj, ctx, at, sha)[0])
        if name == "orderBookOrders":
            return dict(orderbook=[self.parse_book(obj, ctx, at, sha)])
        return {}
