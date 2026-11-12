"""V2 phase 7 reliability: characterization tests written BEFORE the fixes.

Each test names the defect it pins down. Books, rates, payments and answers are MOCK
fixtures, not market evidence."""
import collections
import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import wide_paper
from v2 import economics
from v2.hybrid_desk import HybridDesk

CFG = dict(wide_paper.WIDE_DEFAULTS)
T0 = 1_800_000_000.0


def bk(mid, depth=100.0):
    return dict(bids=[(mid - 0.01, depth)], asks=[(mid + 0.01, depth)])


class Provider:
    def __init__(self):
        self.books = {("Arcus", "BTC"): bk(100.0), ("Lighter", "BTC"): bk(101.0)}
        self.rows = {("Arcus", "BTC"): dict(fee=0.0005, rate=0.0, exchange_id="arcus"),
                     ("Lighter", "BTC"): dict(fee=0.0004, rate=0.0001,
                                              exchange_id="lighter_rh")}
        self.paid = {}
        self.fail = set()

    def rows_by(self):
        return self.rows

    def book(self, v, a):
        if (v, a) in self.fail:
            raise ConnectionError("venue unreachable")
        return self.books.get((v, a))

    def settled(self, v, a, at):
        return self.paid.get((v, a), [])


EV = dict(baseline=0.0, mad=0.001, deviation=0.00995, funding=0.5, basis=5.0, net=3.0)
C = dict(asset="BTC", long="Arcus", short="Lighter")


