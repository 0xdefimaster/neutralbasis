"""RWA identity gate (2026-11-10): Lighter publishes no asset class, so a ticker some
venue lists as a real-world asset (Extended category RWA, RISEx stocks/ETF/commodity)
is never treated as the same instrument elsewhere: every pair on it stays research.
MOCK fixtures (test_wide_paper)."""
import unittest
from types import SimpleNamespace

import test_wide_paper as twp
import zero_trade


class IdentityGateTests(unittest.TestCase):
    setUp = twp.WidePaperTests.setUp
    tearDown = twp.WidePaperTests.tearDown
    fund_history = twp.WidePaperTests.fund_history
    setup_pair = twp.WidePaperTests.setup_pair

    def test_pair_on_an_rwa_ticker_is_research_and_never_opens(self):
        self.setup_pair()
        self.src.rows[0]["category"] = "RWA"  # one venue lists BTC as a real-world asset
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        c = self.desk.candidates[0]
        self.assertTrue(any("Kimlik doğrulanmadı" in r for r in c["research"]))
        self.assertEqual(zero_trade.code_of(c["research"][-1])[0], "IDENTITY_UNVERIFIED")

    def test_rwa_tickers_from_the_feed_and_suffixes(self):
        self.src.engine = SimpleNamespace(feed=SimpleNamespace(rwa_tickers={"MSTR"}), wide=None)
        self.desk.rows[("Extended", "SOXL_24_5")] = dict(category="RWA")
        rwa = self.desk.rwa_assets()
        self.assertTrue({"MSTR", "SOXL_24_5", "SOXL"} <= rwa)

    def test_crypto_pair_unaffected(self):
        self.setup_pair()
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1)


if __name__ == "__main__":
    unittest.main()
