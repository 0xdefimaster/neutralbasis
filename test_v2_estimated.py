"""ESTIMATED PAPER (opt-in), the RFQ fill estimator, reference classes, unpriced account
payments, data freshness and the opportunity lifecycle.

All data here is MOCK unless stated; EstimatedDataTests replays the OBSERVED 05:12
snapshot (skipped when absent).
"""
import os
import unittest
from pathlib import Path

from v2 import paths
from unittest import mock

from v2 import economics, estimated, gates, opportunity_log, references, rfq_paper
from v2 import funding_evidence as fe
from v2.strategies import funding as funding_strategy
from v2.strategies.common import Leg
from v2.venues import txflow

AT = 1_794_458_185.0
ISO = "2026-11-12T04:36:20.000Z"          # 5.13 s before AT
PHASE4 = paths.bench("v2-phase4-2026-11-12")
PILOT_DB = paths.pilot_db()


def record(updated=ISO, **quotes):
    q = dict(base=(99.99, 100.01), size_1k=(99.9, 100.1), size_100k=(99.0, 101.0))
    q.update(quotes)
    return dict(quotes={k: v for k, v in q.items() if v is not None},
                quotes_updated=updated)


class RfqQuoteTests(unittest.TestCase):
    def test_exact_bracketed_above(self):
        r = record()
        self.assertEqual(rfq_paper.quote_for(r, 1000.0)["status"], rfq_paper.EXACT)
        b = rfq_paper.quote_for(r, 5000.0)
        self.assertEqual((b["status"], b["upper"]["tier"], b["lower"]["tier"]),
                         (rfq_paper.BRACKETED, "size_100k", "size_1k"))
        self.assertEqual(rfq_paper.quote_for(r, 200000.0)["status"], rfq_paper.ABOVE)
        small = rfq_paper.quote_for(r, 500.0)
        self.assertEqual((small["upper"]["tier"], small["lower"]["tier"]),
                         ("size_1k", "base"))
        self.assertEqual(rfq_paper.quote_for(dict(quotes={}), 1000.0)["status"],
                         rfq_paper.NO_QUOTE)

    def test_between_tiers_has_no_price_only_the_observed_range(self):
        # Phase 8 A3: no unverified interpolation and no "larger tier is worse" claim.
        f = rfq_paper.fill(record(), True, 5000.0, AT)
        self.assertEqual(f["reason"], rfq_paper.UNPRICED)
        self.assertNotIn("price", f)
        self.assertEqual(f["observed_range"], [100.1, 101.0])
        self.assertEqual(f["label"], rfq_paper.LABEL)

    def test_larger_tier_is_not_assumed_worse(self):
        # A 100k quote TIGHTER than the 1k one: the observed order is reported, and a
        # 5k size is still unpriced.
        r = record(size_100k=(99.95, 100.05))
        self.assertEqual(rfq_paper.tier_order(r)["status"], "OBSERVED_NOT_MONOTONE")
        self.assertEqual(rfq_paper.tier_order(record())["status"], "OBSERVED_MONOTONE")
        f = rfq_paper.fill(r, True, 5000.0, AT)
        self.assertEqual(f["reason"], rfq_paper.UNPRICED)
        self.assertEqual(f["observed_range"], [100.05, 100.1])
        self.assertEqual(f["tier_order"], "OBSERVED_NOT_MONOTONE")

    def test_no_liquidity_is_invented_above_the_largest_tier(self):
        r = record(size_100k=None)
        f = rfq_paper.fill(r, True, 5000.0, AT)
        self.assertEqual(f["reason"], rfq_paper.ABOVE)

    def test_staleness_and_clock(self):
        self.assertEqual(rfq_paper.fill(record("2026-11-12T04:30:00Z"), True, 1000, AT)
                         ["reason"], "STALE_QUOTE")
        self.assertEqual(rfq_paper.fill(record("2026-11-12T04:40:00Z"), True, 1000, AT)
                         ["reason"], "QUOTE_IN_FUTURE")
        self.assertEqual(rfq_paper.fill(record(None), True, 1000, AT)["reason"],
                         "QUOTE_TIME_UNKNOWN")
        self.assertAlmostEqual(rfq_paper.quote_age(record(), AT), 5.0, places=3)

    def test_spread_is_counted_once(self):
        rt, why = rfq_paper.round_trip(record(), True, 1000.0, AT, fee=0.0)
        self.assertIsNone(why)
        qty = 1000.0 / 100.0
        self.assertAlmostEqual(rt["total"], qty * (100.1 - 99.9))      # ask - bid, once
        self.assertAlmostEqual(rt["entry_cost"] + rt["exit_cost"], rt["total"])

    def test_open_and_close_on_different_quotes(self):
        o, c = record(), record("2026-11-12T04:41:20.000Z", size_1k=(100.4, 100.6))
        s = rfq_paper.simulate(o, c, True, 1000.0, AT, AT + 300, 0.0001)
        self.assertEqual(s["status"], "SIMULATED")
        self.assertNotEqual(s["status"], "FILLED")
        self.assertAlmostEqual(s["price_pnl"], (1000 / 100.0) * (100.4 - 100.1))
        self.assertAlmostEqual(s["funding_estimated"], -0.0001 * 1000 * 300 / 3600)
        self.assertIn("RATE_PUBLISHED", s["funding_basis"])
        self.assertEqual(rfq_paper.simulate(o, c, True, 1000.0, AT, AT, 0.0)["reason"],
                         "CLOSE_NOT_AFTER_OPEN")

    def test_stress_unknown_volatility_is_not_zero(self):
        st = {x["name"]: x for x in rfq_paper.stress(10.0, 1000.0, 2, 3.0, 30.0, None)}
        self.assertIsNone(st["staleness_1sigma"]["net"])
        self.assertFalse(st["rfq_moderate"]["staleness_included"])
        self.assertAlmostEqual(st["firm_gap_10bp"]["net"], 10.0 - 2 * 0.001 * 1000)
        self.assertAlmostEqual(st["last_look_p0.2"]["net"], 10.0 - 2 * 0.2 * 3.0)
        st2 = {x["name"]: x for x in rfq_paper.stress(10.0, 1000.0, 2, 3.0, 36.0, 0.01)}
        self.assertAlmostEqual(st2["staleness_1sigma"]["net"],
                               10.0 - 2 * 1e4 * 0.01 * 0.1 * 1e-4 * 1000)