class HybridRecoveryDefects(unittest.TestCase):
    """Task 1: a position with an open quantity must never read as recovered/closed."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.pv = Provider()
        self.t = [T0]

    def desk(self):
        return HybridDesk(os.path.join(self.dir.name, "h.json"), self.pv, CFG,
                          clock=lambda: self.t[0])

    def test_recovery_on_a_thin_book_stays_pending_with_its_residual(self):
        d = self.desk()
        self.pv.books[("Lighter", "BTC")] = dict(bids=[(50.0, 1.0)], asks=[(150.0, 1.0)])
        # The long can be opened (asks deep) but only 1 unit can be sold back.
        self.pv.books[("Arcus", "BTC")] = dict(bids=[(99.99, 1.0)], asks=[(100.01, 100.0)])
        p = d.open(C, EV, 1000.0)
        self.assertNotEqual(p["state"], "LEG_RECOVERY")
        self.assertGreater(p["legs"]["long"]["qty"], 0)
        self.assertIn(p, d.open_positions())
        self.assertGreater(abs(d.exposure(p)), 0)
        self.pv.books[("Arcus", "BTC")] = bk(100.0)
        d.cycle()
        self.assertEqual(p["state"], "LEG_RECOVERY")
        self.assertAlmostEqual(p["legs"]["long"]["qty"], 0.0)
        self.assertEqual(d.exposure(p), 0.0)

    def test_unwind_without_a_book_keeps_the_exposure_instead_of_crashing(self):
        d = self.desk()
        self.pv.books[("Lighter", "BTC")] = bk(101.0, depth=4.0)
        real = self.pv.book
        calls = collections.Counter()

        def book(v, a):
            calls[v] += 1
            if v == "Arcus" and calls[v] >= 2:   # the unwind read fails
                return None
            return real(v, a)

        self.pv.book = book
        p = d.open(C, EV, 1000.0)
        self.assertEqual(p["state"], "PARTIAL_EXPOSURE")
        self.assertAlmostEqual(d.exposure(p), p["legs"]["long"]["qty"] - p["legs"]["short"]["qty"])
        self.assertGreater(d.exposure(p), 0)
        self.pv.book = real
        d.cycle()
        self.assertEqual(p["state"], "HEDGED_OPEN")
        self.assertAlmostEqual(d.exposure(p), 0.0)

    def test_book_errors_do_not_break_the_cycle_or_close_anything(self):
        d = self.desk()
        p = d.open(C, EV, 1000.0)
        self.pv.fail.add(("Lighter", "BTC"))
        d.close(p, ["MAX_HOLD"])
        d.cycle()
        self.assertEqual(p["state"], "EXIT_PENDING")
        self.assertTrue(p["legs"]["long"]["open"] or p["legs"]["short"]["open"])

    def test_uneven_close_records_the_imbalance(self):
        d = self.desk()
        p = d.open(C, EV, 1000.0)
        q = p["legs"]["long"]["qty"]
        self.pv.books[("Lighter", "BTC")] = dict(bids=[(100.99, 100.0)],
                                                 asks=[(101.01, q / 2)])
        d.close(p, ["MAX_HOLD"])
        self.assertEqual(p["state"], "EXIT_PENDING")
        self.assertAlmostEqual(d.exposure(p), 0.0)   # never one leg closed alone

    def test_restart_keeps_a_pending_recovery(self):
        d = self.desk()
        self.pv.books[("Lighter", "BTC")] = dict(bids=[(50.0, 1.0)], asks=[(150.0, 1.0)])
        self.pv.books[("Arcus", "BTC")] = dict(bids=[(99.99, 1.0)], asks=[(100.01, 100.0)])
        p = d.open(C, EV, 1000.0)
        d2 = self.desk()
        self.assertEqual(d2.positions[p["id"]]["state"], p["state"])
        self.assertTrue(d2.risk())               # open exposure is reported, not hidden


class FundingAccountingDefects(unittest.TestCase):
    """Task 3: entry-price valuation is an estimate; distinct payments stay distinct."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.pv = Provider()
        self.t = [T0]
        self.d = HybridDesk(os.path.join(self.dir.name, "h.json"), self.pv, CFG,
                            clock=lambda: self.t[0])
        self.p = self.d.open(C, EV, 1000.0)
        self.t[0] += 7200

    def test_entry_price_valuation_is_estimated_not_realized(self):
        self.pv.paid[("Lighter", "BTC")] = [dict(ts=T0 + 3600, rate=0.0001)]
        self.d.settle(self.p)
        kinds = [e["kind"] for e in self.p["ledger"] if "funding" in e["kind"]]
        self.assertEqual(kinds, ["funding_estimated"])
        pn = HybridDesk.pnl(self.p)
        self.assertEqual(pn["funding"], 0.0)
        self.assertGreater(pn["funding_estimated"], 0)

    def test_payment_with_its_settlement_price_is_realized(self):
        self.pv.paid[("Lighter", "BTC")] = [dict(ts=T0 + 3600, rate=0.0001, price=100.5,
                                                 price_basis="index")]
        self.d.settle(self.p)
        e = [e for e in self.p["ledger"] if "funding" in e["kind"]][0]
        self.assertEqual(e["kind"], "funding")
        self.assertAlmostEqual(e["amount"], self.p["legs"]["short"]["qty"] * 100.5 * 0.0001)

    def test_payments_after_close_use_the_size_held_at_payment_time(self):
        self.pv.paid[("Lighter", "BTC")] = [dict(ts=T0 + 3600, rate=0.0001)]
        q = self.p["legs"]["short"]["qty"]
        self.d.close(self.p, ["MAX_HOLD"])          # closes at T0+7200, then settles
        e = [e for e in self.p["ledger"] if "funding" in e["kind"]]
        self.assertEqual(len(e), 1)
        self.assertAlmostEqual(e[0]["units"], q)
        self.assertNotEqual(e[0]["amount"], 0.0)

    def test_distinct_payments_at_the_same_second_are_not_merged(self):
        self.pv.paid[("Lighter", "BTC")] = [dict(ts=T0 + 3600, rate=0.0001),
                                            dict(ts=T0 + 3600, rate=0.0002),
                                            dict(ts=T0 + 3600, rate=0.0001)]
        self.d.settle(self.p)
        self.assertEqual(sum(1 for e in self.p["ledger"] if "funding" in e["kind"]), 2)


