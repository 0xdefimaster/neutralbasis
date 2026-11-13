"""Phase 15 P6: replay and failure injection. Every scenario of
tools/v2_failure_harness.py must PASS (MOCK inputs into the real modules); the fill
guard must not change any decision on the recorded stream (OBSERVED, skipped when the
saved stream is absent)."""
import shutil
import tempfile
import unittest
from pathlib import Path

from tools import v2_failure_harness as h
from v2 import paths
from v2.control import netguard

STREAMS = sorted(paths.bench().glob("v2-phase8-rescue-*/stream-*/stream.jsonl"))


class FailureInjectionTests(unittest.TestCase):
    def test_every_scenario_passes_offline(self):
        with netguard.offline() as g:
            res = h.run_all()
        self.assertEqual(len(g), 0)
        self.assertEqual(len(res), 17)
        for r in res:
            with self.subTest(r["name"]):
                self.assertEqual(r["status"], "PASS", r)

    def test_the_unguarded_engine_really_filled_a_wrong_price(self):
        r = h.sc_wrong_price_fill()["detail"]["cases"]
        self.assertAlmostEqual(r["no_guard/scale_x1000"]["price"], 2997.0)   # old behaviour
        self.assertFalse(r["guarded/scale_x1000"]["filled"])
        self.assertTrue(r["guarded/control_ok"]["filled"])

    def test_rfq_fill_price_is_guarded_too(self):
        import wide_paper
        from v2 import paper_engine, paper_ledger as L
        d = Path(tempfile.mkdtemp(prefix="nb-p6-"))
        try:
            led = L.PaperLedger(d / "led")
            p = h.partial_position(led)
            eng = paper_engine.PaperEngine(led, dict(wide_paper.WIDE_DEFAULTS), [])
            self.assertIsNone(eng.fill_guard(p, "short", h.FakeObs(h.T0, {}), price=3.1))
            self.assertIn("PRICE_SCALE_SUSPECT",
                          eng.fill_guard(p, "short", h.FakeObs(h.T0, {}), price=30.0))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipUnless(STREAMS, "saved stream absent")
    def test_guard_never_refuses_a_fill_on_the_recorded_stream(self):
        """OBSERVED: the ESTIMATED replay of the rescued Phase 8 stream books no
        FILL_GUARD refusal (the guard only stops pathological books)."""
        from tools import v2_paper_replay as rp
        d = Path(tempfile.mkdtemp(prefix="nb-p6-replay-"))
        try:
            with netguard.offline() as g:
                obs, _ = rp.load([], [STREAMS[0]], rp.closes_db())
                rp.run(d / "estimated", obs, rp.NATGAS_PAIRS, False,
                       recovery_timeout_s=60.0)
            self.assertEqual(len(g), 0)
            text = (d / "estimated" / "ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn('"kind":"fill"', text)
            self.assertNotIn("FILL_GUARD", text)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
