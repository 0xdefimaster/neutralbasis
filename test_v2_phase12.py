"""Phase 12 prep: Hyperliquid fast-mode allowlist and probe (MOCK), Variational freshness
analysis (MOCK), and guards that must not move: Variational stays
EXECUTION_MODEL_UNVERIFIED, the 2 s freshness floor stays, TxFlow activeAssetCtx funding
is never an account payment.

All frames and answers here are MOCK; no network is used.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_v2_phase10 as t10
from v2 import gates, observation, opportunity_log, storage, ws_readonly
from v2 import funding_evidence as fe


class FastModeAllowlistTests(unittest.TestCase):
    def test_fast_boolean_only_on_hyperliquid_l2book(self):
        ok = {"method": "subscribe", "subscription": {"type": "l2Book", "coin": "INIT",
                                                      "fast": True}}
        self.assertIsNone(ws_readonly.check_frame("hyperliquid", ok))
        bad = json.loads(json.dumps(ok))
        bad["subscription"]["fast"] = "yes"
        self.assertIsNotNone(ws_readonly.check_frame("hyperliquid", bad))
        other = {"method": "subscribe", "subscription": {"type": "trades", "coin": "INIT",
                                                         "fast": True}}
        self.assertIsNotNone(ws_readonly.check_frame("hyperliquid", other))
        tx = {"method": "subscribe", "type": "l2Book",
              "subscription": {"type": "l2Book", "coin": "119", "fast": True}}
        self.assertIsNotNone(ws_readonly.check_frame("txflow", tx))


def hl(coin, t, levels=5, bid=1.0):
    return json.dumps({"channel": "l2Book", "data": {"coin": coin, "time": t, "levels": [
        [{"px": str(bid - i * 0.01), "sz": "10", "n": 1} for i in range(levels)],
        [{"px": str(bid + 0.01 + i * 0.01), "sz": "10", "n": 1} for i in range(levels)]]}})


class HlFastProbeTests(unittest.TestCase):
    def test_refuses_without_clock_verification(self):
        from tools import v2_hl_fast_probe as hp

        for argv in (["x", "--out", "C:\\tmp\\h", "--clock-offset", "0", "--owner-approved"],
                     ["x", "--out", "C:\\tmp\\h", "--clock-offset", "0", "--clock-verified"]):
            with mock.patch("sys.argv", argv), \
                    mock.patch.dict(os.environ, {ws_readonly.ENV: "1"}):
                with self.assertRaises(SystemExit):
                    hp.main()

    def test_mock_mode_run_and_summary(self):
        from tools import v2_hl_fast_probe as hp

        now = [1_000.0]
        frames = []
        for k in range(6):                                     # a push every 2 s
            frames.append(hl("INIT", int((1_000 + 2 * k) * 1000), levels=5))
            frames.append(hl("xyz:NATGAS", int((1_000 + 2 * k) * 1000), levels=5, bid=3.6))

        class Conn(t10.FakeConn):
            def recv_or_tick(self):
                now[0] += 1.0
                return self.frames.pop(0) if self.frames else None

        mode = hp.run_mode(True, 30, connect=lambda u: Conn(list(frames)),
                           clock=lambda: now[0], sleep=lambda s: None, offset_s=0.0)
        self.assertEqual(mode["seconds"], 30.0)
        rest = [dict(coin="INIT", recv=1_004.5, src=1_004.0, bid=1.0, ask=1.01),
                dict(coin="INIT", recv=1_009.0, src=1_008.5, bid=1.0, ask=1.01)]
        s = hp.summarize(mode, rest)
        c = s["coins"]["INIT"]
        self.assertEqual(c["pushes"], 6)
        self.assertAlmostEqual(c["interval_s"]["median"], 2.0)
        self.assertEqual(c["levels"]["bids_median"], 5)
        self.assertEqual(c["same_time_price_diff_bp"], [0.0])  # only the same-time sample
        self.assertIsNotNone(c["share_age_le_2s"])
        self.assertEqual(hp.run_mode(True, 999, connect=lambda u: Conn([]),
                                     clock=lambda: now[0], sleep=lambda s: None)["seconds"],
                         hp.MAX_PER_MODE_S)


class VariationalFreshnessTests(unittest.TestCase):
    def test_analysis_counts_and_best_case(self):
        from tools import v2_variational_freshness as vf

        d = Path(tempfile.mkdtemp(prefix="nb-p12-"))
        try:
            log = storage.ChainLog(d / "s.jsonl")
            for k, (t1, upd) in enumerate([(100, "1970-01-01T00:01:39Z"),
                                           (120, "1970-01-01T00:01:39Z"),
                                           (140, "1970-01-01T00:02:19Z")]):
                log.append(dict(kind="cycle", n=k, answers={"variational_stats": dict(
                    ok=True, t0=t1 - 1, t1=t1, data={"listings": [
                        {"ticker": "INIT-PERP", "quotes": {"updated_at": upd}}]})}))
            log.append(dict(kind="cycle", n=3, answers={"variational_stats": dict(
                ok=False, t0=1, t1=2, error="RateLimited: HTTP 429")}))
            r = vf.analyse([d / "s.jsonl"])
            x = r["listings"]["INIT-PERP"]
            self.assertEqual((x["reads"], x["distinct_quotes"], x["repeated_reads"]), (3, 2, 1))
            self.assertEqual(x["update_interval_s"]["median"], 40.0)
            self.assertAlmostEqual(x["best_case_share_of_time_le_2s"], 2 / 40)
            self.assertEqual(r["http_429"], 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class GuardsThatMustNotMove(unittest.TestCase):
    def test_variational_stays_blocked_and_the_floor_stays(self):
        self.assertIn("Variational", gates.RFQ_VENUES)
        self.assertEqual(observation.VENUE_MATRIX["variational"]["status"],
                         "BLOCKED_EXECUTION_MODEL")
        self.assertEqual(opportunity_log.FLOOR_S, 2.0)
        self.assertEqual(opportunity_log.freshness_limit(0.05), (2.0, "FLOOR"))

    def test_txflow_ctx_funding_is_never_an_account_payment(self):
        b = ws_readonly.Books(lambda: 1_794_500_001.0)
        b.handle("txflow", json.dumps({"channel": "activeAssetCtx", "data": {
            "coin": "119", "ctx": {"funding": "-0.00156476", "blockTime": 1_794_500_000_000}}}))
        ctx = b.ctx[("txflow", "119")]
        self.assertNotIn("kind", ctx)                           # raw market context only
        self.assertEqual(fe.VENUE_BEST["txflow"], fe.RATE_PUBLISHED)
        self.assertNotEqual(fe.kind_status("observed_published"),
                            fe.kind_status("account_payment"))


if __name__ == "__main__":
    unittest.main()
