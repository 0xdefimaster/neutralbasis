"""tools/shadow_ranking: no-lookahead and cost rules. All series are MOCK (synthetic,
deterministic); they test the method, they are not evidence about any market."""
import math
import random
import unittest

from tools import shadow_ranking as sr

H = 3600
T0 = 1_000 * 86400


def mock_pair(days=30, seed=1, carry=2e-5, gap_mean=0.002, gap_noise=0.0005):
    rnd = random.Random(seed)
    carry_rows, gaps, g = [], [], gap_mean
    for i in range(days * 24):
        ts = T0 + i * H
        carry_rows.append((ts, carry + rnd.gauss(0, carry)))
        g = gap_mean + 0.8 * (g - gap_mean) + rnd.gauss(0, gap_noise)
        gaps.append((ts, g))
    return carry_rows, gaps


COSTS = dict(fees=0.0014, drag=0.0004)


class NoLookaheadTests(unittest.TestCase):
    def test_score_ignores_everything_at_or_after_t(self):
        carry, gaps = mock_pair()
        t = T0 + 20 * 86400
        full = sr.score(carry, gaps, t, COSTS)
        cut = sr.score([r for r in carry if r[0] < t], [r for r in gaps if r[0] < t], t, COSTS)
        self.assertEqual(full, cut)

    def test_changing_the_future_does_not_change_the_score(self):
        carry, gaps = mock_pair()
        t = T0 + 20 * 86400
        base = sr.score(carry, gaps, t, COSTS)
        future_carry = [(ts, v if ts < t else v * -50) for ts, v in carry]
        future_gaps = [(ts, v if ts < t else 0.5) for ts, v in gaps]
        self.assertEqual(base, sr.score(future_carry, future_gaps, t, COSTS))
        # ...while the realized outcome does change: it reads only the future.
        a = sr.realized(carry, gaps, t, 48, COSTS)["value"]
        b = sr.realized(future_carry, future_gaps, t, 48, COSTS)["value"]
        self.assertNotEqual(a, b)

    def test_walk_forward_scores_equal_truncated_scores(self):
        carry, gaps = mock_pair(days=22)
        rows = sr.walk_forward(carry, gaps, COSTS, T0 + 15 * 86400, T0 + 22 * 86400, step_h=24)
        self.assertTrue(rows)
        for r in rows:
            t = r["t"]
            cut = sr.score([x for x in carry if x[0] < t], [x for x in gaps if x[0] < t], t, COSTS)
            self.assertEqual(r["score"]["best_value"], cut["best_value"])

    def test_incomplete_outcome_window_is_none_not_extrapolated(self):
        carry, gaps = mock_pair(days=16)
        t = T0 + 15 * 86400
        self.assertIsNone(sr.realized(carry, gaps, t, 48, COSTS))

    def test_reversion_fit_uses_only_closed_windows_before_t(self):
        carry, gaps = mock_pair()
        t = T0 + 20 * 86400
        normal = sr.normals(gaps)
        a = sr.reversion(gaps, t, 48, normal)
        poisoned = [(ts, v if ts <= t else 9.0) for ts, v in gaps]
        b = sr.reversion(poisoned, t, 48, sr.normals(poisoned))
        self.assertEqual(a, b)


