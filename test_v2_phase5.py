"""V2 phases 5-6: market-data integrity, recorder, long-run safety, the hybrid paper
desk end to end, the runtime loop and the dashboard.

All books, rates, frames and answers here are MOCK fixtures, not market evidence."""
import collections
import gzip
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import wide_paper
from v2 import dashboard, marketdata, ops
from v2.hybrid_desk import (CLOSED, EXIT_PENDING, FAILED, HEDGED_OPEN, LEG_RECOVERY,
                            OPENING, HybridDesk)
from v2.recorder import Recorder
from v2.runtime import Runtime

CFG = dict(wide_paper.WIDE_DEFAULTS)
T0 = 1_800_000_000.0


class MarketDataTests(unittest.TestCase):
    def test_backoff_grows_caps_and_resets(self):
        b = marketdata.Backoff(1.0, 8.0, rand=lambda: 1.0)
        self.assertEqual([b.next() for _ in range(5)], [1.0, 2.0, 4.0, 8.0, 8.0])
        b.reset()
        self.assertEqual(b.next(), 1.0)

    def lv(self, bids, asks):
        return [[{"px": str(p), "sz": str(q)} for p, q in bids],
                [{"px": str(p), "sz": str(q)} for p, q in asks]]

    def test_seq_book_snapshot_delta_gap_and_crossed(self):
        b = marketdata.SeqBook()
        self.assertEqual(b.apply(dict(type="delta", seq=1, prevSeq=0,
                                      levels=self.lv([], []))), "resync")
        self.assertEqual(b.apply(dict(type="snapshot", seq=10, levels=self.lv(
            [(99, 1), (98, 2)], [(101, 1)]))), "ok")
        self.assertEqual(b.apply(dict(type="delta", seq=11, prevSeq=10, levels=self.lv(
            [(98, 0), (100, 3)], []))), "ok")
        self.assertEqual(sorted(b.bids), [99.0, 100.0])
        self.assertEqual(b.apply(dict(type="delta", seq=11, prevSeq=10,
                                      levels=self.lv([], []))), "ignored")
        self.assertEqual(b.apply(dict(type="delta", seq=13, prevSeq=12,
                                      levels=self.lv([], []))), "resync")  # gap
        self.assertFalse(b.valid)
        b.apply(dict(type="snapshot", seq=20, levels=self.lv([(99, 1)], [(101, 1)])))
        self.assertEqual(b.apply(dict(type="delta", seq=21, prevSeq=20, levels=self.lv(
            [(102, 1)], []))), "resync")  # crossed

    def test_txflow_stream_resubscribes_pings_and_reconnects(self):
        t = [T0]
        sent, frames = [], collections.deque()

        class Conn:
            def send(self, op, payload):
                sent.append(json.loads(payload))

            def recv(self):
                if not frames:
                    raise ConnectionError("lost")
                return frames.popleft()

            def close(self):
                pass

        snap = {"coin": "1", "type": "snapshot", "seq": 5, "prevSeq": None, "time": 1,
                "levels": [[{"px": "99", "sz": "1"}], [{"px": "101", "sz": "1"}]]}
        gap = {"coin": "1", "type": "delta", "seq": 9, "prevSeq": 8, "time": 2,
               "levels": [[], []]}
        frames.extend([json.dumps({"data": snap}), json.dumps({"data": gap})])
        s = marketdata.TxFlowDepthStream(["1"], lambda url: Conn(), clock=lambda: t[0],
                                         sleep=lambda x: None)
        self.assertEqual(s.step(), "ok")
        self.assertIsNotNone(s.book("1"))
        self.assertEqual(s.step(), "resync")
        self.assertEqual([m["method"] for m in sent[-2:]], ["unsubscribe", "subscribe"])
        self.assertIsNone(s.book("1"))          # invalid until a fresh snapshot
        t[0] += 11
        self.assertEqual(s.step(), "error")     # connection lost: closed, backed off
        self.assertIn({"method": "ping"}, sent)
        frames.append(json.dumps({"data": snap}))
        self.assertEqual(s.step(), "ok")        # reconnected and resubscribed
        self.assertEqual(s.stats["connects"], 2)
        self.assertEqual(sent[0]["subscription"], {"type": "l2BookDepth", "coin": "1"})

    def test_poll_worker_backs_off_marks_stale_and_drops_unwanted(self):
        t = [T0]
        wanted = {("Arcus", "BTC"), ("Arcus", "ETH")}

        def fetch(v, a):
            if a == "ETH":
                raise IOError("down")
            return dict(bids=[(1, 1)], asks=[(2, 1)], received=t[0])

        w = marketdata.PollWorker("Arcus", "arcus", lambda: wanted, fetch,
                                  clock=lambda: t[0], sleep=lambda x: None)
        w.poll_once()
        h = w.health()
        self.assertEqual((h["fresh"], h["failing"]), (1, 1))
        t[0] += 60
        self.assertEqual(w.health()["stale"], 1)
        wanted.discard(("Arcus", "BTC"))
        w.poll_once()
        self.assertEqual(w.health()["markets"], 0)

    def test_stale_limits(self):
        self.assertTrue(marketdata.stale(None, "arcus", T0))
        self.assertFalse(marketdata.stale(dict(received=T0 - 5), "arcus", T0))
        self.assertTrue(marketdata.stale(dict(received=T0 - 16), "arcus", T0))