class RisexSettlementPriceTests(unittest.TestCase):
    def test_risex_rows_carry_their_settlement_index_price(self):
        from v2.venues import risex

        rows = risex.parse_funding_history({"data": {"records": [
            {"funding_rate": "-0.0000038", "index_price": "74012.77",
             "start_time": "1794441600000000000", "end_time": "1794445200000000000"}]}})
        self.assertEqual(rows[0]["price"], 74012.77)
        self.assertTrue(rows[0]["price_basis"].startswith("index"))


class CostDefects(unittest.TestCase):
    """Task 4: entry and exit apart, exit not guaranteed, stress, no RFQ fills."""

    def test_entry_exit_apart_and_stress_scenarios(self):
        c, why = economics.round_trip(bk(100.0), bk(100.0), 1000.0, 0.0005, 0.0005)
        self.assertIsNone(why)
        self.assertIn("entry_cost", c)
        self.assertIn("exit_cost", c)
        self.assertEqual(c["exit_basis"], "current book (not guaranteed at exit)")
        s = economics.stress(c, funding=2.0, basis=1.0, reserve=0.2)
        names = [x["name"] for x in s]
        for n in ("base", "exit_spread_x3", "slippage_+5bp", "funding_-50%",
                  "second_leg_half_filled"):
            self.assertIn(n, names)
        base = next(x for x in s if x["name"] == "base")["net"]
        self.assertTrue(all(x["net"] <= base + 1e-9 for x in s))

    def test_rfq_quote_is_not_a_fill(self):
        rfq = dict(bk(100.0), model="rfq_indicative")
        c, why = economics.round_trip(rfq, bk(100.0), 1000.0, 0.0, 0.0005)
        self.assertEqual(why, "EXECUTION_MODEL_UNVERIFIED")


class RecorderDefects(unittest.TestCase):
    """Task 2: raw answers and API errors recorded with provenance; replay refuses
    corrupt or missing segments."""

    def test_collector_records_raw_answers_and_errors(self):
        from v2.collector import Collector

        seen = []

        def transport(xid, method, url, body, headers=None):
            if "fail" in url:
                raise IOError("HTTP 500")
            return {"ok": 1}

        col = Collector(transport=transport, clock=lambda: T0)
        col.sink = lambda **kw: seen.append(kw)
        col.call("arcus", ("GET", "https://api.arcus.xyz/v1/markets", None))
        with self.assertRaises(IOError):
            col.call("arcus", ("GET", "https://api.arcus.xyz/v1/fail", None))
        self.assertEqual([x["kind"] for x in seen], ["raw", "api_error"])
        self.assertEqual(seen[0]["exchange_id"], "arcus")
        self.assertIn("url", seen[0]["source"])

    def test_replay_refuses_corrupt_and_missing_segments(self):
        from v2.recorder import Recorder
        from v2 import replay

        with tempfile.TemporaryDirectory() as d:
            r = Recorder(d, lambda: {("Arcus", "BTC")}, segment_bytes=100,
                         clock=lambda: T0)
            for i in range(6):
                r.record("book", "Arcus", "BTC", {"i": i})
            r.close()
            ok = replay.verify(d)
            self.assertTrue(ok["ok"], ok)
            seg = sorted(Path(d).glob("segments/*/*.jsonl.gz"))
            seg[0].write_bytes(b"not gzip")
            seg[1].unlink()
            bad = replay.verify(d)
            self.assertFalse(bad["ok"])
            self.assertEqual(len(bad["corrupt"]), 1)
            self.assertEqual(len(bad["missing"]), 1)
            with self.assertRaises(replay.ReplayError):
                list(replay.iterate(d))


