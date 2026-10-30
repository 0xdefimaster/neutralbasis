"""Comparison identity: one completed comparison per (venue, market, actual settlement
time), however often matching runs. MOCK responses only (tests/fakes.py)."""

import json
import os
import sqlite3
import tempfile
import unittest

from tools.phase1_recorder.config import load
from tools.phase1_recorder.recorder import Recorder
from tools.phase1_recorder.storage import Store
from tools.phase1_recorder.validation import comparison_index, consistency, coverage

from .fakes import Clock, FakeVenues

FINAL = Store.FINAL


class Base(unittest.TestCase):
    venues = ["Hyperliquid", "Lighter", "Extended"]

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.fake = FakeVenues(self.clock, assets=("BTC",))
        self.cfg = load(
            root=os.path.join(self.dir.name, "phase1-evidence"),
            assets=["BTC"],
            venues=self.venues,
        )
        self.cfg["backfill_days"] = 1
        self.rec = None

    def tearDown(self):
        if self.rec:
            self.rec.store.close()
        self.dir.cleanup()

    def start(self):
        if self.rec:
            self.rec.store.close()
        self.rec = Recorder(
            self.cfg, transport=self.fake, clock=self.clock, sleep=self.clock.sleep
        )
        self.by = {v.venue: v for v in self.rec.venues}
        return self.rec

    def watch(self, minutes, settle=True):
        """Funding snapshots every minute, then the settlement task."""
        for _ in range(minutes):
            self.clock.t += 60
            for v in self.by.values():
                v.run_task("funding")
        if settle:
            for v in self.by.values():
                v.run_task("settlements")

    def boot(self):
        self.start()
        for v in self.by.values():
            v.run_task("metadata")

    # --- counts ------------------------------------------------------------------
    def db_rows(self, venue=None):
        q = "select venue, market_id, settled_at, expected_settlement_at, status from comparisons"
        rows = self.rec.store.db.execute(q).fetchall()
        return [r for r in rows if venue is None or r[0] == venue]

    def jsonl(self, venue=None):
        return [
            c
            for c in self.rec.store.records("comparison")
            if venue is None or c["venue"] == venue
        ]

    def assert_unique(self):
        rows = self.db_rows()
        # count(comparisons) == count(distinct venue, market_id, settled_at)
        total, distinct = self.rec.store.db.execute(
            "select count(*), (select count(*) from (select distinct venue, market_id,"
            " settled_at from comparisons)) from comparisons"
        ).fetchone()
        self.assertEqual(total, distinct)
        self.assertTrue(all(r[2] is not None for r in rows))
        keys = [
            (c["venue"], str(c["market_id"]), int(round(c["settled_at"])))
            for c in self.jsonl()
        ]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual({(v, m, t) for v, m, t, _, _ in rows}, set(keys))
        self.assertEqual(comparison_index(self.rec.store)["problems"], [])
        return total

    def watched_settlements(self, venue):
        """Settlements whose own period contains at least one snapshot."""
        snaps = [
            s["received_at"] for s in self.rec.store.records("funding_snapshot", venue)
        ]
        paid = sorted(
            {
                int(round(p["settled_at"]))
                for p in self.rec.store.records("settlement", venue)
            }
        )
        out = set()
        for prev, t in zip(paid, paid[1:]):
            if any(prev < x < t for x in snaps):
                out.add(t)
        return out


