"""Phase 15 P4: data quality and recovery - silent callback failures, bounded
cardinality, state vs metric eviction, Doctor integration. All frames are MOCK."""
import json
import shutil
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path

from v2 import bounded, ws_readonly
from v2.control import doctor, telemetry

T0 = 1_794_484_800.0


def hl_frame(coin, t_ms, px=10.0):
    return json.dumps({"channel": "l2Book", "data": {
        "coin": coin, "time": t_ms, "levels": [[{"px": str(px - 0.1), "sz": "1"}],
                                               [{"px": str(px + 0.1), "sz": "1"}]]}})


class CallbackErrorTests(unittest.TestCase):
    def test_failing_hook_never_stops_data_and_is_never_silent(self):
        calls = []

        def hook(venue, r):
            calls.append(r)
            raise ValueError("boom api_key=SECRET123 at https://x.io/ws?token=abc "
                             + "0x" + "ab" * 32)

        b = ws_readonly.Books(clock=lambda: T0, on_record=hook)
        r = b.handle("hyperliquid", hl_frame("INIT", T0 * 1000))
        self.assertEqual(r["status"], "ok")                 # the data path went on
        self.assertIn("INIT", b.snap)                       # and the state was kept
        h = b.health(now=T0 + 1)
        cb = h["callback"]
        self.assertEqual((cb["total"], cb["in_window"], cb["status"]), (1, 1, "DEGRADED"))
        self.assertEqual(cb["top_sources"][0]["source"], ["hyperliquid", "l2Book", "INIT"])
        self.assertEqual(cb["last_at"], T0)
        self.assertFalse(h["telemetry_complete"])
        self.assertFalse(h["integrity_verifiable"])
        for leak in ("SECRET123", "token=abc", "ab" * 32):
            self.assertNotIn(leak, cb["last_error"])
        self.assertTrue(cb["last_error"].startswith("ValueError: boom"))
        later = b.health(now=T0 + 400)["callback"]          # outside the 5 min window
        self.assertEqual((later["status"], later["in_window"], later["total"]),
                         ("OK", 0, 1))
        self.assertFalse(later["telemetry_complete"])       # still incomplete for the run

    def test_clean_hook_reports_complete(self):
        tel = telemetry.Telemetry(clock=lambda: T0)
        b = ws_readonly.Books(clock=lambda: T0)
        b.on_record = telemetry.books_hook(tel, b)
        b.handle("hyperliquid", hl_frame("INIT", T0 * 1000))
        h = b.health()
        self.assertTrue(h["telemetry_complete"])
        self.assertEqual(h["callback"]["status"], "OK")
        self.assertEqual(tel.keys.stats()["keys"], 1)


