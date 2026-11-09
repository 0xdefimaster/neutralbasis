"""NeutralBasis P0/P1 regression tests. PAPER ONLY; no network/trading."""

import contextlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import zero_trade
from desk_analytics import DeskEngine
from tools.phase1_recorder.storage import Store


class WhyNoTradeTests(unittest.TestCase):
    def make_engine(self, now, ended=None, running=True, busy=False):
        # Uses the actual method on a minimal engine-like stub. No database/network.
        diag = SimpleNamespace(
            cycles=[{"at": now - 30, "ended": ended if ended is not None else now - 25}],
            report=lambda at: {"verdict": {"key": "DATA", "text": "Veri kalitesi yetersiz."}},
        )
        lock = threading.Lock()
        if busy:
            lock.acquire()
        obj = SimpleNamespace(
            wide_paper=SimpleNamespace(diag=diag),
            lock=lock,
            running=running,
        )
        return obj, lock

    def test_recent_cycle_does_not_get_false_stale_verdict(self):
        obj, _ = self.make_engine(now=1000, ended=990)
        with patch("desk_analytics.now", return_value=1000):
            report = DeskEngine.why_no_trade(obj)
        self.assertEqual(report["verdict"]["key"], "DATA")
        self.assertEqual(report["engine_state"]["last_cycle_at"], 990)
        self.assertEqual(report["engine_state"]["last_cycle_age_seconds"], 10)

    def test_stale_cycle_is_recognized(self):
        obj, _ = self.make_engine(now=5000, ended=1000)
        with patch("desk_analytics.now", return_value=5000):
            report = DeskEngine.why_no_trade(obj)
        self.assertEqual(report["verdict"]["key"], "SCAN_STALE")
        self.assertEqual(report["verdict"]["evidence"]["age_seconds"], 4000)

    def test_stopped_worker_remains_stopped(self):
        obj, _ = self.make_engine(now=5000, ended=1000, running=False)
        with patch("desk_analytics.now", return_value=5000):
            report = DeskEngine.why_no_trade(obj)
        self.assertEqual(report["verdict"]["key"], "STOPPED")

    def test_busy_engine_is_reported_without_false_data(self):
        obj, lock = self.make_engine(now=5000, ended=1000, busy=True)
        try:
            with patch("desk_analytics.now", return_value=5000):
                report = DeskEngine.why_no_trade(obj)
            self.assertEqual(report["verdict"]["key"], "SCAN_BUSY")
        finally:
            lock.release()


class ReasonCodeTests(unittest.TestCase):
    def test_orderbook_desynchronization(self):
        self.assertEqual(
            zero_trade.code_of("İki bacak defterleri güncel/senkron değil"),
            ("ORDERBOOK_DESYNC", "DATA"),
        )

    def test_rfq_stream_missing(self):
        self.assertEqual(
            zero_trade.code_of("RFQ akışı bu piyasa için başlatılmadı"),
            ("RFQ_STREAM_UNAVAILABLE", "DATA"),
        )

    def test_rfq_429_is_rate_limited(self):
        self.assertEqual(
            zero_trade.code_of("RFQ akışı bağlı değil; WebSocket 429 Too Many Requests"),
            ("API_RATE_LIMIT", "DATA"),
        )

    def test_new_codes_aggregate_as_data(self):
        # Verifies that CAT categories also understand the new return codes.
        for code in ("ORDERBOOK_DESYNC", "RFQ_STREAM_UNAVAILABLE", "API_RATE_LIMIT"):
            self.assertEqual(zero_trade.CATEGORY[code], "DATA")


class RecorderCloseTests(unittest.TestCase):
    def test_failed_flush_rolls_back_index(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder, batch_records=500, batch_seconds=1000)
            store.db.execute("CREATE TABLE hotfix_test(x INTEGER)")
            store.db.commit()
            store.db.execute("INSERT INTO hotfix_test VALUES (123)")
            with patch.object(store, "flush", side_effect=OSError("MOCK JSONL write failed")):
                with self.assertRaises(OSError):
                    store.close()
            self.assertTrue(store.closed)
            with contextlib.closing(sqlite3.connect(str(Path(folder) / "recorder.sqlite3"))) as check:
                count = check.execute("SELECT COUNT(*) FROM hotfix_test").fetchone()[0]
                self.assertEqual(count, 0)

    def test_successful_close_commits_index(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder, batch_records=500, batch_seconds=1000)
            store.db.execute("CREATE TABLE hotfix_test(x INTEGER)")
            store.db.commit()
            store.db.execute("INSERT INTO hotfix_test VALUES (456)")
            store.close()
            with contextlib.closing(sqlite3.connect(str(Path(folder) / "recorder.sqlite3"))) as check:
                val = check.execute("SELECT x FROM hotfix_test").fetchone()[0]
                self.assertEqual(val, 456)


class PairIdentityGuardTests(unittest.TestCase):
    def test_wildly_incompatible_price_scales_never_rank_as_funding(self):
        # An extreme same-ticker price mismatch must not consume a deep-check slot.
        from wide_paper import rank
        at = 1800000000
        a = dict(venue="A", asset="PURR", mark=1.0, rate=0.001,
                 raw_rate=0.001, fee=0.0001, interval=3600)
        b = dict(venue="B", asset="PURR", mark=90.0, rate=0.002,
                 raw_rate=0.002, fee=0.0001, interval=3600)
        with patch("wide_paper.usable_rows", return_value={"PURR": {"A": a, "B": b}}), \
             patch("wide_paper.payments_within", return_value=24):
            self.assertEqual(rank([], {"wide_horizon": 24}, at), [])


if __name__ == "__main__":
    unittest.main()
