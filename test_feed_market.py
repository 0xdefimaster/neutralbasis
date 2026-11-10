"""A venue whose market list failed this cycle gives a readable book error, not a bare
KeyError whose text ("'Extended'") became a rejection reason in the pilot."""
import unittest
from unittest.mock import patch

import bot
import risk_scan


def bare(cls, meta):
    f = cls.__new__(cls)
    f.meta = meta
    return f


class FeedMarketTests(unittest.TestCase):
    def test_missing_venue_is_a_named_value_error(self):
        for cls in (bot.Feed, risk_scan.FiveFeed):
            for v in ("Extended", "Lighter", "RISEx"):
                with self.assertRaises(ValueError) as cm:
                    bare(cls, {}).book(v, "UNI")
                text = str(cm.exception)
                self.assertNotIsInstance(cm.exception, KeyError)
                self.assertIn(v, text)
                self.assertIn("piyasa listesi bu turda alınamadı", text)

    def test_missing_market_is_a_named_value_error(self):
        for cls in (bot.Feed, risk_scan.FiveFeed):
            with self.assertRaises(ValueError) as cm:
                bare(cls, {"Extended": {}, "RISEx": {}}).book("Extended", "UNI")
            self.assertIn("Extended UNI", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            bare(risk_scan.FiveFeed, {"RISEx": {}}).book("RISEx", "SOL")
        self.assertIn("RISEx SOL", str(cm.exception))

    def test_funding_history_names_a_missing_venue(self):
        # The pilot recorded "'RISEx'" and "'Lighter'" as reasons from this path.
        for cls, venues in ((bot.Feed, ("Lighter", "Extended")),
                            (risk_scan.FiveFeed, ("Lighter", "RISEx", "TxFlow"))):
            for v in venues:
                with self.assertRaises(ValueError) as cm:
                    bare(cls, {}).funding_history(v, "SOL", 0)
                self.assertIn(v + ": piyasa listesi bu turda alınamadı", str(cm.exception))

    def test_refresh_keeps_other_venues_readable_until_replaced(self):
        old_rise = {"BTC": dict(price_type="trade", received=1.0, mark=1.0)}
        new_rise = {"ETH": dict(price_type="trade", received=2.0, mark=2.0)}
        f = bare(risk_scan.FiveFeed, {"RISEx": old_rise, "Lighter": {}, "Extended": {}})
        f.symbols, f.status = [], {}
        f.store = type("S", (), {"mark": lambda *a, **k: None})()
        seen = {}

        def base_markets(feed):
            feed.meta = feed.publish_markets({"Lighter": {}, "Extended": {}})
            seen["mid_refresh"] = feed.meta.get("RISEx")
            return feed.meta

        def req(url, *a, **k):
            if "rise.trade" in url:
                return {}, {}
            raise ValueError("offline")

        with patch.object(bot.Feed, "markets", base_markets), \
                patch.object(risk_scan.FiveFeed, "req", side_effect=req), \
                patch.object(risk_scan, "rise_rows", return_value=(new_rise, {})):
            f.markets()
        # During the refresh RISEx was still the last listing, never missing.
        self.assertIs(seen["mid_refresh"], old_rise)
        # Afterwards it is this refresh's listing; failed venues are not kept.
        self.assertIs(f.meta["RISEx"], new_rise)
        self.assertNotIn("Arcus", f.meta)
        self.assertNotIn("TxFlow", f.meta)

    def test_failed_listing_drops_the_venue(self):
        f = bare(risk_scan.FiveFeed, {"RISEx": {"BTC": {}}, "Lighter": {}, "Extended": {}})
        f.symbols, f.status = [], {}

        def base_markets(feed):
            feed.meta = feed.publish_markets({"Lighter": {}, "Extended": {}})
            return feed.meta

        with patch.object(bot.Feed, "markets", base_markets), \
                patch.object(risk_scan.FiveFeed, "req", side_effect=ValueError("offline")):
            f.markets()
        self.assertNotIn("RISEx", f.meta)
        self.assertFalse(f.status["RISEx"]["ok"])
        # The base feed alone keeps its own behaviour: it publishes only what it read.
        self.assertEqual(bare(bot.Feed, {"RISEx": {}}).publish_markets({"Lighter": {}}),
                         {"Lighter": {}})

    def test_present_market_is_returned(self):
        m = dict(rfq=True)
        f = bare(bot.Feed, {"Extended": {"UNI": m}})
        self.assertIs(f.market("Extended", "UNI"), m)
        # An RFQ market still refuses its indicative book, as before.
        with self.assertRaises(ValueError):
            f.book("Extended", "UNI")


if __name__ == "__main__":
    unittest.main()
