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


class RfqBookSelectionTests(unittest.TestCase):
    """DeskEngine.select_rfq_books on a MOCK engine (no network, no database)."""

    def engine(self, scores, held=()):
        from types import SimpleNamespace
        from desk_analytics import DeskEngine

        meta = {a: dict(rfq=True, id=a + "-USD") for a in scores}
        meta["NORFQ"] = dict(rfq=False, id="NORFQ-USD")
        wp = SimpleNamespace(
            workspace=lambda: {},
            positions=[dict(asset=a, legs=[dict(venue="Extended")]) for a in held],
            candidates=[dict(asset=a, long="Extended", short="Hyperliquid", score=s)
                        for a, s in scores.items()]
            + [dict(asset="OTHER", long="Paradex", short="Hyperliquid", score=99)],
            conv_candidates=[],
            rejected={},
            pair_key=lambda c: (c["asset"], c["long"], c["short"]),
            blocked=lambda c: False,
        )
        e = DeskEngine.__new__(DeskEngine)
        e.feed = SimpleNamespace(meta={"Extended": meta, "Lighter": {}})
        e.wide_paper = wp
        return e

    def test_young_books_survive_re_ranking_until_the_hold_ends(self):
        scores = {f"A{i}": 100 - i for i in range(14)}
        e = self.engine(scores)
        first = set(e.select_rfq_books(at=1000))
        self.assertEqual(first, {f"A{i}" for i in range(12)})
        # Next cycle the two lowest fall behind two newcomers.
        for a in ("A12", "A13"):
            e.wide_paper.candidates[int(a[1:])]["score"] = 1000
        second = set(e.select_rfq_books(at=1070))
        self.assertEqual(second, first)  # no retirement 70 s after start
        later = set(e.select_rfq_books(at=1000 + 601))
        self.assertIn("A12", later)
        self.assertIn("A13", later)
        self.assertEqual(len(later), 12)

    def test_held_legs_kept_and_only_extended_rfq_markets_streamed(self):
        e = self.engine({"A0": 1, "A1": 2}, held=("A0",))
        got = e.select_rfq_books(at=1000)
        self.assertEqual(set(got), {"A0", "A1"})
        self.assertNotIn("NORFQ", got)
        self.assertNotIn("OTHER", got)


if __name__ == "__main__":
    unittest.main()