class AtomicWriteTests(unittest.TestCase):
    def test_transient_windows_lock_is_retried_and_a_permanent_one_raises(self):
        from v2 import ops

        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "s.json"
            target.write_text("old")
            n = [0]

            def flaky(a, b):
                n[0] += 1
                if n[0] < 3:
                    raise PermissionError("WinError 5")
                os.replace(a, b)

            self.assertEqual(ops.atomic_write(target, "new", replace=flaky,
                                              sleep=lambda s: None), 2)
            self.assertEqual(target.read_text(), "new")

            def locked(a, b):
                raise PermissionError("WinError 5")

            with self.assertRaises(PermissionError):
                ops.atomic_write(target, "newer", tries=3, replace=locked,
                                 sleep=lambda s: None)
            self.assertEqual(target.read_text(), "new")   # never half written


class LaunchDefects(unittest.TestCase):
    """Task 7: no live PAPER start without a healthy disk, room and a backup."""

    def test_launch_check(self):
        from v2 import ops

        du = lambda free: (lambda p: collections.namedtuple("u", "total used free")(
            1, 1, free))
        with tempfile.TemporaryDirectory() as d:
            ok, why = ops.launch_check(d, disk_usage=du(12 * 1024 ** 3))
            self.assertFalse(ok)
            self.assertTrue(any("free" in w for w in why))
            self.assertTrue(any("backup" in w for w in why))
            ok, why = ops.launch_check("E:/x", disk_usage=du(10 ** 13))
            self.assertFalse(ok)
            ok, why = ops.launch_check("D:/x", disk_usage=du(10 ** 13))
            self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()


class StrategyEndToEnd(unittest.TestCase):
    """Task 5: funding and convergence through the paper desk with the V2 gate attached
    (open, track, the strategy's own close, a risk stop, restart, ledger reconcile).
    The hybrid's are in HybridRecoveryDefects / test_v2_phase5.HybridDeskTests."""

    def helper(self):
        import test_wide_paper as tw

        h = tw.WidePaperTests("test_payment_counts_respect_each_venue_interval")
        h.setUp()
        self.addCleanup(h.doCleanups)
        self.addCleanup(h.tearDown)
        h.desk.v2_gate = lambda c, out: []      # the V2 gate passes these MOCK pairs
        return h, tw

    def reconcile(self, h):
        if h.desk.ledger.ready():
            self.assertEqual(h.desk.ledger.reconcile(), [])

    def test_gate_reason_blocks_the_entry(self):
        h, tw = self.helper()
        h.setup_pair()
        h.desk.v2_gate = lambda c, out: ["V2 FEE_UNKNOWN: mock"]
        h.desk.cycle()
        self.assertEqual(h.desk.positions, [])

    def test_funding_open_track_flip_close_restart(self):
        h, tw = self.helper()
        h.setup_pair()
        h.desk.cycle()
        self.assertEqual(len(h.desk.positions), 1)
        p = h.desk.positions[0]
        legs = {l["side"]: l["qty"] for l in p["legs"]}
        self.assertAlmostEqual(legs["long"], legs["short"])     # hedged
        again = tw.WidePaper(h.store, h.src, sleep=lambda s: None)   # restart
        again.v2_gate = h.desk.v2_gate
        self.assertEqual(again.positions[0]["id"], p["id"])
        h.clock = tw.T0 + 60
        h.src.rows = [dict(tw.row("Hyperliquid", rate=0.001, at=h.clock)),
                      dict(tw.row("Binance", rate=-0.0005, interval=28800, at=h.clock))]
        for v in ("Hyperliquid", "Binance"):
            h.src.books[(v, "BTC")] = tw.book(100.0, h.clock)
        again.cycle(entries=False)
        h.clock = tw.T0 + 60 + 31 * 60
        h.src.rows = [dict(r, received=h.clock) for r in h.src.rows]
        for v in ("Hyperliquid", "Binance"):
            h.src.books[(v, "BTC")] = tw.book(100.0, h.clock)
        again.cycle(entries=False)
        self.assertEqual(again.positions, [])
        t = again.trade_log()[0]
        self.assertEqual(t["outcome"], "STRATEGY_EXIT")
        self.assertAlmostEqual(t["net"], t["price_pnl"] + t["funding"] - t["fees"])
        h.desk = again
        self.reconcile(h)

    def test_funding_risk_stop(self):
        h, tw = self.helper()
        h.setup_pair()
        h.desk.cycle()
        self.assertEqual(len(h.desk.positions), 1)
        h.desk.update_config({"wide_risk": 0.01})
        h.clock = tw.T0 + 60
        h.src.books[("Hyperliquid", "BTC")] = tw.book(97.0, h.clock)   # long leg falls
        h.src.books[("Binance", "BTC")] = tw.book(100.0, h.clock)
        h.src.rows = [dict(r, received=h.clock) for r in h.src.rows]
        h.desk.cycle(entries=False)
        self.assertEqual(h.desk.positions, [])
        self.assertEqual(h.desk.trade_log()[0]["outcome"], "RISK_EXIT")
        self.reconcile(h)

    def test_convergence_open_close_on_cross_and_restart(self):
        h, tw = self.helper()
        h.setup_convergence()
        h.desk.cycle()
        self.assertEqual(len(h.desk.positions), 1)
        self.assertEqual(h.desk.positions[0]["strategy"], "convergence")
        again = tw.WidePaper(h.store, h.src, sleep=lambda s: None)
        again.v2_gate = h.desk.v2_gate
        h.clock = tw.T0 + 3600
        h.src.rows = [dict(r, received=h.clock) for r in h.src.rows]
        for v in ("Hyperliquid", "Binance"):
            h.src.books[(v, "BTC")] = tw.book(100.0, h.clock)
        again.cycle(entries=False)
        self.assertEqual(again.positions, [])
        t = again.trade_log()[0]
        self.assertEqual(t["outcome"], "STRATEGY_EXIT")
        self.assertGreater(t["net"], 0)
        h.desk = again
        self.reconcile(h)


