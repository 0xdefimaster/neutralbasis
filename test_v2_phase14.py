"""Phase 14: Doctor, health model, capability matrix, data contract, telemetry,
funding persistence, read model and the GET-only Control Center server.

MOCK data is labelled; OBSERVED-data tests skip when the saved artifacts are absent.
"""
import json
import os
import shutil
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from v2 import paths
from v2.control import capability, contract, doctor, model, netguard

HAVE_DATA = paths.bench("v2-phase4-2026-11-12", "answers.json.gz").exists()


class ModelTests(unittest.TestCase):
    def test_unknown_is_never_healthy(self):
        r = model.record("txflow", "connectivity", "HEALTHY", "socket open")
        self.assertEqual(r["health_status"], "UNVERIFIED")          # no evidence level
        r = model.record("txflow", "freshness", "HEALTHY", "x", evidence_level="LIVE",
                         data_source="MOCK")
        self.assertEqual(r["health_status"], "UNVERIFIED")          # MOCK cannot prove
        r = model.record("txflow", "freshness", "HEALTHY", "fresh", evidence_level="LIVE",
                         data_source="OBSERVED")
        self.assertEqual((r["health_status"], r["time_unit"]), ("HEALTHY", "s"))
        with self.assertRaises(model.ModelError):
            model.record("x", "vibes", "HEALTHY", "y")
        with self.assertRaises(model.ModelError):
            model.record("x", "freshness", "OK", "y")
        self.assertEqual(model.worst([]), "NOT_TESTED")
        self.assertEqual(model.worst(["HEALTHY", "STALE"]), "STALE")

    def test_dimensions_are_independent(self):
        self.assertEqual(len(model.DIMENSIONS), 7)
        self.assertIn("funding_evidence", model.DIMENSIONS)
        self.assertIn("connectivity", model.DIMENSIONS)


class CapabilityTests(unittest.TestCase):
    def test_every_venue_every_capability_with_a_level(self):
        mx = capability.matrix(now=2_000_000_000.0, index={})
        self.assertEqual(set(mx), set(capability.VENUES))
        self.assertEqual(len(mx), 12)                     # 8 PAPER + 11 OBSERVE, 12 unique
        for v, m in mx.items():
            self.assertEqual(set(m["capabilities"]), set(capability.CAPABILITIES))
            for c in m["capabilities"].values():
                self.assertIn(c["level"], capability.LEVELS)
        caps = lambda v: mx[v]["capabilities"]  # noqa: E731
        self.assertEqual(caps("variational")["execution_model"]["level"], "UNSUPPORTED")
        self.assertEqual(caps("variational")["websocket"]["level"], "UNSUPPORTED")
        self.assertEqual(caps("txflow")["funding_history"]["level"], "UNSUPPORTED")
        self.assertEqual(caps("hyperliquid_native")["execution_model"]["level"], "UNSUPPORTED")
        self.assertEqual(caps("txflow")["websocket"]["level"], "MOCK_TESTED")
        self.assertNotIn("VERIFIED", [c["level"] for c in caps("txflow").values()])

    def test_old_live_observation_is_not_current(self):
        idx = {"txflow": {"ws": [dict(as_of=1000.0, source="probe")],
                          "rest": [dict(as_of=1000.0, source="stream")]}}
        mx = capability.matrix(now=1000.0 + 7200, index=idx)
        ws = mx["txflow"]["capabilities"]["websocket"]
        self.assertEqual((ws["level"], ws["current"]), ("LIVE_OBSERVED", False))
        self.assertFalse(mx["txflow"]["last_verification_current"])
        mx2 = capability.matrix(now=1000.0 + 60, index=idx)
        self.assertTrue(mx2["txflow"]["capabilities"]["websocket"]["current"])

    @unittest.skipUnless(HAVE_DATA, "OBSERVED snapshot not here")
    def test_snapshot_keys_are_parsed_to_venue_ids(self):
        idx = capability.artifact_index()
        for v in ("lighter_rh", "arcus", "txflow", "tradexyz", "variational"):
            self.assertIn("rest", idx.get(v, {}), v)
        self.assertFalse(any(" " in k for k in idx))          # no "xid POST url" keys


