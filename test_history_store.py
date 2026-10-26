import os, tempfile, threading, unittest
from unittest import mock
import bot, history_store
from history_store import HistoryKeeper, HistoryStore

T0 = 3_000 * 86400


class Source:
    def __init__(self):
        self.calls = []
        self.published = {}
        self.fail = set()
        self.list = [
            ("Binance", "BTC", "perp", 28800, "trade"),
            ("Lighter", "BTC", "perp", 3600, "trade"),
        ]

    def markets(self):
        return list(self.list)

    def candles(self, v, a, kind, start, end):
        self.calls.append(("candles", v, start, end))
        if v in self.fail:
            raise ValueError("kapalı")
        return {t: 100.0 + (t % 7) for t in range(start, end, 3600)}

    def funding(self, v, a, start):
        self.calls.append(("funding", v, start))
        step = 28800 if v == "Binance" else 3600
        first = int(start) - int(start) % step + step
        return [
            dict(ts=t, rate=0.0001, value=None)
            for t in range(first, int(bot.now()) + 1, step)
        ]

    def publish(self, v, a, kind, prices, rates, interval, price_type):
        self.published[(v, a, kind)] = (prices, rates, interval, price_type)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "h.db")
        self.store = bot.Store(self.path)
        self.clock = T0
        for mod in (bot, history_store):
            p = mock.patch.object(mod, "now", lambda: self.clock)
            p.start()
            self.addCleanup(p.stop)
        self.src = Source()
        self.keeper = HistoryKeeper(
            self.store, self.src, threading.Event(), sleep=lambda s: None
        )

    def tearDown(self):
        self.store.db.close()
        self.dir.cleanup()

    def test_first_pass_fetches_thirty_days_and_publishes(self):
        self.assertEqual(self.keeper.run_once(), 2)
        prices, rates, interval, _ = self.src.published[("Binance", "BTC", "perp")]
        self.assertEqual(len(prices), 30 * 24)
        self.assertEqual(len(rates), 30 * 3)
        self.assertEqual(interval, 28800)
        first = [c for c in self.src.calls if c[0] == "candles" and c[1] == "Binance"][
            0
        ]
        self.assertEqual(first[2], bot.hour(T0) - 30 * 86400)

    def test_later_pass_fetches_only_new_candles(self):
        self.keeper.run_once()
        self.src.calls.clear()
        self.keeper.run_once()
        # Not due yet: nothing is requested again within the refresh window.
        self.assertEqual(self.src.calls, [])
        self.clock = T0 + history_store.REFRESH_SECONDS + 3 * 3600
        self.keeper.run_once()
        c = [x for x in self.src.calls if x[0] == "candles" and x[1] == "Binance"][0]
        self.assertEqual(c[2], bot.hour(T0))  # resumes after the last stored hour
        prices = self.src.published[("Binance", "BTC", "perp")][0]
        self.assertEqual(max(prices), bot.hour(self.clock) - 3600)

    def test_restart_publishes_stored_history_without_requests(self):
        self.keeper.run_once()
        self.store.db.close()
        self.store = bot.Store(self.path)
        src = Source()
        again = HistoryKeeper(self.store, src, threading.Event(), sleep=lambda s: None)
        again.publish_stored()
        self.assertEqual(src.calls, [])
        self.assertEqual(len(src.published[("Lighter", "BTC", "perp")][0]), 30 * 24)
        self.assertEqual(len(src.published[("Lighter", "BTC", "perp")][1]), 30 * 24)

    def test_failure_is_recorded_and_retried_later(self):
        self.src.fail.add("Lighter")
        self.keeper.run_once()
        st = self.keeper.status()
        self.assertEqual((st["synced"], st["failed"]), (1, 1))
        self.src.fail.clear()
        self.src.calls.clear()
        self.keeper.run_once()
        self.assertEqual(self.src.calls, [])
        self.clock += history_store.RETRY_SECONDS
        self.keeper.run_once()
        self.assertTrue(any(c[1] == "Lighter" for c in self.src.calls))
        self.assertEqual(self.keeper.status()["failed"], 0)

    def test_old_rows_are_pruned(self):
        db = HistoryStore(self.store)
        old = bot.hour(T0) - 40 * 86400
        db.add_candles("X", "BTC", "perp", {old: 1.0, bot.hour(T0) - 3600: 2.0})
        self.assertEqual(list(db.candles("X", "BTC")), [bot.hour(T0) - 3600])


if __name__ == "__main__":
    unittest.main()
