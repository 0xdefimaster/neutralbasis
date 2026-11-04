"""Resource lifecycle: every Recorder owns and releases its own log file and database,
so a folder can be renamed or deleted once the recorder is closed (Windows refuses
to delete a file a process still holds open: WinError 32).
MOCK responses only (tests/fakes.py); never reported as market evidence."""

import logging
import os
import tempfile
import unittest
from unittest import mock

from tools.phase1_recorder import recorder as recorder_mod
from tools.phase1_recorder.config import load
from tools.phase1_recorder.recorder import LIVE, Recorder
from tools.phase1_recorder.storage import Store

from .fakes import Clock, FakeVenues


def held_under(path):
    """Files under `path` this process holds open, or None where the platform does
    not tell (no /proc). On Windows the rename/delete checks below are the proof."""
    fds = "/proc/self/fd"
    if not os.path.isdir(fds):
        return None
    prefix = os.path.realpath(path).rstrip(os.sep) + os.sep
    out = []
    for fd in os.listdir(fds):
        try:
            target = os.readlink(os.path.join(fds, fd))
        except OSError:
            continue
        target = target.replace(" (deleted)", "")
        if target.startswith(prefix):
            out.append(target)
    return sorted(out)


def lines(path):
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()


class LifecycleBase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()

    def tempdir(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)  # a no-op if the test removed it already
        return d

    def cfg(self, d):
        return load(
            root=os.path.join(d.name, "phase1-evidence"),
            assets=["BTC"],
            venues=["Extended"],
        )

    def recorder(self, cfg):
        return Recorder(
            cfg,
            transport=FakeVenues(self.clock),
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def assertReleased(self, d):
        held = held_under(d.name)
        if held is not None:
            self.assertEqual(held, [])
        # The Windows failure mode: removing the folder while a file is open.
        d.cleanup()
        self.assertFalse(os.path.exists(d.name))


class RecorderLogTests(LifecycleBase):
    def test_consecutive_recorders_in_different_roots_each_write_their_own_log(self):
        d1, d2 = self.tempdir(), self.tempdir()
        c1, c2 = self.cfg(d1), self.cfg(d2)
        log1 = os.path.join(c1["root"], "logs", "recorder.log")
        log2 = os.path.join(c2["root"], "logs", "recorder.log")
        with self.recorder(c1) as r1:
            r1.run("once")
        first = lines(log1)
        self.assertTrue(any("Oturum başladı" in x for x in first))
        self.assertTrue(any("Oturum bitti" in x for x in first))
        with self.recorder(c2) as r2:
            self.assertIsNot(r2.log, r1.log)
            self.assertIsNot(r2.log_handler, r1.log_handler)
            self.assertEqual(os.path.realpath(log_path(r2)), os.path.realpath(log2))
            r2.run("once")
        # The second session went to the second root only.
        self.assertEqual(lines(log1), first)
        second = lines(log2)
        self.assertTrue(any("Oturum başladı" in x for x in second))
        self.assertTrue(any("Oturum bitti" in x for x in second))
        self.assertReleased(d1)
        self.assertReleased(d2)

    def test_two_open_recorders_never_share_a_handler(self):
        d1, d2 = self.tempdir(), self.tempdir()
        r1, r2 = self.recorder(self.cfg(d1)), self.recorder(self.cfg(d2))
        try:
            self.assertEqual(r1.log.handlers, [r1.log_handler])
            self.assertEqual(r2.log.handlers, [r2.log_handler])
            self.assertNotEqual(log_path(r1), log_path(r2))
        finally:
            r1.close()
            r2.close()

    def test_after_close_the_log_file_can_be_renamed_and_deleted(self):
        d = self.tempdir()
        cfg = self.cfg(d)
        rec = self.recorder(cfg)
        rec.run("once")
        handler = rec.log_handler
        rec.close()
        self.assertEqual(rec.log.handlers, [])
        self.assertIsNone(handler.stream)
        log = os.path.join(cfg["root"], "logs", "recorder.log")
        moved = log + ".old"
        os.rename(log, moved)
        os.remove(moved)
        os.rename(cfg["root"], cfg["root"] + "-moved")  # the whole root, too
        self.assertReleased(d)

    def test_close_is_idempotent_and_leaves_other_loggers_alone(self):
        d = self.tempdir()
        other = logging.getLogger("phase1")
        stream_handler = logging.StreamHandler()
        other.addHandler(stream_handler)
        self.addCleanup(other.removeHandler, stream_handler)
        rec = self.recorder(self.cfg(d))
        rec.run("once")
        rec.close()
        rec.close()
        self.assertTrue(rec.store.closed)
        self.assertIn(stream_handler, other.handlers)
        self.assertNotIn(rec.log_handler, other.handlers)
        self.assertReleased(d)

    def test_the_session_is_finished_before_the_log_is_closed(self):
        # Closing the handler must not cut the session short: `run` writes the end
        # of the session first; close only happens after it returns.
        d = self.tempdir()
        cfg = self.cfg(d)
        rec = self.recorder(cfg)
        self.assertEqual(rec.run("once"), "bitti")
        ended = rec.store.db.execute(
            "select ended, stopped_by from sessions"
        ).fetchall()
        self.assertEqual(len(ended), 1)
        self.assertIsNotNone(ended[0][0])
        self.assertEqual(ended[0][1], "bitti")
        rec.close()
        log = lines(os.path.join(cfg["root"], "logs", "recorder.log"))
        self.assertTrue(log[-1].endswith("Oturum bitti: bitti"))
        self.assertReleased(d)


class FailureReleaseTests(LifecycleBase):
    def test_a_failing_constructor_releases_the_log_and_database(self):
        d = self.tempdir()
        cfg = self.cfg(d)
        with mock.patch.object(
            recorder_mod, "make_client", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.recorder(cfg)
        self.assertReleased(d)

    def test_a_refused_data_source_releases_everything(self):
        d = self.tempdir()
        cfg = self.cfg(d)
        store = Store(cfg["root"])
        store.set_checkpoint("data_source", LIVE)
        store.close()
        with self.assertRaises(ValueError):
            self.recorder(cfg)
        self.assertReleased(d)

    def test_an_error_inside_the_with_block_still_releases(self):
        d = self.tempdir()
        with self.assertRaises(RuntimeError):
            with self.recorder(self.cfg(d)) as rec:
                rec.run("once")
                raise RuntimeError("after the session")
        self.assertTrue(rec.store.closed)
        self.assertReleased(d)

    def test_a_store_that_fails_to_open_does_not_hold_the_database(self):
        d = self.tempdir()
        root = os.path.join(d.name, "phase1-evidence")
        Store(root).close()
        with mock.patch.object(Store, "ensure_comparisons", side_effect=OSError("x")):
            with self.assertRaises(OSError):
                Store(root)
        self.assertReleased(d)


def log_path(rec):
    return rec.log_handler.baseFilename


if __name__ == "__main__":
    unittest.main()
