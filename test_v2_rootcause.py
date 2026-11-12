"""Root-cause fixes after f03eb91: RWA reference registry, market-level funding
evidence, the TxFlow userFundings exception (authorized address only), the Variational
RFQ requirements, same-timestamp pairing and the radar over every NOT_EVALUATED trace.

Data labels: MOCK unless stated. FunnelDataTests replays the OBSERVED 05:12 snapshot
from C:\\Users\\W10\\nb-benchmark (skipped when that folder is absent).
"""
import json
import os
import unittest
from pathlib import Path

from v2 import funnel, gates, observation, references
from v2 import funding_evidence as fe
from v2.venues import txflow, variational

PHASE4 = Path(r"C:\Users\W10\nb-benchmark\v2-phase4-2026-11-12")
PILOT_DB = Path(r"C:\Users\W10\nb-pilot-loose\data\paper.sqlite3")
ADDR = "0x" + "ab" * 20
TX = "rpc.txflow.com"


def body(**kw):
    return json.dumps(kw).encode()


class Provider:
    def __init__(self, rows, books=None, settled=None):
        self.rows, self.books, self.s = rows, books or {}, settled or {}

    def rows_by(self):
        return dict(self.rows)

    def book(self, v, a):
        return self.books.get(v)

    def settled(self, v, a, at):
        return self.s.get(v)

    def closes(self, v, a):
        return None

    def rth_open(self):
        return True


CFG = dict(wide_size=1000.0, wide_horizon=24.0, wide_min_net=0.5,
           wide_funding_realization=1.0, wide_max_breakeven_hours=24.0)


def row(venue, xid, asset="NATGAS", fine="commodity", rate=0.0):
    return dict(venue=venue, asset=asset, exchange_id=xid, fine_class=fine, rate=rate,
                fee=0.0004, mark=3.6, interval=3600)


class ReferenceTests(unittest.TestCase):
    def test_natgas_references_are_not_verified_and_name_the_fields(self):
        c = references.compare("txflow", "tradexyz", "NATGAS")
        self.assertFalse(c["verified"])
        for f in ("underlying", "contract_month", "roll_rule", "off_hours"):
            self.assertIn(f, c["missing"])
        c2 = references.compare("tradexyz", "variational", "NATGAS")
        self.assertFalse(c2["verified"])
        self.assertIn("exchange", c2["missing"])          # neither names the exchange
        self.assertIn("roll_rule", c2["missing"])         # xyz docs conflict 5-9 vs 5-10

    def test_every_registered_field_has_a_source(self):
        for key, spec in references.REGISTRY.items():
            self.assertEqual(set(spec), set(references.REQUIRED), key)
            for f, (v, src) in spec.items():
                self.assertTrue(src.startswith("http"), (key, f))

    def test_unregistered_market_is_not_verified(self):
        c = references.compare("arcus", "tradexyz", "GOLD")
        self.assertFalse(c["verified"])
        self.assertFalse(c["registered"]["a"])

    def test_gate_opens_only_on_a_full_verified_match(self):
        rows = {("A", "X"): row("A", "arcus", "X"), ("B", "X"): row("B", "lighter_rh", "X")}
        g = gates.Gate(Provider(rows), CFG)
        c = dict(asset="X", long="A", short="B")
        self.assertIn("RWA_REFERENCE_UNVERIFIED",
                      [r["code"] for r in g.static(dict(c, strategy="convergence"), rows)])
        same = {f: ("same value", "https://example.invalid/doc") for f in references.REQUIRED}
        saved = dict(references.REGISTRY)
        try:  # MOCK registry: identical, fully known specs
            references.REGISTRY[("arcus", "X")] = same
            references.REGISTRY[("lighter_rh", "X")] = same
            self.assertNotIn("RWA_REFERENCE_UNVERIFIED", [
                r["code"] for r in g.static(dict(c, strategy="convergence"), rows)])
            one_unknown = dict(same, exchange=(references.UNKNOWN, "https://x.invalid"))
            references.REGISTRY[("lighter_rh", "X")] = one_unknown
            self.assertIn("RWA_REFERENCE_UNVERIFIED", [
                r["code"] for r in g.static(dict(c, strategy="convergence"), rows)])
        finally:
            references.REGISTRY.clear()
            references.REGISTRY.update(saved)


