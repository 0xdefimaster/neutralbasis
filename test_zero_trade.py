"""Zero-trade diagnostics (zero_trade.py) on the paper desk. MOCK fixtures only
(test_wide_paper's FakeSource); nothing here is market evidence."""

import ast
import os
import re
import unittest

import capabilities
import test_wide_paper as twp
import zero_trade

HERE = os.path.dirname(os.path.abspath(__file__))


def literal_reasons():
    """Every reason text the desk and the capability gate write (format templates
    with their placeholders filled by a sample)."""
    out = set()
    for name in ("wide_paper.py", "capabilities.py"):
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            target = getattr(node.func.value, "id", "") or getattr(
                node.func.value, "attr", ""
            )
            if node.func.attr != "append" or target not in ("why", "reasons", "stale"):
                continue
            a = node.args[0] if node.args else None
            if isinstance(a, ast.BinOp) and isinstance(a.left, ast.Constant):
                a = a.left
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                out.add(re.sub(r"%[-+ 0-9.]*[a-z]", "1", a.value))
    # The venue gate: "<venue>: <capability label> yok|doğrulanmadı".
    for need in capabilities.REQUIRED.values():
        for c in need:
            for word in ("yok", "doğrulanmadı"):
                out.add("Hyperliquid: %s %s" % (capabilities.LABELS[c], word))
    return sorted(out)


class CodeTests(unittest.TestCase):
    def test_every_engine_reason_has_a_code(self):
        unmatched = [r for r in literal_reasons() if zero_trade.code_of(r)[0] == "OTHER"]
        # The generic "%s: %s %s" template itself is covered by the labels above.
        unmatched = [r for r in unmatched if r not in ("1: 1 1",)]
        self.assertEqual(unmatched, [])

    def test_codes_of_the_gate_and_the_economics(self):
        cases = {
            "Hyperliquid: Funding işareti doğrulanmadı": "FUNDING_SIGN_UNVERIFIED",
            "Extended: İşlem fiyatı geçmişi doğrulanmadı": "INSUFFICIENT_HISTORY",
            "Lighter: Gerçekleşmiş funding doğrulanmadı": "FUNDING_HISTORY_UNVERIFIED",
            "Paradex: Kontrat bilgisi doğrulanmadı": "CONTRACT_SPEC_UNKNOWN",
            "RISEx: Ücret doğrulanmadı": "FEE_UNKNOWN",
            "Funding geliri ücret + kayma + spread riski payını karşılamıyor · x":
                "NET_EXPECTED_PNL_NEGATIVE",
            "Hyperliquid: girişte defter eski": "STALE_ORDERBOOK",
            "Hyperliquid: girişte defter derinliği yetersiz · 1 / 2":
                "INSUFFICIENT_LIQUIDITY",
            "Günün kalan kayıp bütçesi yetmiyor": "DRAWDOWN_LIMIT",
            "Paper emirleri doldurmadı: x": "EXECUTION_FAILED",
        }
        for text, code in cases.items():
            self.assertEqual(zero_trade.code_of(text)[0], code, text)

    def test_a_cycle_record_round_trips_and_ages_out(self):
        d = zero_trade.Diagnostics()
        d.start(1000, dict(enabled=True, funding=True, convergence=True))
        d.set("markets_discovered", 5)
        d.note(dict(strategy="funding", asset="BTC", long="A", short="B", spread=0.001,
                    score=0.002), 1000, "research",
               ["A: Funding işareti doğrulanmadı"], "evidence")
        d.finish(1001)
        e = zero_trade.Diagnostics(d.state())
        self.assertEqual(e.window(1, 1100)["totals"]["markets_discovered"], 5)
        self.assertEqual(e.rejected[0]["reason_codes"], ["FUNDING_SIGN_UNVERIFIED"])
        self.assertEqual(e.rejected[0]["signed_spread_bp"], 10.0)
        e.start(1000 + 25 * 3600, {})
        e.finish(1000 + 25 * 3600)
        self.assertEqual(len(e.cycles), 1)  # the day-old one left

    def test_a_silent_engine_is_reported_as_stopped(self):
        d = zero_trade.Diagnostics()
        self.assertEqual(d.verdict(1000)["key"], "STOPPED")
        d.start(1000, dict(enabled=True, funding=True, convergence=True))
        d.set("markets_discovered", 3)
        d.set("data_quality_passed", 3)
        d.finish(1000)
        self.assertNotEqual(d.verdict(1100)["key"], "STOPPED")
        self.assertEqual(d.verdict(1000 + 3600)["key"], "STOPPED")

    def test_missing_stages_stay_unknown_not_zero(self):
        d = zero_trade.Diagnostics()
        d.start(1000, {})
        d.finish(1000)
        c = d.cycles[0]["counts"]
        self.assertIsNone(c["combined_candidates"])
        self.assertIsNone(c["markets_discovered"])


