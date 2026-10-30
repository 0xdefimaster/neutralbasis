import glob, json, os, sqlite3, tempfile, unittest
from unittest import mock
import bot, migrations, wide_paper
from ledger import Ledger
import test_wide_paper as twp
from test_wide_paper import book, FUNDING_WAIT, T0


def tables(db):
    return {
        r[0] for r in db.execute("select name from sqlite_master where type='table'")
    }


def old_records(store):
    """A database as the desk left it before migration 1."""
    trade = dict(
        id="w1",
        asset="BTC",
        strategy="funding",
        legs=[dict(side="long", venue="A"), dict(side="short", venue="B")],
        opened=100.0,
        closed_at=200.0,
        price_pnl=-1.0,
        funding=3.0,
        fees=0.5,
        net=1.5,
        close_reason="test",
        decision=dict(size=500.0),
    )
    store.put("wide_closed", [trade])
    store.put("wide_ledger", {"w0": dict(asset="ETH", net=2.0, fees=0.1)})
    store.put(
        "wide_epochs",
        [
            dict(
                started=1.0,
                ended=50.0,
                capital=1000.0,
                venues=None,
                trades=1,
                net=-4.0,
                closed=[
                    dict(
                        trade,
                        id="w-old",
                        net=-4.0,
                        price_pnl=-4.0,
                        funding=0.0,
                        fees=0.0,
                    )
                ],
            )
        ],
    )
    store.put("wide_epoch_start", 60.0)
    store.put("wide_wallet", {"cash": 1001.5})
    for i, cash in enumerate((1.0, 2.0)):
        store.db.execute(
            "insert into payments values(?,?,?)",
            (
                "w1:A:%d" % i,
                150.0 + i,
                json.dumps(dict(position="w1", venue="A", cash=cash)),
            ),
        )
    store.db.commit()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "p.sqlite3")

    def tearDown(self):
        self.dir.cleanup()

    def backups(self):
        return glob.glob(self.path + ".backup-*")

    def test_a_fresh_database_is_migrated_without_a_backup(self):
        s = bot.Store(self.path)
        self.assertEqual(migrations.current(s.db), 1)
        self.assertTrue(
            {"paper_periods", "paper_trades", "ledger_events"} <= tables(s.db)
        )
        self.assertEqual(self.backups(), [])
        s.db.close()

    def test_old_records_are_backed_up_mirrored_and_left_unchanged(self):
        s = bot.Store(self.path, migrate=False)
        old_records(s)
        before = s.db.execute("select k, v from kv order by k").fetchall()
        s.db.close()
        s = bot.Store(self.path)
        try:
            # The JSON records are untouched.
            self.assertEqual(
                s.db.execute("select k, v from kv order by k").fetchall(), before
            )
            (bak,) = self.backups()
            b = sqlite3.connect(bak)
            self.assertEqual(
                b.execute("select k, v from kv order by k").fetchall(), before
            )
            self.assertEqual(
                b.execute("select count(*) from payments").fetchone()[0], 2
            )
            b.close()
            periods = s.db.execute(
                "select source, ended, capital from paper_periods order by id"
            ).fetchall()
            self.assertEqual(
                periods, [("arşiv", 50.0, 1000.0), ("açık dönem", None, None)]
            )
            trades = dict(
                s.db.execute("select id, period_id from paper_trades").fetchall()
            )
            self.assertEqual(trades, {"w-old": 1, "w1": 2, "w0": 2})
            # w1: price -1, fees -0.5, payments +1 +2 = net 1.5.
            self.assertEqual(Ledger(s).reconcile(), [])
            total = s.db.execute(
                "select sum(amount) from ledger_events where position='w1'"
            ).fetchone()[0]
            self.assertAlmostEqual(total, 1.5)
        finally:
            s.db.close()

    def test_a_failing_migration_leaves_the_database_as_it_was(self):
        s = bot.Store(self.path, migrate=False)
        old_records(s)

        def broken(db):
            db.execute("create table half(x)")
            raise RuntimeError("yarıda kaldı")

        with self.assertRaises(RuntimeError):
            migrations.migrate(s, self.path, [(1, "broken", broken, None)])
        self.assertNotIn("half", tables(s.db))
        self.assertEqual(migrations.current(s.db), 0)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(
            s.get("wide_ledger", None), {"w0": dict(asset="ETH", net=2.0, fees=0.1)}
        )
        s.db.close()

    def test_rollback_and_re_apply(self):
        s = bot.Store(self.path, migrate=False)
        old_records(s)
        migrations.migrate(s, self.path)
        migrations.rollback(s, 0)
        self.assertEqual(migrations.current(s.db), 0)
        self.assertFalse(
            {"paper_periods", "paper_trades", "ledger_events"} & tables(s.db)
        )
        self.assertEqual(len(s.get("wide_closed", [])), 1)
        migrations.migrate(s, self.path)
        self.assertEqual(migrations.current(s.db), 1)
        self.assertEqual(
            s.db.execute("select count(*) from paper_trades").fetchone()[0], 3
        )
        self.assertEqual(len(self.backups()), 2)
        s.db.close()


