"""V2 venue policy, egress scope, identity gate, discovery and fee records.

Hosts, payloads and instruments below are MOCK fixtures (shaped after the venues'
documented requests), not market evidence. No network access."""
import json
import unittest

from pilot import network_guard
from v2 import discovery, fees, identity, network_policy as npol, policy
from v2.identity import Instrument


class UniverseTests(unittest.TestCase):
    def test_exactly_eight_venues(self):
        self.assertEqual(len(policy.EXCHANGE_IDS), 8)
        self.assertEqual(len(set(policy.EXCHANGE_IDS)), 8)
        self.assertEqual(policy.exchange_id("Lighter"), "lighter_rh")
        self.assertIsNone(policy.exchange_id("LighterCore"))  # RH is not Core
        self.assertEqual(policy.exchange_id("tradeXYZ"), "tradexyz")

    def test_legacy_and_cex_cannot_enter(self):
        for name in policy.LEGACY_OUT_OF_SCOPE + policy.CEX:
            self.assertIsNone(policy.exchange_id(name), name)
            with self.assertRaises(policy.PolicyError):
                policy.require_in_scope(["RISEx", name])
        kept, dropped = policy.filter_config_venues(
            {"RISEx": 1000, "Extended": 1000, "Binance": 1, "tradeXYZ": 5})
        self.assertEqual(set(kept), {"RISEx", "tradeXYZ"})
        self.assertEqual(dropped, ["Binance", "Extended"])

    def test_catalog_keeps_only_the_eight(self):
        import copy

        import wide_markets

        cat = copy.deepcopy(wide_markets.CATALOG)
        off = policy.restrict_catalog(cat)
        live = {v for v, s in cat.items() if s.get("scan") or s.get("adapter")}
        self.assertTrue(live <= {n for n in policy.LEGACY_NAME.values() if n}, live)
        for cex in ("Binance", "Bybit", "OKX", "Bitget", "KuCoin", "Gate", "HTX", "MEXC",
                    "BingX", "Deribit", "Phemex", "BloFin", "KrakenFutures"):
            self.assertIn(cex, off)
        for legacy in ("Hyperliquid", "LighterCore", "Aster", "Nado", "ApeX", "Hibachi"):
            self.assertIn(legacy, off)

    def test_v2_switch_is_fail_closed(self):
        self.assertFalse(policy.v2_enabled({}))
        self.assertTrue(policy.v2_enabled({"NB_V2": "1"}))
        with self.assertRaises(policy.PolicyError):
            policy.v2_enabled({"NB_V2": "yes"})
        with self.assertRaises(policy.PolicyError):
            policy.check_environment({"NB_V2": "1", "NB_EXTRA_PERP_DEXES": "Aster,tradeXYZ"})
        with self.assertRaises(policy.PolicyError):
            policy.check_environment({"NB_V2": "1", "NB_RFQ_STREAMS": "40"})
        self.assertEqual(policy.check_environment(
            {"NB_V2": "1", "NB_EXTRA_PERP_DEXES": "tradeXYZ"}), [])


def post(obj):
    return json.dumps(obj).encode()