class StreamHookTests(unittest.TestCase):
    def test_collector_prefers_a_fresh_stream_book(self):
        from v2.collector import Collector

        col = Collector(transport=lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
                        clock=lambda: T0)
        col.by_key = {("TxFlow", "BTC"): dict(exchange_id="txflow", market_id="1",
                                              multiplier=1.0)}

        class S:
            def __init__(self, age):
                self.age = age

            def book(self, coin):
                return dict(bids=[(99.0, 1.0)], asks=[(101.0, 1.0)], received=T0 - self.age,
                            model="orderbook")

        col.streams["txflow"] = S(1)
        self.assertEqual(col.book("TxFlow", "BTC")["bids"][0], (99.0, 1.0))
        col.streams["txflow"] = S(60)   # stale stream: falls back to REST (here: refused)
        with self.assertRaises(AssertionError):
            col.book("TxFlow", "BTC")


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.t = [T0]
        self.free = [10 ** 12]
        self.wanted = {("Arcus", "BTC")}

    def rec(self, **kw):
        du = lambda p: collections.namedtuple("u", "total used free")(1, 1, self.free[0])
        return Recorder(self.dir.name, lambda: self.wanted, clock=lambda: self.t[0],
                        disk_usage=du, **kw)

    def test_only_wanted_markets_are_written_and_segments_are_sealed_with_hashes(self):
        r = self.rec(segment_bytes=200)
        self.assertFalse(r.record("book", "TxFlow", "SOLO", {}))
        for i in range(5):
            self.assertTrue(r.record("book", "Arcus", "BTC", {"i": i}))
        r.close()
        m = [json.loads(x) for x in (Path(self.dir.name) / "manifest.jsonl").read_text().splitlines()]
        self.assertGreaterEqual(len(m), 1)
        f = Path(self.dir.name) / m[0]["file"]
        import hashlib
        self.assertEqual(hashlib.sha256(f.read_bytes()).hexdigest(), m[0]["sha256"])
        self.assertEqual(r.stats["refused_unwanted"], 1)

    def test_crash_leftover_is_recovered_not_lost(self):
        r = self.rec()
        r.record("book", "Arcus", "BTC", {"x": 1})
        r.fh.close()            # simulated crash: the part file stays behind
        r2 = self.rec()
        self.assertEqual(r2.stats["recovered"], 1)
        m = (Path(self.dir.name) / "manifest.jsonl").read_text()
        self.assertIn('"recovered": true', m)
        self.assertFalse(list(Path(self.dir.name).glob("segments/*/*.part.jsonl.gz")))

    def test_disk_guard_stops_writing_and_marks_a_gap(self):
        r = self.rec()
        self.free[0] = 1
        self.assertFalse(r.record("book", "Arcus", "BTC", {}))
        self.assertFalse(r.health()["writing"])
        self.free[0] = 10 ** 12
        self.assertTrue(r.record("book", "Arcus", "BTC", {}))
        gaps = (Path(self.dir.name) / "gaps.jsonl").read_text().splitlines()
        self.assertEqual(len(gaps), 2)

    def test_retention_never_touches_open_segment(self):
        r = self.rec(segment_bytes=50)
        for i in range(6):
            r.record("book", "Arcus", "BTC", {"i": i})
        removed = r.retention(max_total_bytes=0)
        self.assertTrue(removed)
        self.assertTrue(all(".part." not in p.name for p in removed))

    def test_refuses_e_drive(self):
        with self.assertRaises(ValueError):
            Recorder("E:/x", lambda: set())


