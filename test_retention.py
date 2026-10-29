import json, os, tempfile, unittest, zlib
import bot, retention

T0 = 2_000_000_000


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = bot.Store(os.path.join(self.dir.name, "r.db"))

    def tearDown(self):
        self.store.db.close()
        self.dir.cleanup()

    def audit(self, ts):
        return self.store.audit("https://x/" + str(ts), b"x" * 5000, ts, {})

    def raw(self, i):
        r = self.store.db.execute("select raw from audit where id=?", (i,)).fetchone()
        return None if r is None else ("gone" if r[0] is None else "kept")

    def test_old_bodies_dropped_rows_kept_referenced_untouched(self):
        fresh = self.audit(T0 - 3600)
        old = self.audit(T0 - 5 * 86400)
        ancient = self.audit(T0 - 40 * 86400)
        held = self.audit(T0 - 40 * 86400)
        paid = self.audit(T0 - 10 * 86400)
        self.store.put(
            "wide_closed",
            [dict(legs=[dict(audit_id=held, exit_audit_id=None)], audit_ids=[])],
        )
        self.store.db.execute(
            "insert into payments values(?,?,?)",
            ("k", T0, json.dumps(dict(rate=dict(audit_id=paid)))),
        )
        self.store.db.execute(
            "insert into marks values('A','BTC',?,1.0)", (T0 - 20 * 86400,)
        )
        self.store.db.execute("insert into marks values('A','BTC',?,1.0)", (T0,))
        self.store.db.commit()
        out = retention.prune(self.store, T0)
        self.assertEqual(self.raw(fresh), "kept")
        self.assertEqual(self.raw(old), "gone")
        self.assertIsNone(self.raw(ancient))
        self.assertEqual((self.raw(held), self.raw(paid)), ("kept", "kept"))
        self.assertEqual((out["audit_stripped"], out["audit_dropped"]), (2, 1))
        self.assertEqual(out["marks"], 1)
        sha = self.store.db.execute(
            "select sha from audit where id=?", (old,)
        ).fetchone()
        self.assertTrue(sha[0])

    def test_offline_maintenance_then_background_never_full_vacuums(self):
        for i in range(300):
            self.store.audit("u", os.urandom(4000), T0 - 10 * 86400, {})
        self.store.db.close()
        path = os.path.join(self.dir.name, "r.db")
        before = os.path.getsize(path)
        with unittest.mock.patch.object(retention, "now", lambda: T0):
            self.assertTrue(retention.maintain(path, log=lambda m: None))
            self.assertFalse(retention.maintain(path, log=lambda m: None))
        self.assertLess(os.path.getsize(path), before / 3)
        self.store = bot.Store(path)
        self.assertTrue(retention.incremental(self.store))
        for i in range(300):
            self.store.audit("u", os.urandom(4000), T0 - 10 * 86400, {})
        r = retention.Retention(self.store, None)
        grown = retention.size(self.store)
        with unittest.mock.patch.object(retention, "now", lambda: T0):
            st = r.run_once()
        self.assertTrue(st["done"])
        self.assertFalse(st["vacuum_pending"])
        self.assertLess(st["bytes"], grown / 3)

    def test_background_run_without_maintenance_does_not_vacuum(self):
        self.audit(T0 - 10 * 86400)
        r = retention.Retention(self.store, None)
        with unittest.mock.patch.object(retention, "now", lambda: T0):
            st = r.run_once()
        self.assertTrue(st["vacuum_pending"])
        self.assertEqual(st["audit_dropped"], 0)
        self.assertEqual(self.raw(1), "gone")

    def test_deadline_stops_in_batches(self):
        for i in range(12):
            self.audit(T0 - 5 * 86400)
        clock = iter(range(100))
        with unittest.mock.patch.object(
            retention, "BATCH", 5
        ), unittest.mock.patch.object(retention.time, "monotonic", lambda: next(clock)):
            out = retention.prune(self.store, T0, deadline=0.5)
        self.assertFalse(out["done"])
        self.assertEqual(out["audit_stripped"], 5)
        out = retention.prune(self.store, T0)
        self.assertTrue(out["done"])
        self.assertEqual(out["audit_stripped"], 7)

    def test_run_resumes_where_it_stopped(self):
        ids = [self.audit(T0 - 5 * 86400) for i in range(6)]
        progress = {}
        retention.prune(self.store, T0, progress=progress)
        self.assertEqual(progress, dict(strip=ids[-1] + 1, drop=ids[0]))
        newer = self.audit(T0 - 4 * 86400)
        # The rows already walked are not read again.
        seen = []
        self.store.db.set_trace_callback(seen.append)
        out = retention.prune(self.store, T0, progress=progress)
        self.store.db.set_trace_callback(None)
        self.assertEqual(out["audit_stripped"], 1)
        self.assertEqual(self.raw(newer), "gone")
        windows = [q for q in seen if "where id>=" in q]
        self.assertTrue(windows)
        self.assertTrue(all(f"id>={newer}" in q for q in windows if "raw is" in q))

    def test_late_written_row_is_stripped_on_a_later_run(self):
        first = self.audit(T0 - 5 * 86400)
        late = self.audit(T0 - 3600)  # a newer time behind older rows' ids
        rest = [self.audit(T0 - 5 * 86400) for i in range(3)]
        progress = {}
        out = retention.prune(self.store, T0, progress=progress)
        self.assertEqual(out["audit_stripped"], 4)
        self.assertEqual((self.raw(first), self.raw(late)), ("gone", "kept"))
        # The next run starts again from the row that was not due yet.
        self.assertEqual(progress["strip"], late)
        out = retention.prune(self.store, T0 + 3 * 86400, progress=progress)
        self.assertEqual((out["audit_stripped"], self.raw(late)), (1, "gone"))
        self.assertEqual(progress["strip"], rest[-1] + 1)

    def steps(self, recent):
        """SQLite work (VM steps) of one prune over 5 due rows and `recent` newer ones."""
        self.store.db.execute("delete from audit")
        for i in range(5):
            self.audit(T0 - 5 * 86400)
        self.store.db.executemany(
            "insert into audit(ts,url,sha,raw) values(?,?,?,?)",
            [(T0 - 60, "u", "s", b"x" * 500)] * recent,
        )
        self.store.db.commit()
        n = [0]

        def tick():
            n[0] += 1

        self.store.db.set_progress_handler(tick, 100)
        try:
            out = retention.prune(self.store, T0)
        finally:
            self.store.db.set_progress_handler(None, 100)
        self.assertEqual(out["audit_stripped"], 5)
        return n[0]

    def test_work_does_not_grow_with_rows_not_due(self):
        # Rows not yet due are never walked: holds on the shared lock stay short however
        # many recent responses the table holds.
        small, large = self.steps(200), self.steps(5000)
        self.assertLess(large, small * 1.5 + 20)

    def test_background_progress_survives_a_restart(self):
        for i in range(3):
            self.audit(T0 - 5 * 86400)
        with unittest.mock.patch.object(retention, "now", lambda: T0):
            retention.Retention(self.store, None).run_once()
        saved = self.store.get("retention_progress", None)
        self.assertEqual(saved["strip"], 4)
        # The saved positions are not mistaken for referenced audit ids.
        self.assertNotIn(4, retention.referenced(self.store))
        r = retention.Retention(self.store, None)
        with unittest.mock.patch.object(retention, "now", lambda: T0):
            r.run_once()
        self.assertEqual(r.progress["strip"], 4)


import unittest.mock

if __name__ == "__main__":
    unittest.main()