class CardinalityTests(unittest.TestCase):
    def flood(self, b, n, venue="txflow"):
        for i in range(n):          # metrics only: activeAssetCtx-like records, no book
            b.record(venue=venue, channel="probe", market="m%d" % i, status="ok",
                     source_ts=None)

    def test_metrics_are_bounded_and_venue_totals_never_shrink(self):
        b = ws_readonly.Books(clock=lambda: T0, max_metric_markets=100, max_total_keys=400)
        self.flood(b, 10000)
        self.assertLessEqual(len(b.per_market), 100)
        self.assertLessEqual(len(b.totals), 401)
        self.assertEqual(b.per_market.evictions, 9900)
        self.assertEqual(b.total("txflow"), 10000)          # folded, not lost
        self.assertEqual(b.total("txflow", market="m9999"), 1)   # newest kept

    def test_active_subscriptions_are_protected(self):
        b = ws_readonly.Books(clock=lambda: T0, max_metric_markets=50, max_total_keys=200)
        for i, c in enumerate(("A", "B", "C")):
            b.handle("hyperliquid", hl_frame(c, (T0 + i) * 1000))
        self.flood(b, 5000, venue="hyperliquid")
        for c in ("A", "B", "C"):
            self.assertIn(("hyperliquid", c), b.per_market)  # metrics of live books kept
            self.assertIn(c, b.snap)                         # state never touched
            self.assertEqual(b.total("hyperliquid", market=c, status="ok"), 1)

    def test_state_is_never_evicted_by_size(self):
        b = ws_readonly.Books(clock=lambda: T0)
        b.MAX_STATE_MARKETS = 3
        for i, c in enumerate(("A", "B", "C", "D")):
            r = b.handle("hyperliquid", hl_frame(c, (T0 + i) * 1000))
        self.assertEqual(r["status"], "state_limit")         # D refused, not stored
        self.assertEqual(sorted(b.snap), ["A", "B", "C"])     # A..C intact
        self.assertEqual(b.health()["state"]["refused_new"], 1)
        r = b.handle("hyperliquid", hl_frame("A", (T0 + 10) * 1000, px=11.0))
        self.assertEqual(r["status"], "ok")                   # existing books still update
        self.assertAlmostEqual(b.snap["A"]["bids"][0][0], 10.9)

    def test_drop_market_is_the_only_state_removal(self):
        b = ws_readonly.Books(clock=lambda: T0)
        b.handle("hyperliquid", hl_frame("A", T0 * 1000))
        b.handle("hyperliquid", hl_frame("B", T0 * 1000))
        self.assertTrue(b.drop_market("hyperliquid", "A"))
        self.assertNotIn("A", b.snap)
        self.assertIn("B", b.snap)
        self.assertNotIn(("hyperliquid", "A"), b.per_market)
        self.assertEqual(b.total("hyperliquid", status="ok"), 2)   # folded into venue total
        self.assertFalse(b.drop_market("hyperliquid", "Z"))

    def test_dedupe_survives_a_rotation(self):
        b = ws_readonly.Books(clock=lambda: T0)
        b.DEDUPE_GENERATION = 3
        for k in range(5):
            self.assertFalse(b.dedupe(("k", k)))
        self.assertEqual(b.seen_rotations, 1)
        self.assertTrue(b.dedupe(("k", 4)))       # newest generation
        self.assertTrue(b.dedupe(("k", 1)))       # previous generation still caught

    def test_telemetry_keys_bounded_with_subscription_protection(self):
        live = {("v", "keep%d" % i) for i in range(5)}
        tel = telemetry.Telemetry(clock=lambda: T0, max_keys=50,
                                  active=lambda v, m, c: (v, m) in live)
        for v, m in live:
            tel.event(v, m, "ch", "message")
        for i in range(1000):
            tel.event("v", "x%d" % i, "ch", "message")
        self.assertLessEqual(len(tel.keys), 50)
        self.assertEqual(tel.meta()["evictions"], 955)
        for v, m in live:
            self.assertIn((v, m, "ch"), tel.keys)
        self.assertEqual(len(tel.snapshot()), len(tel.keys))

    def test_memory_bounded_vs_unbounded(self):
        """High-cardinality stress: 30k distinct markets. The bounded configuration
        must stay far below the unbounded one (measured with tracemalloc)."""
        def peak(**kw):
            tracemalloc.start()
            b = ws_readonly.Books(clock=lambda: T0, **kw)
            tel = telemetry.Telemetry(clock=lambda: T0,
                                      max_keys=kw.get("max_metric_markets") or None)
            b.on_record = telemetry.books_hook(tel, b)
            for i in range(30000):
                b.record(venue="txflow", channel="probe", market="m%d" % i, status="ok",
                         source_ts=T0 - 0.2)
            _, pk = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            return pk, b, tel

        ws_readonly.Books.MAX_METRIC_MARKETS, saved = 10 ** 9, ws_readonly.Books.MAX_METRIC_MARKETS
        try:
            unb, _, _ = peak(max_metric_markets=10 ** 9, max_total_keys=10 ** 9)
        finally:
            ws_readonly.Books.MAX_METRIC_MARKETS = saved
        bnd, b, tel = peak(max_metric_markets=2000, max_total_keys=20000)
        self.assertLess(bnd, unb / 2)
        self.assertLessEqual(len(b.per_market), 2000)
        self.assertLessEqual(len(tel.keys), 2000)
        self.measured = dict(unbounded_mb=unb / 2 ** 20, bounded_mb=bnd / 2 ** 20)


class DoctorIngestTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="nb-p4-"))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def hb(self, name, ingest=None, tel=None):
        (self.d / ("heartbeat-%s.json" % name)).write_text(json.dumps(dict(
            at=T0, every_s=15, ingest=ingest, telemetry=tel)), encoding="utf-8")

    def test_doctor_warns_on_callback_errors_and_cardinality(self):
        bad = bounded.CallbackErrors()
        bad.add(RuntimeError("x"), now=T0 - 10, source=("txflow", "l2BookDepth", "7"))
        old = bounded.CallbackErrors()
        old.add(RuntimeError("y"), now=T0 - 3600)
        ok = bounded.CallbackErrors()
        m = dict(per_market=dict(keys=10, max_keys=2000, evictions=0, over_cap=0),
                 totals=dict(keys=10, max_keys=20000, evictions=5, over_cap=0))
        self.hb("rec_bad", dict(callback=bad.summary(T0), metrics=m, state={}))
        self.hb("rec_old", dict(callback=old.summary(T0), metrics=m, state={}))
        self.hb("rec_ok", dict(callback=ok.summary(T0), metrics=m,
                               state=dict(refused_new=2)),
                tel=dict(keys=3, max_keys=5000, evictions=0, over_cap=0))
        recs = {r["channel"]: r for r in doctor.ingest_health(T0, folder=self.d)}
        self.assertEqual(recs["telemetry:rec_bad"]["health_status"], "DEGRADED")
        self.assertIn("RuntimeError: x", recs["telemetry:rec_bad"]["health_reason"])
        self.assertEqual(recs["telemetry:rec_old"]["health_status"], "UNVERIFIED")
        self.assertEqual(recs["telemetry:rec_ok"]["health_status"], "HEALTHY")
        self.assertEqual(recs["cardinality:rec_ok"]["health_status"], "DEGRADED")  # refused
        self.assertEqual(recs["cardinality:rec_bad"]["health_status"], "HEALTHY")
        self.assertEqual(recs["telemetry_keys:rec_ok"]["health_status"], "HEALTHY")

    def test_probe_integrity_is_unverified_when_telemetry_incomplete(self):
        rep = self.d / "v2-paper-data" / "ws-probe-x"
        rep.mkdir(parents=True)
        cb = bounded.CallbackErrors()
        cb.add(KeyError("k"), now=T0)
        res = dict(txflow=dict(resyncs=0, stale=0, duplicates=0, rest_vs_ws={},
                               messages={"ok": 10},
                               ingest=dict(callback=cb.summary(T0), telemetry_complete=False)),
                   lighter_rh=dict(resyncs=0, stale=0, duplicates=0, rest_vs_ws={},
                                   messages={"ok": 10},
                                   ingest=dict(callback=bounded.CallbackErrors().summary(T0),
                                               telemetry_complete=True)),
                   hyperliquid=dict(resyncs=0, messages={"ok": 1}))
        (rep / "ws_probe_report.json").write_text(json.dumps(dict(results=res,
                                                                  created=T0)))
        recs = {r["exchange_id"]: r for r in doctor.integrity(T0, bench=self.d)}
        self.assertEqual(recs["txflow"]["health_status"], "UNVERIFIED")
        self.assertIn("TELEMETRY INCOMPLETE", recs["txflow"]["health_reason"])
        self.assertEqual(recs["lighter_rh"]["health_status"], "HISTORICAL_ONLY")
        self.assertIn("not recorded", recs["hyperliquid"]["health_reason"])


if __name__ == "__main__":
    unittest.main()