class NetGuardTests(unittest.TestCase):
    def test_offline_blocks_and_records_any_connection(self):
        with netguard.offline() as attempts:
            with self.assertRaises(netguard.OfflineViolation):
                socket.create_connection(("api.hyperliquid.xyz", 443), timeout=1)
            with self.assertRaises(netguard.OfflineViolation):
                socket.getaddrinfo("rpc.txflow.com", 443)
        self.assertEqual(len(attempts), 2)

    def test_loopback_can_be_allowed(self):
        with netguard.offline(allow_loopback=True) as attempts:
            with self.assertRaises(netguard.OfflineViolation):
                socket.getaddrinfo("example.com", 80)
            try:
                socket.getaddrinfo("127.0.0.1", 80)
            except netguard.OfflineViolation:
                self.fail("loopback refused")
        self.assertEqual(attempts, ["example.com"])


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nb-p14-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_offline_doctor_makes_no_network_call_and_never_claims_live_health(self):
        with mock.patch.dict(os.environ, {"NB_V2_BENCH_DIR": str(self.tmp)}):
            with netguard.offline() as attempts:
                res = doctor.run(now=2_000_000_000.0)
        self.assertEqual(attempts, [])
        venue_recs = [r for r in res["records"] if r["exchange_id"] in capability.VENUES]
        self.assertTrue(venue_recs)
        self.assertNotIn("HEALTHY", {r["health_status"] for r in venue_recs})
        self.assertEqual(res["mode"], "OFFLINE")
        sysrecs = {r["channel"]: r for r in res["records"] if r["exchange_id"] == "system"}
        self.assertEqual(sysrecs["recorder"]["health_status"], "DISCONNECTED")
        self.assertNotEqual(sysrecs["clock"]["health_status"], "HEALTHY")

    def test_cli_live_refused_and_status_without_saved_result(self):
        from tools import nb_doctor

        self.assertEqual(nb_doctor.main(["--live"]), 2)
        with mock.patch.dict(os.environ, {"NB_V2_BENCH_DIR": str(self.tmp)}):
            self.assertEqual(nb_doctor.main(["--status"]), 1)
            self.assertEqual(nb_doctor.main(["--offline", "--json"]), 0)
            self.assertEqual(nb_doctor.main(["--status"]), 0)

    def test_save_is_bounded(self):
        with mock.patch.dict(os.environ, {"NB_V2_BENCH_DIR": str(self.tmp)}):
            for k in range(8):
                doctor.save(dict(generated_at=1_800_000_000 + k, records=[]))
            files = list(doctor.control_dir().glob("doctor-2*.json"))
        self.assertEqual(len(files), 5)


class ContractTests(unittest.TestCase):
    def test_ws_and_rest_records_map_to_one_contract(self):
        ws = contract.normalize_ws(dict(venue="txflow", market="119", channel="l2BookDepth",
                                        source_ts=1.5, recv_ts=2.0, seq=7, prev_seq=6,
                                        kind="delta"), "txflow")
        self.assertEqual(set(ws), set(contract.FIELDS))
        self.assertEqual((ws["source_unit"], ws["sequence"], ws["kind"]), ("ms", 7, "delta"))
        lr = contract.normalize_ws(dict(venue="lighter_rh", market="1", channel="order_book",
                                        source_raw=1794534826596857), "lighter_rh")
        self.assertEqual((lr["source_unit"], lr["source_ts"]), ("NOT_STATED", None))
        ans = contract.normalize_answer("xyz_book_NATGAS", dict(
            ok=True, xid="tradexyz", t1=10.0, data={"time": 9500, "levels": [[], []]}),
            raw_ref="stream#1")
        self.assertEqual((ans["source_ts"], ans["data_type"], ans["raw_ref"]),
                         (9.5, "book", "stream#1"))
        self.assertEqual(len(contract.AUDIT), 4)


