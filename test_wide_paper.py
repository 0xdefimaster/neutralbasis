import json, math, os, tempfile, unittest
from unittest import mock
import bot
import wide_paper
from wide_paper import (
    WidePaper,
    rank,
    rank_spreads,
    payments_within,
    next_payment,
    pair_stats,
    crossings,
    excursions,
    stop_breaches,
)

T0 = 1_000 * 86400  # aligned to the hour and to 8h


def row(
    venue, asset="BTC", rate=0.0, interval=3600, fee=0.0005, mark=100.0, at=T0, **kw
):
    return dict(
        asset=asset,
        venue=venue,
        kind="perp",
        mark=mark,
        rate=rate / (interval / 3600),
        raw_rate=rate,
        interval=interval,
        next=at + interval,
        received=at,
        fee=fee,
        leverage=None,
        price_type="mark",
        **kw,
    )


def book(mid, at, depth=1000.0, half=0.01):
    return dict(
        bids=[(mid - half, depth)],
        asks=[(mid + half, depth)],
        received=at,
        audit_id=None,
    )


def lot_free():
    return dict(step=None, minimum=None, min_quote=None)


class FakeSource:
    def __init__(self):
        self.rows = []
        self.books = {}
        self.history = {}
        self.rates = {}
        # Payments the history keeper recorded, by (venue, asset).
        self.stored = {}

    def market_rows(self):
        return [dict(r) for r in self.rows]

    def book(self, v, a):
        b = self.books[(v, a)]
        return dict(b, received=bot.now())

    def prices(self, v, a):
        return self.history[(v, a)]

    def funding(self, v, a, start):
        return [r for r in self.rates.get((v, a), []) if r["ts"] >= start]

    def stored_funding(self, v, a, start):
        return [r for r in self.stored.get((v, a), []) if r["ts"] >= start]


def candles(at, drift=0.0, noise=0.0):
    end = bot.hour(at)
    return dict(
        price_type="trade",
        prices={
            t: 100 * (1 + 0.001 * math.sin(i / 3)) * (1 + drift) + noise * (i % 2)
            for i, t in enumerate(range(end - 730 * 3600, end, 3600))
        },
    )


class WidePaperTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = bot.Store(os.path.join(self.dir.name, "p.sqlite3"))
        self.clock = T0
        patcher = [
            mock.patch.object(bot, "now", lambda: self.clock),
            mock.patch.object(wide_paper, "now", lambda: self.clock),
        ]
        for p in patcher:
            p.start()
            self.addCleanup(p.stop)
        self.src = FakeSource()
        self.desk = WidePaper(self.store, self.src, sleep=lambda s: None)

    def tearDown(self):
        self.store.db.close()
        self.dir.cleanup()

    def fund_history(self, venue, per_payment, interval, days=30):
        self.src.rates[(venue, "BTC")] = [
            dict(ts=t, rate=per_payment, value=None, audit_id=None)
            for t in range(T0 - days * 86400 + interval, T0 + 1, interval)
        ]

    def setup_pair(self, long_rate=-0.0001, short_rate=0.0008, short_interval=28800):
        # Binance pays every 8h; Hyperliquid every hour.
        self.src.rows = [
            row("Hyperliquid", rate=long_rate, interval=3600, fee=0.00045),
            row("Binance", rate=short_rate, interval=short_interval, fee=0.0005),
        ]
        for v in ("Hyperliquid", "Binance"):
            self.src.books[(v, "BTC")] = book(100.0, T0)
            self.src.history[(v, "BTC")] = candles(T0)
        self.fund_history("Hyperliquid", long_rate, 3600)
        self.fund_history("Binance", short_rate, short_interval)

    def test_payment_counts_respect_each_venue_interval(self):
        r1 = row("A", interval=3600)
        r8 = row("B", interval=28800)
        self.assertEqual(payments_within(r1, T0, T0 + 24 * 3600), 24)
        self.assertEqual(payments_within(r8, T0, T0 + 24 * 3600), 3)
        # An 8h venue may pay nothing inside a short horizon.
        self.assertEqual(payments_within(r8, T0, T0 + 6 * 3600), 0)
        stale = dict(r8, next=T0 - 3600)
        self.assertEqual(next_payment(stale, T0), T0 - 3600 + 28800)

    def test_rank_spans_all_venues_and_charges_four_fees(self):
        rows = [
            row("Lighter", rate=0.00001, fee=0.00028),
            row("Binance", rate=0.0004, interval=28800, fee=0.0005),
            row("Bybit", rate=0.0002, interval=28800, fee=0.00055),
            row("OKX", asset="ETH", rate=0.0001),
        ]
        out = rank(rows, wide_paper.WIDE_DEFAULTS, T0)
        best = out[0]
        self.assertEqual((best["long"], best["short"]), ("Lighter", "Binance"))
        expected = 0.0004 * 3 - 0.00001 * 24 - 2 * (0.00028 + 0.0005)
        self.assertAlmostEqual(best["score"], expected)
        self.assertEqual(len(out), 3)  # ETH has a single venue; no pair.
        self.assertTrue(all(c["funding_gap"] > 0 for c in out))

    def test_rank_excludes_unknown_fee_interval_and_last_trade(self):
        rows = [
            row("A", rate=0.0),
            row("B", rate=0.001, fee=None),
            dict(row("C", rate=0.001), interval=None),
            dict(row("D", rate=0.001), price_type="last_trade"),
            dict(row("E", rate=0.001), received=T0 - 600),
        ]
        self.assertEqual(rank(rows, wide_paper.WIDE_DEFAULTS, T0), [])

    def test_snapshot_shows_time_between_cycles(self):
        self.desk.cycle()
        self.assertIsNone(self.desk.snapshot()["cycle_seconds"])
        self.clock += 420
        self.desk.cycle()
        snap = self.desk.snapshot()
        self.assertEqual(snap["cycle_seconds"], 420)
        self.assertEqual(snap["downtime_gap"], wide_paper.DOWNTIME_GAP)

    def test_opens_best_pair_with_real_fees(self):
        self.setup_pair()
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        p = self.desk.positions[0]
        self.assertEqual(p["status"], "OPEN")
        legs = {l["side"]: l for l in p["legs"]}
        self.assertEqual(legs["long"]["venue"], "Hyperliquid")
        self.assertEqual(legs["short"]["venue"], "Binance")
        self.assertAlmostEqual(legs["short"]["fee_rate"], 0.0005)
        self.assertEqual(legs["short"]["interval"], 28800)
        self.assertAlmostEqual(self.desk.wallet["cash"], 10000 - p["fees"])

    def test_loss_budget_sizes_down_for_the_book_spread(self):
        # The budget binds: the entry shrinks until fees, spread and stress fit,
        # instead of being refused for a spread the first sizing could not see.
        self.setup_pair()
        self.desk.cfg["wide_risk"] = 1.0
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        d = self.desk.positions[0]["decision"]
        worst = d["entry_fee"] + d["exit_fee"] + d["execution_drag"] + d["stress_loss"]
        self.assertLessEqual(worst, 1.0 + 1e-6)
        self.assertGreater(d["execution_drag"], 0)
        self.assertLess(d["size"], 1.0 / d["fee_roundtrip"])

    # --- the desk's word on one pair, for the pair page ----------------------------
    def test_verdict_tells_a_rejected_pair_why_and_when_it_returns(self):
        self.setup_pair()
        self.desk.update_config({"wide_min_net": 1000})
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        v = self.desk.verdict("BTC", "Binance", "Hyperliquid")
        self.assertEqual(v["legs"], {"Binance": None, "Hyperliquid": None})
        f = v["strategies"][0]
        self.assertEqual((f["strategy"], f["rank"]), ("funding", 1))
        self.assertEqual(f["decision"]["outcome"], "rejected")
        self.assertIn(
            "Funding geliri ücret + kayma + spread riski payını karşılamıyor",
            [wide_paper.reason_group(r) for r in f["decision"]["reasons"]],
        )
        # The measured net and the threshold follow the reason.
        self.assertTrue(any("< $1000.00" in r for r in f["decision"]["reasons"]))
        self.assertEqual(f["retry_at"], T0 + wide_paper.REJECT_COOLDOWN)
        self.assertEqual((f["long"], f["short"]), ("Hyperliquid", "Binance"))

    def test_verdict_tells_an_opened_pair_and_its_position(self):
        self.setup_pair()
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        v = self.desk.verdict("BTC", "Hyperliquid", "Binance")
        self.assertEqual(v["strategies"][0]["decision"]["outcome"], "opened")
        (p,) = v["positions"]
        self.assertEqual((p["long"], p["short"]), ("Hyperliquid", "Binance"))
        self.assertTrue(p["entry_reason"].startswith("Funding:"))

    def test_verdict_keeps_why_an_eligible_pair_was_not_entered(self):
        x = dict(
            strategy="funding",
            asset="BTC",
            long="Hyperliquid",
            short="Binance",
            reasons=[],
            net=1.25,
        )
        self.desk.decide(x, T0, "skipped", ["Borsa teminatı yetersiz"])
        d = self.desk.verdict("BTC", "Binance", "Hyperliquid")["strategies"][0]
        self.assertEqual(
            d["decision"],
            dict(
                at=T0,
                outcome="skipped",
                reasons=["Borsa teminatı yetersiz"],
                net=1.25,
                long="Hyperliquid",
                short="Binance",
            ),
        )
        # Without explicit reasons the pair's own reasons are kept.
        self.desk.decide(dict(x, reasons=["Hedge beta uyumsuz"]), T0 + 1, "rejected")
        d = self.desk.verdict("BTC", "Binance", "Hyperliquid")["strategies"][0]
        self.assertEqual(d["decision"]["reasons"], ["Hedge beta uyumsuz"])

    def test_verdict_names_a_leg_without_an_account_or_data(self):
        self.setup_pair()
        self.desk.reset(venue_capital=1000, venues=["Hyperliquid", "Extended"])
        self.desk.cycle()
        v = self.desk.verdict("BTC", "Hyperliquid", "Binance")
        self.assertEqual(v["legs"]["Binance"], "hesap listesinde değil")
        v = self.desk.verdict("BTC", "Hyperliquid", "Extended")
        self.assertEqual(v["legs"]["Extended"], "piyasa verisi yok")

    def test_rejects_when_fees_beat_funding(self):
        self.setup_pair(long_rate=0.0, short_rate=0.00005)
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        # Negative score after four fees never reaches the deep check.
        self.assertTrue(self.desk.candidates)
        self.assertLessEqual(self.desk.candidates[0]["score"], 0)
        self.assertEqual(self.desk.assessed, [])

    def test_realized_funding_caps_a_spiking_rate(self):
        self.setup_pair(short_rate=0.002)
        # Realized history was flat; only the live quote spiked.
        self.fund_history("Binance", 0.00001, 28800)
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        c = self.desk.assessed[0]
        self.assertAlmostEqual(c["conservative_raw"]["short"], 0.00001)
        self.assertFalse(c["eligible"])

    def test_missing_realized_history_blocks_entry(self):
        self.setup_pair()
        self.src.rates[("Binance", "BTC")] = []
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        self.assertTrue(any("24 saat" in r for r in self.desk.assessed[0]["reasons"]))

    def test_uncorrelated_history_blocks_entry(self):
        self.setup_pair()
        end = bot.hour(T0)
        self.src.history[("Binance", "BTC")] = dict(
            price_type="trade",
            prices={
                t: 100 + (i * 7919 % 13)
                for i, t in enumerate(range(end - 730 * 3600, end, 3600))
            },
        )
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])

    def test_settles_each_venue_on_its_own_schedule(self):
        self.setup_pair()
        self.desk.cycle()
        p = self.desk.positions[0]
        qty = p["legs"][0]["qty"]
        cash0 = self.desk.wallet["cash"]
        self.store.mark("Binance", "BTC", T0 + 8 * 3600 - 10, 100.0)
        for h in range(1, 9):
            self.store.mark("Hyperliquid", "BTC", T0 + h * 3600 - 10, 100.0)
        for v, per, iv in (("Hyperliquid", -0.0001, 3600), ("Binance", 0.0008, 28800)):
            self.src.rates[(v, "BTC")] += [
                dict(ts=t, rate=per, value=None, audit_id=None)
                for t in range(T0 + iv, T0 + 8 * 3600 + 1, iv)
            ]
        self.clock = T0 + 8 * 3600 + 30
        self.desk.cache.clear()
        for v in ("Hyperliquid", "Binance"):
            self.src.books[(v, "BTC")] = book(100.0, self.clock)
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        self.desk.cycle(entries=False)
        # Long receives on negative rate: 8 hourly payments; short receives one 8h payment.
        expected = qty * 100 * (8 * 0.0001 + 0.0008)
        self.assertAlmostEqual(p["funding"], expected, places=6)
        self.assertAlmostEqual(self.desk.wallet["cash"] - cash0, expected, places=6)
        # Re-running never pays twice.
        self.desk.cache.clear()
        self.desk.cycle(entries=False)
        self.assertAlmostEqual(p["funding"], expected, places=6)

    def test_unpublished_payment_recorded_unverified(self):
        self.setup_pair()
        self.desk.cycle()
        p = self.desk.positions[0]
        self.clock = T0 + 8 * 3600 + FUNDING_WAIT + 60
        self.desk.cache.clear()
        for v in ("Hyperliquid", "Binance"):
            self.src.books[(v, "BTC")] = book(100.0, self.clock)
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        self.desk.cycle(entries=False)
        venues = {x["venue"] for x in p.get("funding_unverified", [])}
        self.assertIn("Binance", venues)
        self.assertEqual(p["funding"], 0)
        # Binance would have paid the short leg: a missing income is no hidden gain.
        self.assertTrue(
            all(
                x["risk"] == 0
                for x in p["funding_unverified"]
                if x["venue"] == "Binance"
            )
        )

    def test_closes_when_carry_flips(self):
        self.setup_pair()
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1)
        self.clock = T0 + 60
        self.src.rows = [
            dict(row("Hyperliquid", rate=0.001, at=self.clock)),
            dict(row("Binance", rate=-0.0005, interval=28800, at=self.clock)),
        ]
        for v in ("Hyperliquid", "Binance"):
            self.src.books[(v, "BTC")] = book(100.0, self.clock)
        self.desk.cycle(entries=False)
        # One adverse quote is not enough: the flip must persist.
        self.assertEqual(len(self.desk.positions), 1)
        self.clock = T0 + 60 + 31 * 60
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        for v in ("Hyperliquid", "Binance"):
            self.src.books[(v, "BTC")] = book(100.0, self.clock)
        self.desk.cycle(entries=False)
        self.assertEqual(self.desk.positions, [])
        self.assertEqual(
            self.desk.closed[0]["close_reason"], "Funding avantajı tersine döndü"
        )
        self.assertEqual(self.desk.closed[0]["status"], "CLOSED")

    def test_manual_close_and_state_survives_restart(self):
        self.setup_pair()
        self.desk.cycle()
        pid = self.desk.positions[0]["id"]
        again = WidePaper(self.store, self.src, sleep=lambda s: None)
        self.assertEqual(again.positions[0]["id"], pid)
        again.manual_close.add(pid)
        again.cycle(entries=False)
        self.assertEqual(again.positions, [])
        self.assertEqual(
            again.closed[-1]["close_reason"], "Kullanıcı paper kapatma talebi"
        )

    def test_no_entries_when_paused(self):
        self.setup_pair()
        self.desk.cycle(entries=False)
        self.assertEqual(self.desk.positions, [])
        self.assertTrue(self.desk.candidates)

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            self.desk.update_config({"wide_leverage": 50})
        with self.assertRaises(ValueError):
            self.desk.update_config({"nope": 1})
        self.desk.update_config({"wide_max_positions": 3, "wide_enabled": False})
        self.assertEqual(self.desk.cfg["wide_max_positions"], 3)
        self.assertFalse(self.desk.cfg["wide_enabled"])

    def test_pair_stats_requires_trade_candles(self):
        h = candles(T0)
        with self.assertRaisesRegex(ValueError, "işlem mumu"):
            pair_stats(dict(h, price_type="oracle"), h, T0, 24)
        s = pair_stats(h, candles(T0, drift=0.001), T0, 24)
        self.assertGreater(s["corr"], 0.99)

    def test_short_history_uses_the_longest_matched_window(self):
        def recent(h, days):
            cut = bot.hour(T0) - days * 86400
            return dict(h, prices={t: p for t, p in h["prices"].items() if t >= cut})

        a, b = recent(candles(T0), 10), recent(candles(T0, drift=0.001), 10)
        self.assertEqual(pair_stats(a, b, T0, 24, 7)["history_days"], 10)
        self.assertEqual(pair_stats(candles(T0), b, T0, 24, 7)["history_days"], 10)
        self.assertEqual(
            pair_stats(candles(T0), candles(T0, 0.001), T0, 24, 7)["history_days"], 30
        )
        with self.assertRaisesRegex(ValueError, "30 günlük eşlenmiş 1h mum eksik"):
            pair_stats(a, b, T0, 24)
        with self.assertRaisesRegex(ValueError, "7 günlük eşlenmiş 1h mum eksik"):
            pair_stats(recent(a, 5), b, T0, 24, 7)

    def test_convergence_on_ten_days_scales_the_thirty_day_thresholds(self):
        self.setup_convergence()
        cut = bot.hour(T0) - 10 * 86400
        for k, h in self.src.history.items():
            self.src.history[k] = dict(
                h, prices={t: p for t, p in h["prices"].items() if t >= cut}
            )
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        h = self.desk.positions[0]["decision"]["convergence_history"]
        self.assertEqual(h["history_days"], 10)
        self.desk.update_config({"wide_min_history_days": 30})
        self.assertEqual(self.desk.cfg["wide_min_history_days"], 30)

    # --- convergence strategy ----------------------------------------------------
    def setup_convergence(
        self, basis=0.0, amplitude=0.0035, gap=0.0031, drift=0.0, grow=0.0, spikes=()
    ):
        # Default: the gap swings ±35bp around its normal level every 12h, and today it
        # is 31bp away (88% of the widest move, all three tiers).
        end = bot.hour(T0)
        hours = list(range(end - 730 * 3600, end, 3600))
        base = {t: 100 * (1 + 0.03 * math.sin(i / 7)) for i, t in enumerate(hours)}
        # Short venue oscillates around the long venue: crosses every 6h.
        self.src.history[("Hyperliquid", "BTC")] = dict(price_type="trade", prices=base)
        self.src.history[("Binance", "BTC")] = dict(
            price_type="trade",
            prices={
                t: base[t]
                * math.exp(
                    basis
                    + drift * i / len(hours)
                    + amplitude
                    * (1 + grow * i / len(hours))
                    * math.sin(2 * math.pi * i / 12)
                    + dict(spikes).get(i, 0.0)
                )
                for i, t in enumerate(hours)
            },
        )
        dear = 100 * math.exp(gap)
        self.src.rows = [
            row("Hyperliquid", rate=0.0, fee=0.0002, mark=100.0),
            row("Binance", rate=0.0, fee=0.0002, mark=dear, interval=28800),
        ]
        self.src.books[("Hyperliquid", "BTC")] = book(100.0, T0)
        self.src.books[("Binance", "BTC")] = book(dear, T0)
        self.desk.update_config({"wide_funding_enabled": False})

    def test_crossing_helpers(self):
        series = [
            (i * 3600, x) for i, x in enumerate([0.001, -0.001, 0.001, 0, -0.001])
        ]
        self.assertEqual(crossings(series, 0.0001)[0], 3)
        h = 3600
        moves = [(0, 0.0), (h, 0.003), (2 * h, 0.001), (3 * h, 0.0)]
        # Back in 2 hours; a move that took 5 hours misses a 4-hour cap; one still
        # open for under the cap is not decided yet.
        slow = [(4 * h, 0.003), (9 * h, 0.0), (10 * h, 0.003), (12 * h, 0.003)]
        self.assertEqual(excursions(moves, 0.002, 0.0002, 4), ([2.0], 0))
        self.assertEqual(excursions(moves + slow, 0.002, 0.0002, 4), ([2.0], 1))
        late = moves + [(4 * h, 0.003), (9 * h, 0.003)]
        self.assertEqual(excursions(late, 0.002, 0.0002, 4), ([2.0], 1))

    def test_stop_breaches_count_separate_moves_past_the_first_half(self):
        h = 3600
        first = [(i * h, x) for i, x in enumerate([0.0, 0.002, 0.0, 0.001])]
        second = [
            (i * h, x) for i, x in enumerate([0.0, 0.0023, 0.0024, 0.0, 0.003], 4)
        ]
        top, count = stop_breaches(first + second, 0.10)
        self.assertEqual(top, 0.002)
        # 0.0023 and 0.0024 are one move past 0.0022; 0.003 after a return is another.
        self.assertEqual(count, 2)
        self.assertEqual(stop_breaches(first + first, 0.10)[1], 0)

    def test_convergence_opens_long_cheap_short_dear_5x(self):
        self.setup_convergence()
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        p = self.desk.positions[0]
        d = p["decision"]
        self.assertEqual(p["strategy"], "convergence")
        self.assertEqual((d["long"], d["short"]), ("Hyperliquid", "Binance"))
        self.assertEqual(d["leverage"], 5.0)
        legs = {l["side"]: l for l in p["legs"]}
        self.assertEqual(legs["long"]["qty"], legs["short"]["qty"])
        h = d["convergence_history"]
        self.assertGreaterEqual(h["cross7"], 6)
        self.assertGreaterEqual(h["cross30"], 20)
        self.assertLessEqual(d["expected_hours"], 6)
        self.assertGreaterEqual(d["max_hold_hours"], d["expected_hours"])

    def test_convergence_closes_when_prices_cross(self):
        self.setup_convergence()
        self.desk.cycle()
        self.clock = T0 + 3600
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        for v in ("Hyperliquid", "Binance"):
            self.src.books[(v, "BTC")] = book(100.0, self.clock)
        self.desk.cycle(entries=False)
        self.assertEqual(self.desk.positions, [])
        self.assertIn(
            self.desk.closed[-1]["close_reason"],
            (
                "Fark ortalama/kesişim bölgesine geldi; kapanış sonrası net hedef sağlandı",
                "Fiyat farkı normal seviyesine döndü",
            ),
        )
        self.assertGreater(self.desk.closed[-1]["net"], 0)

    def test_convergence_waits_for_the_normal_gap_not_the_entry_estimate(self):
        self.setup_convergence()
        self.desk.cycle()
        (p,) = self.desk.positions
        d = p["decision"]
        # Three quarters of the gap have closed and the trade is in profit, but the gap
        # is still above normal: it waits.
        self.clock = T0 + 3600
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        rest = 100 * math.exp(d["entry_gap"] / 4)
        self.src.books[("Hyperliquid", "BTC")] = book(100.0, self.clock)
        self.src.books[("Binance", "BTC")] = book(rest, self.clock)
        self.desk.cycle(entries=False)
        self.assertEqual(len(self.desk.positions), 1)
        self.assertGreater(p["net_if_closed"], 0)
        self.assertGreater(p["live_gap"], d["target_gap"] + 0.0002)
        # Back at the normal gap it closes.
        self.clock += 3600
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        for v in ("Hyperliquid", "Binance"):
            self.src.books[(v, "BTC")] = book(100.0, self.clock)
        self.desk.cycle(entries=False)
        self.assertEqual(self.desk.positions, [])
        self.assertGreater(self.desk.closed[-1]["net"], 0)

    def test_persistent_basis_never_qualifies(self):
        # Dear venue always 30bp rich and barely moves: today's 60bp has no history
        # of excursions that came back.
        self.setup_convergence(basis=0.003, amplitude=0.0003, gap=0.006)
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        reasons = " ".join(self.desk.assessed[0]["reasons"])
        self.assertRegex(reasons, "kesişim yok|en büyük açılmanın üstünde")

    def test_constant_basis_reverts_to_its_normal_gap(self):
        # 0.99 vs 0.98 style: the dear venue is normally 30bp rich and swings ±35bp
        # around that; today it is 61bp rich, so the trade targets ~30bp, not zero.
        self.setup_convergence(basis=0.003, gap=0.0061)
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        d = self.desk.positions[0]["decision"]
        self.assertAlmostEqual(d["convergence_history"]["baseline"], 0.003, places=3)
        self.assertAlmostEqual(d["target_gap"], 0.003, places=3)

    def test_drifting_basis_is_rejected(self):
        self.setup_convergence(drift=0.006, gap=0.012)
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        reasons = " ".join(self.desk.assessed[0]["reasons"])
        self.assertIn("Normal fiyat farkı kayıyor", reasons)

    # --- tiers and the stop past the widest move ------------------------------------
    def move_to(self, dev, after=600):
        """Books (and rows) at a gap `dev` away from the fixture's normal level."""
        self.clock += after
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        self.src.books[("Hyperliquid", "BTC")] = book(100.0, self.clock)
        self.src.books[("Binance", "BTC")] = book(100 * math.exp(dev), self.clock)

    def test_tiers_take_more_risk_closer_to_the_widest_move(self):
        self.desk.update_config({"wide_size": 100000.0})
        sizes = {}
        for gap in (0.0019, 0.0031):  # 54% and 88% of the widest 35bp move
            self.setup_convergence(gap=gap)
            self.desk.candidates = wide_paper.rank_spreads(self.src.rows, T0)
            x = self.desk.assess(self.desk.candidates[0])
            self.assertTrue(x["eligible"], x["reasons"])
            sizes[gap] = x
        low, high = sizes[0.0019], sizes[0.0031]
        self.assertEqual([t["filled"] for t in low["tiers"]], [True, False, False])
        self.assertEqual([t["filled"] for t in high["tiers"]], [True, True, True])
        budget = self.desk.risk_budget()
        self.assertAlmostEqual(low["budget"], 0.2 * budget)
        self.assertAlmostEqual(high["budget"], budget)
        # Sized for the hard stop: the loss there plus fees and spread fits the share.
        for x in (low, high):
            hard = x["hard_stop_gap"]
            self.assertAlmostEqual(hard, x["baseline"] + 1.25 * x["widest"])
            worst = (
                x["entry_fee"] + x["exit_fee"] + x["execution_drag"] + x["stress_loss"]
            )
            self.assertLessEqual(worst, x["budget"] + 1e-6)
        self.assertGreater(high["size"], 5 * low["size"])

    def test_wider_gap_adds_the_next_tiers_to_the_same_position(self):
        self.desk.update_config({"wide_size": 100000.0})
        self.setup_convergence(gap=0.0019)
        self.desk.cycle()
        (p,) = self.desk.positions
        first_qty = [leg["qty"] for leg in p["legs"]]
        opened = self.clock
        self.move_to(0.0032)
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1)
        d = p["decision"]
        self.assertEqual([t["filled"] for t in d["tiers"]], [True, True, True])
        long, short = p["legs"]
        self.assertAlmostEqual(long["qty"], short["qty"])
        self.assertGreater(long["qty"], 3 * first_qty[0])
        self.assertEqual(len(long["adds"]), 1)
        self.assertAlmostEqual(d["qty"], long["qty"])
        # The short leg's average entry moved up toward the wider gap.
        self.assertGreater(short["entry"], 100 * math.exp(0.0019) - 0.02)
        # Funding before the add is settled on what was held then.
        self.assertAlmostEqual(self.desk.held_at(p, long, opened + 1), first_qty[0])
        self.assertAlmostEqual(self.desk.held_at(p, long, self.clock + 1), long["qty"])
        # Each tier enters once.
        self.move_to(0.0033)
        self.desk.cycle()
        self.assertEqual(len(long["adds"]), 1)

    def test_gap_past_the_widest_move_is_cut_after_it_holds(self):
        self.setup_convergence()
        self.desk.cycle()
        (p,) = self.desk.positions
        d = p["decision"]
        past = d["widest"] * 1.15  # past the 10% stop, inside the 25% hard stop
        self.move_to(past)
        self.desk.cycle(entries=False)
        self.assertEqual(len(self.desk.positions), 1)
        self.assertIsNotNone(p["stop_since"])
        self.move_to(past, after=self.desk.cfg["wide_conv_stop_minutes"] * 60)
        self.desk.cycle(entries=False)
        self.assertEqual(self.desk.positions, [])
        self.assertEqual(
            self.desk.closed[-1]["close_reason"],
            "Fark geçmiş en büyük açılmanın ötesinde kaldı; zarar kesildi",
        )
        self.assertLess(self.desk.closed[-1]["net"], 0)

    def test_gap_back_inside_the_stop_resets_the_wait(self):
        self.setup_convergence()
        self.desk.cycle()
        (p,) = self.desk.positions
        self.move_to(p["decision"]["widest"] * 1.15)
        self.desk.cycle(entries=False)
        self.move_to(0.0031, after=120)
        self.desk.cycle(entries=False)
        self.assertIsNone(p["stop_since"])
        self.move_to(p["decision"]["widest"] * 1.15, after=240)
        self.desk.cycle(entries=False)
        self.assertEqual(len(self.desk.positions), 1)

    def test_hard_stop_cuts_at_once(self):
        self.setup_convergence()
        self.desk.cycle()
        self.move_to(self.desk.positions[0]["decision"]["widest"] * 1.4)
        self.desk.cycle(entries=False)
        self.assertEqual(self.desk.positions, [])
        self.assertIn(
            self.desk.closed[-1]["close_reason"],
            (
                "Fark geçmiş en büyük açılmayı sert sınırın ötesinde aştı; zarar kesildi",
                "Spread stres bütçesi aşıldı",
            ),
        )

    def test_history_that_keeps_breaking_its_record_sets_no_stop(self):
        # The swings double over the month: the first half's widest move is passed
        # again and again in the second half.
        self.setup_convergence(grow=1.0, gap=0.0045)
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        reasons = " ".join(self.desk.assessed[0]["reasons"])
        self.assertIn("Geçmiş sınır ileri dönemde aşıldı", reasons)

    def test_wider_tier_needs_its_own_history_of_coming_back(self):
        # ±20bp swings every 12h, and two one-hour spikes on a crest to 40bp: moves to 28bp and
        # 34bp (tiers 2 and 3) happened only twice, so only tier 1 risk is taken.
        self.desk.update_config({"wide_size": 100000.0})
        self.setup_convergence(
            amplitude=0.002, gap=0.0036, spikes=((207, 0.002), (507, 0.002))
        )
        self.desk.candidates = wide_paper.rank_spreads(self.src.rows, T0)
        x = self.desk.assess(self.desk.candidates[0])
        h = x["convergence_history"]
        self.assertAlmostEqual(h["widest"], 0.004, places=4)
        self.assertEqual([t["qualified"] for t in h["tiers"]], [True, False, False])
        self.assertTrue(x["eligible"], x["reasons"])
        self.assertEqual([t["filled"] for t in x["tiers"]], [True, False, False])
        self.assertAlmostEqual(x["budget"], 0.2 * self.desk.risk_budget())

    def test_convergence_charges_funding_paid_while_waiting(self):
        self.setup_convergence()
        # Long venue charges heavy hourly funding during the expected hold.
        self.src.rows[0] = row("Hyperliquid", rate=0.002, fee=0.0002, mark=100.0)
        self.desk.cycle()
        c = self.desk.assessed[0]
        self.assertLess(c["conservative_funding"], 0)
        self.assertEqual(self.desk.positions, [])

    def test_convergence_charges_funding_over_the_longest_hold(self):
        self.setup_convergence()
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        d = self.desk.positions[0]["decision"]
        hold, longest, size = d["expected_hours"], d["max_hold_hours"], d["size"]
        self.assertLess(hold, longest)
        # An hourly cost the expected hold could absorb, but not the longest one.
        rate = (d["net"] - self.desk.cfg["wide_min_net"]) / (
            size * (hold + longest) / 2
        )
        store = bot.Store(os.path.join(self.dir.name, "q.sqlite3"))
        self.addCleanup(store.db.close)
        fresh = WidePaper(store, self.src, sleep=lambda s: None)
        fresh.update_config({"wide_funding_enabled": False})
        self.src.rows[0] = row("Hyperliquid", rate=rate, fee=0.0002, mark=100.0)
        fresh.cycle()
        self.assertEqual(fresh.positions, [])
        x = fresh.assessed[0]
        self.assertAlmostEqual(
            x["funding_cost_reserve"], -rate * x["size"] * longest, delta=rate * size
        )

    def test_a_small_funding_reversal_is_held_through(self):
        self.setup_pair()
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)

        def rates(long_rate, short_rate):
            self.src.rows = [
                row("Hyperliquid", rate=long_rate, fee=0.00045, at=self.clock),
                row(
                    "Binance",
                    rate=short_rate,
                    interval=28800,
                    fee=0.0005,
                    at=self.clock,
                ),
            ]
            for v in ("Hyperliquid", "Binance"):
                self.src.books[(v, "BTC")] = book(100.0, self.clock)

        # The carry turns slightly against the position: over the horizon it costs
        # less than closing now, so the position is held.
        for step in (60, 31 * 60):
            self.clock += step
            rates(0.00001, 0.0)
            self.desk.cycle(entries=False)
        self.assertEqual(len(self.desk.positions), 1)
        p = self.desk.positions[0]
        self.assertLess(
            -p["remaining_funding_fraction"] * p["decision"]["size"], p["exit_cost"]
        )
        # A reversal that costs more than leaving still closes it.
        for step in (60, 31 * 60):
            self.clock += step
            rates(0.001, -0.0005)
            self.desk.cycle(entries=False)
        self.assertEqual(self.desk.positions, [])
        self.assertEqual(
            self.desk.closed[0]["close_reason"], "Funding avantajı tersine döndü"
        )

    # --- funding exits follow the carry ----------------------------------------------
    def test_price_windfall_alone_does_not_close_the_carry(self):
        self.setup_pair()
        self.desk.cycle()
        (p,) = self.desk.positions
        # Prices jump in the position's favour (short leg falls) while the funding
        # advantage lasts: the hedge's noise is not a reason to leave.
        self.clock += 600
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        self.src.books[("Hyperliquid", "BTC")] = book(100.0, self.clock)
        self.src.books[("Binance", "BTC")] = book(99.0, self.clock)
        self.desk.cycle(entries=False)
        self.assertEqual(len(self.desk.positions), 1)
        self.assertGreater(p["net_if_closed"], p["decision"]["net"])
        self.assertGreater(p["remaining_funding_fraction"], 0)

    def test_retired_profit_lock_setting_is_dropped(self):
        self.store.put("wide_config", dict(wide_lock_trail=0.5, wide_size=700.0))
        desk = WidePaper(self.store, self.src, sleep=lambda s: None)
        self.assertNotIn("wide_lock_trail", desk.cfg)
        self.assertEqual(desk.cfg["wide_size"], 700.0)
        with self.assertRaises(ValueError):
            desk.update_config({"wide_lock_trail": 0.5})

    # --- final spec guards ---------------------------------------------------------
    def test_entry_reason_and_strategy_recorded(self):
        self.setup_pair()
        self.desk.cycle()
        p = self.desk.positions[0]
        self.assertEqual(p["strategy"], "funding")
        self.assertIn("Funding: short Binance", p["entry_reason"])
        self.desk.manual_close.add(p["id"])
        self.desk.cycle(entries=False)
        closed = self.desk.closed[-1]
        self.assertEqual(closed["close_reason"], "Kullanıcı paper kapatma talebi")
        self.assertTrue(closed["entry_reason"])

    def test_report_keeps_strategies_apart(self):
        self.setup_pair()
        self.desk.cycle()
        self.desk.manual_close.add(self.desk.positions[0]["id"])
        self.desk.cycle(entries=False)
        r = self.desk.report()
        self.assertEqual(r["funding"]["trades"], 1)
        self.assertEqual(r["convergence"]["trades"], 0)
        closed = self.desk.closed[-1]
        self.assertAlmostEqual(
            r["funding"]["net"],
            closed["price_pnl"] + closed["funding"] - closed["fees"],
        )

    def test_funding_api_down_still_monitors_and_closes(self):
        self.setup_pair()
        self.desk.cycle()
        p = self.desk.positions[0]

        def broken(*a):
            raise ValueError("funding API kapalı")

        self.src.funding = broken
        self.desk.cache.clear()
        self.clock = T0 + 120
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        for v in ("Hyperliquid", "Binance"):
            self.src.books[(v, "BTC")] = book(100.0, self.clock)
        self.desk.manual_close.add(p["id"])
        self.desk.cycle(entries=False)
        self.assertEqual(self.desk.positions, [])
        self.assertTrue(self.desk.closed[-1]["pending_funding"])

    def test_market_list_failure_keeps_monitoring(self):
        self.setup_pair()
        self.desk.cycle()
        pid = self.desk.positions[0]["id"]

        def broken():
            raise ValueError("tarayıcı kapalı")

        self.src.market_rows = broken
        self.desk.manual_close.add(pid)
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        self.assertIn("Piyasa listesi alınamadı", self.desk.error)

    def test_leg_margin_alarm_closes_alone(self):
        self.setup_pair()
        self.desk.cycle()
        p = self.desk.positions[0]
        short = next(l for l in p["legs"] if l["side"] == "short")
        # Short venue rallies; the long venue's gain does not protect this leg's margin.
        self.desk.update_config({"wide_risk": 10000, "wide_daily_loss": 100000})
        p["decision"]["stress_loss"] = 1e9
        self.src.books[("Binance", "BTC")] = book(100.0 * 1.2, T0)
        self.src.books[("Hyperliquid", "BTC")] = book(100.0 * 1.2, T0)
        self.desk.cycle(entries=False)
        self.assertEqual(self.desk.positions, [])
        self.assertEqual(
            self.desk.closed[-1]["close_reason"], "Bacak marjin riski: Binance"
        )

    def test_unstable_spread_blocks_funding_entry(self):
        self.setup_pair()
        self.desk.update_config({"wide_max_spread_sd_bps": 1})
        self.src.history[("Binance", "BTC")] = candles(T0, noise=0.5)
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        self.assertTrue(
            any("istikrarlı değil" in r for r in self.desk.assessed[0]["reasons"])
        )

    # --- funding decision: 1, 3 and 7 days; the long window is context ---------------
    def carry_history(self, venue, interval, rate_of, days=30):
        """Payments whose rate depends on their age in whole days (0 = the last 24h)."""
        return [
            dict(ts=t, rate=rate_of((T0 - t) // 86400), value=None, audit_id=None)
            for t in range(T0 - days * 86400 + interval, T0 + 1, interval)
        ]

    def set_carry(self, good_days, stored=False):
        """Default pair (long Hyperliquid 1h, short Binance 8h): days in `good_days` pay
        this direction 0.48% a day, the other days the same against it."""
        for v, iv, good in (
            ("Hyperliquid", 3600, -0.0001),
            ("Binance", 28800, 0.0008),
        ):
            rows = self.carry_history(
                v, iv, lambda d: good if d in good_days else -good
            )
            self.src.rates[(v, "BTC")] = rows
            if stored:
                self.src.stored[(v, "BTC")] = rows

    def reasons(self):
        (x,) = self.desk.assessed
        return [wide_paper.reason_group(r) for r in x["reasons"]]

    def test_advantage_resting_on_one_day_is_refused(self):
        self.setup_pair()
        # Today pays well; the six days before paid a little against this direction.
        for v, iv, today, before in (
            ("Hyperliquid", 3600, -0.0001, 0.00002),
            ("Binance", 28800, 0.0008, -0.0001),
        ):
            self.src.rates[(v, "BTC")] = self.carry_history(
                v, iv, lambda d: today if d == 0 else before
            )
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        (x,) = self.desk.assessed
        self.assertGreater(sum(x["realized_days"]), 0)
        self.assertIn("Avantaj tek bir güne dayanıyor", self.reasons())

    def test_direction_that_turned_this_week_is_refused(self):
        self.setup_pair()
        # The last two days favour this direction; the five before paid against it.
        self.set_carry({0, 1})
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        self.assertGreater(sum(self.desk.assessed[0]["realized_days"][:3]), 0)
        self.assertIn(
            "7 günde gerçekleşen funding bu yönde avantaj göstermiyor", self.reasons()
        )

    def test_last_three_days_against_the_direction_are_refused(self):
        self.setup_pair()
        self.set_carry({0, 3, 4, 5, 6})
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        reasons = self.reasons()
        self.assertIn(
            "3 günde gerçekleşen funding bu yönde avantaj göstermiyor", reasons
        )
        self.assertNotIn(
            "7 günde gerçekleşen funding bu yönde avantaj göstermiyor", reasons
        )

    def test_expected_funding_takes_the_three_day_average_when_lower(self):
        self.setup_pair()
        # Today pays twice what the two days before did.
        for v, iv, rate in (("Hyperliquid", 3600, -0.0001), ("Binance", 28800, 0.0008)):
            self.src.rates[(v, "BTC")] = self.carry_history(
                v, iv, lambda d: rate if d == 0 else rate / 2
            )
        self.desk.cycle()
        (x,) = self.desk.assessed
        self.assertLess(x["funding_day3"], x["funding_day1"])
        self.assertAlmostEqual(x["conservative_funding"], x["funding_day3"])
        self.assertAlmostEqual(
            x["funding_day3"], x["size"] * (0.0048 + 2 * 0.0024) / 3, places=6
        )

    def test_weak_long_window_correlation_halves_the_size(self):
        self.setup_pair()
        end = bot.hour(T0)
        h = candles(T0)
        # Older than 8 days, Binance printed noise the other venue never saw.
        self.src.history[("Binance", "BTC")] = dict(
            h,
            prices={
                t: p + (0.5 if t < end - 8 * 86400 and (t // 3600) % 2 else 0.0)
                for t, p in h["prices"].items()
            },
        )
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        p = self.desk.positions[0]
        ctx = p["decision"]["context"]
        self.assertEqual(ctx["price_days"], 30)
        self.assertLess(ctx["corr_long"], 0.9)
        self.assertGreater(self.desk.assessed[0]["stats"]["corr"], 0.9)
        self.assertEqual(ctx["confidence"], 0.5)
        self.assertLessEqual(p["decision"]["size"], 500 + 1e-6)
        self.assertIn("büyüklük yarıya indi", p["entry_reason"])
        # The long window never closes the position either.
        self.clock = T0 + 60
        self.desk.cache.clear()
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        for v in ("Hyperliquid", "Binance"):
            self.src.books[(v, "BTC")] = book(100.0, self.clock)
        self.desk.cycle(entries=False)
        self.assertEqual(len(self.desk.positions), 1)
        d = self.desk.verdict("BTC", "Hyperliquid", "Binance")["strategies"][0]
        self.assertEqual(d["decision"]["confidence"], 0.5)
        self.assertTrue(any("korelasyonu" in n for n in d["decision"]["notes"]))

    def test_long_window_funding_against_the_direction_halves_the_size(self):
        self.setup_pair()
        # The last week pays this direction; the three weeks before paid against it.
        self.set_carry(set(range(7)), stored=True)
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        ctx = self.desk.positions[0]["decision"]["context"]
        self.assertEqual(ctx["funding_days"], 30)
        self.assertLess(ctx["carry"], 0)
        self.assertEqual(ctx["confidence"], 0.5)
        self.assertLessEqual(self.desk.positions[0]["decision"]["size"], 500 + 1e-6)

    def test_long_window_agreeing_keeps_the_full_size(self):
        self.setup_pair()
        self.set_carry(set(range(30)), stored=True)
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        ctx = self.desk.positions[0]["decision"]["context"]
        self.assertEqual((ctx["confidence"], ctx["notes"]), (1.0, []))
        self.assertGreater(self.desk.positions[0]["decision"]["size"], 900)

    def test_missing_week_of_payments_waits_for_history(self):
        self.setup_pair()
        self.fund_history("Binance", 0.0008, 28800, days=3)
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        (x,) = self.desk.assessed
        self.assertTrue(any("7 günlük" in r for r in x["reasons"]), x["reasons"])
        self.assertEqual(self.reasons(), ["Geçmiş eksik"])
        (r,) = self.desk.rejected.values()
        self.assertEqual(r["until"], T0 + wide_paper.HISTORY_RETRY)

    def test_stored_payments_complete_a_short_venue_history(self):
        self.setup_pair()
        full = list(self.src.rates[("Binance", "BTC")])
        self.src.stored[("Binance", "BTC")] = full
        # The venue's API only returns the last 3 days.
        self.src.rates[("Binance", "BTC")] = [
            r for r in full if r["ts"] > T0 - 3 * 86400
        ]
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)

    def test_hole_in_the_week_of_payments_is_incomplete(self):
        self.setup_pair()
        self.src.rates[("Binance", "BTC")] = [
            r
            for r in self.src.rates[("Binance", "BTC")]
            if not T0 - 4 * 86400 < r["ts"] < T0 - 3 * 86400
        ]
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        self.assertEqual(self.reasons(), ["Geçmiş eksik"])

    def test_sparse_hourly_history_is_incomplete_not_cheaper(self):
        self.setup_pair()
        # Hyperliquid pays hourly; before the last day only every 8th payment came back.
        # Each gap is 8 hours, so a gap test alone passed and the missing payments read
        # as a smaller cost.
        self.src.rates[("Hyperliquid", "BTC")] = [
            r
            for r in self.src.rates[("Hyperliquid", "BTC")]
            if T0 - r["ts"] < 86400 or (T0 - r["ts"]) % 28800 == 0
        ]
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        (x,) = self.desk.assessed
        self.assertEqual(self.reasons(), ["Geçmiş eksik"])
        self.assertIn("42/168 ödeme", x["reasons"][0])

    def test_one_missing_payment_in_a_week_is_tolerated(self):
        self.setup_pair()
        rates = self.src.rates[("Hyperliquid", "BTC")]
        rates.remove(next(r for r in rates if r["ts"] == T0 - 3 * 86400))
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)

    def test_a_venue_that_moved_to_a_shorter_schedule_waits_for_its_week(self):
        self.setup_pair()
        # Binance paid every 8h until two days ago and hourly since: the week does not
        # yet hold the hourly schedule, so the pair waits instead of guessing.
        self.src.rows[1].update(interval=3600, rate=0.0008 / 8, raw_rate=0.0008 / 8)
        self.src.rows[1]["next"] = T0 + 3600
        self.src.rates[("Binance", "BTC")] = [
            dict(ts=t, rate=0.0008, value=None, audit_id=None)
            for t in range(T0 - 30 * 86400 + 28800, T0 - 2 * 86400 + 1, 28800)
        ] + [
            dict(ts=t, rate=0.0001, value=None, audit_id=None)
            for t in range(T0 - 2 * 86400 + 3600, T0 + 1, 3600)
        ]
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        self.assertEqual(self.reasons(), ["Geçmiş eksik"])

    def test_sparse_long_window_is_noted_without_cutting_the_size(self):
        self.setup_pair()
        self.set_carry(set(range(30)), stored=True)
        # The keeper's record holds only every 8th hourly payment older than 8 days.
        self.src.stored[("Hyperliquid", "BTC")] = [
            r
            for r in self.src.stored[("Hyperliquid", "BTC")]
            if T0 - r["ts"] < 8 * 86400 or (T0 - r["ts"]) % 28800 == 0
        ]
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        ctx = self.desk.positions[0]["decision"]["context"]
        self.assertEqual(ctx["confidence"], 1.0)
        self.assertNotIn("carry", ctx)
        self.assertTrue(any("geçmişi eksik" in n for n in ctx["notes"]), ctx["notes"])

    def test_funnel_says_why_markets_never_reach_the_ranking(self):
        self.setup_pair()
        self.src.rows += [
            row("Bybit", rate=0.0002, fee=None),
            dict(row("OKX", rate=0.0002), received=T0 - 3600),
            dict(row("Gate", rate=0.0002), price_type="last_trade"),
        ]
        self.desk.cycle(entries=False)
        f = self.desk.funnel
        self.assertEqual((f["markets"], f["usable"]), (5, 2))
        self.assertEqual(
            f["excluded"],
            {
                "Taker ücreti bilinmiyor": 1,
                "Veri eski": 1,
                "Yalnızca son işlem fiyatı": 1,
            },
        )
        self.assertEqual((f["assets"], f["pairs"]), (1, 1))
        self.assertEqual(f["strategies"]["funding"]["positive"], 1)
        # With per-venue accounts, a venue without one is counted apart.
        self.desk.reset(venue_capital=1000, venues=["Hyperliquid", "Extended"])
        self.desk.cycle(entries=False)
        self.assertEqual(self.desk.funnel["excluded"]["Hesap borsası değil"], 4)
        self.assertEqual(self.desk.funnel["pairs"], 0)

    def test_leverage_is_a_ceiling_cut_by_leg_volatility(self):
        self.setup_pair()
        self.desk.cycle(entries=False)
        c = self.desk.candidates[0]
        calm = self.desk.priced(c, 5.0, 0.001, 500, vol=0.0005, hold=8)
        self.assertEqual(calm["leverage"], 5.0)
        # 2% hourly legs: a 4-sigma move over 24h would eat a 5x leg's margin.
        wild = self.desk.priced(c, 5.0, 0.001, 500, vol=0.02, hold=24)
        self.assertLess(wild["leverage"], 1.5)
        self.assertGreaterEqual(wild["leverage"], 1.0)

    # --- review fixes ----------------------------------------------------------------
    def test_entry_refreshes_funding_and_skips_a_flip(self):
        self.setup_pair()
        first = [dict(r) for r in self.src.rows]
        flipped = [
            row("Hyperliquid", rate=0.001, interval=3600, fee=0.00045),
            row("Binance", rate=-0.0005, interval=28800, fee=0.0005),
        ]
        calls = iter([first, flipped])
        self.src.market_rows = lambda: [dict(r) for r in next(calls)]
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        self.assertIn(
            "Girişte funding yenilendi: avantaj kalmadı",
            self.desk.assessed[0]["reasons"],
        )
        # One pair examined, refused at entry: not counted as eligible as well.
        self.assertEqual(
            self.desk.funnel["strategies"]["funding"]["round"],
            dict(examined=1, eligible=0, opened=0, skipped=0, rejected=1),
        )

    def books_after_pricing(self, **later):
        """The first read of each venue's book prices the entry; later reads (the fills,
        an unwind) see the given books."""
        seen, base = {}, self.src.book

        def read(v, a):
            seen[v] = seen.get(v, 0) + 1
            if seen[v] > 1 and v in later:
                return dict(later[v], received=bot.now())
            return base(v, a)

        self.src.book = read

    def test_entry_without_any_fill_is_not_a_trade(self):
        self.setup_pair()
        # Between pricing and the fill the long venue's asks moved 0.5% up, past the
        # order's 0.1% limit: the immediate-or-cancel order fills nothing.
        self.books_after_pricing(Hyperliquid=book(100.5, T0))
        self.desk.cycle()
        self.assertEqual((self.desk.positions, self.desk.closed), ([], []))
        self.assertEqual(self.desk.report()["funding"]["trades"], 0)
        self.assertEqual(self.desk.wallet["cash"], self.desk.cfg["wide_capital"])
        (r,) = self.desk.rejected.values()
        self.assertIn("Giriş yapılmadı: Dolum olmadı", r["reason"])
        # The pair page and the funnel say it was not entered, never "opened".
        d = self.desk.verdict("BTC", "Hyperliquid", "Binance")["strategies"][0]
        self.assertEqual(d["decision"]["outcome"], "skipped")
        self.assertIn("Giriş yapılmadı: Dolum olmadı", d["decision"]["reasons"][0])
        self.assertEqual(self.desk.review["opened"], 0)
        self.assertEqual(
            self.desk.funnel["strategies"]["funding"]["round"],
            dict(examined=1, eligible=1, opened=0, skipped=1, rejected=0),
        )

    def test_thin_first_leg_sizes_the_second(self):
        self.setup_pair()
        self.books_after_pricing(
            Hyperliquid=dict(
                bids=[(99.99, 1000.0)], asks=[(100.01, 5.0), (101.0, 1000.0)]
            )
        )
        self.desk.cycle()
        (p,) = self.desk.positions
        planned = p["decision"]["planned_qty"]
        self.assertGreater(planned, 5.0)
        self.assertEqual([l["qty"] for l in p["legs"]], [5.0, 5.0])
        self.assertAlmostEqual(p["fill_ratio"], 5.0 / planned)
        self.assertAlmostEqual(p["decision"]["qty"], 5.0)
        kinds = [k for (k,) in self.store.db.execute("select kind from events")]
        self.assertIn("WIDE_FILL_PARTIAL", kinds)
        self.assertNotIn("unwound", p)

    def test_thin_second_leg_unwinds_the_unmatched_part(self):
        self.setup_pair()
        self.books_after_pricing(
            Binance=dict(bids=[(99.99, 4.0), (98.0, 1000.0)], asks=[(100.01, 1000.0)])
        )
        self.desk.cycle()
        (p,) = self.desk.positions
        planned = p["decision"]["planned_qty"]
        long, short = p["legs"]
        self.assertEqual((long["qty"], short["qty"]), (4.0, 4.0))
        (u,) = p["unwound"]
        self.assertEqual((u["venue"], u["qty"]), ("Hyperliquid", planned - 4.0))
        # The unmatched part is bought at the ask and sold back at the bid at once:
        # a real loss plus its fee, both in the trade's result and the wallet.
        self.assertLess(u["pnl"], 0)
        self.assertAlmostEqual(p["price_pnl"], u["pnl"])
        self.assertAlmostEqual(
            self.desk.wallet["cash"], 10000 - p["fees"] + p["price_pnl"]
        )
        self.assertAlmostEqual(
            long["margin"], 4.0 * long["entry"] / p["decision"]["leverage"]
        )

    def test_failed_unwind_keeps_the_hedge_and_shrinks_the_excess(self):
        self.setup_pair()
        # The short fills 4 of 10; unwinding the 6 unmatched long finds 1 bid only.
        seen, base = {}, self.src.book
        thin = {
            "Binance": dict(
                bids=[(99.99, 4.0), (98.0, 1000.0)], asks=[(100.01, 1000.0)]
            ),
            "Hyperliquid": dict(bids=[(99.99, 1.0)], asks=[(100.01, 1000.0)]),
        }

        def read(v, a):
            seen[v] = seen.get(v, 0) + 1
            if seen[v] > (1 if v == "Binance" else 2):
                return dict(thin[v], received=bot.now())
            return base(v, a)

        self.src.book = read
        self.desk.cycle()
        (p,) = self.desk.positions
        long, short = p["legs"]
        # One unit of the excess sold; the 4 matched units stay hedged, not closed.
        self.assertEqual((long["qty"], short["qty"]), (9.0, 4.0))
        self.assertFalse(long["closed"] or short["closed"])
        self.assertEqual(p["status"], "RECOVERY")
        self.assertEqual(
            (p["exposure"]["venue"], p["exposure"]["side"], p["exposure"]["qty"]),
            ("Hyperliquid", "long", 5.0),
        )
        self.assertEqual(self.desk.entry_halt(), "açık pozisyon RECOVERY durumunda")
        self.assertAlmostEqual(
            self.desk.wallet["cash"], 10000 - p["fees"] + p["price_pnl"]
        )
        # With the books back the rest closes and the record adds up.
        self.src.book = base
        self.clock += 60
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        self.desk.cycle(entries=False)
        self.assertEqual(self.desk.positions, [])
        c = self.desk.closed[-1]
        self.assertAlmostEqual(c["net"], c["price_pnl"] + c["funding"] - c["fees"])
        self.assertAlmostEqual(self.desk.wallet["cash"] - 10000, c["net"])

    def test_a_close_never_leaves_one_leg_alone(self):
        self.setup_pair()
        self.desk.cycle()
        (p,) = self.desk.positions
        base = self.src.book

        def read(v, a):
            if v == "Binance":
                raise ValueError("borsa yanıt vermiyor")
            return base(v, a)

        self.src.book = read
        self.desk.close(p, "Kullanıcı paper kapatma talebi")
        # The Hyperliquid leg could close, but alone it would leave the short unhedged.
        self.assertEqual([l["closed"] for l in p["legs"]], [False, False])
        self.assertEqual((p["status"], p["exposure"]), ("RECOVERY", None))
        self.src.book = base
        self.desk.close(p, "Kullanıcı paper kapatma talebi")
        self.assertEqual(self.desk.positions, [])

    def test_thin_books_close_the_matched_part_on_both_legs(self):
        self.setup_pair()
        self.desk.cycle()
        (p,) = self.desk.positions
        self.src.books[("Hyperliquid", "BTC")] = dict(
            bids=[(99.99, 3.0)], asks=[(100.01, 1000.0)], received=T0
        )
        self.src.books[("Binance", "BTC")] = dict(
            bids=[(99.99, 1000.0)], asks=[(100.01, 5.0)], received=T0
        )
        self.desk.close(p, "Kullanıcı paper kapatma talebi")
        long, short = p["legs"]
        self.assertEqual((long["qty"], short["qty"]), (7.0, 7.0))
        self.assertEqual((p["status"], p["exposure"]), ("RECOVERY", None))
        self.assertEqual([x["qty"] for x in long["partial_exits"]], [3.0])
        self.assertAlmostEqual(
            self.desk.wallet["cash"], 10000 - p["fees"] + p["price_pnl"]
        )

    def test_partial_fill_below_a_venue_minimum_is_no_fill(self):
        self.setup_pair()
        self.src.rows[0]["minimum"] = 6.0
        self.books_after_pricing(
            Hyperliquid=dict(
                bids=[(99.99, 1000.0)], asks=[(100.01, 5.0), (101.0, 1000.0)]
            )
        )
        self.desk.cycle()
        self.assertEqual((self.desk.positions, self.desk.closed), ([], []))

    def test_old_no_fill_records_are_not_counted(self):
        self.desk.closed.append(
            dict(
                strategy="funding",
                legs=[],
                net=0.0,
                price_pnl=0.0,
                funding=0.0,
                fees=0.0,
            )
        )
        self.assertEqual(self.desk.report()["funding"]["trades"], 0)

    def test_entry_skips_stale_funding_quote(self):
        self.setup_pair()
        first = [dict(r) for r in self.src.rows]
        stale = [dict(r, funding_received=T0 - 600) for r in first]
        calls = iter([first, stale])
        self.src.market_rows = lambda: [dict(r) for r in next(calls)]
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        self.assertIn("Girişte funding verisi eski", self.desk.assessed[0]["reasons"])

    def test_stale_bulk_quote_is_reread_at_entry(self):
        self.setup_pair()
        first = [dict(r) for r in self.src.rows]
        stale = [dict(r, funding_received=T0 - 600) for r in first]
        calls = iter([first, stale])
        self.src.market_rows = lambda: [dict(r) for r in next(calls)]
        current = {(r["venue"], r["asset"]): r for r in first}
        asked = []

        def current_funding(v, a):
            asked.append(v)
            r = current[(v, a)]
            return dict(
                raw_rate=r["raw_rate"],
                rate=r["rate"],
                interval=r["interval"],
                next=r["next"],
                funding_received=T0,
            )

        self.src.current_funding = current_funding
        self.desk.cycle()
        self.assertEqual(sorted(asked), ["Binance", "Hyperliquid"])
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)

    def test_late_rate_after_close_replaces_unverified_hour(self):
        self.setup_pair()
        self.desk.cycle()
        p = self.desk.positions[0]
        qty = p["legs"][0]["qty"]
        pay = T0 + 8 * 3600
        # Binance never published its 8h payment; the trade closes after the wait.
        self.clock = pay + bot.FUNDING_UNVERIFIED_AFTER + 60
        self.desk.cache.clear()
        for v in ("Hyperliquid", "Binance"):
            self.src.books[(v, "BTC")] = book(100.0, self.clock)
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        self.desk.manual_close.add(p["id"])
        self.desk.cycle(entries=False)
        closed = self.desk.closed[-1]
        self.assertIn("Binance", {x["venue"] for x in closed["funding_unverified"]})
        self.assertFalse(
            [x for x in closed.get("pending_funding", []) if x.startswith("Binance")]
        )
        before = closed["funding"]
        # The rate is published late, after the close.
        self.store.mark("Binance", "BTC", pay - 10, 100.0)
        self.src.rates[("Binance", "BTC")].append(
            dict(ts=pay, rate=0.0008, value=None, audit_id=None)
        )
        self.clock += 600
        self.desk.cache.clear()
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        self.desk.cycle(entries=False)
        self.assertAlmostEqual(closed["funding"] - before, qty * 100 * 0.0008, places=6)
        self.assertNotIn(
            "Binance", {x["venue"] for x in closed.get("funding_unverified", [])}
        )
        self.assertAlmostEqual(
            closed["net"], closed["price_pnl"] + closed["funding"] - closed["fees"]
        )

    def test_unpublished_cost_on_a_paying_leg_is_charged_in_the_stress(self):
        # The long leg pays 0.01% an hour; the short receives 0.30% every 8h.
        self.setup_pair(long_rate=0.0001, short_rate=0.003)
        self.desk.cycle()
        (p,) = self.desk.positions
        qty = p["legs"][0]["qty"]
        pay = T0 + 8 * 3600
        self.src.rates[("Binance", "BTC")].append(
            dict(ts=pay, rate=0.003, value=None, audit_id=None)
        )
        self.store.mark("Binance", "BTC", pay - 10, 100.0)
        for h in range(1, 11):
            self.store.mark("Hyperliquid", "BTC", T0 + h * 3600 - 10, 100.0)
        # Hyperliquid publishes none of its hourly payments before the close.
        self.clock = pay + 3600 + bot.FUNDING_UNVERIFIED_AFTER + 60
        self.desk.cache.clear()
        for v in ("Hyperliquid", "Binance"):
            self.src.books[(v, "BTC")] = book(100.0, self.clock)
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        self.desk.manual_close.add(p["id"])
        self.desk.cycle(entries=False)
        closed = self.desk.closed[-1]
        # The last hourly payment before the close settles an hour later.
        self.clock += 3700
        self.desk.cache.clear()
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        self.desk.cycle(entries=False)
        # Booked as 0, each would have cost qty * 100 * 0.01%.
        self.assertAlmostEqual(closed["funding"], qty * 100 * 0.003)
        (entry,) = self.desk.trade_log()
        self.assertFalse(entry["pending"])
        self.assertEqual(entry["unverified"], 10)
        self.assertAlmostEqual(entry["unverified_risk"], -10 * qty * 100 * 0.0001)
        r = self.desk.period_report()
        stress = {x["label"]: x["net"] for x in r["stress"]}
        self.assertAlmostEqual(
            stress["Doğrulanmamış ödemeler aleyhte"],
            r["net"] + entry["unverified_risk"],
        )
        check = self.check(r, "Doğrulanmamış funding aleyhte sayılınca net")
        self.assertEqual(check["ok"], r["net"] + entry["unverified_risk"] > 0)
        self.assertIn("10 ödeme", check["value"])
        import csv, io

        rows = list(
            csv.reader(io.StringIO(wide_paper.trades_csv(self.desk.trade_log())))
        )
        row = dict(zip(rows[0], rows[1]))
        self.assertAlmostEqual(
            float(row["unverified_risk_usd"]), entry["unverified_risk"]
        )
        # Published late, the payments replace the zeros and the charge goes away.
        self.src.rates[("Hyperliquid", "BTC")] += [
            dict(ts=T0 + h * 3600, rate=0.0001, value=None, audit_id=None)
            for h in range(1, 11)
        ]
        self.clock += 600
        self.desk.cache.clear()
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        self.desk.cycle(entries=False)
        (entry,) = self.desk.trade_log()
        self.assertEqual((entry["unverified"], entry["unverified_risk"]), (0, 0))
        self.assertAlmostEqual(closed["funding"], qty * 100 * (0.003 - 10 * 0.0001))

    def test_leverage_settings_capped_at_five(self):
        for key in ("wide_leverage", "wide_conv_leverage"):
            with self.assertRaises(ValueError):
                self.desk.update_config({key: 6})
        self.desk.update_config({"wide_leverage": 5, "wide_conv_leverage": 5})

    def test_reset_archives_the_period_and_unlocks_capital(self):
        self.desk.closed = [
            dict(
                asset="ERA",
                strategy="funding",
                legs=[dict(venue="A")],
                net=-3.0,
                price_pnl=-1.0,
                funding=0.5,
                fees=2.5,
                closed_at=T0,
            )
        ]
        self.desk.wallet = {"cash": 997.0}
        self.desk.cfg["wide_capital"] = 1000.0
        with self.assertRaises(ValueError):
            self.desk.update_config({"wide_capital": 10000})
        self.desk.positions = [dict(id="x", asset="BTC", legs=[], status="OPEN")]
        with self.assertRaisesRegex(ValueError, "Açık pozisyon"):
            self.desk.reset(10000)
        self.desk.positions = []
        self.desk.reset(10000)
        self.assertEqual(self.desk.closed, [])
        self.assertEqual(self.desk.wallet["cash"], 10000)
        self.assertEqual(self.desk.cfg["wide_capital"], 10000)
        (e,) = self.desk.snapshot()["epochs"]
        self.assertEqual(
            (e["capital"], e["cash"], e["trades"], e["net"]), (1000, 997, 1, -3)
        )
        self.assertNotIn("closed", e)
        # The archive survives a restart; the new period starts clean.
        again = WidePaper(self.store, self.src, sleep=lambda s: None)
        self.assertEqual((again.wallet["cash"], again.closed), (10000, []))
        self.assertEqual(
            self.store.get("wide_epochs", [])[0]["closed"][0]["asset"], "ERA"
        )

    # --- per-venue collateral ------------------------------------------------
    def setup_account_pair(self):
        """Hyperliquid long, Extended short: both are account venues."""
        self.setup_pair()
        self.src.rows = [
            dict(r, venue="Extended") if r["venue"] == "Binance" else r
            for r in self.src.rows
        ]
        for d in (self.src.books, self.src.history, self.src.rates):
            if ("Binance", "BTC") in d:
                d[("Extended", "BTC")] = d.pop(("Binance", "BTC"))

    def test_per_venue_reset_gives_each_account_its_own_collateral(self):
        self.desk.reset(venue_capital=1000)
        venues = self.desk.wallet["venues"]
        self.assertEqual(set(venues), set(wide_paper.ACCOUNT_VENUES))
        self.assertTrue(all(x == 1000 for x in venues.values()))
        self.assertGreaterEqual(len(venues), 15)
        self.assertEqual(self.desk.wallet["cash"], 1000 * len(venues))
        self.assertEqual(self.desk.cfg["wide_capital"], 1000 * len(venues))
        with self.assertRaisesRegex(ValueError, "Borsa başı"):
            self.desk.update_config({"wide_capital": 9000})
        again = WidePaper(self.store, self.src, sleep=lambda s: None)
        self.assertEqual(again.wallet["venues"], venues)

    def test_a_new_period_takes_the_current_defaults(self):
        # A desk that traded under the old defaults kept them; its next period does not.
        self.desk.cfg.update(wide_paper.OLD_DEFAULTS)
        self.desk.cfg["wide_size"] = 700.0  # changed by the user: stays
        self.desk.closed = [dict(asset="ERA", legs=[dict(venue="A")], net=1.0)]
        self.desk.reset(venue_capital=1000)
        for k in (
            "wide_max_positions",
            "wide_risk",
            "wide_daily_loss",
            "wide_deep_top",
        ):
            self.assertEqual(self.desk.cfg[k], wide_paper.WIDE_DEFAULTS[k], k)
        self.assertEqual(self.desk.cfg["wide_size"], 700.0)
        self.assertEqual(self.desk.cfg["wide_capital"], 1000 * 15)

    def test_per_venue_reset_takes_a_chosen_venue_list(self):
        self.desk.reset(venue_capital=500, venues=["Binance", "Hyperliquid"])
        self.assertEqual(
            self.desk.wallet["venues"], {"Binance": 500, "Hyperliquid": 500}
        )
        with self.assertRaisesRegex(ValueError, "Taranmayan borsa"):
            self.desk.reset(venue_capital=500, venues=["Binance", "Nowhere"])
        with self.assertRaisesRegex(ValueError, "En az iki"):
            self.desk.reset(venue_capital=500, venues=["Binance"])
        self.assertTrue(
            set(wide_paper.ACCOUNT_VENUES) <= set(wide_paper.venue_choices())
        )

    def test_per_venue_trades_only_between_account_venues(self):
        self.setup_pair()  # Hyperliquid / Binance: Binance holds no account here
        self.desk.reset(venue_capital=1000, venues=["Hyperliquid", "Extended"])
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        self.assertTrue(
            all("Binance" not in (c["long"], c["short"]) for c in self.desk.candidates)
        )

    def test_per_venue_charges_each_leg_to_its_own_venue(self):
        self.setup_account_pair()
        self.desk.reset(venue_capital=1000)
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        p = self.desk.positions[0]
        venues = self.desk.wallet["venues"]
        for leg in p["legs"]:
            self.assertAlmostEqual(venues[leg["venue"]], 1000 - leg["entry_fee"])
            # Margin and fee fit inside the venue's own collateral after the reserve.
            self.assertLessEqual(
                leg["margin"] + leg["entry_fee"],
                1000 * (1 - self.desk.cfg["wide_reserve"]) + 1e-6,
            )
        self.assertAlmostEqual(self.desk.wallet["cash"], sum(venues.values()))
        self.assertEqual(venues["Lighter"], 1000)
        b = {x["venue"]: x for x in self.desk.snapshot()["venue_balances"]}
        self.assertTrue(b["Extended"]["data"])
        self.assertFalse(b["TxFlow"]["data"])

    def test_per_venue_size_follows_the_smaller_venue(self):
        self.setup_account_pair()
        self.desk.reset(venue_capital=1000)
        self.desk.wallet["venues"]["Extended"] = 100.0
        self.desk.cfg.update(
            wide_risk=1000.0, wide_daily_loss=10000.0, wide_min_net=0.1
        )
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        short = next(l for l in self.desk.positions[0]["legs"] if l["side"] == "short")
        self.assertLessEqual(
            short["margin"] + short["entry_fee"],
            100 * (1 - self.desk.cfg["wide_reserve"]) + 1e-6,
        )

    def test_venue_collateral_is_not_covered_by_the_other_venue(self):
        self.setup_account_pair()
        self.desk.reset(venue_capital=1000)
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        short = next(l for l in self.desk.positions[0]["legs"] if l["side"] == "short")
        # Extended's account runs thin while Hyperliquid stays rich.
        self.desk.wallet["venues"]["Extended"] = short["margin"] * 0.4
        self.clock = T0 + 60
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        for v in ("Hyperliquid", "Extended"):
            self.src.books[(v, "BTC")] = book(100.0, self.clock)
        self.desk.cycle(entries=False)
        self.assertEqual(self.desk.positions, [])
        self.assertEqual(
            self.desk.closed[0]["close_reason"], "Borsa teminatı riski: Extended"
        )

    def test_order_size_follows_each_venue_lot_rules(self):
        lots = [dict(step=0.01, minimum=0.05, min_quote=10), dict(step=0.001)]
        self.assertAlmostEqual(wide_paper.fit_lots(2.3456, 100, lots), 2.34)
        with self.assertRaisesRegex(ValueError, "asgari emir miktarı"):
            wide_paper.fit_lots(0.049, 100, lots)
        with self.assertRaisesRegex(ValueError, "asgari emir tutarı"):
            wide_paper.fit_lots(0.09, 100, lots)
        with self.assertRaisesRegex(ValueError, "adımı uyumsuz"):
            wide_paper.fit_lots(1.0, 100, [dict(step=0.3), dict(step=0.2)])
        # Unpublished rules do not block; they are not invented either.
        self.assertEqual(wide_paper.fit_lots(1.2345, 100, [None, lot_free()]), 1.2345)

    def test_entry_quantity_is_rounded_to_the_coarser_step(self):
        self.setup_pair()
        self.src.rows = [dict(r, step=0.1, minimum=0.1) for r in self.src.rows]
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        qty = self.desk.positions[0]["legs"][0]["qty"]
        self.assertAlmostEqual(qty / 0.1, round(qty / 0.1))
        self.assertTrue(all(l["qty"] == qty for l in self.desk.positions[0]["legs"]))

    def test_entry_below_a_venue_minimum_is_refused(self):
        self.setup_pair()
        self.src.rows = [dict(r, minimum=10_000) for r in self.src.rows]
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        self.assertIn(
            "Borsanın asgari emir miktarının altında",
            self.desk.assessed[0]["reasons"][0],
        )

    def test_fill_waits_for_the_venue_measured_round_trip(self):
        self.setup_pair()
        slept = []
        self.desk.sleep = slept.append
        self.desk.rtt["Binance"].extend([0.2] * 9 + [2.5] * 3)
        self.desk.rtt["Hyperliquid"].extend([0.1] * 20)
        self.assertAlmostEqual(self.desk.latency("Binance"), 2.5)
        self.assertAlmostEqual(self.desk.latency("Hyperliquid"), 0.75)
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        legs = [l["venue"] for l in self.desk.positions[0]["legs"]]
        self.assertEqual(slept, [self.desk.latency(v) for v in legs])
        self.assertIn(2.5, slept)

    # --- user fees and the period report ---------------------------------------
    def test_user_fee_replaces_the_public_tariff(self):
        self.setup_pair()
        self.desk.set_fees({"Binance": 0.0002})
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        short = next(l for l in self.desk.positions[0]["legs"] if l["side"] == "short")
        self.assertEqual(short["fee_rate"], 0.0002)
        with self.assertRaisesRegex(ValueError, "%0 ile %1"):
            self.desk.set_fees({"Binance": 0.02})
        with self.assertRaisesRegex(ValueError, "Taranmayan"):
            self.desk.set_fees({"Nowhere": 0.0001})
        again = WidePaper(self.store, self.src, sleep=lambda s: None)
        self.assertEqual(again.fees, {"Binance": 0.0002})
        again.set_fees({"Binance": None})
        self.assertEqual(again.fees, {})

    def test_unknown_fee_venue_trades_once_the_user_enters_its_fee(self):
        self.setup_pair()
        self.src.rows = [
            dict(r, fee=None) if r["venue"] == "Binance" else r for r in self.src.rows
        ]
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        self.desk.set_fees({"Binance": 0.0005})
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)

    def test_venue_table_says_why_a_venue_cannot_trade(self):
        self.setup_pair()
        self.src.rows.append(row("RISEx", fee=None))
        self.desk.reset(venue_capital=1000)
        self.desk.cycle()
        b = {x["venue"]: x for x in self.desk.venue_balances()}
        self.assertEqual(
            (b["Hyperliquid"]["fee"], b["Hyperliquid"]["fee_source"]),
            (0.00045, "public"),
        )
        self.assertIsNone(b["Hyperliquid"]["reason"])
        self.assertFalse(b["RISEx"]["data"])
        self.assertEqual(b["RISEx"]["reason"], "Taker ücreti bilinmiyor")
        self.assertEqual(b["TxFlow"]["reason"], "Piyasa verisi yok")
        self.desk.set_fees({"RISEx": 0.0004})
        self.desk.cycle(entries=False)
        b = {x["venue"]: x for x in self.desk.venue_balances()}
        self.assertTrue(b["RISEx"]["data"])
        self.assertEqual(
            (b["RISEx"]["fee"], b["RISEx"]["fee_source"]), (0.0004, "user")
        )

    def check(self, report, label):
        return next(c for c in report["checks"] if c["label"] == label)

    def test_period_report_counts_downtime_pauses_and_changes(self):
        self.desk.reset(venue_capital=1000)
        self.desk.cycle()
        self.clock += 120
        self.desk.cycle(entries=False)
        self.clock += 3600  # the machine slept
        self.desk.cycle()
        self.desk.update_config({"wide_min_net": 0.6})
        # Saving unchanged settings or fees is not a change.
        self.desk.update_config({"wide_min_net": 0.6})
        self.desk.set_fees({})
        r = self.desk.period_report()
        self.assertAlmostEqual(r["downtime_hours"], 1.0)
        self.assertEqual(r["gaps"], 1)
        self.assertAlmostEqual(r["paused_hours"], 120 / 3600)
        self.assertEqual(r["changes"], 1)
        self.assertFalse(self.check(r, "Kesintisiz izleme")["ok"])
        self.assertTrue(self.check(r, "Yeni girişler açık kaldı")["ok"])
        self.assertFalse(self.check(r, "Dönem içinde ayar değişikliği")["ok"])
        self.assertFalse(self.check(r, "Süre")["ok"])
        self.assertFalse(r["ready"])
        # Time since the last cycle counts while the desk is down.
        self.clock += 1200
        self.assertAlmostEqual(self.desk.period_report()["downtime_hours"], 4800 / 3600)
        # A new period starts clean.
        self.desk.reset(venue_capital=1000)
        r = self.desk.period_report()
        self.assertEqual((r["gaps"], r["changes"], r["paused_hours"]), (0, 0, 0))
        self.assertEqual(self.store.get("wide_equity", None), [])

    def test_period_report_checks_how_profit_is_spread(self):
        self.desk.reset(venue_capital=1000)

        def trade(net):
            return dict(
                asset="X",
                strategy="funding",
                legs=[dict(venue="Binance"), dict(venue="Bybit")],
                net=net,
                price_pnl=0.0,
                funding=net,
                fees=0.0,
                closed_at=self.clock,
            )

        self.desk.closed = [trade(10), trade(5), trade(-2)] + [trade(1)] * 40
        r = self.desk.period_report()
        self.assertEqual((r["settled"], r["wins"]), (43, 42))
        self.assertAlmostEqual(r["net"], 53)
        self.assertAlmostEqual(r["profit_factor"], 27.5)
        self.assertAlmostEqual(r["top3_share"], 16 / 53)
        self.assertEqual(r["venues"], {"Binance": 43, "Bybit": 43})
        for label in (
            "İşlem sayısı",
            "Ücretler sonrası net",
            "Kazanç / kayıp oranı",
            "En iyi 3 işlemin nete payı",
        ):
            self.assertTrue(self.check(r, label)["ok"], label)
        # One lucky trade carrying the period fails the spread check.
        self.desk.closed = [trade(30), trade(1), trade(-1)]
        r = self.desk.period_report()
        self.assertFalse(self.check(r, "En iyi 3 işlemin nete payı")["ok"])
        self.assertFalse(self.check(r, "İşlem sayısı")["ok"])

    def test_fills_record_the_slippage_paid_from_mid(self):
        self.setup_pair()
        self.desk.cycle()
        (p,) = self.desk.positions
        qty = p["legs"][0]["qty"]
        # Asks 100.01 and bids 99.99 around a 100 mid, plus the 1 bp extra slippage.
        one_way = qty * ((100.01 * 1.0001 - 100) + (100 - 99.99 * 0.9999))
        self.assertAlmostEqual(p["slippage"], one_way)
        self.desk.manual_close.add(p["id"])
        self.clock += 60
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        self.desk.cycle(entries=False)
        (logged,) = self.desk.trade_log()
        self.assertAlmostEqual(logged["slippage"], 2 * one_way)

    def test_stress_runs_the_period_under_adverse_assumptions(self):
        self.desk.reset(venue_capital=1000)

        def trade(net, funding, fees, slippage, estimated=0.0):
            return dict(
                asset="X",
                strategy="funding",
                legs=[
                    dict(venue="Binance", side="long"),
                    dict(venue="Bybit", side="short"),
                ],
                net=net,
                price_pnl=net - funding + fees,
                funding=funding,
                estimated_funding=estimated,
                fees=fees,
                slippage=slippage,
                closed_at=self.clock,
            )

        self.desk.closed = [
            trade(8.0, 6.0, 1.0, 1.5, estimated=2.0),
            trade(1.0, 2.0, 0.6, 0.4),
        ]
        r = self.desk.period_report()
        stress = {x["label"]: x["net"] for x in r["stress"]}
        self.assertAlmostEqual(stress["Kayma 2 katı"], 9.0 - 1.9)
        self.assertAlmostEqual(stress["Ücret +%50"], 9.0 - 0.8)
        self.assertAlmostEqual(stress["Funding geliri −%25"], 9.0 - 2.0)
        self.assertAlmostEqual(stress["Tahmini değerlenen funding sayılmaz"], 7.0)
        # Together: the estimate removed, then a quarter of the funding left is cut.
        self.assertAlmostEqual(
            stress["Hepsi birlikte"], 9.0 - 1.9 - 0.8 - 2.0 - 0.25 * (4.0 + 2.0)
        )
        self.assertTrue(self.check(r, "Stres altında net (hepsi birlikte)")["ok"])
        self.assertEqual(r["stress_unmeasured"], 0)
        # A result that only holds on the paper's own fills does not pass.
        self.desk.closed = [trade(1.0, 3.0, 1.0, 1.5)]
        r = self.desk.period_report()
        self.assertTrue(self.check(r, "Ücretler sonrası net")["ok"])
        self.assertFalse(self.check(r, "Stres altında net (hepsi birlikte)")["ok"])

    def test_scorecard_per_strategy_and_pair(self):
        self.desk.reset(venue_capital=1000)

        def trade(net, long, short, opened, closed, price=0.0, fees=0.5, **kw):
            return dict(
                asset="X",
                strategy="funding",
                legs=[dict(venue=long, side="long"), dict(venue=short, side="short")],
                net=net,
                price_pnl=price,
                funding=net - price + fees,
                fees=fees,
                opened=opened,
                closed_at=closed,
                **kw,
            )

        h = 3600
        self.desk.closed = [
            trade(3.0, "Binance", "Bybit", 0, 2 * h, slippage=0.2),
            trade(-2.0, "Binance", "Bybit", h, 5 * h, price=-4.0, slippage=0.3),
            trade(1.0, "Binance", "Bybit", 2 * h, 6 * h, funding_unverified=[{}]),
            trade(0.5, "OKX", "Gate", 0, h, estimated_funding=0.5),
        ]
        whole, pair, other = self.desk.period_report()["scorecard"]
        self.assertEqual(
            (whole["pair"], whole["trades"], whole["net"]), (False, 4, 2.5)
        )
        self.assertEqual(
            (pair["long"], pair["short"], pair["trades"]), ("Binance", "Bybit", 3)
        )
        self.assertAlmostEqual(pair["average"], 2.0 / 3)
        self.assertEqual(
            (pair["wins"], pair["worst"], pair["worst_price"]), (2, -2.0, -4.0)
        )
        # Fees against funding + price before fees: 1.5 / (2.0 + 3.5) here.
        self.assertAlmostEqual(pair["fee_share"], 1.5 / 3.5)
        self.assertAlmostEqual(pair["hold_hours"], (2 + 4 + 4) / 3)
        # Cumulative net 3, 1, 2 by close time: the deepest fall from a high is 2.
        self.assertAlmostEqual(pair["drawdown"], 2.0)
        self.assertAlmostEqual(pair["slippage"], 0.5)
        self.assertEqual(pair["unverified"], 1)
        self.assertAlmostEqual(other["estimated_share"], 0.5 / 1.0)

    def test_period_report_tracks_drawdown_and_open_positions(self):
        self.desk.reset(venue_capital=1000)
        self.store.put(
            "wide_equity", [[0, 15000], [3600, 15200], [7200, 14900], [10800, 15100]]
        )
        self.desk.positions = [dict(id="x", asset="BTC", legs=[{}], net_if_closed=-4.0)]
        r = self.desk.period_report()
        self.assertAlmostEqual(r["max_drawdown"], 300)
        self.assertEqual((r["open"], r["open_net"]), (1, -4.0))

    def test_period_keeps_every_trade_beyond_the_closed_list(self):
        self.desk.reset(venue_capital=1000)
        for i in range(600):
            p = dict(
                id="t%d" % i,
                asset="X",
                strategy="funding",
                legs=[dict(venue="Binance"), dict(venue="Bybit")],
                net=1.0,
                price_pnl=0.0,
                funding=1.0,
                fees=0.0,
                closed_at=self.clock,
                close_reason="test",
            )
            self.desk.closed.append(p)
            self.desk.record_close(p)
        self.desk.persist()
        again = WidePaper(self.store, self.src, sleep=lambda s: None)
        self.assertEqual(len(again.closed), 500)
        r = again.period_report()
        self.assertEqual((r["settled"], r["net"]), (600, 600.0))
        f = again.report()["funding"]
        self.assertEqual((f["trades"], f["net"], f["funding"]), (600, 600.0, 600.0))
        # A late funding payment on a recent trade updates its ledger entry.
        again.closed[-1]["net"] = 2.0
        again.record_close(again.closed[-1])
        self.assertEqual(again.period_report()["net"], 601.0)
        again.reset(venue_capital=1000)
        self.assertEqual(again.period_report()["settled"], 0)

    def test_closing_a_position_records_it_in_the_ledger(self):
        self.setup_pair()
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        pid = self.desk.positions[0]["id"]
        self.desk.manual_close.add(pid)
        self.clock += 60
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        self.desk.cycle(entries=False)
        entry = self.store.get("wide_ledger", {})[pid]
        closed = self.desk.closed[-1]
        self.assertEqual(entry["venues"], ["Hyperliquid", "Binance"])
        self.assertEqual(entry["net"], closed["net"])
        self.assertEqual(
            (entry["asset"], entry["long"], entry["short"]),
            ("BTC", "Hyperliquid", "Binance"),
        )
        self.assertEqual(
            (entry["opened"], entry["closed_at"], entry["size"]),
            (closed["opened"], closed["closed_at"], closed["decision"]["size"]),
        )
        (logged,) = self.desk.trade_log()
        self.assertEqual((logged["id"], logged["net"]), (pid, closed["net"]))
        # Why it opened and why it closed travel with the trade.
        self.assertTrue(
            entry["entry_reason"].startswith("Funding: short Binance, long Hyperliquid")
        )
        self.assertEqual(entry["reason"], "Kullanıcı paper kapatma talebi")

    def test_trade_log_is_latest_first_and_exports_to_csv(self):
        self.desk.reset(venue_capital=1000)
        for i, closed_at in enumerate((T0 + 300, T0 + 900, T0 + 600)):
            p = dict(
                id="t%d" % i,
                asset="BTC",
                strategy="funding",
                legs=[
                    dict(side="long", venue="Bybit"),
                    dict(side="short", venue="Aster"),
                ],
                opened=T0,
                closed_at=closed_at,
                decision=dict(size=1000.0),
                net=float(i),
                price_pnl=-0.5,
                funding=float(i) + 1,
                fees=0.5,
                close_reason="Kâr kilidi",
                entry_reason="Funding: short Aster, long Bybit",
            )
            self.desk.closed.append(p)
            self.desk.record_close(p)
        log = self.desk.trade_log()
        self.assertEqual([x["id"] for x in log], ["t1", "t2", "t0"])
        self.assertEqual(self.desk.snapshot()["trades"][0]["id"], "t1")
        import csv, io

        rows = list(csv.reader(io.StringIO(wide_paper.trades_csv(log))))
        self.assertEqual(
            rows[0][:6], ["id", "asset", "strategy", "long", "short", "opened_utc"]
        )
        self.assertEqual(len(rows), 4)
        row = dict(zip(rows[0], rows[1]))
        self.assertEqual(
            [
                row[k]
                for k in ("id", "long", "short", "opened_utc", "closed_utc", "size_usd")
            ],
            ["t1", "Bybit", "Aster", str(T0), str(T0 + 900), "1000.0"],
        )
        self.assertEqual(
            (row["entry_reason"], row["close_reason"]),
            ("Funding: short Aster, long Bybit", "Kâr kilidi"),
        )
        # A trade from before partial fills and slippage records: full fill, no slippage.
        self.assertEqual((row["fill_ratio"], row["slippage_usd"]), ("1.0", ""))

    def test_cycles_record_hourly_equity(self):
        self.desk.reset(venue_capital=1000)
        self.desk.cycle()
        self.clock += 60
        self.desk.cycle()
        self.clock += 3600
        self.desk.cycle()
        samples = self.store.get("wide_equity", [])
        self.assertEqual([t for t, e in samples], [bot.hour(T0), bot.hour(T0 + 3660)])
        self.assertEqual(samples[0][1], 15000)