class EgressTests(unittest.TestCase):
    HL = "api.hyperliquid.xyz"

    def ok(self, *a):
        d = npol.check(*a)
        self.assertTrue(d.allowed, d)
        return d

    def no(self, *a):
        d = npol.check(*a)
        self.assertFalse(d.allowed, d)
        return d

    def test_hyperliquid_only_inside_the_xyz_dex(self):
        self.assertEqual(self.ok(self.HL, "POST", "/info",
                                 post({"type": "metaAndAssetCtxs", "dex": "xyz"})).exchange_id,
                         "tradexyz")
        self.ok(self.HL, "POST", "/info", post({"type": "meta", "dex": "xyz"}))
        self.ok(self.HL, "POST", "/info", post({"type": "l2Book", "coin": "xyz:NVDA"}))
        self.ok(self.HL, "POST", "/info", post({"type": "fundingHistory", "coin": "xyz:TSLA",
                                                "startTime": 0}))
        self.ok(self.HL, "POST", "/info", post({"type": "candleSnapshot",
                                                "req": {"coin": "xyz:NVDA"}}))
        self.assertEqual(self.ok(self.HL, "POST", "/info", post({"type": "perpDexs"})).flag,
                         npol.BROAD)
        # Native perps, the default dex, other HIP-3 dexes, first-dex-only queries.
        self.no(self.HL, "POST", "/info", post({"type": "metaAndAssetCtxs"}))
        self.no(self.HL, "POST", "/info", post({"type": "metaAndAssetCtxs", "dex": ""}))
        self.no(self.HL, "POST", "/info", post({"type": "meta", "dex": "flx"}))
        self.no(self.HL, "POST", "/info", post({"type": "l2Book", "coin": "BTC"}))
        self.no(self.HL, "POST", "/info", post({"type": "l2Book", "coin": "flx:XYZ"}))
        self.no(self.HL, "POST", "/info", post({"type": "fundingHistory", "coin": "NVDA"}))
        self.no(self.HL, "POST", "/info", post({"type": "predictedFundings"}))
        self.no(self.HL, "POST", "/info", post({"type": "allMids", "dex": "xyz"}))
        self.no(self.HL, "POST", "/info", post({"type": "clearinghouseState", "dex": "xyz",
                                                "user": "0x0"}))
        self.no(self.HL, "POST", "/exchange", post({"action": {}}))
        self.no(self.HL, "GET", "/info")
        self.no(self.HL, "POST", "/info", b"not json")

    def test_txflow_public_reads_only(self):
        h = "rpc.txflow.com"
        self.ok(h, "POST", "/info", post({"type": "activeAssetCtx", "coin": "1"}))
        self.ok(h, "POST", "/info", post({"type": "feeTierRates", "accountType": "perp"}))
        self.no(h, "POST", "/info", post({"type": "feeTierRates", "accountType": "perp",
                                          "user": "0xabc"}))
        self.no(h, "POST", "/info", post({"type": "userFundings", "user": "0xabc"}))
        self.no(h, "POST", "/exchange", post({"type": "order"}))

    def test_get_venues_paths(self):
        self.ok("api.rh.lighter.xyz", "GET", "/api/v1/fundings?market_id=1")
        self.no("api.rh.lighter.xyz", "POST", "/api/v1/sendTx")
        self.no("api.rh.lighter.xyz", "GET", "/api/v1/account")
        self.ok("api.rise.trade", "GET", "/v1/markets/id/3/funding-rate-history")
        self.no("api.rise.trade", "GET", "/v1/user/fees")
        self.ok("api.arcus.xyz", "GET", "/v1/fundingRates")
        self.no("api.arcus.xyz", "GET", "/v1/funding")  # account funding payments
        self.ok("omni-client-api.prod.ap-northeast-1.variational.io", "GET",
                "/metadata/v2/stats")

    def test_legacy_and_cex_hosts_are_refused(self):
        legacy = ("mainnet.zklighter.elliot.ai", "api.starknet.extended.exchange",
                  "api.prod.paradex.trade", "fapi.asterdex.com", "api.pacifica.fi",
                  "gateway.prod.nado.xyz", "data-api.hibachi.xyz", "omni.apex.exchange")
        cex = ("api.binance.com", "fapi.binance.com", "api.bybit.com", "www.okx.com",
               "api.bitget.com", "api.kucoin.com", "futures.kraken.com", "api.gateio.ws",
               "api.huobi.pro", "contract.mexc.com", "open-api.bingx.com",
               "www.deribit.com", "api.phemex.com", "openapi.blofin.com",
               "api.exchange.coinbase.com")
        for h in legacy + cex:
            self.assertNotIn(h, npol.allowed_hosts())
            self.no(h, "GET", "/api/v3/ticker")

    def test_every_ccxt_cex_host_is_outside_the_allowlist(self):
        try:
            import ccxt
        except ImportError:
            self.skipTest("ccxt not installed")
        from urllib.parse import urlparse

        allowed = npol.allowed_hosts()
        for name in ("binance", "binanceusdm", "bybit", "okx", "bitget", "kucoin",
                     "krakenfutures", "gate", "htx", "mexc", "bingx", "deribit", "phemex",
                     "blofin", "coinbase"):
            ex = getattr(ccxt, name)()
            urls = []

            def walk(x):
                if isinstance(x, dict):
                    for v in x.values():
                        walk(v)
                elif isinstance(x, str) and x.startswith("http"):
                    urls.append(x)

            walk(ex.urls.get("api"))
            for u in urls:
                self.assertNotIn(urlparse(u.replace("{hostname}", "x")).hostname, allowed,
                                 (name, u))


