"""V2 phase 3: group identity, venue adapters, shared request budgets, source-level
V2 switches, the tradeXYZ client and the strategy-row source.

All answers below are MOCK fixtures shaped like the venues' documented/observed public
answers (2026-11-12); they are not market evidence. No network access."""
import json
import os
import unittest
from unittest import mock

from v2 import discovery, identity, network_policy as npol, ratelimit, source
from v2.identity import Instrument
from v2.venues import arcus, lighter_rh, risex, tradexyz, txflow, variational

V2_ON = {"NB_V2": "1"}


def inst(xid, symbol, cls="unknown", price=100.0, name=None, **kw):
    return Instrument(xid, symbol, symbol, asset_class=cls, price=price, name=name,
                      class_source=None if cls == "unknown" else "mock", **kw)


class GroupIdentityTests(unittest.TestCase):
    def test_two_independent_anchors_make_tier_a(self):
        a, b = inst("risex", "BTC", "crypto", 70000), inst("arcus", "BTC-USD", "crypto", 70100)
        g = identity.verify([a, b])
        self.assertEqual(g["crypto:BTC"]["status"], "MULTI_VENUE")
        self.assertEqual(g["crypto:BTC"]["tier"], "A")

    def test_price_mismatch_and_name_conflict(self):
        # Chevron vs "Convex Finance": same ticker, different price.
        cvx = [inst("arcus", "CVX-USD", "equities", 150.0, "Chevron"),
               inst("txflow", "CVX", "equity", 150.5),
               inst("variational", "CVX-PERP", price=1.68, name="Convex Finance")]
        identity.verify(cvx)
        self.assertEqual(cvx[2].reason, identity.PRICE_MISMATCH)
        # SpaceX vs a SPAC ETF at the same price: names disagree, both named leave.
        sp = [inst("arcus", "SPCX-USD", "equities", 138.7,
                   "Space Exploration Technologies Corp"),
              inst("lighter_rh", "SPCX", "rwa", 138.6),
              inst("variational", "SPCX-PERP", price=138.69, name="SPAC and New Issue ETF")]
        identity.verify(sp)
        self.assertEqual((sp[0].reason, sp[2].reason),
                         (identity.NAME_CONFLICT, identity.NAME_CONFLICT))
        # The unnamed anchor is left alone on its venue.
        self.assertEqual(sp[1].reason, identity.SINGLE_VENUE)

    def test_single_anchor_needs_confirmation(self):
        # TxFlow "Commodities" GAS + Variational "Gas": one venue's class is not enough.
        gas = [inst("txflow", "GAS", "commodity", 1.034),
               inst("variational", "GAS-PERP", price=1.0367, name="Gas")]
        identity.verify(gas)
        self.assertEqual(gas[1].reason, identity.WEAK)
        # A named anchor that agrees confirms the joiner (tier A).
        amd = [inst("arcus", "AMD-USD", "equities", 160.0, "Advanced Micro Devices"),
               inst("variational", "AMD-PERP", price=160.3, name="Advanced Micro Devices, Inc.")]
        g = identity.verify(amd)
        self.assertEqual(g["rwa:AMD"]["tier"], "A")

    def test_tier_b_crypto_is_reported_not_traded(self):
        ada = [inst("txflow", "ADA", "crypto", 0.500),
               inst("variational", "ADA-PERP", price=0.501, name="Cardano")]
        g = identity.verify(ada)
        self.assertEqual(g["crypto:ADA"]["tier"], "B")
        # Too far for tier B (0.5 %), or a corporate name: not joined.
        far = [inst("txflow", "XYZ", "crypto", 1.00),
               inst("variational", "XYZ-PERP", price=1.008, name="Xyz Token")]
        identity.verify(far)
        self.assertEqual(far[1].reason, identity.WEAK)
        corp = [inst("txflow", "ABC", "crypto", 10.0),
                inst("variational", "ABC-PERP", price=10.01, name="ABC Holdings Inc.")]
        identity.verify(corp)
        self.assertEqual(corp[1].reason, identity.WEAK)

    def test_preipo_conflicts_and_single_venue(self):
        xs = [inst("lighter_rh", "SPACEX", "preipo", 500.0),
              inst("variational", "QNT-PERP", "unknown", 178.0),
              inst("arcus", "ONLY-USD", "crypto", 1.0)]
        identity.verify(xs)
        self.assertEqual([x.reason for x in xs],
                         [identity.PREIPO_REASON, identity.CONFLICT, identity.SINGLE_VENUE])


