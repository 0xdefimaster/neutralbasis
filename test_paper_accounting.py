"""Paper accounting separation (PR #54 stage 3): payment statuses, realized /
estimated / uncertainty results, a ledger that never shows an unsettled payment as a
realized zero, and settled-only reconciliation. The wallet, the position net and the
limits are unchanged. MOCK fixture data only (test_wide_paper's FakeSource)."""

import json
import unittest

import bot
import test_wide_paper as twp
from ledger import (
    NOT_RECORDED,
    SETTLED,
    SETTLED_ESTIMATED_PRICE,
    UNKNOWN,
    payment_status,
)
from test_wide_paper import FUNDING_WAIT, T0, book

H = 3600


class Fixture(unittest.TestCase):
    tearDown = twp.WidePaperTests.tearDown
    setup_pair = twp.WidePaperTests.setup_pair
    fund_history = twp.WidePaperTests.fund_history

    def setUp(self):
        twp.WidePaperTests.setUp(self)

    def open(self):
        # The long (Hyperliquid) leg pays 0.0001 an hour: an unsettled hour can cost.
        self.setup_pair(long_rate=0.0001, short_rate=0.003)
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        return self.desk.positions[0]

    def publish(self, hours, rate=0.0001, mark_hours=()):
        """Hyperliquid publishes `hours`; marks exist just before `mark_hours`."""
        for h in mark_hours:
            self.store.mark("Hyperliquid", "BTC", T0 + h * H - 10, 100.0)
        self.src.rates[("Hyperliquid", "BTC")] += [
            dict(ts=T0 + h * H, rate=rate, value=None, audit_id=None) for h in hours
        ]

    def run_at(self, t):
        self.clock = t
        self.desk.cache.clear()
        for v in ("Hyperliquid", "Binance"):
            self.src.books[(v, "BTC")] = book(100.0, self.clock)
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        self.desk.cycle(entries=False)

    def payments(self, p, venue=None):
        rows = self.store.db.execute(
            "select key, body from payments where key like ?", (p["id"] + ":%",)
        ).fetchall()
        return {
            k: json.loads(b)
            for k, b in rows
            if venue is None or (":" + venue + ":") in k
        }

    def statuses(self, p, venue=None):
        out = {}
        for b in self.payments(p, venue).values():
            out[payment_status(b)] = out.get(payment_status(b), 0) + 1
        return out

    def events(self, p):
        return self.store.db.execute(
            "select event_key, kind, amount from ledger_events where position=?"
            " order by id",
            (p["id"],),
        ).fetchall()


class StatusTests(Fixture):
    def test_each_payment_has_its_status_and_only_settled_is_realized(self):
        p = self.open()
        cash0 = self.desk.wallet["cash"]
        # Hours 1-4 have a mark (SETTLED); 5-7 none (SETTLED_ESTIMATED_PRICE); hour 8
        # and Binance's 8-hour payment are never published (NOT_RECORDED).
        self.publish(range(1, 8), mark_hours=range(1, 5))
        self.run_at(T0 + 8 * H + FUNDING_WAIT + 60)
        self.assertEqual(
            self.statuses(p, "Hyperliquid"),
            {SETTLED: 4, SETTLED_ESTIMATED_PRICE: 3, NOT_RECORDED: 1},
        )
        self.assertEqual(self.statuses(p, "Binance"), {NOT_RECORDED: 1})
        bodies = self.payments(p)
        settled = sum(
            b["cash"] for b in bodies.values() if payment_status(b) == SETTLED
        )
        estimated = sum(
            b["cash"]
            for b in bodies.values()
            if payment_status(b) == SETTLED_ESTIMATED_PRICE
        )
        self.assertLess(settled, 0)  # the long leg pays
        a = self.desk.accounting(p, open_position=True)
        self.assertAlmostEqual(a["realized_pnl"], p["price_pnl"] + settled - p["fees"])
        self.assertAlmostEqual(
            a["estimated_pnl"], a["realized_pnl"] + estimated + p["unrealized"]
        )
        self.assertLess(a["uncertainty_risk"], 0)  # the paying leg's missing hour
        self.assertEqual((a["not_recorded"], a["unknown"]), (2, 0))
        # Unchanged: the wallet and the position's funding still hold every cash
        # payment (settled and estimated), and an unsettled one adds nothing.
        self.assertAlmostEqual(p["funding"], settled + estimated)
        self.assertAlmostEqual(self.desk.wallet["cash"], cash0 + settled + estimated)

    def test_the_ledger_never_shows_an_unsettled_payment_as_a_realized_zero(self):
        p = self.open()
        self.publish(range(1, 8), mark_hours=range(1, 8))
        self.run_at(T0 + 8 * H + FUNDING_WAIT + 60)
        kinds = {}
        for key, kind, amount in self.events(p):
            kinds.setdefault(kind, []).append(amount)
        self.assertEqual(len(kinds["funding"]), 7)
        self.assertTrue(all(a is None for a in kinds["funding_not_recorded"]))
        self.assertNotIn(0.0, [a for v in kinds.values() for a in v])