class Provider:
    def __init__(self, rows, books, settled=None, closes=None, session=True):
        self.rows, self.books = rows, books
        self.s, self.c, self.session = settled or {}, closes or {}, session

    def rows_by(self):
        return dict(self.rows)

    def book(self, v, a):
        return self.books.get(v)

    def settled(self, v, a, at):
        return self.s.get(v)

    def closes(self, v, a):
        return self.c.get(v)

    def rth_open(self):
        return self.session


CFG = dict(wide_size=1000.0, wide_horizon=24.0, wide_min_net=0.5,
           wide_funding_realization=1.0, wide_max_breakeven_hours=24.0)


def ob(mid, half=0.0005, depth=1e9):
    return dict(bids=[(mid * (1 - half), depth)], asks=[(mid * (1 + half), depth)],
                model="orderbook", empty=False, crossed=False, received=AT)


def row(v, xid, rate, fine="crypto", fee=0.0004, asset="X"):
    return dict(venue=v, asset=asset, exchange_id=xid, rate=rate, fee=fee, mark=100.0,
                interval=3600, fine_class=fine, market_id=asset)


class EstimatedTests(unittest.TestCase):
    def test_switch(self):
        self.assertFalse(estimated.enabled({}))
        self.assertTrue(estimated.enabled({estimated.ENV: "1"}))
        with self.assertRaises(estimated.EstimatedError):
            estimated.enabled({estimated.ENV: "on"})

    def rfq_setup(self, long_rate=-0.002, short_rate=0.0, quote=None):
        rows = {("Variational", "X"): row("Variational", "variational", long_rate, fee=0.0),
                ("Arcus", "X"): row("Arcus", "arcus", short_rate)}
        books = {"Variational": dict(ob(100.0), model="rfq_indicative"), "Arcus": ob(100.0)}
        rec = quote or record(size_1k=(99.98, 100.02))
        g = gates.Gate(Provider(rows, books), CFG)
        return g, estimated.EstimatedEvaluator(g, CFG, lambda v, a: rec if v ==
                                               "Variational" else None)

    def test_rfq_leg_is_estimated_and_strict_stays_closed(self):
        g, ev = self.rfq_setup()
        c = dict(asset="X", long="Variational", short="Arcus")
        strict = g.evaluate(dict(c, strategy="funding"), AT, "funding")
        self.assertFalse(strict["eligible"])
        self.assertIn("EXECUTION_MODEL_UNVERIFIED", [r["code"] for r in strict["reasons"]])
        r = ev.evaluate(dict(c, strategy="funding"), AT, "funding")
        self.assertFalse(r["strict_eligible"])
        self.assertEqual(r["status"], estimated.ENTERED)
        self.assertEqual(r["legs"]["long"]["label"], rfq_paper.LABEL)
        self.assertIn(rfq_paper.LABEL, [a["label"] for a in r["assumptions"]])
        self.assertIn("ESTIMATED_FUNDING_PUBLISHED", [a["label"] for a in r["assumptions"]])
        self.assertNotIn("eligible", r)                         # never a strict flag
        # the strict gate result is unchanged by the estimated evaluation
        self.assertEqual(g.evaluate(dict(c, strategy="funding"), AT, "funding")["reasons"],
                         strict["reasons"])

    def test_stale_quote_rejects_the_estimate(self):
        g, ev = self.rfq_setup(quote=record("2026-11-12T04:20:00Z"))
        r = ev.evaluate(dict(asset="X", long="Variational", short="Arcus"), AT, "funding")
        self.assertEqual((r["status"], r["first"]), (estimated.REJECTED, "STALE_QUOTE"))

    def test_thresholds_are_the_strict_ones(self):
        g, ev = self.rfq_setup(long_rate=-0.00001)              # tiny carry
        r = ev.evaluate(dict(asset="X", long="Variational", short="Arcus"), AT, "funding")
        self.assertEqual(r["status"], estimated.REJECTED)
        self.assertIn("BREAKEVEN", r["reasons"])
        self.assertEqual(CFG["wide_max_breakeven_hours"], 24.0)

    def test_hard_reasons_are_never_estimated(self):
        rows = {("A", "X"): row("A", "arcus", -0.002, fine="equity"),
                ("B", "X"): row("B", "lighter_rh", 0.0, fine="equity")}
        ev = estimated.EstimatedEvaluator(
            gates.Gate(Provider(rows, {"A": ob(100), "B": ob(100)}, session=False), CFG),
            CFG)
        r = ev.evaluate(dict(asset="X", long="A", short="B"), AT, "funding")
        self.assertEqual((r["status"], r["first"]), (estimated.REJECTED,
                                                     "RWA_SESSION_CLOSED"))
        rows2 = {("A", "X"): row("A", "arcus", -0.002),
                 ("H", "X"): row("H", "hyperliquid_native", 0.0)}
        ev2 = estimated.EstimatedEvaluator(gates.Gate(Provider(rows2, {"A": ob(100),
                                                                       "H": ob(100)}),
                                                      CFG), CFG)
        r2 = ev2.evaluate(dict(asset="X", long="A", short="H"), AT, "funding")
        self.assertEqual((r2["status"], r2["first"]), (estimated.REJECTED, "CONFIG_EXCLUDED"))
        sc = ev2.scenario(dict(asset="X", long="A", short="H"), AT)
        self.assertEqual(sc["status"], "SCENARIO_ONLY")
        self.assertNotEqual(sc["status"], estimated.ENTERED)

    def test_reference_classes_drive_the_estimate(self):
        rows = {("A", "X"): row("A", "arcus", -0.002, fine="commodity"),
                ("B", "X"): row("B", "lighter_rh", 0.0, fine="commodity")}
        books = {"A": ob(100), "B": ob(100)}
        settled = {v: [dict(ts=AT - h * 3600, rate=0.0, kind="settled_public")
                       for h in range(168)] for v in ("A", "B")}
        g = gates.Gate(Provider(rows, books, settled), CFG)
        ev = estimated.EstimatedEvaluator(g, CFG)
        saved = dict(references.NORM)
        try:
            base = dict(underlying_id="U", unit="u", exchange="E", contract_month="M",
                        roll_rule_id="R")
            references.NORM[("arcus", "X")] = dict(base)
            references.NORM[("lighter_rh", "X")] = dict(base, underlying_id="V")
            r = ev.evaluate(dict(asset="X", long="A", short="B"), AT, "funding")
            self.assertEqual(r["first"], "DIFFERENT_UNDERLYING")
            references.NORM[("lighter_rh", "X")] = dict(base, contract_month=references.UNKNOWN)
            r = ev.evaluate(dict(asset="X", long="A", short="B"), AT, "funding")
            self.assertIn("ESTIMATED_REFERENCE_BASIS_RISK", [a["label"] for a in
                                                             r["assumptions"]])
        finally:
            references.NORM.clear()
            references.NORM.update(saved)

    def test_runtime_never_hands_estimates_to_the_desk(self):
        from v2.runtime import Runtime

        class Desk:
            opened = []

            def cycle(self):
                pass

            def open_positions(self):
                return []

            def open(self, *a, **k):
                Desk.opened.append(a)

        class Col:
            by_key, records, reasons, sink = {}, {}, {}, None

            def refresh(self):
                return []

        g, _ = self.rfq_setup()
        rt = Runtime(Col(), g, dict(CFG, wide_hybrid_enabled=True), desk=Desk(),
                     estimated=True, clock=lambda: AT)
        with mock.patch.object(rt, "candidates", lambda rows, at: dict(
                funding=[dict(asset="X", long="Variational", short="Arcus",
                              strategy="funding")], convergence=[], hybrid=[])):
            snap = rt.evaluate_once()
        self.assertEqual(Desk.opened, [])
        self.assertEqual(snap["estimated"]["summary"]["funding"]["seen"], 1)
        self.assertIn("not strict", snap["estimated"]["label"])
        rt2 = Runtime(Col(), g, CFG, estimated=False, clock=lambda: AT)
        with mock.patch.object(rt2, "candidates", lambda rows, at: dict(
                funding=[], convergence=[], hybrid=[])):
            self.assertIsNone(rt2.evaluate_once()["estimated"])


