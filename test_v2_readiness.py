"""Production-readiness audit (after Phase 15): regressions for real faults found while
auditing. MOCK inputs only; no network, no clock or service change."""
import os
import subprocess
import unittest
from unittest import mock

from v2.control import doctor


class ClockStatusTests(unittest.TestCase):
    def run_with(self, stdout, stderr=b"", code=0):
        cp = subprocess.CompletedProcess(["w32tm"], code, stdout=stdout, stderr=stderr)
        with mock.patch.object(doctor.subprocess, "run", return_value=cp), \
                mock.patch.object(doctor.os, "name", "nt"):
            return doctor.clock_status()

    def test_stopped_time_service_in_a_local_code_page_never_crashes(self):
        """2026-11-14: w32tm printed Turkish OEM text (byte 0x8d) with the Windows Time
        service stopped; decoding raised in a reader thread, stdout became None and the
        whole Doctor crashed."""
        r = self.run_with(b"The following error occurred: Hizmet ba\x8dlat\x8dlmad\x8d. "
                          b"(0x80070426)\r\n", code=2147943462)
        self.assertEqual(r["health_status"], "UNVERIFIED")
        self.assertEqual(r["clock_confidence"], "NONE")
        self.assertIn("0x80070426", r["health_reason"])

    def test_synced_state_is_still_only_unverified_offline(self):
        r = self.run_with(b"Leap Indicator: 0\r\nLast Successful Sync Time: 14.11.2026 "
                          b"09:00:00\r\n")
        self.assertEqual(r["health_status"], "UNVERIFIED")       # offset needs NTP
        self.assertEqual(r["clock_confidence"], "UNKNOWN_OFFSET")

    def test_decoder_never_raises(self):
        for b in (b"\x8d\x9f\xff\xfe", b"", None, "ok".encode("utf-16")):
            self.assertIsInstance(doctor.decode_console(b), str)


T0 = 1_794_484_800.0


class TimeBasisSwitchTests(unittest.TestCase):
    """Owner-approved switch to the answer time basis: the nine checks of the
    2026-11-14 directive, MOCK data, real Collector / source / engine code."""

    def setUp(self):
        from v2 import marketdata
        self.md = marketdata
        p = mock.patch.object(marketdata, "TIME_BASIS", marketdata.ANSWER)
        p.start()
        self.addCleanup(p.stop)

    def collector(self, now, stream=None):
        from v2.collector import Collector
        col = Collector(transport=lambda *a, **k: {"MOCK": 1}, clock=lambda: now[0],
                        mono=lambda: now[0])
        col.by_key = {("Arcus", "BTC"): dict(exchange_id="arcus", market_id="BTC-USD",
                                             multiplier=1.0),
                      ("TxFlow", "BTC"): dict(exchange_id="txflow", market_id="1",
                                              multiplier=1.0)}
        if stream:
            col.streams["txflow"] = stream
        return col

    def test_1_cache_hit_is_never_re_presented_as_fresh(self):
        now = [1000.0]
        col = self.collector(now)
        with mock.patch("v2.venues.arcus.parse_book",
                        return_value=dict(bids=[(1.0, 1.0)], asks=[(2.0, 1.0)])):
            col.book("Arcus", "BTC")
            now[0] = 1004.9                                  # still a cache hit
            b = col.book("Arcus", "BTC")
        self.assertEqual(b["received"], 1000.0)
        self.assertTrue(self.md.stale(b, "arcus", 1015.5))    # 15.5 s since the fetch
        self.assertFalse(self.md.stale(b, "arcus", 1014.0))

    def test_2_unknown_fetch_time_is_never_fresh(self):
        from v2 import source
        self.assertTrue(self.md.stale(dict(received=None, answer_received=None),
                                      "arcus", T0))
        self.assertIsNone(source.row_time(T0, None))
        rows, _, reasons = self.build_one(times=lambda x, m: (None, None))
        self.assertEqual(rows, [])
        self.assertIn(source.ANSWER_TIME_UNKNOWN, dict(reasons["arcus"]))
        rows, _, _ = self.build_one(times=lambda x, m: (T0 - 30, T0 - 500))
        self.assertEqual((rows[0]["received"], rows[0]["funding_answer_received"]),
                         (T0 - 30, T0 - 500))

    def build_one(self, times):
        from v2 import source
        from v2.identity import Instrument

        def rec(xid, legacy, symbol, price):
            return dict(instrument=Instrument(xid, symbol, symbol, asset_class="crypto",
                                              price=price, class_source="mock"),
                        legacy_venue=legacy, mark=price, index=None, fee_taker=0.0004,
                        funding=dict(raw=0.00001, unit="fraction", interval_s=3600))
        recs = {"arcus": [rec("arcus", "Arcus", "PEPE-USD", 0.000012)],
                "risex": [rec("risex", "RISEx", "PEPE-USD", 0.0000121)]}
        rows, g, reasons = source.build(recs, at=T0, times=times)
        return [r for r in rows if r["exchange_id"] == "arcus"], g, reasons

    def test_3_ws_message_time_and_rest_fetch_time_are_not_mixed(self):
        now = [1000.0]

        class Stream:
            def book(self, mid):
                return dict(bids=[(1.0, 1.0)], asks=[(2.0, 1.0)], ts=999.0,
                            received=998.5, model="orderbook")
        col = self.collector(now, stream=Stream())
        b = col.book("TxFlow", "BTC")
        self.assertEqual(b["received"], 998.5)               # the WS arrival time
        self.assertNotIn("answer_received", b)               # never a REST fetch time
        self.assertEqual(self.md.book_time(b), 998.5)

    def test_4_source_fetch_read_and_evaluation_times_kept_apart(self):
        now = [1000.0]
        col = self.collector(now)
        with mock.patch("v2.venues.arcus.parse_book",
                        return_value=dict(bids=[(1.0, 1.0)], asks=[(2.0, 1.0)], ts=999.2)):
            col.book("Arcus", "BTC")
            now[0] = 1003.0
            b = col.book("Arcus", "BTC")
        v = self.md.stale_views(b, "arcus", 1004.0)          # evaluation time
        self.assertEqual((b["ts"], b["answer_received"], b["read_at"]),
                         (999.2, 1000.0, 1003.0))
        self.assertAlmostEqual(v["source_age_s"], 4.8)
        self.assertAlmostEqual(v["answer_age_s"], 4.0)
        self.assertAlmostEqual(v["legacy_age_s"], 4.0)       # received = fetch now

    def test_5_source_clock_skew_is_stated(self):
        now = [1000.0]
        col = self.collector(now)
        with mock.patch("v2.venues.arcus.parse_book",
                        return_value=dict(bids=[(1.0, 1.0)], asks=[(2.0, 1.0)], ts=1030.0)):
            b = col.book("Arcus", "BTC")
        self.assertTrue(b["time_flags"][0].startswith("SOURCE_AHEAD_OF_FETCH"))

    def test_6_funding_rows_use_the_rates_own_answer_time(self):
        from v2.engine_source import V2EngineSource
        now = [2000.0]
        col = self.collector(now)
        col.cache["rh:markets"] = (1990.0, {}, 0.0)
        col.cache["rh:fund:3"] = (1500.0, {}, 0.0)            # rate answer 500 s old
        self.assertEqual(col.answer_times("lighter_rh", "3"), (1990.0, 1500.0))
        col.cache["tx:ctx:7"] = (1995.0, {}, 0.0)
        self.assertEqual(col.answer_times("txflow", "7"), (1995.0, 1995.0))
        src = V2EngineSource.__new__(V2EngineSource)
        c = mock.Mock()
        c.rows = [dict(venue="Lighter", asset="X", received=1990.0,
                       funding_answer_received=1500.0),
                  dict(venue="Lighter", asset="Y", received=1990.0,
                       funding_answer_received=None)]
        c.reasons = {}
        src.collector, src.clock, src.last, src.refresh_every = c, lambda: 2000.0, 1999.0, 60
        src.excluded = {}
        rows = src.market_rows()
        self.assertEqual(rows[0]["funding_received"], 1500.0)   # not the price time
        self.assertEqual(rows[1]["funding_received"], float("-inf"))  # unknown: never fresh

    def test_7_and_8_strict_and_estimated_difference_and_never_looser(self):
        from tools import v2_time_basis_sim as sim
        b = sim.books(delays=(0.0, 6.0))
        for d, res in b.items():
            for v, r in res.items():
                self.assertGreaterEqual(r["answer_stale"], r["legacy_stale"], (d, v))
        self.assertEqual(b["gate_delay_0s"]["txflow"]["flips"], 0)
        self.assertEqual(b["gate_delay_6s"]["txflow"]["flips"], 600)
        f = sim.funding()
        self.assertEqual(f["share_over_limit"]["legacy"], 0.0)
        self.assertGreater(f["share_over_limit"]["answer"], 0.5)

    def test_9_offline_replays_and_rows_use_no_future_time(self):
        from v2 import source
        for fetched in (T0 - 5, T0):
            self.assertLessEqual(source.row_time(T0, fetched), T0)
        b = dict(received=T0 + 3600, answer_received=T0 + 3600)   # a future time
        self.assertTrue(self.md.stale(b, "arcus", T0))