class RateLimitTests(unittest.TestCase):
    def test_shared_cache_is_bounded_and_single_flight(self):
        from v2 import ratelimit

        now = [0.0]
        c = ratelimit.SharedCache(ttl=10, clock=lambda: now[0], max_items=3)
        calls = []
        for k in range(5):
            now[0] += 1
            c.get(k, lambda k=k: calls.append(k) or k)
        self.assertEqual(len(c.items), 3)
        self.assertEqual(sorted(c.items), [2, 3, 4])
        v, hit = c.get(4, lambda: calls.append("again"))
        self.assertTrue(hit)
        self.assertNotIn("again", calls)


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nb-p14c-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_windows_cadence_and_ages(self):
        from v2.control import telemetry as tm

        now, mono = [1_800_000_000.0], [100.0]
        t = tm.Telemetry(clock=lambda: now[0], mono=lambda: mono[0])
        for k in range(10):                               # a message every 3 s
            t.event("txflow", "119", "activeAssetCtx", "message", source_ts=now[0] - 0.4)
            if k % 2 == 0:
                t.event("txflow", "119", "activeAssetCtx", "data_change")
            now[0] += 3
            mono[0] += 3
        t.event("txflow", "119", "activeAssetCtx", "http_429")
        s = t.snapshot()[0]
        self.assertAlmostEqual(s["observed_cadence_s"], 3.0)
        self.assertEqual(s["windows"]["5m"]["counts"]["message"], 10)
        self.assertEqual(s["windows"]["5m"]["counts"]["http_429"], 1)
        self.assertAlmostEqual(s["message_age_s"], 3.0)
        self.assertAlmostEqual(s["data_change_age_s"], 6.0)     # last change at k=8
        self.assertEqual(s["clock_confidence"], "UNKNOWN")
        now[0] += 3600
        s = t.snapshot()[0]
        self.assertEqual(s["windows"]["1m"]["counts"], {})        # rolled out
        self.assertEqual(s["windows"]["1440m"]["counts"]["message"], 10)
        with self.assertRaises(ValueError):
            t.event("x", None, "y", "bogus")

    def test_monotonic_cadence_survives_a_wall_clock_step(self):
        from v2.control import telemetry as tm

        now, mono = [1_800_000_000.0], [0.0]
        t = tm.Telemetry(clock=lambda: now[0], mono=lambda: mono[0])
        t.event("hl", "INIT", "l2Book", "message")
        now[0] -= 30                                      # NTP steps the wall clock back
        mono[0] += 10
        t.event("hl", "INIT", "l2Book", "message")
        self.assertEqual(t.snapshot()[0]["observed_cadence_s"], 10.0)

    def test_books_hook_turns_ws_records_into_events(self):
        import test_v2_phase10 as t10
        from v2 import ws_readonly
        from v2.control import telemetry as tm

        tel = tm.Telemetry(clock=lambda: 1_794_500_001.0, mono=time.monotonic)
        b = ws_readonly.Books(lambda: 1_794_500_001.0, on_record=tm.books_hook(tel))
        b.handle("txflow", t10.depth(1, None, "snapshot", [(3.6, 1)], [(3.61, 1)]))
        b.handle("txflow", t10.depth(3, 2, "delta", [(3.59, 1)], []))   # gap
        s = {x["channel"]: x for x in tel.snapshot()}["l2BookDepth"]
        c = s["windows"]["1m"]["counts"]
        self.assertEqual((c["message"], c["data_change"], c["sequence_gap"]), (2, 1, 1))

    def test_heartbeat_atomic_bounded_and_stale_detection(self):
        from v2.control import telemetry as tm

        now = [1_800_000_000.0]
        t = tm.Telemetry(clock=lambda: now[0])
        t.event("txflow", "119", "ctx", "message")
        w = tm.HeartbeatWriter(self.tmp, "recorder", every_s=15, max_bytes=2000,
                               clock=lambda: now[0])
        self.assertTrue(w.maybe_write(t))
        self.assertFalse(w.maybe_write(t))                 # within the interval
        for _ in range(40):
            now[0] += 16
            w.maybe_write(t)
        self.assertLessEqual(w.history.stat().st_size, 2000)
        self.assertTrue(w.history.with_suffix(".jsonl.1").exists())
        r = tm.read_heartbeat(self.tmp, "recorder", now=now[0] + 10)
        self.assertEqual(r["status"], "RUNNING")
        r = tm.read_heartbeat(self.tmp, "recorder", now=now[0] + 100)
        self.assertEqual(r["status"], "STALE")
        self.assertEqual(tm.read_heartbeat(self.tmp, "nope")["status"], "NOT_RUNNING")
        self.assertTrue(tm.read_history(self.tmp, "recorder", since_s=3600, now=now[0]))
        self.assertIsInstance(tm.process_memory_mb(), float)


