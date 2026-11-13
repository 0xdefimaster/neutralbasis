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


if __name__ == "__main__":
    unittest.main()
