"""Lighter RH and Extended market listings: field-level isolation.

Fixtures in testdata/ are RECORDED REAL public responses (trimmed to a few markets,
records unmodified). Tests that break a field do so on a copy and are labelled MOCK
mutation; they are not market evidence. No network and no database: `get` is stubbed
and only the two listing URLs may be requested."""

import copy, json, os, types, unittest
from unittest import mock
import bot

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name):
    with open(os.path.join(HERE, "testdata", name), encoding="utf8") as f:
        return json.load(f)["response"]


LIGHTER = load("lighter_rh_orderBookDetails_20261109.json")
EXTENDED = load("extended_info_markets_20261109.json")
LIGHTER_BTC_FUNDINGS = load("lighter_rh_fundings_btc_20261109.json")
# 15 minutes after the last recorded BTC settlement (1794240000): the time the
# recorded data was current, so the engine's own freshness checks apply as they did.
RECORDED_AT = 1794240000 + 900


class FakeStore:
    def __init__(self):
        self.rows = []

    def marks(self, rows):
        self.rows += rows


class ListingFeed(bot.Feed):
    def __init__(self, lighter=LIGHTER, extended=EXTENDED):
        super().__init__(FakeStore())
        self.listings = dict(lighter=lighter, extended=extended)
        self.urls = []

    def get(self, url, max_age=None):
        self.urls.append(url)
        info = dict(audit_id=None, received=bot.now(), http_age=0)
        if url.endswith("orderBookDetails"):
            return copy.deepcopy(self.listings["lighter"]), info
        if url.endswith("info/markets"):
            return copy.deepcopy(self.listings["extended"]), info
        if "fundings?" in url and "market_id=1&" in url:
            return copy.deepcopy(LIGHTER_BTC_FUNDINGS), info
        raise AssertionError("Beklenmeyen ağ isteği: " + url)


def lighter_market(o, symbol):
    return next(d for d in o["order_book_details"] if d["symbol"] == symbol)


def extended_stats(o, asset):
    return next(d for d in o["data"] if d["assetName"] == asset)["marketStats"]


class LighterRhListingTests(unittest.TestCase):
    def test_real_response_without_open_interest_usd_keeps_the_venue(self):
        f = ListingFeed()
        meta = f.markets()
        self.assertEqual(sorted(meta["Lighter"]), ["BTC", "ETH", "HYPE", "SOL", "XRP"])
        self.assertTrue(f.status["Lighter"]["ok"])
        self.assertEqual(f.status["Lighter"]["skipped_markets"], 0)
        for m in meta["Lighter"].values():
            self.assertIsNone(m["oi"])  # UNKNOWN, not 0 and not converted
            self.assertIsInstance(m["volume"], float)
        btc = meta["Lighter"]["BTC"]
        self.assertEqual(btc["id"], 1)
        self.assertGreater(btc["mark"], 0)
        # Only the two listings were requested.
        self.assertEqual(len(f.urls), 2)

    def test_base_unit_open_interest_is_not_converted(self):
        f = ListingFeed()
        meta = f.markets()
        raw = lighter_market(LIGHTER, "BTC")
        self.assertIn("open_interest", raw)  # present, in base units
        self.assertIsNone(meta["Lighter"]["BTC"]["oi"])

    def test_open_interest_usd_is_used_when_the_venue_sends_it(self):
        # MOCK mutation of a recorded record: the venue sends open_interest_usd.
        o = copy.deepcopy(LIGHTER)
        lighter_market(o, "ETH")["open_interest_usd"] = "123.5"
        meta = ListingFeed(lighter=o).markets()
        self.assertEqual(meta["Lighter"]["ETH"]["oi"], 123.5)
        self.assertIsNone(meta["Lighter"]["BTC"]["oi"])

    def test_one_malformed_market_is_skipped_not_the_venue(self):
        # MOCK mutation: one market loses a required price.
        o = copy.deepcopy(LIGHTER)
        lighter_market(o, "BTC")["mark_price"] = None
        f = ListingFeed(lighter=o)
        meta = f.markets()
        self.assertNotIn("BTC", meta["Lighter"])
        self.assertEqual(sorted(meta["Lighter"]), ["ETH", "HYPE", "SOL", "XRP"])
        self.assertTrue(f.status["Lighter"]["ok"])
        self.assertEqual(f.status["Lighter"]["skipped_markets"], 1)
        self.assertIn("atlandı", f.status["Lighter"]["message"])
        self.assertIn("BTC", f.market_errors["Lighter"])
        # Extended is unaffected.
        self.assertTrue(f.status["Extended"]["ok"])

    def test_every_market_malformed_reports_the_venue_down(self):
        # MOCK mutation: every market loses its index price.
        o = copy.deepcopy(LIGHTER)
        for d in o["order_book_details"]:
            del d["index_price"]
        f = ListingFeed(lighter=o)
        meta = f.markets()
        self.assertNotIn("Lighter", meta)
        self.assertFalse(f.status["Lighter"]["ok"])
        self.assertIn("hiçbir piyasa", f.status["Lighter"]["message"])
        self.assertTrue(f.status["Extended"]["ok"])

    def test_a_null_market_config_does_not_crash(self):
        # MOCK mutation: market_config sent as null.
        o = copy.deepcopy(LIGHTER)
        lighter_market(o, "SOL")["market_config"] = None
        meta = ListingFeed(lighter=o).markets()
        self.assertIn("SOL", meta["Lighter"])


