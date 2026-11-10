"""Variational read-only research adapter on a REAL recorded /metadata/v2/stats response
(testdata, trimmed, unmodified). The broken listing is MOCK."""
import copy
import json
import os
import unittest

import variational_public as vp

HERE = os.path.dirname(os.path.abspath(__file__))


def stats():
    with open(os.path.join(HERE, "testdata", "variational_stats_v2_20261110.json"), encoding="utf8") as f:
        return json.load(f)["response"]


class VariationalResearchTests(unittest.TestCase):
    def setUp(self):
        self.obj = stats()
        self.at = 1794276000.0
        self.rows, self.skipped = vp.parse(self.obj, self.at)

    def test_reference_listings_are_discovered(self):
        for a in ("DRAM", "SOL", "BZ", "VVV", "XRP", "CL", "SOXL", "MSTR", "AERO"):
            self.assertIn(a, self.rows, a)
        self.assertEqual(self.skipped, {})

    def test_documented_funding_units_and_sign(self):
        cl = self.rows["CL"]
        self.assertEqual((cl["interval"], cl["funding_semantics"]), (14400, "CURRENT_INDICATIVE"))
        self.assertAlmostEqual(cl["rate_per_interval"], -0.00006889)
        self.assertAlmostEqual(cl["rate_hourly"], -0.00006889 / 4)
        self.assertEqual(cl["underlying_documented"], "WTI Crude Oil futures (two-contract roll)")
        self.assertIsNone(self.rows["SOXL"]["underlying_documented"])  # default rule only

    def test_execution_is_research_only_and_history_unknown(self):
        r = self.rows["DRAM"]
        self.assertEqual(r["paper_execution"], "RESEARCH_ONLY")
        self.assertTrue(r["funding_history"].startswith("UNKNOWN"))
        self.assertEqual(r["fee_taker"], 0.0)

    def test_indicative_cost_uses_documented_quote_sizes(self):
        x = self.rows["XRP"]
        bid, ask = x["quotes"]["size_1k"]
        self.assertAlmostEqual(vp.indicative_cost(x, "buy", 1000), ask / x["mark"] - 1)
        bid, ask = x["quotes"]["size_100k"]
        self.assertAlmostEqual(vp.indicative_cost(x, "sell", 5000), 1 - bid / x["mark"])
        self.assertIsNone(vp.indicative_cost(dict(x, quotes={}), "buy", 1000))

    def test_quote_time_with_nanoseconds_parses(self):
        self.assertIsNotNone(self.rows["BTC"]["quoted_at"])
        self.assertIsNone(vp.iso("not a time"))

    def test_broken_listing_does_not_drop_the_venue(self):
        o = copy.deepcopy(self.obj)
        o["listings"].append(dict(ticker="BAD-PERP", contract_type="perpetual", funding=7))  # MOCK
        rows, skipped = vp.parse(o, self.at)
        self.assertIn("BAD-PERP", skipped)
        self.assertIn("DRAM", rows)


if __name__ == "__main__":
    unittest.main()
