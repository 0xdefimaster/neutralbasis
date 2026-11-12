"""NATGAS/INIT forensics, observation_v3 universe and network mode, funding evidence,
the opportunity funnel and the radar (master prompt 2026-11-12, phases B-F).

Data labels: rows and books marked OBSERVED_0512 are copied from the 05:12 TR phase-4
snapshot (answers.json.gz sha256 f1ff9f2a..., top two book levels only). Values marked
SCREENSHOT_UNVERIFIED come from docs/v2/fixtures/arbsh_screenshots_2026-11-12.json.
Everything else is MOCK test data.
"""
import json
import os
import unittest
from pathlib import Path

from pilot import network_guard
from v2 import dashboard, funnel, gates, network_policy as v2np, observation, policy
from v2 import funding_evidence as fe
from v2.venues import hyperliquid_obs as hlo
from v2.venues import variational

HERE = Path(__file__).resolve().parent
SHOTS = json.loads((HERE / "docs" / "v2" / "fixtures" /
                    "arbsh_screenshots_2026-11-12.json").read_text(encoding="utf-8"))
HL = "api.hyperliquid.xyz"


def body(**kw):
    return json.dumps(kw).encode()


# OBSERVED_0512: NATGAS rows and books at the 05:12 snapshot.
def natgas_rows():
    base = dict(asset="NATGAS", kind="perp", next=None, received=0.0, leverage=None,
                price_type="mark", fine_class="commodity", underlying_id="rwa:NATGAS",
                multiplier=1.0)
    rows = {
        ("tradeXYZ", "NATGAS"): dict(base, venue="tradeXYZ", exchange_id="tradexyz",
                                     market_id="xyz:NATGAS", mark=3.6402,
                                     rate=0.0003778109, raw_rate=0.0003778109,
                                     interval=3600, fee=9e-05),
        ("TxFlow", "NATGAS"): dict(base, venue="TxFlow", exchange_id="txflow",
                                   market_id="119", mark=3.63, rate=-1.51614e-05,
                                   raw_rate=-1.51614e-05, interval=3600.0, fee=0.00045),
        ("Variational", "NATGAS"): dict(base, venue="Variational", exchange_id="variational",
                                        market_id="NATGAS-PERP", mark=3.627781523937963,
                                        rate=0.0002273, raw_rate=0.0009092,
                                        interval=14400.0, fee=0.0),
    }
    return rows


NATGAS_BOOKS = {
    "TxFlow": dict(bids=[(3.633, 1152.0), (3.632, 9100.8)],
                   asks=[(3.635, 1564.7), (3.636, 26292.0)], model="orderbook",
                   empty=False, crossed=False, received=0.0),
    "tradeXYZ": dict(bids=[(3.6435, 3.3), (3.6434, 273.9)],
                     asks=[(3.6439, 1070.5), (3.6441, 563.1)], model="orderbook",
                     empty=False, crossed=False, received=0.0),
    "Variational": dict(bids=[(3.62683, 275.555347358182)],
                        asks=[(3.63124, 275.555347358182)], model="rfq_indicative",
                        empty=False, crossed=False, received=0.0),
}


class Provider:
    def __init__(self, rows, books, settled=None, session=False):
        self.rows, self.books, self.s, self.session = rows, books, settled or {}, session

    def rows_by(self):
        return dict(self.rows)

    def book(self, v, a):
        return self.books.get(v)

    def settled(self, v, a, at):
        return self.s.get(v)

    def closes(self, v, a):
        return None

    def rth_open(self):
        return self.session


CFG = dict(wide_size=1000.0, wide_horizon=24.0, wide_min_net=0.5,
           wide_funding_realization=1.0, wide_max_breakeven_hours=24.0)