class ReferenceClassTests(unittest.TestCase):
    def test_natgas_classes(self):
        self.assertEqual(references.classify("txflow", "tradexyz", "NATGAS")["cls"],
                         references.UNKNOWN_CLASS)
        c = references.classify("tradexyz", "variational", "NATGAS")
        self.assertEqual(c["cls"], references.ECONOMICALLY_RELATED)
        self.assertFalse(c["hedge_ok"])
        self.assertIn("contract_month", c["unknown"])

    def test_oracle_method_is_not_a_different_underlying(self):
        saved = dict(references.NORM)
        try:
            same = dict(underlying_id="U", unit="u", exchange="E", contract_month="M",
                        roll_rule_id="R")
            references.NORM[("tradexyz", "NATGAS")] = dict(same)
            references.NORM[("variational", "NATGAS")] = dict(same)
            c = references.classify("tradexyz", "variational", "NATGAS")
            self.assertEqual(c["cls"], references.EXACT_REFERENCE)
            self.assertTrue(c["method_differences"])            # reported, not blocking
        finally:
            references.NORM.clear()
            references.NORM.update(saved)

    def test_same_ticker_is_not_evidence(self):
        self.assertEqual(references.classify("arcus", "lighter_rh", "GOLD")["cls"],
                         references.UNKNOWN_CLASS)


