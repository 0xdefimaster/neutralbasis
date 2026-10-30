"""Stage 1 (settlement identity tolerance, AMBIGUOUS) and Stage 2 (historical
reconciliation). MOCK responses only (tests/fakes.py); never market evidence."""

import os
import tempfile
import unittest

from tools.phase1_recorder.clients import make_client
from tools.phase1_recorder.clients.records import (
    counted,
    dedupe_settlements,
    same_event_tolerance,
)
from tools.phase1_recorder.config import load
from tools.phase1_recorder.recorder import Recorder
from tools.phase1_recorder.storage import Store
from tools.phase1_recorder.validation import consistency, coverage

from .fakes import T0, Clock, FakeVenues

H = 3600


class ToleranceTests(unittest.TestCase):
    def test_effective_tolerance_is_the_smallest_limit(self):
        self.assertEqual(same_event_tolerance(60, 3600), 36)  # 1% of an hour
        self.assertEqual(same_event_tolerance(60, 8 * 3600), 60)  # the 60 s cap
        self.assertEqual(same_event_tolerance(10, 3600), 10)  # the configured value
        self.assertEqual(same_event_tolerance(120, 8 * 3600), 60)
        self.assertEqual(same_event_tolerance(60, None), 1)  # no interval: an instant
        self.assertEqual(same_event_tolerance(None, 3600), 36)

    def test_per_market_then_per_venue_configuration(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = load(
                root=os.path.join(d, "e"), assets=["BTC", "ETH"], venues=["Extended"]
            )
            cfg["same_event_seconds"] = {"Extended": 20, "Extended|ETH": 5}
            clock = Clock()
            rec = Recorder(
                cfg, transport=FakeVenues(clock), clock=clock, sleep=clock.sleep
            )
            try:
                v = rec.venues[0]
                self.assertEqual(
                    v.tolerance(dict(asset="BTC", market_id="BTC-USD")), 20
                )
                self.assertEqual(v.tolerance(dict(asset="ETH", market_id="ETH-USD")), 5)
                cfg["same_event_seconds"] = {"Extended": 600}
                # Capped by 1% of the declared hourly interval.
                self.assertEqual(
                    v.tolerance(dict(asset="BTC", market_id="BTC-USD")), 36
                )
            finally:
                rec.store.close()


class IdentityFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = load(root=os.path.join(self.dir.name, "e"), assets=["BTC"])
        self.store = Store(self.cfg["root"])
        self.hl = make_client("Hyperliquid", self.store, self.cfg)
        self.tol = same_event_tolerance(60, 3600)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def rows(self, *entries, coin="BTC", client=None):
        rows, _ = (client or self.hl).parse_settlements(
            [dict(coin=coin, fundingRate=r, time=int(t * 1000)) for t, r in entries],
            dict(asset=coin, market_id=coin),
            T0,
            "sha",
        )
        return rows

    def put(self, *entries, coin="BTC"):
        return [
            self.store.put_settlement(r, T0, self.tol)
            for r in self.rows(*entries, coin=coin)
        ]

    def latest(self, venue="Hyperliquid"):
        return self.store.latest("settlement", venue)


class IdentityBoundaryTests(IdentityFixture):
    def test_same_values_inside_and_outside_the_tolerance(self):
        self.assertEqual(self.put((T0, "0.0001")), ["new"])
        self.assertEqual(self.put((T0 + 35, "0.0001")), ["same"])  # inside 36 s
        self.assertEqual(self.put((T0 + 37, "0.0001")), ["new"])  # outside: another
        self.assertEqual(len(self.latest()), 2)

    def test_exact_tolerance_edge(self):
        self.put((T0, "0.0001"))
        self.assertEqual(self.put((T0 + 36, "0.0001")), ["same"])  # inclusive edge

    def test_a_different_value_at_the_same_instant_is_a_correction(self):
        self.put((T0, "0.0001"))
        self.assertEqual(self.put((T0 + 0.4, "0.0002")), ["correction"])
        self.assertEqual(self.put((T0 + 1.4, "0.0003")), ["correction"])
        (r,) = self.latest()
        self.assertEqual((r["revision"], r["identity_status"]), (3, "UNIQUE"))

    def test_a_different_payment_seconds_away_is_ambiguous_and_not_counted(self):
        self.put((T0, "0.0001"))
        self.assertEqual(self.put((T0 + 30, "0.0003")), ["ambiguous"])
        rows = self.latest()
        self.assertEqual(
            sorted(r["identity_status"] for r in rows), ["AMBIGUOUS", "AMBIGUOUS"]
        )
        self.assertEqual(counted(rows), [])
        # Both versions of the first payment are kept.
        firsts = [
            r
            for r in self.store.records("settlement", "Hyperliquid")
            if r["settled_at"] == T0
        ]
        self.assertEqual(sorted(r["revision"] for r in firsts), [1, 2])
        # Seeing either again changes nothing.
        self.assertEqual(
            self.put((T0, "0.0001"), (T0 + 30, "0.0003")), ["same", "same"]
        )
        self.assertEqual(len(self.store.records("settlement", "Hyperliquid")), 3)

    def test_rounding_at_half_a_second(self):
        self.put((T0 + 0.5, "0.0001"))
        self.assertEqual(self.put((T0 + 0.49, "0.0001")), ["same"])
        self.assertEqual(len(self.latest()), 1)

    def test_other_markets_and_venues_never_merge(self):
        self.assertEqual(self.put((T0, "0.0001")), ["new"])
        self.assertEqual(self.put((T0, "0.0009"), coin="ETH"), ["new"])
        lt = make_client("Lighter", self.store, self.cfg)
        rows, _ = lt.parse_settlements(
            {
                "code": 200,
                "fundings": [{"timestamp": T0, "rate": "0.01", "direction": "long"}],
            },
            dict(asset="BTC", market_id=1),
            T0,
            "s",
        )
        self.assertEqual([self.store.put_settlement(r, T0, 60) for r in rows], ["new"])
        core = make_client("LighterCore", self.store, self.cfg)
        rows, _ = core.parse_settlements(
            {
                "code": 200,
                "fundings": [
                    {"timestamp": T0 + 5, "rate": "0.05", "direction": "short"}
                ],
            },
            dict(asset="BTC", market_id=1),
            T0,
            "s",
        )
        self.assertEqual([self.store.put_settlement(r, T0, 60) for r in rows], ["new"])
        self.assertEqual(len(self.latest()), 2)
        self.assertEqual(len(self.latest("Lighter")), 1)
        self.assertEqual(len(self.latest("LighterCore")), 1)
        self.assertTrue(
            all(
                r["identity_status"] == "UNIQUE"
                for r in self.store.latest("settlement")
            )
        )

    def test_ambiguity_inside_one_response(self):
        rows = self.rows((T0, "0.0001"), (T0 + 20, "0.0005"), (T0 + 3600, "0.0001"))
        kept, stats = dedupe_settlements(rows, self.tol)
        self.assertEqual(stats["ambiguous"], 1)
        self.assertEqual(
            [r["identity_status"] for r in kept], ["AMBIGUOUS", "AMBIGUOUS", "UNIQUE"]
        )
        out = [self.store.put_settlement(r, T0, self.tol) for r in kept]
        self.assertEqual(out, ["new", "ambiguous", "new"])
        self.assertEqual(len(counted(self.latest())), 1)

    def test_identity_survives_an_index_rebuild(self):
        self.put((T0, "0.0001"))
        self.put((T0 + 30, "0.0003"))
        self.store.db.execute("delete from settlement_index")
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.cfg["root"])
        rows = self.store.db.execute(
            "select settled_at, identity_status from settlement_index order by settled_at"
        ).fetchall()
        self.assertEqual(rows, [(T0, "AMBIGUOUS"), (T0 + 30, "AMBIGUOUS")])