# --------------------------------------------------------------- universes (1-4)
class UniverseTests(unittest.TestCase):
    def test_paper_universe_is_exactly_the_owners_eight(self):
        self.assertEqual(observation.PAPER_EXECUTION_UNIVERSE_V2, (
            "lighter_rh", "risex", "arcus", "txflow", "variational", "popdex_perps",
            "veranta", "tradexyz"))
        self.assertEqual(observation.PAPER_EXECUTION_UNIVERSE_V2, policy.EXCHANGE_IDS)

    def test_observation_universe_has_eleven_and_four_observation_only(self):
        self.assertEqual(len(observation.OBSERVATION_UNIVERSE_V3), 11)
        self.assertEqual(set(observation.OBSERVATION_ONLY),
                         {"entropy_io", "hyperliquid_native", "phoenix_perps", "qfex"})

    def test_observation_only_venue_can_never_be_a_paper_leg(self):
        for x in observation.OBSERVATION_ONLY + ("binance", None):
            with self.assertRaises(observation.ObservationError):
                observation.assert_paper_venue(x)
        self.assertEqual(observation.assert_paper_venue("tradexyz"), "tradexyz")

    def test_matrix_covers_every_venue_and_no_cex(self):
        self.assertEqual(set(observation.VENUE_MATRIX),
                         set(observation.OBSERVATION_UNIVERSE_V3) | {"veranta"})
        for c in policy.CEX:
            self.assertNotIn(c.lower(), observation.VENUE_MATRIX)

    def test_env_switch_parse(self):
        self.assertFalse(observation.enabled({}))
        self.assertTrue(observation.enabled({"NB_OBSERVATION_V3": "1"}))
        with self.assertRaises(observation.ObservationError):
            observation.enabled({"NB_OBSERVATION_V3": "yes"})


# ---------------------------------------------------------- network modes (5-10)
class NetworkModeTests(unittest.TestCase):
    def test_nb_v2_policy_unchanged_refuses_observation_reads(self):
        for b in (body(type="metaAndAssetCtxs"), body(type="metaAndAssetCtxs", dex="io"),
                  body(type="fundingHistory", coin="INIT", startTime=0),
                  body(type="l2Book", coin="io:AAPL")):
            self.assertFalse(v2np.check(HL, "POST", "/info", b).allowed, b)
        self.assertFalse(v2np.check(observation.PHOENIX_HOST, "GET",
                                    "/v1/exchange/snapshot").allowed)
        self.assertNotIn(observation.PHOENIX_HOST, v2np.allowed_hosts())

    def test_observation_allows_native_and_io_scoped_reads(self):
        cases = [(body(type="metaAndAssetCtxs"), "hyperliquid_native"),
                 (body(type="metaAndAssetCtxs", dex=""), "hyperliquid_native"),
                 (body(type="metaAndAssetCtxs", dex="io"), "entropy_io"),
                 (body(type="metaAndAssetCtxs", dex="xyz"), "tradexyz"),
                 (body(type="fundingHistory", coin="INIT", startTime=1), "hyperliquid_native"),
                 (body(type="l2Book", coin="io:AAPL"), "entropy_io"),
                 (body(type="candleSnapshot", req=dict(coin="INIT", interval="1h",
                                                       startTime=0, endTime=1)),
                  "hyperliquid_native")]
        for b, xid in cases:
            d = observation.check(HL, "POST", "/info", b)
            self.assertTrue(d.allowed, b)
            self.assertEqual(d.exchange_id, xid)

    def test_observation_refuses_other_dex_spot_account_predicted(self):
        for b in (body(type="metaAndAssetCtxs", dex="flx"),
                  body(type="l2Book", coin="flx:GAS"),
                  body(type="l2Book", coin="@107"),
                  body(type="l2Book", coin="PURR/USDC"),
                  body(type="predictedFundings"),
                  body(type="clearinghouseState", user="0xabc"),
                  body(type="fundingHistory", coin="INIT", user="0xabc"),
                  body(type="spotMeta"), body(type="allMids")):
            self.assertFalse(observation.check(HL, "POST", "/info", b).allowed, b)
        self.assertFalse(observation.check(HL, "GET", "/info").allowed)

    def test_phoenix_only_documented_path_and_qfex_blocked(self):
        self.assertTrue(observation.check(observation.PHOENIX_HOST, "GET",
                                          "/v1/exchange/snapshot").allowed)
        for p in ("/v1/markets", "/v1/orders", "/v1/traders/x"):
            self.assertFalse(observation.check(observation.PHOENIX_HOST, "GET", p).allowed)
        self.assertFalse(observation.check(observation.PHOENIX_HOST, "POST",
                                           "/v1/exchange/snapshot").allowed)
        for h in observation.QFEX_HOSTS:
            self.assertFalse(observation.check(h, "GET", "/refdata").allowed)
            self.assertNotIn(h, observation.allowed_hosts())

    def test_cex_hosts_refused_in_both_modes(self):
        for h in ("fapi.binance.com", "api.bybit.com", "www.okx.com"):
            self.assertFalse(observation.check(h, "GET", "/").allowed)
            self.assertFalse(v2np.check(h, "GET", "/").allowed)
            self.assertNotIn(h, observation.allowed_hosts())

    def test_guard_selects_policy_and_refuses_unlisted_host(self):
        g = network_guard.Guard(None, mode=observation.MODE)
        self.assertIs(g.policy(), observation)
        self.assertTrue(g.v2)
        with self.assertRaises(network_guard.Refused):
            g("socket.getaddrinfo", ("fapi.binance.com", 443))
        g("socket.getaddrinfo", (observation.PHOENIX_HOST, 443))   # allowed host
        g2 = network_guard.Guard(None, v2=True)
        self.assertIs(g2.policy(), v2np)
        with self.assertRaises(network_guard.Refused):
            g2("socket.getaddrinfo", (observation.PHOENIX_HOST, 443))

    def test_install_refuses_observation_inside_nb_v2(self):
        saved = network_guard._GUARD
        network_guard._GUARD = None
        try:
            with self.assertRaises(RuntimeError):
                network_guard.install(None, v2=True, mode=observation.MODE)
        finally:
            network_guard._GUARD = saved

    def test_run_pilot_refuses_observation_mode(self):
        src = (HERE / "pilot" / "run_pilot.py").read_text(encoding="utf-8")
        self.assertIn("observation.enabled()", src)
        self.assertIn("read-only observation; the PAPER pilot does not run in it", src)


