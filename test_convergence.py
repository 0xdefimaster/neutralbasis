import unittest
from convergence import qualify, exit_reason


class ConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(min_net=0.25, horizon=24)
        self.c = dict(
            reasons=["Kalıcı pozitif funding avantajı yok"],
            qty=1,
            entry_long=100,
            entry_short=102,
            execution_drag=0.1,
            conservative_funding=-0.1,
            entry_fee=0.05,
            exit_fee=0.05,
            stress_loss=0.1,
        )
        self.h = dict(
            crossings=12,
            mean=0,
            sd=0.0001,
            points=[dict(spread=0.0001 * (-1) ** i) for i in range(168)],
        )

    def test_profitable_convergence_with_negative_funding(self):
        c = qualify(self.c, self.h, self.cfg)
        self.assertTrue(c["eligible"])
        self.assertAlmostEqual(c["net"], 1.65)

    def test_costs_can_consume_entire_gap(self):
        self.c["conservative_funding"] = -3
        self.assertFalse(qualify(self.c, self.h, self.cfg)["eligible"])

    def test_persistent_basis_rejected(self):
        self.h["mean"] = 0.003
        self.assertFalse(qualify(self.c, self.h, self.cfg)["eligible"])

    def test_no_repeated_crossings_rejected(self):
        self.h["crossings"] = 2
        self.assertFalse(qualify(self.c, self.h, self.cfg)["eligible"])

    def test_other_risk_checks_preserved(self):
        self.c["reasons"] = ["Pozisyon sayısı sınırı"]
        self.assertFalse(qualify(self.c, self.h, self.cfg)["eligible"])

    def test_missing_history_rejected(self):
        self.assertFalse(qualify(self.c, None, self.cfg)["eligible"])

    def test_exit_requires_net_after_closing_cost(self):
        p = dict(
            decision=dict(long="Lighter", short="Extended"),
            opened=0,
            legs=[
                dict(side="long", exit_estimate=100),
                dict(side="short", exit_estimate=100.01),
            ],
            net_if_closed=0.1,
        )
        books = {v: dict(received=10) for v in ("Lighter", "Extended")}
        self.assertIsNone(exit_reason(p, books, self.cfg, 10))
        p["net_if_closed"] = 0.3
        self.assertIn("yakınsadı", exit_reason(p, books, self.cfg, 10))
        self.assertIn("azami", exit_reason(p, books, self.cfg, 90000))


if __name__ == "__main__":
    unittest.main()
