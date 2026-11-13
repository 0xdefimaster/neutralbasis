"""Phase 13: opportunity labels as a VIEW of the existing funnel / estimated / ledger
state, strategy-aware discovery freshness, funding economics across intervals, the
opportunity scanner, Variational adaptive polling and its PnL levels.

All data here is MOCK unless a test says OBSERVED.
"""
import unittest

from v2 import funnel, opportunity, opportunity_log
from v2 import gates


class LabelTests(unittest.TestCase):
    def test_labels_derive_from_existing_state_only(self):
        self.assertEqual(set(opportunity.LABELS), set(opportunity.DERIVED_FROM))
        L = opportunity.label
        self.assertEqual(L(trace=dict(outcome="ELIGIBLE")), "STRICT_PAPER_ELIGIBLE")
        self.assertEqual(L(trace=dict(outcome="NOT_EVALUATED"), simulated=True),
                         "ESTIMATED_PAPER_SIMULATED")
        self.assertEqual(L(est=dict(reasons=["DIFFERENT_UNDERLYING"])),
                         "BLOCKED_REFERENCE_MISMATCH")
        self.assertEqual(L(est=dict(status="ESTIMATED_ENTRY_SIGNAL"),
                           freshness=dict(status="STALE")), "DATA_STALE")
        self.assertEqual(L(est=dict(status="ESTIMATED_ENTRY_SIGNAL")),
                         "ESTIMATED_PAPER_CANDIDATE")
        self.assertEqual(L(est=dict(status="ESTIMATED_REJECTED", first="STALE_QUOTE",
                                    strict_reasons=["EXECUTION_MODEL_UNVERIFIED"])),
                         "BLOCKED_EXECUTION_MODEL")
        self.assertEqual(L(trace=dict(outcome="UNPROFITABLE")), "BLOCKED_ECONOMICS")
        self.assertEqual(L(est=dict(status="ESTIMATED_REJECTED", first="STRESS_NEGATIVE",
                                    net=0.5, strict_reasons=["EXECUTION_MODEL_UNVERIFIED"])),
                         "BLOCKED_ECONOMICS")
        self.assertEqual(L(trace=dict(outcome="NOT_EVALUATED"), carry=1e-4), "WATCHLIST")
        self.assertEqual(L(trace=dict(outcome="NOT_EVALUATED"), carry=-1e-4), "DISCOVERED")

    def test_no_new_state_machine(self):
        # the funnel's own stages/outcomes are unchanged and still the source
        self.assertEqual(funnel.OUTCOMES, ("NOT_EVALUATED", "FAILED", "UNPROFITABLE",
                                           "RESEARCH_ONLY", "ELIGIBLE"))
        self.assertNotIn("WATCHLIST", funnel.OUTCOMES)


class FreshnessPolicyTests(unittest.TestCase):
    def leg(self, price_age, rate_age=60.0, interval=3600.0, t=1000.0):
        return dict(price_age_s=price_age, rate_age_s=rate_age, interval_s=interval,
                    price_time=t)

    def test_funding_discovery_accepts_variational_research_ages(self):
        f = opportunity.discovery_freshness("funding", [self.leg(55.0), self.leg(2.0)])
        self.assertEqual(f["status"], "FRESH")                     # 40-60 s quote: research
        self.assertEqual(opportunity.discovery_freshness(
            "funding", [self.leg(335.0), self.leg(2.0)])["status"], "STALE")
        self.assertEqual(opportunity.discovery_freshness(
            "funding", [self.leg(5.0, rate_age=5 * 3600, interval=4 * 3600),
                        self.leg(2.0)])["status"], "STALE")        # previous period's rate

    def test_price_gap_needs_synchronous_prices(self):
        a = self.leg(3.0, t=1000.0)
        b = self.leg(3.0, t=1030.0)
        self.assertEqual(opportunity.discovery_freshness("price_gap", [a, b], 0.005)["status"],
                         "FRESH")                                  # 30 s < ~36 s limit
        self.assertEqual(opportunity.discovery_freshness("price_gap", [a, b], 0.05)["status"],
                         "ASYNCHRONOUS")                           # volatile: 2 s floor

    def test_strict_execution_limits_unchanged(self):
        self.assertEqual(opportunity_log.FLOOR_S, 2.0)
        self.assertIn("Variational", gates.RFQ_VENUES)
        from v2 import marketdata, rfq_paper

        self.assertEqual(rfq_paper.MAX_QUOTE_AGE_S, 60.0)
        self.assertEqual(marketdata.MAX_AGE["tradexyz"], 15.0)