class AdapterTests(unittest.TestCase):
    def test_lighter_rh(self):
        raw = {"order_book_details": [
            {"symbol": "BTC", "market_id": 1, "market_type": "perp", "status": "active",
             "funding_premium_multiplier": 100, "multiplier": "1.0", "taker_fee": "0.0350",
             "is_taker_fee_enabled": True, "mark_price": "70000", "index_price": "70010",
             "market_config": {}},
            {"symbol": "NVDA", "market_id": 20, "market_type": "perp", "status": "active",
             "funding_premium_multiplier": 50, "multiplier": "1.0", "taker_fee": "0.0000",
             "is_taker_fee_enabled": True, "mark_price": "160", "market_config": {}},
            {"symbol": "OPENAI", "market_id": 7, "market_type": "perp", "status": "active",
             "funding_premium_multiplier": 1, "multiplier": "1.0", "market_config": {}},
            {"symbol": "ODD", "market_id": 9, "market_type": "perp", "status": "active",
             "funding_premium_multiplier": 77, "multiplier": "10", "market_config": {}},
        ]}
        recs = lighter_rh.parse_markets(raw)
        self.assertEqual([r["instrument"].asset_class for r in recs],
                         ["crypto", "rwa", "preipo", "unknown"])
        self.assertAlmostEqual(recs[0]["fee_taker"], 0.00035)  # percent -> fraction
        self.assertIsNone(recs[3]["instrument"].multiplier)    # undocumented != 1
        f = lighter_rh.parse_fundings({"fundings": [
            {"timestamp": 3600, "rate": "0.0012", "direction": "long"},
            {"timestamp": 7200, "rate": "0.0030", "direction": "short"}]})
        self.assertAlmostEqual(f[0]["rate"], 0.000012)
        self.assertAlmostEqual(f[1]["rate"], -0.00003)
        self.assertEqual(lighter_rh.current_funding(f)["kind"], "last_settled_projection")

    def test_risex(self):
        raw = {"data": {"markets": [
            {"market_id": "1", "config": {"name": "BTC/USDC", "unlocked": True},
             "category": "crypto", "active": True, "mark_price": "70000",
             "current_funding_rate": "-0.0000048", "funding_interval": "3600000000000",
             "next_funding_time": "1794445200000000000"}]}}
        r = risex.parse_markets(raw)[0]
        self.assertEqual(r["funding"]["interval_s"], 3600)
        self.assertEqual(r["funding"]["next_ts"], 1794445200)
        self.assertIsNone(r["fee_taker"])  # UNKNOWN, never 0
        h = risex.parse_funding_history({"data": {"records": [
            {"funding_rate": "-0.0000038", "start_time": "1794441600000000000",
             "end_time": "1794445200000000000"}]}})
        self.assertEqual((h[0]["ts"], h[0]["interval_s"]), (1794445200, 3600))

    def test_arcus(self):
        raw = {"markets": [{"marketDisplayName": "AMD-USD", "baseAsset": "AMD",
                            "fullAssetName": "Advanced Micro Devices", "category": "EQUITIES",
                            "type": "PERPETUAL", "status": "ONLINE", "markPrice": "160",
                            "fundingRate": "0.0000125", "nextFundingAt": 1794445200,
                            "effectiveTakerFeePpm": 225, "regularTradingHours": {"x": 1}}]}
        r = arcus.parse_markets(raw)[0]
        self.assertAlmostEqual(r["fee_taker"], 0.000225)
        self.assertEqual((r["instrument"].coarse, r["instrument"].session), ("rwa", "RTH"))
        b = arcus.parse_book({"bids": [["10", "1"], ["11", "2"]], "asks": [["12", "1"]],
                              "timestamp": 1794445200000000, "lastSequenceId": 5})
        self.assertEqual((b["bids"][0][0], b["ts"], b["seq"], b["crossed"]),
                         (11.0, 1794445200.0, 5, False))

    def test_txflow_classes_and_percent_funding(self):
        tags = {"data": {"l1": [
            {"name": "TradFi", "kind": "operational", "l2Refs": [1, 2]},
            {"name": "Crypto", "kind": "operational", "l2Refs": [4, 9]}],
            "l2": [{"id": 1, "name": "Stocks"}, {"id": 2, "name": "Commodities"},
                   {"id": 4, "name": "Layer 1"}, {"id": 9, "name": "Meme"}],
            "instrumentTags": [{"instrumentId": 1, "tagIds": [4]},
                               {"instrumentId": 2, "tagIds": [1]},
                               {"instrumentId": 3, "tagIds": [2, 9]}]}}
        c = txflow.classes(tags)
        self.assertEqual((c[1][0], c[2][0], c[3][0]), ("crypto", "equity", "conflict"))
        meta = {"data": {"universe": [{"index": 1, "baseCurrency": "BTC", "quoteCurrency": "USDC"}]}}
        ctxs = {"1": {"data": {"nodeCtx": {"funding": "0.00125", "markPx": "70000",
                                           "prevFundingTime": 1794441600000,
                                           "nextFundingTime": 1794445200000}}}}
        r = txflow.parse_markets(meta, tags, ctxs)[0]
        self.assertEqual(r["funding"]["unit"], "percent")
        self.assertAlmostEqual(source.per_hour(r["funding"]), 0.0000125)

    def test_variational_doc_classes_and_quotes(self):
        raw = {"listings": [
            {"ticker": "XAU-PERP", "contract_type": "perpetual", "name": "Gold",
             "mark_price": "4000", "funding": {"last": {"interval_rate": "0.0001"},
                                               "interval_seconds": 14400},
             "quotes": {"size_1k": {"bid": "3999", "ask": "4001"}}},
            {"ticker": "XAU-SWAP", "contract_type": "swap", "mark_price": "4000"},
            {"ticker": "BTC-PERP", "contract_type": "perpetual", "name": "Bitcoin",
             "mark_price": "70000"}]}
        recs = variational.parse_markets(raw)
        self.assertEqual(recs[0]["instrument"].asset_class, "commodity")  # docs table
        self.assertEqual(recs[1]["instrument"].contract_type, "swap")
        self.assertEqual(recs[2]["instrument"].asset_class, "unknown")    # never assumed
        q = variational.quote_book(recs[0])
        self.assertEqual((q["bids"][0][0], q["asks"][0][0]), (3999.0, 4001.0))

    def test_tradexyz_categories_only_xyz(self):
        cats = tradexyz.categories([["xyz:NVDA", "stocks"], ["flx:NVDA", "crypto"]])
        self.assertEqual(cats, {"xyz:NVDA": "stocks"})

    def test_every_adapter_request_is_inside_the_scope(self):
        reqs = [lighter_rh.markets_request(), lighter_rh.book_request(1),
                lighter_rh.fundings_request(1, 0, 3600), risex.markets_request(),
                risex.book_request(1), risex.funding_history_request(1),
                arcus.markets_request(), arcus.book_request("BTC-USD"),
                txflow.meta_request(), txflow.tags_request(), txflow.ctx_request(1),
                txflow.book_request(1), variational.stats_request()]
        for method, url, body in reqs:
            host, path = url.split("//", 1)[1].split("/", 1)
            d = npol.check(host, method, "/" + path,
                           json.dumps(body).encode() if body else None)
            self.assertTrue(d.allowed, (url, body, d))
        for body in (tradexyz.categories_request(), tradexyz.candles_request("NVDA", "1h", 0, 1)):
            self.assertTrue(npol.check("api.hyperliquid.xyz", "POST", "/info",
                                       json.dumps(body).encode()).allowed)


