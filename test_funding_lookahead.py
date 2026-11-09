"""Funding timing and lookahead (funding_observations.py) on RECORDED REAL public data
(testdata/risex_lighter_funding_20261109.json). Positions are MOCK and labelled.
Local time on the recording machine: UTC+3; epoch seconds are used throughout."""

import json, os, unittest

import capabilities
import funding_observations as fo

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "testdata", "risex_lighter_funding_20261109.json"),
          encoding="utf8") as f:
    FX = json.load(f)

H20, H21 = 1794243600, 1794247200          # 2026-11-09 20:00 and 21:00 (UTC+3)
RATE_20 = 2.06416900385e-05               # period ending 20:00 (BTC)
RATE_21 = 1.7807523761369e-05             # period ending 21:00 (BTC)


def btc():
    obs = []
    for h in FX["history"]:
        if h["market"] == "BTC":
            obs += [fo.risex_settled(r, h["observed_at"]) for r in h["records"]]
    obs += [fo.risex_live(w["BTC"], w["observed_at"], 3600) for w in FX["markets_watch"]]
    return obs


class L1PeriodNotKnownBeforeItEnds(unittest.TestCase):
    def test_at_2030_only_the_period_ending_2000_is_known(self):
        at = H20 + 1800
        rate, end, kind = fo.latest_rate(btc(), at)
        self.assertEqual(end, H20)
        self.assertAlmostEqual(rate, RATE_20, places=15)
        self.assertFalse(any(o.period_end == H21 for o in fo.knowable(btc(), at)))


class L2SettledIsNotObserved(unittest.TestCase):
    def test_settled_at_2100_is_not_known_at_2101(self):
        at = H21 + 60
        pending = fo.settled_but_not_observed(btc(), at)
        self.assertTrue(any(o.period_end == H21 for o in pending))
        self.assertEqual(fo.latest_rate(btc(), at)[1], H20)  # still the old period
        # Our first sample after 21:00 (21:03:40) is the first time it is known.
        first = min(o.observed_at for o in btc()
                    if o.period_end == H21 and o.kind == "last_settled")
        self.assertGreater(first, H21 + 200)
        self.assertEqual(fo.latest_rate(btc(), first)[1], H21)


class L3LastSettledIsNotAForecast(unittest.TestCase):
    def test_live_rate_is_last_settled_and_no_forecast_exists(self):
        live = [o for o in btc() if o.source == "markets"]
        self.assertTrue(live)
        self.assertTrue(all(o.kind == "last_settled" for o in live))
        at = H20 + 1800
        self.assertIsNone(fo.forecast_for(btc(), H21, at))
        self.assertIsNone(fo.view(btc(), at)["forecast_next"])
        # In-period samples carry the PREVIOUS period's rate (recorded experiment).
        inside = [o for o in live if H20 < o.observed_at < H21]
        self.assertEqual({o.period_end for o in inside}, {H20})
        self.assertEqual({round(o.rate, 18) for o in inside}, {round(RATE_20, 18)})


class L4TruncationEquivalence(unittest.TestCase):
    def test_no_decision_view_changes_when_future_data_is_removed(self):
        everything = btc()
        for at in range(H20 - 300, H21 + 1500, 60):
            self.assertEqual(fo.view(everything, at),
                             fo.view(fo.knowable(everything, at), at), at)


class L5PaperAccrualTiming(unittest.TestCase):
    """MOCK paper positions on real settlements."""

    def test_position_opened_inside_a_period_gets_no_full_period_accrual(self):
        rows = fo.paper_accrual(entry_at=H20 + 1800, exit_at=H21 + 1800, size=1.0,
                                entry_checkpoint=None, settled=btc(), at=H21 + 1800)
        self.assertEqual([r["status"] for r in rows], ["UNKNOWN_PARTIAL_PERIOD"])
        self.assertIsNone(rows[0]["payment"])
        self.assertIsNone(rows[0]["cash"])

    def test_nothing_accrues_before_the_settlement_is_observed(self):
        for at in (H21 - 60, H21 + 60):
            self.assertEqual(fo.paper_accrual(H20, H21 + 3600, 1.0, None, btc(), at), [])

    def test_whole_periods_use_checkpoints_and_signed_size(self):
        # MOCK long held 17:00-20:00 (UTC+3), evaluated at 20:10 on the real records.
        h17 = H20 - 3 * 3600
        rows = fo.paper_accrual(h17, H20, 1.0, None, btc(), H20 + 600)
        self.assertEqual([r["status"] for r in rows],
                         ["UNKNOWN_NO_CHECKPOINT", "KNOWN", "KNOWN"])
        settled = {o.period_end: o for o in btc() if o.kind == "settled"}
        for r in rows[1:]:
            o, prev = settled[r["period_end"]], settled[r["period_end"] - 3600]
            self.assertAlmostEqual(r["payment"], o.accumulated - prev.accumulated, places=9)
            self.assertAlmostEqual(r["payment"], o.rate * o.index_price, places=6)
            self.assertEqual(r["cash"], -r["payment"])
            self.assertEqual(r["evidence"], "paper_accrual")
        short = fo.paper_accrual(h17, H20, -1.0, None, btc(), H20 + 600)
        self.assertEqual([r["payment"] for r in short[1:]],
                         [-r["payment"] for r in rows[1:]])

    def test_a_checkpoint_really_observed_at_entry_makes_the_first_period_known(self):
        # MOCK: a checkpoint observation taken at the moment of entry.
        h17 = H20 - 3 * 3600
        s17 = next(o for o in btc() if o.kind == "settled" and o.period_end == h17)
        cp = fo.FundingObservation("RISEx", "BTC", "settled", s17.rate, observed_at=h17,
                                   accumulated=s17.accumulated, source="MOCK checkpoint")
        rows = fo.paper_accrual(h17, H20, 1.0, cp, btc(), H20 + 600)
        self.assertEqual([r["status"] for r in rows], ["KNOWN"] * 3)

    def test_a_missing_period_is_not_filled(self):
        h17 = H20 - 3 * 3600
        gap = [o for o in btc() if not (o.kind == "settled" and o.period_end == h17 + 3600)]
        rows = fo.paper_accrual(h17, H20, 1.0, None, gap, H20 + 600)
        self.assertIn("MISSING", [r["status"] for r in rows])
        self.assertTrue(all(r["payment"] is None for r in rows if r["status"] != "KNOWN"))


