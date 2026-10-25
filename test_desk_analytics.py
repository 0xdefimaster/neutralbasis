import math, unittest
from desk_analytics import hold_backtest, history_summary


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.end = 720 * 3600
        self.times = list(range(self.end - 25 * 3600, self.end, 3600))
        self.prices = {
            v: dict(
                price_type="trade",
                prices={
                    t: 100 + (0.1 if v == "B" else 0.2) * i
                    for i, t in enumerate(self.times)
                },
            )
            for v in ("A", "B")
        }
        self.rates = {
            v: [
                dict(ts=t + 3600, rate=0.0001, value=0.01, audit_id=1)
                for t in self.times
            ]
            for v in ("A", "B")
        }

    def run_model(self, **kw):
        args = dict(
            prices=self.prices,
            funding=self.rates,
            fees={"A": 0.001, "B": 0.001},
            asset="BTC",
            lv="A",
            sv="B",
            size=1000,
            hours=24,
            slippage_bps=2,
            include_price=True,
            at=self.end,
        )
        args.update(kw)
        return hold_backtest(**args)

    def test_reconciliation_and_equal_base(self):
        r = self.run_model()
        self.assertEqual(r["qty"], 10)
        self.assertAlmostEqual(r["funding"], 0)
        self.assertAlmostEqual(r["price"], 24)
        self.assertAlmostEqual(r["net"], r["funding"] + r["price"] - r["costs"])
        self.assertEqual(r["payment_counts"], {"A": 24, "B": 24})
        self.assertEqual(r["end"] - r["start"], 24 * 3600)

    def test_missing_funding_never_zero(self):
        self.rates["B"].pop(8)
        with self.assertRaisesRegex(ValueError, "funding geçmişinde"):
            self.run_model()

    def test_missing_price_never_forward_filled(self):
        del self.prices["A"]["prices"][self.times[8]]
        with self.assertRaisesRegex(ValueError, "kesintisiz"):
            self.run_model()

    def test_oracle_rejected(self):
        self.prices["B"]["price_type"] = "oracle"
        with self.assertRaisesRegex(ValueError, "oracle"):
            self.run_model()

    def test_price_toggle_and_slippage(self):
        a = self.run_model(include_price=False, slippage_bps=0)
        b = self.run_model(include_price=False, slippage_bps=2)
        self.assertEqual(a["price"], 0)
        self.assertGreater(b["costs"], a["costs"])
        self.assertLess(b["net"], a["net"])

    def test_estimated_valuation_uses_prior_candle(self):
        for r in self.rates["A"]:
            r["value"] = None
        r = self.run_model()
        self.assertEqual(r["estimated_payments"], 24)
        self.assertTrue(all(p["estimated"] for p in r["payments"] if p["venue"] == "A"))

    def test_nonzero_historical_mean_not_a_crossing(self):
        ts = list(range(self.end - 168 * 3600, self.end, 3600))
        a = dict(price_type="trade", prices={t: 100 for t in ts})
        b = dict(
            price_type="trade",
            prices={t: 101 + 0.001 * math.sin(i) for i, t in enumerate(ts)},
        )
        r = history_summary(a, b, self.end)
        self.assertEqual(r["points"][0]["long_price"], 100)
        self.assertEqual(r["points"][0]["short_price"], b["prices"][ts[0]])
        self.assertEqual(r["crossings"], 0)
        self.assertGreater(r["mean"], 0)


if __name__ == "__main__":
    unittest.main()