class MarketEvidenceTests(unittest.TestCase):
    AT = 100 * 86400.0

    def pts(self, hours, kind="settled_public"):
        return [dict(ts=self.AT - h * 3600, rate=0.0001, kind=kind) for h in range(hours)]

    def test_full_partial_missing_and_rate_only(self):
        full = fe.market_evidence("tradexyz", self.pts(168), self.AT, 3600, 7)
        self.assertEqual((full["status"], full["confidence"], full["sufficient"]),
                         (fe.MARKET_SETTLEMENT, "HIGH", True))
        part = fe.market_evidence("tradexyz", self.pts(100), self.AT, 3600, 7)
        self.assertEqual((part["confidence"], part["sufficient"]), ("MEDIUM", False))
        nr = fe.market_evidence("tradexyz", None, self.AT)
        self.assertEqual((nr["status"], nr["basis"], nr["confidence"]),
                         (fe.MISSING, "HISTORY_NOT_READ", "NONE"))
        ro = fe.market_evidence("txflow", None, self.AT)
        self.assertEqual((ro["status"], ro["basis"], ro["confidence"]),
                         (fe.RATE_PUBLISHED, "VENUE_PUBLISHES_RATE_ONLY", "LOW"))
        self.assertEqual(fe.market_evidence("tradexyz", [], self.AT)["basis"],
                         "EMPTY_HISTORY")

    def test_sampled_rates_are_not_payments_and_revisions_void(self):
        s = fe.market_evidence("variational", self.pts(168, "sampled_at_cutoff"), self.AT)
        self.assertEqual(s["status"], fe.RATE_SAMPLED_AT_CUTOFF)
        self.assertFalse(s["sufficient"])
        mixed = self.pts(168) + [dict(ts=self.AT, rate=0.0001, kind="observed_published")]
        self.assertEqual(fe.market_evidence("tradexyz", mixed, self.AT)["status"],
                         fe.RATE_PUBLISHED)                  # the weakest kind wins
        rev = self.pts(168) + [dict(ts=self.AT, rate=0.0009, kind="settled_public")]
        r = fe.market_evidence("tradexyz", rev, self.AT)
        self.assertEqual((r["status"], r["confidence"]), (fe.REVISED, "NONE"))
        acc = fe.market_evidence("txflow", self.pts(168, "account_payment"), self.AT)
        self.assertEqual((acc["status"], acc["sufficient"]), (fe.ACCOUNT_PAYMENT, True))

    def test_gate_settlement_is_per_market_not_per_venue(self):
        rows = {("TxFlow", "BTC"): row("TxFlow", "txflow", "BTC", "crypto"),
                ("Arcus", "BTC"): row("Arcus", "arcus", "BTC", "crypto")}
        c = dict(asset="BTC", long="TxFlow", short="Arcus", strategy="funding")
        codes = lambda g: [r["code"] for r in g.static(c, rows, self.AT)]  # noqa: E731
        self.assertIn("SETTLEMENT_EVIDENCE_MISSING", codes(gates.Gate(Provider(rows), CFG)))
        sampled = gates.Gate(Provider(rows, settled={"TxFlow": self.pts(168, "sampled_at_cutoff")}),
                             CFG)
        self.assertIn("SETTLEMENT_EVIDENCE_MISSING", codes(sampled))
        paid = gates.Gate(Provider(rows, settled={"TxFlow": self.pts(168, "account_payment")}),
                          CFG)
        self.assertNotIn("SETTLEMENT_EVIDENCE_MISSING", codes(paid))
        self.assertIn("SETTLEMENT_EVIDENCE_MISSING",
                      [r["code"] for r in gates.Gate(Provider(rows, settled={
                          "TxFlow": self.pts(168, "account_payment")}), CFG).static(c, rows)])

    def test_trace_confidence_is_the_weakest_leg(self):
        rows = {("Arcus", "BTC"): row("Arcus", "arcus", "BTC", "crypto"),
                ("Lighter", "BTC"): row("Lighter", "lighter_rh", "BTC", "crypto")}
        c = dict(asset="BTC", long="Arcus", short="Lighter")
        ev = dict(reasons=[dict(category="COST", code="BREAKEVEN", text="")], net=-1)
        hi = fe.market_evidence("arcus", self.pts(168), self.AT)
        mid = fe.market_evidence("lighter_rh", self.pts(90), self.AT)
        t = funnel.trace(c, ev, rows, None, "s", "funding", evidence=dict(long=hi, short=mid))
        self.assertEqual(t["confidence"], "MEDIUM")
        self.assertEqual(t["legs"]["long"]["evidence_basis"], "MARKET_POINTS")
        t2 = funnel.trace(c, ev, rows, None, "s", "funding")
        self.assertEqual(t2["confidence"], "LOW")              # capability only
        self.assertEqual(t2["legs"]["long"]["evidence_basis"], "VENUE_CAPABILITY")