class ExtendedListingTests(unittest.TestCase):
    def test_real_response_parses(self):
        f = ListingFeed()
        meta = f.markets()
        self.assertEqual(sorted(meta["Extended"]), ["AIA", "BTC", "ENA", "ETH"])
        self.assertTrue(meta["Extended"]["AIA"]["rfq"])
        for m in meta["Extended"].values():
            self.assertIsInstance(m["oi"], float)
            self.assertIsInstance(m["volume"], float)
            self.assertIsInstance(m["rate"], float)

    def test_missing_optional_stats_stay_unknown(self):
        # MOCK mutation: volume and open interest missing for one market.
        o = copy.deepcopy(EXTENDED)
        s = extended_stats(o, "ENA")
        del s["openInterest"]
        s["dailyVolume"] = ""
        meta = ListingFeed(extended=o).markets()
        self.assertIsNone(meta["Extended"]["ENA"]["oi"])
        self.assertIsNone(meta["Extended"]["ENA"]["volume"])
        self.assertIsInstance(meta["Extended"]["BTC"]["oi"], float)

    def test_missing_funding_rate_skips_only_that_market(self):
        # MOCK mutation: a required field (funding rate) missing for one market.
        o = copy.deepcopy(EXTENDED)
        del extended_stats(o, "BTC")["fundingRate"]
        f = ListingFeed(extended=o)
        meta = f.markets()
        self.assertNotIn("BTC", meta["Extended"])
        self.assertEqual(sorted(meta["Extended"]), ["AIA", "ENA", "ETH"])
        self.assertEqual(f.status["Extended"]["skipped_markets"], 1)
        self.assertTrue(f.status["Lighter"]["ok"])


class FakeWide:
    """No CCXT venues in this test: only the native feed's rows are ranked."""

    def snapshot(self):
        return [], {}

    def universe_snapshot(self):
        return {}


class FakeEngine:
    def __init__(self, feed):
        self.feed = feed
        self.cfg = dict(bot.DEFAULTS)
        self.wide = FakeWide()
        self.wide_paper = types.SimpleNamespace(positions=[])

    def fee(self, v):
        import risk_scan

        # The engine's real fee rule (configured Lighter/Extended taker fees).
        return risk_scan.RiskEngine.fee(self, v)


class ScanChainTests(unittest.TestCase):
    """Feed.markets -> rate hydration -> Connectors.market_rows -> usable_rows, on
    recorded real responses. Funding evidence gates are not involved or relaxed:
    usable_rows only checks that a row is fresh and fully specified."""

    def run_chain(self):
        import connectors, wide_paper

        with mock.patch.object(bot, "now", lambda: RECORDED_AT):
            feed = ListingFeed()
            feed.arcus_fee = None
            feed.markets()
            # The hydration the RH feed runs for its selected symbols (rh_rates).
            feed.funding_history("Lighter", "BTC", RECORDED_AT - 4 * 3600)
            rows = connectors.Connectors(FakeEngine(feed)).market_rows()
            why = {}
            groups = wide_paper.usable_rows(rows, RECORDED_AT, why)
        return feed, rows, groups, why

    def test_lighter_rh_btc_reaches_the_scan_with_the_recorded_settlement(self):
        feed, rows, groups, why = self.run_chain()
        row = next(r for r in rows if r["venue"] == "Lighter" and r["asset"] == "BTC")
        # 0.0012 % per hour, direction long -> +1.2e-5 (the engine's sign rule).
        self.assertAlmostEqual(row["rate"], 1.2e-5)
        self.assertAlmostEqual(row["raw_rate"], 1.2e-5)
        self.assertEqual(row["interval"], 3600)
        self.assertEqual(row["fee"], bot.DEFAULTS["lighter_fee"])
        self.assertIsNone(row["oi"])
        self.assertEqual(
            feed.meta["Lighter"]["BTC"]["funding_kind"], "last_settled_projection"
        )
        self.assertIn("BTC", groups)
        self.assertEqual(sorted(groups["BTC"]), ["Extended", "Lighter"])

    def test_markets_without_a_settled_rate_are_not_given_one(self):
        feed, rows, groups, why = self.run_chain()
        # ETH, SOL, XRP, HYPE on Lighter RH: listed, but no recorded settlement was
        # hydrated, so they carry no rate and stay out of the scan.
        lighter = {r["asset"] for r in rows if r["venue"] == "Lighter"}
        self.assertEqual(lighter, {"BTC"})
        for a in ("ETH", "SOL", "XRP", "HYPE"):
            self.assertIsNone(feed.meta["Lighter"][a]["rate"])
            self.assertNotIn("Lighter", groups.get(a, {}))

    def test_the_old_parser_would_have_dropped_lighter_entirely(self):
        # Guard against a regression to the venue-wide failure seen on 2026-11-09.
        feed, rows, groups, why = self.run_chain()
        self.assertTrue(feed.status["Lighter"]["ok"])
        self.assertEqual(feed.status["Lighter"]["skipped_markets"], 0)


class OptionalNumberTests(unittest.TestCase):
    def test_values(self):
        self.assertIsNone(bot.optional_number(None))
        self.assertIsNone(bot.optional_number(""))
        self.assertIsNone(bot.optional_number("abc"))
        self.assertIsNone(bot.optional_number("nan"))
        self.assertIsNone(bot.optional_number("inf"))
        self.assertEqual(bot.optional_number("0"), 0.0)  # a real zero stays zero
        self.assertEqual(bot.optional_number("12.5"), 12.5)


if __name__ == "__main__":
    unittest.main()
