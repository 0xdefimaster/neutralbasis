"""Phase 15: legacy vs answer time basis - SHADOW reporting and the prepared switch
(docs/v2/45). Default stays legacy; nothing here changes a live decision. MOCK data."""
import itertools
import unittest
from unittest import mock

from test_v2_phase4 import AT, CFG, FakeProvider, row
from v2 import gates, marketdata, source

T0 = 1_794_484_800.0


def book(received, answer=None, ts=None, with_key=True):
    b = dict(bids=[(99.99, 100.0)], asks=[(100.01, 100.0)], empty=False, crossed=False,
             model="orderbook", received=received, ts=ts)
    if with_key:
        b["answer_received"] = answer
    return b


class TimeBasisTests(unittest.TestCase):
    def test_default_is_legacy(self):
        self.assertEqual(marketdata.TIME_BASIS, marketdata.LEGACY)

    def test_answer_basis_is_never_less_strict(self):
        """Property over a grid: a fetch precedes its read, so every book stale under
        the legacy basis is stale under the answer basis."""
        for read_age, cache_age, venue in itertools.product(
                (0, 3, 9, 14, 16, 29, 31, 59, 61), (0, 1, 4.9, 6, 20), marketdata.MAX_AGE):
            b = book(T0 - read_age, T0 - read_age - cache_age)
            leg = marketdata.stale(b, venue, T0, marketdata.LEGACY)
            ans = marketdata.stale(b, venue, T0, marketdata.ANSWER)
            self.assertTrue(ans or not leg, (read_age, cache_age, venue))

    def test_unknown_fetch_time_is_never_fresh(self):
        b = book(T0, answer=None)                       # key present, value unknown
        self.assertFalse(marketdata.stale(b, "arcus", T0, marketdata.LEGACY))
        self.assertTrue(marketdata.stale(b, "arcus", T0, marketdata.ANSWER))
        self.assertEqual(marketdata.stale_views(b, "arcus", T0)["verdict"],
                         "ANSWER_TIME_UNKNOWN")
        self.assertTrue(marketdata.stale(None, "arcus", T0, marketdata.ANSWER))

    def test_books_without_a_cache_are_identical_under_both(self):
        b = book(T0 - 12, with_key=False)               # WS / stream book: no cache
        for v in ("arcus", "txflow"):
            self.assertEqual(marketdata.stale(b, v, T0, marketdata.LEGACY),
                             marketdata.stale(b, v, T0, marketdata.ANSWER))

    def test_shadow_verdicts(self):
        v = marketdata.stale_views(book(T0 - 1, T0 - 20, ts=T0 - 21), "arcus", T0)
        self.assertEqual(v["verdict"], "LEGACY_FRESH_ANSWER_STALE")
        self.assertAlmostEqual(v["source_age_s"], 21)
        self.assertEqual(marketdata.stale_views(book(T0 - 1, T0 - 2), "arcus", T0)["verdict"],
                         "AGREE_FRESH")
        self.assertEqual(marketdata.stale_views(book(T0 - 40, T0 - 41), "arcus", T0)["verdict"],
                         "AGREE_STALE")


class GateShadowTests(unittest.TestCase):
    def gate(self, arcus_book):
        rows = [row("Arcus"), row("Lighter")]
        books = {("Arcus", "BTC"): arcus_book,
                 ("Lighter", "BTC"): book(AT - 1, AT - 2)}
        return gates.Gate(FakeProvider(rows, books), CFG)

    def test_shadow_reports_without_changing_the_strict_decision(self):
        g = self.gate(book(AT - 1, AT - 20))           # cache hit 19 s older than read
        ev = g.evaluate(dict(asset="BTC", long="Arcus", short="Lighter"), AT, "convergence")
        codes = [r["code"] for r in ev["reasons"]]
        self.assertNotIn("BOOK_STALE", codes)            # legacy decision unchanged
        sh = {x["venue"]: x for x in ev["time_shadow"]}
        self.assertEqual(sh["arcus"]["verdict"], "LEGACY_FRESH_ANSWER_STALE")
        self.assertEqual(sh["lighter_rh"]["verdict"], "AGREE_FRESH")

    def test_prepared_switch_makes_the_gate_honest(self):
        g = self.gate(book(AT - 1, AT - 20))
        with mock.patch.object(marketdata, "TIME_BASIS", marketdata.ANSWER):
            ev = g.evaluate(dict(asset="BTC", long="Arcus", short="Lighter"), AT,
                            "convergence")
        self.assertIn("BOOK_STALE", [r["code"] for r in ev["reasons"]])
        self.assertFalse(ev["eligible"])


