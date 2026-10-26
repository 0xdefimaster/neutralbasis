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

    def test_space_is_given_back(self):
        for i in range(300):
            self.store.audit("u", os.urandom(4000), T0 - 10 * 86400, {})
        r = retention.Retention(self.store, None)
        before = retention.size(self.store)
        with unittest.mock.patch.object(retention, "now", lambda: T0):
            st = r.run_once()
        self.assertTrue(st["vacuumed"])
        self.assertLess(st["bytes"], before / 3)
        mode = self.store.db.execute("pragma auto_vacuum").fetchone()[0]
        self.assertEqual(mode, 2)
        # Second run: already incremental, no full vacuum.
        with unittest.mock.patch.object(retention, "now", lambda: T0):
            self.assertFalse(r.run_once()["vacuumed"])


import unittest.mock

if __name__ == "__main__":
    unittest.main()