class UserFundingsTests(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get(observation.FUNDING_ADDRESS_ENV)
        os.environ.pop(observation.FUNDING_ADDRESS_ENV, None)

    def tearDown(self):
        os.environ.pop(observation.FUNDING_ADDRESS_ENV, None)
        if self.saved is not None:
            os.environ[observation.FUNDING_ADDRESS_ENV] = self.saved

    def test_refused_without_authorized_address(self):
        d = observation.check(TX, "POST", "/info", body(type="userFundings", user=ADDR))
        self.assertFalse(d.allowed)

    def test_only_the_authorized_address_and_never_in_nb_v2(self):
        os.environ[observation.FUNDING_ADDRESS_ENV] = ADDR
        ok = observation.check(TX, "POST", "/info", body(type="userFundings", user=ADDR.upper(), coin="NATGAS"))
        self.assertTrue(ok.allowed)
        self.assertEqual(ok.flag, observation.ACCOUNT_READ)
        other = "0x" + "cd" * 20
        self.assertFalse(observation.check(TX, "POST", "/info",
                                           body(type="userFundings", user=other)).allowed)
        self.assertFalse(observation.check(TX, "POST", "/info", body(
            type="userFundings", user=ADDR, signature="x")).allowed)
        self.assertFalse(observation.check(TX, "POST", "/info", body(
            type="clearinghouseState", user=ADDR)).allowed)
        from v2 import network_policy as v2np
        self.assertFalse(v2np.check(TX, "POST", "/info",
                                    body(type="userFundings", user=ADDR)).allowed)

    def test_malformed_address_is_not_authorized(self):
        self.assertIsNone(observation.authorized_address({observation.FUNDING_ADDRESS_ENV: "abc"}))

    def test_parser_keeps_rate_unknown(self):
        raw = {"data": {"data": [dict(time=7200000, coin="NATGAS", usdc="10", szi="-3",
                                      fundingRate="0.0012", amount="0.04"),
                                 dict(time=3600000, coin="NATGAS", usdc="10", szi="-3",
                                      fundingRate="0.0011", amount="0.03")],
                        "hasMore": False}}                                    # MOCK
        out = txflow.parse_user_fundings(raw)
        self.assertEqual([x["ts"] for x in out], [3600.0, 7200.0])
        self.assertTrue(all(x["rate"] is None and x["kind"] == "account_payment" for x in out))
        self.assertEqual(txflow.USER_FUNDINGS_RATE_UNIT, "UNKNOWN")
        b = txflow.user_fundings_request(ADDR, "NATGAS", 0, 1)[2]
        self.assertLessEqual(b["limit"], 2000)


class RfqTests(unittest.TestCase):
    def test_rfq_model_stays_blocked(self):
        status, gaps = variational.rfq_model_status()
        self.assertEqual(status, "BLOCKED")
        for g in ("firm_vs_indicative", "last_look_rejects", "public_fills"):
            self.assertIn(g, gaps)

    def test_quote_age(self):
        r = dict(quotes_updated="2026-11-12T04:11:21.500Z")
        import datetime
        t = datetime.datetime(2026, 11, 12, 4, 11, 31, 500000,
                              tzinfo=datetime.timezone.utc).timestamp()
        self.assertAlmostEqual(variational.quote_age(r, t), 10.0, places=3)
        self.assertIsNone(variational.quote_age({}, t))


class SourceIsolationTests(unittest.TestCase):
    def test_variational_answers_never_cross_sources(self):
        variational._CACHE.items.clear()
        a = variational.stats(lambda: {"src": "MOCK-A"}, source="replay-a")[0]
        b = variational.stats(lambda: {"src": "MOCK-B"}, source="replay-b")[0]
        self.assertEqual((a["src"], b["src"]), ("MOCK-A", "MOCK-B"))
        f = lambda: {"src": "MOCK-F"}                              # noqa: E731
        self.assertFalse(variational.stats(f)[1])
        self.assertTrue(variational.stats(f)[1])                    # same fetch: cached
        self.assertEqual(variational.stats(lambda: {"src": "MOCK-G"})[0]["src"], "MOCK-G")
        variational._CACHE.items.clear()

    def test_replay_collector_has_its_own_source(self):
        from v2.collector import Collector

        c1, c2 = Collector(transport=lambda *a, **k: None), Collector()
        self.assertNotEqual(c1.source, variational.LIVE)
        self.assertEqual(c2.source, variational.LIVE)


class AlignedTests(unittest.TestCase):
    def leg(self, venue, times, hourly=0.0001):
        book = dict(bids=[(99.99, 1e6)], asks=[(100.01, 1e6)], model="orderbook")
        return dict(venue=venue, exchange_id="tradexyz", hourly=hourly, fee=0.0004,
                    book=book, times=times)

    def test_rates_from_different_moments_are_never_combined(self):
        from tools.v2_aligned import MAX_SKEW_S, pair

        ok = pair("X", self.leg("A", [0.0, 5.0], -0.0001), self.leg("B", [3.0]), 0.0)
        self.assertEqual(ok["status"], "ALIGNED")
        self.assertIsNotNone(ok["carry_8h"])
        bad = pair("X", self.leg("A", [0.0]), self.leg("B", [MAX_SKEW_S + 1]), 0.0)
        self.assertEqual(bad["status"], "TIME_MISALIGNED")
        self.assertIsNone(bad["carry_8h"])
        none = pair("X", self.leg("A", [None]), self.leg("B", [None]), 0.0)
        self.assertEqual(none["status"], "TIME_MISALIGNED")


class RadarIndexTests(unittest.TestCase):
    def test_every_not_evaluated_trace_is_listed(self):
        traces = [dict(trace_id="t%d" % i, outcome=o, asset="X", long=l, short=s)
                  for i, (o, l, s) in enumerate([("NOT_EVALUATED", "A", "B"),
                                                 ("NOT_EVALUATED", "A", "B"),
                                                 ("NOT_EVALUATED", "B", "A"),
                                                 ("UNPROFITABLE", "A", "C")])]

        def leg_input(v, a):
            if v == "B":
                raise LookupError("no row")                  # still listed, with error
            return dict(venue=v, exchange_id="arcus", hourly=0.0, fee=0.0004,
                        book=dict(bids=[(99.0, 1e9)], asks=[(101.0, 1e9)]))

        idx = funnel.radar_index(traces, leg_input)
        ids = {i for e in idx for i in e["trace_ids"]}
        self.assertEqual(ids, {"t0", "t1", "t2"})
        self.assertTrue(all(e["eligible"] is False for e in idx))
        self.assertTrue(all("error" in e for e in idx))


@unittest.skipUnless((PHASE4 / "answers.json.gz").exists() and PILOT_DB.exists(),
                     "OBSERVED 05:12 snapshot not on this machine")
class FunnelDataTests(unittest.TestCase):
    """All 2,597 candidates of the OBSERVED 05:12 snapshot."""

    @classmethod
    def setUpClass(cls):
        from tools import v2_forensics as vf

        cls.r = vf.replay(PHASE4)
        cls.traces = vf.all_traces(cls.r)
        cls.radar = vf.radar_all(cls.r, cls.traces)

    def test_counts(self):
        s = funnel.summary(self.traces)
        self.assertEqual(s["traces"], 2597)
        self.assertEqual(s["passed_at_least"]["PAPER_ELIGIBLE"], 0)
        self.assertEqual(sum(s["outcome"].values()), 2597)

    def test_every_trace_is_consistent(self):
        ids = set()
        for t in self.traces:
            ids.add(t["trace_id"])
            self.assertIn(t["outcome"], funnel.OUTCOMES)
            self.assertIn(t["stage_passed"], funnel.STAGES)
            if t["outcome"] == "NOT_EVALUATED":
                self.assertIsNone(t["net"])
                self.assertEqual(t["economics"], "NOT_EVALUATED")
                self.assertEqual(t["failed_at"], "EXECUTION_MODEL_CHECK")
                self.assertTrue(any(r["code"] in funnel.PRE_ECONOMICS
                                    for r in t["all_rejections"]))
            if t["outcome"] == "UNPROFITABLE":
                self.assertTrue(all(r["category"] == "COST" for r in t["all_rejections"]))
            if t["outcome"] != "ELIGIBLE":
                self.assertTrue(t["first_rejection"])
                self.assertEqual(t["first_rejection"], t["all_rejections"][0]["canonical"])
            for code in t["canonical_codes"]:
                self.assertIn(code, funnel.CANONICAL + funnel.EXTRA)
            for leg in t["legs"].values():
                self.assertTrue(leg["paper_venue"])
                if leg["evidence_basis"] == "MARKET_POINTS" and \
                        leg["evidence_confidence"] == "HIGH":
                    self.assertTrue(leg["evidence_sufficient"])
        self.assertEqual(len(ids), 2597)                          # ids are unique

    def test_every_not_evaluated_candidate_is_on_the_radar(self):
        ne = {t["trace_id"] for t in self.traces if t["outcome"] == "NOT_EVALUATED"}
        covered = {i for e in self.radar for i in e["trace_ids"]}
        self.assertEqual(ne, covered)
        self.assertTrue(all(e["eligible"] is False for e in self.radar))

    def test_natgas_is_on_the_radar_and_still_blocked(self):
        nat = [e for e in self.radar if e["asset"] == "NATGAS" and e["long"] == "TxFlow"
               and e["short"] == "tradeXYZ"]
        self.assertEqual(len(nat), 1)
        self.assertEqual(nat[0]["label"], "POTENTIAL")
        t = next(t for t in self.traces if t["asset"] == "NATGAS" and
                 t["strategy"] == "funding" and t["long"] == "TxFlow")
        self.assertEqual(t["canonical_codes"],
                         ["RWA_REFERENCE_UNVERIFIED", "SETTLEMENT_EVIDENCE_MISSING"])
        self.assertEqual(t["legs"]["short"]["evidence"], fe.MARKET_SETTLEMENT)
        self.assertEqual(t["legs"]["long"]["evidence_basis"], "VENUE_PUBLISHES_RATE_ONLY")


if __name__ == "__main__":
    unittest.main()
