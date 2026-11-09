import os, tempfile, threading, time, unittest
import bot
import runtime_metrics
from runtime_metrics import StageClock, TimedLock


class TimedLockTests(unittest.TestCase):
    def test_reentrant_like_an_rlock_and_only_the_outer_hold_counts(self):
        lock = TimedLock()
        with lock:
            with lock:
                self.assertTrue(lock.acquire(blocking=False))
                lock.release()
        r = lock.report()
        self.assertEqual(r["acquisitions"], 1)
        self.assertEqual(r["hold_seconds"]["n"], 1)
        self.assertIn("test_reentrant_like_an_rlock", r["busiest_sites"][0]["site"])

    def test_wait_and_hold_are_measured_under_contention(self):
        lock = TimedLock(slow=0.1)
        held = threading.Event()

        def holder():
            with lock:
                held.set()
                time.sleep(0.3)

        t = threading.Thread(target=holder, name="slow-writer")
        t.start()
        held.wait(5)
        with lock:
            pass
        t.join(5)
        r = lock.report()
        self.assertGreaterEqual(r["wait_seconds"]["max"], 0.2)
        self.assertGreaterEqual(r["hold_seconds"]["max"], 0.25)
        slow = r["slow_holds"]
        self.assertEqual(slow[0]["thread"], "slow-writer")
        self.assertIn("holder", slow[0]["site"])

    def test_failed_try_acquire_is_not_counted_and_other_threads_are_excluded(self):
        lock = TimedLock()
        out = {}
        with lock:
            t = threading.Thread(target=lambda: out.update(got=lock.acquire(timeout=0.05)))
            t.start()
            t.join(5)
        self.assertFalse(out["got"])
        self.assertEqual(lock.report()["acquisitions"], 1)

    def test_empty_report_is_unknown_not_zero(self):
        r = TimedLock().report()
        self.assertEqual(r["wait_seconds"], dict(n=0, p50=None, p95=None, max=None))

    def test_store_uses_it_and_stays_usable(self):
        with tempfile.TemporaryDirectory() as d:
            s = bot.Store(os.path.join(d, "t.sqlite3"))
            try:
                s.put("k", {"a": 1})
                self.assertEqual(s.get("k", None), {"a": 1})
                self.assertIsInstance(s.lock, TimedLock)
                self.assertGreater(s.lock.report()["acquisitions"], 0)
            finally:
                s.db.close()


class StageClockTests(unittest.TestCase):
    def test_stage_durations_come_from_stage_marks(self):
        t = [0.0]
        c = StageClock(clock=lambda: t[0])
        c.begin()
        c.mark("scan")
        t[0] = 4.0
        c.mark("deep")
        t[0] = 10.0
        last = c.end()
        self.assertEqual(last["stages"], [("scan", 4.0), ("deep", 6.0)])
        r = c.report()
        self.assertEqual(r["stages"]["deep"]["max"], 6.0)
        self.assertEqual(r["cycle_seconds"]["max"], 10.0)

    def test_engine_stage_assignment_marks_the_clock(self):
        e = bot.Engine.__new__(bot.Engine)
        self.assertEqual(e.stage, "Başlatılıyor")
        e.stage = "before clock"  # no clock yet: just stored
        e.stage_clock = StageClock()
        e.stage_clock.begin()
        e.stage = "A"
        e.stage = "B"
        e.stage_clock.end()
        self.assertEqual(e.stage, "B")
        self.assertEqual([s for s, _ in e.stage_clock.report()["last"]["stages"]], ["A", "B"])
        self.assertIsNone(e.runtime_metrics()["store_lock"])


class StallDumpTests(unittest.TestCase):
    def tearDown(self):
        runtime_metrics.disarm_stall_dump()
        runtime_metrics.STALL_LOG = None

    def test_a_stalled_cycle_dumps_all_thread_stacks(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "stall.log")
            with open(path, "w", encoding="utf8") as f:
                runtime_metrics.STALL_LOG = f
                self.assertTrue(runtime_metrics.arm_stall_dump(0.2))
                time.sleep(0.6)  # the "cycle" is stuck here
                runtime_metrics.disarm_stall_dump()
            with open(path, encoding="utf8") as f:
                text = f.read()
            self.assertIn("test_a_stalled_cycle_dumps_all_thread_stacks", text)

    def test_a_finished_cycle_leaves_no_dump(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "stall.log")
            with open(path, "w", encoding="utf8") as f:
                runtime_metrics.STALL_LOG = f
                runtime_metrics.arm_stall_dump(0.3)
                runtime_metrics.disarm_stall_dump()
                time.sleep(0.5)
            self.assertEqual(os.path.getsize(path), 0)


if __name__ == "__main__":
    unittest.main()