class FakeConn:
    def __init__(self, host):
        self.host = host


class GuardV2Tests(unittest.TestCase):
    def guard(self):
        return network_guard.Guard(v2=True, sleep=lambda s: None)

    def test_body_sent_with_or_after_the_headers_is_checked(self):
        g = self.guard()
        hl = FakeConn("api.hyperliquid.xyz")
        head = b"POST /info HTTP/1.1\r\nHost: api.hyperliquid.xyz\r\n\r\n"
        g("http.client.send", (hl, head + post({"type": "meta", "dex": "xyz"})))
        g("http.client.send", (hl, head))  # urllib3: body in a later chunk
        g("http.client.send", (hl, post({"type": "l2Book", "coin": "xyz:NVDA"})))
        g("http.client.send", (hl, head))
        with self.assertRaises(network_guard.Refused):
            g("http.client.send", (hl, post({"type": "metaAndAssetCtxs"})))
        self.assertEqual(g.by_venue.get("tradexyz"), 2)

    def test_dns_and_unchecked_body_are_fail_closed(self):
        g = self.guard()
        with self.assertRaises(network_guard.Refused):
            g("socket.getaddrinfo", ("fapi.binance.com", 443))
        with self.assertRaises(network_guard.Refused):
            g("socket.getaddrinfo", ("api.starknet.extended.exchange", 443))
        g("socket.getaddrinfo", ("api.hyperliquid.xyz", 443))
        c = FakeConn("rpc.txflow.com")
        head = b"POST /info HTTP/1.1\r\nHost: rpc.txflow.com\r\n\r\n"
        g("http.client.send", (c, head))
        with self.assertRaises(network_guard.Refused):
            g("http.client.send", (c, head))  # second request before the first body

    def test_legacy_mode_is_unchanged_by_default(self):
        g = network_guard.Guard(sleep=lambda s: None)
        g("socket.getaddrinfo", ("api.starknet.extended.exchange", 443))


def inst(xid, symbol, cls="crypto", price=100.0, **kw):
    # MOCK price: the group identity check needs one per market.
    return Instrument(exchange_id=xid, market_id=symbol, symbol=symbol, asset_class=cls,
                      price=price, class_source=None if cls == "unknown" else "mock", **kw)


class IdentityTests(unittest.TestCase):
    def test_multiplier_prefixes(self):
        self.assertEqual(identity.parse_symbol("1000PEPE"), ("PEPE", 1000))
        self.assertEqual(identity.parse_symbol("kPEPE"), ("PEPE", 1000))
        self.assertEqual(identity.parse_symbol("xyz:NVDA"), ("NVDA", 1))
        self.assertEqual(identity.parse_symbol("BTC-PERP"), ("BTC", 1))
        self.assertEqual(identity.parse_symbol("BTCUSDT"), ("BTC", 1))
        self.assertEqual(identity.parse_symbol("1INCH"), ("1INCH", 1))
        self.assertEqual(identity.parse_symbol("5000X"), ("5000X", None))

    def test_ticker_match_is_not_identity(self):
        # RWA ticker on two venues: no registry evidence -> unverified.
        a, b = inst("tradexyz", "xyz:NVDA", "equity"), inst("lighter_rh", "NVDA", "equity")
        for x in (a, b):
            identity.resolve(x)
        ok, why = identity.equivalent(a, b)
        self.assertFalse(ok)
        # Known conflicts never match, even as "crypto".
        q1, q2 = inst("variational", "QNT-PERP"), inst("arcus", "QNT-USD")
        for x in (q1, q2):
            identity.resolve(x)
        self.assertFalse(identity.equivalent(q1, q2)[0])
        self.assertIn("CONFLICT", q1.evidence[0])
        an = inst("variational", "ANTHROPIC-PERP", "preipo")
        identity.resolve(an)
        self.assertEqual(an.status, identity.UNVERIFIED)

    def test_crypto_with_multiplier_and_swap_vs_perp(self):
        a, b = inst("lighter_rh", "1000PEPE"), inst("tradexyz", "kPEPE")
        for x in (a, b):
            identity.resolve(x)
        self.assertTrue(identity.equivalent(a, b)[0])
        self.assertEqual((a.multiplier, b.multiplier), (1000, 1000))
        s = inst("variational", "BTC-SWAP", contract_type="swap")
        p = inst("arcus", "BTC-USD")
        for x in (s, p):
            identity.resolve(x)
        self.assertFalse(identity.equivalent(s, p)[0])

    def test_registry_is_the_only_way_for_rwa(self):
        a, b = inst("tradexyz", "xyz:NVDA", "equity"), inst("arcus", "NVDA-USD", "equity")
        reg = {("tradexyz", "xyz:NVDA"): "equity:NVDA.US", ("arcus", "NVDA-USD"): "equity:NVDA.US"}
        for x in (a, b):
            identity.resolve(x, reg)
        self.assertTrue(identity.equivalent(a, b)[0])

    def test_price_is_a_check_not_proof(self):
        self.assertTrue(identity.price_consistent(0.012, 1000, 0.0000121, 1))
        self.assertFalse(identity.price_consistent(178.2, 1, 40.33, 1))