class CollectorSwitchTests(unittest.TestCase):
    def collector(self, now):
        from v2.collector import Collector
        col = Collector(transport=lambda *a, **k: {"x": 1}, clock=lambda: now[0],
                        mono=lambda: now[0])
        col.by_key = {("Arcus", "BTC"): dict(exchange_id="arcus", market_id="BTC-USD",
                                             multiplier=1.0)}
        return col

    def test_answer_basis_stamps_received_with_the_fetch_time(self):
        now = [1000.0]
        col = self.collector(now)
        with mock.patch("v2.venues.arcus.parse_book",
                        return_value=dict(bids=[(1.0, 1.0)], asks=[(2.0, 1.0)])):
            col.book("Arcus", "BTC")
            now[0] = 1004.0
            legacy = col.book("Arcus", "BTC")
            with mock.patch.object(marketdata, "TIME_BASIS", marketdata.ANSWER):
                honest = col.book("Arcus", "BTC")
        self.assertEqual(legacy["received"], 1004.0)     # unchanged default
        self.assertEqual(honest["received"], 1000.0)     # the answer's fetch time
        self.assertEqual(honest["answer_received"], 1000.0)

    def test_rows_take_the_venue_fetch_time_under_the_answer_basis(self):
        with mock.patch.object(marketdata, "TIME_BASIS", marketdata.LEGACY):
            self.assertEqual(source.row_time(500.0, 440.0), 500.0)
        with mock.patch.object(marketdata, "TIME_BASIS", marketdata.ANSWER):
            self.assertEqual(source.row_time(500.0, 440.0), 440.0)
            self.assertIsNone(source.row_time(500.0, None))         # fail-closed: no row

    def test_invalidate_rate_drops_only_that_markets_answers(self):
        now = [1000.0]
        col = self.collector(now)
        for k in ("tx:ctx:7", "tx:ctx:8", "xyz:meta", "rh:markets", "rh:fund:3"):
            col.cache[k] = (990.0, {}, 0.0)
        self.assertEqual(col.invalidate_rate("txflow", "7"), ["tx:ctx:7"])
        self.assertIn("tx:ctx:8", col.cache)             # never a venue-wide refetch
        self.assertEqual(sorted(col.invalidate_rate("lighter_rh", "3")),
                         ["rh:fund:3", "rh:markets"])
        self.assertEqual(col.invalidate_rate("variational", "X"), [])

    def test_current_funding_rereads_for_real_under_the_answer_basis(self):
        from v2.engine_source import V2EngineSource
        col = mock.Mock()
        col.row.return_value = dict(exchange_id="txflow", market_id="7", raw_rate=1e-5,
                                    rate=1e-5, interval=3600, next=None, received=10.0,
                                    answer_received=5.0)
        col.rows = [dict(venue="TxFlow", asset="NATGAS")]
        col.reasons = {}
        src = V2EngineSource.__new__(V2EngineSource)
        src.collector, src.clock, src.last, src.refresh_every = col, lambda: 20.0, 19.0, 60.0
        src.excluded = {}
        src.current_funding("TxFlow", "NATGAS")             # legacy: no forced re-read
        col.invalidate_rate.assert_not_called()
        col.refresh.assert_not_called()
        with mock.patch.object(marketdata, "TIME_BASIS", marketdata.ANSWER):
            out = src.current_funding("TxFlow", "NATGAS")
        col.invalidate_rate.assert_called_once_with("txflow", "7")
        col.refresh.assert_called_once()
        self.assertEqual(out["funding_answer_received"], 5.0)


class FundingClassTests(unittest.TestCase):
    """Phase 15 §3: classes are reported, never an entry gate (MOCK nets)."""

    def test_classes_are_cumulative_and_unknown_is_insufficient(self):
        from v2 import funding_expectation as fx
        legs_pub = {"long": dict(fit_status="NO_HISTORY"), "short": dict(fit_status="OK")}
        legs_set = {"long": dict(fit_status="OK"), "short": dict(fit_status="OK")}
        c = fx.classify(True, dict(baseline=5.0, persistence=None, adverse=None), legs_pub)
        self.assertEqual(c["classes"], ["DISCOVERED", "BASELINE_POSITIVE",
                                        "INSUFFICIENT_HISTORY"])
        self.assertEqual(c["profit_label"], "ESTIMATED_SETTLEMENT_UNVERIFIED")
        c = fx.classify(True, dict(baseline=5.0, persistence=2.0, adverse=0.5), legs_set,
                        "NOT_READY")
        self.assertEqual(c["highest"], "ADVERSE_POSITIVE")
        self.assertEqual(c["profit_label"], "ESTIMATED")               # never VERIFIED
        self.assertEqual(c["paper_entry_readiness"], "NOT_READY")      # passed through
        c = fx.classify(False, dict(baseline=-1.0, persistence=-2.0, adverse=-3.0), legs_set)
        self.assertEqual((c["classes"], c["highest"]), ([], None))
        for label in ("VERIFIED", "CONFIRMED"):
            self.assertNotIn(label, json_dump(c))

    def test_estimated_decisions_do_not_use_the_classes(self):
        """The ESTIMATED evaluator's decision code never reads a class (reporting
        only): its source has no reference to classify / funding_class."""
        import inspect
        from v2 import estimated, gates as g
        for mod in (estimated, g):
            src = inspect.getsource(mod)
            for needle in ("fx.classify(", "funding_expectation.classify(", "funding_class"):
                self.assertFalse(needle in src, "%s uses %s" % (mod.__name__, needle))


def json_dump(x):
    import json
    return json.dumps(x)


if __name__ == "__main__":
    unittest.main()
