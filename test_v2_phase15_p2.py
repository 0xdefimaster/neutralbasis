"""Phase 15 P2: cross-process rate-limit coordinator. Offline / MOCK only: no request
leaves the machine (subprocess workers only take tokens and write local files)."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from v2 import ratecoord as rc

ROOT = str(Path(__file__).resolve().parent)
PY = sys.executable


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.0)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="nb-p2-"))

    def tearDown(self):
        for c in getattr(self, "_coords", []):
            c.close()
        shutil.rmtree(self.d, ignore_errors=True)

    def coord(self, limits=None, **kw):
        self.clk = Clock()
        c = rc.Coordinator(self.d / "rc", limits or {"v": (5, 10.0)}, clock=self.clk,
                           sleep=self.clk.sleep, rand=lambda: 1.0, **kw)
        self._coords = getattr(self, "_coords", []) + [c]
        return c


class BucketTests(Base):
    def test_capacity_then_refill(self):
        c = self.coord()
        t0 = self.clk.t
        for _ in range(5):                                    # burst = capacity
            c.release("v.default.local", c.acquire("v.default.local"))
        self.assertEqual(self.clk.t, t0)
        c.acquire("v.default.local")
        self.assertAlmostEqual(self.clk.t - t0, 2.0, places=2)   # 1 token per 2 s

    def test_low_priority_keeps_a_reserve_for_high(self):
        c = self.coord(reserve_frac=0.4)                         # reserve 2 of 5 tokens
        s = "v.default.local"
        for _ in range(3):
            c.release(s, c.acquire(s, priority=rc.LOW))
        t = self.clk.t
        c.release(s, c.acquire(s, priority=rc.HIGH))             # HIGH may use reserve
        self.assertEqual(self.clk.t, t)
        c.release(s, c.acquire(s, priority=rc.LOW))              # LOW must wait
        self.assertGreater(self.clk.t, t)

    def test_retry_after_pauses_every_caller(self):
        c = self.coord()
        s = "v.default.local"
        c.release(s, c.acquire(s), ok=False, status=429, retry_after=7.0)
        t = self.clk.t
        c.release(s, c.acquire(s))
        self.assertGreaterEqual(self.clk.t - t, 7.0)
        self.assertEqual(c.snapshot()[s]["counters"]["http_429"], 1)

    def test_breaker_opens_half_opens_and_closes(self):
        c = self.coord(trip_after=3, open_base_s=10.0)
        s = "v.default.local"
        for _ in range(3):
            c.release(s, c.acquire(s), ok=False, status=503)
        with self.assertRaises(rc.CircuitOpen):
            c.acquire(s)
        self.assertEqual(c.snapshot()[s]["counters"]["breaker_refusals"], 1)  # saved
        self.clk.t += 10.0
        probe = c.acquire(s)                                     # HALF_OPEN: one probe
        self.assertEqual(c.snapshot()[s]["breaker"], rc.HALF_OPEN)
        c.release(s, probe, ok=True)
        self.assertEqual(c.snapshot()[s]["breaker"], rc.CLOSED)
        for _ in range(3):
            c.release(s, c.acquire(s), ok=False, status="timeout")
        self.clk.t += 10.0
        c.release(s, c.acquire(s), ok=False, status=503)         # probe fails
        st = c.snapshot()[s]
        self.assertEqual(st["breaker"], rc.OPEN)
        self.assertGreater(st["open_until"] - self.clk.t, 10.0)   # longer next time

    def test_crashed_probe_does_not_block_half_open_forever(self):
        c = self.coord(trip_after=1, open_base_s=1.0, lease_s=5.0)
        s = "v.default.local"
        c.release(s, c.acquire(s), ok=False, status=503)
        self.clk.t += 1.0
        c.acquire(s)                       # the probe; its holder "crashes" (no release)
        self.clk.t += 6.0                  # lease expires -> reaped -> probe slot freed
        c.acquire(s)
        self.assertGreaterEqual(c.snapshot()[s]["counters"]["leases_reaped"], 1)

    def test_clock_step_back_adds_no_tokens(self):
        c = self.coord()
        s = "v.default.local"
        for _ in range(5):
            c.release(s, c.acquire(s))
        self.clk.t -= 3600.0
        self.assertLess(c.snapshot()[s]["tokens"], 1.0)
        t = self.clk.t
        c.release(s, c.acquire(s))
        self.assertGreater(self.clk.t, t)                        # still had to wait

    def test_corrupt_state_resets_conservatively(self):
        c = self.coord()
        s = "v.default.local"
        c.release(s, c.acquire(s))
        lp = self.d / "rc" / (s + ".lock")
        with open(lp, "r+b") as f:                     # a torn record (crash mid-write)
            f.seek(rc.STATE_OFFSET + 50)
            f.write(b"\x00garbage")
        t = self.clk.t
        c.release(s, c.acquire(s))
        self.assertGreater(self.clk.t, t)                        # no free burst
        self.assertEqual(c.snapshot()[s]["counters"]["state_resets"], 1)

    def test_dead_pid_lease_is_reaped_and_in_flight_is_bounded(self):
        c = self.coord(max_in_flight=2)
        s = "v.default.local"
        a, b = c.acquire(s), c.acquire(s)
        lp = self.d / "rc" / (s + ".lock")
        with rc.FileLock(lp) as lk:
            st = rc.decode_state(rc.read_fd(lk.fd))
            st["leases"][a]["pid"] = 2 ** 22 + 12345             # a pid that does not exist
            rc.write_fd(lk.fd, st)
        c.acquire(s)                                             # reaped -> slot free
        self.assertEqual(c.snapshot()[s]["counters"]["leases_reaped"], 1)
        self.assertLessEqual(c.snapshot()[s]["in_flight"], 2)

    def test_lock_timeout_never_waits_forever(self):
        lp = self.d / "x.lock"
        with rc.FileLock(lp, timeout=1.0):
            t = time.monotonic()
            with self.assertRaises(rc.LockTimeout):
                with rc.FileLock(lp, timeout=0.2):
                    pass
            self.assertLess(time.monotonic() - t, 1.0)

    def test_state_record_is_binary_safe_and_detects_torn_writes(self):
        import random
        rnd = random.Random(7)
        for i in range(200):
            st = dict(tokens=rnd.random() * 100, counters={"x": i},
                      leases={"%d" % i: dict(pid=i, started=rnd.random())},
                      note="".join(chr(rnd.randrange(1, 255)) for _ in range(20)))
            blob = rc.encode_state(st)
            self.assertEqual(rc.decode_state(blob), st)
            for cut in (5, 39, len(blob) - 1):
                with self.assertRaises(ValueError):
                    rc.decode_state(blob[:cut])
        self.assertIsNone(rc.decode_state(b""))

    def test_threads_of_one_process_share_the_budget(self):
        import threading
        c = rc.Coordinator(self.d / "rc_t", {"v": (40, 1.0)}, max_in_flight=100)
        self._coords = getattr(self, "_coords", []) + [c]
        grants = []
        lock = threading.Lock()

        def work():
            for _ in range(20):
                c.release("v.default.local", c.acquire("v.default.local", timeout=30))
                with lock:
                    grants.append(time.time())

        t0 = time.time()
        ts = [threading.Thread(target=work) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        self.assertEqual(len(grants), 80)
        self.assertGreater(max(grants) - t0, 0.8)      # (80 - 40) / 40 = 1 s minimum

    def test_backoff_is_jittered_exponential_and_capped(self):
        self.assertEqual(rc.backoff_s(0), 0.0)
        lo = [rc.backoff_s(n, base=1, cap=30, rand=lambda: 0.0) for n in range(1, 8)]
        hi = [rc.backoff_s(n, base=1, cap=30, rand=lambda: 1.0) for n in range(1, 8)]
        self.assertEqual(hi, [1, 2, 4, 8, 16, 30, 30])
        self.assertEqual(lo, [x / 2 for x in hi])

    def test_never_writes_to_the_failing_disk(self):
        with self.assertRaises(Exception):
            rc.Coordinator("E:\\nb-ratecoord", {"v": (1, 1.0)})


WORKER = r'''
import json, os, sys, time
sys.path.insert(0, %(root)r)
from v2 import ratecoord as rc
c = rc.Coordinator(%(folder)r, {"v": (10, 2.0)})
out = []
for _ in range(%(n)d):
    l = c.acquire("v.default.local", timeout=60)
    out.append(time.time())
    c.release("v.default.local", l)
open(%(log)r + str(os.getpid()), "w").write(json.dumps(out))
'''


class CrossProcessTests(Base):
    def run_workers(self, code, n):
        ps = [subprocess.Popen([PY, "-c", code]) for _ in range(n)]
        for p in ps:
            self.assertEqual(p.wait(60), 0)

    def test_three_processes_share_one_budget(self):
        log = str(self.d / "grants-")
        code = WORKER % dict(root=ROOT, folder=str(self.d / "rc"), n=12, log=log)
        t0 = time.time()
        self.run_workers(code, 3)
        ts = sorted(t for f in self.d.glob("grants-*") for t in json.loads(f.read_text()))
        self.assertEqual(len(ts), 36)
        # token bucket bound: in any interval of length T at most capacity + rate x T
        for i, a in enumerate(ts):
            for b in ts[i:]:
                self.assertLessEqual(sum(1 for t in ts if a <= t <= b),
                                     10 + 5.0 * (b - a) + 1, (a, b))
        # three uncoordinated per-process budgets would finish in about 2.8 s; the shared
        # budget needs (36 - 10) / 5 = 5.2 s
        self.assertGreater(ts[-1] - t0, 4.5)

    def test_a_process_dying_inside_the_lock_releases_it(self):
        lp = self.d / "held.lock"
        code = ("import os, sys, time; sys.path.insert(0, %r); from v2 import ratecoord as rc;"
                "l = rc.FileLock(%r); l.__enter__(); open(%r, 'w').write('in');"
                "time.sleep(0.5); os._exit(3)") % (ROOT, str(lp), str(self.d / "flag"))
        p = subprocess.Popen([PY, "-c", code])
        for _ in range(200):
            if (self.d / "flag").exists():
                break
            time.sleep(0.02)
        self.assertEqual(p.wait(30), 3)                          # crashed while holding
        t = time.monotonic()
        with rc.FileLock(lp, timeout=5.0):
            pass
        self.assertLess(time.monotonic() - t, 2.0)

    def test_a_lease_of_a_dead_process_is_reaped(self):
        folder = str(self.d / "rc")
        code = ("import os, sys; sys.path.insert(0, %r); from v2 import ratecoord as rc;"
                "c = rc.Coordinator(%r, {'v': (10, 2.0)}, max_in_flight=1);"
                "c.acquire('v.default.local'); os._exit(4)") % (ROOT, folder)
        self.assertEqual(subprocess.call([PY, "-c", code], timeout=60), 4)
        c = rc.Coordinator(folder, {"v": (10, 2.0)}, max_in_flight=1)
        c.release("v.default.local", c.acquire("v.default.local", timeout=5))
        self.assertEqual(c.snapshot()["v.default.local"]["counters"]["leases_reaped"], 1)

    def test_shared_cache_single_flight_across_processes(self):
        folder, cnt = str(self.d / "cache"), str(self.d / "fetches")
        code = "\n".join([
            "import sys, time, json",
            "sys.path.insert(0, %r)" % ROOT,
            "from v2 import ratecoord as rc",
            "c = rc.SharedFileCache(%r, ttl=60)" % folder,
            "def f():",
            "    open(%r, 'a').write('x')" % cnt,
            "    time.sleep(0.5)",
            "    return {'v': 1}",
            "v, hit, at = c.get(('stats', 'live'), f)",
            "print(json.dumps([v, hit]))"])
        ps = [subprocess.Popen([PY, "-c", code], stdout=subprocess.PIPE) for _ in range(3)]
        outs = [json.loads(p.communicate(timeout=60)[0]) for p in ps]
        self.assertEqual(Path(cnt).read_text(), "x")             # ONE fetch
        self.assertEqual(sorted(o[1] for o in outs), [False, True, True])
        self.assertTrue(all(o[0] == {"v": 1} for o in outs))

    def test_shared_cache_is_bounded(self):
        c = rc.SharedFileCache(self.d / "cache2", ttl=60, max_entries=5)
        for i in range(12):
            c.get(("k", i), lambda i=i: i)
        self.assertLessEqual(len(list((self.d / "cache2").glob("*.json"))), 5)
        self.assertEqual(c.stats["evictions"], 7)


class HttpIntegrationTests(Base):
    def test_switch_off_keeps_the_per_process_path(self):
        with mock.patch.dict(os.environ, {rc.ENV: "0"}):
            self.assertFalse(rc.enabled())
        with self.assertRaises(ValueError):
            rc.enabled({rc.ENV: "yes"})

    def test_shared_path_handles_429_then_success(self):
        from v2.venues import base
        clk = Clock()
        coord = rc.Coordinator(self.d / "rc", {"arcus": (60, 60.0)}, clock=clk,
                               sleep=clk.sleep, rand=lambda: 1.0)
        calls = []

        def fake_open(method, url, data, h, timeout):
            calls.append(url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(url, 429, "busy", {"Retry-After": "3"},
                                             io.BytesIO(b"{}"))
            return {"ok": 1}

        with mock.patch.object(rc, "_SHARED", coord), \
                mock.patch.dict(os.environ, {rc.ENV: "1"}), \
                mock.patch.object(base, "_open", fake_open):
            out = base.http_json("arcus", "GET", "https://api.arcus.xyz/v1/markets")
        self.assertEqual(out, {"ok": 1})
        st = coord.snapshot()["arcus.default.local"]
        self.assertEqual((st["counters"]["http_429"], st["counters"]["granted"]), (1, 2))
        self.assertEqual(st["in_flight"], 0)
        self.assertGreaterEqual(clk.t - 1_000_000.0, 3.0)        # Retry-After honoured

    def test_doctor_shows_budget_and_unknown_external_share(self):
        from v2.control import doctor
        recs = doctor.rate_budget(time.time(), folder=self.d / "missing")
        self.assertEqual(recs[0]["health_status"], "NOT_TESTED")
        self.assertIn("UNKNOWN", recs[0]["health_reason"])
        c = self.coord(limits={"arcus": (60, 60.0)})
        c.release("arcus.default.local", c.acquire("arcus.default.local"), ok=False,
                  status=429, retry_after=1e9)
        recs = doctor.rate_budget(self.clk.t, folder=self.d / "rc")
        self.assertEqual(recs[0]["health_status"], "RATE_LIMITED")
        self.assertIn("UNKNOWN", recs[0]["health_reason"])


if __name__ == "__main__":
    unittest.main()
