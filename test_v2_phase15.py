"""Phase 15: funding semantics (P0-A), funding expectation models (P0-B), sign / unit
checks (P0-C) and the multi-venue data contract (P1).

Every rate below is MOCK data unless a test says OBSERVED; OBSERVED-data tests skip when
the saved artifacts are absent.
"""
import unittest

from v2 import funding_evidence as fe
from v2 import funding_semantics as fs
from v2 import paths
from v2.control import capability

HAVE_STREAM = any(paths.bench().glob("v2-phase8-rescue-*/stream-*/stream.jsonl"))
HAVE_SNAPSHOT = paths.bench("v2-phase4-2026-11-12", "answers.json.gz").exists()

T0 = 1_794_484_800.0  # 2026-11-12 12:00:00 UTC, a funding boundary (MOCK clock)


class FundingSemanticsTests(unittest.TestCase):
    def test_registry_covers_every_venue_and_never_invents(self):
        for xid in capability.VENUES:
            self.assertIn(xid, fs.VENUES, xid)
            v = fs.VENUES[xid]
            self.assertIn(v["published"]["rate_kind"], fs.RATE_KINDS)
        for xid in ("popdex_perps", "veranta", "phoenix_perps", "qfex"):
            v = fs.VENUES[xid]
            self.assertEqual(v["published"]["rate_kind"], fs.UNKNOWN)
            self.assertIsNone(v["positive_pays"])
            self.assertIsNone(v["interval_s"])

    def test_payment_relation_known_only_where_observed(self):
        known = {x for x in fs.VENUES if fs.payment_relation_known(x)}
        self.assertEqual(known, {"tradexyz", "hyperliquid_native"})
        self.assertFalse(fs.payment_relation_known("entropy_io"))   # inferred only
        self.assertFalse(fs.payment_relation_known("txflow"))
        self.assertFalse(fs.payment_relation_known("variational"))

    def test_txflow_percent_is_converted_once_and_is_a_projection(self):
        # MOCK: nodeCtx.funding 0.0042 (percent per hour), next payment in 10 min
        f = dict(raw=0.0042, unit="percent", interval_s=3600.0, next_ts=T0 + 3600,
                 kind="observed_published")
        o = fs.observation("txflow", "7", f, observed_at=T0 + 3000, evidence="MOCK")
        self.assertAlmostEqual(o["rate_per_hour"], 4.2e-5)
        self.assertEqual(o["settlement_status"], fs.PROJECTED)
        self.assertEqual(o["valid_for"], "CURRENT_PERIOD_ONLY")
        self.assertEqual(o["rate_kind"], fs.RUNNING_PAYMENT_UNKNOWN)
        self.assertIn("PAYMENT_RELATION_UNKNOWN", o["warnings"])
        self.assertEqual(o["evidence"], "MOCK")
        self.assertEqual(o["next_payment_status"], "FUTURE")

    def test_hourly_matches_the_old_hard_coded_txflow_formula(self):
        for raw, iv in ((0.0042, 3600.0), (-0.0016, None), (0.0955, 3600.0)):
            f = dict(raw=raw, unit="percent", interval_s=iv)
            old = fe.per_hour(raw / 100.0, iv or 3600)
            self.assertAlmostEqual(fs.hourly(f, "txflow"), old, places=15)

    def test_interval_from_doc_is_labelled(self):
        o = fs.observation("txflow", "7", dict(raw=0.01, unit="percent", interval_s=None,
                                               next_ts=None), observed_at=T0)
        self.assertEqual(o["interval_source"], "VENUE_DOC")
        self.assertIn("INTERVAL_FROM_DOC", o["warnings"])
        self.assertIn("NEXT_PAYMENT_UNKNOWN", o["warnings"])

    def test_per_interval_rate_is_divided_by_its_own_interval(self):
        # MOCK Variational NATGAS: 0.00288 per 4 h period
        o = fs.observation("variational", "NATGAS-PERP",
                           dict(raw=0.00288, unit="fraction", interval_s=14400.0,
                                next_ts=T0 + 14400), observed_at=T0 + 60)
        self.assertAlmostEqual(o["rate_per_hour"], 0.00072)
        self.assertAlmostEqual(o["rate_per_period"], 0.00288)

    def test_variational_interval_is_never_defaulted(self):
        o = fs.observation("variational", "X", dict(raw=0.001, unit="fraction",
                                                    interval_s=None), observed_at=T0)
        self.assertIsNone(o["rate_per_hour"])
        self.assertIn("INTERVAL_UNKNOWN", o["warnings"])

    def test_next_payment_in_the_past_is_not_used(self):
        # Variational kept next_timestamp at the boundary for up to 285 s (OBSERVED)
        o = fs.observation("variational", "INIT-PERP",
                           dict(raw=-0.0001, unit="fraction", interval_s=3600.0,
                                next_ts=T0), observed_at=T0 + 7)
        self.assertEqual(o["next_payment_status"], "PAST_AT_READ")
        self.assertIsNone(o["next_payment_ts"])
        self.assertEqual(o["next_payment_raw"], T0)
        self.assertIn("NEXT_PAYMENT_IN_PAST", o["warnings"])

    def test_unknown_unit_is_refused_not_guessed(self):
        o = fs.observation("arcus", "BTC", dict(raw=5.0, unit="bips?", interval_s=3600),
                           observed_at=T0)
        self.assertIsNone(o["rate_per_hour"])
        self.assertIn("UNIT_UNKNOWN", o["warnings"])
        o = fs.observation("arcus", "BTC", None, observed_at=T0)
        self.assertIsNone(o["rate_per_hour"])
        self.assertIn("RATE_OR_UNIT_MISSING", o["warnings"])

    def test_lighter_projection_is_labelled_last_settlement(self):
        f = dict(raw=1e-5, unit="fraction", interval_s=3600, next_ts=T0 + 3600,
                 kind="last_settled_projection")
        o = fs.observation("lighter_rh", "1", f, observed_at=T0 + 5)
        self.assertEqual(o["rate_kind"], fs.LAST_SETTLED)
        self.assertEqual(o["settlement_status"], fs.PROJECTED)   # still not a payment
        self.assertIn("LAST_SETTLEMENT_REUSED", o["warnings"])

    def test_settlement_refuses_projections(self):
        self.assertIsNone(fs.settlement("txflow", "7", dict(ts=T0, rate=1e-5,
                                                            kind="observed_published")))
        self.assertIsNone(fs.settlement("lighter_rh", "1", dict(
            ts=T0, rate=1e-5, kind="last_settled_projection")))
        s = fs.settlement("risex", "1", dict(ts=T0, rate=8e-5, kind="settled_public",
                                             interval_s=7200.0))
        self.assertEqual(s["settlement_status"], fs.SETTLED_PUBLIC)
        self.assertEqual(s["valid_for"], "PAST_PERIOD")
        self.assertAlmostEqual(s["rate_per_hour"], 4e-5)
        u = fs.settlement("txflow", "7", dict(ts=T0, rate=None, kind="account_payment"))
        self.assertEqual(u["settlement_status"], fs.ACCOUNT_PAYMENT)
        self.assertIsNone(u["rate_per_hour"])
        self.assertIn("RATE_NOT_PRICED", u["warnings"])

    def test_observation_is_never_a_settlement(self):
        for xid in fs.VENUES:
            o = fs.observation(xid, "m", dict(raw=1e-5, unit="fraction", interval_s=3600,
                                              next_ts=T0 + 10), observed_at=T0)
            self.assertEqual(o["settlement_status"], fs.PROJECTED, xid)
            self.assertEqual(o["valid_for"], "CURRENT_PERIOD_ONLY", xid)

    def test_matrix_rows(self):
        rows = {r["exchange_id"]: r for r in fs.matrix()}
        self.assertEqual(rows["txflow"]["unit"], "percent")
        self.assertIsNone(rows["txflow"]["settled_history"])
        self.assertEqual(rows["variational"]["period_basis"], "per_interval")
        self.assertTrue(rows["tradexyz"]["payment_relation_known"])


