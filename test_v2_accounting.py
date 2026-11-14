"""V2 accounting: outcome classes (LEG_RECOVERY is never a win), entry-time economics
kept with each trade, and the dynamic break-even gate of funding entries.

All books, rates and prices here are MOCK fixtures (test_wide_paper helpers), not
market evidence."""
import os
import tempfile
import unittest
from unittest import mock

import bot
import wide_paper
from test_wide_paper import T0, FakeSource, book, candles, row
from wide_paper import (
    OUTCOME_LEG_RECOVERY,
    OUTCOME_MANUAL,
    OUTCOME_RISK,
    OUTCOME_STRATEGY,
    WidePaper,
    funding_economics,
    is_win,
    outcome_of,
    trade_outcome,
)


class OutcomeTests(unittest.TestCase):
    def test_a_trade_with_one_filled_leg_is_a_leg_recovery(self):
        self.assertEqual(trade_outcome("Fiyat farkı normal seviyesine döndü", 1),
                         OUTCOME_LEG_RECOVERY)
        self.assertEqual(
            trade_outcome("Bacak kurtarma: Dolum olmadı: defter ...", 2),
            OUTCOME_LEG_RECOVERY,
        )

    def test_other_classes(self):
        self.assertEqual(trade_outcome("Kullanıcı paper kapatma talebi", 2), OUTCOME_MANUAL)
        self.assertEqual(trade_outcome("Pozisyon kayıp limiti", 2), OUTCOME_RISK)
        self.assertEqual(trade_outcome("Fiyat uyumu bozuldu", 2), OUTCOME_RISK)
        self.assertEqual(trade_outcome("Funding avantajı tersine döndü", 2), OUTCOME_STRATEGY)

    def test_entries_kept_before_outcomes_existed_are_classified(self):
        # Shaped like the 2026-11-11 pilot UNI entry (short leg never filled).
        legacy = dict(net=0.90, long="Extended", short=None,
                      reason="Bacak kurtarma: Dolum olmadı")
        self.assertEqual(outcome_of(legacy), OUTCOME_LEG_RECOVERY)
        self.assertFalse(is_win(legacy))
        clean = dict(net=0.75, venues=["Extended", "LighterCore"],
                     reason="Fark ortalama/kesişim bölgesine geldi")
        self.assertEqual(outcome_of(clean), OUTCOME_STRATEGY)
        self.assertTrue(is_win(clean))


class EconomicsTests(unittest.TestCase):
    def test_breakeven_is_the_pairs_own_payback_time(self):
        e = funding_economics(2.4, 24, 0.5, 0.5, 0.2, 0.0)
        self.assertAlmostEqual(e["hourly_cashflow"], 0.1)
        self.assertAlmostEqual(e["round_trip_cost"], 1.2)
        self.assertAlmostEqual(e["breakeven_hours"], 12.0)
        self.assertAlmostEqual(e["expected_net_cashflow"], 1.2)
        self.assertAlmostEqual(e["exit_cost"], 0.6)

    def test_realization_only_ever_lowers_income(self):
        self.assertAlmostEqual(
            funding_economics(2.4, 24, 0.5, 0.5, 0.2, 0.0, realization=5.0)[
                "expected_funding"], 2.4)
        e = funding_economics(2.4, 24, 0.5, 0.5, 0.2, 0.0, realization=0.5)
        self.assertAlmostEqual(e["breakeven_hours"], 24.0)

    def test_no_income_never_breaks_even(self):
        self.assertIsNone(funding_economics(-1.0, 24, 0.5, 0.5, 0.2, 0.0)["breakeven_hours"])
        why = wide_paper.breakeven_refusal(
            funding_economics(0.0, 24, 0.5, 0.5, 0.2, 0.0),
            wide_paper.WIDE_DEFAULTS)
        self.assertIn("gelir değil", why)


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = bot.Store(os.path.join(self.dir.name, "p.sqlite3"))
        self.addCleanup(self.store.db.close)
        self.clock = T0
        for p in (
            mock.patch.object(bot, "now", lambda: self.clock),
            mock.patch.object(wide_paper, "now", lambda: self.clock),
            mock.patch.object(wide_paper.capabilities, "pair_blockers", lambda *a: []),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.src = FakeSource()
        self.desk = WidePaper(self.store, self.src, sleep=lambda s: None)

    def setup_pair(self, long_rate=-0.0001, short_rate=0.0008):
        # MOCK venues and rates.
        self.src.rows = [
            row("Hyperliquid", rate=long_rate, interval=3600, fee=0.00045),
            row("Binance", rate=short_rate, interval=28800, fee=0.0005),
        ]
        for v, iv, r in (("Hyperliquid", 3600, long_rate), ("Binance", 28800, short_rate)):
            self.src.books[(v, "BTC")] = book(100.0, T0)
            self.src.history[(v, "BTC")] = candles(T0)
            self.src.rates[(v, "BTC")] = [
                dict(ts=t, rate=r, value=None, audit_id=None)
                for t in range(T0 - 30 * 86400 + iv, T0 + 1, iv)
            ]

    def test_funding_entry_keeps_its_economics_and_manual_close_is_classified(self):
        self.setup_pair()
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        p = self.desk.positions[0]
        e = p["entry_economics"]
        self.assertGreater(e["conservative_funding"], 0)
        self.assertLessEqual(e["economics"]["breakeven_hours"], 24)
        self.assertEqual(e["horizon_hours"], 24)
        self.desk.manual_close.add(p["id"])
        self.clock += 60
        self.desk.cycle(entries=False)
        log = self.desk.trade_log()
        self.assertEqual(log[0]["outcome"], OUTCOME_MANUAL)
        self.assertEqual(log[0]["entry_economics"]["economics"],
                         e["economics"])
        r = self.desk.report()["funding"]
        self.assertEqual(r["outcomes"][OUTCOME_MANUAL]["trades"], 1)
        self.assertAlmostEqual(
            r["price_pnl"] + r["settled_funding"] + r["estimated_funding"] - r["fees"],
            r["net"])

    def test_breakeven_gate_refuses_a_slow_payback(self):
        self.setup_pair()
        self.desk.update_config({"wide_max_breakeven_hours": 1})
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        reasons = [r for a in self.desk.assessed for r in a.get("reasons", [])]
        self.assertTrue(any("Başa baş tutma süresi" in r for r in reasons), reasons)

    def test_profitable_single_leg_is_a_leg_recovery_not_a_win(self):
        self.setup_pair()
        original = self.desk.fill

        def fill(v, a, qty, buy, limit, lots, other=None):
            if v == "Binance":
                # MOCK: the long leg's price jumps, then the short leg finds no fill.
                self.src.books[("Hyperliquid", "BTC")] = book(101.0, T0)
                return 0.0, None, self.src.book(v, a)
            return original(v, a, qty, buy, limit, lots, other=other)

        with mock.patch.object(self.desk, "fill", fill):
            self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        log = self.desk.trade_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["outcome"], OUTCOME_LEG_RECOVERY)
        self.assertGreater(log[0]["net"], 0)
        r = self.desk.report()["funding"]
        self.assertEqual(r["wins"], 0)
        self.assertEqual(r["leg_recovery"]["trades"], 1)
        self.assertAlmostEqual(r["two_leg_net"], 0.0)
        pr = self.desk.period_report()
        self.assertEqual((pr["wins"], pr["leg_recovery"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
