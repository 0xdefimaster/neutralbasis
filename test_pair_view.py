import unittest
import pair_view as pv

AT = 1_800_000_000


def payments(rate, interval, days, at=AT):
    n = int(days * 86400 / interval)
    return [dict(ts=at - i * interval, rate=rate) for i in range(n)]


class MergeLegsTests(unittest.TestCase):
    def test_earlier_source_wins_and_later_fills_missing_fields(self):
        native = {"Lighter": dict(rate=0.0001, interval=3600, mark=100, received=AT)}
        rows = {
            "Lighter": dict(rate=0.0009, interval=3600, oi=5e6, volume=1e7, received=AT)
        }
        universe = {
            "Lighter": dict(rate=0.0007, interval=3600, fee=0.0004, received=AT)
        }
        legs = pv.merge_legs(native, rows, universe, {"Lighter": "DEX"}, {}, AT)
        x = legs["Lighter"]
        self.assertEqual(x["rate"], 0.0001)
        self.assertEqual((x["oi"], x["volume"], x["fee"]), (5e6, 1e7, 0.0004))

    def test_stale_rate_and_price_are_not_current(self):
        universe = {
            "Old": dict(rate=0.001, interval=3600, mark=1, received=AT - 600),
            "OldPrice": dict(rate=0.001, interval=3600, mark=1, received=AT - 120),
        }
        legs = pv.merge_legs({}, {}, universe, {}, {}, AT)
        self.assertNotIn("Old", legs)
        self.assertIsNone(legs["OldPrice"]["mark"])

    def test_past_next_payment_is_dropped(self):
        universe = {"A": dict(rate=0.001, interval=3600, next=AT - 7200, received=AT)}
        self.assertIsNone(pv.merge_legs({}, {}, universe, {}, {}, AT)["A"]["next"])


class RealizedTests(unittest.TestCase):
    def test_window_complete_only_with_every_payment(self):
        r = pv.realized(payments(0.0001, 28800, 7), 28800, AT)
        self.assertTrue(r["7d"]["complete"])
        self.assertAlmostEqual(r["7d"]["sum"], 21 * 0.0001)
        self.assertEqual(r["1d"]["count"], 3)
        self.assertFalse(r["14d"]["complete"])
        self.assertEqual(r["14d"]["count"], 21)

    def test_unknown_interval_is_never_complete(self):
        self.assertFalse(pv.realized([], None, AT)["1d"]["complete"])


class PairDetailTests(unittest.TestCase):
    def legs(self):
        base = dict(next=None, volume=None, oi=None, leverage=None, kind="DEX")
        return {
            "L": dict(
                base,
                venue="L",
                label="L",
                rate=0.00001,
                interval=3600,
                fee=0.0005,
                mark=100.0,
            ),
            "S": dict(
                base,
                venue="S",
                label="S",
                rate=0.0001,
                interval=28800,
                fee=0.0004,
                mark=100.5,
            ),
            "X": dict(
                base,
                venue="X",
                label="X",
                rate=0.00005,
                interval=3600,
                fee=None,
                mark=None,
            ),
        }

    def test_pair_funding_is_short_received_minus_long_paid(self):
        f = {"L": payments(0.00001, 3600, 7), "S": payments(0.0008, 28800, 7)}
        d = pv.pair_detail("BTC", "L", "S", self.legs(), f, AT)
        w = d["windows"]["7d"]
        self.assertTrue(w["complete"])
        self.assertAlmostEqual(w["funding"], 21 * 0.0008 - 168 * 0.00001)
        self.assertFalse(d["windows"]["30d"]["complete"])
        self.assertAlmostEqual(d["funding_gap"], 0.00009)
        self.assertAlmostEqual(d["price_gap"], 0.005)
        self.assertAlmostEqual(d["fee_roundtrip"], 2 * (0.0005 + 0.0004))
        self.assertEqual([x["venue"] for x in d["others"]], ["S", "X", "L"])
        # Path covers only the span where both legs have payments: 21 short, 161 long.
        self.assertAlmostEqual(d["path"][-1]["value"], 21 * 0.0008 - 161 * 0.00001)

    def test_path_starts_where_both_legs_have_payments(self):
        f = {"L": payments(0.00001, 3600, 30), "S": payments(0.0008, 28800, 2)}
        d = pv.pair_detail("BTC", "L", "S", self.legs(), f, AT)
        first_short = min(r["ts"] for r in f["S"])
        self.assertGreaterEqual(d["path"][0]["ts"], first_short - 1)
        self.assertAlmostEqual(d["path"][-1]["value"], 6 * 0.0008 - 41 * 0.00001)

    def test_unknown_fee_leaves_cost_unknown(self):
        d = pv.pair_detail("BTC", "X", "S", self.legs(), {}, AT)
        self.assertIsNone(d["fee_roundtrip"])
        self.assertIsNone(d["price_gap"])

    def test_rejects_same_or_missing_venue(self):
        with self.assertRaises(ValueError):
            pv.pair_detail("BTC", "L", "L", self.legs(), {}, AT)
        with self.assertRaises(ValueError):
            pv.pair_detail("BTC", "L", "Nope", self.legs(), {}, AT)

    def test_different_tokens_do_not_show_a_price_gap(self):
        legs = self.legs()
        legs["S"]["mark"] = 150.0
        self.assertIsNone(pv.pair_detail("BTC", "L", "S", legs, {}, AT)["price_gap"])