class OpsTests(unittest.TestCase):
    def test_wal_levels_and_preflight(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.sqlite3")
            Path(db + "-wal").write_bytes(b"x" * 10)
            self.assertEqual(ops.wal_status(db, warn=5, stop=20)["level"], "WARN")
            self.assertEqual(ops.wal_status(db, warn=50, stop=100)["level"], "OK")
            self.assertEqual(ops.wal_status(db, warn=1, stop=5)["level"], "STOP")
            du = lambda p: collections.namedtuple("u", "total used free")(1, 1, 10)
            ok, why = ops.preflight(d, db, min_free=100, disk_usage=du)
            self.assertFalse(ok)
            self.assertFalse(ops.preflight("E:/data")[0])

    def test_risk_defaults_unchanged(self):
        self.assertEqual(wide_paper.WIDE_DEFAULTS["wide_max_breakeven_hours"], 24.0)
        self.assertEqual(wide_paper.WIDE_DEFAULTS["wide_funding_realization"], 1.0)


class Provider:
    """MOCK books/rows/settled payments for the hybrid desk."""

    def __init__(self):
        self.books = {("Arcus", "BTC"): self.bk(100.0), ("Lighter", "BTC"): self.bk(101.0)}
        self.rows = {("Arcus", "BTC"): dict(fee=0.0005, rate=0.0),
                     ("Lighter", "BTC"): dict(fee=0.0004, rate=0.0001)}
        self.paid = {}

    @staticmethod
    def bk(mid, depth=100.0):
        return dict(bids=[(mid - 0.01, depth)], asks=[(mid + 0.01, depth)])

    def rows_by(self):
        return self.rows

    def book(self, v, a):
        return self.books.get((v, a))

    def settled(self, v, a, at):
        return self.paid.get((v, a), [])


class HybridDeskTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.t = [T0]
        self.pv = Provider()
        self.ev = dict(baseline=0.0, mad=0.001, deviation=0.00995, funding=0.5, basis=5.0,
                       net=3.0)
        self.c = dict(asset="BTC", long="Arcus", short="Lighter")

    def desk(self):
        return HybridDesk(os.path.join(self.dir.name, "h.json"), self.pv, CFG,
                          clock=lambda: self.t[0])

    def test_open_settle_converge_close_once(self):
        d = self.desk()
        p = d.open(self.c, self.ev, 1000.0)
        self.assertEqual(p["state"], HEDGED_OPEN)
        self.assertAlmostEqual(p["legs"]["long"]["qty"], p["legs"]["short"]["qty"])
        # Two hourly payments, seen twice: booked once each.
        self.pv.paid[("Lighter", "BTC")] = [dict(ts=T0 + 3600, rate=0.0001),
                                            dict(ts=T0 + 7200, rate=0.0001)]
        self.t[0] += 7300
        d.settle(p)
        d.settle(p)
        # Valued at the entry price: estimates (phase 7), booked once each.
        self.assertEqual(sum(1 for e in p["ledger"] if e["kind"] == "funding_estimated"), 2)
        # Gap back to baseline AND funding reversed in the same cycle: one close.
        self.pv.books[("Lighter", "BTC")] = Provider.bk(100.0)
        self.pv.rows[("Lighter", "BTC")]["rate"] = -0.001
        p["weak_since"] = self.t[0] - 3600
        d.cycle()
        self.assertEqual(p["state"], CLOSED)
        self.assertEqual(p["close_reason"], "CONVERGED")
        self.assertEqual(p["secondary"], ["FUNDING_REVERSED"])
        fees = [e for e in p["ledger"] if e["kind"] == "fee"]
        self.assertEqual(len(fees), 4)                   # 2 opens + 2 closes, no more
        pn = HybridDesk.pnl(p)
        self.assertAlmostEqual(pn["net"], pn["price_pnl"] + pn["funding"] - pn["fees"])
        d.cycle()                                        # closed: nothing happens again
        self.assertEqual(len([e for e in p["ledger"] if e["kind"] == "fee"]), 4)

    def test_partial_second_leg_unwinds_the_excess(self):
        self.pv.books[("Lighter", "BTC")] = Provider.bk(101.0, depth=4.0)
        p = self.desk().open(self.c, self.ev, 1000.0)
        self.assertEqual(p["state"], HEDGED_OPEN)
        self.assertAlmostEqual(p["legs"]["long"]["qty"], p["legs"]["short"]["qty"])
        self.assertTrue(any(e["key"].startswith("unwind:") for e in p["ledger"]))

    def test_second_leg_without_fill_is_leg_recovery_and_first_without_fill_failed(self):
        self.pv.books[("Lighter", "BTC")] = dict(bids=[(50.0, 1.0)], asks=[(150.0, 1.0)])
        p = self.desk().open(self.c, self.ev, 1000.0)
        self.assertEqual(p["state"], LEG_RECOVERY)
        self.assertFalse(p["legs"]["long"]["open"])
        self.pv.books[("Arcus", "BTC")] = None
        p = self.desk().open(self.c, self.ev, 1000.0)
        self.assertEqual(p["state"], FAILED)

    def test_exit_waits_for_both_books_and_restart_recovers(self):
        d = self.desk()
        p = d.open(self.c, self.ev, 1000.0)
        pid = p["id"]
        self.pv.books[("Arcus", "BTC")] = None          # long book gone: cannot close now
        d.close(p, ["MAX_HOLD"])
        self.assertEqual(p["state"], EXIT_PENDING)
        self.pv.books[("Arcus", "BTC")] = Provider.bk(100.0)
        d2 = self.desk()                                 # restart resumes the exit
        self.assertEqual(d2.positions[pid]["state"], CLOSED)
        self.assertEqual(d2.positions[pid]["close_reason"], "MAX_HOLD")

    def test_crash_while_opening_is_recovered_as_leg_recovery(self):
        d = self.desk()
        p = d.open(self.c, self.ev, 1000.0)
        p["state"] = OPENING
        p["legs"].pop("short")                           # crash after the first leg
        d.save()
        d2 = self.desk()
        self.assertEqual(d2.positions[p["id"]]["state"], LEG_RECOVERY)

    def test_stop_has_priority(self):
        d = self.desk()
        p = d.open(self.c, self.ev, 1000.0)
        self.pv.books[("Lighter", "BTC")] = Provider.bk(110.0)   # gap widens far
        self.t[0] += 80 * 3600
        self.assertEqual(d.exit_reasons(p)[0], "STOP")
        self.assertIn("MAX_HOLD", d.exit_reasons(p))


class FakeCollector:
    def __init__(self):
        self.reasons = {"arcus": {"ROW": 1}}

    def refresh(self):
        return [dict(venue="Arcus", asset="BTC", kind="perp", mark=100.0, rate=0.0,
                     raw_rate=0.0, interval=3600, next=None, received=T0, fee=0.0005,
                     leverage=None, price_type="mark"),
                dict(venue="Lighter", asset="BTC", kind="perp", mark=100.5,
                     rate=0.0005, raw_rate=0.0005, interval=3600, next=None,
                     received=T0, fee=0.0004, leverage=None, price_type="mark")]


class FakeGate:
    def evaluate(self, c, at, strategy):
        if strategy == "hybrid":
            return dict(reasons=[], eligible=False, research_only=True, net=1.5,
                        funding=1.0, basis=2.0, fees=1.0, spread=0.3, reserve=0.2,
                        baseline=0.0, mad=0.001, deviation=0.005)
        return dict(reasons=[dict(category="COST", code="NET_NEGATIVE", text="x")],
                    eligible=False, net=-1.0)


class RuntimeTests(unittest.TestCase):
    def test_round_summarizes_and_never_opens_hybrids_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            pv = Provider()
            desk = HybridDesk(os.path.join(d, "h.json"), pv, CFG, clock=lambda: T0)
            rt = Runtime(FakeCollector(), FakeGate(), CFG, desk=desk, clock=lambda: T0)
            snap = rt.evaluate_once()
            # Convergence is evaluated in both directions: two hybrid candidates.
            self.assertEqual(snap["strategies"]["hybrid"]["by_category"], {"RESEARCH_ONLY": 2})
            self.assertEqual(snap["opened"], [])
            rt.cfg["wide_hybrid_enabled"] = True
            snap = rt.evaluate_once()
            self.assertEqual(len(snap["opened"]), 1)
            html = dashboard.render(rt.snapshot())
            self.assertIn("Hibrit", html)
            self.assertIn("NET_NEGATIVE", html)

    def test_run_survives_errors(self):
        class Boom(FakeCollector):
            n = 0

            def refresh(self):
                Boom.n += 1
                if Boom.n == 1:
                    raise IOError("venue down")
                return super().refresh()

        stop = threading.Event()
        waits = []

        def wait(s):
            waits.append(s)
            if len(waits) >= 2:
                stop.set()
            return stop.is_set()

        stop.wait = wait
        rt = Runtime(Boom(), FakeGate(), CFG, clock=lambda: T0)
        rt.run(stop, every=60)
        self.assertEqual(len(rt.errors), 1)
        self.assertEqual(rt.rounds, 1)

    def test_safety_stops_new_hybrids(self):
        with tempfile.TemporaryDirectory() as d:
            desk = HybridDesk(os.path.join(d, "h.json"), Provider(), CFG, clock=lambda: T0)
            rt = Runtime(FakeCollector(), FakeGate(), dict(CFG, wide_hybrid_enabled=True),
                         desk=desk, data_dir=d, clock=lambda: T0)
            with mock.patch.object(ops, "preflight", return_value=(False, ["disk"])):
                snap = rt.evaluate_once()
            self.assertEqual(snap["opened"], [])
            self.assertFalse(snap["safe"])

    def test_dashboard_escapes_and_labels_paper(self):
        html = dashboard.render(dict(rows=1, strategies={"funding": dict(
            candidates=1, by_category={"DATA": 1}, first_reason={"<b>x": 1})}))
        self.assertIn("&lt;b&gt;x", html)
        self.assertIn("PAPER ONLY", html)


if __name__ == "__main__":
    unittest.main()