class DiscoveryTests(unittest.TestCase):
    def catalog(self):
        return {
            "risex": [inst("risex", "BTC"), inst("risex", "ONLYRISE")],
            "arcus": [inst("arcus", "BTC-USD"), inst("arcus", "NVDA-USD", "equity")],
            "txflow": [inst("txflow", "ETH")],
        }

    def test_single_venue_markets_get_no_streams_or_pairs(self):
        g = discovery.intersect(self.catalog())
        self.assertEqual(g["crypto:BTC"]["status"], discovery.MULTI)
        self.assertEqual(g["unverified:txflow:ETH"]["status"], discovery.SINGLE)
        s = discovery.summary(g)
        self.assertEqual((s["MULTI_VENUE"], s["venue_pairs"]), (1, 1))
        m = discovery.SubscriptionManager()
        opened, closed = m.apply(g)
        self.assertEqual({(x, c) for x, _, c in opened if c == "book"},
                         {("risex", "book"), ("arcus", "book")})
        self.assertFalse(any(mid in ("ETH", "ONLYRISE", "NVDA-USD") for _, mid, _ in opened))
        self.assertEqual(m.apply(g), ([], []))  # idempotent

    def test_second_listing_opens_and_delisting_closes_except_holds(self):
        cat = self.catalog()
        m = discovery.SubscriptionManager()
        m.apply(discovery.intersect(cat))
        cat["arcus"].append(inst("arcus", "ETH-USD"))
        opened, _ = m.apply(discovery.intersect(cat))
        self.assertIn(("txflow", "ETH", "book"), opened)
        m.hold("txflow", "ETH", "PAPER position")
        cat["arcus"] = [x for x in cat["arcus"] if x.symbol != "ETH-USD"]
        _, closed = m.apply(discovery.intersect(cat))
        self.assertNotIn(("txflow", "ETH", "book"), closed)  # held
        self.assertIn(("arcus", "ETH-USD", "book"), closed)
        m.release("txflow", "ETH", "PAPER position")
        _, closed = m.apply(discovery.intersect(cat))
        self.assertIn(("txflow", "ETH", "book"), closed)

    def test_pair_counts(self):
        self.assertEqual(len(discovery.pairs(range(3))[0]), 3)
        self.assertEqual(len(discovery.pairs(range(4))[0]), 6)
        u, d = discovery.pairs(policy.EXCHANGE_IDS)
        self.assertEqual((len(u), len(d)), (28, 56))

    def test_light_refresh_respects_ttl_and_scope(self):
        t = [0.0]
        calls = []

        def fetch(xid):
            calls.append(xid)
            return [inst(xid, "BTC")]

        d = discovery.LightweightDiscovery(fetch, ttl=100, jitter=0, clock=lambda: t[0])
        d.refresh(("risex", "arcus"))
        d.refresh(("risex", "arcus"))
        self.assertEqual(calls, ["risex", "arcus"])
        t[0] = 101
        d.refresh(("risex",))
        self.assertEqual(calls[-1], "risex")
        with self.assertRaises(ValueError):
            d.refresh(("Binance",))


