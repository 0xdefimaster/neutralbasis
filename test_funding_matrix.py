import math, os, tempfile, threading, unittest
from unittest import mock
import bot, funding_matrix, wide_markets
from funding_matrix import build, paid7, top_pairs
from history_store import HistoryStore
from wide_markets import WideMarkets

T0 = 3_000 * 86400


class FakeExchange:
    has = {"fetchFundingRates": True}

    def __init__(self):
        self.markets = {
            "BTC/USDT:USDT": dict(
                symbol="BTC/USDT:USDT",
                base="BTC",
                quote="USDT",
                swap=True,
                linear=True,
                taker=0.0005,
            ),
            "BTC/USDC:USDC": dict(
                symbol="BTC/USDC:USDC",
                base="BTC",
                quote="USDC",
                swap=True,
                linear=True,
                taker=0.0002,
            ),
            "ZRO/USDT:USDT": dict(
                symbol="ZRO/USDT:USDT",
                base="ZRO",
                quote="USDT",
                swap=True,
                linear=True,
                taker=0.0005,
            ),
            "OLD/USDT:USDT": dict(
                symbol="OLD/USDT:USDT",
                base="OLD",
                quote="USDT",
                swap=True,
                linear=True,
                active=False,
            ),
            "ETH/USD:ETH": dict(
                symbol="ETH/USD:ETH", base="ETH", quote="USD", swap=True, linear=False
            ),
        }
        self.calls = 0

    def fetch_funding_rates(self):
        self.calls += 1
        return {
            "BTC/USDT:USDT": dict(
                fundingRate=0.0008,
                interval="8h",
                nextFundingTimestamp=(T0 + 3600) * 1000,
            ),
            "ZRO/USDT:USDT": dict(
                fundingRate=-0.0004,
                interval="4h",
                nextFundingTimestamp=(T0 + 600) * 1000,
            ),
            "OLD/USDT:USDT": dict(fundingRate=0.01, interval="1h"),
        }


class UniverseTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(wide_markets, "now", lambda: T0)
        p.start()
        self.addCleanup(p.stop)
        self.wide = WideMarkets(None, ["BTC"], threading.Event())

    def test_bulk_call_covers_every_listed_linear_perp(self):
        ex = FakeExchange()
        self.wide.scan_universe("Binance", ex)
        u = self.wide.universe_snapshot()["Binance"]
        self.assertEqual(ex.calls, 1)
        self.assertEqual(set(u), {"BTC", "ZRO"})
        self.assertEqual(u["BTC"]["symbol"], "BTC/USDT:USDT")
        self.assertAlmostEqual(u["BTC"]["rate"], 0.0001)
        self.assertEqual(u["ZRO"]["interval"], 4 * 3600)
        self.assertAlmostEqual(u["ZRO"]["rate"], -0.0001)

    def test_venue_without_bulk_endpoint_is_skipped(self):
        ex = FakeExchange()
        ex.has = {}
        self.assertEqual(self.wide.scan_universe("Bybit", ex), {})
        self.assertEqual(ex.calls, 0)

    def test_stale_universe_is_hidden(self):
        self.wide.scan_universe("Binance", FakeExchange())
        with mock.patch.object(wide_markets, "now", lambda: T0 + 301):
            self.assertEqual(self.wide.universe_snapshot()["Binance"], {})


class LiquidExchange(FakeExchange):
    has = {"fetchFundingRates": True, "fetchTickers": True, "fetchOpenInterests": True}

    def fetch_funding_rates(self):
        rates = super().fetch_funding_rates()
        rates["BTC/USDT:USDT"]["markPrice"] = 50000.0
        return rates

    def fetch_tickers(self, symbols=None):
        return {
            "BTC/USDT:USDT": dict(quoteVolume=2.5e8),
            "ZRO/USDT:USDT": dict(quoteVolume=None, baseVolume=1000.0, last=3.0),
        }

    def fetch_open_interests(self, symbols=None):
        return {"BTC/USDT:USDT": dict(openInterestAmount=100.0)}


class UniverseLiquidityTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(wide_markets, "now", lambda: T0)
        p.start()
        self.addCleanup(p.stop)
        self.wide = WideMarkets(None, ["BTC"], threading.Event())

    def test_bulk_volume_and_open_interest(self):
        self.wide.scan_universe("Binance", LiquidExchange())
        u = self.wide.universe_snapshot()["Binance"]
        self.assertEqual(u["BTC"]["volume"], 2.5e8)
        self.assertEqual(u["BTC"]["oi"], 100.0 * 50000.0)
        self.assertEqual(u["ZRO"]["volume"], 3000.0)
        self.assertIsNone(u["ZRO"]["oi"])

    def test_venue_without_liquidity_calls_stays_unknown(self):
        self.wide.scan_universe("Binance", FakeExchange())
        u = self.wide.universe_snapshot()["Binance"]
        self.assertIsNone(u["BTC"]["volume"])
        self.assertIsNone(u["BTC"]["oi"])

    def test_failing_ticker_call_does_not_drop_funding(self):
        class Broken(LiquidExchange):
            def fetch_tickers(self, symbols=None):
                raise RuntimeError("rate limited")

        self.wide.scan_universe("Binance", Broken())
        u = self.wide.universe_snapshot()["Binance"]
        self.assertEqual(set(u), {"BTC", "ZRO"})
        self.assertIsNone(u["BTC"]["volume"])


def row(v, a, rate, interval=3600, fee=0.0005, received=T0, mark=None):
    return dict(
        venue=v,
        asset=a,
        rate=rate,
        interval=interval,
        fee=fee,
        received=received,
        mark=mark,
    )


class MatrixTests(unittest.TestCase):
    def test_paid7_needs_full_week(self):
        self.assertEqual(paid7({("A", "X"): (0.01, 21)}, "A", "X", 28800), 0.01)
        self.assertIsNone(paid7({("A", "X"): (0.01, 20)}, "A", "X", 28800))
        self.assertIsNone(paid7({}, "A", "X", 3600))

    def test_cells_spread_and_sources(self):
        native = [row("Lighter", "BTC", 0.00002)]
        rows = [
            row("Binance", "BTC", 0.0001),
            row("Binance", "BTC", 0.5, received=T0 - 999),
        ]
        universe = {
            "Binance": {"BTC": dict(rate=9.0, interval=28800, fee=None, received=T0)},
            "Bybit": {
                "BTC": dict(rate=-0.00005, interval=28800, fee=0.00055, received=T0),
                "ZRO": dict(rate=0.0003, interval=14400, fee=0.00055, received=T0),
            },
        }
        sums = {("Bybit", "BTC"): (-0.0012, 21)}
        m = build(
            native,
            rows,
            universe,
            sums,
            {"Lighter": "DEX", "Bybit": "CEX", "Binance": "CEX"},
            {},
            at=T0,
        )
        btc = next(x for x in m["assets"] if x["asset"] == "BTC")
        # Selected-scan row wins over the bulk universe entry; stale rows are dropped.
        self.assertEqual(btc["cells"]["Binance"][0], 0.0001)
        self.assertAlmostEqual(btc["max_spread"], 0.00015)
        self.assertEqual(btc["cells"]["Bybit"][3], -0.0012)
        self.assertIsNone(btc["cells"]["Binance"][3])
        zro = next(x for x in m["assets"] if x["asset"] == "ZRO")
        self.assertIsNone(zro["max_spread"])
        self.assertEqual(m["assets"][0]["asset"], "BTC")
        self.assertEqual(m["venues"][0]["kind"], "CEX")
        self.assertEqual(
            {v["id"] for v in m["venues"]}, {"Lighter", "Binance", "Bybit"}
        )

    def test_top_pairs_names_both_extremes(self):
        m = build(
            [],
            [row("A", "X", 0.001), row("B", "X", -0.001), row("C", "X", 0.0)],
            {},
            {},
            {},
            {},
            at=T0,
        )
        self.assertEqual(top_pairs(m), [("A", "X", 3600), ("B", "X", 3600)])