# ------------------------------------------------------- funding evidence (11-17)
class FundingEvidenceTests(unittest.TestCase):
    def test_natgas_screenshot_sign(self):
        s = SHOTS["NATGAS"]
        long_h = s["long"]["funding_8h_pct"] / 100 / 8
        short_h = s["short"]["funding_8h_pct"] / 100 / 8
        self.assertGreater(fe.leg_cashflow("long", long_h), 0)    # long receives
        self.assertGreater(fe.leg_cashflow("short", short_h), 0)  # short receives
        self.assertAlmostEqual(100 * fe.per_8h(fe.pair_carry(long_h, short_h)),
                               s["net_8h_pct"], places=4)

    def test_init_screenshot_sign(self):
        s = SHOTS["INIT"]
        long_h = s["long"]["funding_8h_pct"] / 100 / 8
        short_h = s["short"]["funding_8h_pct"] / 100 / 8
        self.assertAlmostEqual(100 * fe.leg_cashflow("long", long_h),
                               s["long"]["long_receives_1h_pct"], places=3)
        self.assertAlmostEqual(-100 * fe.leg_cashflow("short", short_h),
                               s["short"]["short_pays_1h_pct"], places=3)
        self.assertAlmostEqual(100 * fe.per_8h(fe.pair_carry(long_h, short_h)),
                               s["net_8h_pct"], places=4)

    def test_missing_rate_is_never_defaulted(self):
        self.assertIsNone(fe.pair_carry(None, 0.001))
        self.assertIsNone(fe.per_hour(None, 3600))
        self.assertIsNone(fe.per_hour(0.001, None))
        with self.assertRaises(ValueError):
            fe.leg_cashflow("buy", 0.1)

    def test_per_hour_from_four_hour_interval(self):
        self.assertAlmostEqual(fe.per_hour(0.0009092, 14400), 0.0002273)

    def test_venue_status_and_settled(self):
        self.assertEqual(fe.venue_status("txflow"), fe.RATE_PUBLISHED)
        self.assertEqual(fe.venue_status("variational"), fe.RATE_PUBLISHED)
        self.assertEqual(fe.venue_status("hyperliquid_native"), fe.MARKET_SETTLEMENT)
        self.assertEqual(fe.venue_status("qfex"), fe.MISSING)
        self.assertEqual(fe.venue_status("unknown"), fe.MISSING)
        self.assertFalse(fe.is_settled(fe.RATE_SAMPLED_AT_CUTOFF))
        self.assertFalse(fe.is_settled(fe.SETTLEMENT_INFERRED))
        self.assertTrue(fe.is_settled(fe.MARKET_SETTLEMENT))
        self.assertEqual(fe.strongest([fe.RATE_PUBLISHED, fe.MARKET_SETTLEMENT]),
                         fe.MARKET_SETTLEMENT)
        self.assertEqual(fe.strongest([fe.MARKET_SETTLEMENT, fe.REVISED]), fe.REVISED)

    def test_coverage_is_coverage_aware_55_of_90_days(self):
        at = 1_000 * 86400.0
        pts = [dict(ts=at - h * 3600, rate=0.0001) for h in range(55 * 24)]   # MOCK
        c = fe.coverage(pts, at)
        self.assertEqual(c["90d"]["status"], "PARTIAL")
        self.assertEqual(c["90d"]["observed"], 55 * 24)
        self.assertEqual(c["90d"]["expected"], 90 * 24)
        self.assertAlmostEqual(c["90d"]["covered_days"], 55.0, places=1)
        self.assertIn("55.0/90 days", c["90d"]["label"])
        self.assertAlmostEqual(c["90d"]["mean_hourly"], 0.0001)   # observed points only
        self.assertEqual(c["30d"]["status"], "FULL")
        self.assertIn("SHORT_HISTORY", fe.anomalies(0.0001, [p["rate"] for p in pts], c))
        self.assertEqual(fe.coverage([], at)["7d"]["status"], "EMPTY")
        self.assertIsNone(fe.coverage([], at)["7d"]["mean_hourly"])

    def test_anomalies_and_revisions(self):
        self.assertIn("EXTREME_RATE", fe.anomalies(-0.0021, [-0.0001] * 20))
        self.assertIn("OUTSIDE_HISTORY", fe.anomalies(-0.0021, [-0.0001] * 20))
        self.assertIn("NO_HISTORY", fe.anomalies(0.0001, []))
        flip = [0.0001, -0.0001] * 20
        self.assertIn("SIGN_FLIPS", fe.anomalies(0.0001, flip))
        at = 200 * 86400.0
        pts = [dict(ts=at - h * 3600, rate=-0.003 if h < 24 else -0.00001)  # MOCK regime
               for h in range(90 * 24)]
        self.assertIn("REGIME_SHIFT", fe.anomalies(-0.002, [p["rate"] for p in pts],
                                                   fe.coverage(pts, at)))
        rev = fe.revisions([dict(ts=1, rate=0.1), dict(ts=1, rate=0.1),
                            dict(ts=2, rate=0.1), dict(ts=2, rate=0.2)])
        self.assertEqual(rev, [dict(ts=2, rates=[0.1, 0.2])])