class AssetDetailTests(unittest.TestCase):
    def test_best_pairs_and_order(self):
        base = dict(
            next=None,
            volume=None,
            oi=None,
            leverage=None,
            kind="DEX",
            fee=None,
            interval=3600,
            label="",
        )
        legs = {
            "A": dict(base, venue="A", rate=0.0002, mark=101.0),
            "B": dict(base, venue="B", rate=-0.0001, mark=100.0),
            "C": dict(base, venue="C", rate=0.0, mark=180.0),
        }
        d = pv.asset_detail("ETH", legs, {"A": payments(0.0002, 3600, 1)}, AT)
        self.assertEqual([x["venue"] for x in d["venues"]], ["A", "C", "B"])
        self.assertEqual(
            (d["best_funding"]["long"], d["best_funding"]["short"]), ("B", "A")
        )
        self.assertEqual(
            (d["widest_price"]["long"], d["widest_price"]["short"]), ("B", "A")
        )
        self.assertTrue(d["venues"][0]["paid"]["1d"]["complete"])


class SyncTests(unittest.TestCase):
    def test_sync_runs_once_per_leg_within_cooldown(self):
        calls = []

        class History:
            def sync(self, v, a, kind, interval):
                calls.append((v, a, kind, interval))
                return 3

        class Views(pv.PairViews):
            def legs(self, asset):
                return {"A": dict(interval=3600), "B": dict(interval=28800)}

        engine = type("E", (), {"history": History()})()
        views = Views(engine)
        first = views.request_sync("btc", ["A", "B", "Z"])
        self.assertEqual(first["started"], ["A", "B"])
        for _ in range(100):
            if all(s["status"] != "running" for s in views.sync_state("BTC").values()):
                break
            threading_wait(0.01)
        self.assertEqual(
            sorted(calls), [("A", "BTC", "perp", 3600), ("B", "BTC", "perp", 28800)]
        )
        self.assertEqual(views.request_sync("BTC", ["A"])["started"], [])
        with self.assertRaises(ValueError):
            views.request_sync("BTC", ["Z"])


class PriceHistoryTests(unittest.TestCase):
    def test_gap_and_window_mean_need_coverage(self):
        hours = [AT - i * 3600 for i in range(24 * 7)]
        long = {t: 100.0 for t in hours}
        short = {t: 100.2 for t in hours}
        points, w = pv.price_history(long, short, AT)
        self.assertEqual(len(points), 24 * 7)
        self.assertAlmostEqual(w["7d"]["mean"], 0.002)
        self.assertAlmostEqual(w["1d"]["mean"], 0.002)
        self.assertIsNone(w["14d"]["mean"])
        self.assertEqual(w["14d"]["count"], 24 * 7)

    def test_only_hours_both_legs_closed(self):
        long = {AT: 100.0, AT - 3600: 100.0}
        short = {AT: 101.0}
        points, _ = pv.price_history(long, short, AT)
        self.assertEqual([p["ts"] for p in points], [AT])

    def test_ticker_clash_has_no_history(self):
        points, w = pv.price_history({AT: 1.0}, {AT: 2.0}, AT)
        self.assertEqual(points, [])
        self.assertIsNone(w["1d"]["mean"])

    def test_pair_detail_carries_price_windows(self):
        legs = PairDetailTests().legs()
        hours = [AT - i * 3600 for i in range(24)]
        prices = {"L": {t: 100.0 for t in hours}, "S": {t: 99.9 for t in hours}}
        d = pv.pair_detail("BTC", "L", "S", legs, {}, AT, prices=prices)
        self.assertAlmostEqual(d["price_windows"]["1d"]["mean"], -0.001)
        self.assertEqual(len(d["price_points"]), 24)