class L6SignChain(unittest.TestCase):
    def test_public_settlements_are_consistent_and_longs_pay_positive_rates(self):
        settled = fo.unique_settled(btc())
        # 48 + the 21:00 record from a later download + 23:00 and 00:00 (a third one).
        self.assertEqual(len(settled), 51)
        checks = fo.settled_consistency(btc())
        # Adjacent periods only: 22:00 is not in the recording, so (21:00, 23:00) is
        # not compared; (23:00, 00:00) is. 48 + 1 adjacent pairs.
        self.assertEqual(len(checks), 49)
        self.assertTrue(all(ok for _, ok in checks), [c for c in checks if not c[1]])
        xs = sorted(settled, key=lambda o: o.period_end)
        for a, b in zip(xs, xs[1:]):
            if b.period_start != a.period_end:
                continue
            step = b.accumulated - a.accumulated
            # docs: payment = step * signed size, positive = paid.
            self.assertEqual(step * 1.0 > 0, b.rate > 0)    # long pays when rate > 0
            self.assertEqual(step * -1.0 > 0, b.rate < 0)   # short pays when rate < 0


class L7LighterAnalog(unittest.TestCase):
    def test_lighter_rows_are_known_only_after_download_and_have_no_period(self):
        lf = FX["lighter"]
        obs = [fo.lighter_settled(r, lf["observed_at"]) for r in lf["rows"]]
        before = lf["observed_at"] - 1
        self.assertEqual(fo.knowable(obs, before), [])
        self.assertEqual(len(fo.settled_but_not_observed(obs, before)), len(obs))
        self.assertTrue(all(o.period_end is None for o in obs))
        self.assertIsNone(fo.latest_rate(obs, lf["observed_at"] + 1))  # no period invented
        self.assertAlmostEqual(obs[-1].rate, 1.2e-5)


class L8GateRegression(unittest.TestCase):
    def test_model_is_not_wired_and_acceptance_rules_are_unchanged(self):
        for name in ("wide_paper.py", "capabilities.py", "desk_analytics.py",
                     "risk_scan.py", "bot.py", "connectors.py"):
            with open(os.path.join(HERE, name), encoding="utf8") as f:
                self.assertNotIn("funding_observations", f.read(), name)
        self.assertEqual(capabilities.SIGN_AGREE, 0.8)
        self.assertEqual(capabilities.PUBLISHED_MIN, 3)
        self.assertEqual(tuple(capabilities.EXECUTABLE_MODELS), ("PERIODIC",))
        self.assertEqual(capabilities.funding_model("RISEx")["model"], "PERIODIC")


class EvidenceCategoryTests(unittest.TestCase):
    def test_categories_stay_separate_and_unknowns_stay_unknown(self):
        self.assertEqual(fo.ACCOUNT_CASH_STATUS, "UNAVAILABLE")
        self.assertEqual(fo.RISEX_SETTLEMENT_TRIGGER, "UNKNOWN")
        with self.assertRaises(ValueError):
            fo.FundingObservation("RISEx", "BTC", "prediction", 0.0, observed_at=1.0)

    def test_accumulated_funding_is_flat_inside_a_period_and_steps_at_settlement(self):
        rows = FX["accumulated_watch"]
        midnight = 1794258000  # 2026-11-10 00:00 (UTC+3)
        before = {r["BTC"]["acc"] for r in rows if r["observed_at"] < midnight}
        after = {r["BTC"]["acc"] for r in rows if r["observed_at"] > midnight + 30}
        self.assertEqual(len(before), 1)
        self.assertEqual(len(after), 1)
        self.assertNotEqual(before, after)
        # The live value IS the last settlement's accumulated funding (checkpoint does
        # not move inside a period): 23:00 before midnight, 00:00 after.
        settled = {o.period_end: o for o in fo.unique_settled(btc())}
        self.assertEqual(float(before.pop()), settled[midnight - 3600].accumulated)
        self.assertEqual(float(after.pop()), settled[midnight].accumulated)


if __name__ == "__main__":
    unittest.main()