# ---------------------------------------------------- NATGAS / INIT (18-22)
class NatgasForensicTests(unittest.TestCase):
    def setUp(self):
        self.rows = natgas_rows()
        self.gate = gates.Gate(Provider(self.rows, NATGAS_BOOKS), CFG)

    def test_top_pair_first_and_all_blockers(self):
        c = dict(asset="NATGAS", long="TxFlow", short="tradeXYZ", strategy="funding")
        ev = self.gate.evaluate(c, 0.0, "funding")
        t = funnel.trace(c, ev, self.rows, None, "sha-0512", "funding")
        self.assertEqual(t["first_rejection"], "RWA_REFERENCE_UNVERIFIED")
        self.assertEqual(t["canonical_codes"],
                         ["RWA_REFERENCE_UNVERIFIED", "SETTLEMENT_EVIDENCE_MISSING"])
        self.assertEqual(t["outcome"], "NOT_EVALUATED")
        self.assertIsNone(t["net"])                         # unknown, not zero
        self.assertEqual(t["failed_at"], "EXECUTION_MODEL_CHECK")
        self.assertEqual(t["equivalence"], "ECONOMICALLY_RELATED_BUT_NOT_IDENTICAL")
        self.assertEqual(t["confidence"], "LOW")
        # The settlement gap is on the TxFlow (long) leg only; tradeXYZ publishes history.
        legs = [r["leg"] for r in t["all_rejections"]
                if r["canonical"] == "SETTLEMENT_EVIDENCE_MISSING"]
        self.assertEqual(legs, ["long"])

    def test_convergence_blocked_by_reference_only(self):
        c = dict(asset="NATGAS", long="TxFlow", short="tradeXYZ", strategy="convergence")
        ev = self.gate.evaluate(c, 0.0, "convergence")
        self.assertEqual([r["code"] for r in ev["reasons"]], ["RWA_REFERENCE_UNVERIFIED"])

    def test_variational_legs_add_execution_model(self):
        c = dict(asset="NATGAS", long="Variational", short="tradeXYZ", strategy="funding")
        t = funnel.trace(c, self.gate.evaluate(c, 0.0, "funding"), self.rows, None, "s",
                         "funding")
        self.assertEqual(t["first_rejection"], "EXECUTION_MODEL_UNVERIFIED")
        self.assertIn("RWA_REFERENCE_UNVERIFIED", t["canonical_codes"])

    def test_radar_shows_the_hidden_potential_but_never_eligible(self):
        def leg(v):
            r = self.rows[(v, "NATGAS")]
            return dict(venue=v, exchange_id=r["exchange_id"], hourly=r["rate"],
                        fee=r["fee"], book=NATGAS_BOOKS[v])
        r = funnel.radar("NATGAS", leg("TxFlow"), leg("tradeXYZ"))
        self.assertEqual(r["label"], "POTENTIAL")
        self.assertFalse(r["eligible"])
        one = r["sizes"][0]
        self.assertEqual(one["size"], 1000.0)
        self.assertLess(one["breakeven_h"], 24)
        self.assertGreater(one["cost_bp"], 0)
        # Only two OBSERVED levels in this fixture: larger sizes are reported as thin.
        self.assertEqual(r["sizes"][2]["reason"], "INSUFFICIENT_LIQUIDITY")
        rev = funnel.radar("NATGAS", leg("tradeXYZ"), leg("TxFlow"))
        self.assertEqual(rev["label"], "RESEARCH_ONLY")
        self.assertLess(rev["carry_hourly"], 0)

    def test_hyperliquid_native_gas_is_not_natgas(self):
        self.assertEqual(observation.coin_venue("GAS"), "hyperliquid_native")
        self.assertNotEqual(funnel.observation_xid("Hyperliquid"), "tradexyz")


