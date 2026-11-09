"""RFQ allocation / deep-check trace (rfq_trace.py): bounded, sanitized, read-only, and
decision-neutral. MOCK fixtures only (labelled); no exchange connection."""

import copy, json, socket, threading, time, unittest, urllib.request, urllib.error
from types import SimpleNamespace
from unittest import mock

import bot
import extended_rfq
import rfq_trace
from rfq_trace import RfqTrace, book_state
import wide_paper
# Helpers are reached through their modules: binding TestCase classes here would make
# the loader run those suites a second time.
import test_wide_paper as twp
import test_desk_analytics as tda
from test_extended_rfq import LocalWsServer, ws_frame, msg

T0 = twp.T0


class RingBufferTests(unittest.TestCase):
    def test_cycles_and_events_are_bounded(self):
        t = RfqTrace(cycles_keep=3, events_keep=5)
        for i in range(10):
            t.begin_cycle(i)
            t.event("connected", "A")
            t.end_cycle({})
        self.assertEqual([c["cycle_id"] for c in t.cycles], [8, 9, 10])
        self.assertEqual(len(t.events), 5)

    def test_only_whitelisted_short_fields_are_kept(self):
        t = RfqTrace()
        t.event("first_snapshot", "BTW", seq=7, error="x" * 1000,
                raw='{"payload": "secret"}', api_key="k", bids=[(1, 2)])
        t.event("not_a_kind", "BTW")
        e = list(t.events)
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["seq"], 7)
        self.assertEqual(len(e[0]["error"]), rfq_trace.TEXT_MAX)
        for k in ("raw", "api_key", "bids"):
            self.assertNotIn(k, e[0])

    def test_book_state_keeps_no_levels(self):
        s = dict(connected=True, ready=True, seq=3, age=1.0, bid_levels=5,
                 best_bid=10.0, error="e", market="X", snapshots=2)
        b = book_state(s)
        self.assertEqual(b["state"], "ready")
        self.assertNotIn("best_bid", b)
        self.assertNotIn("bid_levels", b)
        self.assertEqual(book_state(None), dict(state="not_subscribed"))
        self.assertEqual(book_state(dict(connected=False))["state"], "connecting")
        self.assertEqual(book_state(dict(connected=True, ready=False))["state"], "warming")
        self.assertEqual(book_state(dict(connected=True, ready=True, age=999))["state"],
                         "stale")

    def test_paging_and_size_limit(self):
        t = RfqTrace()
        for i in range(30):
            t.begin_cycle(i)
            t.end_cycle({"A%d" % j: dict(state="ready") for j in range(50)})
        p = t.page(since_cycle=0, cycles=500)
        self.assertEqual(len(p["cycles"]), rfq_trace.PAGE_CYCLES_MAX)
        self.assertEqual(p["next_cycle"], 20)
        p2 = t.page(since_cycle=p["next_cycle"])
        self.assertEqual([c["cycle_id"] for c in p2["cycles"]], list(range(21, 31)))
        small = t.page(max_bytes=20_000)
        self.assertTrue(small["truncated"])
        self.assertLessEqual(len(json.dumps(small)), 20_000)
        self.assertEqual(small["mode"], "PAPER")

    def test_disabled_trace_records_nothing(self):
        t = RfqTrace(enabled=False)
        t.begin_cycle(1)
        t.note_deep("funding", 0, dict(asset="A"), "assessed", True, None)
        t.event("connected", "A")
        t.end_cycle({})
        self.assertEqual((len(t.cycles), len(t.events)), (0, 0))


