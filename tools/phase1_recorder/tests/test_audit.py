"""Data reliability audit of PR #54: settlement identity, corrections, conflicts,
missing data, crash safety, interrupted repairs and mock/live separation.
MOCK responses only (tests/fakes.py); never reported as market evidence."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from tools.phase1_recorder import matching
from tools.phase1_recorder.clients import make_client
from tools.phase1_recorder.clients.base import SchemaError, num
from tools.phase1_recorder.config import load
from tools.phase1_recorder.export import export
from tools.phase1_recorder.recorder import LIVE, Recorder, VenueRecorder
from tools.phase1_recorder.storage import Store
from tools.phase1_recorder.validation import consistency, coverage, text_report

from .fakes import T0, Clock, FakeVenues

HL = dict(asset="BTC", market_id="BTC")
LT = dict(asset="BTC", market_id=1)
EX = dict(asset="BTC", market_id="BTC-USD")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = load(
            root=os.path.join(self.dir.name, "phase1-evidence"), assets=["BTC"]
        )
        self.store = Store(self.cfg["root"])
        self.hl = make_client("Hyperliquid", self.store, self.cfg)
        self.lt = make_client("Lighter", self.store, self.cfg)
        self.ex = make_client("Extended", self.store, self.cfg)

    def tearDown(self):
        if self.store is not None:
            self.store.close()
        self.dir.cleanup()

    def hl_rows(self, *entries):
        rows, _ = self.hl.parse_settlements(
            [dict(coin="BTC", fundingRate=r, time=t) for t, r in entries],
            HL,
            T0,
            "sha-hl",
        )
        return rows

    def settle(self, rows):
        return [self.store.put_settlement(r, T0) for r in rows]

    def latest(self, venue="Hyperliquid"):
        return self.store.latest("settlement", venue)


class SettlementIdentityTests(Fixture):
    def test_every_venue_stores_whole_utc_seconds_and_the_api_time(self):
        hl = self.hl_rows((T0 * 1000 + 76, "0.0001"))[0]
        lt, _ = self.lt.parse_settlements(
            {
                "code": 200,
                "fundings": [{"timestamp": T0, "rate": "0.01", "direction": "long"}],
            },
            LT,
            T0,
            "s",
        )
        lt_ms, _ = self.lt.parse_settlements(
            {
                "code": 200,
                "fundings": [
                    {"timestamp": T0 * 1000, "rate": "0.01", "direction": "long"}
                ],
            },
            LT,
            T0,
            "s",
        )
        ex, _ = self.ex.parse_settlements(
            {"status": "OK", "data": [{"m": "BTC-USD", "T": T0 * 1000, "f": "0.0001"}]},
            EX,
            T0,
            "s",
        )
        for r in (hl, lt[0], lt_ms[0], ex[0]):
            self.assertIsInstance(r["settled_at"], int)
            self.assertEqual(r["settled_at"], T0)
        self.assertAlmostEqual(hl["settled_at_api"], T0 + 0.076)

    def test_sub_second_differences_are_one_payment(self):
        # The same payment reported as .499 s and later as .501 s (rounds apart).
        first = self.hl_rows((T0 * 1000 + 499, "0.0001"))
        again = self.hl_rows((T0 * 1000 + 501, "0.0001"))
        self.assertEqual(self.settle(first), ["new"])
        self.assertEqual(self.settle(again), ["same"])
        self.assertEqual(len(self.latest()), 1)
        n = self.store.db.execute("select count(*) from settlement_index").fetchone()[0]
        self.assertEqual(n, 1)

    def test_two_real_payments_an_hour_apart_stay_two(self):
        self.settle(self.hl_rows((T0 * 1000, "0.0001"), ((T0 + 3600) * 1000, "0.0001")))
        self.assertEqual(
            sorted(r["settled_at"] for r in self.latest()), [T0, T0 + 3600]
        )

    def test_a_correction_by_the_venue_is_a_new_revision_and_the_first_stays(self):
        self.settle(self.hl_rows((T0 * 1000, "0.0001")))
        out = self.settle(self.hl_rows((T0 * 1000 + 3, "0.0002")))
        self.assertEqual(out, ["correction"])
        (r,) = self.latest()
        self.assertEqual(
            (r["revision"], r["settled_rate_raw"], r["settled_at"]), (2, "0.0002", T0)
        )
        self.assertEqual(r["previous"]["settled_rate_raw"], "0.0001")
        all_lines = self.store.records("settlement", "Hyperliquid")
        self.assertEqual(
            sorted(x["revision"] for x in all_lines), [1, 2]
        )  # nothing erased
        (c,) = self.store.records("settlement_correction", "Hyperliquid")
        self.assertEqual(
            (c["before"]["settled_rate_raw"], c["after"]["settled_rate_raw"]),
            ("0.0001", "0.0002"),
        )
        # Reading the corrected value again is not another correction.
        self.assertEqual(self.settle(self.hl_rows((T0 * 1000, "0.0002"))), ["same"])
        self.assertTrue(
            consistency(self.store, self.cfg)["settlement_index"]["problems"] == []
        )

    def test_a_corrected_payment_replaces_its_comparison_not_adds_one(self):
        snaps = [
            dict(
                venue="Hyperliquid",
                asset="BTC",
                market_id="BTC",
                received_at=T0 - 600,
                published_rate=0.0001,
                published_rate_raw="0.0001",
                rate_unit="UNKNOWN",
                next_funding_at=None,
                raw_sha256="snap",
            )
        ]
        self.settle(self.hl_rows(((T0 - 3600) * 1000, "0.0001"), (T0 * 1000, "0.0001")))
        final, _ = matching.compare(snaps, self.latest(), T0 + 60, 7200)
        for c in final:
            self.store.put_comparison(c, T0)
        self.settle(self.hl_rows((T0 * 1000, "-0.0001")))  # the venue flips the sign
        final, _ = matching.compare(snaps, self.latest(), T0 + 60, 7200)
        for c in final:
            self.assertTrue(self.store.put_comparison(c, T0))
        rows = self.store.comparisons("Hyperliquid")
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]["comparison_status"], rows[0]["settlement_revision"]),
            ("SIGN_CONFLICT", 2),
        )

    def test_conflicting_entries_in_one_response_are_never_a_value(self):
        rows, stats = self.lt.parse_settlements(
            {
                "code": 200,
                "fundings": [
                    {"timestamp": T0, "rate": "0.01", "direction": "long"},
                    {"timestamp": T0, "rate": "0.01", "direction": "short"},
                ],
            },
            LT,
            T0,
            "s",
        )
        from tools.phase1_recorder.clients.records import dedupe_settlements

        kept, merged = dedupe_settlements(rows)
        self.assertEqual(len(kept), 1)
        self.assertIsNone(kept[0]["settled_rate"])
        self.assertEqual(kept[0]["settled_rate_status"], "CONFLICT")
        self.assertEqual(len(kept[0]["conflict_values"]), 2)
        self.assertEqual(merged["conflicting_duplicates"], 1)
        snap = dict(
            venue="Lighter",
            asset="BTC",
            market_id=1,
            received_at=T0 - 600,
            published_rate=1e-5,
            published_rate_raw="1e-5",
            next_funding_at=None,
            raw_sha256="x",
        )
        prev = dict(kept[0], settled_at=T0 - 3600, settled_rate=1e-4)
        final, _ = matching.compare([snap], [prev, kept[0]], T0, 7200)
        self.assertEqual(final[-1]["comparison_status"], "NO_SIGNED_RATE")
        # A later clean answer resolves it as a correction.
        self.settle(kept)
        clean, _ = self.lt.parse_settlements(
            {
                "code": 200,
                "fundings": [{"timestamp": T0, "rate": "0.01", "direction": "long"}],
            },
            LT,
            T0,
            "s2",
        )
        self.assertEqual(self.settle(clean), ["correction"])
        self.assertEqual(self.latest("Lighter")[0]["settled_rate_status"], "DECLARED")


class MissingDataTests(Fixture):
    def test_missing_values_are_never_zero(self):
        self.assertIsNone(num(None))
        self.assertIsNone(num(""))
        self.assertIsNone(num("nan"))
        self.assertEqual(num("0"), 0.0)  # a published zero stays a zero
        rows, _ = self.lt.parse_settlements(
            {"code": 200, "fundings": [{"timestamp": T0, "direction": "long"}]},
            LT,
            T0,
            "s",
        )
        self.assertIsNone(rows[0]["settled_rate"])
        self.assertIsNone(rows[0]["settled_rate_raw"])
        with self.assertRaises(SchemaError):
            self.hl.parse_settlements([dict(coin="BTC", time=T0 * 1000)], HL, T0, "s")
        with self.assertRaises(SchemaError):
            self.ex.parse_settlements(
                {"status": "OK", "data": [{"T": T0 * 1000}]}, EX, T0, "s"
            )

    def test_unknown_rates_never_count_as_a_comparison(self):
        snaps = [
            dict(
                venue="X",
                asset="A",
                market_id="A",
                received_at=T0 + 10,
                published_rate=None,
                published_rate_raw=None,
                next_funding_at=None,
                raw_sha256="s",
            )
        ]
        paid = [
            dict(
                settled_at=T0,
                settled_rate=1e-5,
                settled_rate_raw="1e-5",
                raw_sha256="p",
            ),
            dict(
                settled_at=T0 + 3600,
                settled_rate=1e-5,
                settled_rate_raw="1e-5",
                raw_sha256="q",
            ),
        ]
        self.assertEqual(matching.compare(snaps, paid, T0 + 3700, 1), ([], []))
        zero = [dict(snaps[0], published_rate=0.0, published_rate_raw="0")]
        final, _ = matching.compare(zero, paid, T0 + 3700, 1)
        self.assertEqual(final[0]["comparison_status"], "TOO_SMALL")


class StatusSeparationTests(unittest.TestCase):
    def test_settled_pending_not_recorded_and_unknown_are_distinct(self):
        h = 3600
        snap = lambda t: dict(
            venue="X",
            asset="A",
            market_id="A",
            received_at=t,
            published_rate=1e-5,
            published_rate_raw="1e-5",
            next_funding_at=None,
            raw_sha256="s%d" % t,
        )
        paid = [
            dict(
                settled_at=T0,
                settled_rate=1e-5,
                settled_rate_raw="1e-5",
                raw_sha256="p0",
            ),
            dict(
                settled_at=T0 + h,
                settled_rate=1e-5,
                settled_rate_raw="1e-5",
                raw_sha256="p1",
            ),
        ]
        final, pending = matching.compare(
            [snap(T0 + 100), snap(T0 + h + 100)], paid, T0 + h + 200, 7200
        )
        self.assertEqual([c["comparison_status"] for c in final], ["MATCHED"])
        self.assertEqual(pending[0]["comparison_status"], "PENDING")
        self.assertIsNone(pending[0]["settled_at"])
        _, pending = matching.compare([snap(T0 + h + 100)], paid, T0 + 5 * h, 7200)
        self.assertEqual(pending[0]["comparison_status"], "NOT_RECORDED")
        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "e"))
            try:
                for p in pending:
                    with self.assertRaises(ValueError):
                        store.put_comparison(p, T0)
            finally:
                store.close()

    def test_a_changed_schedule_never_links_across_periods(self):
        # Hourly payments, then an 8-hour gap: only snapshots after the previous
        # payment can be linked to the 8-hour one.
        h = 3600
        snap = lambda t, r: dict(
            venue="X",
            asset="A",
            market_id="A",
            received_at=t,
            published_rate=r,
            published_rate_raw=str(r),
            next_funding_at=None,
            raw_sha256="s%d" % t,
        )
        paid = [
            dict(
                settled_at=T0 + i * h,
                settled_rate=1e-5,
                settled_rate_raw="1e-5",
                raw_sha256="p%d" % i,
            )
            for i in range(4)
        ]
        paid.append(
            dict(
                settled_at=T0 + 11 * h,
                settled_rate=8e-5,
                settled_rate_raw="8e-5",
                raw_sha256="p8",
            )
        )
        snaps = [snap(T0 + 2 * h + 10, 1e-5), snap(T0 + 10 * h + 600, 8e-5)]
        final, _ = matching.compare(snaps, paid, T0 + 12 * h, 7200)
        last = [c for c in final if c["settled_at"] == T0 + 11 * h][0]
        self.assertEqual(last["observed_at"], T0 + 10 * h + 600)
        self.assertAlmostEqual(last["ratio"], 1.0)


class CrashSafetyTests(Fixture):
    def path_of(self, kind, venue="hyperliquid"):
        folder = os.path.join(self.cfg["root"], "normalized", kind, venue)
        return os.path.join(folder, sorted(os.listdir(folder))[0])

    def test_a_torn_line_is_reported_and_never_swallows_the_next_record(self):
        self.settle(self.hl_rows((T0 * 1000, "0.0001")))
        with open(self.path_of("settlement"), "a", encoding="utf-8") as f:
            f.write('{"venue": "Hyperli')  # the process died mid-write
        self.settle(self.hl_rows(((T0 + 3600) * 1000, "0.0001")))
        self.assertEqual(len(self.latest()), 2)
        cons = consistency(self.store, self.cfg)
        self.assertEqual(len(cons["torn_lines"]), 1)
        self.assertEqual(cons["schema_failures"], [])
        self.assertEqual(cons["settlement_index"]["problems"], [])

    def test_a_line_written_twice_is_read_once(self):
        self.settle(self.hl_rows((T0 * 1000, "0.0001")))
        path = self.path_of("settlement")
        line = open(path, encoding="utf-8").read()
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                line
            )  # crash after the line, before the index commit, then re-fetch
        self.assertEqual(len(self.store.records("settlement", "Hyperliquid")), 1)
        cons = consistency(self.store, self.cfg)
        self.assertEqual(cons["repeated_keys"], {"settlement": 1})

    def test_indexes_out_of_step_with_the_jsonl_heal_on_open(self):
        self.settle(self.hl_rows((T0 * 1000, "0.0001"), ((T0 + 3600) * 1000, "0.0001")))
        c = dict(
            venue="Hyperliquid",
            asset="BTC",
            market_id="BTC",
            settled_at=T0 + 3600,
            expected_settlement_at=None,
            comparison_status="MATCHED",
            observed_at=T0 + 10,
            published_rate_raw="1",
            settled_rate_raw="1",
            snapshot_raw_sha256="a",
            settlement_raw_sha256="b",
        )
        self.store.put_record(
            "comparison", "Hyperliquid", "BTC|%d" % (T0 + 3600), c, T0
        )
        # The comparison line was written but the process died before its index row;
        # one settlement row was lost the same way.
        self.store.db.execute("delete from settlement_index where settled_at=?", (T0,))
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.cfg["root"])
        self.assertIsNotNone(self.store.settlement_repair)
        self.assertIsNotNone(self.store.repair)
        self.assertEqual(self.store.comparison_counts(), (1, 1))
        cons = consistency(self.store, self.cfg)
        self.assertEqual(cons["settlement_index"]["problems"], [])
        self.assertEqual(cons["comparison_index"]["problems"], [])

    def test_an_interrupted_repair_leaves_the_old_state_and_completes_later(self):
        for i in range(3):
            c = dict(
                venue="Hyperliquid",
                asset="BTC",
                market_id="BTC",
                settled_at=T0 + i * 3600,
                expected_settlement_at=None,
                comparison_status="MATCHED",
                observed_at=T0,
                published_rate_raw="1",
                settled_rate_raw="1",
                snapshot_raw_sha256="a",
                settlement_raw_sha256="b",
            )
            self.store.put_record(
                "comparison", "Hyperliquid", "BTC|%d" % (T0 + i * 3600), c, T0
            )
        db = self.store.db
        db.execute("drop table comparisons")
        db.execute(
            "create table comparisons(venue text, market_id text, settled_at real, asset text,"
            " status text, body text, at real, primary key(venue, market_id, settled_at))"
        )
        db.execute(
            "insert into comparisons values('Hyperliquid','BTC',NULL,'BTC','MATCHED','{}',0)"
        )
        db.commit()
        self.store.close()
        real = Store._comparison_row
        calls = [0]

        def crash(store, c, at):
            calls[0] += 1
            if calls[0] == 2:  # the power fails halfway through the rebuild
                raise RuntimeError("güç kesildi")
            return real(store, c, at)

        self.store = None
        with mock.patch.object(Store, "_comparison_row", crash):
            with self.assertRaises(RuntimeError):
                Store(self.cfg["root"])
        raw = sqlite3.connect(os.path.join(self.cfg["root"], "recorder.sqlite3"))
        cols = [r[1] for r in raw.execute("pragma table_info(comparisons)")]
        legacy = [
            r[0]
            for r in raw.execute(
                "select name from sqlite_master where name like 'comparisons_legacy%'"
            )
        ]
        raw.close()
        self.assertNotIn(
            "expected_settlement_at", cols
        )  # rolled back: old table as it was
        self.assertEqual(legacy, [])
        self.store = Store(self.cfg["root"])  # the next open completes it
        self.assertEqual(self.store.repair["rebuilt_rows"], 3)
        self.assertEqual(self.store.comparison_counts(), (3, 3))


class DataSourceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.cfg = load(
            root=os.path.join(self.dir.name, "phase1-evidence"),
            assets=["BTC"],
            venues=["Extended"],
        )

    def tearDown(self):
        self.dir.cleanup()

    def test_mock_data_is_labelled_everywhere_and_never_mixed_with_live(self):
        rec = Recorder(
            self.cfg,
            transport=FakeVenues(self.clock),
            clock=self.clock,
            sleep=self.clock.sleep,
        )
        try:
            rec.run("once")
            cov = coverage(rec.store, self.cfg, self.clock())
            self.assertEqual(cov["data_source"], "MOCK:FakeVenues")
            self.assertFalse(cov["live"])
            m = cov["venues"]["Extended"]["markets"]["BTC"]
            self.assertTrue(m["funding_interval"].startswith("MOCK:"))
            self.assertIn("MOCK", cov["venues"]["Extended"]["connection"])
            text = text_report(cov)
            self.assertIn("GERÇEK BORSA VERİSİ İÇERMİYOR", text)
            _, manifest = export(rec.store, self.cfg, now=self.clock())
            self.assertFalse(manifest["live_data"])
            self.assertEqual(manifest["data_source"], "MOCK:FakeVenues")
            self.assertTrue(
                manifest["warnings"][0].startswith("GERÇEK BORSA VERİSİ DEĞİL")
            )
            src = rec.store.db.execute("select source from sessions").fetchone()[0]
            self.assertEqual(src, "MOCK:FakeVenues")
        finally:
            rec.store.close()
        # Live recording into this mock folder is refused.
        with self.assertRaises(ValueError):
            Recorder(self.cfg)

    def test_mock_recording_into_a_live_folder_is_refused(self):
        store = Store(self.cfg["root"])
        store.set_checkpoint("data_source", LIVE)
        store.close()
        with self.assertRaises(ValueError):
            Recorder(self.cfg, transport=FakeVenues(self.clock), clock=self.clock)

    def test_only_a_live_folder_carries_unprefixed_evidence(self):
        rec = Recorder(
            self.cfg,
            transport=FakeVenues(self.clock),
            clock=self.clock,
            sleep=self.clock.sleep,
        )
        try:
            rec.run("once")
            # Relabel the folder to show what a live folder's report looks like.
            rec.store.set_checkpoint("data_source", LIVE)
            cov = coverage(rec.store, self.cfg, self.clock())
            m = cov["venues"]["Extended"]["markets"]["BTC"]
            self.assertTrue(cov["live"])
            self.assertEqual(m["funding_interval"], "VERIFIED")
            self.assertNotIn("İÇERMİYOR", text_report(cov))
        finally:
            rec.store.close()

    def test_without_payments_no_evidence_status_is_produced(self):
        clock = Clock()
        fake = FakeVenues(clock)
        fake.fail["funding"] = [(503, b"down")] * 100
        self.cfg["venues"] = ["Extended"]
        rec = Recorder(self.cfg, transport=fake, clock=clock, sleep=clock.sleep)
        try:
            rec.run("once")
            rec.store.set_checkpoint("data_source", LIVE)  # even if it were live
            m = coverage(rec.store, self.cfg, clock())["venues"]["Extended"]["markets"][
                "BTC"
            ]
            self.assertEqual(m["settled_payments"], 0)
            self.assertEqual(m["funding_interval"], "UNKNOWN")
            self.assertEqual(m["rate_unit"], "UNKNOWN")
            self.assertNotIn(m["direction"], ("OBSERVED", "VERIFIED"))
        finally:
            rec.store.close()


if __name__ == "__main__":
    unittest.main()