class UnpricedPaymentTests(unittest.TestCase):
    def pts(self, n=168):
        return [dict(ts=AT - h * 3600, rate=None, kind="account_payment", amount=-0.01)
                for h in range(n)]

    def test_payments_without_rate_are_not_a_yield(self):
        e = fe.market_evidence("txflow", self.pts(), AT)
        self.assertEqual((e["status"], e["basis"], e["sufficient"]),
                         (fe.ACCOUNT_PAYMENT, "RATE_UNIT_UNVERIFIED", False))
        rows = {("TxFlow", "X"): row("TxFlow", "txflow", 0.0), ("Arcus", "X"):
                row("Arcus", "arcus", 0.0)}
        g = gates.Gate(Provider(rows, {}, {"TxFlow": self.pts()}), CFG)
        self.assertFalse(g.market_settled("TxFlow", "X", AT))
        self.assertIn("SETTLEMENT_EVIDENCE_MISSING", [r["code"] for r in g.static(
            dict(asset="X", long="TxFlow", short="Arcus", strategy="funding"), rows, AT)])

    def test_funding_strategy_does_not_crash_on_unpriced_points(self):
        long = Leg(row=row("TxFlow", "txflow", 0.0), book=ob(100.0), settled=self.pts())
        short = Leg(row=row("Arcus", "arcus", 0.0), book=ob(100.0),
                    settled=[dict(ts=AT - h * 3600, rate=0.00001, kind="settled_public")
                             for h in range(168)])
        ev = funding_strategy.evaluate(long, short, 1000.0, CFG, AT)
        self.assertIn("SETTLEMENT_EVIDENCE_MISSING", [r["code"] for r in ev["reasons"]])

    def test_rate_unit_needs_consistent_evidence(self):
        rows = [dict(ts=AT + i * 3600, szi=10.0, amount=-0.012, rate_raw=0.012)
                for i in range(4)]                                          # MOCK
        unit, _ = txflow.infer_rate_unit(rows, lambda ts: 10.0)   # implied 0.00012
        self.assertEqual(unit, "PERCENT")
        unit2, _ = txflow.infer_rate_unit(rows[:2], lambda ts: 10.0)
        self.assertEqual(unit2, "UNVERIFIED")                       # too few rows
        unit3, _ = txflow.infer_rate_unit(rows, lambda ts: None)    # no mark: no guess
        self.assertEqual(unit3, "UNVERIFIED")
        self.assertTrue(all(r["rate"] is None for r in
                            txflow.apply_rate_unit(rows, "UNVERIFIED")))
        self.assertAlmostEqual(txflow.apply_rate_unit(rows, "PERCENT")[0]["rate"], 0.00012)


