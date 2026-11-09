"""Lighter Core funding and fee (2026-11-10).

Defect: CCXT's lighter adapter returns a funding rate with no interval and no
timestamps, has no funding history (fetchFundingRateHistory NotSupported) and reports
taker fee 0.0. The scanner therefore dropped every Lighter Core market from the
universe ("Funding aralığı kaynakta yok"), could never settle a paper leg or verify
interval/sign, and priced the venue at a zero fee whose account type is unverified.

Fix: funding from Lighter Core's own public `fundings` endpoint (last settled payment,
labelled a projection, as Lighter RH); interval only from observed settlement spacing;
documented Premium taker fee. Venue responses below are MOCK, shaped like the real
`fundings` JSON in testdata/ (lighter_rh_fundings_btc_20261109.json)."""
import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import wide_markets
from wide_markets import (
    LIGHTER_CORE_TAKER_FEE,
    LIGHTER_SETTLED_MAX_AGE,
    WideMarkets,
    lighter_fundings,
    settled_interval,
)

H = 3600
T0 = 1_000 * 86400  # on the hour


def mock_fundings(last_ts, n=6, rate="0.0012", direction="long", step=H):
    """MOCK Lighter `fundings` response (rate in percent, as the venue sends)."""
    return {"code": 200, "resolution": "1h", "fundings": [
        dict(timestamp=last_ts - i * step, value="0.01", rate=rate, direction=direction)
        for i in reversed(range(n))
    ]}


class ParsingTests(unittest.TestCase):
    def test_sign_and_scale_follow_the_engine_convention(self):
        rows = lighter_fundings(mock_fundings(T0, n=2, rate="0.0012", direction="short"))
        self.assertEqual([r["ts"] for r in rows], [T0 - H, T0])
        self.assertAlmostEqual(rows[-1]["rate"], -0.000012)
        with self.assertRaises(ValueError):
            lighter_fundings({"fundings": [dict(timestamp=T0, rate="1", direction="x")]})

    def test_real_fixture_parses(self):
        path = os.path.join(os.path.dirname(__file__), "testdata",
                            "lighter_rh_fundings_btc_20261109.json")
        with open(path, encoding="utf8") as f:
            rows = lighter_fundings(json.load(f)["response"])  # real recorded response
        self.assertEqual(len(rows), 8)
        self.assertEqual(settled_interval(rows), 3600)
        self.assertAlmostEqual(rows[0]["rate"], 0.000012)  # "0.0012" %, direction long

    def test_interval_comes_only_from_regular_settlements(self):
        hourly = lighter_fundings(mock_fundings(T0, n=4))
        self.assertEqual(settled_interval(hourly), 3600)
        self.assertIsNone(settled_interval(hourly[:3]))  # too few to prove
        irregular = hourly[:-1] + [dict(hourly[-1], ts=hourly[-1]["ts"] + 600)]
        self.assertIsNone(settled_interval(irregular))


class NativeFundingTests(unittest.TestCase):
    def setUp(self):
        self.w = WideMarkets(store=None, symbols=["BTC"], stop=threading.Event())
        self.calls = []
        self.clock = T0 + 120
        for p in (mock.patch.object(wide_markets, "now", lambda: self.clock),):
            p.start()
            self.addCleanup(p.stop)

        def fake_json(url):
            self.calls.append(url)
            return self.response, dict(received=self.clock, audit_id=None, http_age=0)

        self.w._price_discovery_json = fake_json
        self.response = mock_fundings(T0)

    def test_last_settlement_is_a_labelled_projection(self):
        self.w.lighter_core_budget = 5
        x = self.w.lighter_core_funding("1", self.clock)
        self.assertAlmostEqual(x["raw_rate"], 0.000012)
        self.assertEqual((x["interval"], x["next"]), (3600, T0 + 3600))
        self.assertEqual(x["funding_kind"], "last_settled_projection")
        self.assertIn("market_id=1", self.calls[0])

    def test_cached_until_the_next_settlement_then_refetched(self):
        self.w.lighter_core_budget = 5
        self.w.lighter_core_funding("1", self.clock)
        self.w.lighter_core_funding("1", self.clock + 600)
        self.assertEqual(len(self.calls), 1)
        self.response = mock_fundings(T0 + H, rate="0.0024")
        self.clock = T0 + H + 400
        x = self.w.lighter_core_funding("1", self.clock)
        self.assertEqual(len(self.calls), 2)
        self.assertAlmostEqual(x["raw_rate"], 0.000024)

    def test_no_budget_and_no_cache_is_unknown_not_a_guess(self):
        self.w.lighter_core_budget = 0
        self.assertIsNone(self.w.lighter_core_funding("1", self.clock))
        self.assertEqual(self.calls, [])

    def test_stale_settlement_is_not_a_current_rate(self):
        self.w.lighter_core_budget = 5
        self.response = mock_fundings(T0 - 2 * H)
        self.assertIsNone(self.w.lighter_core_funding("1", T0 - 2 * H + LIGHTER_SETTLED_MAX_AGE + 1))

    def test_ccxt_rate_and_zero_fee_are_replaced(self):
        self.w.lighter_core_budget = 5
        row = dict(raw_rate=0.000096, rate=None, interval=None, next=None, fee=0.0)
        self.w.apply_lighter_core(row, dict(id="1"), self.clock)
        self.assertAlmostEqual(row["raw_rate"], 0.000012)
        self.assertAlmostEqual(row["rate"], 0.000012)
        self.assertEqual(row["fee"], LIGHTER_CORE_TAKER_FEE)
        self.assertIn("doğrulanmadı", row["fee_source"])

    def test_without_native_evidence_the_ccxt_rate_is_dropped(self):
        self.w.lighter_core_budget = 0
        row = dict(raw_rate=0.000096, rate=0.000096, interval=3600, next=None, fee=0.0)
        self.w.apply_lighter_core(row, dict(id="1"), self.clock)
        self.assertEqual((row["raw_rate"], row["rate"], row["interval"]), (None, None, None))
        self.assertEqual(row["fee"], LIGHTER_CORE_TAKER_FEE)

    def test_history_reads_the_native_endpoint(self):
        self.response = mock_fundings(T0, n=48)
        rows = self.w.lighter_core_history("1", T0 - 24 * H)
        self.assertEqual(len(rows), 25)  # T0-24h .. T0 inclusive
        self.assertTrue(all(r["ts"] >= T0 - 24 * H for r in rows))
        self.assertIn("count_back=750", self.calls[0])


