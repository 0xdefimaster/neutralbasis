"""Published/settled matching reads the files once per task, not once per market,
and gives every market exactly what the per-market read gave it.
MOCK responses only (tests/fakes.py); never market evidence."""

import os
import tempfile
import unittest
from unittest import mock

from tools.phase1_recorder import matching
from tools.phase1_recorder.config import load
from tools.phase1_recorder.recorder import Recorder

from .fakes import Clock, FakeVenues

ASSETS = ["BTC", "ETH"]  # the markets the fake venues list


class MatchScalingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)  # runs last
        self.clock = Clock()
        cfg = load(
            root=os.path.join(self.dir.name, "phase1-evidence"),
            assets=ASSETS,
            venues=["Hyperliquid", "LighterCore", "Extended"],
        )
        cfg["backfill_days"] = 2
        self.rec = Recorder(
            cfg, transport=FakeVenues(self.clock), clock=self.clock,
            sleep=self.clock.sleep,
        )
        self.addCleanup(self.rec.close)
        self.rec.run("once")
        self.rec.stop.clear()

    def captured(self, call):
        """The (snapshots, payments) handed to matching.compare, per market."""
        seen = {}
        real = matching.compare

        def spy(snaps, paid, *rest):
            mids = {s["market_id"] for s in snaps} | {p["market_id"] for p in paid}
            self.assertLessEqual(len(mids), 1)  # never another market's rows
            key = mids.pop() if mids else None
            seen.setdefault(key, []).append((snaps, paid))
            return real(snaps, paid, *rest)

        with mock.patch.object(matching, "compare", side_effect=spy):
            call()
        return seen

    def test_every_market_gets_what_the_per_market_read_gave_it(self):
        at = self.clock()
        for v in self.rec.venues:
            for since in (None, at - 5 * 86400):
                with self.subTest(venue=v.venue, since=since):
                    one_by_one = self.captured(
                        lambda: [v.match(m, at, since) for m in v.selected]
                    )
                    together = self.captured(
                        lambda: v.match_all(v.selected, at, since)
                    )
                    self.assertEqual(together, one_by_one)
                    self.assertTrue(any(p for runs in together.values()
                                        for _, p in runs))  # payments were there

    def test_a_task_reads_the_files_once_not_once_per_market(self):
        store = self.rec.store
        for v in self.rec.venues:
            for task in ("settlements", "reconcile"):
                # Every read of the files goes through iter_records (records too).
                with self.subTest(venue=v.venue, task=task), mock.patch.object(
                    store, "iter_records", wraps=store.iter_records
                ) as reads:
                    self.assertTrue(v.run_task(task))
                    kinds = [c.args[0] for c in reads.call_args_list]
                    self.assertEqual(len(v.selected), len(ASSETS))  # > 1 market
                    self.assertEqual(kinds.count("funding_snapshot"), 1)
                    self.assertEqual(kinds.count("settlement"), 1)

    def test_reconcile_still_marks_every_market_after_its_comparisons(self):
        v = next(x for x in self.rec.venues if x.venue == "Extended")
        self.assertTrue(v.run_task("reconcile"))
        for m in v.selected:
            cp = self.rec.store.checkpoint(
                "reconcile:%s:%s" % (v.venue, m["market_id"])
            )
            self.assertIsNotNone(cp)
            self.assertIn("checked", cp)
            self.assertIsNotNone(
                self.rec.store.checkpoint("pending:%s:%s" % (v.venue, m["market_id"]))
            )


if __name__ == "__main__":
    unittest.main()