class AcceleratedStress(unittest.TestCase):
    """Task 6: OFFLINE ACCELERATED STRESS (MOCK market, simulated clock). A shorter run
    than the 72 h tool run; the same faults in compressed windows."""

    def test_24h_with_faults(self):
        from tools import v2_stress

        faults = dict(outage=[(2, 3)], rate_limit=[(4, 4.5)], stale=[(6, 7)],
                      delist=[(8, 10)], disk_full=[(11, 12)], wal=[(13, 14)], crash=[16])
        r = v2_stress.run(hours=24, step=300, faults=faults)
        self.assertIn("OFFLINE ACCELERATED STRESS", r["label"])
        self.assertEqual(r["exceptions_escaped"], [])
        self.assertEqual(r["crashes"], 1)
        self.assertEqual(r["rounds_failed"], 18)            # 12 outage + 6 rate-limit
        self.assertGreater(r["entries_blocked_by_safety"], 0)
        self.assertGreater(r["recorder"]["refused_disk"], 0)
        self.assertGreater(r["recorder"]["gaps"], 0)
        self.assertTrue(r["ledger_reconciled"])
        self.assertEqual(r["open_exposure"], [])
        self.assertTrue(r["replay_ok"])


class LaunchWiring(unittest.TestCase):
    def test_run_pilot_refuses_without_launch_evidence_under_v2(self):
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, NB_V2="1", NB_PILOT_DATA=d, NB_PILOT_PORT="18799")
            for k in ("NB_EXTRA_PERP_DEXES", "NB_RFQ_STREAMS"):
                env.pop(k, None)
            p = subprocess.run([sys.executable, "pilot/run_pilot.py"], env=env,
                               capture_output=True, text=True, timeout=120,
                               cwd=str(Path(__file__).resolve().parent))
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("launch check", (p.stdout + p.stderr).lower())
