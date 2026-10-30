import os, tempfile, threading, unittest
from unittest import mock
import bot, history_store, wide_paper
from wide_paper import WidePaper, reason_group
from history_store import HistoryKeeper
from test_wide_paper import FakeSource, T0


def cand(asset, score=1.0, strategy="funding"):
    return dict(asset=asset, long="A", short="B", score=score, strategy=strategy)


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = bot.Store(os.path.join(self.dir.name, "p.sqlite3"))
        self.clock = T0
        self.cands = [cand(a, 10 - i) for i, a in enumerate("ABCDEFG")]
        self.reasons = {}
        for p in (
            mock.patch.object(bot, "now", lambda: self.clock),
            mock.patch.object(wide_paper, "now", lambda: self.clock),
            mock.patch.object(wide_paper, "rank", lambda rows, cfg, at: self.cands),
            mock.patch.object(wide_paper, "rank_spreads", lambda rows, at: []),
            mock.patch.object(wide_paper.capabilities, "pair_blockers", lambda *a: []),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.desk = WidePaper(self.store, FakeSource(), sleep=lambda s: None)
        self.desk.cfg.update(wide_enabled=True, wide_deep_top=3)
        self.seen = []

        def assess(c):
            self.seen.append(c["asset"])
            reason = self.reasons.get(
                c["asset"],
                "Funding geliri ücret + kayma + spread riski payını karşılamıyor",
            )
            return dict(c, reasons=[reason], eligible=False, net=None)

        self.desk.assess = assess

    def tearDown(self):
        self.store.db.close()
        self.dir.cleanup()

    def test_rejected_pairs_give_way_to_the_next_ones(self):
        self.reasons["B"] = "30 günlük eşlenmiş 1h mum eksik"
        self.desk.cycle()
        # B fails on missing history and does not use up one of the three slots.
        self.assertEqual(self.seen, ["A", "B", "C", "D"])
        self.clock += 60
        self.desk.cycle()
        self.assertEqual(self.seen[4:], ["E", "F", "G"])
        r = self.desk.review_snapshot()
        self.assertEqual((r["rounds"], r["examined"], r["eligible"]), (2, 7, 0))
        self.assertEqual(r["reasons"]["Geçmiş eksik"], 1)
        self.assertEqual(
            r["reasons"][
                "Funding geliri ücret + kayma + spread riski payını karşılamıyor"
            ],
            6,
        )
        self.assertEqual(r["waiting"]["Geçmiş eksik"], 1)
        # History retry comes back sooner than an ordinary rejection.
        self.clock = T0 + wide_paper.HISTORY_RETRY + 1
        del self.reasons["B"]
        self.desk.cycle()
        self.assertEqual(self.seen[7:], ["B"])
        self.clock = T0 + wide_paper.REJECT_COOLDOWN + 1
        self.desk.cycle()
        self.assertEqual(self.seen[8:], ["A", "C", "D"])

    def test_slow_reasons_wait_longer_so_the_list_moves_on(self):
        # Seven days of funding against the direction cannot change in half an hour.
        self.reasons["A"] = (
            "7 günde gerçekleşen funding bu yönde avantaj göstermiyor · -$4.10"
        )
        self.desk.cycle()
        self.clock = T0 + wide_paper.REJECT_COOLDOWN + 1
        self.desk.cycle()
        # B, C and D come back after the usual wait; A waits for its slow reason.
        self.assertEqual(self.seen[3:], ["B", "C", "D"])
        (a,) = [x for x in self.desk.rejected.values() if x["asset"] == "A"]
        self.assertEqual(a["until"], T0 + 6 * 3600)

    def test_a_reason_naming_its_window_still_waits_longer(self):
        self.assertEqual(
            wide_paper.reason_wait(
                "Normal fiyat farkı kayıyor: 7g ve 12g medyanı uyuşmuyor"
            ),
            3 * 3600,
        )
        self.assertEqual(
            wide_paper.reason_wait("İşlem kayıp bütçesi aşılıyor"),
            wide_paper.REJECT_COOLDOWN,
        )

    def test_funnel_counts_every_step_and_how_far_the_list_was_checked(self):
        self.desk.cycle()
        f = self.desk.snapshot()["funnel"]["strategies"]["funding"]
        self.assertEqual((f["candidates"], f["positive"]), (7, 7))
        # A, B and C were checked this round; D is the first not yet reached.
        self.assertEqual((f["reached"], f["checked"], f["waiting"]), (3, 3, 3))
        self.assertEqual(
            f["round"],
            dict(examined=3, eligible=0, opened=0, skipped=0, rejected=3),
        )
        self.clock += 60
        self.desk.cycle()
        f = self.desk.funnel["strategies"]["funding"]
        self.assertEqual((f["reached"], f["checked"]), (6, 6))

    def test_history_needs_put_history_rejections_first(self):
        self.reasons["C"] = "B: 24 saat gerçekleşmiş funding geçmişi eksik"
        self.desk.cycle()
        needs = self.desk.history_needs()
        self.assertEqual(needs[:2], [("A", "C"), ("B", "C")])
        self.assertIn(("A", "G"), needs)

    def test_reason_groups(self):
        self.assertEqual(
            reason_group("Gerçek işlem mumu yok; oracle/eksik seri ile giriş yapılmaz"),
            "Geçmiş eksik",
        )
        self.assertEqual(
            reason_group("Bu genişlikte geçmiş makas kapanışı yetersiz (2)"),
            "Bu genişlikte geçmiş makas kapanışı yetersiz (2)",
        )


class EntryLoopTests(unittest.TestCase):
    """Several qualifying pairs in one round: every entry re-checks the desk first."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = bot.Store(os.path.join(self.dir.name, "e.sqlite3"))
        self.cands = [cand(a, 10 - i) for i, a in enumerate("ABC")]
        for p in (
            mock.patch.object(bot, "now", lambda: T0),
            mock.patch.object(wide_paper, "now", lambda: T0),
            mock.patch.object(wide_paper, "rank", lambda rows, cfg, at: self.cands),
            mock.patch.object(wide_paper, "rank_spreads", lambda rows, at: []),
            mock.patch.object(wide_paper.capabilities, "pair_blockers", lambda *a: []),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.desk = WidePaper(self.store, FakeSource(), sleep=lambda s: None)
        self.desk.cfg.update(wide_enabled=True)
        self.desk.assess = lambda c: dict(
            c,
            reasons=[],
            eligible=True,
            net=c["score"],
            size=1000.0,
            leverage=2.0,
            entry_fee=0.5,
            exit_fee=0.5,
            execution_drag=0.5,
            stress_loss=5.0,
        )
        self.desk.entry_funding_check = lambda c, *a: None
        # The entry loop is under test, not the market re-check before each entry.
        self.desk.entry_evidence = lambda c, *a: []
        self.opened = []
        self.status = "OPEN"

        def fake_open(c):
            self.opened.append(c["asset"])
            self.desk.positions.append(
                dict(id=c["asset"], asset=c["asset"], legs=[], status=self.status)
            )

        self.desk.open = fake_open
        # The fake positions above are not marked again within the round under test.
        self.desk.settle = lambda p: None

    def tearDown(self):
        self.store.db.close()
        self.dir.cleanup()

    def test_all_qualifying_pairs_open_best_first(self):
        self.desk.cycle()
        self.assertEqual(self.opened, ["A", "B", "C"])

    def test_first_entry_in_recovery_stops_the_round(self):
        self.status = "RECOVERY"
        self.desk.cycle()
        self.assertEqual(self.opened, ["A"])
        halts = self.store.db.execute(
            "select body from events where kind='WIDE_ENTRY_HALT'"
        ).fetchall()
        self.assertIn("RECOVERY", halts[0][0])

    def test_daily_loss_budget_is_checked_before_each_entry(self):
        # The second entry would fit the per-trade limit but not what is left of the day.
        self.desk.cfg.update(wide_daily_loss=10.0)

        def losing_open(c):
            self.opened.append(c["asset"])
            self.desk.wallet["cash"] -= 5

        self.desk.daily_pnl()
        self.desk.open = losing_open
        self.desk.cycle()
        self.assertEqual(self.opened, ["A"])


class PrioritySource:
    def __init__(self):
        self.list = [
            ("X", "BTC", "perp", 3600, "trade"),
            ("Y", "ETH", "perp", 3600, "trade"),
        ]
        self.calls = []

    def markets(self):
        return list(self.list)

    def candles(self, v, a, kind, start, end):
        self.calls.append(v)
        return {}

    def funding(self, v, a, start):
        return []

    def publish(self, *a):
        pass

    def priority(self):
        return [("Y", "ETH", "perp")]


class KeeperBatchTests(unittest.TestCase):
    def test_batch_limit_and_priority_status(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        store = bot.Store(os.path.join(d.name, "h.db"))
        self.addCleanup(store.db.close)
        with mock.patch.object(history_store, "now", lambda: T0), mock.patch.object(
            bot, "now", lambda: T0
        ):
            src = PrioritySource()
            k = HistoryKeeper(store, src, threading.Event(), sleep=lambda s: None)
            self.assertEqual(k.status()["priority_missing"], 1)
            self.assertEqual(k.run_once(1), 1)
            self.assertEqual(src.calls, ["X"])
            k.run_once(1)
            st = k.status()
        # Synced without error but the source returned nothing: still missing.
        self.assertEqual((st["priority"], st["priority_missing"]), (1, 1))
        self.assertEqual((st["synced"], st["complete"]), (2, 0))


if __name__ == "__main__":
    unittest.main()