class FundingMathTests(unittest.TestCase):
    H = 3600.0

    def test_interval_mismatch_counts_real_payment_times(self):
        from v2 import funding_math as fm

        t0 = 1_000_000.0 * 3600
        one_h = dict(rate_per_period=0.0001, interval_s=self.H, next_ts=t0 + 1800)
        eight_h = dict(rate_per_period=0.0008, interval_s=8 * self.H, next_ts=t0 + 7 * self.H)
        r = fm.pair_cash(one_h, eight_h, 1000.0, t0, t0 + 6 * self.H)
        self.assertEqual(len(r["long"]["payments"]), 6)            # 0.5h ... 5.5h
        self.assertEqual(len(r["short"]["payments"]), 0)           # 8h leg: none in 6 h
        self.assertAlmostEqual(r["total"], -0.6)                   # long pays 6 x 0.1
        # the common-horizon view says both are 0.08 %/8h: equal on paper, not in cash
        self.assertAlmostEqual(r["common_horizon_8h"]["long"], 0.0008)
        self.assertAlmostEqual(r["common_horizon_8h"]["short"], 0.0008)

    def test_sign_inversion_and_open_at_payment(self):
        from v2 import funding_math as fm

        t0 = 0.0
        neg = dict(rate_per_period=-0.0002, interval_s=self.H, next_ts=t0 + self.H)
        self.assertAlmostEqual(fm.leg_cash("long", neg, 1000, t0, t0 + 2.5 * self.H)["total"],
                               +0.4)                               # long RECEIVES at r < 0
        self.assertAlmostEqual(fm.leg_cash("short", neg, 1000, t0, t0 + 2.5 * self.H)["total"],
                               -0.4)
        # closed exactly at the payment instant: not paid (conservative)
        self.assertEqual(fm.leg_cash("long", neg, 1000, t0, t0 + self.H)["payments"], [])

    def test_unknown_phase_gives_a_band_not_a_number(self):
        from v2 import funding_math as fm

        leg = dict(rate_per_period=0.0001, interval_s=self.H, next_ts=None)
        r = fm.leg_cash("short", leg, 1000, 0.0, 2.5 * self.H)
        self.assertEqual(r["phase"], "UNKNOWN")
        self.assertIsNone(r["total"])
        self.assertEqual(r["band"], (0.2, 0.30000000000000004))

    def test_breakeven_on_the_discrete_schedule(self):
        from v2 import funding_math as fm

        t0 = 0.0
        lg = dict(rate_per_period=0.0, interval_s=self.H, next_ts=t0 + 600)
        sh = dict(rate_per_period=0.0005, interval_s=4 * self.H, next_ts=t0 + 3 * self.H)
        # $1000: +0.5 per 4h payment at 3h, 7h, 11h...; cost 1.2 -> third payment (11h)
        self.assertAlmostEqual(fm.min_hold_to_breakeven(lg, sh, 1000, t0, 1.2), 11.0)
        self.assertIsNone(fm.min_hold_to_breakeven(lg, sh, 1000, t0, 1e9))
        self.assertEqual(len(fm.PNL_CLASSES), 7)


class FreshnessUnknownTests(unittest.TestCase):
    def test_a_known_stale_leg_beats_an_unknown_one(self):
        legs = [dict(price_age_s=335.0, rate_age_s=None, interval_s=3600, price_time=None),
                dict(price_age_s=None, rate_age_s=None, interval_s=3600, price_time=None)]
        self.assertEqual(opportunity.discovery_freshness("funding", legs)["status"], "STALE")
        legs[0]["price_age_s"] = 40.0
        self.assertEqual(opportunity.discovery_freshness("funding", legs)["status"], "UNKNOWN")


from v2 import paths as _paths  # noqa: E402


