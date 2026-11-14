"""Single-boundary pilot tool: MOCK transport and clock only; nothing is sent."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import v2_boundary_pilot as bp

B = 1_794_488_400.0          # a whole hour (MOCK)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


def mock_http(fail=None):
    calls = []

    def http(xid, method, url, body, headers=None):
        calls.append((xid, body.get("type")))
        if fail and fail(xid, len(calls)):
            raise TimeoutError("MOCK timeout")
        if xid == "txflow":
            return {"data": {"nodeCtx": {"funding": "0.0042", "blockTime": int(clk_ref[0]() * 1000),
                                         "prevFundingTime": int((B if clk_ref[0]() >= B else
                                                                 B - 3600) * 1000)}}}
        if body.get("type") == "fundingHistory":
            return [{"time": int(B * 1000), "fundingRate": "0.0006906"}]
        return [{"universe": [{"name": "xyz:NATGAS"}]}, [{"funding": "0.00069058"}]]
    return http, calls


clk_ref = [None]


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="nb-pilot-"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def pilot(self, http, free=10 ** 12):
        clk = Clock(B - 600)
        clk_ref[0] = clk
        return bp.Pilot(self.d / "out", http, clock=clk, sleep=clk.sleep,
                        disk_free=lambda p: free, index="119"), clk

    def test_exactly_52_requests_and_the_cap_holds(self):
        self.assertEqual(bp.planned_requests(B), 52)
        http, calls = mock_http()
        p, clk = self.pilot(http)
        man = p.run(B)
        self.assertEqual((man["requests"], len(calls), man["abort"]), (52, 52, None))
        self.assertLessEqual(clk(), B + bp.STOP_AFTER_S)
        with self.assertRaises(bp.PilotRefused):
            p.call("txflow", "POST", "u", {"type": "x"})                # a 53rd
        self.assertLess(man["bytes"], bp.MAX_BYTES)

    def test_a_failing_venue_stops_after_three_and_the_other_goes_on(self):
        http, calls = mock_http(fail=lambda xid, n: xid == "txflow")
        p, _ = self.pilot(http)
        man = p.run(B)
        self.assertEqual(man["stopped"], {"txflow": "3 consecutive failures"})
        self.assertEqual(sum(1 for x, _ in calls if x == "txflow"), 3)
        self.assertEqual(sum(1 for x, _ in calls if x == "tradexyz"), 27)

    def test_guard_refusal_and_disk_floor_abort(self):
        def refused(xid, method, url, body, headers=None):
            raise PermissionError("PAPER pilot network guard: host not allowed")
        p, _ = self.pilot(refused)
        self.assertTrue(p.run(B)["abort"].startswith("EGRESS_GUARD"))
        shutil.rmtree(self.d / "out")
        http, _ = mock_http()
        p, _ = self.pilot(http, free=1 * 2 ** 30)
        self.assertEqual(p.run(B)["abort"], "DISK_FLOOR")

    def test_never_appends_and_never_on_the_failing_disk(self):
        http, _ = mock_http()
        p, _ = self.pilot(http)
        p.run(B)
        p2, _ = self.pilot(http)
        with self.assertRaises(bp.PilotRefused):
            p2.run(B)
        with self.assertRaises(Exception):
            bp.Pilot(Path("E:/nb-pilot"), http).run(B)

    def test_analysis_uses_venue_times(self):
        http, _ = mock_http()
        p, _ = self.pilot(http)
        p.run(B)
        r = bp.analyze(self.d / "out")
        self.assertEqual(r["txflow"]["pattern"], "CONTINUES")
        self.assertTrue(r["txflow"]["prev_funding_time_rolled"])
        self.assertEqual(r["txflow"]["payment"], "UNKNOWN (no account evidence)")
        self.assertEqual(r["tradexyz"]["verdict"], "CONVERGES")

    def test_run_without_approval_sends_nothing(self):
        with mock.patch("pilot.network_guard.install") as inst, \
                mock.patch("v2.venues.base.http_json") as http:
            rc = bp.main(["--run", "--out", str(self.d / "x")])
        self.assertEqual(rc, 2)
        inst.assert_not_called()
        http.assert_not_called()

    def test_preflight_is_offline_and_reports(self):
        from v2.control import netguard
        with netguard.offline() as g:
            res = bp.preflight(self.d / "pf", now=B - 600, environ={}, run=True)
        self.assertEqual(len(g), 0)
        st = {n: s for n, s, _ in res}
        self.assertEqual(st["plan"], "PASS")
        self.assertEqual(st["timing"], "PASS")
        self.assertEqual(st["approval env"], "BLOCK")
        self.assertEqual(st["shared budget"], "WARN")


if __name__ == "__main__":
    unittest.main()