class InitForensicTests(unittest.TestCase):
    def test_init_in_v2_is_single_venue(self):
        from v2.identity import Instrument, verify

        inst = Instrument("variational", "INIT-PERP", "INIT-PERP", name="Initia",
                          price=0.1246)
        groups = verify([inst])
        self.assertEqual(groups, {})                 # no multi-venue group is formed
        self.assertEqual(inst.reason, "SINGLE_VENUE_ONLY")

    def test_init_hyperliquid_leg_is_config_excluded(self):
        rows = {("Variational", "INIT"): dict(venue="Variational", asset="INIT",
                                              exchange_id="variational", rate=-0.001534,
                                              fee=0.0, mark=0.1415)}
        c = dict(asset="INIT", long="Variational", short="Hyperliquid", strategy="funding")
        g = gates.Gate(Provider(rows, {}), CFG)
        t = funnel.trace(c, g.evaluate(c, 0.0, "funding"), rows, None, "s", "funding")
        self.assertEqual(t["first_rejection"], "CONFIG_EXCLUDED")
        self.assertEqual(t["outcome"], "NOT_EVALUATED")
        self.assertEqual(t["legs"]["short"]["exchange_id"], "hyperliquid_native")
        self.assertFalse(t["legs"]["short"]["paper_venue"])

    def test_observation_row_mixed_into_paper_rows_is_refused(self):
        rows = {("Variational", "INIT"): dict(venue="Variational", asset="INIT",
                                              exchange_id="variational", rate=-0.0015,
                                              fee=0.0, mark=0.14),
                ("Hyperliquid", "INIT"): dict(venue="Hyperliquid", asset="INIT",
                                              exchange_id="hyperliquid_native",
                                              rate=-0.0006, fee=0.00045, mark=0.14)}
        g = gates.Gate(Provider(rows, {}), CFG)
        ev = g.evaluate(dict(asset="INIT", long="Variational", short="Hyperliquid"), 0.0,
                        "convergence")
        self.assertEqual([r["code"] for r in ev["reasons"]], ["CONFIG_EXCLUDED"])
        self.assertFalse(ev["eligible"])

    def test_init_ladder_rfq_bucket_and_thin_book(self):
        rec = dict(quotes=dict(size_1k=(0.1421, 0.1426), size_100k=(0.1408, 0.1442)))
        self.assertIn("size_1k", variational.quote_book_for(rec, 1000)["source"])
        self.assertIn("size_100k", variational.quote_book_for(rec, 5000)["source"])
        self.assertTrue(variational.quote_book_for(dict(quotes={}), 1000)["empty"])
        # MOCK thin book: ~$1,060 of bids inside the 0.1 % band, the rest far away.
        hl = dict(bids=[(0.14164, 4000.0), (0.14160, 3500.0), (0.1410, 1e6)],
                  asks=[(0.14176, 4000.0), (0.14180, 3500.0), (0.1425, 1e6)], model="orderbook")
        vb = dict(variational.quote_book_for(rec, 1000), model="rfq_indicative")
        r = funnel.radar("INIT", dict(venue="Variational", exchange_id="variational",
                                      hourly=-0.001534, fee=0.0, book=vb),
                         dict(venue="Hyperliquid", exchange_id="hyperliquid_native",
                              hourly=-0.00066826, fee=0.00045, book=hl), size_list=(1000.0,
                                                                                    5000.0))
        self.assertEqual(r["sizes"][0]["quotes"], ["INDICATIVE_RFQ", "ORDERBOOK"])
        self.assertEqual(r["sizes"][1]["reason"], "INSUFFICIENT_LIQUIDITY")
        self.assertFalse(r["paper_venues"])
        self.assertFalse(r["eligible"])