class FundingPersistenceTests(unittest.TestCase):
    """Slice D. All series here are MOCK (synthetic), never evidence."""

    @staticmethod
    def mock_ar1(n, phi, mu=1e-4, sd=2e-5, seed=7, t0=1_790_000_000.0):
        import random
        rnd = random.Random(seed)
        x, out = mu, []
        for i in range(n):
            x = mu + phi * (x - mu) + rnd.gauss(0, sd)
            out.append((t0 + i * 3600.0, x))
        return out

    def test_ar1_recovers_phi_and_half_life(self):
        from v2 import funding_persistence as fp
        s = self.mock_ar1(3000, 0.9)
        mu, phi = fp.ar1([r for _, r in s])
        self.assertAlmostEqual(phi, 0.9, delta=0.03)
        self.assertAlmostEqual(fp.half_life(0.5), 1.0)
        self.assertIsNone(fp.half_life(-0.2))
        m = fp.metrics(s)
        self.assertEqual(m["points"], 3000)
        self.assertAlmostEqual(m["half_life_h"], fp.half_life(m["ar1_phi"]))

    def test_insufficient_history(self):
        from v2 import funding_persistence as fp
        s = self.mock_ar1(50, 0.9)
        v = fp.validate(s)
        self.assertEqual(v["status"], "INSUFFICIENT_HISTORY")
        self.assertEqual(fp.confidence(fp.metrics(s), v), "INSUFFICIENT_HISTORY")
        self.assertEqual(fp.metrics([])["confidence"], "INSUFFICIENT_HISTORY")

    def test_validation_has_no_look_ahead(self):
        from v2 import funding_persistence as fp
        s = self.mock_ar1(400, 0.95)
        v1 = fp.validate(s, return_rows=True)
        cut = v1["train_points"]
        # change everything after the train part: the fit must not move
        s2 = s[:cut] + [(t, r * -3 + 1e-3) for t, r in s[cut:]]
        v2 = fp.validate(s2, return_rows=True)
        for k in ("train_mean", "train_phi", "adverse_q10", "adverse_q90"):
            self.assertEqual(v1[k], v2[k], k)
        # a forecast made at t uses nothing after t: perturb only the points after the
        # first validation row and its forecasts stay identical
        t = v1["rows"][0]["t"]
        s3 = [(u, r if u <= t else r + 5e-4) for u, r in s]
        v3 = fp.validate(s3, return_rows=True)
        a, b = v1["rows"][0], v3["rows"][0]
        for k in ("base", "pers", "trail24", "local"):
            self.assertEqual(a[k], b[k], k)
        self.assertNotEqual(a["real"], b["real"])     # only the realized value moved

    def test_confidence_never_high_and_needs_30_days(self):
        from v2 import funding_persistence as fp
        short = self.mock_ar1(24 * 10, 0.9)
        long_ = self.mock_ar1(24 * 40, 0.9)
        self.assertEqual(fp.confidence(fp.metrics(short), fp.validate(short)), "LOW")
        self.assertEqual(fp.confidence(fp.metrics(long_), fp.validate(long_)), "MEDIUM")

    def test_no_history_leg_is_trusted_for_one_period_only(self):
        from v2 import funding_persistence as fp
        p = fp.leg_path(-1.6e-5, "persistence", 24)
        self.assertEqual(p[0], -1.6e-5)
        self.assertTrue(all(x is None for x in p[1:]))
        cash, partial = fp.pair_cash(p, [7e-4] * 24, 1000.0)
        self.assertIsNone(cash)                          # never a number: UNKNOWN hours
        self.assertEqual(partial["known_hours"], 1)
        p2 = fp.leg_path(4e-5, "persistence", 24, samples=[7e-4, 9e-4, 4e-5])
        self.assertEqual(p2[0], 4e-5)
        self.assertAlmostEqual(p2[5], (7e-4 + 9e-4 + 4e-5) / 3)
        # baseline holds the published rate for the whole hold: kept only as a
        # comparison model, it is what the research shows to be unsafe
        self.assertEqual(fp.leg_path(4e-5, "baseline", 24), [4e-5] * 24)

    def test_snap_hour_normalizes_interval_and_drops_off_grid(self):
        from tools.v2_funding_persistence import snap_hour
        t0 = 1_790_006_400.0  # multiple of 8 h
        rows = [dict(ts=t0 + 0.05, rate=8e-4), dict(ts=t0 + 8 * 3600 + 1000, rate=1e-3),
                dict(ts=t0 + 16 * 3600, rate=None)]
        s = snap_hour(rows, 8 * 3600)
        self.assertEqual(s, [(t0, 1e-4)])                # per hour; off-grid row dropped

    @unittest.skipUnless(HAVE_DATA, "saved artifacts absent")
    def test_observed_natgas_case_offline(self):
        """OBSERVED saved data (not MOCK): the 15:00 reference exists, the baseline is
        the only positive model, and the TxFlow payment stays UNVERIFIED."""
        from tools import v2_funding_persistence as t
        bench = paths.bench()
        with netguard.offline() as g:
            rows, later, xyz = t.published_natgas(bench)
            fit = dict(mean=1e-4, phi=0.96, adverse_q10=-3e-5)
            c = t.natgas_case(rows, later, xyz, fit)
        self.assertEqual(g, [])
        self.assertGreater(c["models"]["baseline"]["cash_24h"], 0)
        self.assertLess(c["models"]["persistence"]["cash_24h"], 0)
        self.assertLess(c["models"]["adverse"]["cash_24h"], 0)
        self.assertIn("UNVERIFIED", c["txflow_payment_evidence"])


if __name__ == "__main__":
    unittest.main()
