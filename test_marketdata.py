"""Data platform phase 1: venue ids, capability manifests and canonical markets.

Recorder folders here are filled by tools/phase1_recorder/tests/fakes.py (MOCK data).
Where a test needs a folder that claims live data, it relabels a MOCK folder itself;
that fixture tests the rule, it is not market evidence."""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unittest

from marketdata import canonical, manifest
from marketdata.evidence import LIVE, Evidence
from marketdata.report import build, text
from marketdata.venues import ALIASES, VENUES, venue_id
from tools.phase1_recorder.config import load
from tools.phase1_recorder.recorder import Recorder
from tools.phase1_recorder.tests.fakes import Clock, FakeVenues

ROOT = os.path.dirname(os.path.abspath(__file__))
TARGETS = (
    "hyperliquid",
    "paradex",
    "txflow",
    "arcus",
    "extended",
    "variational",
    "risex",
    "lighter_core",
    "lighter_rh",
)


class VenueTests(unittest.TestCase):
    def test_the_nine_target_venues_and_the_lighter_names(self):
        self.assertEqual(set(VENUES), set(TARGETS))
        # "Lighter" in the engine and recorder is the Robinhood deployment.
        self.assertEqual(venue_id("Lighter"), "lighter_rh")
        self.assertEqual(venue_id("LighterCore"), "lighter_core")
        self.assertIn("rh.lighter", VENUES["lighter_rh"]["host"])
        self.assertIn("zklighter", VENUES["lighter_core"]["host"])
        self.assertEqual(venue_id("lighter_rh"), "lighter_rh")
        with self.assertRaises(KeyError):
            venue_id("Binance")
        self.assertTrue(set(ALIASES.values()) <= set(VENUES))


class ManifestTests(unittest.TestCase):
    def test_every_venue_lists_every_capability(self):
        for vid in TARGETS:
            self.assertEqual(
                set(manifest.MANIFESTS[vid]["caps"]), set(manifest.CAPABILITIES)
            )

    def test_every_endpoint_is_one_the_cited_module_already_reads(self):
        for vid, m in manifest.MANIFESTS.items():
            for cap, sources in m["caps"].items():
                for s in sources:
                    with open(os.path.join(ROOT, s.module), encoding="utf-8") as f:
                        code = f.read()
                    self.assertIn(s.endpoint, code, (vid, cap, s.module))
                    self.assertIn(s.path, manifest.PATHS)
                    self.assertIn(s.transport, ("REST", "WebSocket"))

    def test_capabilities_no_adapter_reads_are_not_implemented(self):
        for vid in TARGETS:
            caps = manifest.MANIFESTS[vid]["caps"]
            for cap in ("trades", "funding_predicted"):
                self.assertEqual(caps[cap], [], (vid, cap))
        self.assertTrue(
            all(not s for s in manifest.MANIFESTS["variational"]["caps"].values())
        )
        self.assertEqual(manifest.MANIFESTS["txflow"]["caps"]["funding_history"], [])

    def test_only_websocket_is_the_extended_rfq_book(self):
        ws = [
            (vid, cap)
            for vid, m in manifest.MANIFESTS.items()
            for cap, sources in m["caps"].items()
            for s in sources
            if s.transport == "WebSocket"
        ]
        self.assertEqual(ws, [("extended", "orderbook_l2")])


class Folder(unittest.TestCase):
    """A recorder folder filled from the MOCK venues."""

    venues = ["Hyperliquid", "LighterCore", "Lighter", "Extended", "Variational"]

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.fake = FakeVenues(self.clock)
        self.cfg = load(
            root=os.path.join(self.dir.name, "phase1-evidence"),
            assets=["BTC", "ETH"],
            venues=self.venues,
        )
        self.cfg["backfill_days"] = 2
        rec = Recorder(
            self.cfg, transport=self.fake, clock=self.clock, sleep=self.clock.sleep
        )
        rec.run("once")
        rec.close()
        self.root = self.cfg["root"]

    def tearDown(self):
        self.dir.cleanup()

    def relabel_live(self):
        """Test fixture only: mark this MOCK folder as live to exercise the rule."""
        db = sqlite3.connect(os.path.join(self.root, "recorder.sqlite3"))
        db.execute(
            "update checkpoints set value=? where name='data_source'",
            (json.dumps(LIVE),),
        )
        db.commit()
        db.close()

    def evaluate(self, at=None):
        at = at or self.clock()
        return manifest.evaluate(Evidence(self.root, at), at)


