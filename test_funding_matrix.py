import os, tempfile, threading, unittest
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


def row(v, a, rate, interval=3600, fee=0.0005, received=T0):
    return dict(
        venue=v, asset=a, rate=rate, interval=interval, fee=fee, received=received
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


if __name__ == "__main__":
    unittest.main()