class UnknownTests(Fixture):
    def failing_history(self, venue="Hyperliquid"):
        real = self.desk.funding_rows

        def rows(v, a, start):
            if v == venue and self.history_down:
                raise ValueError("geçmiş okunamadı")
            return real(v, a, start)

        self.history_down = True
        self.desk.funding_rows = rows

    def test_payments_due_while_the_history_cannot_be_read_are_unknown(self):
        p = self.open()
        self.failing_history()
        cash0 = self.desk.wallet["cash"]
        self.run_at(T0 + 4 * H + FUNDING_WAIT + 60)
        self.assertEqual(self.statuses(p, "Hyperliquid"), {UNKNOWN: 4})
        self.assertEqual(len(p["funding_unknown"]), 4)
        self.assertTrue(all(x["risk"] < 0 for x in p["funding_unknown"]))
        self.assertTrue(any("okunamadı" in x for x in p["pending_funding"]))
        self.assertEqual(self.desk.wallet["cash"], cash0)  # no cash moves
        kinds = [k for _, k, a in self.events(p) if k == "funding_unknown"]
        self.assertEqual(len(kinds), 4)
        a = self.desk.accounting(p, open_position=True)
        self.assertEqual(a["unknown"], 4)
        self.assertLess(a["uncertainty_risk"], 0)
        # Another round while it is still down adds nothing twice.
        self.run_at(T0 + 4 * H + FUNDING_WAIT + 120)
        self.assertEqual(self.statuses(p, "Hyperliquid"), {UNKNOWN: 4})
        self.assertEqual(len(p["funding_unknown"]), 4)

    def test_unknown_becomes_settled_or_not_recorded_when_readable_again(self):
        p = self.open()
        self.failing_history()
        self.run_at(T0 + 4 * H + FUNDING_WAIT + 60)
        cash0 = self.desk.wallet["cash"]
        # Back online: hours 1-2 are published (late), 3-4 never were.
        self.history_down = False
        self.publish((1, 2), mark_hours=(1, 2))
        self.run_at(T0 + 4 * H + FUNDING_WAIT + 120)
        self.assertEqual(self.statuses(p, "Hyperliquid"), {SETTLED: 2, NOT_RECORDED: 2})
        self.assertEqual(p.get("funding_unknown"), [])
        self.assertEqual(len(p["funding_unverified"]), 2)
        late = [a for k, kind, a in self.events(p) if kind == "funding_late"]
        self.assertEqual(len(late), 2)
        self.assertLess(self.desk.wallet["cash"], cash0)  # the paid hours, now settled
        # Repeating changes nothing.
        before = (self.statuses(p), len(self.events(p)), self.desk.wallet["cash"])
        self.run_at(T0 + 4 * H + FUNDING_WAIT + 180)
        self.assertEqual(
            (self.statuses(p), len(self.events(p)), self.desk.wallet["cash"]), before
        )