class SectionLabelTests(unittest.TestCase):
    """Panel labels: a running recorder never makes an old scan look live."""

    def build(self, scan_age_s, recorder="RUNNING"):
        import json
        import shutil
        import tempfile
        from pathlib import Path
        from v2.control import readmodel
        d = Path(tempfile.mkdtemp(prefix="nb-rm-"))
        self.addCleanup(shutil.rmtree, d, True)
        now = 2_000_000_000.0
        scan = dict(at=now - scan_age_s, counts={}, DISCOVERY={}, ECONOMIC={},
                    EXECUTION_READINESS={}, _path="MOCK")
        with mock.patch.object(readmodel, "load_scan", return_value=(scan, [])), \
                mock.patch.object(readmodel.telemetry, "read_heartbeat",
                                  return_value=dict(status=recorder, age_s=5.0)):
            readmodel.build(now=now, out=d / "rm")
        return {n: json.loads((d / "rm" / (n + ".json")).read_text())
                for n in ("overview", "opportunities", "paper_positions")}

    def test_old_scan_is_historical_even_with_a_running_recorder(self):
        r = self.build(scan_age_s=7200)
        self.assertEqual(r["overview"]["data_mode"], "LIVE_OBSERVED")      # recorder runs
        self.assertEqual(r["opportunities"]["data_mode"], "HISTORICAL")    # old scan
        self.assertIn("2.0 h old", r["opportunities"]["banner"])
        self.assertEqual(r["paper_positions"]["data_mode"], "SIMULATION")

    def test_fresh_scan_with_a_running_recorder_is_live(self):
        r = self.build(scan_age_s=60)
        self.assertEqual(r["opportunities"]["data_mode"], "LIVE_OBSERVED")
        self.assertIsNone(r["opportunities"]["banner"])

    def test_no_recorder_is_historical_everywhere(self):
        r = self.build(scan_age_s=60, recorder="NOT_RUNNING")
        self.assertEqual(r["overview"]["data_mode"], "HISTORICAL")
        self.assertEqual(r["opportunities"]["data_mode"], "HISTORICAL")


if __name__ == "__main__":
    unittest.main()