# ------------------------------------------------------------ funnel (23-26)
class FunnelTests(unittest.TestCase):
    def test_trace_id_is_stable_and_distinct(self):
        a = funnel.trace_id("s", "funding", "BTC", "Arcus", "Lighter")
        self.assertEqual(a, funnel.trace_id("s", "funding", "BTC", "Arcus", "Lighter"))
        self.assertNotEqual(a, funnel.trace_id("s", "funding", "BTC", "Lighter", "Arcus"))
        self.assertNotEqual(a, funnel.trace_id("t", "funding", "BTC", "Arcus", "Lighter"))

    def test_three_outcomes_are_never_merged(self):
        R = lambda cat, code: dict(category=cat, code=code, text="")   # noqa: E731
        self.assertEqual(funnel.outcome_of(dict(reasons=[R("DATA", "RWA_REFERENCE_UNVERIFIED")])),
                         "NOT_EVALUATED")
        self.assertEqual(funnel.outcome_of(dict(reasons=[R("EXECUTION", "INSUFFICIENT_LIQUIDITY")])),
                         "FAILED")
        self.assertEqual(funnel.outcome_of(dict(reasons=[R("COST", "BREAKEVEN")], net=-1.0)),
                         "UNPROFITABLE")
        self.assertEqual(funnel.outcome_of(dict(reasons=[], eligible=True)), "ELIGIBLE")
        self.assertEqual(funnel.outcome_of(dict(reasons=[])), "RESEARCH_ONLY")

    def test_canonical_codes(self):
        self.assertEqual(funnel.canonical("SETTLEMENT_EVIDENCE_MISSING", fe.RATE_PUBLISHED),
                         "SETTLEMENT_EVIDENCE_MISSING")
        self.assertEqual(funnel.canonical("SETTLEMENT_EVIDENCE_MISSING", fe.MARKET_SETTLEMENT),
                         "MISSING_FUNDING_HISTORY")
        self.assertEqual(funnel.canonical("BREAKEVEN"), "NET_AFTER_COSTS")
        self.assertEqual(funnel.canonical("RWA_SESSION_UNKNOWN"), "SESSION_UNKNOWN")
        self.assertEqual(funnel.canonical("RWA_SESSION_CLOSED"), "SESSION_CLOSED")
        for code in funnel.MAP.values():
            self.assertIn(code, funnel.CANONICAL + funnel.EXTRA)

    def test_equivalence_classes(self):
        self.assertEqual(funnel.equivalence(dict(status="MULTI_VENUE", tier="A"), "crypto"),
                         "EXACT")
        self.assertEqual(funnel.equivalence(dict(status="MULTI_VENUE", tier="A"), "commodity"),
                         "ECONOMICALLY_RELATED_BUT_NOT_IDENTICAL")
        self.assertEqual(funnel.equivalence(dict(status="MULTI_VENUE", tier="B"), "crypto"),
                         "UNVERIFIED")
        self.assertEqual(funnel.equivalence(dict(status="SINGLE_VENUE_ONLY")), "UNVERIFIED")
        self.assertEqual(funnel.equivalence(dict(status="X", reason="CONFLICT_LIST")),
                         "CONFLICT")
        self.assertEqual(funnel.equivalence(None), "UNVERIFIED")

    def test_summary_counts(self):
        rows = natgas_rows()
        gate = gates.Gate(Provider(rows, NATGAS_BOOKS), CFG)
        ts = []
        for lv, sv in (("TxFlow", "tradeXYZ"), ("Variational", "TxFlow")):
            c = dict(asset="NATGAS", long=lv, short=sv)
            ts.append(funnel.trace(c, gate.evaluate(c, 0.0, "funding"), rows, None, "s",
                                   "funding"))
        s = funnel.summary(ts)
        self.assertEqual(s["traces"], 2)
        self.assertEqual(s["not_evaluated"], 2)
        self.assertEqual(s["passed_at_least"]["ECONOMIC_CANDIDATE"], 2)
        self.assertEqual(s["passed_at_least"]["PAPER_ELIGIBLE"], 0)