class BudgetTests(unittest.TestCase):
    def test_window_limit_and_429_pause(self):
        t = [0.0]
        waits = []

        def sleep(s):
            waits.append(s)
            t[0] += s

        b = ratelimit.Budget(2, 10.0, clock=lambda: t[0], sleep=sleep)
        b.acquire(), b.acquire()
        self.assertGreater(b.acquire(), 0)  # third call waits for the window
        self.assertAlmostEqual(waits[-1], 10.0)
        b.rate_limited(3)
        before = t[0]
        b.acquire()
        self.assertGreaterEqual(t[0] - before, 3.0)

    def test_shared_variational_answer_is_fetched_once(self):
        calls = []
        variational._CACHE.items.clear()

        def fetch():
            calls.append(1)
            return {"listings": []}

        variational.stats(fetch)
        cached = variational.stats(fetch)[1]
        self.assertEqual((len(calls), cached), (1, True))

    def test_budgets_follow_the_docs(self):
        self.assertLessEqual(ratelimit.BUDGETS["variational"][0], 10)
        self.assertEqual(ratelimit.BUDGETS["variational"][1], 10.0)


class SourceLevelOffTests(unittest.TestCase):
    def test_base_feed_reads_no_extended_under_v2(self):
        import bot

        with mock.patch.dict(os.environ, V2_ON):
            with mock.patch.object(bot, "L", "https://api.rh.lighter.xyz/api/v1/"):
                self.assertEqual(bot.base_feed_venues(), ("Lighter",))
            with mock.patch.object(bot, "L", "https://mainnet.zklighter.elliot.ai/api/v1/"):
                with self.assertRaises(Exception):
                    bot.base_feed_venues()
        with mock.patch.dict(os.environ, {"NB_V2": "0"}):
            self.assertEqual(bot.base_feed_venues(), ("Lighter", "Extended"))

    def test_no_rfq_streams_and_no_price_discovery_under_v2(self):
        import desk_analytics
        import threading
        import wide_markets

        with mock.patch.dict(os.environ, V2_ON):
            self.assertEqual(desk_analytics.DeskEngine.select_rfq_books(object()), {})
            wm = wide_markets.WideMarkets(None, [], threading.Event())
            with mock.patch.object(wm, "_price_discovery_json",
                                   side_effect=AssertionError("network call")):
                wm.scan_price_discovery()
                with self.assertRaises(ValueError):
                    wm.lighter_core_marks(0)
            self.assertEqual(wm.price_discovery_rows, [])

    def test_connection_test_sends_nothing_for_out_of_scope(self):
        import connections

        c = connections.Connections.__new__(connections.Connections)
        c.results, c.store = {}, mock.Mock()
        with mock.patch.dict(os.environ, V2_ON), \
                mock.patch.object(c, "request", side_effect=AssertionError("network")):
            r = c.test("Extended")
        self.assertFalse(r["ok"])


