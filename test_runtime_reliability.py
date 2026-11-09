"""MOCK/isolated SQLite regressions for the paper data and decision pipeline."""
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import bot
import capabilities
from history_store import HistoryKeeper
from test_history_store import Source, T0


class RuntimeReliabilityTests(unittest.TestCase):
    def test_paper_book_read_does_not_wait_for_background_scan_lock(self):
        from wide_markets import WideMarkets
        from types import SimpleNamespace
        markets = WideMarkets(None, ["BTC"], threading.Event())
        markets.rows["Hyperliquid"] = [dict(asset="BTC", kind="perp", symbol="BTC/USDC:USDC")]
        ex = SimpleNamespace(fetch_order_book=lambda *a:dict(bids=[[100,2]],asks=[[101,2]]),
                             market=lambda s:dict(contractSize=1), neutralbasis_audit=None)
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        def hold_scan():
            with markets.client_locks["Hyperliquid"]:
                entered.set()
                release.wait(3)
        blocker=threading.Thread(target=hold_scan)
        blocker.start()
        self.assertTrue(entered.wait(1))
        result=[]
        def read_book():
            try:
                result.append(markets.book("Hyperliquid", "BTC"))
            finally:
                done.set()
        with mock.patch.object(markets, "client", return_value=ex) as client:
            reader=threading.Thread(target=read_book)
            reader.start()
            completed=done.wait(1)
            release.set()
            blocker.join(3)
            reader.join(3)
            self.assertTrue(completed, "Decision read queued behind the backfill")
            client.assert_called_once_with("Hyperliquid", False, decision=True)
        self.assertEqual(result[0]["bids"], [(100.0,2.0)])

    def test_active_paper_retention_monitor_never_prunes(self):
        import retention
        stop=threading.Event()
        worker=retention.Retention(None,stop)
        with mock.patch.object(worker,"run_once",side_effect=AssertionError("prune during PAPER")) as prune, mock.patch.object(retention,"size",return_value=123):
            worker.start(monitor_only=True)
            try:
                for _ in range(100):
                    if worker.status.get("status")=="monitoring": break
                    threading.Event().wait(.01)
                self.assertEqual(worker.status["status"],"monitoring")
                prune.assert_not_called()
            finally:
                stop.set()
                worker.thread.join(2)

    def test_dashboard_events_do_not_include_large_analysis_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(os.path.join(directory, "paper.sqlite3"))
            try:
                body = dict(message="MOCK decision", asset="BTC", net=-2,
                            reasons=["cost exceeds edge"], analysis={"prices": [100]*10000})
                store.event("TOP5_RISK", body)
                brief = store.recent("events", 1, brief=True)[0]["body"]
                self.assertEqual(brief["reasons"], body["reasons"])
                self.assertEqual(brief["net"], -2)
                self.assertNotIn("analysis", brief)
                self.assertEqual(store.recent("events", 1)[0]["body"], body)
                plan = store.db.execute("explain query plan select * from events order by ts desc limit 60").fetchall()
                self.assertIn("events_time", str(plan))
            finally:
                store.db.close()

    def test_configured_history_window_does_not_secretly_require_thirty_days(self):
        with tempfile.TemporaryDirectory() as directory:
            store = bot.Store(os.path.join(directory, "paper.sqlite3"))
            try:
                src = Source()
                src.list = [("Lighter", "BTC", "perp", 3600, "trade")]
                src.since["Lighter"] = T0-10*86400
                with mock.patch("bot.now", return_value=T0), mock.patch("history_store.now", return_value=T0):
                    keeper = HistoryKeeper(store, src, threading.Event(), sleep=lambda _:None)
                    keeper.run_once()
                state=keeper.market_state("Lighter", "BTC")
                self.assertFalse(state["complete"])
                self.assertTrue(state["coverage"]["windows"]["7"])
                self.assertFalse(state["coverage"]["windows"]["30"])
                reg=capabilities.Registry()
                reg.observe_history({("Lighter","BTC","perp"):state}, T0, minimum_days=7)
                self.assertEqual(reg.history_counts(T0)["Lighter"]["complete"], 1)
                # Tightening the configuration cannot reuse the shorter proof.
                reg.observe_history({}, T0, minimum_days=30)
                self.assertEqual(reg.history_counts(T0)["Lighter"]["complete"], 0)
                reg.observe_history({("Lighter","BTC","perp"):state}, T0, minimum_days=30)
                self.assertFalse(reg.market_history["Lighter|BTC"][1])
            finally:
                store.db.close()


if __name__ == "__main__":
    unittest.main()
