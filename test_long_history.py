import tempfile, unittest, urllib.parse
from pathlib import Path
from unittest.mock import patch
import bot, risk_scan

END = 2_000 * 86400


class LongHistoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = bot.Store(Path(self.dir.name) / "t.db")
        self.feed = risk_scan.FiveFeed(self.store)
        self.feed.meta = {
            v: {"BTC": dict(id="7", price_type="trade")}
            for v in ("Lighter", "Extended", "RISEx")
        }
        self.calls = []
        patcher = patch.object(bot, "now", lambda: END)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.store.db.close()
        self.dir.cleanup()

    def fake(self, url, data=None, max_age=None):
        self.calls.append(url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if "candles?" in url and "lighter" in url:
            s, e = int(q["start_timestamp"]), int(q["end_timestamp"])
            return dict(c=[dict(t=t * 1000, c=100.0) for t in range(s, e, 3600)]), {
                "audit_id": 1
            }
        if "/trades?" in url:
            e = int(q["endTime"]) // 1000
            # Extended returns at most 750 candles ending at endTime.
            return dict(
                data=[dict(T=t * 1000, c=101.0) for t in range(e - 750 * 3600, e, 3600)]
            ), {"audit_id": 2}
        if "fundings?" in url:
            s, e = int(q["start_timestamp"]), int(q["end_timestamp"])
            return dict(
                fundings=[
                    dict(timestamp=t, rate="0.001", value="0.1", direction="long")
                    for t in range(s - s % 3600 + 3600, e + 1, 3600)
                ]
            ), {"audit_id": 3}
        if "/funding?" in url:
            s, e = int(q["startTime"]) // 1000, int(q["endTime"]) // 1000
            return dict(
                data=[
                    dict(T=t * 1000, f="0.00001")
                    for t in range(s - s % 3600 + 3600, e + 1, 3600)
                ]
            ), {"audit_id": 4}
        raise AssertionError(url)

    def test_ninety_days_of_native_candles_are_paged(self):
        start = END - 90 * 86400
        with patch.object(self.feed, "req", self.fake):
            for v in ("Lighter", "Extended"):
                out = self.feed.candles_range(v, "BTC", start, END)
                self.assertEqual(len(out["prices"]), 90 * 24, v)
                self.assertEqual(min(out["prices"]), start)
                self.assertEqual(max(out["prices"]), END - 3600)
                self.assertEqual(out["price_type"], "trade")
        # 15-day windows: 6 requests per venue keep every request inside row limits.
        self.assertEqual(len(self.calls), 12)

    def test_ninety_days_of_native_funding_deduplicated(self):
        start = END - 90 * 86400
        with patch.object(self.feed, "req", self.fake):
            for v in ("Lighter", "Extended"):
                rows = self.feed.funding_range(v, "BTC", start, END)
                ts = [r["ts"] for r in rows]
                self.assertEqual(len(ts), len(set(ts)), v)
                self.assertEqual(len(ts), 90 * 24, v)
                self.assertTrue(all(start <= t <= END for t in ts))

    def test_txflow_funding_never_invented(self):
        self.feed.meta["TxFlow"] = {"BTC": dict(id="1")}
        with self.assertRaises(ValueError):
            self.feed.funding_range("TxFlow", "BTC", END - 86400, END)


if __name__ == "__main__":
    unittest.main()
