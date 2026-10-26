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


def threading_wait(t):
    import threading

    threading.Event().wait(t)


if __name__ == "__main__":
    unittest.main()
