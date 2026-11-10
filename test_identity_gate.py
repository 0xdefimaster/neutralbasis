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

    # Owner decision 2026-11-10 (PAPER): price-verified RWA pairs, off by default.
    def enable(self, gap_bps=50.0):
        self.desk.update_config({"wide_rwa_verified": True, "wide_rwa_max_gap_bps": gap_bps})

    def test_price_verified_rwa_pair_leaves_research_and_opens(self):
        self.setup_pair()
        self.src.rows[0]["category"] = "RWA"
        self.enable()
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1)
        self.assertFalse(self.desk.candidates[0].get("research"))

    def test_rwa_mark_gap_too_wide_stays_research(self):
        self.setup_pair()
        self.src.rows[0]["category"] = "RWA"
        self.src.rows[1]["mark"] = 101.0  # ~99.5 bp from the other leg
        self.enable()
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        c = self.desk.candidates[0]
        self.assertTrue(any("anlık fiyat farkı" in r for r in c["research"]))
        self.assertEqual(zero_trade.code_of(c["research"][-1])[0], "IDENTITY_UNVERIFIED")

    def test_rwa_history_gap_too_wide_is_rejected_in_the_deep_check(self):
        self.setup_pair()
        self.src.rows[0]["category"] = "RWA"
        h = self.src.history[("Binance", "BTC")]
        # Same marks now, but the last 7 days of closes sat ~99.5 bp apart.
        self.src.history[("Binance", "BTC")] = dict(
            h, prices={t: p * 1.01 for t, p in h["prices"].items()})
        self.enable()
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        reasons = [r for a in self.desk.assessed for r in a.get("reasons") or []]
        self.assertTrue(any("RWA fiyat uyumu yok" in r for r in reasons), reasons)

    def test_rwa_check_off_by_default(self):
        self.assertFalse(self.desk.cfg["wide_rwa_verified"])
        self.setup_pair()
        self.src.rows[0]["category"] = "RWA"
        self.desk.cycle()
        c = self.desk.candidates[0]
        self.assertTrue(any("doğrulaması kapalı" in r for r in c["research"]))


if __name__ == "__main__":
    unittest.main()