class ComparisonIdentityTests(Base):
    def test_1_2_3_identity_is_the_actual_settlement_for_every_venue(self):
        self.boot()
        self.watch(4 * 60)
        for venue, announced in (
            ("Hyperliquid", False),
            ("Lighter", False),
            ("Extended", True),
        ):
            with self.subTest(venue=venue):
                snaps = self.rec.store.records("funding_snapshot", venue)
                self.assertEqual(any(s["next_funding_at"] for s in snaps), announced)
                rows = self.db_rows(venue)
                self.assertEqual(len(rows), 4)
                for _, _, settled, expected, status in rows:
                    self.assertIsNotNone(settled)
                    self.assertIn(status, FINAL)
                    if announced:
                        self.assertEqual(expected, settled)  # a field, equal here
                    else:
                        self.assertIsNone(expected)  # null never enters the key
                self.assertEqual({r[2] for r in rows}, self.watched_settlements(venue))
                self.assertEqual(len(self.jsonl(venue)), len(rows))
        self.assert_unique()

    def test_4_repeated_matching_adds_nothing(self):
        self.boot()
        self.watch(4 * 60)
        before = self.assert_unique()
        lines = len(self.jsonl())
        for v in self.by.values():
            for m in v.selected:
                for _ in range(10):
                    v.match(m, self.clock())
        self.assertEqual(self.assert_unique(), before)
        self.assertEqual(len(self.jsonl()), lines)

    def test_5_6_repeated_settlement_tasks_without_new_payments_add_nothing(self):
        self.boot()
        self.watch(4 * 60)
        before = self.assert_unique()
        for _ in range(5):
            for v in self.by.values():
                v.run_task("settlements")
        self.assertEqual(self.assert_unique(), before)
        # A later payment adds exactly one comparison per market.
        self.watch(60)
        self.assertEqual(self.assert_unique(), before + len(self.venues))

    def test_7_coverage_counts_unique_settlements(self):
        self.boot()
        self.watch(4 * 60)
        for v in self.by.values():
            for m in v.selected:
                for _ in range(5):
                    v.match(m, self.clock())
        cov = coverage(self.rec.store, self.cfg, self.clock())
        for venue in self.venues:
            m = cov["venues"][venue]["markets"]["BTC"]
            self.assertEqual(m["comparisons"], len(self.watched_settlements(venue)))
            self.assertEqual(sum(m["comparison_statuses"].values()), m["comparisons"])

    def test_8_three_unique_settlements_are_needed_for_observed(self):
        self.boot()
        self.watch(2 * 60)  # two settlements watched
        cov = coverage(self.rec.store, self.cfg, self.clock())
        for venue in self.venues:
            m = cov["venues"][venue]["markets"]["BTC"]
            self.assertEqual(m["comparison_statuses"].get("MATCHED"), 2, venue)
            self.assertEqual(m["rate_unit"], "UNKNOWN", venue)
        self.watch(60)  # the third one
        cov = coverage(self.rec.store, self.cfg, self.clock())
        for venue in self.venues:
            m = cov["venues"][venue]["markets"]["BTC"]
            self.assertEqual(m["comparison_statuses"].get("MATCHED"), 3, venue)
            self.assertEqual(m["rate_unit"], "MOCK:OBSERVED", venue)

    def test_9_one_settlement_processed_many_times_cannot_reach_three(self):
        self.boot()
        self.watch(60)  # one settlement watched
        for _ in range(20):
            for v in self.by.values():
                v.run_task("settlements")
                for m in v.selected:
                    v.match(m, self.clock())
        self.assertEqual(self.assert_unique(), len(self.venues))
        cov = coverage(self.rec.store, self.cfg, self.clock())
        for venue in self.venues:
            m = cov["venues"][venue]["markets"]["BTC"]
            self.assertEqual(m["comparisons"], 1)
            self.assertEqual(m["rate_unit"], "UNKNOWN")
        # The store itself refuses a second copy, null announced time or not.
        c = self.rec.store.comparisons("Hyperliquid")[0]
        self.assertIsNone(c["expected_settlement_at"])
        for _ in range(5):
            self.assertFalse(self.rec.store.put_comparison(dict(c), self.clock()))
        self.assertEqual(len(self.db_rows("Hyperliquid")), 1)

    def test_10_restart_does_not_duplicate(self):
        self.boot()
        self.watch(4 * 60)
        before = self.assert_unique()
        lines = len(self.jsonl())
        for _ in range(3):
            self.start()  # a new recorder on the same folder
            for v in self.by.values():
                v.run_task("settlements")
        self.assertEqual(self.assert_unique(), before)
        self.assertEqual(len(self.jsonl()), lines)

    def test_pending_comparisons_are_never_completed_ones(self):
        self.boot()
        self.watch(3 * 60)
        self.watch(30, settle=False)
        for v in self.by.values():
            v.run_task("settlements")
        pending = self.rec.store.checkpoint("pending:Hyperliquid:BTC")
        self.assertEqual(pending[0]["comparison_status"], "PENDING")
        with self.assertRaises(ValueError):
            self.rec.store.put_comparison(pending[0], self.clock())
        self.assertTrue(all(r[4] in FINAL for r in self.db_rows()))
        cov = coverage(self.rec.store, self.cfg, self.clock())
        m = cov["venues"]["Hyperliquid"]["markets"]["BTC"]
        self.assertNotIn("PENDING", m["comparison_statuses"])
        self.assertEqual(m["comparisons"], len(self.db_rows("Hyperliquid")))