class LedgerSettledUnitTests(unittest.TestCase):
    def test_settled_rate_per_period_is_booked_once(self):
        """G5: a 2 h settled point (MOCK) books rate x units x price exactly once."""
        from v2 import paper_engine as pe

        booked = []

        class Ledger:
            def emit(self, kind, **kw):
                booked.append(kw)

        eng = pe.PaperEngine.__new__(pe.PaperEngine)
        eng.ledger = Ledger()
        p = dict(id="p1", asset="X", funding_keys=set())
        leg = dict(interval=7200.0, venue="V", qty=2.0)
        prev = dict(marks=dict(short=100.0), rates=dict(short=None), at=T0 - 100)
        eng.book_payment(p, "short", leg, T0, dict(rate=0.001, interval_s=7200.0), True,
                         prev, None)
        self.assertEqual(len(booked), 1)
        self.assertAlmostEqual(booked[0]["amount"], 2.0 * 100.0 * 0.001)
        self.assertEqual(booked[0]["evidence"], "SETTLED_PUBLIC_RATE")


def _hourly_history(n, start, f, kind="settled_public"):
    """MOCK settled points (rate per hour period) on the hour."""
    return [dict(ts=start + i * 3600.0, rate=f(i), kind=kind) for i in range(n)]


class FundingExpectationTests(unittest.TestCase):
    def setUp(self):
        from v2 import funding_expectation as fx
        self.fx = fx

    def test_baseline_is_the_published_rate_held(self):
        fx = self.fx
        lg = fx.leg("tradexyz", 1e-4, T0 + 11, next_ts=T0 + 3600, side="short")
        p, basis = fx.path(lg, "baseline", 24)
        self.assertEqual(p, [1e-4] * 24)
        self.assertIn("comparison only", basis)

    def test_no_history_no_samples_is_unknown_after_the_current_period(self):
        fx = self.fx
        lo = fx.leg("txflow", 4.2e-5, T0 + 11, next_ts=T0 + 3600, side="long")
        sh = fx.leg("tradexyz", 7.36e-4, T0 + 11, next_ts=T0 + 3600, side="short")
        r = fx.expect(lo, sh, 1000.0, 24)
        self.assertGreater(r["models"]["baseline"]["cash"], 0)
        self.assertIsNone(r["models"]["persistence"]["cash"])        # never defaulted
        self.assertEqual(r["models"]["persistence"]["partial"]["known_hours"], 1)
        self.assertIn("BASELINE_ONLY_POSITIVE", r["flags"])
        self.assertIn("PAYMENT_RELATION_UNKNOWN:txflow", r["flags"])
        self.assertEqual(r["evidence_class"], "ESTIMATED")
        self.assertEqual(r["confidence"], "NONE")

    def test_adverse_is_side_aware(self):
        fx = self.fx
        # MOCK history: rates alternate around 1e-4 so residual quantiles are two-sided
        h = _hourly_history(200, T0 - 200 * 3600, lambda i: 1e-4 + (1e-5 if i % 3 else -2e-5))
        lo = fx.leg("arcus", 1e-4, T0, next_ts=T0 + 3600, history=h, side="long")
        sh = fx.leg("arcus", 1e-4, T0, next_ts=T0 + 3600, history=h, side="short")
        self.assertEqual(lo["fit"]["status"], "OK")
        pl, _ = fx.path(lo, "adverse", 24)
        ps, _ = fx.path(sh, "adverse", 24)
        self.assertGreater(pl[0], ps[0])     # long: pays more; short: receives less
        self.assertAlmostEqual(pl[0] - 1e-4, lo["fit"]["resid_q90"])
        self.assertAlmostEqual(ps[0] - 1e-4, sh["fit"]["resid_q10"])

    def test_persistence_reverts_after_the_current_period(self):
        fx = self.fx
        h = _hourly_history(300, T0 - 300 * 3600, lambda i: 2e-5 * (1 if i % 2 else 0.5))
        lg = fx.leg("lighter_rh", 5e-4, T0 + 1800, next_ts=T0 + 3600, history=h, side="short")
        p, _ = fx.path(lg, "persistence", 24)
        self.assertEqual(p[0], 5e-4)                       # current period: published
        self.assertLess(abs(p[-1] - lg["fit"]["mean"]), abs(p[1] - lg["fit"]["mean"]))

    def test_no_look_ahead_and_projections_refused(self):
        fx = self.fx
        past = _hourly_history(100, T0 - 100 * 3600, lambda i: 1e-4 + 1e-6 * (i % 7))
        future = _hourly_history(50, T0 + 3600, lambda i: 9e-3)
        proj = [dict(ts=T0 - 1800, rate=5e-2, kind="observed_published")]
        a = fx.leg("arcus", 1e-4, T0, history=past, side="long")
        b = fx.leg("arcus", 1e-4, T0, history=past + future + proj, side="long")
        self.assertEqual(a["fit"], b["fit"])

    def test_confidence_never_high_and_capped_without_payment_relation(self):
        fx = self.fx
        h = _hourly_history(24 * 40, T0 - 24 * 40 * 3600, lambda i: 1e-4 * (1 + (i % 5) / 10))
        good = fx.leg("tradexyz", 1e-4, T0, history=h, side="short")
        self.assertEqual(fx.leg_confidence(good), "MEDIUM")
        capped = fx.leg("txflow", 1e-4, T0, history=h, side="long")   # MOCK history
        self.assertEqual(fx.leg_confidence(capped), "LOW")
        r = fx.expect(capped, good, 1000.0, 24)
        self.assertNotIn(r["confidence"], ("HIGH", "VERIFIED"))

    def test_model_nets_replace_only_the_funding_component(self):
        fx = self.fx
        res = dict(models=dict(baseline=dict(cash=16.0), persistence=dict(cash=-3.0),
                               adverse=dict(cash=None)))
        n = fx.model_nets(res, expected_net=4.0, expected_funding=16.0)
        self.assertAlmostEqual(n["baseline"], 4.0)
        self.assertAlmostEqual(n["persistence"], -15.0)
        self.assertIsNone(n["adverse"])

    def test_model_failure_never_changes_the_evaluation(self):
        from unittest import mock
        from v2 import estimated
        cls = next(c for c in vars(estimated).values() if isinstance(c, type) and
                   hasattr(c, "funding_models"))
        obj = cls.__new__(cls)
        obj.cfg = dict(wide_funding_realization=1.0)
        with mock.patch.object(self.fx, "expect", side_effect=RuntimeError("boom")):
            r = obj.funding_models(dict(exchange_id="arcus"), dict(exchange_id="risex"),
                                   dict(rate=1e-5), dict(rate=2e-5), 1000.0, 24, T0, 1.0, 2.0)
        self.assertIn("MODELS_FAILED", r["flags"])
        self.assertIsNone(r["net"])

    @unittest.skipUnless(HAVE_STREAM and paths.bench(
        "v2-aligned-2026-11-12", "aligned.json.gz").exists(), "saved artifacts absent")
    def test_observed_natgas_case_matches_phase14(self):
        """OBSERVED: long TxFlow / short tradeXYZ at 15:00:11 local; baseline +$16.66,
        persistence and adverse negative (docs/v2/41)."""
        from tools import v2_funding_persistence as tp
        from v2.control import netguard
        fx = self.fx
        with netguard.offline() as g:
            rows, later, settled = tp.published_natgas(paths.bench())
            xyz = tp.aligned_histories(paths.bench())["tradexyz NATGAS (90d)"]
        self.assertEqual(len(g), 0)
        ref = next(r for r in rows if r["prev_ft"] and r["t"] >= r["prev_ft"] and
                   r["xyz_rate_h"] - r["txflow_rate_h"] > 0 and r["t"] % 86400 >= 12 * 3600)
        past = [r["txflow_rate_h"] for r in rows if r["t"] <= ref["t"]]
        series = [(t, x) for t, x in xyz if t <= ref["t"]]
        lo = fx.leg("txflow", ref["txflow_rate_h"], ref["t"], next_ts=ref["next_ft"],
                    samples=past, side="long")
        sh = fx.leg("tradexyz", ref["xyz_rate_h"], ref["t"], next_ts=ref["next_ft"],
                    side="short", fit_override=fx.fit(series))
        r = fx.expect(lo, sh, 1000.0, 24)
        self.assertAlmostEqual(r["models"]["baseline"]["cash"], 16.66, places=2)
        self.assertLess(r["models"]["persistence"]["cash"], 0)
        self.assertLess(r["models"]["adverse"]["cash"], r["models"]["persistence"]["cash"])
        self.assertIn("BASELINE_ONLY_POSITIVE", r["flags"])
        self.assertEqual(r["confidence"], "LOW")