class XyzClientTests(unittest.TestCase):
    META = [{"universe": [{"name": "xyz:NVDA", "szDecimals": 3, "maxLeverage": 20,
                           "deployerFeeScale": "1.0", "growthMode": "enabled"},
                          {"name": "xyz:OLD", "szDecimals": 2, "isDelisted": True}],
             "collateralToken": 0},
            [{"funding": "0.0000125", "markPx": "160.1", "oraclePx": "160.0",
              "midPx": "160.05", "dayNtlVlm": "1000000"}, {"funding": None}]]

    def transport(self, seen):
        def t(url, method, headers, body):
            obj = json.loads(body)
            seen.append(obj)
            self.assertTrue(npol.check("api.hyperliquid.xyz", method, "/info",
                                       body.encode()).allowed, obj)
            if obj["type"] == "metaAndAssetCtxs":
                return self.META
            if obj["type"] == "l2Book":
                return {"time": 1, "levels": [[{"px": "160", "sz": "2", "n": 1}],
                                              [{"px": "160.2", "sz": "1", "n": 1}]]}
            if obj["type"] == "fundingHistory":
                return [{"coin": obj["coin"], "fundingRate": "0.00001", "time": 3600000}]
            return []
        return t

    def test_ccxt_shape_from_scoped_queries(self):
        from v2.venues.tradexyz_client import XyzClient

        seen = []
        ex = XyzClient(self.transport(seen), clock=lambda: 7200.5)
        m = ex.load_markets()
        self.assertEqual(set(m), {"NVDA/USDC:USDC", "OLD/USDC:USDC"})
        self.assertFalse(m["OLD/USDC:USDC"]["active"])
        self.assertAlmostEqual(m["NVDA/USDC:USDC"]["taker"], 0.00009)
        r = ex.fetch_funding_rates(None, {"dex": "xyz"})
        self.assertEqual(set(r), {"NVDA/USDC:USDC"})  # missing funding stays out
        self.assertEqual(r["NVDA/USDC:USDC"]["nextFundingTimestamp"], 10800000)
        self.assertEqual(ex.fetch_order_book("NVDA/USDC:USDC")["asks"][0], [160.2, 1.0])
        self.assertEqual(len(ex.fetch_funding_rate_history("NVDA/USDC:USDC", 0)), 1)
        self.assertTrue(all(s.get("dex") == "xyz" or str(s.get("coin", "")).startswith("xyz:")
                            for s in seen))

    def test_wide_markets_uses_it_under_v2(self):
        import threading
        import wide_markets
        from v2.venues.tradexyz_client import XyzClient

        seen, test = [], self
        store = mock.Mock()
        store.audit.return_value = 1
        with mock.patch.dict(os.environ, V2_ON), \
                mock.patch.object(XyzClient, "fetch",
                                  lambda self, u, m="GET", h=None, b=None:
                                  test.transport(seen)(u, m, h, b)):
            wm = wide_markets.WideMarkets(store, [], threading.Event())
            ex = wm.client("tradeXYZ")
        self.assertIsInstance(ex, XyzClient)
        self.assertIn("NVDA/USDC:USDC", ex.markets)