class EvaluateTests(Folder):
    def test_a_mock_folder_verifies_nothing(self):
        out = self.evaluate()
        for v in out.values():
            for c in v["capabilities"].values():
                self.assertNotEqual(c["availability"], manifest.VERIFIED)
        hl = out["hyperliquid"]["capabilities"]
        self.assertTrue(hl["orderbook_l2"]["mock_evidence"])
        report = text(build(self.root, self.clock()))
        self.assertIn("MOCK", report)
        self.assertIn("hiçbir yetenek DOĞRULANDI sayılmaz", report)
        self.assertNotIn("DOĞRULANDI ", report.split("\n\n", 2)[-1])

    def test_live_records_verify_only_the_recorder_path(self):
        self.relabel_live()
        hl = self.evaluate()["hyperliquid"]["capabilities"]
        for cap in (
            "market_discovery",
            "mark_price",
            "orderbook_l2",
            "bid_ask",
            "ohlcv",
            "funding_current",
            "funding_history",
            "funding_settlements",
            "instrument_metadata",
            "trading_constraints",
        ):
            self.assertEqual(hl[cap]["availability"], manifest.VERIFIED, cap)
            self.assertEqual(hl[cap]["verified_path"], manifest.RECORDER)
            self.assertIsNotNone(hl[cap]["last_success"])
        # CCXT-only capabilities are not proven by the recorder.
        for cap in ("ticker", "open_interest", "volume"):
            self.assertEqual(hl[cap]["availability"], manifest.IMPLEMENTED, cap)
        # No source: never available, even with records that could look like proof.
        self.assertEqual(hl["index_price"]["availability"], manifest.NOT_IMPLEMENTED)
        self.assertEqual(hl["trades"]["availability"], manifest.NOT_IMPLEMENTED)

    def test_venues_without_a_collector_are_not_verified(self):
        self.relabel_live()
        out = self.evaluate()
        for vid in ("paradex", "txflow", "arcus", "risex"):
            self.assertEqual(out[vid]["health"], "NO_COLLECTOR")
            for c in out[vid]["capabilities"].values():
                self.assertIn(
                    c["availability"], (manifest.IMPLEMENTED, manifest.NOT_IMPLEMENTED)
                )
        for c in out["variational"]["capabilities"].values():
            self.assertEqual(c["availability"], manifest.NOT_IMPLEMENTED)

    def test_old_evidence_is_stale_and_a_silent_venue_is_down(self):
        self.relabel_live()
        out = self.evaluate()
        self.assertEqual(out["hyperliquid"]["health"], "OK")
        self.assertEqual(
            out["hyperliquid"]["capabilities"]["orderbook_l2"]["freshness"], "FRESH"
        )
        later = self.clock() + 3600
        out = self.evaluate(later)
        self.assertEqual(out["hyperliquid"]["health"], "DOWN")
        self.assertEqual(
            out["hyperliquid"]["capabilities"]["orderbook_l2"]["freshness"], "STALE"
        )
        # Metadata is refreshed every 6 h: still fresh an hour later.
        self.assertEqual(
            out["hyperliquid"]["capabilities"]["market_discovery"]["freshness"], "FRESH"
        )

    def test_health_from_request_history(self):
        h = manifest.venue_health
        self.assertEqual(h(None, 100), "NO_DATA")
        self.assertEqual(
            h(dict(requests=3, last_ok=None, errors_last_hour=3), 100), "DOWN"
        )
        self.assertEqual(
            h(dict(requests=3, last_ok=90, errors_last_hour=1), 100), "DEGRADED"
        )
        self.assertEqual(h(dict(requests=3, last_ok=90, errors_last_hour=0), 100), "OK")

    def test_reading_changes_nothing_in_the_folder(self):
        # A read-only SQLite reader of a WAL database creates the empty -wal/-shm
        # side files (as a running recorder has them anyway); no data file changes.
        def fingerprint():
            out = {}
            for folder, _, names in os.walk(self.root):
                for n in names:
                    p = os.path.join(folder, n)
                    if n.endswith(("-wal", "-shm")):
                        if n.endswith("-wal"):
                            self.assertEqual(os.path.getsize(p), 0)
                        continue
                    with open(p, "rb") as f:
                        out[p] = hashlib.sha256(f.read()).hexdigest()
            return out

        before = fingerprint()
        build(self.root, self.clock())
        text(build(self.root, self.clock()), True)
        self.assertEqual(fingerprint(), before)

    def test_a_missing_folder_is_an_error_not_an_empty_success(self):
        rep = build(os.path.join(self.dir.name, "yok"), self.clock())
        self.assertIsNotNone(rep["error"])
        self.assertFalse(rep["live"])