class AllocationTraceTests(unittest.TestCase):
    """DeskEngine.select_rfq_books with and without the trace (MOCK engine)."""

    def engine(self, scores, trace=None):
        e = tda.RfqBookSelectionTests.engine(self, scores)
        for c in e.wide_paper.candidates:
            # As pair_fields() sets them on real candidates.
            c["rfq_long"] = c["long"] == "Extended"
            c["rfq_short"] = c["short"] == "Extended"
        e.wide_paper.last_cycle = 900.0
        if trace is not None:
            e.rfq_trace = trace
        return e

    def steps(self, e):
        out = []
        for at, bump in ((1000, None), (1070, ("A12", "A13")), (1601, None)):
            if bump:
                for a in bump:
                    e.wide_paper.candidates[int(a[1:])]["score"] = 1000
            out.append((sorted(e.select_rfq_books(at=at)), dict(e.rfq_since)))
        return out

    def test_decisions_are_identical_with_and_without_trace(self):
        scores = {f"A{i}": 100 - i for i in range(14)}
        # A rejected pair (waiting) and a blocked one, so priority keys differ.
        plain = self.engine(scores)
        traced = self.engine(scores, RfqTrace())
        for e in (plain, traced):
            e.wide_paper.rejected = {("funding", "A1", "Extended", "Hyperliquid"):
                                     dict(until=10_000, group="RFQ akışı")}
            e.wide_paper.pair_key = lambda c: (c.get("strategy") or "funding", c["asset"],
                                               c["long"], c["short"])
        traced.rfq_trace.begin_cycle(1000)
        self.assertEqual(self.steps(plain), self.steps(traced))

    def test_allocation_records_ranking_source_reasons_and_events(self):
        t = RfqTrace()
        e = self.engine({f"A{i}": 100 - i for i in range(14)}, t)
        t.begin_cycle(1000)
        e.select_rfq_books(at=1000)
        t.end_cycle({})
        c = t.cycles[-1]
        self.assertEqual(c["ranking_at"], 900.0)  # the list from the previous cycle
        alloc = c["allocation"]
        self.assertEqual(alloc["cap"], 12)
        self.assertEqual(len(alloc["chosen"]), 12)
        self.assertEqual(set(alloc["added"].values()), {"ranked"})
        self.assertEqual(len(c["rfq_candidates"]), 14)
        first = c["rfq_candidates"][0]
        self.assertEqual(first[0], "funding")
        self.assertEqual([x["kind"] for x in t.events].count("subscribe"), 12)
        # Next cycle: two newcomers outrank two young streams; youth keeps them.
        t.begin_cycle(1070)
        for a in ("A12", "A13"):
            e.wide_paper.candidates[int(a[1:])]["score"] = 1000
        e.select_rfq_books(at=1070)
        t.end_cycle({})
        kept = t.cycles[-1]["allocation"]["kept"]
        self.assertEqual(sorted(a for a, r in kept.items() if r == "young_kept"),
                         ["A10", "A11"])