class LedgerTests(unittest.TestCase):
    # The paper fixture, borrowed without re-running its tests here.
    tearDown = twp.WidePaperTests.tearDown
    setup_pair = twp.WidePaperTests.setup_pair
    fund_history = twp.WidePaperTests.fund_history

    def setUp(self):
        twp.WidePaperTests.setUp(self)

    def events(self, position):
        return self.store.db.execute(
            "select event_key, amount from ledger_events where position=? order by id",
            (position,),
        ).fetchall()

    def test_a_closed_trade_reconciles_with_its_payments(self):
        self.setup_pair()
        self.desk.cycle()
        p = self.desk.positions[0]
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
        self.desk.close(p, "Kullanıcı paper kapatma talebi")
        keys = [k for k, _ in self.events(p["id"])]
        self.assertEqual(len([k for k in keys if k.startswith("payment:")]), 9)
        self.assertIn("trade:%s:price" % p["id"], keys)
        total = sum(a for _, a in self.events(p["id"]))
        self.assertAlmostEqual(total, p["net"], places=9)
        self.assertEqual(self.desk.ledger.reconcile(), [])
        # Writing again never doubles a cash flow.
        self.desk.ledger.record_trade(p)
        self.desk.record_close(p)
        self.assertAlmostEqual(
            sum(a for _, a in self.events(p["id"])), p["net"], places=9
        )
        check = {c["label"]: c for c in self.desk.period_report()["checks"]}
        self.assertTrue(check["Muhasebe defteri işlemlerle tutarlı"]["ok"])

    def test_an_unverified_zero_and_its_late_rate_are_two_events(self):
        self.setup_pair()
        self.desk.cycle()
        p = self.desk.positions[0]
        self.clock = T0 + 8 * 3600 + FUNDING_WAIT + 60
        self.desk.cache.clear()
        for v in ("Hyperliquid", "Binance"):
            self.src.books[(v, "BTC")] = book(100.0, self.clock)
        self.src.rows = [dict(r, received=self.clock) for r in self.src.rows]
        self.desk.cycle(entries=False)
        zeros = [k for k, a in self.events(p["id"]) if a == 0 and "Binance" in k]
        self.assertTrue(zeros)
        self.store.mark("Binance", "BTC", T0 + 8 * 3600 - 10, 100.0)
        self.src.rates[("Binance", "BTC")].append(
            dict(ts=T0 + 8 * 3600, rate=0.0008, value=None, audit_id=None)
        )
        self.desk.cache.clear()
        self.desk.cycle(entries=False)
        late = [(k, a) for k, a in self.events(p["id"]) if k.endswith(":late")]
        self.assertEqual(len(late), 1)
        self.assertGreater(late[0][1], 0)
        paid = sum(a for k, a in self.events(p["id"]) if k.startswith("payment:"))
        self.assertAlmostEqual(paid, p["funding"], places=9)

    def test_periods_follow_resets_and_workspaces(self):
        pid = self.desk.ledger.current_period()
        ws = self.desk.start_workspace(
            {"Hyperliquid": 1000.0, "Paradex": 1000.0},
            dict(funding=True),
        )
        rows = self.store.db.execute(
            "select id, ended, source, workspace from paper_periods order by id"
        ).fetchall()
        old, new = rows[-2], rows[-1]
        self.assertEqual(old[0], pid)
        self.assertIsNotNone(old[1])
        self.assertEqual(old[2], "arşiv")
        self.assertEqual((new[1], new[2]), (None, "açık dönem"))
        self.assertEqual(json.loads(new[3])["venues"], ws["venues"])


if __name__ == "__main__":
    unittest.main()
