import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bot
import test_bot as fixtures


class FundingRecoveryTests(unittest.TestCase):
    setUp = fixtures.PaperTests.setUp
    tearDown = fixtures.PaperTests.tearDown
    candidate = fixtures.PaperTests.candidate

    def payment_gap(self):
        self.e.open(self.candidate())
        position = self.e.positions[0]
        payment = bot.hour(self.clock) + 3600
        self.clock = payment + 10
        self.feed.funding[("Lighter", "BTC")] = [
            dict(ts=payment, rate=0, value=0, audit_id=11)
        ]
        self.feed.funding[("Extended", "BTC")] = [
            dict(ts=payment, rate=0.0005, value=None, audit_id=12)
        ]
        return position, payment

    def test_missing_mark_recovers_using_audited_closed_candle(self):
        position, payment = self.payment_gap()
        self.feed.funding_reference = Mock(
            return_value=dict(
                price=103,
                ts=payment,
                method="historical_trade_close_proxy",
                audit_id=21,
            )
        )
        before = sum(self.e.wallet.values())
        self.e.settle(position)
        self.assertFalse(position["pending_funding"])
        expected = position["legs"][1]["qty"] * 103 * 0.0005
        self.assertAlmostEqual(position["funding"], expected)
        self.assertAlmostEqual(sum(self.e.wallet.values()) - before, expected)
        payment_record = next(
            x["body"]
            for x in self.e.store.recent("payments")
            if x["body"]["venue"] == "Extended"
        )
        self.assertEqual(payment_record["valuation"]["audit_id"], 21)
        self.assertTrue(payment_record["valuation_estimated"])
        self.e.settle(position)
        self.assertAlmostEqual(position["funding"], expected)

    def test_closed_trade_does_not_permanently_lock_after_history_outage(self):
        position, payment = self.payment_gap()
        self.e.settle(position)
        self.assertTrue(position["pending_funding"])
        for leg in position["legs"]:
            leg.update(closed=True, exited=payment + 20)
        position.update(status="CLOSED", closed_at=payment + 20, net=0)
        self.e.positions.remove(position)
        self.e.closed.append(position)
        self.clock = payment + 3601
        self.feed.funding_reference = Mock(side_effect=ValueError("API unavailable"))
        self.e.settle(position)
        self.assertFalse(
            any(p.get("pending_funding") for p in self.e.positions + self.e.closed)
        )
        self.assertNotEqual(position["funding"], 0)
        records = self.e.store.recent("payments")
        extended = next(r["body"] for r in records if r["body"]["venue"] == "Extended")
        self.assertEqual(extended["valuation"]["method"], "entry_fill_fallback")
        self.assertEqual(extended["valuation"]["price"], position["legs"][1]["entry"])
        self.assertEqual(extended["valuation"]["warning"], "API unavailable")
        self.e.persist()
        restarted = bot.Engine(Path(self.tmp.name) / "paper.db", fixtures.FixtureFeed())
        try:
            before = dict(restarted.wallet)
            restarted.settle(restarted.closed[0])
            self.assertEqual(restarted.wallet, before)
            self.assertFalse(restarted.closed[0]["pending_funding"])
        finally:
            restarted.store.db.close()

    def test_future_price_never_used_before_fallback_delay(self):
        position, payment = self.payment_gap()
        self.feed.funding_reference = Mock(
            return_value=dict(price=999, ts=payment + 1, method="future", audit_id=21)
        )
        self.e.settle(position)
        self.assertTrue(position["pending_funding"])
        self.assertEqual(position["estimated_funding"], 0)

    def test_missing_published_rate_is_not_fabricated(self):
        position, payment = self.payment_gap()
        for leg in position["legs"]:
            leg.update(closed=True, exited=payment + 20)
        self.feed.funding[("Extended", "BTC")] = []
        self.clock = payment + 7200
        self.e.settle(position)
        self.assertTrue(position["pending_funding"])
        self.assertEqual(position["estimated_funding"], 0)

    def test_history_request_selects_exact_closed_candle_and_preserves_audit(self):
        feed = bot.Feed(self.e.store)
        payment = bot.hour(self.clock)
        with patch.object(
            feed,
            "get",
            return_value=(
                dict(
                    data=[
                        dict(T=(payment - 3600) * 1000, c="101"),
                        dict(T=payment * 1000, c="999"),
                    ]
                ),
                dict(audit_id=81),
            ),
        ) as request:
            result = feed.funding_reference("BTC", payment)
            self.assertEqual(result["price"], 101)
            self.assertEqual(result["ts"], payment)
            self.assertEqual(result["audit_id"], 81)
            feed.funding_reference("BTC", payment)
            self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
