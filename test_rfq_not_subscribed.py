"""F1 (2026-11-10): an RFQ pair whose market has no stream in the engine is set aside
before the deep check, without using a deep-check slot and without a rejection wait.
Before, its first book read failed with "RFQ akışı bu piyasa için başlatılmadı", the
slot was consumed and the pair waited 60 s, so pairs further down the list were not
checked. All fixtures below are MOCK (test_wide_paper helpers); nothing is evidence."""
import types
import unittest

import test_wide_paper as twp
import zero_trade
from rfq_trace import RfqTrace
from test_rfq_trace import FakeBook

T0 = twp.T0


class NotSubscribedTests(unittest.TestCase):
    setUp = twp.WidePaperTests.setUp
    tearDown = twp.WidePaperTests.tearDown
    fund_history = twp.WidePaperTests.fund_history

    def scenario(self, streams, extended_error="RFQ akışı bu piyasa için başlatılmadı"):
        twp.RfqSplitTests.setup_btw(self)
        # MOCK: the Extended leg has its history, so a deep check would reach its book.
        self.src.history[("Extended", "BTC")] = twp.candles(T0)
        self.fund_history("Extended", -0.0005, 3600)
        reads = []

        def books(v, a, _real=FakeBook(self.src)):
            reads.append(v)
            if v == "Extended":
                raise ValueError(extended_error)
            return _real(v, a)

        self.src.book = books
        # MOCK engine stream registry (ExtendedRfq.streams: asset -> RfqStream).
        if streams is not None:
            self.src.engine = types.SimpleNamespace(rfq=types.SimpleNamespace(streams=streams))
        self.desk.cfg["wide_deep_top"] = 1
        return reads

    def funding(self):
        return [(x["long"], x["short"]) for x in self.desk.assessed
                if x["strategy"] == "funding"]

    def test_unsubscribed_rfq_pair_does_not_use_the_slot(self):
        reads = self.scenario(streams={})
        t = RfqTrace()
        self.desk.rfq_trace = t
        t.begin_cycle(self.clock)
        self.desk.cycle()
        t.end_cycle({})
        # The single slot went to the next pair instead of the unreadable RFQ book.
        assessed = self.funding()
        self.assertTrue(assessed)
        self.assertTrue(all("Extended" not in pair for pair in assessed))
        self.assertNotIn("Extended", reads)
        # No rejection wait and no leg set aside: next round it is ranked as before.
        self.assertFalse(any("Extended" in k for k in self.desk.rejected))
        self.assertFalse(any(v == "Extended" for v, _ in self.desk.legs_down))
        rows = [r for r in t.cycles[0]["deep_checks"] if r[0] == "funding"]
        skip = [r for r in rows if r[5] == "skip_not_subscribed"]
        self.assertTrue(skip)
        self.assertTrue(all(not r[6] for r in skip))  # no slot consumed
        self.assertEqual(skip[0][8], ("RFQ_STREAM_NOT_SUBSCRIBED",))

    def test_reason_keeps_its_code(self):
        self.scenario(streams={})
        why = self.desk.rfq_not_subscribed(dict(asset="BTC", rfq_long=True))
        self.assertEqual(zero_trade.code_of(why), ("RFQ_STREAM_NOT_SUBSCRIBED", "DATA"))
        self.assertIsNone(self.desk.rfq_not_subscribed(dict(asset="BTC")))

    def test_started_but_failing_stream_is_still_checked_and_rejected(self):
        # A stream exists (e.g. disconnected): unchanged — it is assessed, the slot is
        # used, and the pair waits like any RFQ failure.
        self.scenario(streams={"BTC": object()},
                      extended_error="RFQ akışı bağlı değil; giriş yok (koptu)")
        self.desk.cycle()
        self.assertEqual(self.funding(), [("Extended", "Binance")])
        x = self.desk.assessed[0]
        self.assertFalse(x["eligible"])
        self.assertTrue(any("Extended" in k for k in self.desk.rejected))

    def test_without_a_stream_registry_behaviour_is_unchanged(self):
        # Sources without an engine (tests, offline replays): no pre-check.
        self.scenario(streams=None)
        self.desk.cycle()
        self.assertEqual(self.funding(), [("Extended", "Binance")])

    def test_pairs_without_an_rfq_leg_are_never_affected(self):
        self.scenario(streams={})
        for c in self.desk.candidates + self.desk.conv_candidates:
            if not (c.get("rfq_long") or c.get("rfq_short")):
                self.assertIsNone(self.desk.rfq_not_subscribed(c))

    def test_decisions_identical_with_and_without_trace(self):
        def run(trace):
            self.scenario(streams={})
            if trace is not None:
                self.desk.rfq_trace = trace
            out = []
            for _ in range(2):
                if trace is not None:
                    trace.begin_cycle(self.clock)
                self.desk.cycle()
                if trace is not None:
                    trace.end_cycle({})
                out.append((self.funding(), sorted(self.desk.rejected),
                            sorted(self.desk.legs_down)))
                self.clock += 30
            return out

        plain = run(None)
        self.tearDown()
        self.setUp()
        self.assertEqual(plain, run(RfqTrace()))


if __name__ == "__main__":
    unittest.main()