def fixture(cls, *sources):
    """Borrow the fixture helpers (not the tests) of test_wide_paper's classes."""
    for src in sources:
        for name, value in vars(src).items():
            if not name.startswith("test") and callable(value) and not hasattr(cls, name):
                setattr(cls, name, value)
    return cls


class OpenedCycleTests(unittest.TestCase):
    setUp = twp.WidePaperTests.setUp
    tearDown = twp.WidePaperTests.tearDown

    def test_an_opened_pair_is_counted_through_every_stage(self):
        self.setup_pair()
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1)
        c = self.desk.diag.cycles[-1]["counts"]
        self.assertGreater(c["markets_discovered"], 0)
        self.assertGreater(c["instruments_matched"], 0)
        self.assertGreater(c["economic_candidates"], 0)
        self.assertGreaterEqual(c["simulation_passed"], 1)
        self.assertEqual(c["risk_approved"], 1)
        self.assertEqual(c["paper_orders_created"], 1)
        self.assertEqual(c["positions_opened"], 1)
        r = self.desk.diag.report(self.desk.diag.cycles[-1]["at"])
        self.assertEqual(r["verdict"]["key"], "TRADING")
        self.assertIn("why_no_trade", self.desk.snapshot())

    def test_diagnostics_survive_a_restart(self):
        self.setup_pair()
        self.desk.cycle()
        saved = self.store.get("wide_zero_trade", {})
        self.assertEqual(len(saved["cycles"]), 1)

    def test_a_failed_cycle_is_still_recorded(self):
        self.setup_pair()
        self.src.market_rows = lambda: (_ for _ in ()).throw(RuntimeError("ağ"))
        self.desk.cycle()
        x = self.desk.diag.cycles[-1]
        self.assertIn("ağ", x["error"])
        self.assertIsNone(x["counts"]["markets_discovered"])


fixture(OpenedCycleTests, twp.WidePaperTests)


class GateCycleTests(unittest.TestCase):
    setUp = twp.CapabilityGateTests.setUp
    tearDown = twp.WidePaperTests.tearDown

    def test_evidence_blocked_pairs_explain_the_zero_trade(self):
        self.setup_pair()
        self.desk.cycle()
        self.assertEqual(self.desk.positions, [])
        x = self.desk.diag.cycles[-1]
        self.assertEqual(x["counts"]["positions_opened"], 0)
        self.assertGreaterEqual(x["counts"]["evidence_blocked"], 1)
        self.assertEqual(x["counts"]["paper_orders_created"], 0)
        self.assertIn("FUNDING_SIGN_UNVERIFIED", x["codes"])
        v = self.desk.diag.verdict(x["at"])
        self.assertEqual(v["key"], "DATA")
        rej = self.desk.diag.rejected[-1]
        self.assertEqual(rej["decision"], "research")
        self.assertEqual((rej["long"], rej["short"]), ("Hyperliquid", "LighterCore"))
        self.assertIsNotNone(rej["score_per_usd"])


fixture(GateCycleTests, twp.CapabilityGateTests, twp.WidePaperTests)


if __name__ == "__main__":
    unittest.main()