class DeepCheckTraceTests(unittest.TestCase):
    """WidePaper deep-check loop with and without the trace (MOCK fixtures)."""

    setUp = twp.WidePaperTests.setUp
    tearDown = twp.WidePaperTests.tearDown
    fund_history = twp.WidePaperTests.fund_history

    def scenario(self):
        twp.RfqSplitTests.setup_btw(self)
        # MOCK: the Extended leg has its history, so the deep check reaches its book.
        self.src.history[("Extended", "BTC")] = twp.candles(T0)
        self.fund_history("Extended", -0.0005, 3600)
        # MOCK: Extended RFQ book not subscribed in the engine (EngineSource would raise).

        def no_stream(v, a, _real=FakeBook(self.src)):
            if v == "Extended":
                raise ValueError("RFQ akışı bu piyasa için başlatılmadı")
            return _real(v, a)

        self.src.book = no_stream
        self.desk.cfg["wide_deep_top"] = 1

    def outcome(self):
        return dict(
            assessed=[(x["strategy"], x["long"], x["short"], x["eligible"],
                       tuple(x["reasons"])) for x in self.desk.assessed],
            rejected={k: (v["until"], v["group"]) for k, v in self.desk.rejected.items()},
            positions=[(p["asset"], p["decision"]["long"], p["decision"]["short"])
                       for p in self.desk.positions],
            legs_down=dict(self.desk.legs_down),
        )

    def run_cycles(self, trace):
        self.scenario()
        if trace is not None:
            self.desk.rfq_trace = trace
        out = []
        for _ in range(2):
            if trace is not None:
                trace.begin_cycle(self.clock)
            self.desk.cycle()
            if trace is not None:
                trace.end_cycle({})
            out.append(self.outcome())
            # Inside the 60 s RFQ wait, so the second round sees the pair waiting.
            self.clock += 30
        return out

    def test_decisions_are_identical_with_and_without_trace(self):
        plain = self.run_cycles(None)
        # Fresh desk and store for the traced run.
        self.tearDown()
        self.setUp()
        traced = self.run_cycles(RfqTrace())
        self.assertEqual(plain, traced)

    def test_quota_use_reason_and_book_state_are_recorded(self):
        t = RfqTrace()
        self.run_cycles(t)
        first = t.cycles[0]
        self.assertEqual(first["deep_ranking_at"], T0)
        rows = [r for r in first["deep_checks"] if r[0] == "funding"]
        rfq = next(r for r in rows if r[5] == "assessed" and "Extended" in (r[3], r[4]))
        # (strategy, order, asset, long, short, action, slot_consumed, book, outcome)
        self.assertTrue(rfq[6])  # a missing RFQ stream consumed the deep-check slot
        self.assertEqual(rfq[7], ("not_subscribed",))
        self.assertIn("RFQ_STREAM_NOT_SUBSCRIBED", rfq[8])
        # The slot was used up: the next pair in this round stops on the quota.
        self.assertTrue(any(r[5] == "stop_quota" for r in rows))
        # Next cycle the RFQ leg is down: the pair is skipped without a slot.
        second = [r for r in t.cycles[1]["deep_checks"] if r[0] == "funding"]
        self.assertTrue(any(r[5] in ("skip_leg_down", "skip_rejected_waiting")
                            and not r[6] for r in second))

    def test_recorded_slot_use_matches_the_engine_rule(self):
        # MOCK: no price history for any leg, so some checks fail on history.
        t = RfqTrace()
        self.scenario()
        self.src.book = FakeBook(self.src)

        def no_history(v, a):
            # MOCK: EngineSource's own message when the keeper has no candles yet.
            raise ValueError("İşlem mumu yok; arka plan geçmiş kuyruğu bekleniyor")

        self.src.prices = no_history
        self.desk.cfg["wide_deep_top"] = 3
        self.desk.rfq_trace = t
        t.begin_cycle(self.clock)
        self.desk.cycle()
        t.end_cycle({})
        rows = [r for r in t.cycles[0]["deep_checks"] if r[5] == "assessed"]
        self.assertEqual(len(rows), len(self.desk.assessed))
        for r, x in zip(rows, self.desk.assessed):
            self.assertEqual((r[3], r[4]), (x["long"], x["short"]))
            engine_rule = not any(
                wide_paper.reason_group(m) == "Geçmiş eksik" for m in x["reasons"]
            )
            self.assertEqual(r[6], engine_rule)
        self.assertTrue(any(not r[6] for r in rows))  # history-only rejects are free

    def test_a_failing_trace_never_changes_or_stops_the_round(self):
        plain = self.run_cycles(None)
        self.tearDown()
        self.setUp()
        broken = RfqTrace()

        def boom(*a, **k):
            raise RuntimeError("MOCK trace failure")

        broken.note_deep = boom
        broken.note_deep_ranking = boom
        self.desk.trace_book = boom
        traced = self.run_cycles(broken)
        self.assertEqual(plain, traced)
        self.assertGreater(broken.errors, 0)
        self.assertIn("MOCK trace failure", broken.last_error)


class FakeBook:
    def __init__(self, src):
        self.src = src

    def __call__(self, v, a):
        b = self.src.books[(v, a)]
        return dict(b, received=bot.now())


class StreamLifecycleTraceTests(unittest.TestCase):
    """Stream events through a local MOCK WebSocket server; single socket owner kept."""

    def setUp(self):
        for name, value in (("POLL_SECONDS", 0.05), ("RECONNECT_MIN", 0)):
            p = mock.patch.object(extended_rfq, name, value)
            p.start()
            self.addCleanup(p.stop)

    def wait(self, cond, seconds=5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.01)
        return False

    def test_connected_first_snapshot_gap_and_retire_without_cross_thread_socket_use(self):
        def script(c):
            c.sendall(ws_frame(0x1, json.dumps(msg("SNAPSHOT", 1, [(10, 5)], [(11, 3)])).encode()))
            c.sendall(ws_frame(0x1, json.dumps(msg("DELTA", 2, [(10, 1)])).encode()))
            c.sendall(ws_frame(0x1, json.dumps(msg("DELTA", 5, [(10, 1)])).encode()))  # gap

        # Every reconnect hits the same gap; enough connections that a reconnect never
        # waits on a closed listener.
        server = LocalWsServer(script, connections=50)
        self.addCleanup(server.close)
        t = RfqTrace()
        s = extended_rfq.RfqStream("BTW-USD", trace=t, asset="BTW")
        s.open = lambda: extended_rfq.WebSocket(server.url, timeout=5, poll=0.05,
                                                cancelled=s.cancelled)
        owners = []
        real = extended_rfq.WebSocket.check_owner

        def spy(ws):
            owners.append(threading.get_ident())
            return real(ws)

        with mock.patch.object(extended_rfq.WebSocket, "check_owner", spy):
            s.start()
            self.assertTrue(self.wait(lambda: any(e["kind"] == "gap" for e in t.events)))
            s.retire()
            self.assertTrue(s.join(8))
        kinds = [e["kind"] for e in t.events]
        self.assertEqual(kinds[:3], ["connected", "first_snapshot", "gap"])
        fs = next(e for e in t.events if e["kind"] == "first_snapshot")
        self.assertEqual((fs["asset"], fs["seq"]), ("BTW", 1))
        self.assertNotIn("lost", kinds[kinds.index("gap") + 1:])  # retire adds no "lost"
        self.assertIsNotNone(s.status()["subscribed_at"])
        self.assertIsNotNone(s.status()["first_snapshot_at"])
        self.assertEqual(set(owners), {s.thread.ident})
        for e in t.events:
            self.assertNotIn("payload", e)


