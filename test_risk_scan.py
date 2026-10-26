import copy, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import bot, risk_scan
from test_bot import FixtureFeed


class FiveFixture(FixtureFeed):
    def __init__(self):
        super().__init__()
        self.arcus_fee = 0.00045

    def markets(self):
        super().markets()
        self.meta["Arcus"] = {"BTC": {**self.meta["Extended"]["BTC"], "venue": "Arcus"}}
        return self.meta

    def candles(self, v, a):
        return {
            **super().candles(v, a),
            "price_type": "oracle" if v == "Arcus" else "trade",
        }


class RiskTests(unittest.TestCase):
    def test_only_requested_five_venues(self):
        self.assertEqual(
            set(risk_scan.VENUES), {"TxFlow", "RISEx", "Arcus", "Extended", "Lighter"}
        )
        self.assertNotIn("Variational", risk_scan.CATALOG)
        self.assertIn("api.rh.lighter.xyz", risk_scan.CATALOG["Lighter"]["url"])

    def test_robust_epoch_precision(self):
        for v in (
            "1790330400000000000",
            "1790330400000000",
            "1790330400000",
            "1790330400",
        ):
            self.assertEqual(risk_scan.epoch(v), 1790330400)

    def meta(self):
        return {
            v: {
                a: dict(
                    rate=(i + 1) * 0.0001, mark=100 + i, next=1792930000, audit_id=i + 1
                )
                for a in ("BTC", "ETH", "SOL")
            }
            for i, v in enumerate(risk_scan.VENUES)
        }

    def test_top_five_distinct_ranked_pairs(self):
        chosen, n = risk_scan.shortlist(self.meta(), ["BTC", "ETH", "SOL"])
        self.assertEqual(n, 30)
        self.assertEqual(len(chosen), 5)
        self.assertEqual(len({(c["asset"], c["long"], c["short"]) for c in chosen}), 5)
        self.assertTrue(all(c["funding_gap"] > 0 for c in chosen))
        self.assertEqual([c["shortlist_rank"] for c in chosen], [1, 2, 3, 4, 5])

    def test_positive_funding_direction_is_preserved(self):
        m = self.meta()
        chosen, _ = risk_scan.shortlist(m, ["BTC"])
        self.assertTrue(
            all(
                m[c["short"]]["BTC"]["rate"] > m[c["long"]]["BTC"]["rate"]
                for c in chosen
            )
        )

    def test_adverse_price_gap_not_rewarded_by_absolute_value(self):
        m = {
            "TxFlow": {"BTC": dict(rate=0.0, mark=200, next=1, audit_id=1)},
            "RISEx": {"BTC": dict(rate=0.01, mark=100, next=1, audit_id=2)},
            "Extended": {"BTC": dict(rate=0.02, mark=201, next=1, audit_id=3)},
        }
        r, _ = risk_scan.shortlist(m, ["BTC"])
        adverse = next(c for c in r if c["long"] == "TxFlow" and c["short"] == "RISEx")
        favorable = next(
            c for c in r if c["long"] == "RISEx" and c["short"] == "Extended"
        )
        self.assertLess(favorable["spread_rank"], adverse["spread_rank"])

    def test_missing_rate_is_not_zero(self):
        m = self.meta()
        m["TxFlow"]["BTC"]["rate"] = None
        r, n = risk_scan.shortlist(m, ["BTC"])
        self.assertEqual(n, 6)
        self.assertFalse(any("TxFlow" in (c["long"], c["short"]) for c in r))

    def test_zero_advantage_produces_no_fake_five(self):
        m = self.meta()
        for ms in m.values():
            ms["BTC"]["rate"] = 0
        self.assertEqual(risk_scan.shortlist(m, ["BTC"]), ([], 0))

    def test_core_host_not_used_by_rh_feed(self):
        with tempfile.TemporaryDirectory() as d:
            s = bot.Store(Path(d) / "test.db")
            f = risk_scan.FiveFeed(s)
            self.assertEqual(bot.L, risk_scan.RH)
            s.db.close()

    def engine(self, path):
        f = FiveFixture()
        f.markets()
        e = risk_scan.RiskEngine(path, f)
        e.cfg["symbols"] = ["BTC"]
        return e, f

    def test_history_success_cannot_override_oracle_rejection(self):
        with tempfile.TemporaryDirectory() as d, patch.object(
            bot, "now", lambda: 1792921000
        ), patch.object(risk_scan, "now", lambda: 1792921000):
            e, f = self.engine(Path(d) / "test.db")
            c = dict(
                asset="BTC",
                long="Lighter",
                short="Arcus",
                shortlist_rank=1,
                selection_score=1,
                funding_rank=1,
                spread_rank=1,
                funding_gap=0.0005,
                price_spread=0,
                price_abs=0,
                rate_basis_long="test",
                rate_basis_short="test",
                next_long=1792922400,
                next_short=1792922400,
            )
            o = e.assess(c)
            self.assertTrue(o.get("analysis"))
            self.assertFalse(o["eligible"])
            self.assertTrue(any("Oracle" in r or "oracle" in r for r in o["reasons"]))
            self.assertEqual(len(o["history"]), 168)
            e.store.db.close()

    def test_five_separate_wallet_allocations(self):
        with tempfile.TemporaryDirectory() as d:
            e, f = self.engine(Path(d) / "test.db")
            self.assertEqual(set(e.wallet), set(risk_scan.VENUES))
            self.assertAlmostEqual(sum(e.wallet.values()), 1000)
            self.assertTrue(all(v == 200 for v in e.wallet.values()))
            e.store.db.close()

    def test_high_correlation_does_not_override_spread_loss(self):
        with tempfile.TemporaryDirectory() as d, patch.object(
            bot, "now", lambda: 1792921000
        ), patch.object(risk_scan, "now", lambda: 1792921000):
            e, f = self.engine(Path(d) / "test.db")
            real = risk_scan.analyze
            c = dict(
                asset="BTC",
                long="Lighter",
                short="Extended",
                shortlist_rank=1,
                selection_score=1,
                funding_rank=1,
                spread_rank=1,
                funding_gap=0.0005,
                price_spread=0,
                price_abs=0,
                rate_basis_long="test",
                rate_basis_short="test",
                next_long=1792922400,
                next_short=1792922400,
            )
            with patch.object(
                risk_scan,
                "analyze",
                side_effect=lambda *args: {**real(*args), "stress_fraction": 0.05},
            ):
                o = e.assess(c)
            self.assertGreater(o["analysis"]["corr7"], 0.99)
            self.assertFalse(o["eligible"])
            self.assertLess(o["reference_coverage"], 1)
            self.assertTrue(any("ücretler hariç" in r for r in o["reasons"]))
            e.store.db.close()

    def test_old_two_venue_wallet_cannot_mix_with_robinhood(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test.db"
            s = bot.Store(p)
            s.put("wallet", {"Lighter": 500, "Extended": 500})
            s.db.close()
            with self.assertRaisesRegex(ValueError, "ayrı veri klasörü"):
                risk_scan.RiskEngine(p, FiveFixture())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class OldEngineCloseTests(unittest.TestCase):
    def test_funding_outage_does_not_block_manual_close(self):
        with tempfile.TemporaryDirectory() as d:
            f = FiveFixture()
            f.markets()
            e = risk_scan.RiskEngine(Path(d) / "t.db", f)
            e.cfg["symbols"] = ["BTC"]
            try:
                legs = [
                    dict(
                        venue=v,
                        side=side,
                        qty=0.01,
                        entry=f.book(v, "BTC")["asks"][0][0],
                        entered=bot.now() - 60,
                        fee_rate=0.0003,
                        entry_fee=0.0,
                        margin=1.0,
                        closed=False,
                        price_pnl=0.0,
                        audit_id=None,
                    )
                    for v, side in (("Lighter", "long"), ("Extended", "short"))
                ]
                p = dict(
                    id="old1",
                    asset="BTC",
                    opened=bot.now() - 60,
                    legs=legs,
                    funding=0.0,
                    estimated_funding=0.0,
                    fees=0.0,
                    unrealized=0.0,
                    status="OPEN",
                    price_pnl=0.0,
                    close_reason=None,
                    decision=dict(
                        long="Lighter",
                        short="Extended",
                        qty=0.01,
                        execution_drag=0.0,
                        stress_loss=1e9,
                    ),
                )
                e.positions.append(p)

                def broken(*a, **k):
                    raise ValueError("funding API kapalı")

                f.funding_history = broken
                e.manual_close.add("old1")
                e.cfg["latency_ms"] = 0
                e.cycle()
                self.assertEqual(e.positions, [])
                self.assertEqual(
                    e.closed[-1]["close_reason"], "Kullanıcı paper kapatma talebi"
                )
            finally:
                e.store.db.close()