class FeeTests(unittest.TestCase):
    def test_units(self):
        self.assertAlmostEqual(fees.to_fraction(450, "ppm"), 0.00045)
        self.assertAlmostEqual(fees.to_fraction(0.01, "percent"), 0.0001)
        self.assertAlmostEqual(fees.to_fraction(4.5, "bp"), 0.00045)
        with self.assertRaises(ValueError):
            fees.to_fraction(1, "per-mille?")
        self.assertEqual(fees.to_seconds(1_700_000_000_000, "ms"), 1_700_000_000)
        self.assertAlmostEqual(fees.to_seconds(1_700_000_000_000_000_000, "ns"), 1.7e9)
        with self.assertRaises(ValueError):
            fees.to_seconds(1, None)

    def test_unknown_is_never_zero(self):
        for xid in ("risex", "popdex_perps", "veranta", "tradexyz"):
            with self.assertRaises(fees.FeeUnknown):
                fees.taker(xid)
        self.assertEqual(fees.taker("variational"), 0.0)  # documented zero, spread apart
        self.assertAlmostEqual(fees.taker("arcus", "Base (crypto)"), 0.00045)
        self.assertAlmostEqual(fees.taker("txflow"), 0.00045)
        with self.assertRaises(fees.FeeUnknown):
            fees.four_leg_fees(1000, fees.taker("txflow"), None)
        f = fees.four_leg_fees(1000, 0.00045, 0.00035)
        self.assertAlmostEqual(f["total"], 1.6)

    def test_hip3_fee_scale(self):
        self.assertAlmostEqual(fees.hip3_scale(0), 1)
        self.assertAlmostEqual(fees.hip3_scale(0.5), 1.5)
        self.assertAlmostEqual(fees.hip3_scale(3), 6)
        self.assertAlmostEqual(fees.hip3_scale(0.5, growth_mode=True), 0.15)
        self.assertIsNone(fees.hip3_scale(None))


if __name__ == "__main__":
    unittest.main()


class TradexyzAdapterTests(unittest.TestCase):
    # MOCK answers shaped like Hyperliquid metaAndAssetCtxs (dex xyz) and perpDexs.
    META = [{"universe": [
        {"name": "xyz:XYZ100", "szDecimals": 4, "maxLeverage": 30, "growthMode": "enabled",
         "deployerFeeScale": "1.0"},
        {"name": "xyz:NVDA", "szDecimals": 3, "maxLeverage": 20, "deployerFeeScale": "1.0"},
    ]}, [{"funding": "0.00000625", "markPx": "28103.0", "oraclePx": "28109.0"},
         {"funding": None, "markPx": "190.1"}]]

    def test_every_built_request_passes_the_scope(self):
        from v2.venues import tradexyz as tx

        for body in (tx.meta_and_ctxs(), tx.l2_book("NVDA"), tx.l2_book("xyz:NVDA"),
                     tx.funding_history("TSLA", 0, 1)):
            d = npol.check("api.hyperliquid.xyz", "POST", "/info", post(body))
            self.assertTrue(d.allowed, (body, d))

    def test_parse_fees_and_missing_values(self):
        from v2.venues import tradexyz as tx

        rows = tx.parse_markets(self.META)
        self.assertAlmostEqual(rows[0]["taker"], 0.00009)  # growth: 0.045% x 2 x 0.1
        self.assertAlmostEqual(rows[1]["taker"], 0.0009)   # 0.045% x 2
        self.assertIsNone(rows[1]["funding_raw"])          # never defaulted to 0
        self.assertIsNone(rows[1]["oracle"])
        bad = [{"universe": [{"name": "BTC"}]}, [{}]]
        with self.assertRaises(ValueError):
            tx.parse_markets(bad)

    def test_perp_dexs_consumes_only_xyz(self):
        from v2.venues import tradexyz as tx

        ans = [None, {"name": "flx", "assetToFundingMultiplier": [["flx:A", "9"]]},
               {"name": "xyz", "assetToFundingMultiplier": [["xyz:AAPL", "0.5"],
                                                            ["xyz:ANT", "0.005"]]}]
        self.assertEqual(tx.funding_multipliers(ans), {"xyz:AAPL": 0.5, "xyz:ANT": 0.005})