class ResourceTests(unittest.TestCase):
    def test_trace_never_touches_the_store_lock(self):
        import os, tempfile

        with tempfile.TemporaryDirectory() as d:
            store = bot.Store(os.path.join(d, "s.sqlite3"))
            try:
                before = store.lock.report()["acquisitions"]
                t = RfqTrace()
                for i in range(50):
                    t.begin_cycle(i)
                    t.note_deep("funding", 0, dict(asset="A"), "assessed", True,
                                dict(state="ready"), ["X"])
                    t.event("connected", "A")
                    t.end_cycle({"A": dict(state="ready")})
                t.page()
                self.assertEqual(store.lock.report()["acquisitions"], before)
            finally:
                store.db.close()


class TraceEndpointTests(unittest.TestCase):
    """GET /api/rfq-trace on a real local server (MOCK engine)."""

    def setUp(self):
        from test_hosted_serve import StubEngine, free_port

        self.port = free_port()
        self.engine = StubEngine()
        self.engine.rfq_trace = RfqTrace()
        self.engine.rfq_trace.begin_cycle(1)
        self.engine.rfq_trace.end_cycle({})
        self.server = threading.Thread(
            target=bot.serve, args=(self.engine, self.port, "0.0.0.0", "s3cret"),
            daemon=True)
        self.server.start()
        for _ in range(50):
            try:
                self.get("127.0.0.1", "/healthz")
                break
            except OSError:
                time.sleep(0.05)

    def tearDown(self):
        html = self.get("127.0.0.1", "/")[1]
        token = html.split("window.paperToken='")[1].split("'")[0]
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/shutdown", data=b"{}",
            headers=self.headers({"X-Paper-Token": token}))
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except urllib.error.HTTPError:
            pass
        self.server.join(5)

    def headers(self, extra=None):
        import base64

        h = {"Host": "bot.up.railway.app",
             "Authorization": "Basic " + base64.b64encode(b"paper:s3cret").decode()}
        h.update(extra or {})
        return h

    def get(self, host, path):
        req = urllib.request.Request(f"http://{host}:{self.port}{path}",
                                     headers=self.headers())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as ex:
            return ex.code, ex.read().decode()

    def test_loopback_read_is_paged_and_paper(self):
        status, body = self.get("127.0.0.1", "/api/rfq-trace?since_cycle=0&cycles=5")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual((d["mode"], len(d["cycles"]), d["next_cycle"]), ("PAPER", 1, 1))
        status, body = self.get("127.0.0.1", "/api/rfq-trace?since_cycle=1")
        self.assertEqual(json.loads(body)["cycles"], [])
        # Bad parameters fall back to defaults rather than failing.
        self.assertEqual(self.get("127.0.0.1", "/api/rfq-trace?cycles=x")[0], 200)

    def test_non_loopback_client_is_refused(self):
        try:
            addrs = [a for a in socket.gethostbyname_ex(socket.gethostname())[2]
                     if not a.startswith("127.")]
        except OSError:
            addrs = []
        if not addrs:
            self.skipTest("Bu makinede loopback dışı IPv4 adresi yok")
        status, _ = self.get(addrs[0], "/api/rfq-trace")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
