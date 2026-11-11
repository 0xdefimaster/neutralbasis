"""Observed published funding recorder: offline checks (MOCK responses, labelled)."""
import sqlite3
import unittest
from unittest import mock

from tools import funding_observer as fo

# MOCK: shaped like TxFlow perpMeta / activeAssetCtx answers; not market evidence.
MOCK_META = {"success": True, "data": {"universe": [
    {"baseCurrency": "BTC", "index": 1},
    {"baseCurrency": "OLD", "index": 2, "delisted": True},
]}}
MOCK_CTX = {"success": True, "data": {"nodeCtx": {
    "funding": "0.0012", "markPx": "74000", "oraclePx": "74010",
    "prevFundingTime": 1794427200000, "nextFundingTime": 1794430800000,
    "blockTime": 1794429000000}}}


class ObserverTests(unittest.TestCase):
    def test_txflow_rows_are_published_rates_with_their_unit(self):
        def fake_post(url, body, timeout=15):
            return MOCK_META if body["type"] == "perpMeta" else MOCK_CTX

        with mock.patch.object(fo, "post", fake_post):
            rows, errors = fo.txflow_round()
        self.assertEqual(errors, 0)
        self.assertEqual([r["asset"] for r in rows], ["BTC"])  # delisted skipped
        r = rows[0]
        self.assertEqual(r["rate_raw"], 0.0012)
        self.assertEqual(r["rate_unit"], "percent per payment")
        self.assertEqual(r["interval_s"], 3600)
        self.assertEqual(r["source_ts"], 1794429000)

    def test_stored_rows_carry_the_observed_label_only(self):
        db = sqlite3.connect(":memory:")
        db.executescript(fo.SCHEMA)
        row = dict(venue="TxFlow", asset="BTC", observed=1.0, source_ts=None, rate_raw=None,
                   rate_unit="percent per payment", interval_s=None, next_ts=None,
                   mark=None, index_px=None, bid_1k=None, ask_1k=None)
        fo.store(db, [row])
        labels = {x for (x,) in db.execute("select label from observations")}
        self.assertEqual(labels, {"OBSERVED_PUBLISHED"})
        # A missing value stays missing, never a default.
        self.assertEqual(db.execute("select rate_raw from observations").fetchone()[0], None)

    def test_unreadable_times_stay_unknown(self):
        self.assertIsNone(fo.epoch(None))
        self.assertIsNone(fo.epoch("not a time"))
        self.assertEqual(fo.epoch(1794429000000), 1794429000)


if __name__ == "__main__":
    unittest.main()