FUNDING_WAIT = bot.FUNDING_UNVERIFIED_AFTER


class DeskIntegrationTests(unittest.TestCase):
    def test_desk_routes_wide_config_and_exposes_snapshot(self):
        from test_risk_scan import FiveFixture
        from desk_analytics import DeskEngine
        from wide_paper import EngineSource

        with tempfile.TemporaryDirectory() as d:
            f = FiveFixture()
            f.markets()
            f.history = {}
            e = DeskEngine(os.path.join(d, "t.db"), f)
            try:
                e.update_config({"wide_max_positions": 4})
                self.assertEqual(e.wide_paper.cfg["wide_max_positions"], 4)
                self.assertNotIn("wide_max_positions", e.cfg)
                source = EngineSource(e)
                rows = source.market_rows()
                self.assertTrue(rows)
                # Feed venues with a known taker fee join; the others say why not.
                self.assertTrue(all(r["venue"] in wide_paper.FEED_VENUES for r in rows))
                self.assertTrue(all(r["fee"] == e.fee(r["venue"]) for r in rows))
                venues = {r["venue"] for r in rows}
                self.assertIn("Arcus", venues)
                # A venue without a verified fee is listed but cannot rank until
                # the user enters the fee from their own account.
                f.meta["RISEx"] = {"BTC": dict(f.meta["Arcus"]["BTC"], venue="RISEx")}
                (rise,) = [r for r in source.market_rows() if r["venue"] == "RISEx"]
                self.assertIsNone(rise["fee"])
                self.assertIn("ücret", source.excluded["RISEx"])
                self.assertNotIn(
                    "RISEx",
                    wide_paper.usable_rows([rise], rise["received"]).get("BTC", {}),
                )
                e.wide_paper.set_fees({"RISEx": 0.0004})
                (rise,) = [
                    r
                    for r in e.wide_paper.with_fees(source.market_rows())
                    if r["venue"] == "RISEx"
                ]
                self.assertEqual((rise["fee"], rise["fee_source"]), (0.0004, "user"))
                for r in rows:
                    m = f.meta[r["venue"]][r["asset"]]
                    # The venue's own schedule; an unknown one is never taken as 1h.
                    self.assertEqual(r["interval"], m.get("interval"))
                    if not r["interval"]:
                        continue
                    self.assertAlmostEqual(
                        r["rate"] * r["interval"] / 3600, r["raw_rate"]
                    )
                # Engine latency and slippage also price this desk: a change counts.
                changes = e.wide_paper.period_report()["changes"]
                e.update_config({"latency_ms": e.cfg["latency_ms"]})
                self.assertEqual(e.wide_paper.period_report()["changes"], changes)
                e.update_config({"latency_ms": e.cfg["latency_ms"] + 100})
                self.assertEqual(e.wide_paper.period_report()["changes"], changes + 1)
                snap = json.loads(json.dumps(e.snapshot()))
                self.assertIn("wide_paper", snap)
                self.assertEqual(snap["wide_paper"]["balance"]["cash"], 10000)
            finally:
                e.store.db.close()


