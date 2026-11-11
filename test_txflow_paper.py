"""TxFlow in PAPER (owner decision 2026-11-11): documented funding model, an optional
waiver of the funding-evidence blocks that need realized TxFlow payments (no public
market-wide history exists), and an optional full TxFlow universe. MOCK inputs."""
import os
import unittest
from unittest.mock import patch

import capabilities
import risk_scan
import wide_paper


class TxFlowPaperTests(unittest.TestCase):
    def test_documented_model_is_periodic_hourly_long_pays(self):
        m = capabilities.funding_model("TxFlow")
        self.assertEqual(m["model"], capabilities.PERIODIC)
        self.assertEqual(m["direction"], capabilities.LONG_PAYS)
        self.assertEqual(m["price"], "Mark fiyatı")
        self.assertIn("docs.txflow.com", m["source"])
        self.assertIn("geçmişi yok", m["history"])

    def test_waiver_lifts_only_payment_evidence_of_txflow(self):
        reasons = [
            "TxFlow: Gerçekleşmiş funding yok",
            "TxFlow: Funding aralığı doğrulanmadı",
            "TxFlow: funding aralığı bu piyasada doğrulanmadı",
            "TxFlow: yayımlanan oran ile gerçekleşen ödeme karşılaştırması yetersiz · 0/3",
            "TxFlow: 30 günlük geçmiş kaydı yok",
            "TxFlow: ücret bilinmiyor",
            "TxFlow: piyasa verisi eski",
            "Extended: yayımlanan oran ile gerçekleşen ödeme karşılaştırması yetersiz · 0/3",
            "Extended: Funding işareti doğrulanmadı",
        ]
        self.assertEqual(
            wide_paper.txflow_waived(reasons),
            [
                "TxFlow: ücret bilinmiyor",
                "TxFlow: piyasa verisi eski",
                "Extended: yayımlanan oran ile gerçekleşen ödeme karşılaştırması yetersiz · 0/3",
                "Extended: Funding işareti doğrulanmadı",
            ],
        )

    def test_waiver_off_by_default_and_a_bool_setting(self):
        self.assertFalse(wide_paper.WIDE_DEFAULTS["wide_txflow_paper"])
        self.assertIn("wide_txflow_paper", wide_paper.BOOLS)

    def test_full_universe_only_with_the_env_switch(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NB_TXFLOW_ALL", None)
            self.assertFalse(risk_scan.txflow_all())
            os.environ["NB_TXFLOW_ALL"] = "1"
            self.assertTrue(risk_scan.txflow_all())
            os.environ["NB_TXFLOW_ALL"] = "yes"
            self.assertFalse(risk_scan.txflow_all())


class RisexRwaTests(unittest.TestCase):
    def rows(self, env):
        o = {"data": {"markets": [
            dict(market_id="7", underlying="DRAM/USDC", active=True, reduce_only=False,
                 category="stocks", mark_price="44.0", index_price="44.0",
                 current_funding_rate="0.0001", funding_interval="3600000000000",
                 next_funding_time="1794330000000000000",
                 config=dict(unlocked=True, step_size="0.01", min_order_size="0.01",
                             max_leverage="20")),
        ]}}

        class Feed:
            def generic(self, v, a, *args, **kw):
                return dict(venue=v, asset=a, **kw)

        f = Feed()
        with patch.dict(os.environ, env, clear=False):
            if not env:
                os.environ.pop("NB_RISEX_RWA", None)
            out, skipped = risk_scan.rise_rows(f, o, {})
        return f, out

    def test_off_by_default_ticker_still_remembered(self):
        f, out = self.rows({})
        self.assertEqual(out, {})
        self.assertIn("DRAM", f.rwa_tickers)

    def test_switch_puts_the_market_in_the_feed_as_rwa(self):
        f, out = self.rows({"NB_RISEX_RWA": "1"})
        self.assertIn("DRAM", out)
        self.assertIn("DRAM", f.rwa_tickers)


if __name__ == "__main__":
    unittest.main()