class FakeLighter:
    """MOCK CCXT lighter client: what the real adapter returns (no interval, no
    timestamps, fee 0.0, no funding history)."""
    id = "lighter"
    precisionMode = None
    urls = {"doc": None}
    neutralbasis_audit = None

    def __init__(self):
        self.has = dict(fetchFundingRates=True, fetchFundingRate=False,
                        fetchFundingRateHistory=False, fetchTickers=True)
        self.markets = {
            "BTC/USDC:USDC": dict(symbol="BTC/USDC:USDC", id="1", base="BTC", quote="USDC",
                                  settle="USDC", swap=True, linear=True, active=True,
                                  contractSize=1.0, taker=0.0, maker=0.0, limits={}),
        }

    def fetch_funding_rates(self, symbols=None):
        return {"BTC/USDC:USDC": dict(fundingRate=0.000096, interval=None,
                                      fundingTimestamp=None, nextFundingTimestamp=None)}

    def fetch_tickers(self, symbols=None):
        return {"BTC/USDC:USDC": dict(bid=99.9, ask=100.1, quoteVolume=1e6, timestamp=None)}

    def market(self, symbol):
        return self.markets[symbol]


class ScanIntegrationTests(NativeFundingTests):
    def test_scan_builds_lighter_core_rows_from_native_funding(self):
        ex = FakeLighter()
        self.w.client = lambda v, *a, **k: ex
        self.w.scan_one("LighterCore")
        self.assertTrue(self.w.status["LighterCore"]["ok"], self.w.status)
        (row,) = [r for r in self.w.rows["LighterCore"] if r["kind"] == "perp"]
        self.assertEqual(row["interval"], 3600)
        self.assertAlmostEqual(row["raw_rate"], 0.000012)  # not CCXT's 0.000096
        self.assertEqual(row["fee"], LIGHTER_CORE_TAKER_FEE)  # not CCXT's 0.0
        self.assertEqual(row["funding_kind"], "last_settled_projection")
        # The bulk universe keeps the market too (it dropped all of them before).
        self.assertIn("BTC", self.w.universe["LighterCore"])
        self.assertEqual(self.w.universe["LighterCore"]["BTC"]["interval"], 3600)
        # One native request served both the universe and the selected row.
        self.assertEqual(len(self.calls), 1)

    def test_selected_markets_get_the_request_budget_first(self):
        ex = FakeLighter()
        for i, a in enumerate(("AAA", "BBB", "CCC")):
            ex.markets["%s/USDC:USDC" % a] = dict(ex.markets["BTC/USDC:USDC"],
                                                  symbol="%s/USDC:USDC" % a, id=str(10 + i), base=a)
        rates = ex.fetch_funding_rates()
        ex.fetch_funding_rates = lambda symbols=None: {
            s: rates["BTC/USDC:USDC"] for s in ex.markets}
        self.w.client = lambda v, *a, **k: ex
        with mock.patch.object(wide_markets, "LIGHTER_CORE_FETCH_PER_SCAN", 1):
            self.w.scan_one("LighterCore")
        (row,) = [r for r in self.w.rows["LighterCore"] if r["kind"] == "perp"]
        self.assertEqual((row["asset"], row["interval"]), ("BTC", 3600))
        self.assertIn("market_id=1&", self.calls[0])

    def test_funding_since_no_longer_refuses_lighter_core(self):
        ex = FakeLighter()
        self.w.client = lambda v, *a, **k: ex
        self.w.scan_one("LighterCore")
        self.response = mock_fundings(T0, n=10)
        rows = self.w.funding_since("LighterCore", "BTC", T0 - 5 * H)
        self.assertEqual([r["ts"] for r in rows], [T0 - i * H for i in reversed(range(6))])

    def test_other_ccxt_venues_are_untouched(self):
        ex = FakeLighter()
        ex.id = "other"
        self.w.client = lambda v, *a, **k: ex
        with mock.patch.dict(wide_markets.CATALOG, {"Other": dict(wide_markets.CATALOG["Hyperliquid"])}):
            self.w.client_locks["Other"] = threading.RLock()
            self.w.scan_one("Other")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