class SourceRowsTests(unittest.TestCase):
    def test_rows_only_for_verified_known_cost_markets_and_rank_pairs_them(self):
        import wide_paper

        def rec(xid, legacy, symbol, cls, price, rate, fee, name=None):
            return dict(instrument=inst(xid, symbol, cls, price, name), legacy_venue=legacy,
                        mark=price, index=None, fee_taker=fee, fee_source="mock",
                        funding=dict(raw=rate, unit="fraction", interval_s=3600,
                                     next_ts=None, kind="observed_published"))

        records = {
            "arcus": [rec("arcus", "Arcus", "BTC-USD", "crypto", 70000, 0.00002, 0.00045)],
            "txflow": [rec("txflow", "TxFlow", "BTC", "crypto", 70010, 0.0001, 0.00045)],
            "risex": [rec("risex", "RISEx", "BTC", "crypto", 70005, 0.0, None)],
            "variational": [rec("variational", "Variational", "ADA-PERP", "unknown",
                                0.501, 0.0, 0.0, name="Cardano")],
        }
        records["txflow"].append(rec("txflow", "TxFlow", "ADA", "crypto", 0.5, 0.0, 0.00045))
        rows, groups, reasons = source.build(records, at=1000.0,
                                             received=dict.fromkeys(records, 1000.0))
        self.assertEqual(sorted((r["venue"], r["asset"]) for r in rows),
                         [("Arcus", "BTC"), ("TxFlow", "BTC")])
        self.assertEqual(reasons["risex"][source.FEE_UNKNOWN], 1)
        self.assertEqual(reasons["variational"][source.TIER_B], 1)
        cands = wide_paper.rank(rows, wide_paper.WIDE_DEFAULTS, 1000.0)
        self.assertTrue(cands)
        self.assertTrue(all({c["long"], c["short"]} == {"Arcus", "TxFlow"} for c in cands))


if __name__ == "__main__":
    unittest.main()