@unittest.skipUnless(_paths.bench("v2-phase4-2026-11-12", "answers.json.gz").exists()
                     and _paths.closes_available(), "OBSERVED 05:12 snapshot not here")
class ScannerDataTests(unittest.TestCase):
    """OBSERVED 05:12 snapshot, a few assets: the three reports stay apart."""

    def test_reports_are_nested_and_nothing_is_verified(self):
        from tools import v2_forensics as vf
        from tools import v2_opportunity_scan as sc

        r = vf.replay(_paths.bench("v2-phase4-2026-11-12"))
        rows, matched = sc.scan(r, 1000.0, assets={"NATGAS", "CL", "BTC", "INIT"})
        rep = sc.report(rows, matched)
        d, e, x = rep["DISCOVERY"], rep["ECONOMIC"], rep["EXECUTION_READINESS"]
        self.assertLessEqual(e["expected_net_positive"], d["total"])
        self.assertLessEqual(e["stress_positive"], e["expected_net_positive"])
        self.assertEqual(x["strict_paper_eligible"], 0)
        self.assertTrue(all(row["label"] in opportunity.LABELS for row in rows))
        self.assertTrue(all(not row["strict_eligible"] for row in rows))
        self.assertIn("NATGAS", {row["asset"] for row in rows
                                 if row["label"] == "ESTIMATED_PAPER_CANDIDATE"})
        # a Variational row with a 335 s quote is DATA_STALE, never a candidate
        var = [row for row in rows if "Variational" in (row["long"], row["short"])]
        self.assertTrue(var and all(row["label"] != "ESTIMATED_PAPER_CANDIDATE" for row in var))


class VariationalPollerTests(unittest.TestCase):
    def test_adaptive_interval_single_flight_and_429(self):
        from v2 import variational_poller as vp

        now = [0.0]
        p = vp.AdaptivePoller(clock=lambda: now[0])
        self.assertTrue(p.acquire())
        self.assertFalse(p.acquire())                       # single flight
        p.on_answer({"INIT-PERP": 100.0})
        self.assertEqual(p.interval, 30.0)
        now[0] = 30.0
        self.assertTrue(p.acquire())
        p.on_answer({"INIT-PERP": 100.0})                   # repeat: venue cache
        self.assertEqual(p.interval, 45.0)
        now[0] = 75.0
        self.assertTrue(p.acquire())
        p.on_429(retry_after=120)
        now[0] = 150.0
        self.assertFalse(p.acquire())                       # retry_after honoured
        now[0] = 196.0
        self.assertTrue(p.acquire())
        self.assertEqual(p.stats["http_429"], 1)

    def test_learns_the_refresh_cadence_within_bounds(self):
        from v2 import variational_poller as vp

        now = [0.0]
        p = vp.AdaptivePoller(clock=lambda: now[0])
        for k, u in enumerate((0, 40, 80, 121, 160)):
            now[0] = k * 100.0
            p.next_at = 0
            self.assertTrue(p.acquire())
            p.on_answer({"X": float(u)})
        self.assertAlmostEqual(p.interval, 40.0, places=0)
        self.assertTrue(vp.MIN_S <= p.interval <= vp.MAX_S)

    def test_five_levels_kept_apart(self):
        from v2 import variational_poller as vp

        q = dict(bid=99.98, ask=100.02)
        other = dict(bids=[(100.50, 10)], asks=[(100.52, 10)])
        r = vp.pnl_levels(q, other, True, 1000.0, 40.0, sigma_hourly=0.01,
                          fee_var=0.0, fee_other=0.0004)
        self.assertAlmostEqual(r["THEORETICAL"], 5.1, places=1)
        self.assertLess(r["AGE_ADJUSTED"][0], r["THEORETICAL"])
        self.assertLess(r["NET_AFTER_COSTS"], r["THEORETICAL"])   # sides + fees
        self.assertLess(r["RFQ_STRESS"], r["NET_AFTER_COSTS"])
        self.assertTrue(r["EXECUTABLE_PROOF"].startswith("NOT_VERIFIED"))
        self.assertIsNone(vp.pnl_levels(q, other, True, 1000.0, 40.0)["AGE_ADJUSTED"])


if __name__ == "__main__":
    unittest.main()