# ---------------------------------------------------- adapters / dashboard (27-28)
class AdapterTests(unittest.TestCase):
    def test_parse_native_and_io(self):
        native = [dict(universe=[dict(name="INIT", szDecimals=0, maxLeverage=5)]),
                  [dict(funding="-0.0006682619", markPx="0.14076", oraclePx="0.1407",
                        openInterest="1", dayNtlVlm="2")]]                    # MOCK
        m = hlo.parse_markets("hyperliquid_native", native)[0]
        self.assertEqual(m["exchange_id"], "hyperliquid_native")
        self.assertEqual(m["funding"]["interval_s"], 3600)
        self.assertAlmostEqual(m["funding"]["raw"], -0.0006682619)
        self.assertTrue(m["observation_only"])
        with self.assertRaises(ValueError):
            hlo.parse_markets("entropy_io", native)
        io = [dict(universe=[dict(name="io:AAPL", deployerFeeScale="1.0")]),
              [dict(funding=None, markPx="200")]]                               # MOCK
        r = hlo.parse_markets("entropy_io", io)[0]
        self.assertIsNone(r["funding"])                                  # never defaulted
        with self.assertRaises(ValueError):
            hlo.parse_markets("hyperliquid_native", io)
        with self.assertRaises(ValueError):
            hlo.coin("hyperliquid_native", "xyz:NATGAS")

    def test_history_paging_dedupes_and_stops(self):
        calls = []

        def fetch(b):                                                       # MOCK pages
            calls.append(b)
            s = b["startTime"]
            n = 500 if len(calls) < 3 else 7
            return [dict(time=s + i * 3_600_000, fundingRate="0.0001") for i in range(n)]

        out = hlo.history(fetch, "hyperliquid_native", "INIT", 0, 10 ** 13)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(out), len({x["ts"] for x in out}))
        self.assertEqual(calls[1]["startTime"], 499 * 3_600_000 + 1)

    def test_dashboard_research_sections(self):
        snap = dict(at=1, rows=3, safe=True, strategies={}, top={}, open_hybrids=[],
                    reasons_by_venue={},
                    funnel=dict(passed_at_least={"DISCOVERED": 5, "PAPER_ELIGIBLE": 0},
                                outcome={"NOT_EVALUATED": 4, "UNPROFITABLE": 1}),
                    venues_v3=observation.VENUE_MATRIX,
                    cards=[dict(asset="NATGAS", facts=[("İlk ret", "RWA_REFERENCE_UNVERIFIED")])],
                    radar=[dict(asset="NATGAS", long="TxFlow", short="tradeXYZ",
                                carry_8h=0.003, label="POTENTIAL",
                                sizes=[dict(size=1000.0, cost_bp=17.7, breakeven_h=4.5)])])
        h = dashboard.render(snap)
        for s in ("Fırsat hunisi", "Değerlendirilmedi", "11 borsa", "hyperliquid_native",
                  "NATGAS — adli inceleme", "Potansiyel Fırsat Radarı", "POTENTIAL",
                  "PAPER değil", "qfex"):
            self.assertIn(s, h)
        self.assertNotIn("<script", h)


if __name__ == "__main__":
    unittest.main()