class ModelReportingTests(unittest.TestCase):
    ROW = dict(strategy="funding", expected_net=4.99, stress_net=-0.45, long_xid="txflow",
               short_xid="tradexyz", freshness="UNKNOWN", reference=None,
               funding=dict(evidence=["RATE_PUBLISHED", "MARKET_SETTLEMENT_CONFIRMED"]),
               funding_models=dict(net=dict(baseline=4.99, persistence=None, adverse=None),
                                   flags=["BASELINE_ONLY_POSITIVE",
                                          "PAYMENT_RELATION_UNKNOWN:txflow"],
                                   confidence="NONE"))   # MOCK row

    def test_read_model_flags_and_carries_models(self):
        from v2.control import readmodel
        o = readmodel.opportunity(self.ROW, 7)
        self.assertIn("BASELINE_ONLY_POSITIVE", o["flags"])
        self.assertIn("PUBLISHED_PAYMENT_RELATION_UNKNOWN", o["flags"])
        self.assertEqual(o["safety"], "NOT_SAFE")
        self.assertIsNone(o["funding_models"]["net"]["persistence"])   # not zero

    def test_scanner_report_counts_unknown_as_unknown(self):
        from tools import v2_opportunity_scan as sc
        r = sc.models_report([self.ROW, dict(self.ROW, funding_models=None)])
        self.assertEqual(r["rows_with_models"], 1)
        self.assertEqual(r["baseline"]["net_positive"], 1)
        self.assertEqual(r["persistence"]["net_positive"], 0)
        self.assertEqual(r["persistence"]["net_unknown"], 1)
        self.assertEqual(r["baseline_only_positive"], 1)


