"""A venue whose market list failed this cycle gives a readable book error, not a bare
KeyError whose text ("'Extended'") became a rejection reason in the pilot."""
import unittest

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

    def test_present_market_is_returned(self):
        m = dict(rfq=True)
        f = bare(bot.Feed, {"Extended": {"UNI": m}})
        self.assertIs(f.market("Extended", "UNI"), m)
        # An RFQ market still refuses its indicative book, as before.
        with self.assertRaises(ValueError):
            f.book("Extended", "UNI")


if __name__ == "__main__":
    unittest.main()