class CanonicalTests(Folder):
    def test_instruments_keep_venue_ids_and_unknown_fields_stay_empty(self):
        rep = build(self.root, self.clock())
        insts = {(i["venue"], i["venue_symbol"]): i for i in rep["instruments"]}
        hl = insts[("hyperliquid", "BTC")]
        self.assertEqual(hl["canonical_id"], "PERP:BTC")
        self.assertIsNone(hl["tick_size"])  # not in Hyperliquid's response
        self.assertEqual(hl["provenance"]["tick_size"], canonical.UNKNOWN)
        self.assertEqual(hl["provenance"]["contract_type"], canonical.DECLARED)
        self.assertEqual(hl["provenance"]["contract_multiplier"], canonical.DECLARED)
        rh = insts[("lighter_rh", "BTC")]
        self.assertEqual(rh["venue_market_id"], "1")
        self.assertEqual(rh["price_precision"], 2)
        self.assertEqual(rh["provenance"]["tick_size"], canonical.DERIVED)
        ex = insts[("extended", "BTC")]
        self.assertEqual(ex["venue_market_id"], "BTC-USD")
        self.assertEqual(ex["settlement_asset"], "USD")
        self.assertEqual(ex["provenance"]["settlement_asset"], canonical.DECLARED)
        self.assertIsNone(ex["quote"])
        self.assertEqual(rep["identities"]["PERP:BTC"]["status"], "PRICE_CONSISTENT")
        self.assertEqual(len(rep["identities"]["PERP:BTC"]["venues"]), 4)

    def test_size_prefixes_give_a_declared_multiplier(self):
        self.assertEqual(canonical.split_prefix("kPEPE"), ("PEPE", 1000, True))
        self.assertEqual(canonical.split_prefix("1000PEPE"), ("PEPE", 1000, True))
        self.assertEqual(canonical.split_prefix("1000000MOG"), ("MOG", 1_000_000, True))
        self.assertEqual(canonical.split_prefix("1INCH"), ("1INCH", 1, False))
        self.assertEqual(canonical.split_prefix("KAITO"), ("KAITO", 1, False))
        a = canonical.from_recorder(
            dict(
                venue="Hyperliquid",
                asset="kPEPE",
                market_id="kPEPE",
                base="kPEPE",
                contract_size=1.0,
                active=True,
                observed_at=1,
            )
        )
        b = canonical.from_recorder(
            dict(
                venue="LighterCore",
                asset="1000PEPE",
                market_id=7,
                base="1000PEPE",
                contract_size=1.0,
                active=True,
                observed_at=1,
            )
        )
        c = canonical.from_recorder(
            dict(
                venue="Extended",
                asset="PEPE",
                market_id="PEPE-USD",
                base="PEPE",
                contract_size=1.0,
                active=True,
                observed_at=1,
            )
        )
        self.assertEqual(
            {a.canonical_id, b.canonical_id, c.canonical_id}, {"PERP:PEPE"}
        )
        self.assertEqual(a.contract_multiplier, 1000)
        self.assertEqual(a.provenance["base"], canonical.DERIVED)
        marks = {
            ("hyperliquid", "kPEPE"): 0.0101,
            ("lighter_core", "7"): 0.0100,
            ("extended", "PEPE-USD"): 0.0000101,
        }
        g = canonical.identities([a, b, c], marks)["PERP:PEPE"]
        self.assertEqual(g["status"], "PRICE_CONSISTENT")

    def test_same_symbol_different_price_is_a_conflict(self):
        x = canonical.from_recorder(
            dict(
                venue="Hyperliquid",
                asset="LIT",
                market_id="LIT",
                base="LIT",
                contract_size=1.0,
                active=True,
                observed_at=1,
            )
        )
        y = canonical.from_recorder(
            dict(
                venue="Extended",
                asset="LIT",
                market_id="LIT-USD",
                base="LIT",
                contract_size=1.0,
                active=True,
                observed_at=1,
            )
        )
        z = canonical.from_recorder(
            dict(
                venue="LighterCore",
                asset="LIT",
                market_id=9,
                base="LIT",
                contract_size=1.0,
                active=True,
                observed_at=1,
            )
        )
        marks = {
            ("hyperliquid", "LIT"): 1.0,
            ("extended", "LIT-USD"): 1.02,
            ("lighter_core", "9"): 7.5,
        }
        g = canonical.identities([x, y, z], marks)["PERP:LIT"]
        self.assertEqual(g["status"], "PRICE_CONFLICT")
        self.assertEqual([c["venue"] for c in g["conflicts"]], ["lighter_core"])
        # Without prices the shared id stays a candidate.
        self.assertEqual(
            canonical.identities([x, y], {})["PERP:LIT"]["status"], "NO_PRICE"
        )
        self.assertEqual(
            canonical.identities([x], {})["PERP:LIT"]["status"], "SINGLE_VENUE"
        )

    def test_listing_status_history_is_kept(self):
        old = dict(
            venue="Extended",
            asset="XYZ",
            market_id="XYZ-USD",
            base="XYZ",
            active=True,
            observed_at=100,
        )
        new = dict(old, active=False, observed_at=200, extra=dict(status="DELISTED"))
        inst = canonical.from_recorder(new, [old])
        self.assertEqual(inst.status, canonical.INACTIVE)
        self.assertEqual(inst.first_seen, 100)
        self.assertEqual(
            inst.status_history, [dict(at=100, active=True), dict(at=200, active=False)]
        )
        hl = canonical.from_recorder(
            dict(
                venue="Hyperliquid",
                asset="OLD",
                market_id="OLD",
                base="OLD",
                active=False,
                observed_at=5,
                extra=dict(isDelisted=True),
            )
        )
        self.assertEqual(hl.status, canonical.DELISTED)

    def test_precision_only_from_power_of_ten_steps(self):
        self.assertEqual(canonical.precision(0.001), 3)
        self.assertEqual(canonical.precision(1), 0)
        self.assertIsNone(canonical.precision(0.5))
        self.assertIsNone(canonical.precision(None))
        self.assertIsNone(canonical.precision(10))


if __name__ == "__main__":
    unittest.main()