class UniverseRowsTests(unittest.TestCase):
    def entry(self, **kw):
        x = dict(
            symbol="IRYS/USDT:USDT",
            rate=0.0001,
            raw_rate=0.0004,
            interval=14400,
            next=T0 + 600,
            received=T0,
            fee=0.0005,
            mark=1.0,
            leverage=20,
            volume=5e6,
            oi=2e6,
            contract_size=1.0,
        )
        x.update(kw)
        return x

    def test_liquid_markets_of_any_asset_become_desk_rows(self):
        universe = {
            "Bybit": {"IRYS": self.entry(), "DUST": self.entry(oi=5e4, volume=1e7)},
            "Aster": {
                "IRYS": self.entry(
                    oi=None, volume=3e6, rate=-0.0001, raw_rate=-0.0001, interval=3600
                ),
                "BTC": self.entry(),
            },
            "Lighter": {"IRYS": self.entry()},
        }
        rows = [dict(venue="Aster", asset="BTC", kind="perp")]
        out = wide_paper.universe_rows(universe, rows)
        keys = sorted((r["venue"], r["asset"]) for r in out)
        # OI decides when published; volume only where OI is unknown. Native venues
        # and markets already in the selected scan are not duplicated.
        self.assertEqual(keys, [("Aster", "IRYS"), ("Bybit", "IRYS")])
        r = next(r for r in out if r["venue"] == "Bybit")
        self.assertEqual(
            (r["raw_rate"], r["interval"], r["price_type"]), (0.0004, 14400, "mark")
        )
        self.assertEqual(len(wide_paper.rank(out, wide_paper.WIDE_DEFAULTS, T0)), 1)

    def test_unknown_schedule_or_contract_size_is_left_out(self):
        rows = [
            dict(self.entry(), venue="A", asset="X", kind="perp", contract_size=None),
            dict(self.entry(), venue="B", asset="X", kind="perp", interval=None),
            dict(self.entry(), venue="C", asset="X", kind="perp"),
        ]
        why = {}
        groups = wide_paper.usable_rows(rows, T0, why)
        self.assertEqual(list(groups["X"]), ["C"])
        self.assertEqual(
            why, {"Kontrat büyüklüğü bilinmiyor": 1, "Funding aralığı yok": 1}
        )

    def test_native_row_without_a_schedule_is_not_made_hourly(self):
        class Feed:
            meta = {
                "RISEx": {
                    "BTC": dict(mark=100.0, rate=0.0001, received=T0, max_leverage=10)
                }
            }

        class Wide:
            def snapshot(self):
                return [], {}

            def universe_snapshot(self):
                return {}

        e = type("E", (), {})()
        e.feed, e.wide = Feed(), Wide()
        e.fee = lambda v: 0.0004
        (r,) = wide_paper.EngineSource(e).market_rows()
        self.assertIsNone(r["interval"])
        self.assertIsNone(r["raw_rate"])
        why = {}
        self.assertEqual(wide_paper.usable_rows([r], T0, why), {})
        self.assertEqual(why, {"Funding aralığı yok": 1})

    def test_held_asset_stays_even_when_thin(self):
        universe = {"Bybit": {"DUST": self.entry(oi=5e4)}}
        self.assertEqual(wide_paper.universe_rows(universe, []), [])
        self.assertEqual(len(wide_paper.universe_rows(universe, [], {"DUST"})), 1)


