"""Phase 15 P3/P5: channel scheduler and channel view. MOCK capability rows, MOCK
clock; no request leaves the machine."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from v2 import scheduler as sch

T0 = 1_794_484_800.0


def cap(level):
    return dict(level=level)


MATRIX = {  # MOCK capability rows (shapes of v2.control.capability.matrix)
    "txflow": dict(capabilities=dict(rest=cap("LIVE_OBSERVED"), websocket=cap("LIVE_OBSERVED"),
                                     orderbook=cap("LIVE_OBSERVED"),
                                     public_trades=cap("DOCUMENTED"),
                                     funding_current=cap("LIVE_OBSERVED"),
                                     funding_history=cap("UNSUPPORTED"))),
    "arcus": dict(capabilities=dict(rest=cap("LIVE_OBSERVED"), websocket=cap("UNVERIFIED"),
                                    orderbook=cap("LIVE_OBSERVED"),
                                    public_trades=cap("DOCUMENTED"),
                                    funding_current=cap("LIVE_OBSERVED"),
                                    funding_history=cap("LIVE_OBSERVED"))),
    "variational": dict(capabilities=dict(rest=cap("LIVE_OBSERVED"),
                                          websocket=cap("UNSUPPORTED"),
                                          orderbook=cap("LIVE_OBSERVED"),
                                          public_trades=cap("UNVERIFIED"),
                                          funding_current=cap("LIVE_OBSERVED"),
                                          funding_history=cap("UNSUPPORTED"))),
    "veranta": dict(capabilities=dict(rest=cap("UNVERIFIED"), websocket=cap("UNVERIFIED"),
                                      orderbook=cap("UNVERIFIED"))),
}


class PlanTests(unittest.TestCase):
    def test_plan_never_assumes_a_capability(self):
        p = sch.plan(MATRIX)
        self.assertEqual(p[("txflow", "orderbook")]["mode"], sch.WS)
        self.assertEqual(p[("arcus", "orderbook")]["mode"], sch.REST)
        self.assertEqual(p[("variational", "orderbook")]["mode"], sch.DISABLED)
        self.assertEqual(p[("variational", "rfq_quote")]["mode"], sch.REST)
        for x in MATRIX:
            self.assertEqual(p[(x, "public_trades")]["mode"], sch.DISABLED)
        self.assertEqual(p[("txflow", "funding_history")]["mode"], sch.DISABLED)
        self.assertEqual(p[("arcus", "funding_history")]["mode"], sch.REST)
        self.assertTrue(all(v["mode"] == sch.DISABLED for (x, c), v in p.items()
                            if x == "veranta"))


class WsFallbackTests(unittest.TestCase):
    def test_ws_first_rest_fallback_and_hysteresis(self):
        s = sch.Scheduler(MATRIX)
        ob = s.channels[("txflow", "orderbook", "*")]
        age = {"v": 1.0}
        due = [d for d in s.tick(T0, lambda v, k: age["v"]) if d[:2] == ("txflow", "orderbook")]
        self.assertEqual(due[0][3], "WS_VERIFICATION")          # first: one REST check
        ob.record(T0, {"b": 1}, True, "WS_VERIFICATION")
        due = [d for d in s.tick(T0 + 5, lambda v, k: age["v"]) if d[:2] == ("txflow",
                                                                              "orderbook")]
        self.assertEqual(due, [])                                # WS fresh: no duplicate
        age["v"] = 30.0                                          # WS stale (limit 10 s)
        due = [d for d in s.tick(T0 + 6, lambda v, k: age["v"]) if d[:2] == ("txflow",
                                                                              "orderbook")]
        self.assertEqual((ob.mode, due[0][3]), (sch.REST_FALLBACK, "REST_FALLBACK"))
        age["v"] = 1.0
        for i in range(sch.RECOVER_AFTER - 1):
            s.tick(T0 + 7 + i, lambda v, k: age["v"])
            self.assertEqual(ob.mode, sch.REST_FALLBACK)          # not yet: hysteresis
        s.tick(T0 + 20, lambda v, k: age["v"])
        self.assertEqual(ob.mode, sch.WS)
        self.assertEqual(ob.mode_changes, 2)

    def test_no_ws_feed_means_rest_at_once(self):
        s = sch.Scheduler(MATRIX)
        due = [d for d in s.tick(T0) if d[:2] == ("txflow", "orderbook")]
        self.assertEqual(due[0][3], "REST_FALLBACK")


class AdaptiveTests(unittest.TestCase):
    def test_request_rate_follows_publication_not_faster(self):
        c = sch.Channel("variational", "rfq_quote", "*", sch.REST, "", "LIVE_OBSERVED",
                        sch.stale_limit_for("variational", "rfq_quote"))
        self.assertEqual(c.rest_interval(), 15.0)               # nothing observed yet
        t = T0
        for i in range(10):                                     # quotes change every 42 s
            for k in range(3):
                c.record(t + k * 14, {"q": i}, True)
            t += 42
        self.assertAlmostEqual(c.publication_cadence(), 42.0)
        self.assertAlmostEqual(c.rest_interval(), 21.0)          # half the cadence
        slow = sch.Channel("arcus", "funding_current", "*", sch.REST, "", "", 3600.0)
        for i in range(5):
            slow.record(T0 + i * 3600, {"r": i}, True)          # changes hourly
        self.assertEqual(slow.rest_interval(), 300.0)           # clamped to rest_max_s
        fast_book = sch.Channel("arcus", "orderbook", "*", sch.REST, "", "", 15.0)
        for i in range(5):
            fast_book.record(T0 + i * 60, {"b": i}, True)       # changes once a minute
        self.assertEqual(fast_book.rest_interval(), 7.5)        # never > stale limit / 2

    def test_error_rate_and_status(self):
        c = sch.Channel("arcus", "orderbook", "*", sch.REST, "", "", 15.0)
        c.record(T0, {"b": 1}, True)
        c.record(T0 + 5, None, False)
        v = c.view(T0 + 20)
        self.assertEqual((v["status"], v["error_rate"]), ("STALE", 0.5))
        self.assertEqual(c.view(T0 + 1)["status"], "FRESH")


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="nb-p3-"))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_incremental_backfill_with_a_durable_cursor(self):
        cp = self.d / "cursors.json"
        s = sch.Scheduler(MATRIX, cursor_path=cp)
        calls = []

        def fetch(k):
            calls.append(k)
            return [dict(ts=T0 - 7200, rate=1e-5), dict(ts=T0 - 3600, rate=1e-5)]

        f = {("arcus", "funding_history"): fetch}
        s.run_due(f, now=T0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(cp.read_text())["arcus|*"], T0 - 3600)
        s.run_due(f, now=T0 + 60)                    # before next settlement + 5 min
        self.assertEqual(len(calls), 1)
        s2 = sch.Scheduler(MATRIX, cursor_path=cp)   # restart: the cursor survives
        s2.run_due(f, now=T0 + 300)
        self.assertEqual(len(calls), 2)              # cursor + 1 h + 5 min reached
        st = sch.CursorStore(cp)
        self.assertFalse(st.advance("arcus|*", T0 - 7200))     # never moves back

    def test_errors_are_counted_never_raised(self):
        s = sch.Scheduler(MATRIX)

        def boom(k):
            raise TimeoutError("MOCK")

        done = s.run_due({("arcus", "orderbook"): boom}, now=T0)
        self.assertIn(("arcus", "orderbook", "*", "REST_POLL", "ERROR TimeoutError"), done)
        self.assertEqual(s.errors["arcus/orderbook"], 1)


class ChannelViewTests(unittest.TestCase):
    def test_view_is_policy_only_without_a_producer_and_observed_with_one(self):
        from v2.control import readmodel
        d = Path(tempfile.mkdtemp(prefix="nb-p5-"))
        try:
            v = readmodel.channel_views(MATRIX, T0, folder=d)
            ob = next(c for c in v["txflow"] if c["channel"] == "orderbook")
            self.assertEqual((ob["status"], ob["data_source"]), ("NOT_RUNNING", "POLICY_ONLY"))
            (d / "heartbeat-recorder.json").write_text(json.dumps(dict(at=T0 - 5, channels=[
                dict(exchange_id="txflow", channel="orderbook", key="*", mode="WS",
                     status="FRESH", data_change_age_s=0.4)])))
            v = readmodel.channel_views(MATRIX, T0, folder=d)
            ob = next(c for c in v["txflow"] if c["channel"] == "orderbook")
            self.assertEqual((ob["status"], ob["data_source"]), ("FRESH", "OBSERVED"))
            self.assertAlmostEqual(ob["heartbeat_age_s"], 5.0)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