class FundingSumsTests(unittest.TestCase):
    def test_sums_by_market(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        store = bot.Store(os.path.join(d.name, "h.db"))
        self.addCleanup(store.db.close)
        with mock.patch("history_store.now", lambda: T0):
            h = HistoryStore(store)
            h.add_funding(
                "A", "X", [dict(ts=T0 - k * 3600, rate=0.001) for k in range(10)]
            )
        sums = h.funding_sums(T0 - 5 * 3600 - 1)
        self.assertEqual(sums[("A", "X")][1], 6)
        self.assertAlmostEqual(sums[("A", "X")][0], 0.006)


class ListingTests(unittest.TestCase):
    def m(self, cells):
        return dict(
            assets=[
                dict(asset=a, cells={v: [0, 3600, None, None] for v in vs})
                for a, vs in cells.items()
            ]
        )

    def test_first_scan_seeds_then_new_assets_are_listed(self):
        from funding_matrix import track_listings

        st, ls = track_listings({}, self.m({"BTC": ["A", "B"]}), at=T0)
        self.assertEqual(ls, [])
        # C is a new venue: seeded silently. NEW appears on A, later on B too.
        st, ls = track_listings(
            st, self.m({"BTC": ["A", "B", "C"], "NEW": ["A"], "OLD": ["C"]}), at=T0 + 60
        )
        self.assertEqual([x["asset"] for x in ls], ["NEW"])
        st, ls = track_listings(
            st,
            self.m({"BTC": ["A"], "NEW": ["A", "B"], "OLD": ["C", "A"]}),
            at=T0 + 120,
        )
        self.assertEqual(ls[0]["venues"], ["A", "B"])
        self.assertEqual(ls[0]["ts"], T0 + 60)
        self.assertEqual(len(ls), 1)
        st, ls = track_listings(st, self.m({}), at=T0 + 60 + 7 * 86400 + 1)
        self.assertEqual(ls, [])


class PriceSpreadTests(unittest.TestCase):
    def test_widest_mark_gap_drops_ticker_clashes(self):
        from funding_matrix import price_spread

        c = lambda m: [0.0, 3600, None, None, m]
        r = price_spread(
            {"A": c(100.0), "B": c(101.0), "C": c(100.5), "D": c(3.2), "E": c(None)}
        )
        self.assertEqual((r["price_long"], r["price_short"]), ("A", "B"))
        self.assertAlmostEqual(r["price_spread"], math.log(1.01))
        self.assertIsNone(price_spread({"A": c(100.0), "D": c(3.2)})["price_spread"])
        self.assertIsNone(price_spread({"A": c(100.0)})["price_spread"])

    def test_build_carries_marks(self):
        m = build(
            [],
            [row("A", "X", 0.0, mark=10.0), row("B", "X", 0.0, mark=10.2)],
            {},
            {},
            {},
            {},
            at=T0,
        )
        x = m["assets"][0]
        self.assertEqual((x["price_long"], x["price_short"]), ("A", "B"))


if __name__ == "__main__":
    unittest.main()


class LiquidityCellTests(unittest.TestCase):
    def test_cells_carry_volume_and_oi_and_native_is_curated(self):
        native = [row("Lighter", "BTC", 0.0001)]
        rows = [dict(row("OKX", "BTC", 0.0002), volume=5e6, oi=2e6)]
        universe = {
            "Gate": {"BTC": dict(rate=0.0003, interval=3600, received=T0, volume=7e5)}
        }
        m = build(native, rows, universe, {}, {}, {}, at=T0)
        cells = m["assets"][0]["cells"]
        self.assertEqual(cells["OKX"][5:], [5e6, 2e6])
        self.assertEqual(cells["Gate"][5:], [7e5, None])
        self.assertEqual(cells["Lighter"][5:], [None, None])
        curated = {v["id"]: v["curated"] for v in m["venues"]}
        self.assertEqual(curated, {"Lighter": True, "OKX": False, "Gate": False})


class HourlySinceTests(unittest.TestCase):
    def test_venue_ignoring_start_with_limit_is_retried_without_limit(self):
        H = 3600000
        latest = [[100 * H + i * H, 0, 0, 0, 1.0, 0] for i in range(500)]
        from_start = [[10 * H + i * H, 0, 0, 0, 1.0, 0] for i in range(2000)]

        class Htx:
            calls = []

            def fetch_ohlcv(self, symbol, tf, since, limit=None):
                self.calls.append(limit)
                return latest if limit else from_start

        ex = Htx()
        self.assertEqual(wide_markets.hourly_since(ex, "X", 10 * H)[0][0], 10 * H)
        self.assertEqual(ex.calls, [500, None])

    def test_normal_venue_makes_one_call(self):
        H = 3600000

        class Normal:
            calls = 0

            def fetch_ohlcv(self, symbol, tf, since, limit=None):
                self.calls += 1
                return [[since + i * H, 0, 0, 0, 1.0, 0] for i in range(limit or 10)]

        ex = Normal()
        self.assertEqual(wide_markets.hourly_since(ex, "X", 5 * H)[0][0], 5 * H)
        self.assertEqual(ex.calls, 1)
