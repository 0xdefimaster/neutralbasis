"""Phase 15: funding semantics (P0-A), funding expectation models (P0-B), sign / unit
checks (P0-C) and the multi-venue data contract (P1).

Every rate below is MOCK data unless a test says OBSERVED; OBSERVED-data tests skip when
the saved artifacts are absent.
"""
import unittest

from v2 import funding_evidence as fe
from v2 import funding_semantics as fs
from v2 import paths
from v2.control import capability

HAVE_STREAM = any(paths.bench().glob("v2-phase8-rescue-*/stream-*/stream.jsonl"))
HAVE_SNAPSHOT = paths.bench("v2-phase4-2026-11-12", "answers.json.gz").exists()

T0 = 1_794_484_800.0  # 2026-11-12 12:00:00 UTC, a funding boundary (MOCK clock)


class FundingSemanticsTests(unittest.TestCase):
    def test_registry_covers_every_venue_and_never_invents(self):
        for xid in capability.VENUES:
            self.assertIn(xid, fs.VENUES, xid)
            v = fs.VENUES[xid]
            self.assertIn(v["published"]["rate_kind"], fs.RATE_KINDS)
        for xid in ("popdex_perps", "veranta", "phoenix_perps", "qfex"):
            v = fs.VENUES[xid]
            self.assertEqual(v["published"]["rate_kind"], fs.UNKNOWN)
            self.assertIsNone(v["positive_pays"])
            self.assertIsNone(v["interval_s"])

    def test_payment_relation_known_only_where_observed(self):
        known = {x for x in fs.VENUES if fs.payment_relation_known(x)}
        self.assertEqual(known, {"tradexyz", "hyperliquid_native"})
        self.assertFalse(fs.payment_relation_known("entropy_io"))   # inferred only
        self.assertFalse(fs.payment_relation_known("txflow"))
        self.assertFalse(fs.payment_relation_known("variational"))

    def test_txflow_percent_is_converted_once_and_is_a_projection(self):
        # MOCK: nodeCtx.funding 0.0042 (percent per hour), next payment in 10 min
        f = dict(raw=0.0042, unit="percent", interval_s=3600.0, next_ts=T0 + 3600,
                 kind="observed_published")
        o = fs.observation("txflow", "7", f, observed_at=T0 + 3000, evidence="MOCK")
        self.assertAlmostEqual(o["rate_per_hour"], 4.2e-5)
        self.assertEqual(o["settlement_status"], fs.PROJECTED)
        self.assertEqual(o["valid_for"], "CURRENT_PERIOD_ONLY")
        self.assertEqual(o["rate_kind"], fs.RUNNING_PAYMENT_UNKNOWN)
        self.assertIn("PAYMENT_RELATION_UNKNOWN", o["warnings"])
        self.assertEqual(o["evidence"], "MOCK")
        self.assertEqual(o["next_payment_status"], "FUTURE")

    def test_hourly_matches_the_old_hard_coded_txflow_formula(self):
        for raw, iv in ((0.0042, 3600.0), (-0.0016, None), (0.0955, 3600.0)):
            f = dict(raw=raw, unit="percent", interval_s=iv)
            old = fe.per_hour(raw / 100.0, iv or 3600)
            self.assertAlmostEqual(fs.hourly(f, "txflow"), old, places=15)

    def test_interval_from_doc_is_labelled(self):
        o = fs.observation("txflow", "7", dict(raw=0.01, unit="percent", interval_s=None,
                                               next_ts=None), observed_at=T0)
        self.assertEqual(o["interval_source"], "VENUE_DOC")
        self.assertIn("INTERVAL_FROM_DOC", o["warnings"])
        self.assertIn("NEXT_PAYMENT_UNKNOWN", o["warnings"])

    def test_per_interval_rate_is_divided_by_its_own_interval(self):
        # MOCK Variational NATGAS: 0.00288 per 4 h period
        o = fs.observation("variational", "NATGAS-PERP",
                           dict(raw=0.00288, unit="fraction", interval_s=14400.0,
                                next_ts=T0 + 14400), observed_at=T0 + 60)
        self.assertAlmostEqual(o["rate_per_hour"], 0.00072)
        self.assertAlmostEqual(o["rate_per_period"], 0.00288)

    def test_variational_interval_is_never_defaulted(self):
        o = fs.observation("variational", "X", dict(raw=0.001, unit="fraction",
                                                    interval_s=None), observed_at=T0)
        self.assertIsNone(o["rate_per_hour"])
        self.assertIn("INTERVAL_UNKNOWN", o["warnings"])

    def test_next_payment_in_the_past_is_not_used(self):
        # Variational kept next_timestamp at the boundary for up to 285 s (OBSERVED)
        o = fs.observation("variational", "INIT-PERP",
                           dict(raw=-0.0001, unit="fraction", interval_s=3600.0,
                                next_ts=T0), observed_at=T0 + 7)
        self.assertEqual(o["next_payment_status"], "PAST_AT_READ")
        self.assertIsNone(o["next_payment_ts"])
        self.assertEqual(o["next_payment_raw"], T0)
        self.assertIn("NEXT_PAYMENT_IN_PAST", o["warnings"])

    def test_unknown_unit_is_refused_not_guessed(self):
        o = fs.observation("arcus", "BTC", dict(raw=5.0, unit="bips?", interval_s=3600),
                           observed_at=T0)
        self.assertIsNone(o["rate_per_hour"])
        self.assertIn("UNIT_UNKNOWN", o["warnings"])
        o = fs.observation("arcus", "BTC", None, observed_at=T0)
        self.assertIsNone(o["rate_per_hour"])
        self.assertIn("RATE_OR_UNIT_MISSING", o["warnings"])

    def test_lighter_projection_is_labelled_last_settlement(self):
        f = dict(raw=1e-5, unit="fraction", interval_s=3600, next_ts=T0 + 3600,
                 kind="last_settled_projection")
        o = fs.observation("lighter_rh", "1", f, observed_at=T0 + 5)
        self.assertEqual(o["rate_kind"], fs.LAST_SETTLED)
        self.assertEqual(o["settlement_status"], fs.PROJECTED)   # still not a payment
        self.assertIn("LAST_SETTLEMENT_REUSED", o["warnings"])

    def test_settlement_refuses_projections(self):
        self.assertIsNone(fs.settlement("txflow", "7", dict(ts=T0, rate=1e-5,
                                                            kind="observed_published")))
        self.assertIsNone(fs.settlement("lighter_rh", "1", dict(
            ts=T0, rate=1e-5, kind="last_settled_projection")))
        s = fs.settlement("risex", "1", dict(ts=T0, rate=8e-5, kind="settled_public",
                                             interval_s=7200.0))
        self.assertEqual(s["settlement_status"], fs.SETTLED_PUBLIC)
        self.assertEqual(s["valid_for"], "PAST_PERIOD")
        self.assertAlmostEqual(s["rate_per_hour"], 4e-5)
        u = fs.settlement("txflow", "7", dict(ts=T0, rate=None, kind="account_payment"))
        self.assertEqual(u["settlement_status"], fs.ACCOUNT_PAYMENT)
        self.assertIsNone(u["rate_per_hour"])
        self.assertIn("RATE_NOT_PRICED", u["warnings"])

    def test_observation_is_never_a_settlement(self):
        for xid in fs.VENUES:
            o = fs.observation(xid, "m", dict(raw=1e-5, unit="fraction", interval_s=3600,
                                              next_ts=T0 + 10), observed_at=T0)
            self.assertEqual(o["settlement_status"], fs.PROJECTED, xid)
            self.assertEqual(o["valid_for"], "CURRENT_PERIOD_ONLY", xid)

    def test_matrix_rows(self):
        rows = {r["exchange_id"]: r for r in fs.matrix()}
        self.assertEqual(rows["txflow"]["unit"], "percent")
        self.assertIsNone(rows["txflow"]["settled_history"])
        self.assertEqual(rows["variational"]["period_basis"], "per_interval")
        self.assertTrue(rows["tradexyz"]["payment_relation_known"])


class LedgerSettledUnitTests(unittest.TestCase):
    def test_settled_rate_per_period_is_booked_once(self):
        """G5: a 2 h settled point (MOCK) books rate x units x price exactly once."""
        from v2 import paper_engine as pe

        booked = []

        class Ledger:
            def emit(self, kind, **kw):
                booked.append(kw)

        eng = pe.PaperEngine.__new__(pe.PaperEngine)
        eng.ledger = Ledger()
        p = dict(id="p1", asset="X", funding_keys=set())
        leg = dict(interval=7200.0, venue="V", qty=2.0)
        prev = dict(marks=dict(short=100.0), rates=dict(short=None), at=T0 - 100)
        eng.book_payment(p, "short", leg, T0, dict(rate=0.001, interval_s=7200.0), True,
                         prev, None)
        self.assertEqual(len(booked), 1)
        self.assertAlmostEqual(booked[0]["amount"], 2.0 * 100.0 * 0.001)
        self.assertEqual(booked[0]["evidence"], "SETTLED_PUBLIC_RATE")


if __name__ == "__main__":
    unittest.main()
