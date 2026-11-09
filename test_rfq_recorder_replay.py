"""RFQ trace recorder (tools/rfq_trace_recorder.py) and replay fidelity checker
(tools/rfq_replay_verify.py). MOCK engines and fixtures only (labelled); traces are
produced by the real allocation and deep-check code. No network, no engine database."""

import copy, json, os, random, tempfile, unittest
from types import SimpleNamespace

from rfq_trace import RfqTrace
from tools import rfq_trace_recorder as rec
from tools import rfq_replay_verify as rv
import test_desk_analytics as tda
import test_rfq_trace as ttr


class FakeEngineHttp:
    """Serves /api/identity and /api/rfq-trace from an RfqTrace (MOCK engine)."""

    def __init__(self, trace, pid=1):
        self.trace, self.pid, self.fail = trace, pid, None

    def __call__(self, path):
        if self.fail:
            raise self.fail
        if path == "/api/identity":
            return dict(pid=self.pid, root="X", mode="PAPER")
        q = dict(p.split("=") for p in path.split("?", 1)[1].split("&"))
        return json.loads(json.dumps(self.trace.page(int(q["since_cycle"]),
                                                     int(q["since_event"]))))


def disk(free):
    return lambda path: SimpleNamespace(free=free)


class SinkTests(unittest.TestCase):
    def test_rotation_total_cap_age_and_free_space_floor(self):
        clock = [1_000_000.0]
        with tempfile.TemporaryDirectory() as d:
            s = rec.Sink(d, max_file_mb=0.001, max_total_mb=0.003, keep_days=1,
                         min_free_gb=0, clock=lambda: clock[0], disk_usage=disk(10**12))
            for i in range(40):
                clock[0] += 1
                s.write([dict(type="meta", i=i, pad="x" * 200)])
            files = s.files()
            self.assertGreater(len(files), 1)  # rotated
            self.assertLessEqual(sum(os.path.getsize(f) for f in files), 3000 + 1200)
            # Age limit: everything but the current file goes after keep_days.
            clock[0] += 3 * 86400
            for f in files:
                os.utime(f, (clock[0] - 2 * 86400, clock[0] - 2 * 86400))
            s.enforce()
            self.assertEqual(s.files(), [s.path])
            low = rec.Sink(d, 1, 10, 1, min_free_gb=5, disk_usage=disk(1 * 10**9))
            self.assertFalse(low.write([dict(type="meta")]))
            self.assertTrue(low.paused)
            self.assertEqual(low.dropped, 1)

    def test_engine_data_folder_is_refused(self):
        for p in (r"E:\NeutralBasis\data-five-rh\trace", r"C:\x\paper.sqlite3"):
            with self.assertRaises(SystemExit):
                rec.check_out(p)

    def test_only_loopback_urls(self):
        for u in ("http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765"):
            rec.check_url(u)
        for u in ("http://192.168.1.5:8765", "https://127.0.0.1:8765", "http://example.com"):
            with self.assertRaises(SystemExit):
                rec.check_url(u)


