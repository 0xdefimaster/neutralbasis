"""MOCK venues for the tests only. These responses are synthetic, shaped after the
fields the project's parsers read; they are never reported as market evidence."""

import json
import urllib.parse

T0 = 1_790_000_000 // 3600 * 3600  # an exact hour


class Clock:
    def __init__(self, t=T0 + 1800):
        self.t = float(t)

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeVenues:
    """A transport answering the four venues' public endpoints from synthetic data.

    `published` maps venue -> published rate (in that venue's raw unit) and `settled`
    venue -> settled rate; `fail` maps an endpoint substring to a list of responses
    (status, body) served before the normal answer."""

    def __init__(self, clock, assets=("BTC", "ETH")):
        self.clock = clock
        self.assets = list(assets)
        self.calls = []
        self.fail = {}
        self.published = dict(
            Hyperliquid=0.0000125,
            Lighter=0.000012,
            LighterCore=0.000012,
            Extended=0.0000125,
        )
        self.settled = dict(
            Hyperliquid=0.0000125,
            Lighter=0.0012,
            LighterCore=0.0012,
            Extended=0.0000125,
        )
        self.direction = "long"
        self.gap = 3600
        self.missing_hours = set()
        self.shuffle_candles = False
        self.duplicate_payment = False
        # Past payments the venue later reports differently ({time: rate}, applied to
        # every venue), and ones it no longer lists.
        self.revised = {}
        self.withdrawn = set()
        # Variational stats body (MOCK keys; the real ones are what discovery finds).
        self.variational = lambda now: {
            "listings": [
                {"ticker": a, "mock_value": "1.0", "mock_nested": {"a": 1}}
                for a in self.assets
            ],
            "mock_total": 2,
        }

    def __call__(self, method, url, body, timeout):
        self.calls.append((method, url, body))
        for key, queue in self.fail.items():
            if key in url + json.dumps(body or {}) and queue:
                status, payload = queue.pop(0)
                if isinstance(payload, Exception):
                    raise payload
                return (
                    status,
                    {},
                    (
                        payload
                        if isinstance(payload, bytes)
                        else json.dumps(payload).encode()
                    ),
                )
        out = self.answer(method, url, body)
        return 200, {"date": "x", "x-request-id": "req-1"}, json.dumps(out).encode()

    # --- helpers ---------------------------------------------------------------------
    def hours(self, start, end):
        h = int(start) // 3600 * 3600
        if h < start:
            h += 3600
        out = []
        while h <= min(end, self.clock()):
            if h not in self.missing_hours:
                out.append(h)
            h += 3600
        return out

    def payments(self, start, end):
        first = int(start) // self.gap * self.gap
        out = []
        t = first if first >= start else first + self.gap
        while t <= min(end, self.clock()):
            out.append(t)
            t += self.gap
        out = [t for t in out if t not in self.withdrawn]
        if self.duplicate_payment and out:
            out.append(out[-1])
        return out

    def answer(self, method, url, body):
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        now = self.clock()
        if "hyperliquid" in u.netloc:
            assert method == "POST"
            t = body["type"]
            if t == "metaAndAssetCtxs":
                return [
                    {
                        "universe": [
                            {"name": a, "szDecimals": 5, "maxLeverage": 40}
                            for a in self.assets
                        ]
                    },
                    [
                        {
                            "funding": str(self.published["Hyperliquid"]),
                            "markPx": "100.0",
                            "oraclePx": "99.9",
                            "premium": "0.0001",
                        }
                        for _ in self.assets
                    ],
                ]
            if t == "fundingHistory":
                ts = self.payments(body["startTime"] / 1000, body["endTime"] / 1000)[
                    :500
                ]
                return [
                    {
                        "coin": body["coin"],
                        "fundingRate": str(
                            self.revised.get(x, self.settled["Hyperliquid"])
                        ),
                        "premium": "0.0",
                        "time": x * 1000,
                    }
                    for x in ts
                ]
            if t == "candleSnapshot":
                r = body["req"]
                hs = [
                    h
                    for h in self.hours(r["startTime"] / 1000, r["endTime"] / 1000)
                    if h + 3600 <= now + 3600
                ]
                rows = [
                    {
                        "t": h * 1000,
                        "T": h * 1000 + 3599999,
                        "s": r["coin"],
                        "i": "1h",
                        "o": "100",
                        "h": "101",
                        "l": "99",
                        "c": "100.5",
                        "v": "10",
                        "n": 5,
                    }
                    for h in hs
                ]
                if self.shuffle_candles and len(rows) > 2:
                    # Mixed order: one entry out of place in an ascending list.
                    rows[0], rows[1] = rows[1], rows[0]
                return rows
            if t == "l2Book":
                return {
                    "coin": body["coin"],
                    "time": int(now * 1000),
                    "levels": [
                        [{"px": "99.9", "sz": "2", "n": 1}],
                        [{"px": "100.1", "sz": "3", "n": 1}],
                    ],
                }
        if "lighter" in u.netloc:
            venue = "Lighter" if "rh.lighter" in u.netloc else "LighterCore"
            path = u.path.rsplit("/", 1)[1]
            ids = {a: i + 1 for i, a in enumerate(self.assets)}
            if path == "orderBookDetails":
                return {
                    "code": 200,
                    "order_book_details": [
                        {
                            "symbol": a,
                            "market_id": i,
                            "market_type": "perp",
                            "status": "active",
                            "is_frozen": False,
                            "mark_price": "100.0",
                            "index_price": "99.95",
                            "supported_size_decimals": 4,
                            "supported_price_decimals": 2,
                            "min_base_amount": "0.001",
                            "min_quote_amount": "10",
                            "default_initial_margin_fraction": 500,
                            "maintenance_margin_fraction": 120,
                        }
                        for a, i in ids.items()
                    ],
                }
            if path == "funding-rates":
                return {
                    "code": 200,
                    "funding_rates": [
                        {
                            "market_id": i,
                            "exchange": "lighter",
                            "symbol": a,
                            "rate": self.published[venue],
                        }
                        for a, i in ids.items()
                    ]
                    + [
                        {
                            "market_id": 1,
                            "exchange": "binance",
                            "symbol": "BTC",
                            "rate": 0.5,
                        }
                    ],
                }
            if path == "fundings":
                ts = self.payments(
                    float(q["start_timestamp"]), float(q["end_timestamp"])
                )
                return {
                    "code": 200,
                    "fundings": [
                        {
                            "timestamp": x,
                            "rate": str(abs(self.revised.get(x, self.settled[venue]))),
                            "direction": self.direction,
                            "value": "0.01",
                        }
                        for x in ts
                    ],
                }
            if path == "candles":
                hs = self.hours(float(q["start_timestamp"]), float(q["end_timestamp"]))
                return {
                    "code": 200,
                    "c": [
                        {"t": h * 1000, "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 7}
                        for h in hs
                    ],
                }
            if path == "orderBookOrders":
                return {
                    "code": 200,
                    "bids": [{"price": "99.9", "remaining_base_amount": "1.5"}],
                    "asks": [{"price": "100.1", "remaining_base_amount": "2"}],
                }
        if "extended" in u.netloc:
            p = u.path.split("/api/v1/")[1]
            if p == "info/markets":
                return {
                    "status": "OK",
                    "data": [
                        {
                            "name": a + "-USD",
                            "assetName": a,
                            "collateralAssetName": "USD",
                            "active": True,
                            "status": "ACTIVE",
                            "type": "PERPETUAL",
                            "category": "Crypto",
                            "isRfq": False,
                            "marketStats": {
                                "markPrice": "100",
                                "indexPrice": "99.98",
                                "fundingRate": str(self.published["Extended"]),
                                "nextFundingRate": (int(now) // 3600 * 3600 + 3600)
                                * 1000,
                            },
                            "tradingConfig": {
                                "minOrderSize": "0.001",
                                "minOrderSizeChange": "0.001",
                                "minPriceChange": "0.1",
                                "maxLeverage": "50",
                                "maxMarketOrderValue": "1000000",
                            },
                        }
                        for a in self.assets
                    ],
                }
            if p.endswith("/funding"):
                m = p.split("/")[1]
                ts = self.payments(
                    float(q["startTime"]) / 1000, float(q["endTime"]) / 1000
                )
                return {
                    "status": "OK",
                    "data": [
                        {
                            "m": m,
                            "T": x * 1000,
                            "f": str(self.revised.get(x, self.settled["Extended"])),
                        }
                        for x in ts
                    ],
                }
            if "/candles/" in p:
                end = float(q["endTime"]) / 1000
                hs = [h for h in self.hours(end - 750 * 3600, end) if h < end]
                return {
                    "status": "OK",
                    "data": [
                        {
                            "T": h * 1000,
                            "o": "100",
                            "h": "101",
                            "l": "99",
                            "c": "100.5",
                            "v": "3",
                        }
                        for h in hs
                    ],
                }
            if p.endswith("/orderbook"):
                return {
                    "status": "OK",
                    "data": {
                        "bid": [{"price": "99.9", "qty": "1"}],
                        "ask": [{"price": "100.1", "qty": "1"}],
                    },
                }
        if "variational" in u.netloc:
            assert method == "GET" and u.path == "/metadata/stats"
            return self.variational(now)
        raise AssertionError("Beklenmeyen istek: %s %s" % (method, url))
