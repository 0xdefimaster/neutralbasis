"""RISEx / Arcus listings: the whole listed crypto universe, one market never drops the
venue (2026-11-10). Before, both feeds kept only the legacy desk's 15 configured
symbols, so e.g. RISEx AERO (active, unlocked, crypto) was silently dropped.

testdata/risex_markets_20261110.json is a REAL recorded public response (trimmed, records
unmodified). The broken market and the Arcus listing below are MOCK."""
import copy
import json
import os
import unittest
from types import SimpleNamespace

import risk_scan

HERE = os.path.dirname(os.path.abspath(__file__))
INFO = dict(received=1794268000.0, audit_id=None, http_age=0)


class FakeFeed:
    """generic() is the real FiveFeed.generic; only `symbols` is the legacy 15-list."""
    symbols = ["BTC", "ETH", "SOL", "HYPE", "SUI", "XRP", "LINK", "DOGE", "AVAX", "BNB",
               "LTC", "AAVE", "UNI", "NEAR", "ADA"]
    generic = risk_scan.FiveFeed.generic


def risex():
    with open(os.path.join(HERE, "testdata", "risex_markets_20261110.json"), encoding="utf8") as f:
        return json.load(f)["response"]


class RisexUniverseTests(unittest.TestCase):
    def test_listed_crypto_outside_the_legacy_symbols_is_kept(self):
        out, skipped = risk_scan.rise_rows(FakeFeed(), risex(), INFO)
        self.assertEqual(skipped, {})
        self.assertTrue({"BTC", "SOL", "AERO", "XMR"} <= set(out))
        aero = out["AERO"]
        self.assertEqual((aero["venue"], aero["interval"]), ("RISEx", 3600))
        self.assertAlmostEqual(aero["raw_rate"], 0.000047220822708962, places=12)

    def test_category_and_status_filters_still_apply(self):
        feed = FakeFeed()
        out, _ = risk_scan.rise_rows(feed, risex(), INFO)
        self.assertNotIn("SNDK", out)  # stocks: a different instrument class
        self.assertIn("SNDK", feed.rwa_tickers)  # remembered for the identity gate
        self.assertFalse(any("deprecated" in a for a in out))  # inactive market

    def test_one_broken_market_does_not_drop_the_venue(self):
        o = risex()
        bad = copy.deepcopy(o["data"]["markets"][0])
        bad["market_id"], bad["mark_price"] = "999", "not-a-number"  # MOCK broken row
        o["data"]["markets"].append(bad)
        out, skipped = risk_scan.rise_rows(FakeFeed(), o, INFO)
        self.assertIn("999", skipped)
        self.assertIn("AERO", out)
        bad2 = dict(copy.deepcopy(bad), market_id="998", mark_price="1",
                    next_funding_time="soon")  # MOCK unparsable timestamp
        o["data"]["markets"].append(bad2)
        out, skipped = risk_scan.rise_rows(FakeFeed(), o, INFO)
        self.assertIn("998", skipped)
        self.assertIn("AERO", out)


class ArcusUniverseTests(unittest.TestCase):
    def market(self, asset, status="ONLINE", mark="1.0"):  # MOCK Arcus listing row
        return dict(baseAsset=asset, status=status, marketDisplayName=asset + "-PERP",
                    markPrice=mark, oraclePrice="1.0", fundingRate="0.00001",
                    nextFundingAt="1794272400000", stepSize="1", minOrderSize="1",
                    initialMarginFraction="0.1")

    def test_listed_market_outside_the_legacy_symbols_is_kept(self):
        o = dict(markets=[self.market("BTC"), self.market("AERO"),
                          self.market("OLD", status="DELISTED"), self.market("BAD", mark="x")])
        out, skipped = risk_scan.arcus_rows(FakeFeed(), o, INFO)
        self.assertEqual(set(out), {"BTC", "AERO"})
        self.assertEqual(list(skipped), ["BAD-PERP"])


if __name__ == "__main__":
    unittest.main()
