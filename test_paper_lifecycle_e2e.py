"""PAPER entry -> management -> exit, end to end, on the real decision code.

MOCK TEST DATA. Every venue response here is synthetic (two DEX venues, flat books,
constant funding); the run proves the PAPER chain works, it is never a market result and
is never counted as a real-data PAPER position.

No gate is bypassed: unlike most WidePaper fixtures, the real capability gate
(capabilities.pair_blockers) runs, and the venues pass it only through evidence recorded
by the registry's own methods (CapabilityGateTests.evidence). Hyperliquid pays hourly,
Lighter Core every 8 h in this fixture.
"""
import json
import os
import unittest

import bot
import test_wide_paper as twp
from test_wide_paper import T0, book, row
from wide_paper import WidePaper

EXTRA = 0.0001  # WidePaper.extra_slippage


class PaperLifecycleE2E(unittest.TestCase):
    setUp = twp.CapabilityGateTests.setUp
    tearDown = twp.CapabilityGateTests.tearDown
    fund_history = twp.WidePaperTests.fund_history
    setup_pair = twp.CapabilityGateTests.setup_pair
    evidence = twp.CapabilityGateTests.evidence

    def refresh(self, at):
        self.clock = at
        for v in ("Hyperliquid", "LighterCore"):
            self.src.books[(v, "BTC")] = book(100.0, at)
        self.src.rows = [dict(r, received=at) for r in self.src.rows]

    def test_entry_management_exit_and_net_pnl(self):
        self.setup_pair()
        self.evidence([(v, "BTC") for v in twp.DEX])
        cash0 = self.desk.wallet["cash"]

        # ---- ENTRY: the economic, evidence and risk gates pass on their own.
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        (c,) = [c for c in self.desk.candidates if c["asset"] == "BTC"]
        self.assertNotIn("research", c)
        p = self.desk.positions[0]
        self.assertEqual((p["status"], p["strategy"]), ("OPEN", "funding"))
        legs = {l["side"]: l for l in p["legs"]}
        self.assertEqual((legs["long"]["venue"], legs["short"]["venue"]),
                         ("Hyperliquid", "LighterCore"))
        qty = legs["long"]["qty"]
        self.assertAlmostEqual(legs["short"]["qty"], qty)  # delta neutral in base units
        entry_long, entry_short = 100.01 * (1 + EXTRA), 99.99 * (1 - EXTRA)
        self.assertAlmostEqual(legs["long"]["entry"], entry_long)
        self.assertAlmostEqual(legs["short"]["entry"], entry_short)
        entry_fees = qty * (entry_long * 0.00045 + entry_short * 0.0005)
        self.assertAlmostEqual(p["fees"], entry_fees)
        self.assertGreaterEqual(p["decision"]["net"], self.desk.cfg["wide_min_net"])
        self.assertAlmostEqual(self.desk.wallet["cash"], cash0 - entry_fees)
        pid = p["id"]

        # ---- MANAGEMENT: 8 h of settlements, each venue on its own schedule.
        self.store.mark("LighterCore", "BTC", T0 + 8 * 3600 - 10, 100.0)
        for h in range(1, 9):
            self.store.mark("Hyperliquid", "BTC", T0 + h * 3600 - 10, 100.0)
        for v, per, iv in (("Hyperliquid", -0.0001, 3600), ("LighterCore", 0.0008, 28800)):
            self.src.rates[(v, "BTC")] += [
                dict(ts=t, rate=per, value=None, audit_id=None)
                for t in range(T0 + iv, T0 + 8 * 3600 + 1, iv)
            ]
        self.refresh(T0 + 8 * 3600 + 30)
        self.desk.cache.clear()
        self.desk.cycle(entries=False)
        # Long receives 8 hourly payments on a negative rate; short receives one 8 h one.
        funding = qty * 100 * (8 * 0.0001 + 0.0008)
        self.assertAlmostEqual(p["funding"], funding, places=6)
        self.desk.cache.clear()
        self.desk.cycle(entries=False)
        self.assertAlmostEqual(p["funding"], funding, places=6)  # never paid twice

        # ---- RESTART: the same position comes back; nothing is reopened or re-paid.
        again = WidePaper(self.store, self.src, sleep=lambda s: None)
        self.assertEqual([x["id"] for x in again.positions], [pid])
        again.cycle(entries=False)
        self.assertEqual(len(again.positions), 1)
        self.assertAlmostEqual(again.positions[0]["funding"], funding, places=6)

        # ---- EXIT: the carry flips and stays flipped -> both legs close at the book.
        self.src.rows = [
            row("Hyperliquid", rate=0.001, fee=0.00045, contract_size=1.0, at=self.clock),
            row("LighterCore", rate=-0.0005, interval=28800, fee=0.0005,
                contract_size=1.0, at=self.clock),
        ]
        self.refresh(self.clock + 60)
        again.cycle(entries=False)
        self.refresh(self.clock + 31 * 60)
        again.cycle(entries=False)
        self.assertEqual(again.positions, [])
        (closed,) = again.closed
        self.assertEqual((closed["id"], closed["status"]), (pid, "CLOSED"))
        self.assertEqual(closed["close_reason"], "Funding avantajı tersine döndü")
        exit_long, exit_short = 99.99 * (1 - EXTRA), 100.01 * (1 + EXTRA)
        price_pnl = qty * (exit_long - entry_long) - qty * (exit_short - entry_short)
        exit_fees = qty * (exit_long * 0.00045 + exit_short * 0.0005)
        self.assertAlmostEqual(closed["price_pnl"], price_pnl, places=6)
        self.assertAlmostEqual(closed["fees"], entry_fees + exit_fees, places=6)
        self.assertAlmostEqual(closed["funding"], funding, places=6)
        net = price_pnl + funding - entry_fees - exit_fees
        self.assertAlmostEqual(closed["net"], net, places=6)
        # Every cent of the result is in the paper wallet, once.
        self.assertAlmostEqual(again.wallet["cash"] - cash0, net, places=6)
        # The lifecycle is recorded as a closed trade, not as an open one.
        self.assertTrue(all(l["closed"] for l in closed["legs"]))
        dump = os.environ.get("NB_E2E_EVIDENCE")
        if dump:  # opt-in evidence file for the report; labelled MOCK
            with open(dump, "w", encoding="utf8") as f:
                json.dump(dict(label="MOCK TEST — not a market result", qty=qty,
                               entry=dict(long=entry_long, short=entry_short),
                               exit=dict(long=exit_long, short=exit_short),
                               fees=dict(entry=entry_fees, exit=exit_fees),
                               funding=funding, price_pnl=price_pnl, net=net,
                               wallet_delta=again.wallet["cash"] - cash0,
                               position=closed), f, indent=1, default=str)


if __name__ == "__main__":
    unittest.main()
