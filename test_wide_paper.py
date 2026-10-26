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
    closing_times,
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


class FakeSource:
    def __init__(self):
        self.rows = []
        self.books = {}
        self.history = {}
        self.rates = {}

    def market_rows(self):
        return [dict(r) for r in self.rows]

    def book(self, v, a):
        b = self.books[(v, a)]
        return dict(b, received=bot.now())

    def prices(self, v, a):
        return self.history[(v, a)]

    def funding(self, v, a, start):
        return [r for r in self.rates.get((v, a), []) if r["ts"] >= start]


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

    def fund_history(self, venue, per_payment, interval):
        self.src.rates[(venue, "BTC")] = [
            dict(ts=t, rate=per_payment, value=None, audit_id=None)
            for t in range(T0 - 3 * 86400 + interval, T0 + 1, interval)
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
    def setup_convergence(self, basis=0.0, amplitude=0.0035, gap=0.006, drift=0.0):
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
                    + amplitude * math.sin(2 * math.pi * i / 12)
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
        self.assertEqual(
            closing_times(
                [(0, 0.0), (3600, 0.003), (7200, 0.001), (10800, 0.0)], 0.002, 0.0002
            ),
            [2.0],
        )

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
                "Beklenen yakınsama kazancı alındı",
            ),
        )
        self.assertGreater(self.desk.closed[-1]["net"], 0)

    def test_persistent_basis_never_qualifies(self):
        # Dear venue always 30bp rich and barely moves: today's 60bp has no history
        # of excursions that came back.
        self.setup_convergence(basis=0.003, amplitude=0.0003)
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        reasons = " ".join(self.desk.assessed[0]["reasons"])
        self.assertRegex(reasons, "kesişim yok|kapanışı yetersiz")

    def test_constant_basis_reverts_to_its_normal_gap(self):
        # 0.99 vs 0.98 style: the dear venue is normally 30bp rich and swings around
        # that; today it is 90bp rich, so the trade targets ~30bp, not zero.
        self.setup_convergence(basis=0.003, gap=0.009)
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

    def test_convergence_charges_funding_paid_while_waiting(self):
        self.setup_convergence()
        # Long venue charges heavy hourly funding during the expected hold.
        self.src.rows[0] = row("Hyperliquid", rate=0.002, fee=0.0002, mark=100.0)
        self.desk.cycle()
        c = self.desk.assessed[0]
        self.assertLess(c["conservative_funding"], 0)
        self.assertEqual(self.desk.positions, [])

    # --- funding profit lock -------------------------------------------------------
    def test_profit_lock_closes_positive_after_pullback(self):
        self.setup_pair()
        self.desk.cycle()
        p = self.desk.positions[0]
        p["peak_net"] = 5.0
        p["decision"]["net"] = 100.0  # isolate the lock from the profit target
        p["funding"] += 3.0
        self.desk.wallet["cash"] += 3.0
        self.desk.cycle(entries=False)
        self.assertEqual(self.desk.positions, [])
        closed = self.desk.closed[-1]
        self.assertEqual(
            closed["close_reason"], "Kâr kilitlendi: kazanç geri verilmeye başladı"
        )
        self.assertGreater(closed["net"], 0)

    def test_no_lock_before_any_gain(self):
        self.setup_pair()
        self.desk.cycle()
        self.desk.cycle(entries=False)
        self.assertEqual(len(self.desk.positions), 1)

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

    def test_leverage_settings_capped_at_five(self):
        for key in ("wide_leverage", "wide_conv_leverage"):
            with self.assertRaises(ValueError):
                self.desk.update_config({key: 6})
        self.desk.update_config({"wide_leverage": 5, "wide_conv_leverage": 5})


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
                rows = EngineSource(e).market_rows()
                self.assertTrue(rows)
                self.assertTrue(
                    all(r["venue"] in ("Lighter", "Extended") for r in rows)
                )
                self.assertTrue(all(r["fee"] == e.fee(r["venue"]) for r in rows))
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

    def test_held_asset_stays_even_when_thin(self):
        universe = {"Bybit": {"DUST": self.entry(oi=5e4)}}
        self.assertEqual(wide_paper.universe_rows(universe, []), [])
        self.assertEqual(len(wide_paper.universe_rows(universe, [], {"DUST"})), 1)


if __name__ == "__main__":
    unittest.main()