class EconomicsTests(unittest.TestCase):
    def test_funding_needs_1_3_and_7_day_agreement(self):
        carry, gaps = mock_pair()
        t = T0 + 20 * 86400
        # The last day turns against the position: no funding is credited.
        flipped = [(ts, -abs(v) if ts >= t - 24 * H else v) for ts, v in carry]
        s = sr.score(flipped, gaps, t, COSTS)
        self.assertTrue(all(x["funding"] == 0 for x in s["horizons"].values()))

    def test_funding_is_the_weakest_window(self):
        carry, gaps = mock_pair(carry=1e-5)
        t = T0 + 20 * 86400
        s = sr.score(carry, gaps, t, COSTS)
        per_h = min(s["carry_1d"], s["carry_3d"], s["carry_7d"])
        self.assertAlmostEqual(s["horizons"][48]["funding"], per_h * 48)

    def test_costs_are_charged_in_full(self):
        carry, gaps = mock_pair()
        t = T0 + 20 * 86400
        cheap = sr.score(carry, gaps, t, dict(fees=0.0, drag=0.0))
        dear = sr.score(carry, gaps, t, dict(fees=0.0014, drag=0.0010))
        for h in sr.HORIZONS:
            self.assertAlmostEqual(cheap["horizons"][h]["value"] - dear["horizons"][h]["value"], 0.0024)

    def test_unknown_book_cost_is_unknown_not_zero(self):
        carry, gaps = mock_pair()
        s = sr.score(carry, gaps, T0 + 20 * 86400, dict(fees=0.0014, drag=None))
        self.assertEqual(s["status"], "UNKNOWN")

    def test_thin_history_is_unknown(self):
        carry, gaps = mock_pair(days=5)
        s = sr.score(carry, gaps, T0 + 5 * 86400, COSTS)
        self.assertEqual(s["status"], "UNKNOWN")

    def test_round_trip_drag_walks_the_book_and_refuses_partial_fills(self):
        book = dict(bids=[(99.0, 1.0), (98.0, 10.0)], asks=[(101.0, 1.0), (102.0, 10.0)])
        # $100 notional = 1 unit: crosses 101 vs 99 on a mid of 100 -> 2 %.
        self.assertAlmostEqual(sr.round_trip_drag(book, 100.0), 0.02)
        # 2 units walk to the second level: (101.5 - 98.5) / 100.
        self.assertAlmostEqual(sr.round_trip_drag(book, 200.0), 0.03)
        self.assertIsNone(sr.round_trip_drag(book, 5000.0))

    def test_funding_only_mode_credits_and_books_no_price(self):
        carry, gaps = mock_pair(gap_noise=0.003)
        t = T0 + 20 * 86400
        s = sr.score(carry, gaps, t, COSTS, price=False)
        self.assertTrue(all(x["price"] == 0.0 and x["reversion"] is None
                            for x in s["horizons"].values()))
        r = sr.realized(carry, gaps, t, 48, COSTS, price=False)
        self.assertAlmostEqual(r["value"], r["funding"] - COSTS["fees"] - COSTS["drag"])
        self.assertNotEqual(r["price_move"], 0.0)  # still reported, as risk
        # Named for what it is: not a position PnL.
        self.assertEqual(r["kind"], sr.KIND_FUNDING)
        self.assertEqual(s["kind"], sr.KIND_FUNDING)

    def test_fixed_reserve_overrides_gap_sd(self):
        carry, gaps = mock_pair()
        t = T0 + 20 * 86400
        s = sr.score(carry, gaps, t, dict(COSTS, reserve=0.0), price=False)
        self.assertTrue(all(x["reserve"] == 0.0 for x in s["horizons"].values()))

    def test_unpublished_settlement_is_not_used(self):
        # A settlement 30 minutes before t, with a one-hour publication delay, was not
        # yet visible at t: changing it must not change the score.
        carry, gaps = mock_pair()
        t = T0 + 20 * 86400 + 1800
        base = sr.score(carry, gaps, t, COSTS, funding_lag=3600)
        late = [(ts, v * 100 if t - 3600 < ts <= t else v) for ts, v in carry]
        self.assertEqual(base, sr.score(late, gaps, t, COSTS, funding_lag=3600))
        # With no delay the same row would have been used (the check is not vacuous).
        self.assertNotEqual(sr.score(carry, gaps, t, COSTS, funding_lag=0)["carry_1d"],
                            sr.score(late, gaps, t, COSTS, funding_lag=0)["carry_1d"])

    def test_candle_is_usable_only_after_it_closes(self):
        carry, gaps = mock_pair()
        t = T0 + 20 * 86400
        # The candle that STARTED one hour before t closes at t: with close_lag 60 s it
        # is not usable yet; nor is the candle that started at t - 30 min.
        base = sr.score(carry, gaps, t, COSTS)
        open_now = [(ts, 0.9 if ts > t - 3600 - 60 else v) for ts, v in gaps]
        self.assertEqual(base, sr.score(carry, open_now, t, COSTS))

    def test_overlapping_selections_are_one_position_per_asset(self):
        sel = [dict(asset="A", t=T0 + i * 6 * H, h=48) for i in range(10)] +               [dict(asset="B", t=T0, h=24)]
        kept = sr.independent(sel)
        self.assertEqual([x["t"] for x in kept if x["asset"] == "A"],
                         [T0, T0 + 48 * H])
        self.assertEqual(len([x for x in kept if x["asset"] == "B"]), 1)

    def test_carry_orientation(self):
        long_f = [(T0, 1e-5)]
        short_f = [(T0, 3e-5)]
        # Short receives 3e-5, long pays 1e-5.
        self.assertAlmostEqual(sr.carry_series(long_f, short_f)[0][1], 2e-5)
        g = sr.gap_series([(T0, 100.0)], [(T0, 101.0)])
        self.assertAlmostEqual(g[0][1], math.log(1.01))


if __name__ == "__main__":
    unittest.main()