class BookCostTests(unittest.TestCase):
    book = dict(
        bids=[(99.0, 50.0), (98.0, 1000.0)], asks=[(101.0, 50.0), (102.0, 1000.0)]
    )

    def test_buy_walks_asks_and_adds_fee(self):
        c = pv.book_cost(self.book, 1000.0, True, 0.0005)
        # 10 units at 101 from a 100 mid: 1% slippage + 5 bp fee.
        self.assertTrue(c["fits"])
        self.assertAlmostEqual(c["bp"], 105.0)
        self.assertAlmostEqual(c["cost"], 10.5)
        self.assertAlmostEqual(c["half_spread_bp"], 100.0)

    def test_sell_walks_bids_and_too_large_does_not_fit(self):
        c = pv.book_cost(self.book, 10000.0, False, None)
        # 50 at 99 + 50 at 98 = 98.5 average: 150 bp, fee unknown.
        self.assertAlmostEqual(c["bp"], 150.0)
        self.assertFalse(c["fee_known"])
        self.assertFalse(pv.book_cost(self.book, 1e7, False, 0.0)["fits"])


class EngineLegTests(unittest.TestCase):
    def engine(self, book_error=None):
        test = self

        class Wide:
            def __init__(self):
                self.calls = []

            def book(self, v, a):
                if book_error:
                    raise ValueError(book_error)
                return BookCostTests.book

            def candles_since(self, v, a, kind, start, end):
                self.calls.append((v, a, kind, start, end))
                return {end - 3600: 1.0}

        class DB:
            def __init__(self):
                self.added = []

            def last_candle(self, v, a, kind):
                return None

            def add_candles(self, v, a, kind, prices):
                self.added.append((v, a, kind, prices))
                return len(prices)

        e = type("E", (), {})()
        e.wide = Wide()
        e.cfg = {"symbols": ["BTC"]}
        e.history = type("H", (), {"db": DB()})()
        e.feed = type("F", (), {"book": lambda self, v, a: BookCostTests.book})()
        return e

    def views(self, engine):
        class Views(pv.PairViews):
            def legs(self, asset):
                base = dict(fee=0.0005, interval=3600)
                return {"A": dict(base), "B": dict(base, fee=None)}

        return Views(engine)

    def test_books_cost_both_legs_at_reference_sizes(self):
        out = self.views(self.engine()).books("eth", "A", "B")
        self.assertEqual(out["asset"], "ETH")
        self.assertEqual(out["legs"]["A"]["side"], "long")
        self.assertEqual(
            [s["usd"] for s in out["legs"]["B"]["sizes"]], list(pv.BOOK_SIZES)
        )

    def test_book_error_is_reported_per_leg(self):
        out = self.views(self.engine("Defter eski")).books("ETH", "A", "B")
        self.assertEqual(out["legs"]["A"]["error"], "Defter eski")
        with self.assertRaises(ValueError):
            self.views(self.engine()).books("ETH", "A", "Z")

    def test_candles_filled_only_outside_selected_assets(self):
        e = self.engine()
        v = self.views(e)
        self.assertEqual(v.fill_candles("A", "BTC"), 0)
        self.assertEqual(v.fill_candles("A", "ETH"), 1)
        self.assertEqual(e.history.db.added[0][:3], ("A", "ETH", "perp"))


def threading_wait(t):
    import threading

    threading.Event().wait(t)


if __name__ == "__main__":
    unittest.main()
