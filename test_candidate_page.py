"""Whole-candidate visibility (2026-11-10): the state snapshot shows the first 40 ranked
pairs only; candidate_page pages through all of them, read-only. MOCK fixtures."""
import unittest

import test_wide_paper as twp
from test_wide_paper import T0


class CandidatePageTests(unittest.TestCase):
    setUp = twp.WidePaperTests.setUp
    tearDown = twp.WidePaperTests.tearDown
    fund_history = twp.WidePaperTests.fund_history
    setup_pair = twp.WidePaperTests.setup_pair

    def synthetic(self, n=95):
        self.desk.candidates = [
            dict(asset="A%03d" % i, long="Hyperliquid", short="Extended", rank=i + 1,
                 score=0.001 - i * 1e-5, research=["Extended: Funding işareti doğrulanmadı"] if i % 2 else [])
            for i in range(n)
        ]

    def test_pages_reach_beyond_the_first_40(self):
        self.synthetic()
        page = self.desk.candidate_page("funding", offset=80, limit=50)
        self.assertEqual((page["total"], page["matched"], len(page["rows"])), (95, 95, 15))
        self.assertEqual(page["rows"][0]["rank"], 81)
        self.assertEqual(self.desk.snapshot()["candidates"][-1]["rank"], 40)  # UI unchanged

    def test_filters_and_classes(self):
        self.synthetic()
        page = self.desk.candidate_page("funding", asset="A051")
        (row,) = page["rows"]
        self.assertEqual(row["cls"], "RESEARCH_CANDIDATE")
        self.assertEqual(row["research"], ["Extended: Funding işareti doğrulanmadı"])
        (row,) = self.desk.candidate_page("funding", asset="A050")["rows"]
        self.assertEqual(row["cls"], "PENDING_DEEP_CHECK")
        self.assertEqual(self.desk.candidate_page("funding", venue="Paradex")["matched"], 0)

    def test_limit_is_bounded(self):
        self.synthetic(300)
        self.assertEqual(len(self.desk.candidate_page("funding", limit=10_000)["rows"]), 200)

    def test_decision_codes_after_a_real_cycle(self):
        self.setup_pair()
        self.desk.update_config({"wide_min_net": 1000})  # every pair is rejected
        self.desk.cycle()
        page = self.desk.candidate_page("funding", asset="BTC")
        row = page["rows"][0]
        self.assertEqual(row["cls"], "REJECTED")
        self.assertEqual(row["first_code"], "NET_EXPECTED_PNL_NEGATIVE")
        self.assertIsNotNone(row["waiting_until"])

    def test_an_unranked_pair_says_why(self):
        self.setup_pair()
        self.desk.cycle()
        page = self.desk.candidate_page("funding", asset="BTC", long="Hyperliquid", short="Paradex")
        self.assertEqual(page["matched"], 0)
        self.assertIn("Paradex: piyasa verisi yok", page["not_ranked"]["why"])

    def test_opened_pair_is_marked(self):
        self.setup_pair()
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1)
        (row,) = self.desk.candidate_page("funding", asset="BTC")["rows"][:1]
        self.assertEqual(row["cls"], "OPENED")


if __name__ == "__main__":
    unittest.main()