class RedactionTests(unittest.TestCase):
    def test_secret_looking_text_and_keys_are_removed(self):
        x = rec.redact(dict(
            error="401 api_key=abcd1234efgh token: 'zzzzzzzzzzzz' bearer QWERTYUIOPAS",
            raw="{...}", payload=[1], headers={"a": 1}, asset="BTW",
            note="0x" + "ab" * 32 + " and " + "A" * 40,
        ))
        self.assertNotIn("raw", x)
        self.assertNotIn("payload", x)
        self.assertNotIn("headers", x)
        self.assertEqual(x["asset"], "BTW")
        for secret in ("abcd1234efgh", "zzzzzzzzzzzz", "QWERTYUIOPAS", "ab" * 32, "A" * 40):
            self.assertNotIn(secret, json.dumps(x))
        # Ordinary reason codes survive.
        self.assertEqual(rec.redact("RFQ_STREAM_NOT_SUBSCRIBED"), "RFQ_STREAM_NOT_SUBSCRIBED")


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.sink = rec.Sink(self.dir.name, 50, 500, 7, 0, disk_usage=disk(10**12))

    def records(self):
        out = []
        for f in self.sink.files():
            with open(f, encoding="utf8") as h:
                out += [json.loads(l) for l in h]
        return out

    def fill(self, t, n):
        for i in range(n):
            t.begin_cycle(i)
            t.event("connected", "A")
            t.end_cycle({})

    def test_pages_until_caught_up_without_repeats(self):
        t = RfqTrace()
        self.fill(t, 45)  # more than one page (20 cycles)
        r = rec.Recorder("http://127.0.0.1:8765", self.sink, 30, fetch=FakeEngineHttp(t),
                         sleep=lambda s: None)
        r.run(polls=2)
        cyc = [x["cycle_id"] for x in self.records() if x["type"] == "cycle"]
        self.assertEqual(cyc, list(range(1, 46)))
        self.assertEqual((r.stats["lost_cycles"], r.stats["repeated"]), (0, 0))

    def test_ring_overflow_is_reported_as_lost_cycles(self):
        t = RfqTrace(cycles_keep=5, events_keep=5)
        http = FakeEngineHttp(t)
        r = rec.Recorder("http://127.0.0.1:8765", self.sink, 30, fetch=http,
                         sleep=lambda s: None)
        self.fill(t, 3)
        r.run(polls=1)
        self.fill(t, 12)  # 12 new cycles, ring keeps 5: 7 lost before the next read
        r.run(polls=1)
        metas = [x for x in self.records() if x["type"] == "meta" and x["kind"] == "lost_cycles"]
        self.assertEqual(sum(m["count"] for m in metas), 7)
        self.assertEqual(r.stats["lost_cycles"], 7)
        self.assertGreater(r.stats["lost_events"], 0)

    def test_engine_restart_resets_ids_and_is_marked(self):
        t = RfqTrace()
        http = FakeEngineHttp(t, pid=1)
        r = rec.Recorder("http://127.0.0.1:8765", self.sink, 30, fetch=http,
                         sleep=lambda s: None)
        self.fill(t, 3)
        r.run(polls=1)
        http.trace, http.pid = RfqTrace(), 2  # MOCK restart: ids start again at 1
        self.fill(http.trace, 2)
        r.run(polls=1)
        kinds = [x.get("kind") for x in self.records() if x["type"] == "meta"]
        self.assertIn("engine_restart", kinds)
        cyc = [(x["pid"], x["cycle_id"]) for x in self.records() if x["type"] == "cycle"]
        self.assertEqual(cyc, [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2)])

    def test_unreachable_engine_backs_off_and_recovers(self):
        t = RfqTrace()
        self.fill(t, 2)
        http = FakeEngineHttp(t)
        waits = []
        r = rec.Recorder("http://127.0.0.1:8765", self.sink, 30, fetch=http,
                         sleep=waits.append)
        http.fail = OSError("connection refused token=abcdefghijkl")
        r.run(polls=3)
        self.assertEqual(waits, [30.0, 60.0, 120.0])
        http.fail = None
        r.run(polls=1)
        self.assertEqual(waits[-1], 30.0)
        errors = [x for x in self.records() if x.get("kind") == "error"]
        self.assertEqual(len(errors), 3)
        self.assertNotIn("abcdefghijkl", json.dumps(errors))
        self.assertEqual([x["cycle_id"] for x in self.records() if x["type"] == "cycle"],
                         [1, 2])

    def test_non_paper_engine_is_not_recorded(self):
        t = RfqTrace()
        self.fill(t, 1)
        http = FakeEngineHttp(t)
        real = http.__call__
        http_live = lambda p: dict(pid=1, root="X", mode="LIVE") if p == "/api/identity" else real(p)
        r = rec.Recorder("http://127.0.0.1:8765", self.sink, 30, fetch=http_live,
                         sleep=lambda s: None)
        r.run(polls=1)
        self.assertEqual([x for x in self.records() if x["type"] == "cycle"], [])
        self.assertEqual(r.stats["errors"], 1)

    def test_interval_floor(self):
        r = rec.Recorder("http://127.0.0.1:8765", self.sink, 5, fetch=lambda p: {})
        self.assertEqual(r.interval, 30.0)