class RfqSplitTests(unittest.TestCase):
    """RFQ markets leave candidate selection before any deep check."""

    setUp = WidePaperTests.setUp
    tearDown = WidePaperTests.tearDown
    fund_history = WidePaperTests.fund_history

    def setup_btw(self):
        # Extended (RFQ) offers the widest carry; Aster–Binance is the executable pair.
        self.src.rows = [
            row("Extended", rate=-0.0005, fee=0.00025, rfq=True),
            row("Aster", rate=-0.0001, fee=0.00035),
            row("Binance", rate=0.0008, interval=28800, fee=0.0005),
        ]
        for v in ("Aster", "Binance"):
            self.src.books[(v, "BTC")] = book(100.0, T0)
            self.src.history[(v, "BTC")] = candles(T0)
        self.fund_history("Aster", -0.0001, 3600)
        self.fund_history("Binance", 0.0008, 28800)
        seen = []
        real = self.src.book

        def tracked(v, a):
            seen.append(v)
            if v == "Extended":
                raise AssertionError("RFQ gösterge defteri okunmamalı")
            return real(v, a)

        self.src.book = tracked
        return seen

    def test_rfq_pair_goes_to_review_and_other_combination_opens(self):
        seen = self.setup_btw()
        self.desk.cfg["wide_deep_top"] = 1
        self.desk.cycle()
        self.assertNotIn("Extended", seen)
        self.assertTrue(
            all("Extended" not in (c["long"], c["short"]) for c in self.desk.candidates)
        )
        self.assertTrue(self.desk.rfq_review)
        for c in self.desk.rfq_review:
            self.assertIn("Extended", (c["long"], c["short"]))
            self.assertEqual(c["reasons"], [wide_paper.RFQ_UNSUPPORTED])
            self.assertFalse(c["eligible"])
        # The single deep-check slot went to the executable pair, which opened.
        self.assertEqual(
            [
                (x["long"], x["short"])
                for x in self.desk.assessed
                if x["strategy"] == "funding"
            ],
            [("Aster", "Binance")],
        )
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        self.assertFalse(
            any(k[2] == "Extended" or k[3] == "Extended" for k in self.desk.rejected)
        )
        snap = json.loads(json.dumps(self.desk.snapshot()))
        self.assertEqual(snap["rfq_review_count"], len(self.desk.rfq_review))

    def test_split_keeps_normal_order(self):
        rows = [
            row("Extended", rate=-0.0005, rfq=True),
            row("Aster", rate=-0.0001),
            row("Binance", rate=0.0008),
        ]
        normal, rfq = wide_paper.split_rfq(rank(rows, wide_paper.WIDE_DEFAULTS, T0))
        self.assertEqual(
            [(c["long"], c["short"]) for c in normal], [("Aster", "Binance")]
        )
        self.assertEqual(len(rfq), 2)


if __name__ == "__main__":
    unittest.main()
