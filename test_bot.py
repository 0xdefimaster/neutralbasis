import copy, math, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import bot


class FixtureFeed:
    def __init__(self):
        self.meta = {}
        self.status = {}
        self.funding = {}
        self.broken = None
        self.candles_broken = False

    def markets(self):
        self.meta = {}
        for venue in ("Lighter", "Extended"):
            self.meta[venue] = {
                "BTC": dict(
                    venue=venue,
                    asset="BTC",
                    id=1 if venue == "Lighter" else "BTC-USD",
                    mark=100.0,
                    index=100.0,
                    step=0.01,
                    minimum=0.01,
                    min_quote=1.0,
                    max_leverage=10.0,
                    mm=0.01,
                    rfq=False,
                    rate=0.0 if venue == "Lighter" else 0.0005,
                    rate_ts=bot.now(),
                    next=bot.hour(bot.now()) + 3600,
                    received=bot.now(),
                    audit_id=1,
                )
            }
            self.status[venue] = dict(ok=True)
        return self.meta

    def candles(self, v, a):
        if self.candles_broken:
            raise ValueError("mum geçmişi eksik")
        end = bot.hour(bot.now())
        return dict(
            prices={
                t: 100 + math.sin(i * 0.11) + i * 0.003
                for i, t in enumerate(range(end - 720 * 3600, end, 3600))
            },
            audit_ids=[1],
            end=end,
        )

    def funding_history(self, v, a, start=None):
        end = bot.hour(bot.now())
        rate = 0 if v == "Lighter" else 0.0005
        rows = [
            dict(ts=t, rate=rate, value=0 if v == "Lighter" else None, audit_id=1)
            for t in range(end - 167 * 3600, end + 1, 3600)
        ]
        self.funding[(v, a)] = rows
        return rows

    def book(self, v, a):
        if self.broken == v:
            raise ValueError("erişim kesildi")
        return dict(
            bids=[(99.99, 100.0)],
            asks=[(100.01, 100.0)],
            received=bot.now(),
            audit_id=1,
        )


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = 1792908123.0
        self.timer = patch.object(bot, "now", lambda: self.clock)
        self.timer.start()
        self.sleep = patch.object(bot.time, "sleep", lambda _: None)
        self.sleep.start()
        self.feed = FixtureFeed()
        self.e = bot.Engine(Path(self.tmp.name) / "paper.db", self.feed)
        self.e.cfg["symbols"] = ["BTC"]
        self.feed.markets()
        self.e.daily_pnl()

    def tearDown(self):
        self.e.store.db.close()
        self.sleep.stop()
        self.timer.stop()
        self.tmp.cleanup()

    def candidate(self, reverse=False):
        self.feed.markets()
        a = "BTC"
        vs = ("Lighter", "Extended")
        hs = {v: self.feed.candles(v, a) for v in vs}
        bs = {v: self.feed.book(v, a) for v in vs}
        for v in vs:
            self.feed.funding_history(v, a)
        lv, sv = vs[::-1] if reverse else vs
        return self.e.evaluate(
            a, lv, sv, bs, bot.analyze(hs[lv], hs[sv], 24, self.clock)
        )

    def test_positive_opportunity_and_reverse_rejected(self):
        c = self.candidate()
        self.assertTrue(c["eligible"], c["reasons"])
        self.assertGreater(c["net"], 0)
        self.assertFalse(self.candidate(True)["eligible"])

    def test_risk_and_reserve_size_caps(self):
        self.e.update_config(dict(capital=100, risk=0.5, size=50000))
        c = self.candidate()
        self.assertLessEqual(c["loss"], 0.5 + 1e-6)
        self.assertLessEqual(c["margin"] / 2, 35)

    def test_spread_erases_funding(self):
        c = self.candidate()
        self.e.cfg["min_net"] = 100
        self.assertFalse(self.candidate()["eligible"])

    def test_walk_depth_and_sides(self):
        self.assertEqual(bot.walk([(100, 1), (101, 1)], 2, True), 100.5)
        self.assertEqual(bot.walk([(99, 1), (98, 1)], 2, False), 98.5)
        with self.assertRaises(ValueError):
            bot.walk([(100, 0.5)], 1, True)

    def test_missing_candle_blocks_entry(self):
        h = self.feed.candles("Lighter", "BTC")
        del h["prices"][min(h["prices"])]
        with self.assertRaises(ValueError):
            bot.analyze(h, self.feed.candles("Extended", "BTC"), 24, self.clock)

    def test_lookahead_candle_excluded_by_analysis_window(self):
        h = self.feed.candles("Lighter", "BTC")
        s = self.feed.candles("Extended", "BTC")
        baseline = bot.analyze(h, s, 24, self.clock)
        h["prices"][bot.hour(self.clock) + 3600] = 1e9
        self.assertEqual(baseline, bot.analyze(h, s, 24, self.clock))

    def test_future_payment_not_booked(self):
        self.e.open(self.candidate())
        p = self.e.positions[0]
        self.feed.funding[("Lighter", "BTC")] = [
            dict(ts=self.clock + 3600, rate=0.1, value=10, audit_id=1)
        ]
        self.e.settle(p)
        self.assertEqual(p["funding"], 0)

    def test_open_close_accounting(self):
        c = self.candidate()
        self.e.open(c)
        self.assertEqual(len(self.e.positions), 1)
        p = self.e.positions[0]
        self.assertEqual(len(p["legs"]), 2)
        self.assertAlmostEqual(sum(self.e.wallet.values()), 1000 - p["fees"])
        self.e.close(p, "test")
        self.assertFalse(self.e.positions)
        self.assertAlmostEqual(
            sum(self.e.wallet.values()), 1000 + self.e.closed[0]["net"]
        )

    def test_second_leg_failure_unwinds_first(self):
        c = self.candidate()
        self.feed.broken = "Extended"
        self.e.open(c)
        self.assertFalse(self.e.positions)
        self.assertEqual(len(self.e.closed), 1)
        self.assertEqual(len(self.e.closed[0]["legs"]), 1)
        self.assertTrue(self.e.closed[0]["legs"][0]["closed"])

    def test_unwind_failure_remains_recovery(self):
        c = self.candidate()
        orig = self.e.quote
        calls = []

        def quote(v, a, q, buy):
            calls.append(v)
            if len(calls) > 1:
                raise ValueError("her iki bağlantı kesildi")
            return orig(v, a, q, buy)

        self.e.quote = quote
        self.e.open(c)
        self.assertEqual(self.e.positions[0]["status"], "RECOVERY")
        self.assertEqual(len(self.e.positions[0]["legs"]), 1)

    def test_funding_boundary_and_idempotency(self):
        self.e.open(self.candidate())
        p = self.e.positions[0]
        event = bot.hour(self.clock) + 3600
        self.clock = event + 10
        self.feed.funding[("Lighter", "BTC")] = [
            dict(ts=event, rate=0.0001, value=0.01, audit_id=1)
        ]
        self.feed.funding[("Extended", "BTC")] = [
            dict(ts=event + 1, rate=0.0005, value=None, audit_id=1)
        ]
        self.e.store.mark("Extended", "BTC", event - 5, 100)
        before = sum(self.e.wallet.values())
        self.e.settle(p)
        value = p["funding"]
        self.assertGreater(value, 0)
        self.assertAlmostEqual(sum(self.e.wallet.values()) - before, value)
        self.e.settle(p)
        self.assertEqual(p["funding"], value)
        self.assertTrue(
            any(
                x["body"]["valuation_estimated"]
                for x in self.e.store.recent("payments")
            )
        )

    def test_missing_payment_mark_stays_pending(self):
        self.e.open(self.candidate())
        p = self.e.positions[0]
        event = bot.hour(self.clock) + 3600
        self.clock = event + 10
        for v in ("Lighter", "Extended"):
            self.feed.funding_history(v, "BTC")
        self.e.settle(p)
        self.assertTrue(p["pending_funding"])
        self.assertEqual(p["estimated_funding"], 0)

    def test_daily_loss_stops_new_entries(self):
        self.e.wallet["Lighter"] -= 21
        with self.assertRaisesRegex(ValueError, "Günlük"):
            self.candidate()

    def test_autocycle_opens_only_one_and_pause_monitors(self):
        self.e.running = True
        self.e.cycle()
        self.assertEqual(len(self.e.positions), 1)
        self.e.cycle()
        self.assertEqual(len(self.e.positions), 1)
        self.e.running = False
        self.e.manual_close.add(self.e.positions[0]["id"])
        self.e.cycle()
        self.assertEqual(len(self.e.positions), 0)

    def test_candle_failure_does_not_disable_risk_exit(self):
        self.e.running = True
        self.e.cycle()
        self.assertEqual(len(self.e.positions), 1)
        self.feed.candles_broken = True
        self.e.manual_close.add(self.e.positions[0]["id"])
        self.e.cycle()
        self.assertFalse(self.e.positions)

    def test_restart_preserves_wallet_and_positions_pauses_entries(self):
        self.e.open(self.candidate())
        other = bot.Engine(Path(self.tmp.name) / "paper.db", FixtureFeed())
        self.assertEqual(other.wallet, self.e.wallet)
        self.assertEqual(len(other.positions), 1)
        self.assertFalse(other.running)
        other.store.db.close()

    def test_config_disallows_live_or_lower_fee_and_recapitalizing(self):
        with self.assertRaises(ValueError):
            self.e.update_config({"live": 1})
        with self.assertRaises(ValueError):
            self.e.update_config({"extended_fee": 0})
        self.e.open(self.candidate())
        with self.assertRaises(ValueError):
            self.e.update_config({"capital": 2000})

    def test_stale_books_reject(self):
        self.candidate()
        hs = {v: self.feed.candles(v, "BTC") for v in ("Lighter", "Extended")}
        bs = {v: self.feed.book(v, "BTC") for v in ("Lighter", "Extended")}
        bs["Lighter"]["received"] -= 60
        c = self.e.evaluate(
            "BTC",
            "Lighter",
            "Extended",
            bs,
            bot.analyze(hs["Lighter"], hs["Extended"], 24, self.clock),
        )
        self.assertFalse(c["eligible"])

    def test_fifteen_symbols_default_and_persisted_migration(self):
        self.assertEqual(len(bot.DEFAULTS["symbols"]), 15)
        self.assertEqual(len(set(bot.DEFAULTS["symbols"])), 15)
        self.e.store.put("config", {"symbols": bot.DEFAULTS["symbols"][:10]})
        other = bot.Engine(Path(self.tmp.name) / "paper.db", FixtureFeed())
        self.assertEqual(len(other.cfg["symbols"]), 15)
        other.store.db.close()

    def test_symbol_input_must_be_exactly_fifteen_unique_common_assets(self):
        with self.assertRaises(ValueError):
            self.e.update_config({"symbols": ["BTC"]})
        with self.assertRaises(ValueError):
            self.e.update_config({"symbols": ["BTC"] * 15})
        with self.assertRaises(ValueError):
            self.e.update_config({"symbols": bot.DEFAULTS["symbols"]})

    def test_unknown_platform_does_not_send_network_request(self):
        with patch.object(self.e.connections, "request") as request:
            with self.assertRaises(ValueError):
                self.e.connections.test("https://invalid.example")
            request.assert_not_called()

    def test_metadata_access_does_not_enable_unverified_paper_adapter(self):
        with patch.object(
            self.e.connections,
            "request",
            return_value=({"markets": [{"marketDisplayName": "BTC-USD"}]}, 200),
        ):
            result = self.e.connections.test("Arcus")
            self.assertTrue(result["ok"])
            self.assertFalse(self.e.connections.view()["Arcus"]["paper"])

    @unittest.skipUnless(bot.os.name == "nt", "Windows DPAPI")
    def test_credential_encrypted_and_not_returned_or_logged(self):
        import base64, connections, json

        secret = "unit-test-only-not-a-real-credential"
        with patch.object(
            self.e.connections,
            "request",
            return_value=(
                {
                    "status": "OK",
                    "data": [{"market": "BTC-USD", "takerFeeRate": "0.00025"}],
                },
                200,
            ),
        ):
            self.e.connections.save_extended_key(secret)
        encrypted = self.e.store.get("extended_key_encrypted", None)
        if encrypted:
            self.assertNotIn(secret, encrypted)
            self.assertEqual(
                connections.protect(base64.b64decode(encrypted), True).decode(), secret
            )
        else:
            self.assertEqual(
                self.e.connections.view()["Extended"]["key_storage"],
                "Yalnızca motor belleği",
            )
        self.assertNotIn(secret, json.dumps(self.e.connections.view()))
        self.assertNotIn(secret, json.dumps(self.e.store.recent("events")))
        self.e.connections.delete_extended_key()
        self.assertFalse(self.e.connections.view()["Extended"]["has_key"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