class ReplayFidelityTests(unittest.TestCase):
    """Traces from the real allocation code (MOCK engine shell, MOCK scores)."""

    def allocation_trace(self, cycles=120, seed=7):
        rng = random.Random(seed)
        t = RfqTrace()
        names = [f"A{i}" for i in range(30)]
        e = ttr.AllocationTraceTests.engine(self, {a: 0 for a in names}, t)
        e.wide_paper.pair_key = lambda c: (c.get("strategy") or "funding", c["asset"],
                                           c["long"], c["short"])
        at = 1000.0
        for k in range(cycles):
            for c in e.wide_paper.candidates:
                c["score"] = rng.uniform(-1, 1)
                c["research"] = ["MOCK araştırma"] if rng.random() < 0.15 else []
            e.wide_paper.rejected = {
                ("funding", a, "Extended", "Hyperliquid"): dict(until=at + rng.choice((-5, 30)),
                                                                group="MOCK bekleme")
                for a in rng.sample(names, 5)
            }
            e.wide_paper.last_cycle = at - 70
            t.begin_cycle(at)
            e.select_rfq_books(at=at)
            t.end_cycle({})
            at += rng.choice((60, 70, 180))
        return t

    def record(self, t):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        sink = rec.Sink(d.name, 50, 500, 7, 0, disk_usage=disk(10**12))
        r = rec.Recorder("http://127.0.0.1:8765", sink, 30, fetch=FakeEngineHttp(t),
                         sleep=lambda s: None)
        r.run(polls=1)
        return sink.files()

    def test_current_scheduler_is_reproduced_on_120_cycles(self):
        files = self.record(self.allocation_trace())
        cycles, _ = rv.load(files)
        res = rv.verify(cycles)
        self.assertEqual(res["complete"], 120)
        self.assertEqual(res["order_match"], 120)
        self.assertEqual(res["chosen_exact"], 120)
        self.assertEqual(res["verdict"], "PASS", res["mismatches"])

    def test_a_tampered_decision_fails(self):
        files = self.record(self.allocation_trace(cycles=110))
        cycles, _ = rv.load(files)
        cycles[50]["allocation"]["chosen"] = cycles[50]["allocation"]["chosen"][:-3]
        res = rv.verify(cycles)
        self.assertEqual(res["verdict"], "FAIL")
        self.assertTrue(any(m["kind"] == "chosen" for m in res["mismatches"]))

    def test_missing_inputs_are_not_counted_as_success(self):
        files = self.record(self.allocation_trace(cycles=105))
        cycles, _ = rv.load(files)
        for c in cycles[:10]:
            c["allocation"].pop("eligible")
        res = rv.verify(cycles)
        self.assertEqual((res["complete"], res["incomplete"]), (95, 10))
        self.assertEqual(res["verdict"], "INSUFFICIENT_DATA")

    def test_fewer_than_100_cycles_is_insufficient(self):
        files = self.record(self.allocation_trace(cycles=40))
        res = rv.verify(rv.load(files)[0])
        self.assertEqual(res["verdict"], "INSUFFICIENT_DATA")


class QuotaReplayTests(unittest.TestCase):
    """The deep-check walk from the real WidePaper loop (MOCK fixtures)."""

    setUp = ttr.DeepCheckTraceTests.setUp
    tearDown = ttr.DeepCheckTraceTests.tearDown
    fund_history = ttr.DeepCheckTraceTests.fund_history
    scenario = ttr.DeepCheckTraceTests.scenario
    outcome = ttr.DeepCheckTraceTests.outcome
    run_cycles = ttr.DeepCheckTraceTests.run_cycles

    def test_recorded_quota_walk_matches_the_rule(self):
        t = RfqTrace()
        self.run_cycles(t)
        for c in t.cycles:
            c = json.loads(json.dumps(c))
            self.assertEqual(c["deep_top"], 1)
            self.assertEqual(rv.walk_quota(c), (True, ""))

    def test_a_tampered_slot_flag_is_detected(self):
        t = RfqTrace()
        self.run_cycles(t)
        c = json.loads(json.dumps(t.cycles[0]))
        for r in c["deep_checks"]:
            if r[5] == "assessed":
                r[6] = not r[6]
        ok, why = rv.walk_quota(c)
        self.assertFalse(ok)
        self.assertTrue(why)


if __name__ == "__main__":
    unittest.main()
