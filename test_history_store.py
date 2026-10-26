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
        self.since = {}
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
        start = max(start, self.since.get(v, 0))
        return {t: 100.0 + (t % 7) for t in range(start, end, 3600)}

    def funding(self, v, a, start):
        self.calls.append(("funding", v, start))
        step = 28800 if v == "Binance" else 3600
        start = max(start, self.since.get(v, 0))
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

    def test_short_first_fill_is_completed_backwards_later(self):
        # The first fill only reached three days back (a cut-short or failed page).
        self.src.since["Binance"] = bot.hour(T0) - 3 * 86400
        self.keeper.run_once()
        key = ("Binance", "BTC", "perp")
        self.assertEqual(len(self.src.published[key][0]), 3 * 24)
        self.src.since.clear()
        self.src.calls.clear()
        # The regular refresh does not go back before BACKFILL_SECONDS.
        self.clock = T0 + history_store.REFRESH_SECONDS
        self.keeper.run_once()
        back = [c for c in self.src.calls if c[1] == "Binance" and c[2] < bot.hour(T0)]
        self.assertEqual(back, [])
        self.clock = T0 + history_store.BACKFILL_SECONDS
        self.keeper.run_once()
        prices, rates, _, _ = self.src.published[key]
        self.assertGreaterEqual(len(prices), 30 * 24 - 1)
        self.assertGreaterEqual(len(rates), 30 * 3 - 1)
        cov = self.keeper.coverage("Binance", "BTC")
        self.assertLessEqual(
            cov["candles_from"], bot.hour(self.clock) - 30 * 86400 + 3600
        )

    def test_venue_with_short_history_is_not_asked_back_every_pass(self):
        self.src.since["Lighter"] = bot.hour(T0) - 3 * 86400
        self.keeper.run_once()
        for i in range(1, 31):
            self.clock = T0 + i * history_store.REFRESH_SECONDS
            self.keeper.run_once()
        back = [
            c
            for c in self.src.calls
            if c[0] == "candles"
            and c[1] == "Lighter"
            and c[2] < bot.hour(T0) - 3 * 86400
        ]
        # One full-window first fill and one backwards retry in 15 hours of passes.
        self.assertEqual(len(back), 2)
        self.assertEqual(
            self.keeper.coverage("Lighter", "BTC")["candles_from"],
            bot.hour(T0) - 3 * 86400,
        )

    def test_interior_gap_is_refetched(self):
        self.keeper.run_once()
        db = self.keeper.db
        hole = range(bot.hour(T0) - 10 * 86400, bot.hour(T0) - 9 * 86400, 3600)
        with self.store.lock:
            self.store.db.execute(
                "delete from hist_candles where venue='Binance' and ts>=? and ts<?",
                (hole.start, hole.stop),
            )
            self.store.db.execute(
                "delete from hist_funding where venue='Binance' and ts>=? and ts<?",
                (hole.start, hole.stop),
            )
        self.assertEqual(len(db.candles("Binance", "BTC")), 30 * 24 - 24)
        self.src.calls.clear()
        self.clock = T0 + history_store.BACKFILL_SECONDS
        self.keeper.run_once()
        spans = [c for c in self.src.calls if c[0] == "candles" and c[1] == "Binance"]
        self.assertIn(("candles", "Binance", hole.start, hole.stop), spans)
        prices = db.candles("Binance", "BTC", start=bot.hour(T0) - 29 * 86400)
        self.assertTrue(all(t in prices for t in hole))
        st = self.keeper.market_state("Binance", "BTC")
        self.assertTrue(st["complete"], st)
        self.assertGreaterEqual(st["coverage"]["funding"], 89)

    def test_candle_failure_still_syncs_and_publishes_funding(self):
        class Half(Source):
            def candles(self, v, a, kind, start, end):
                raise ValueError("ohlcv kapalı")

        src = Half()
        keeper = HistoryKeeper(self.store, src, threading.Event(), sleep=lambda s: None)
        keeper.run_once()
        st = keeper.market_state("Binance", "BTC")
        self.assertFalse(st["ok"])
        self.assertIn("Mum: ohlcv kapalı", st["error"])
        self.assertEqual(st["coverage"]["funding"], 30 * 3)
        self.assertFalse(st["complete"])
        _, rates, _, _ = src.published[("Binance", "BTC", "perp")]
        self.assertEqual(len(rates), 30 * 3)

    def test_partial_coverage_is_reported(self):
        self.src.since["Lighter"] = bot.hour(T0) - 3 * 86400
        self.keeper.run_once()
        cov = self.keeper.market_state("Lighter", "BTC")["coverage"]
        self.assertEqual((cov["candles"], cov["candle_hours"]), (72, 720))
        self.assertEqual(cov["funding_expected"], 720)
        st = self.keeper.status()
        self.assertEqual(st["complete"], 1)

    def test_old_rows_are_pruned(self):
        db = HistoryStore(self.store)
        old = bot.hour(T0) - 40 * 86400
        db.add_candles("X", "BTC", "perp", {old: 1.0, bot.hour(T0) - 3600: 2.0})
        self.assertEqual(list(db.candles("X", "BTC")), [bot.hour(T0) - 3600])


class DeskSourceTests(unittest.TestCase):
    def engine(self):
        class Wide:
            def snapshot(self, max_age=180):
                return [], {}

            def universe_snapshot(self, max_age=300):
                return {"Bybit": {"IRYS": dict(interval=14400)}}

            def candles_since(self, v, a, kind, start, end):
                return {start: 1.0}

        class Matrix:
            def get(self):
                raise ValueError("not yet")

        e = type("E", (), {})()
        e.cfg = {"symbols": ["BTC"]}
        e.feed = type("F", (), {"meta": {}})()
        e.wide = Wide()
        e.matrix = Matrix()
        e.wide_paper = type(
            "W", (), {"history_needs": lambda self: [("Bybit", "IRYS"), ("Gone", "X")]}
        )()
        return e

    def test_paper_candidate_legs_of_any_asset_get_price_history_first(self):
        from desk_analytics import DeskHistorySource

        src = DeskHistorySource(self.engine())
        self.assertEqual(src.markets(), [("Bybit", "IRYS", "perp", 14400, "trade")])
        self.assertEqual(src.candles("Bybit", "IRYS", "perp", 3600, 7200), {3600: 1.0})


if __name__ == "__main__":
    unittest.main()