class SignAndUnitTests(unittest.TestCase):
    """P0-C: a POSITIVE published rate must make the LONG pay on every adapter, end to
    end (adapter -> contract observation -> leg cash flow). Fixtures are MOCK answers
    shaped like the venues' documented / observed public answers (test_v2_phase3)."""

    def assert_long_pays(self, xid, rec_or_funding, per_hour):
        f = rec_or_funding.get("funding", rec_or_funding)
        o = fs.observation(xid, "m", f, observed_at=T0)
        self.assertAlmostEqual(o["rate_per_hour"], per_hour, places=12)
        self.assertEqual(o["positive_pays"], "long")
        self.assertLess(fe.leg_cashflow("long", o["rate_per_hour"]), 0)
        self.assertGreater(fe.leg_cashflow("short", o["rate_per_hour"]), 0)

    def test_every_paper_adapter_positive_means_long_pays(self):
        from v2.venues import arcus, hyperliquid_obs, lighter_rh, risex, tradexyz, txflow,             variational
        a = arcus.parse_markets({"markets": [{
            "marketDisplayName": "BTC-USD", "baseAsset": "BTC", "category": "CRYPTO",
            "type": "PERPETUAL", "status": "ONLINE", "markPrice": "70000",
            "fundingRate": "0.0000125", "nextFundingAt": T0 + 3600}]})[0]
        self.assert_long_pays("arcus", a, 1.25e-5)
        r = risex.parse_markets({"data": {"markets": [{
            "market_id": "1", "config": {"name": "BTC/USDC"}, "category": "crypto",
            "mark_price": "70000", "current_funding_rate": "0.0000048",
            "funding_interval": "3600000000000",
            "next_funding_time": "%d" % int((T0 + 3600) * 1e9)}]}})[0]
        self.assert_long_pays("risex", r, 4.8e-6)
        meta = {"data": {"universe": [{"index": 1, "baseCurrency": "BTC"}]}}
        ctxs = {"1": {"data": {"nodeCtx": {"funding": "0.00125", "markPx": "70000",
                                           "prevFundingTime": T0 * 1000,
                                           "nextFundingTime": (T0 + 3600) * 1000}}}}
        x = txflow.parse_markets(meta, None, ctxs)[0]
        self.assert_long_pays("txflow", x, 1.25e-5)          # percent -> fraction once
        v = variational.parse_markets({"listings": [{
            "ticker": "NATGAS-PERP", "contract_type": "perpetual", "mark_price": "3",
            "funding": {"last": {"interval_rate": "0.00288"}, "interval_seconds": 14400,
                        "next_timestamp": T0 + 14400}}]})[0]
        self.assert_long_pays("variational", v, 0.00072)     # per 4 h -> per hour
        ans = [{"universe": [{"name": "xyz:NATGAS"}]}, [{"funding": "0.000736",
                                                          "markPx": "3"}]]
        self.assert_long_pays("tradexyz", tradexyz.parse_markets(ans)[0], 7.36e-4)
        nat = [{"universe": [{"name": "INIT"}]}, [{"funding": "0.0000125", "markPx": "1"}]]
        self.assert_long_pays("hyperliquid_native",
                              hyperliquid_obs.parse_markets("hyperliquid_native", nat)[0],
                              1.25e-5)
        rows = lighter_rh.parse_fundings({"fundings": [
            {"timestamp": T0, "rate": "0.0012", "direction": "long"}]})
        self.assert_long_pays("lighter_rh", lighter_rh.current_funding(rows), 1.2e-5)

    def test_lighter_short_direction_is_negative(self):
        from v2.venues import lighter_rh
        rows = lighter_rh.parse_fundings({"fundings": [
            {"timestamp": T0, "rate": "0.0030", "direction": "short"},
            {"timestamp": T0 + 3600, "rate": "0.0030", "direction": "sideways"}]})
        self.assertEqual(len(rows), 1)                        # unknown direction dropped
        self.assertAlmostEqual(rows[0]["rate"], -3e-5)

    def test_unit_mistake_would_be_a_fake_carry_and_cannot_happen(self):
        """MOCK: TxFlow 0.00125 % vs tradeXYZ 1.25e-5 fraction are the SAME rate; read
        as a fraction the TxFlow value would invent a 99x carry."""
        tx = fs.observation("txflow", "1", dict(raw=0.00125, unit="percent",
                                                interval_s=3600), observed_at=T0)
        xy = fs.observation("tradexyz", "xyz:X", dict(raw=1.25e-5, unit="fraction",
                                                      interval_s=3600), observed_at=T0)
        self.assertAlmostEqual(fe.pair_carry(tx["rate_per_hour"], xy["rate_per_hour"]), 0.0)
        wrong = fe.pair_carry(0.00125, 1.25e-5)              # the mistake, for scale
        self.assertLess(wrong, -1e-3)

    def test_cross_venue_check_flags_inversion_and_scale(self):
        from tools import v2_funding_semantics_check as ck
        base = {T0 + h * 3600.0: 1e-5 * (1 + (h % 7)) * (1 if h % 11 else -1)
                for h in range(100)}                          # MOCK settled series
        self.assertEqual(ck.compare(base, dict(base))["flags"], [])
        inv = {t: -r for t, r in base.items()}
        self.assertIn("SIGN_INVERSION_SUSPECT", ck.compare(base, inv)["flags"])
        x100 = {t: r * 100 for t, r in base.items()}
        self.assertIn("SCALE_SUSPECT", ck.compare(x100, base)["flags"])
        self.assertIsNone(ck.compare(base, {T0: 1e-5}))      # too few common hours

    @unittest.skipUnless(HAVE_STREAM, "saved artifacts absent")
    def test_observed_boundary_convergence(self):
        from tools import v2_funding_semantics_check as ck
        from v2.control import netguard
        with netguard.offline() as g:
            rows = ck.boundary_convergence(paths.bench())
            var = ck.variational_fields(paths.bench())
        self.assertEqual(len(g), 0)
        verdict = {r["venue"]: r["verdict"] for r in rows}
        self.assertEqual(verdict.get("tradexyz"), "CONVERGES")
        self.assertEqual(verdict.get("hyperliquid_native"), "CONVERGES")
        self.assertEqual(verdict.get("txflow"), "NO_SETTLED_HISTORY")
        self.assertEqual(var["last_ne_next"], 0)
        self.assertGreater(var["next_timestamp_past_reads"], 0)


if __name__ == "__main__":
    unittest.main()