class ReconcileAndReportTests(Fixture):
    def close_with_mixed_payments(self):
        p = self.open()
        self.publish(range(1, 8), mark_hours=range(1, 4))
        self.run_at(T0 + 8 * H + FUNDING_WAIT + 60)
        self.desk.close(p, "Kullanıcı paper kapatma talebi")
        return p

    def test_reconciliation_uses_settled_payments_only(self):
        p = self.close_with_mixed_payments()
        self.assertGreater(p["estimated_funding"] ** 2, 0)
        self.assertEqual(self.desk.ledger.reconcile(), [])
        # The realized result is the net minus the estimated funding.
        entry = self.desk.ledger_entry(p)
        self.assertAlmostEqual(entry["realized_pnl"], p["net"] - p["estimated_funding"])
        check = {c["label"]: c for c in self.desk.period_report()["checks"]}
        self.assertTrue(check["Muhasebe defteri işlemlerle tutarlı"]["ok"])

    def test_the_period_report_keeps_the_three_results_apart(self):
        p = self.close_with_mixed_payments()
        acc = self.desk.period_report()["accounting"]
        entry = self.desk.ledger_entry(p)
        self.assertAlmostEqual(acc["realized_pnl"], entry["realized_pnl"])
        self.assertAlmostEqual(acc["estimated_pnl"], p["net"])
        self.assertLess(acc["uncertainty_risk"], 0)
        self.assertEqual(
            acc["payments"],
            {SETTLED: 3, SETTLED_ESTIMATED_PRICE: 4, NOT_RECORDED: 2},
        )

    def test_records_from_before_the_statuses_are_read_from_their_flags(self):
        self.assertEqual(payment_status(dict(cash=0.0, unverified=True)), NOT_RECORDED)
        self.assertEqual(
            payment_status(dict(cash=1.0, valuation_estimated=True)),
            SETTLED_ESTIMATED_PRICE,
        )
        self.assertEqual(payment_status(dict(cash=1.0)), SETTLED)
        # An older ledger entry without realized_pnl still reports a realized result.
        old = dict(net=5.0, estimated_funding=2.0, unverified_risk=-0.5)
        acc = self.desk.period_accounting([dict(old, id="x")], [])
        self.assertEqual(
            (acc["realized_pnl"], acc["estimated_pnl"], acc["uncertainty_risk"]),
            (3.0, 5.0, -0.5),
        )

    def test_old_ledger_events_reconcile_under_the_new_rule(self):
        p = self.close_with_mixed_payments()
        db = self.store.db
        # Rewrite this trade's events the way the old code wrote them: every payment
        # as kind "funding", an unsettled one with amount 0.
        db.execute(
            "update ledger_events set kind='funding', amount=coalesce(amount, 0)"
            " where position=? and kind like 'funding%'",
            (p["id"],),
        )
        db.commit()
        self.assertEqual(self.desk.ledger.reconcile(), [])


class UnchangedBehaviourTests(Fixture):
    def test_net_wallet_and_limits_follow_the_old_formulas(self):
        p = self.open()
        self.publish(range(1, 8), mark_hours=range(1, 5))
        self.run_at(T0 + 8 * H + FUNDING_WAIT + 60)
        self.desk.close(p, "Kullanıcı paper kapatma talebi")
        # The position net is price + all cash funding - fees, as before.
        self.assertAlmostEqual(p["net"], p["price_pnl"] + p["funding"] - p["fees"])
        # The daily PnL (the loss limit's input) is still wallet equity based.
        self.assertAlmostEqual(
            self.desk.daily_pnl(),
            self.desk.wallet["cash"] - self.store.get("wide_daily", {})["equity"],
        )


if __name__ == "__main__":
    unittest.main()
