"""Phase 1 recorder tests. All responses come from tests/fakes.py (MOCK data, never
reported as market evidence). Tests that need the real public APIs are in
LivePublicApiTests and run only with NB_LIVE=1."""

import gzip
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

from tools.phase1_recorder import matching
from tools.phase1_recorder.clients import make_client
from tools.phase1_recorder.clients.base import SchemaError, epoch
from tools.phase1_recorder.clients.records import series_stats
from tools.phase1_recorder.config import load
from tools.phase1_recorder.export import export, verify_export
from tools.phase1_recorder.http import Http, HttpError, redact
from tools.phase1_recorder.recorder import Recorder, VenueRecorder
from tools.phase1_recorder.storage import Store, check_root
from tools.phase1_recorder.validation import consistency, coverage, verify_hashes

from .fakes import T0, Clock, FakeVenues

REPO = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


class Base(unittest.TestCase):
    venues = ["Hyperliquid", "LighterCore", "Lighter", "Extended"]

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.fake = FakeVenues(self.clock)
        self.cfg = load(
            root=os.path.join(self.dir.name, "phase1-evidence"),
            assets=["BTC", "ETH"],
            venues=self.venues,
        )
        self.cfg["backfill_days"] = 2
        self.recorder = None

    def tearDown(self):
        if self.recorder:
            self.recorder.store.close()
        self.dir.cleanup()

    def make(self):
        if self.recorder:
            self.recorder.store.close()
        self.recorder = Recorder(
            self.cfg, transport=self.fake, clock=self.clock, sleep=self.clock.sleep
        )
        return self.recorder

    def venue(self, name):
        return next(v for v in self.make_or_get().venues if v.venue == name)

    def make_or_get(self):
        return self.recorder or self.make()


class MetadataTests(Base):
    def test_each_venue_parses_its_markets_and_leaves_unknowns_null(self):
        rec = self.make()
        rec.run("once")
        store = rec.store
        hl = {m["asset"]: m for m in store.records("market", "Hyperliquid")}
        self.assertEqual(hl["BTC"]["max_leverage"], 40)
        self.assertAlmostEqual(hl["BTC"]["amount_step"], 1e-5)
        self.assertIsNone(hl["BTC"]["price_step"])  # not an API field: never guessed
        self.assertIsNone(hl["BTC"]["quote"])
        self.assertEqual(hl["BTC"]["contract_size_status"], "DECLARED")
        core = {m["asset"]: m for m in store.records("market", "LighterCore")}
        rh = {m["asset"]: m for m in store.records("market", "Lighter")}
        self.assertEqual(core["BTC"]["market_id"], 1)
        self.assertEqual(core["BTC"]["max_leverage"], 20)
        self.assertAlmostEqual(rh["ETH"]["price_step"], 0.01)
        # Separate venues, separate folders.
        self.assertTrue(
            os.path.isdir(os.path.join(self.cfg["root"], "raw", "lighter_rh"))
        )
        self.assertTrue(
            os.path.isdir(os.path.join(self.cfg["root"], "raw", "lighter_core"))
        )
        ex = {m["asset"]: m for m in store.records("market", "Extended")}
        self.assertEqual(ex["BTC"]["market_id"], "BTC-USD")
        self.assertEqual(ex["BTC"]["quote"], "USD")
        self.assertTrue(ex["BTC"]["active"])

    def test_a_response_missing_a_field_is_an_error_not_a_crash(self):
        self.fake.fail["orderBookDetails"] = [(200, {"code": 200, "nothing": []})] * 3
        rec = self.make()
        rec.run("once")
        rh = next(v for v in rec.venues if v.venue == "Lighter")
        self.assertEqual(rh.status["tasks"]["metadata"]["failed"], 1)
        self.assertIn("SchemaError", rh.status["tasks"]["metadata"]["note"])
        hl = next(v for v in rec.venues if v.venue == "Hyperliquid")
        self.assertEqual(hl.status["tasks"]["metadata"]["ok"], 1)
        errors = rec.store.records("error", "Lighter")
        self.assertTrue(any("order_book_details" in e["error"] for e in errors))


