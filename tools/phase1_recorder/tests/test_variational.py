"""Variational discovery: raw responses and their schema only, no field interpreted.
All responses come from tests/fakes.py (MOCK data, never reported as evidence)."""

import os
import tempfile
import unittest

from tools.phase1_recorder.clients.variational import shape
from tools.phase1_recorder.config import load
from tools.phase1_recorder.recorder import Recorder
from tools.phase1_recorder.validation import consistency, coverage, text_report

from .fakes import Clock, FakeVenues


class VariationalTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.fake = FakeVenues(self.clock)
        self.cfg = load(
            root=os.path.join(self.dir.name, "phase1-evidence"),
            assets=["BTC", "ETH"],
            venues=["Variational"],
        )
        self.rec = Recorder(
            self.cfg, transport=self.fake, clock=self.clock, sleep=self.clock.sleep
        )

    def tearDown(self):
        self.rec.store.close()
        self.dir.cleanup()

    def state(self):
        return self.rec.store.checkpoint("discovery:Variational")

    def test_a_poll_stores_the_raw_body_and_its_schema_only(self):
        self.rec.run("once")
        store = self.rec.store
        raw = os.path.join(store.root, "raw", "variational")
        self.assertTrue(os.path.isdir(raw) and os.listdir(raw))
        (schema,) = store.records("schema", "Variational")
        self.assertEqual(schema["listing_count"], 2)
        self.assertEqual(schema["listing_keys"]["ticker"], 2)
        self.assertEqual(schema["paths"]["$.listings[].mock_nested.a"], ["number"])
        # Nothing is parsed into market evidence.
        for kind in ("market", "funding_snapshot", "settlement", "candle", "orderbook"):
            self.assertEqual(store.records(kind, "Variational"), [])
        self.assertEqual(self.state()["polls"], 1)

    def polls(self, n):
        (v,) = self.rec.venues
        for _ in range(n):
            self.assertTrue(v.run_task("discovery"))
            self.clock.sleep(60)

    def test_same_body_adds_no_schema_and_changes_are_measured(self):
        self.polls(5)
        st = self.state()
        self.assertEqual(st["polls"], 5)
        self.assertEqual(st["content_changes"], 1)  # the body never changed
        self.assertEqual(len(self.rec.store.records("schema", "Variational")), 1)
        # The venue changes the body every 2 minutes, then its shape once.
        self.fake.variational = lambda now: {
            "listings": [{"ticker": "BTC", "mock_value": str(int(now) // 120)}],
            "mock_total": 1,
        }
        self.polls(6)
        st = self.state()
        # The first body, then four distinct 2-minute buckets in six polls.
        self.assertEqual(st["content_changes"], 5)
        self.assertEqual(st["schema_changes"], 1)  # mock_nested disappeared
        self.assertEqual(len(self.rec.store.records("schema", "Variational")), 2)
        cov = coverage(self.rec.store, self.cfg, now=self.clock())
        self.assertEqual(
            cov["venues"]["Variational"]["discovery"]["median_change_seconds"], 120
        )

    def test_the_report_lists_the_discovered_fields_and_says_mock(self):
        self.rec.run("once")
        cov = coverage(self.rec.store, self.cfg, now=self.clock())
        v = cov["venues"]["Variational"]
        self.assertEqual(v["markets"], {})
        self.assertIn("MOCK", v["connection"])
        text = text_report(cov)
        self.assertIn("BU KLASÖR GERÇEK BORSA VERİSİ İÇERMİYOR", text)
        self.assertIn("KEŞİF", text)
        self.assertIn("ticker: 2", text)
        self.assertIn("$.listings[].mock_value: string", text)
        self.assertIn("hiçbir alan funding", text)

    def test_stored_responses_replay_to_the_same_schema(self):
        self.rec.run("once")
        cons = consistency(self.rec.store, self.cfg)
        self.assertTrue(cons["ok"], cons)
        self.assertGreater(cons["replayed_responses"], 0)

    def test_shape_merges_list_elements_and_caps_paths(self):
        paths, cut = shape({"a": [{"x": 1}, {"x": "s", "y": None}], "b": True})
        self.assertEqual(paths["$.a[].x"], ["number", "string"])
        self.assertEqual(paths["$.a[].y"], ["null"])
        self.assertFalse(cut)
        paths, cut = shape({str(i): i for i in range(1000)})
        self.assertTrue(cut)
        self.assertLessEqual(len(paths), 400)


if __name__ == "__main__":
    unittest.main()