class LegacyRepairTests(Base):
    venues = ["Hyperliquid", "Lighter"]

    def test_a_database_written_before_the_fix_is_repaired_from_the_jsonl(self):
        self.boot()
        self.watch(4 * 60)
        good = self.assert_unique()
        root = self.cfg["root"]
        db = self.rec.store.db
        raw = db.execute("select count(*) from raw").fetchone()[0]
        records = db.execute("select count(*) from records").fetchone()[0]
        bodies = [json.loads(r[0]) for r in db.execute("select body from comparisons")]
        # What the old code left: the old table, keyed on the announced time, with
        # every re-match of a null-announced comparison stored again.
        db.execute("drop table comparisons")
        db.execute(
            "create table comparisons(venue text, market_id text, settled_at real,"
            " asset text, status text, body text, at real,"
            " primary key(venue, market_id, settled_at))"
        )
        for _ in range(4):
            for c in bodies:
                db.execute(
                    "insert or replace into comparisons values(?,?,?,?,?,?,?)",
                    (
                        c["venue"],
                        str(c["market_id"]),
                        c["expected_settlement_at"],
                        c["asset"],
                        c["comparison_status"],
                        json.dumps(c),
                        0,
                    ),
                )
        db.commit()
        legacy_rows = db.execute("select count(*) from comparisons").fetchone()[0]
        self.assertEqual(legacy_rows, 4 * good)  # the bug: nulls never collide
        self.rec.store.close()
        self.rec = None
        store = Store(root)
        try:
            self.assertIsNotNone(store.repair)
            self.assertEqual(store.repair["legacy_rows"], legacy_rows)
            self.assertEqual(store.repair["rebuilt_rows"], good)
            self.assertEqual(store.comparison_counts(), (good, good))
            self.assertEqual(comparison_index(store)["problems"], [])
            # The old rows are kept for inspection; nothing else changed.
            tables = {r[0] for r in store.db.execute("select name from sqlite_master")}
            self.assertIn(store.repair["legacy_table"], tables)
            self.assertEqual(
                store.db.execute("select count(*) from raw").fetchone()[0], raw
            )
            self.assertEqual(
                store.db.execute("select count(*) from records").fetchone()[0], records
            )
            # Opening again does not repair twice.
            store.close()
            store = Store(root)
            self.assertIsNone(store.repair)
            self.assertTrue(consistency(store, self.cfg)["ok"])
        finally:
            store.close()

    def test_a_fresh_database_needs_no_repair(self):
        store = Store(self.cfg["root"])
        try:
            self.assertIsNone(store.repair)
            cols = [r[1] for r in store.db.execute("pragma table_info(comparisons)")]
            self.assertIn("expected_settlement_at", cols)
            notnull = {
                r[1]: r[3] for r in store.db.execute("pragma table_info(comparisons)")
            }
            self.assertEqual(notnull["settled_at"], 1)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