class FundingSemanticsTests(Base):
    def test_published_rates_are_raw_with_unknown_unit(self):
        self.make().run("once")
        for v in self.venues:
            for s in self.recorder.store.records("funding_snapshot", v):
                self.assertIsNone(s["normalized_hourly_rate"])
                self.assertEqual(s["rate_unit_status"], "UNKNOWN")
        ex = self.recorder.store.records("funding_snapshot", "Extended")[0]
        self.assertEqual(ex["next_funding_source"], "API marketStats.nextFundingRate")
        self.assertEqual(ex["next_funding_at"], (int(self.clock()) // 3600 + 1) * 3600)
        hl = self.recorder.store.records("funding_snapshot", "Hyperliquid")[0]
        self.assertIsNone(hl["next_funding_at"])
        self.assertIsNone(hl["index_price"])
        self.assertEqual(hl["oracle_price"], 99.9)
        # Other exchanges listed by Lighter's funding-rates are not Lighter's rates.
        core = self.recorder.store.records("funding_snapshot", "LighterCore")
        self.assertTrue(all(s["published_rate"] != 0.5 for s in core))

    def test_lighter_settled_rate_keeps_raw_and_declared_reading(self):
        self.fake.direction = "short"
        self.make().run("once")
        s = self.recorder.store.records("settlement", "Lighter")[0]
        self.assertEqual(s["settled_rate_raw"], "0.0012")
        self.assertEqual(s["direction_raw"], "short")
        self.assertEqual(s["direction_status"], "OBSERVED")
        self.assertAlmostEqual(s["settled_rate"], -0.000012)
        self.assertEqual(s["settled_rate_status"], "DECLARED")

    def test_an_unknown_direction_is_never_derived(self):
        self.fake.direction = None
        self.make().run("once")
        s = self.recorder.store.records("settlement", "LighterCore")[0]
        self.assertIsNone(s["settled_rate"])
        self.assertEqual(s["direction_status"], "UNKNOWN")
        hl = self.recorder.store.records("settlement", "Hyperliquid")[0]
        self.assertIsNone(hl["direction_raw"])
        self.assertEqual(hl["direction_status"], "UNKNOWN")

    def test_interval_states(self):
        self.make().run("once")
        cov = coverage(self.recorder.store, self.cfg, self.clock())
        self.assertEqual(
            cov["venues"]["Hyperliquid"]["markets"]["BTC"]["funding_interval"],
            "VERIFIED",
        )
        # A venue paying every 8 hours against a declared hour is a conflict.
        self.dir2 = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir2.cleanup)
        self.cfg["root"] = os.path.join(self.dir2.name, "phase1-evidence")
        self.fake.gap = 8 * 3600
        self.make().run("once")
        cov = coverage(self.recorder.store, self.cfg, self.clock())
        m = cov["venues"]["Extended"]["markets"]["BTC"]
        self.assertEqual(m["funding_interval"], "CONFLICT")
        self.assertEqual(m["observed_interval_seconds"], 28800)


class MatchingTests(unittest.TestCase):
    def snap(self, t, rate, nxt=None):
        return dict(
            venue="X",
            asset="A",
            market_id="A",
            received_at=t,
            published_rate=rate,
            published_rate_raw=str(rate),
            rate_unit="UNKNOWN",
            next_funding_at=nxt,
            raw_sha256="s%d" % t,
        )

    def paid(self, t, rate, direction=None):
        return dict(
            settled_at=t,
            settled_rate=rate,
            settled_rate_raw=str(rate),
            direction_raw=direction,
            raw_sha256="p%d" % t,
        )

    def test_the_latest_snapshot_of_the_payments_own_period_is_used(self):
        h = 3600
        snaps = [
            self.snap(T0 + 100, 1e-5),
            self.snap(T0 + 3000, 2e-5),
            self.snap(T0 + h + 10, 9e-5),
        ]
        paid = [
            self.paid(T0, 1e-5),
            self.paid(T0 + h, 2e-5),
            self.paid(T0 + 2 * h, 9e-5),
        ]
        out, pending = matching.compare(snaps, paid, T0 + 2 * h + 60, 7200)
        by = {c["settled_at"]: c for c in out}
        self.assertEqual(by[T0 + h]["observed_at"], T0 + 3000)
        self.assertEqual(by[T0 + h]["ratio"], 1.0)
        # The snapshot after T0+h belongs to the next period only.
        self.assertEqual(by[T0 + 2 * h]["observed_at"], T0 + h + 10)
        self.assertNotIn(T0, by)  # nothing observed in its period
        self.assertEqual(pending, [])

    def test_one_comparison_per_settlement_and_announced_times_must_agree(self):
        h = 3600
        snaps = [
            self.snap(T0 + 100, 1e-5, nxt=T0 + 2 * h)
        ]  # announced the wrong payment
        paid = [self.paid(T0, 1e-5), self.paid(T0 + h, 1e-5), self.paid(T0 + h, 1e-5)]
        out, _ = matching.compare(snaps, paid, T0 + h + 60, 7200)
        self.assertEqual(out, [])
        snaps = [self.snap(T0 + 100, 1e-5, nxt=T0 + h)]
        out, _ = matching.compare(snaps, paid, T0 + h + 60, 7200)
        self.assertEqual(len(out), 1)

    def test_signs_and_missing_directions(self):
        h = 3600
        out, _ = matching.compare(
            [self.snap(T0 + 10, 1e-5)],
            [self.paid(T0 - 1, 1), self.paid(T0 + h, -1e-5)],
            T0 + h,
            1,
        )
        self.assertEqual(out[-1]["comparison_status"], "SIGN_CONFLICT")
        out, _ = matching.compare(
            [self.snap(T0 + 10, 1e-5)],
            [self.paid(T0, 1), self.paid(T0 + h, None)],
            T0 + h,
            1,
        )
        self.assertEqual(out[-1]["comparison_status"], "NO_SIGNED_RATE")
        out, _ = matching.compare(
            [self.snap(T0 + 10, 0.0)],
            [self.paid(T0, 1), self.paid(T0 + h, 1e-5)],
            T0 + h,
            1,
        )
        self.assertEqual(out[-1]["comparison_status"], "TOO_SMALL")

    def test_pending_then_not_recorded(self):
        h = 3600
        paid = [self.paid(T0, 1e-5), self.paid(T0 + h, 1e-5)]
        _, pending = matching.compare(
            [self.snap(T0 + h + 60, 1e-5)], paid, T0 + h + 120, 7200
        )
        self.assertEqual(pending[0]["comparison_status"], "PENDING")
        self.assertEqual(pending[0]["expected_settlement_at"], T0 + 2 * h)
        _, pending = matching.compare(
            [self.snap(T0 + h + 60, 1e-5)], paid, T0 + 5 * h, 7200
        )
        self.assertEqual(pending[0]["comparison_status"], "NOT_RECORDED")


class LiveObservationTests(Base):
    """Funding snapshots every minute across several payments, then settlements."""

    venues = ["Lighter", "Extended", "Hyperliquid"]

    def drive(self, hours):
        rec = self.make()
        vr = {v.venue: v for v in rec.venues}
        for v in vr.values():
            v.run_task("metadata")
        for _ in range(int(hours * 60)):
            self.clock.t += 60
            for v in vr.values():
                v.run_task("funding")
        for v in vr.values():
            v.run_task("settlements")
        return rec

    def test_three_consistent_comparisons_are_reported(self):
        rec = self.drive(4)
        cov = coverage(rec.store, self.cfg, self.clock())
        for v in self.venues:
            m = cov["venues"][v]["markets"]["BTC"]
            self.assertGreaterEqual(m["comparison_statuses"].get("MATCHED", 0), 3, v)
            self.assertEqual(m["rate_unit"], "OBSERVED")  # never VERIFIED: docs unread
            self.assertEqual(m["sign_consistency"], 100.0)
        c = rec.store.comparisons("Lighter")[0]
        self.assertTrue(c["snapshot_raw_sha256"] and c["settlement_raw_sha256"])
        self.assertIsNotNone(rec.store.read_raw(c["snapshot_raw_sha256"]))

    def test_a_published_rate_in_other_units_is_a_conflict(self):
        self.fake.published["Extended"] = 0.0001  # 8x the settled per-payment rate
        rec = self.drive(4)
        m = coverage(rec.store, self.cfg, self.clock())["venues"]["Extended"][
            "markets"
        ]["BTC"]
        self.assertEqual(m["rate_unit"], "CONFLICT")
        self.assertAlmostEqual(m["published_settled_ratio"], 8.0)

    def test_opposite_signs_mark_the_direction_conflicted(self):
        self.fake.published["Hyperliquid"] = -0.0000125
        rec = self.drive(4)
        m = coverage(rec.store, self.cfg, self.clock())["venues"]["Hyperliquid"][
            "markets"
        ]["BTC"]
        self.assertEqual(m["direction"], "CONFLICT")
        self.assertEqual(m["sign_consistency"], 0.0)


class DataQualityTests(Base):
    venues = ["Hyperliquid"]

    def test_duplicate_payments_are_counted_and_stored_once(self):
        self.fake.duplicate_payment = True
        rec = self.make()
        rec.run("once")
        paid = rec.store.records("settlement", "Hyperliquid")
        keys = [(p["market_id"], p["settled_at"]) for p in paid]
        self.assertEqual(len(keys), len(set(keys)))
        cov = coverage(rec.store, self.cfg, self.clock())
        self.assertGreaterEqual(
            cov["venues"]["Hyperliquid"]["markets"]["BTC"]["duplicate_payments"], 1
        )

    def test_missing_candle_hours_stay_missing_and_are_retried_up_to_the_limit(self):
        gap = int(self.clock()) // 3600 * 3600 - 10 * 3600
        self.fake.missing_hours = {gap}
        rec = self.make()
        vr = rec.venues[0]
        vr.run_task("metadata")
        for _ in range(self.cfg["candle_retry_limit"] + 2):
            vr.run_task("candles")
        cov = coverage(rec.store, self.cfg, self.clock())
        self.assertEqual(
            cov["venues"]["Hyperliquid"]["markets"]["BTC"]["missing_candle_hours"], 1
        )
        asked = [
            c for c in self.fake.calls if c[2] and c[2].get("type") == "candleSnapshot"
        ]
        # BTC and ETH: one backfill each, then retries only while under the limit.
        self.assertEqual(len(asked), 2 * self.cfg["candle_retry_limit"])
        closes = [c["close"] for c in rec.store.records("candle", "Hyperliquid")]
        self.assertTrue(all(x == 100.5 for x in closes))  # nothing interpolated

    def test_out_of_order_candles_are_counted_and_stored_sorted(self):
        self.fake.shuffle_candles = True
        rec = self.make()
        rec.run("once")
        stats = rec.store.checkpoint("stats:Hyperliquid:BTC")
        self.assertGreater(stats["candle_out_of_order"], 0)

    def test_old_and_future_timestamps(self):
        st = series_stats([100.0, T0, T0 + 10 * 86400], T0)
        self.assertEqual((st["too_old"], st["future"]), (1, 1))
        self.assertEqual(epoch(T0 * 1000), T0)
        self.assertEqual(epoch(T0 * 10**9), T0)
        self.assertIsNone(epoch("x"))


class HttpTests(Base):
    venues = ["Hyperliquid"]

    def http(self, transport):
        store = Store(self.cfg["root"])
        self.addCleanup(store.close)
        h = Http(
            "Extended",
            store,
            self.cfg,
            transport=transport,
            sleep=self.clock.sleep,
            clock=self.clock,
        )
        return h, store

    def errors(self, store):
        return store.db.execute(
            "select status, error, rate_limited from audit order by id"
        ).fetchall()

    def test_timeout_is_retried(self):
        calls = []

        def t(method, url, body, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise socket.timeout("timed out")
            return 200, {}, b'{"ok": 1}'

        h, store = self.http(t)
        self.assertEqual(h.request("x", "https://e/x")[0], {"ok": 1})
        self.assertIn("Zaman aşımı", self.errors(store)[0][1])

    def test_429_backs_off_and_respects_retry_after(self):
        seq = [(429, {"retry-after": "7"}, b""), (200, {}, b"[]")]
        h, store = self.http(lambda *a: seq.pop(0))
        start = self.clock()
        h.request("x", "https://e/x")
        self.assertGreaterEqual(self.clock() - start, 7)
        self.assertEqual(self.errors(store)[0][2], 1)

    def test_5xx_retries_then_gives_up(self):
        h, store = self.http(lambda *a: (503, {}, b"down"))
        with self.assertRaises(HttpError) as cm:
            h.request("x", "https://e/x")
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(len(self.errors(store)), self.cfg["retries"] + 1)

    def test_broken_json_is_an_error(self):
        h, store = self.http(lambda *a: (200, {}, b"{not json"))
        with self.assertRaises(HttpError):
            h.request("x", "https://e/x")
        self.assertIn("Bozuk JSON", self.errors(store)[-1][1])

    def test_a_failing_venue_does_not_stop_the_others(self):
        self.cfg["venues"] = ["Hyperliquid", "Extended"]
        self.fake.fail["hyperliquid"] = [(503, b"down")] * 100
        rec = self.make()
        rec.run("once")
        st = {v.venue: v.status for v in rec.venues}
        self.assertFalse(st["Hyperliquid"]["connected"])
        self.assertTrue(st["Extended"]["connected"])
        cov = coverage(rec.store, self.cfg, self.clock())
        self.assertEqual(
            cov["venues"]["Hyperliquid"]["connection"], "BAĞLANTI KURULAMADI"
        )
        self.assertEqual(cov["venues"]["Extended"]["connection"], "BAĞLANDI")

    def test_secrets_are_never_recorded_and_only_public_reads_are_made(self):
        self.assertEqual(
            redact({"api_key": "k", "X-Signature": "s", "coin": "BTC"}),
            {"api_key": "<redacted>", "X-Signature": "<redacted>", "coin": "BTC"},
        )
        h, store = self.http(lambda *a: (200, {}, b"{}"))
        h.request("x", "https://e/x", params=dict(token="abc", market="BTC"))
        params = store.db.execute("select params from audit").fetchone()[0]
        self.assertNotIn("abc", params)
        with self.assertRaises(ValueError):
            h.request("x", "https://e/x", method="DELETE")
        hl = make_client(
            "Hyperliquid", store, self.cfg, transport=lambda *a: (200, {}, b"{}")
        )
        with self.assertRaises(ValueError):
            hl.info({"type": "clearinghouseState", "user": "0x0"}, "x")


class PersistenceTests(Base):
    venues = ["Lighter", "Extended"]

    def test_a_restart_continues_from_the_checkpoints(self):
        self.make().run("once")
        first = len(self.fake.calls)
        self.clock.t += 3600
        self.make().run("once")
        later = self.fake.calls[first:]
        fundings = [c for c in later if "fundings" in c[1]]
        starts = [
            int(
                dict(x.split("=") for x in c[1].split("?")[1].split("&"))[
                    "start_timestamp"
                ]
            )
            for c in fundings
        ]
        # Only the last two hours before the checkpoint are read again, not 2 days.
        self.assertTrue(all(s >= self.clock() - 4 * 3600 for s in starts), starts)
        candle_calls = [c for c in later if "candles" in c[1]]
        self.assertTrue(len(candle_calls) <= 4)

    def test_the_same_record_is_never_written_twice(self):
        rec = self.make()
        v = rec.venues[1]
        v.run_task("metadata")
        v.run_task("funding")
        v.run_task("funding")  # same second, same body
        snaps = rec.store.records("funding_snapshot", "Extended")
        self.assertEqual(len(snaps), 2)  # BTC and ETH once each
        raw = rec.store.db.execute("select count(*) from raw").fetchone()[0]
        self.assertEqual(raw, 1)  # one body, stored once

    def test_stop_file_and_ctrl_c_end_the_session_safely(self):
        rec = self.make()
        open(os.path.join(self.cfg["root"], "STOP"), "w").close()
        # A stale STOP from an earlier session is cleared when a session starts...
        real_join = threading.Thread.join
        state = dict(n=0)

        def join(self_, timeout=None):
            state["n"] += 1
            if state["n"] == 3:
                raise KeyboardInterrupt
            return real_join(self_, timeout)

        with mock.patch.object(threading.Thread, "join", join):
            stopped = rec.run("duration", hours=1, poll=0.01)
        self.assertEqual(stopped, "Ctrl+C")
        self.assertFalse(
            os.path.exists(os.path.join(self.cfg["root"], "recorder.lock"))
        )
        ended = rec.store.db.execute(
            "select ended, stopped_by from sessions"
        ).fetchone()
        self.assertIsNotNone(ended[0])
        self.assertEqual(ended[1], "Ctrl+C")
        # ...and a STOP written during a session ends it.
        rec2 = self.make()
        rec2.venues = rec2.venues[:1]

        def stop_soon():
            open(rec2.stop_file, "w").close()

        threading.Timer(0.2, stop_soon).start()
        self.assertEqual(rec2.run("duration", hours=1, poll=0.05), "STOP dosyası")


class IntegrityTests(Base):
    venues = ["Extended"]

    def test_hashes_detect_a_changed_raw_file(self):
        rec = self.make()
        rec.run("once")
        bad, n = verify_hashes(rec.store)
        self.assertEqual(bad, [])
        path = rec.store.db.execute("select path from raw limit 1").fetchone()[0]
        with gzip.open(os.path.join(self.cfg["root"], path), "wb") as f:
            f.write(b'{"tampered": true}')
        bad, _ = verify_hashes(rec.store)
        self.assertEqual(len(bad), 1)

    def test_replay_runs_without_any_network(self):
        rec = self.make()
        rec.run("once")
        with mock.patch(
            "tools.phase1_recorder.http.UrllibTransport.__call__",
            side_effect=AssertionError("ağa çıkıldı"),
        ):
            cons = consistency(rec.store, self.cfg)
        self.assertTrue(cons["ok"], cons)
        self.assertGreater(cons["replayed_responses"], 0)

    def test_export_is_complete_and_its_checksums_hold(self):
        rec = self.make()
        rec.run("once")
        path, manifest = export(rec.store, self.cfg, now=self.clock())
        self.assertEqual(verify_export(path), [])
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            top = "phase1-evidence-export/"
            for n in (
                "manifest.json",
                "source_versions.json",
                "coverage_report.json",
                "consistency_report.json",
                "checksums.sha256",
                "errors/http_errors.jsonl",
            ):
                self.assertIn(top + n, names)
            self.assertTrue(any(n.startswith(top + "raw/") for n in names))
            self.assertTrue(any(n.startswith(top + "normalized/") for n in names))
            text = z.read(top + "manifest.json").decode()
        # No local folder path ends up in the package.
        self.assertNotIn(self.dir.name, text)
        self.assertIn("extended.info/markets", manifest["endpoints"])
        # A changed file inside the zip is caught.
        bad = path + ".bad.zip"
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(bad, "w") as dst:
            for n in src.namelist():
                data = src.read(n)
                if n.endswith("source_versions.json"):
                    data = b"{}"
                dst.writestr(n, data)
        self.assertTrue(verify_export(bad))


class EngineIsolationTests(unittest.TestCase):
    def test_engine_folders_are_refused(self):
        for d in ("data-five-rh", "data", "data-railway"):
            with self.assertRaises(ValueError):
                check_root(os.path.join(REPO, d, "phase1"))

    def test_a_run_writes_nothing_outside_its_folder(self):
        with tempfile.TemporaryDirectory() as d:
            engine = os.path.join(d, "data-five-rh")
            os.makedirs(engine)
            db = os.path.join(engine, "paper.sqlite3")
            with open(db, "wb") as f:
                f.write(b"engine")
            before = os.stat(db).st_mtime_ns
            clock = Clock()
            cfg = load(
                root=os.path.join(d, "phase1-evidence"),
                assets=["BTC"],
                venues=["Extended"],
            )
            rec = Recorder(
                cfg, transport=FakeVenues(clock), clock=clock, sleep=clock.sleep
            )
            rec.run("once")
            rec.store.close()
            self.assertEqual(os.stat(db).st_mtime_ns, before)
            self.assertEqual(sorted(os.listdir(engine)), ["paper.sqlite3"])
            self.assertEqual(sorted(os.listdir(d)), ["data-five-rh", "phase1-evidence"])

    def test_the_recorder_does_not_load_the_engine(self):
        code = (
            "import sys, tools.phase1_recorder.cli, tools.phase1_recorder.recorder, "
            "tools.phase1_recorder.export;"
            "bad=[m for m in ('bot','risk_main','wide_paper','desk_analytics','risk_scan','ccxt') if m in sys.modules];"
            "print(bad); sys.exit(1 if bad else 0)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


@unittest.skipUnless(
    os.environ.get("NB_LIVE") == "1", "Gerçek public API testi: NB_LIVE=1 ile çalışır"
)
class LivePublicApiTests(unittest.TestCase):
    """Needs network access to the four venues' public APIs. Reports what it got;
    connection failures are failures, never replaced by mock data."""

    def test_once_against_the_real_apis(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = load(root=os.path.join(d, "phase1-evidence"), assets=["BTC"])
            cfg["backfill_days"] = 1
            rec = Recorder(cfg)
            rec.run("once")
            cov = coverage(rec.store, cfg)
            rec.store.close()
            for v, x in cov["venues"].items():
                with self.subTest(venue=v):
                    self.assertEqual(x["connection"], "BAĞLANDI", x["api"])


if __name__ == "__main__":
    unittest.main()