class RecorderFixture(unittest.TestCase):
    venues = ["Hyperliquid", "Lighter", "Extended"]

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.fake = FakeVenues(self.clock, assets=("BTC",))
        self.cfg = load(
            root=os.path.join(self.dir.name, "e"), assets=["BTC"], venues=self.venues
        )
        self.cfg["backfill_days"] = 2
        self.rec = None

    def tearDown(self):
        if self.rec:
            self.rec.store.close()
        self.dir.cleanup()

    def start(self):
        if self.rec:
            self.rec.store.close()
        self.rec = Recorder(
            self.cfg, transport=self.fake, clock=self.clock, sleep=self.clock.sleep
        )
        self.by = {v.venue: v for v in self.rec.venues}
        for v in self.by.values():
            v.run_task("metadata")
        return self.rec

    def watch(self, minutes, step=10):
        for _ in range(minutes // step):
            self.clock.t += step * 60
            for v in self.by.values():
                v.run_task("funding")
        for v in self.by.values():
            v.run_task("settlements")

    def reconcile(self):
        for v in self.by.values():
            self.assertTrue(v.run_task("reconcile"), v.status["tasks"].get("reconcile"))

    def lines(self, kind="settlement"):
        return len(self.rec.store.records(kind))

    def payment_count(self, venue):
        return len(counted(self.rec.store.latest("settlement", venue)))

    def now_hour(self):
        return int(self.clock()) // H * H


class AmbiguousComparisonTests(RecorderFixture):
    venues = ["Hyperliquid"]

    def test_an_ambiguous_payment_excludes_its_comparison(self):
        self.start()
        self.watch(4 * 60)
        store = self.rec.store
        before = coverage(store, self.cfg, self.clock())["venues"]["Hyperliquid"][
            "markets"
        ]["BTC"]
        self.assertEqual(before["comparisons"], 4)
        last = self.now_hour()
        rows, _ = self.by["Hyperliquid"].client.parse_settlements(
            [dict(coin="BTC", fundingRate="0.0009", time=(last + 20) * 1000)],
            dict(asset="BTC", market_id="BTC"),
            self.clock(),
            "sha-x",
        )
        self.assertEqual(store.put_settlement(rows[0], self.clock(), 36), "ambiguous")
        m = coverage(store, self.cfg, self.clock())["venues"]["Hyperliquid"]["markets"][
            "BTC"
        ]
        self.assertEqual(m["comparisons"], 3)
        self.assertEqual(m["excluded_comparisons"], 1)
        self.assertEqual(m["ambiguous_payments"], 2)
        # Matching again never brings the comparison back.
        for _ in range(3):
            self.by["Hyperliquid"].run_task("settlements")
        m = coverage(store, self.cfg, self.clock())["venues"]["Hyperliquid"]["markets"][
            "BTC"
        ]
        self.assertEqual(m["comparisons"], 3)
        self.assertTrue(
            consistency(store, self.cfg)["comparison_index"]["problems"] == []
        )


class ReconcileTests(RecorderFixture):
    def test_older_corrections_inside_the_window_are_caught(self):
        self.start()
        self.watch(60)
        self.clock.t += 30 * H
        self.watch(60)
        now = self.now_hour()
        counts = {v: self.payment_count(v) for v in self.venues}
        # The venue revises payments 5 h, 23 h and 25 h old.
        for age in (5, 23, 25):
            self.fake.revised[now - age * H] = 0.0007
        self.reconcile()
        for v in self.venues:
            fixed = {
                c["settled_at"]
                for c in self.rec.store.records("settlement_correction", v)
            }
            self.assertEqual(fixed, {now - 5 * H, now - 23 * H}, v)
            self.assertEqual(self.payment_count(v), counts[v])  # never more payments
        # A wider window (capped by backfill_days) reaches the 25-hour one.
        self.cfg["reconcile_hours"] = 48
        self.reconcile()
        for v in self.venues:
            fixed = {
                c["settled_at"]
                for c in self.rec.store.records("settlement_correction", v)
            }
            self.assertIn(now - 25 * H, fixed)
            self.assertEqual(self.payment_count(v), counts[v])

    def test_reconciliation_is_idempotent_and_keeps_every_version(self):
        self.start()
        self.watch(3 * 60)
        self.fake.revised[self.now_hour() - 2 * H] = 0.0007
        self.reconcile()
        lines = {
            k: self.lines(k)
            for k in ("settlement", "settlement_correction", "comparison")
        }
        for _ in range(3):
            self.reconcile()
        self.assertEqual({k: self.lines(k) for k in lines}, lines)
        for v in self.venues:
            versions = [
                r["revision"]
                for r in self.rec.store.records("settlement", v)
                if r["settled_at"] == self.now_hour() - 2 * H
            ]
            self.assertEqual(sorted(versions), [1, 2])
            st = self.rec.store.checkpoint(
                "reconcile:%s:%s" % (v, self.by[v].selected[0]["market_id"])
            )
            self.assertEqual(st["window_hours"], 24)
            self.assertEqual(
                st.get("correction", 0), 0
            )  # the last run found nothing new
            self.assertGreater(st["same"], 0)

    def test_withdrawn_payments_are_reported_never_deleted(self):
        self.start()
        self.watch(6 * 60)
        gone = self.now_hour() - 3 * H
        counts = {v: self.payment_count(v) for v in self.venues}
        self.fake.withdrawn.add(gone)
        self.reconcile()
        self.reconcile()
        for v in self.venues:
            w = self.rec.store.records("settlement_withdrawn", v)
            self.assertEqual([x["settled_at"] for x in w], [gone], v)
            self.assertEqual(self.payment_count(v), counts[v])
        m = coverage(self.rec.store, self.cfg, self.clock())["venues"]["Extended"][
            "markets"
        ]["BTC"]
        self.assertEqual(m["withdrawn_payments"], 1)

    def test_a_corrected_old_payment_gets_its_comparison_recomputed(self):
        self.start()
        self.watch(6 * 60)
        target = self.now_hour() - 4 * H
        before = {
            v: next(
                c for c in self.rec.store.comparisons(v) if c["settled_at"] == target
            )
            for v in self.venues
        }
        self.assertTrue(all(c["settlement_revision"] == 1 for c in before.values()))
        total = {v: len(self.rec.store.comparisons(v)) for v in self.venues}
        self.fake.revised[target] = 0.0002
        self.reconcile()
        for v in self.venues:
            after = next(
                c for c in self.rec.store.comparisons(v) if c["settled_at"] == target
            )
            self.assertEqual(after["settlement_revision"], 2, v)
            self.assertEqual(len(self.rec.store.comparisons(v)), total[v])

    def test_checkpoints_and_statistics_survive_a_restart(self):
        self.start()
        self.watch(2 * 60)
        self.reconcile()
        cp = self.rec.store.checkpoint("reconcile:Extended:BTC-USD")
        stats = self.rec.store.checkpoint("stats:Extended:BTC-USD")
        self.start()  # a new recorder on the same folder
        self.assertEqual(self.rec.store.checkpoint("reconcile:Extended:BTC-USD"), cp)
        self.assertEqual(self.rec.store.checkpoint("stats:Extended:BTC-USD"), stats)
        self.reconcile()
        self.assertGreater(
            self.rec.store.checkpoint("stats:Extended:BTC-USD")["reconcile_checked"],
            stats["reconcile_checked"],
        )

    def test_the_window_is_capped_by_backfill_days(self):
        self.cfg["reconcile_hours"] = 500
        self.cfg["backfill_days"] = 1
        self.start()
        self.watch(60)
        self.reconcile()
        st = self.rec.store.checkpoint("reconcile:Extended:BTC-USD")
        self.assertEqual(st["window_hours"], 24)


if __name__ == "__main__":
    unittest.main()
