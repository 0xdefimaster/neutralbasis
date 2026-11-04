"""Batched writes in storage.Store: same records as the commit-per-write path, lines
always written before their index rows are committed, bounded waiting, and a known
loss on a crash. MOCK records only (tests/fakes.py and synthetic rows); never market
evidence."""

import glob
import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from tools.phase1_recorder.clients.records import record_key
from tools.phase1_recorder.config import load
from tools.phase1_recorder.recorder import Recorder
from tools.phase1_recorder.storage import Store

from .fakes import Clock, FakeVenues

T = 1_780_000_000


def candle(mid, i):
    return dict(venue="Hyperliquid", market_id=mid, asset=mid, open_time=T + i * 3600,
                close="1.0", source="MOCK")


class FailingCommit:
    """Stands in for the database connection; its commit fails (a crash between
    writing the lines and committing the index)."""

    def __init__(self, db):
        self._db = db

    def commit(self):
        raise OSError("simulated crash before commit")

    def __getattr__(self, name):
        return getattr(self._db, name)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)  # runs last
        self.root = os.path.join(self.dir.name, "phase1-evidence")

    def store(self, **kw):
        s = Store(self.root, **kw)
        self.addCleanup(s.close)
        return s

    def put(self, store, n, start=0, mid="BTC"):
        for i in range(start, start + n):
            r = candle(mid, i)
            store.put_record("candle", "Hyperliquid", record_key("candle", r), r, T)

    def committed_rows(self):
        """What another reader of the database sees (committed rows only)."""
        db = sqlite3.connect(os.path.join(self.root, "recorder.sqlite3"))
        try:
            return db.execute("select count(*) from records").fetchone()[0]
        finally:
            db.close()

    def file_lines(self):
        n = 0
        for path in glob.glob(
            os.path.join(self.root, "normalized", "candle", "*", "*.jsonl")
        ):
            with open(path, encoding="utf-8") as f:
                n += sum(1 for line in f if line.strip())
        return n


class StoreBatchTests(Base):
    def test_the_default_still_commits_every_write(self):
        s = self.store()
        self.put(s, 3)
        self.assertEqual(self.committed_rows(), 3)
        self.assertEqual(self.file_lines(), 3)

    def test_batched_changes_wait_but_are_visible_to_the_recorder_itself(self):
        s = self.store(batch_records=1000, batch_seconds=3600)
        self.put(s, 10)
        self.assertEqual(self.committed_rows(), 0)  # not yet committed
        self.assertTrue(s.has_record("candle", "Hyperliquid", "BTC|%d" % T))
        self.assertEqual(len(s.records("candle")), 10)  # lines written for readers
        self.assertEqual(self.committed_rows(), 0)  # reading never commits
        s.flush()
        self.assertEqual(self.committed_rows(), 10)
        self.assertEqual(self.file_lines(), 10)

    def test_a_full_batch_is_flushed(self):
        s = self.store(batch_records=5, batch_seconds=3600)
        self.put(s, 4)
        self.assertEqual(self.committed_rows(), 0)
        self.put(s, 1, start=4)
        self.assertEqual(self.committed_rows(), 5)
        self.assertEqual(self.file_lines(), 5)

    def test_waiting_is_bounded_in_time(self):
        s = self.store(batch_records=1000, batch_seconds=0.05)
        self.put(s, 1)
        self.assertEqual(self.committed_rows(), 0)
        time.sleep(0.1)
        self.put(s, 1, start=1)  # the next write finds the oldest change too old
        self.assertEqual(self.committed_rows(), 2)
        self.assertGreaterEqual(s.write_stats["max_wait_seconds"], 0.05)

    def test_close_writes_everything_waiting(self):
        s = Store(self.root, batch_records=1000, batch_seconds=3600)
        self.put(s, 25)
        s.close()
        self.assertEqual(self.committed_rows(), 25)
        self.assertEqual(self.file_lines(), 25)

    def test_a_crash_before_a_flush_loses_the_waiting_changes_together(self):
        s = Store(self.root, batch_records=1000, batch_seconds=3600)
        self.put(s, 10)
        s.db.close()  # the process dies: nothing committed, buffered lines gone
        s.closed = True
        again = self.store()
        self.assertEqual(self.committed_rows(), 0)
        self.assertEqual(self.file_lines(), 0)  # no line without its row
        self.assertFalse(again.has_record("candle", "Hyperliquid", "BTC|%d" % T))
        self.put(again, 10)  # history is fetched again and stored
        self.assertEqual(self.committed_rows(), 10)

    def test_a_crash_between_lines_and_commit_is_read_once_per_key(self):
        s = Store(self.root, batch_records=1000, batch_seconds=3600)
        self.put(s, 10)
        real = s.db
        s.db = FailingCommit(real)
        with self.assertRaises(OSError):
            s.flush()
        self.assertEqual(s.write_stats["errors"], 1)
        self.assertIn("simulated crash", s.write_stats["last_error"])
        real.close()  # the process dies: the open transaction is rolled back
        s.closed = True
        again = self.store()
        self.assertEqual(self.committed_rows(), 0)
        self.assertEqual(self.file_lines(), 10)  # lines first, as always
        self.put(again, 10)  # written again: a second line per key
        again.flush()
        self.assertEqual(self.file_lines(), 20)
        self.assertEqual(len(again.records("candle")), 10)  # read once per key
        self.assertEqual(self.committed_rows(), 10)

    def test_one_operation_is_flushed_whole(self):
        # A correction changes the index and writes two records; with commit per
        # write it is still one flush, never half of it.
        s = self.store()
        base = dict(venue="Extended", asset="BTC", market_id="BTC-USD",
                    settled_at=T, settled_at_api=T, settled_rate_raw="0.0001",
                    raw_sha256=None, source="MOCK")
        self.assertEqual(s.put_settlement(base, T), "new")
        before = s.write_stats["flushes"]
        self.assertEqual(s.put_settlement(dict(base, settled_rate_raw="0.0002"), T), "correction")
        self.assertEqual(s.write_stats["flushes"], before + 1)
        self.assertEqual(len(s.records("settlement_correction")), 1)

    def test_a_steady_minute_of_202_assets_is_stored_completely(self):
        # One minute of the planned 202-asset recording: a funding snapshot and a
        # 20-level book for every market of four venues (load test; speed is
        # measured by the benchmark script, not asserted here).
        s = self.store(batch_records=500, batch_seconds=1.0)
        venues = ("Hyperliquid", "LighterCore", "Lighter", "Extended")
        levels = [["100.%04d" % i, "1.5"] for i in range(20)]
        n = 0
        for v in venues:
            for a in range(202):
                mid = "A%03d" % a
                snap = dict(venue=v, market_id=mid, asset=mid, received_at=T,
                            rate="0.00001", source="MOCK")
                book = dict(venue=v, market_id=mid, asset=mid, received_at=T,
                            bids=levels, asks=levels, source="MOCK")
                for kind, r in (("funding_snapshot", snap), ("orderbook", book)):
                    self.assertTrue(
                        s.put_record(kind, v, record_key(kind, r), r, T)
                    )
                    n += 1
        s.flush()
        self.assertEqual(n, 1616)
        db = sqlite3.connect(os.path.join(self.root, "recorder.sqlite3"))
        try:
            self.assertEqual(
                db.execute("select count(*) from records").fetchone()[0], n
            )
        finally:
            db.close()
        self.assertEqual(len(s.records("funding_snapshot")), 808)
        self.assertEqual(len(s.records("orderbook")), 808)
        self.assertLessEqual(s.write_stats["flushes"], 5)  # not one per record
        self.assertEqual(s.write_stats["errors"], 0)


