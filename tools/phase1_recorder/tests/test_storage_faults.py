"""Fault injection for the Phase-1 recorder store: a normalized line and its index row
must never disagree in the dangerous direction (an index row whose line is missing).
MOCK records in temporary folders; no recorder or engine data is touched."""

import builtins, contextlib, os, sqlite3, tempfile, unittest
from unittest import mock

from tools.phase1_recorder import storage
from tools.phase1_recorder.storage import Store

AT = 1_800_000_000.0


def rec(n):
    # MOCK funding snapshot; only the fields record_key needs, plus a marker.
    return dict(venue="Extended", market_id="BTC-USD", received_at=AT + n, mock=True, n=n)


def key(n):
    return "BTC-USD|%d" % int(AT + n)


def indexed_keys(folder):
    with contextlib.closing(sqlite3.connect(os.path.join(folder, "recorder.sqlite3"))) as c:
        return sorted(k for (k,) in c.execute("select key from records"))


class FailingOpen:
    """Wraps open(): append-mode files write `keep` characters and then fail, once."""

    def __init__(self, keep=None):
        self.keep = keep
        self.armed = True
        self.real = builtins.open

    def __call__(self, path, mode="r", *a, **kw):
        f = self.real(path, mode, *a, **kw)
        if not (self.armed and "a" in mode):
            return f
        outer = self

        class Wrapped:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                f.close()
                return False

            def write(self, data):
                outer.armed = False
                if outer.keep:
                    f.write(data[: outer.keep])
                    f.flush()
                raise OSError(28, "MOCK no space left on device")

        return Wrapped()


class StorageFaultTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.folder = self.dir.name
        self.store = Store(self.folder, batch_records=1000, batch_seconds=1e9)

    def tearDown(self):
        with contextlib.suppress(Exception):
            self.store.db.close()
        self.dir.cleanup()

    def put(self, n):
        self.assertTrue(self.store.put_record("funding_snapshot", "Extended", key(n), rec(n), AT))

    def test_disk_full_on_flush_commits_no_index_row_and_a_retry_writes_once(self):
        for n in range(3):
            self.put(n)
        with mock.patch.object(storage, "open", FailingOpen(), create=True):
            with self.assertRaises(OSError):
                self.store.flush()
        self.assertEqual(indexed_keys(self.folder), [])  # nothing claims a missing line
        self.assertEqual(self.store.write_stats["errors"], 1)
        self.store.flush()  # disk has space again
        self.assertEqual(indexed_keys(self.folder), sorted(key(n) for n in range(3)))
        rows = self.store.records("funding_snapshot", "Extended")
        self.assertEqual(sorted(r["n"] for r in rows), [0, 1, 2])
        self.assertEqual(self.store.read_problems, set())

    def test_partial_line_is_isolated_reported_and_not_duplicated(self):
        for n in range(2):
            self.put(n)
        with mock.patch.object(storage, "open", FailingOpen(keep=25), create=True):
            with self.assertRaises(OSError):
                self.store.flush()
        self.assertEqual(indexed_keys(self.folder), [])
        self.store.flush()
        rows = list(self.store.iter_records("funding_snapshot", "Extended"))
        self.assertEqual(sorted(r["n"] for r in rows), [0, 1])  # each once, none guessed
        self.assertEqual(len(self.store.read_problems), 1)  # the cut line is reported
        self.assertEqual(len(indexed_keys(self.folder)), 2)

    def test_commit_failure_after_the_lines_keeps_the_index_pending(self):
        self.put(0)
        real = self.store.db

        class CommitFails:
            def __init__(self):
                self.failed = False

            def __getattr__(self, name):
                return getattr(real, name)

            def commit(self):
                if not self.failed:
                    self.failed = True
                    raise sqlite3.OperationalError("MOCK database is locked")
                return real.commit()

        self.store.db = CommitFails()
        with self.assertRaises(sqlite3.OperationalError):
            self.store.flush()
        self.assertEqual(indexed_keys(self.folder), [])  # line written, row not yet
        self.store.flush()
        self.assertEqual(indexed_keys(self.folder), [key(0)])
        self.store.db = real
        self.assertEqual(len(list(self.store.iter_records("funding_snapshot", "Extended"))), 1)

    def test_crash_between_line_and_commit_replays_idempotently(self):
        self.put(0)
        self.put(1)
        # The lines reach the file, then the process dies before the commit.
        self.store._write_lines()
        self.store.db.close()
        self.store = Store(self.folder, batch_records=1000, batch_seconds=1e9)
        self.assertEqual(indexed_keys(self.folder), [])
        self.assertFalse(self.store.has_record("funding_snapshot", "Extended", key(0)))
        # The recorder fetches again and puts the same records.
        self.put(0)
        self.put(1)
        self.store.flush()
        raw = list(self.store.iter_records("funding_snapshot", "Extended"))
        self.assertEqual(len(raw), 4)  # documented: a replayed line appears twice
        rows = self.store.records("funding_snapshot", "Extended")
        self.assertEqual(sorted(r["n"] for r in rows), [0, 1])  # readers keep one per key
        self.assertEqual(indexed_keys(self.folder), [key(0), key(1)])

    def test_a_key_is_written_once(self):
        self.put(0)
        self.assertFalse(
            self.store.put_record("funding_snapshot", "Extended", key(0), rec(0), AT)
        )
        self.store.flush()
        self.assertEqual(len(list(self.store.iter_records("funding_snapshot", "Extended"))), 1)

    def test_close_after_a_failed_flush_rolls_back_and_releases_the_file(self):
        self.put(0)
        with mock.patch.object(storage, "open", FailingOpen(), create=True):
            with self.assertRaises(OSError):
                self.store.close()
        self.assertTrue(self.store.closed)
        self.assertEqual(indexed_keys(self.folder), [])
        # The folder can be reopened (no handle left open on Windows).
        again = Store(self.folder)
        again.close()


if __name__ == "__main__":
    unittest.main()
