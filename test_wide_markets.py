import unittest, threading, time
from types import SimpleNamespace
from wide_markets import (
    CATALOG,
    LABELS,
    funding_interval,
    finite,
    expanded_view,
    comparable_prices,
)
import test_desk_analytics as fixtures


class WideTests(unittest.TestCase):
    def test_full_catalog_honest_capabilities(self):
        self.assertEqual(len(CATALOG), 63)
        self.assertEqual(sum(v["scan"] for v in CATALOG.values()), 30)
        self.assertFalse(CATALOG["GMX"]["scan"])
        self.assertFalse(CATALOG["Binance"]["paper"])
        self.assertTrue(CATALOG["Binance"]["scan"])

    def test_core_and_robinhood_are_distinct(self):
        self.assertNotEqual(LABELS["Lighter"], LABELS["LighterCore"])
        self.assertIsNone(CATALOG["Lighter"]["adapter"])
        self.assertEqual(CATALOG["LighterCore"]["adapter"], "lighter")

    def test_no_guessed_default_period(self):
        self.assertIsNone(funding_interval({"fundingRate": 0.01}, {"info": {}}))
        self.assertEqual(funding_interval({"interval": "8h"}, {}), 28800)
        self.assertEqual(funding_interval({"interval": "30m"}, {}), 1800)

    def test_nonfinite_rejected(self):
        self.assertIsNone(finite("NaN"))
        self.assertIsNone(finite("inf"))
        self.assertIsNone(finite(None))
        self.assertEqual(finite("3.2"), 3.2)

    def test_last_trade_is_not_an_executable_spot_quote(self):
        a = dict(received=100, price_type="last_trade")
        b = dict(received=100, price_type="mark")
        self.assertFalse(comparable_prices(a, b, 100))
        a["price_type"] = "best_ask"
        self.assertTrue(comparable_prices(a, b, 100))

    def test_eight_hour_funding_coverage(self):
        fixture = fixtures.AnalyticsTests()
        fixture.setUp()
        fixture.rates = {
            v: [r for r in rows if r["ts"] % (8 * 3600) == 0]
            for v, rows in fixture.rates.items()
        }
        r = fixture.run_model(intervals={"A": 28800, "B": 28800})
        self.assertEqual(r["payment_counts"], {"A": 3, "B": 3})

    def test_missing_eight_hour_payment_is_not_zero(self):
        fixture = fixtures.AnalyticsTests()
        fixture.setUp()
        fixture.rates = {
            v: [r for r in rows if r["ts"] % (8 * 3600) == 0]
            for v, rows in fixture.rates.items()
        }
        fixture.rates["B"].pop(1)
        with self.assertRaisesRegex(ValueError, "funding geçmişinde"):
            fixture.run_model(intervals={"A": 28800, "B": 28800})

    def test_spot_has_no_funding(self):
        fixture = fixtures.AnalyticsTests()
        fixture.setUp()
        fixture.rates["A"] = []
        r = fixture.run_model(intervals={"A": 0, "B": 3600})
        self.assertEqual(r["payment_counts"]["A"], 0)
        self.assertGreater(r["funding"], 0)

    def test_duplicate_funding_not_double_paid(self):
        fixture = fixtures.AnalyticsTests()
        fixture.setUp()
        before = fixture.run_model()["funding"]
        fixture.rates["A"].append(dict(fixture.rates["A"][5]))
        self.assertEqual(fixture.run_model()["funding"], before)

    def test_conflicting_funding_fails(self):
        fixture = fixtures.AnalyticsTests()
        fixture.setUp()
        fixture.rates["A"].append({**fixture.rates["A"][5], "rate": 0.9})
        with self.assertRaisesRegex(ValueError, "çelişen"):
            fixture.run_model()

    def test_asynchronous_price_snapshots_not_ranked_as_spread(self):
        at = time.time()
        common = dict(asset="BTC", mark=100, rate=0.0001, fee=0.0005, leverage=5)
        left = dict(common, venue="Lighter", received=at)
        right = dict(common, venue="Extended", received=at - 40)
        wide = SimpleNamespace(
            snapshot=lambda: ([], {}), lock=threading.RLock(), histories={}
        )
        engine = SimpleNamespace(
            wide=wide,
            cfg={"symbols": ["BTC"]},
            feed=SimpleNamespace(funding={}, status={}),
        )
        self.assertEqual(
            expanded_view(engine, {"markets": [left, right], "pairs": []})["pairs"], []
        )
        right["received"] = at - 20
        self.assertEqual(
            len(
                expanded_view(engine, {"markets": [left, right], "pairs": []})["pairs"]
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