class RecorderBatchTests(unittest.TestCase):
    def record(self, d, venue, **cfg_overrides):
        # One venue per run: venue threads share the fake clock, so several venues
        # would interleave differently from run to run.
        clock = Clock()
        cfg = load(root=os.path.join(d, "phase1-evidence"), assets=["BTC", "ETH"],
                   venues=[venue])
        cfg["backfill_days"] = 2
        cfg.update(cfg_overrides)
        rec = Recorder(cfg, transport=FakeVenues(clock), clock=clock,
                       sleep=clock.sleep)
        try:
            self.assertEqual(rec.run("once"), "bitti")
            db = sqlite3.connect(os.path.join(cfg["root"], "recorder.sqlite3"))
            try:  # committed before close: tasks and the session end flush
                counts = dict(db.execute(
                    "select kind, count(*) from records group by kind").fetchall())
            finally:
                db.close()
            sid = rec.store.db.execute("select max(id) from sessions").fetchone()[0]
            stats = rec.store.checkpoint("write_stats:%d" % sid)
        finally:
            rec.close()
        lines = {}
        for path in glob.glob(
            os.path.join(cfg["root"], "normalized", "*", "*", "*.jsonl")
        ):
            kind = os.path.basename(os.path.dirname(os.path.dirname(path)))
            with open(path, encoding="utf-8") as f:
                lines.setdefault(kind, []).extend(
                    json.dumps(json.loads(x), sort_keys=True) for x in f if x.strip()
                )
        return counts, {k: sorted(v) for k, v in lines.items()}, stats

    def test_batched_and_per_write_recordings_are_identical(self):
        for venue in ("Hyperliquid", "LighterCore", "Lighter", "Extended"):
            with self.subTest(venue=venue), tempfile.TemporaryDirectory() as a, \
                    tempfile.TemporaryDirectory() as b:
                batched = self.record(a, venue)
                single = self.record(
                    b, venue, write_batch_records=1, write_batch_seconds=0.0
                )
                self.assertEqual(batched[0], single[0])
                self.assertEqual(batched[1], single[1])
                self.assertGreater(sum(batched[0].values()), 0)
                self.assertEqual(batched[2]["errors"], 0)
                self.assertEqual(single[2]["errors"], 0)
                # Far fewer commits than changes when batched.
                self.assertLess(batched[2]["flushes"], batched[2]["changes"])

    def test_a_failing_flush_is_logged_and_the_task_goes_on(self):
        with tempfile.TemporaryDirectory() as d:
            clock = Clock()
            cfg = load(root=os.path.join(d, "phase1-evidence"), assets=["BTC"],
                       venues=["Extended"])
            cfg["backfill_days"] = 1
            rec = Recorder(cfg, transport=FakeVenues(clock), clock=clock,
                           sleep=clock.sleep)
            try:
                v = rec.venues[0]
                with mock.patch.object(rec.store, "flush",
                                       side_effect=OSError("disk full")):
                    self.assertTrue(v.run_task("metadata"))
                rec.store.flush()  # the waiting changes are written later
                self.assertTrue(rec.store.records("market"))
            finally:
                rec.close()
            with open(os.path.join(cfg["root"], "logs", "recorder.log"),
                      encoding="utf-8") as f:
                self.assertIn("yazma hatası: OSError: disk full", f.read())


if __name__ == "__main__":
    unittest.main()
