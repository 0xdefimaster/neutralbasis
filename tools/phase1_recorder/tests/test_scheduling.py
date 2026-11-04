"""Task scheduling in the duration loop: periods count from a task's start, and the
one-request funding snapshot keeps its own interval while long per-market tasks
(books, history) run. MOCK responses and a fake clock only; never market evidence."""

import os
import tempfile
import unittest
from unittest import mock

from tools.phase1_recorder.config import load
from tools.phase1_recorder.recorder import Recorder

from .fakes import Clock, FakeVenues

BOOK_SECONDS = 40  # each market's book takes this long (fake clock)


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)  # runs last
        self.clock = Clock()
        cfg = load(
            root=os.path.join(self.dir.name, "phase1-evidence"),
            assets=["BTC", "ETH"],
            venues=["LighterCore"],
        )
        cfg["backfill_days"] = 1
        self.rec = Recorder(
            cfg, transport=FakeVenues(self.clock), clock=self.clock,
            sleep=self.clock.sleep,
        )
        self.addCleanup(self.rec.close)
        self.rec.run("once")
        self.rec.stop.clear()
        self.v = self.rec.venues[0]
        self.assertEqual(len(self.v.selected), 2)
        real = self.v.client.orderbook

        def slow(m, at):
            self.clock.sleep(BOOK_SECONDS)
            return real(m, self.clock())

        p = mock.patch.object(self.v.client, "orderbook", side_effect=slow)
        p.start()
        self.addCleanup(p.stop)
        self.started = []
        run = self.v.run_task

        def record(task):
            self.started.append((task, self.clock()))
            return run(task)

        p = mock.patch.object(self.v, "run_task", side_effect=record)
        p.start()
        self.addCleanup(p.stop)

    def snapshots(self):
        return len(self.rec.store.records("funding_snapshot"))

    def test_a_due_funding_snapshot_runs_between_book_markets(self):
        v = self.v
        v.looping = True
        now = self.clock()
        v.next_due["funding"] = now + BOOK_SECONDS / 2  # due during the books
        before = self.snapshots()
        v.run_due("orderbook")
        tasks = [t for t, _ in self.started]
        self.assertEqual(tasks, ["orderbook", "funding"])
        self.assertGreater(self.snapshots(), before)
        funding_at = dict((t, at) for t, at in self.started)["funding"]
        self.assertEqual(v.next_due["funding"], funding_at + v.period["funding"])

    def test_nothing_is_interleaved_outside_the_duration_loop(self):
        v = self.v
        v.looping = False
        v.next_due["funding"] = 0
        v.run_due("orderbook")
        self.assertEqual([t for t, _ in self.started], ["orderbook"])

    def test_a_fast_task_is_never_nested_in_itself(self):
        v = self.v
        v.looping = True
        v.in_fast = True
        v.next_due["funding"] = 0
        v.between_markets()
        self.assertEqual(self.started, [])

    def test_the_next_run_counts_from_the_start_not_the_end(self):
        v = self.v
        started = self.clock()
        v.run_due("orderbook")
        ended = self.clock()
        self.assertEqual(ended - started, 2 * BOOK_SECONDS)  # longer than its period
        self.assertEqual(v.next_due["orderbook"], started + v.period["orderbook"])
        self.assertLess(v.next_due["orderbook"], ended)  # due again at once

    def test_in_the_loop_funding_keeps_its_interval_while_books_take_longer(self):
        v = self.v
        now = self.clock()
        for t in v.tasks:  # history tasks already ran in setUp
            v.next_due[t] = now + (0 if t in ("funding", "orderbook") else 10**6)
        wait = mock.patch.object(
            v.stop, "wait", side_effect=lambda s: self.clock.sleep(s) or False
        )
        with wait:
            v.loop(now + 900)
        self.assertFalse(v.looping)
        funding = [at for t, at in self.started if t == "funding"]
        books = [at for t, at in self.started if t == "orderbook"]
        gaps = [b - a for a, b in zip(funding, funding[1:])]
        period = v.period["funding"]
        # At most one market's book late; before, a funding snapshot waited for
        # the whole book round plus a full period (here 80 + 60 s).
        self.assertLessEqual(max(gaps), period + BOOK_SECONDS + 5)
        self.assertGreaterEqual(len(funding), 900 // (period + BOOK_SECONDS))
        # Books run back to back: each round started one round after the last.
        book_gaps = [b - a for a, b in zip(books, books[1:])]
        self.assertTrue(all(g < 2 * BOOK_SECONDS + period for g in book_gaps))
        # Nothing new starts after the end of the session.
        self.assertTrue(all(at < now + 900 for _, at in self.started))


if __name__ == "__main__":
    unittest.main()