class FreshnessLifecycleTests(unittest.TestCase):
    def test_freshness_limit(self):
        self.assertEqual(opportunity_log.freshness_limit(None), (60.0, "VOL_UNKNOWN"))
        lim, basis = opportunity_log.freshness_limit(0.005)          # 0.5 %/h
        self.assertEqual(basis, "VOLATILITY")
        self.assertAlmostEqual(lim, 36.0)
        self.assertEqual(opportunity_log.freshness_limit(0.05)[1], "FLOOR")
        self.assertEqual(opportunity_log.freshness_limit(0.0001)[1], "CAP")

    def test_lifecycle(self):
        obs = [dict(at=0, asset="X", long="A", short="B", present=True, advantage=0.001,
                    liquid=True, estimated_return=5.0, verified_return=None),
               dict(at=60, asset="X", long="A", short="B", present=True, advantage=0.003,
                    liquid=True, estimated_return=6.0, verified_return=4.0),
               dict(at=120, asset="X", long="A", short="B", present=False, advantage=None,
                    liquid=False)]
        s = opportunity_log.lifecycle(obs)["funding|X|A|B"]
        self.assertEqual((s["first_seen"], s["last_seen"], s["duration_s"]), (0, 60, 60))
        self.assertEqual(s["max_net_advantage"], 0.003)
        self.assertEqual(s["liquid_duration_s"], 60)
        self.assertEqual(s["estimated_vs_verified"], 2.0)
        self.assertEqual(s["verified"], "PARTIAL")


@unittest.skipUnless((PHASE4 / "answers.json.gz").exists() and PILOT_DB.exists(),
                     "OBSERVED 05:12 snapshot not on this machine")
class EstimatedDataTests(unittest.TestCase):
    def test_strict_unchanged_and_estimated_separate(self):
        from tools import v2_estimated as ve

        r = ve.snapshot_compare(PHASE4)
        s = r["summary"]
        self.assertEqual(sum(x["seen"] for x in s.values()), 2597)
        self.assertEqual(sum(x["strict_eligible"] for x in s.values()), 0)
        entries = {(e["strategy"], e["asset"], e["long"], e["short"]) for e in r["entries"]}
        self.assertIn(("funding", "NATGAS", "TxFlow", "tradeXYZ"), entries)
        self.assertTrue(all(e["status"] == estimated.ENTERED and not e["strict_eligible"]
                            for e in r["entries"]))
        self.assertEqual(r["init"], [])                 # INIT: no V2 candidate at all


if __name__ == "__main__":
    unittest.main()
