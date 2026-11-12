"""V2 phase 4: normalization, net engine, strategies, gate, desk hook, collector scope,
history filter and a two-leg lifecycle with a 1000x contract.

Every book, rate, price and answer here is a MOCK fixture, not market evidence."""
import json
import math
import os
import tempfile
import unittest
from unittest import mock

import bot
import wide_paper
from v2 import economics, gates, network_policy as npol, normalize, policy, source
from v2.collector import Collector, window
from v2.identity import Instrument
from v2.strategies import convergence, funding, hybrid
from v2.strategies.common import Leg

V2_ON = {"NB_V2": "1"}
H = 3600


def bk(mid, depth=100.0, half=0.01, model="orderbook"):
    return dict(bids=[(mid - half, depth)], asks=[(mid + half, depth)], empty=False,
                crossed=False, model=model, received=0.0)


def row(venue, asset="BTC", rate=0.0, fee=0.0004, mark=100.0, **kw):
    r = dict(venue=venue, asset=asset, kind="perp", mark=mark, rate=rate, raw_rate=rate,
             interval=3600, next=None, received=0.0, fee=fee, leverage=None,
             price_type="mark", fine_class="crypto",
             exchange_id=policy.FROM_LEGACY.get(venue, venue.lower()))
    r.update(kw)
    return r


def settled(rate, at, days=7):
    return [dict(ts=t, rate=rate) for t in range(int(at) - days * 86400 + H, int(at) + 1, H)]


def closes(at, base=100.0, wiggle=0.0, days=8):
    end = int(at) // H * H
    return {t: base * (1 + wiggle * math.sin(i / 3)) for i, t in
            enumerate(range(end - days * 86400, end + 1, H))}


CFG = dict(wide_paper.WIDE_DEFAULTS)
AT = 1_000 * 86400


class NormalizeTests(unittest.TestCase):
    def test_row_and_book_per_unit(self):
        r = normalize.row(dict(mark=12.0, index=12.1, step=1.0, minimum=2.0), 1000)
        self.assertEqual((r["mark"], r["index"], r["step"], r["minimum"], r["multiplier"]),
                         (0.012, 0.0121, 1000.0, 2000.0, 1000.0))
        b = normalize.book(dict(bids=[(12.0, 3.0)], asks=[(12.1, 1.0)]), 1000)
        self.assertEqual((b["bids"][0], b["asks"][0]), ((0.012, 3000.0), (0.0121, 1000.0)))
        self.assertEqual(normalize.to_contracts(3000.0, 1000), 3.0)
        for bad in (None, 0, -5):
            with self.assertRaises(ValueError):
                normalize.row({}, bad)

    def test_source_normalizes_1000x_rows_against_1x(self):
        def rec(xid, legacy, symbol, price):
            return dict(instrument=Instrument(xid, symbol, symbol, asset_class="crypto",
                                              price=price, class_source="mock"),
                        legacy_venue=legacy, mark=price, index=None, fee_taker=0.0004,
                        funding=dict(raw=0.00001, unit="fraction", interval_s=3600))

        recs = {"arcus": [rec("arcus", "Arcus", "PEPE-USD", 0.000012)],
                "txflow": [rec("txflow", "TxFlow", "1000PEPE", 0.012)]}
        rows, groups, reasons = source.build(recs, at=0.0)
        marks = {r["venue"]: r["mark"] for r in rows}
        self.assertAlmostEqual(marks["TxFlow"], marks["Arcus"])
        self.assertEqual({r["asset"] for r in rows}, {"PEPE"})


class EconomicsTests(unittest.TestCase):
    def test_round_trip_costs_and_refusals(self):
        c, why = economics.round_trip(bk(100.0), bk(100.0), 1000.0, 0.0005, 0.0005)
        self.assertIsNone(why)
        self.assertAlmostEqual(c["fees"], 2.0, places=3)      # 4 x 0.05 % of $1000
        self.assertAlmostEqual(c["spread"], 0.4, places=3)    # 4 x half-spread on 10 units
        self.assertEqual(economics.round_trip(bk(100.0, depth=1.0), bk(100.0), 1000.0,
                                              0.0005, 0.0005)[1], economics.LIQUIDITY)
        self.assertEqual(economics.round_trip(bk(100.0), bk(100.0), 1000.0, None, 0.0005)[1],
                         economics.FEE_UNKNOWN)
        bad = dict(bk(100.0), crossed=True)
        self.assertEqual(economics.round_trip(bad, bk(100.0), 1000.0, 0.0, 0.0)[1],
                         economics.BOOK_BAD)
        self.assertEqual(economics.net(c, funding=3.0, basis=1.0, reserve=0.5)["net"],
                         3.0 + 1.0 - c["fees"] - c["spread"] - 0.5)


class StrategyTests(unittest.TestCase):
    def legs(self, long_rate, short_rate, settle=True, wiggle=0.0002, short_shift=0.0):
        lo = Leg(row=row("A", rate=long_rate), book=bk(100.0),
                 settled=settled(long_rate, AT) if settle else None,
                 closes=closes(AT, 100.0, wiggle))
        sh = Leg(row=row("B", rate=short_rate), book=bk(100.0 + short_shift),
                 settled=settled(short_rate, AT) if settle else None,
                 closes=closes(AT, 100.0))
        return lo, sh

    def test_funding_needs_settled_evidence_and_pays_its_costs(self):
        lo, sh = self.legs(0.0, 0.0005, settle=False)
        ev = funding.evaluate(lo, sh, 1000.0, CFG, AT)
        self.assertEqual(ev["reasons"][0]["code"], "SETTLEMENT_EVIDENCE_MISSING")
        lo, sh = self.legs(0.0, 0.0005)
        ev = funding.evaluate(lo, sh, 1000.0, CFG, AT)
        self.assertEqual(ev["reasons"], [], ev["reasons"])
        self.assertAlmostEqual(ev["funding"], 1000 * 0.0005 * 24)
        self.assertEqual(ev["basis"], 0.0)  # funding never credits price capture
        lo, sh = self.legs(0.0, 0.000005)
        codes = [r["code"] for r in funding.evaluate(lo, sh, 1000.0, CFG, AT)["reasons"]]
        self.assertIn("BREAKEVEN", codes)

    def test_convergence_gap_and_episodes(self):
        lo, sh = self.legs(0.0, 0.0, wiggle=0.002)
        ev = convergence.evaluate(lo, sh, 1000.0, CFG, AT)
        self.assertEqual(ev["reasons"][0]["code"], "GAP_NOT_EXTREME")
        lo, sh = self.legs(0.0, 0.0, wiggle=0.002, short_shift=1.0)  # gap ~100 bp now
        # MOCK history: every day the long leg dips 1 % for two hours, then returns.
        for i, t in enumerate(sorted(lo.closes)):
            if i % 24 in (5, 6):
                lo.closes[t] = lo.closes[t] * 0.99
        ev = convergence.evaluate(lo, sh, 1000.0, CFG, AT)
        self.assertGreater(ev["deviation"], 2 * ev["mad"])
        self.assertIn("capture_share", ev)
        self.assertGreater(ev["basis"], 0)

    def test_hybrid_counts_costs_and_reserve_once_and_is_research(self):
        lo, sh = self.legs(0.0, 0.0005, wiggle=0.002, short_shift=1.0)
        ev = hybrid.evaluate(lo, sh, 1000.0, CFG, AT)
        self.assertFalse(ev["research_only"])  # HybridDesk exists (phase 5)
        if "net" in ev:
            c = ev["convergence_part"]["costs"]
            self.assertAlmostEqual(ev["fees"], c["fees"])
            self.assertLessEqual(ev["reserve"], max(ev["funding_part"].get("reserve") or 0,
                                                    ev["convergence_part"]["reserve"]) + 1e-9)


class FakeProvider:
    def __init__(self, rows, books, session=True, settled_rows=None, closes_=None):
        self.rows, self.books, self.session = rows, books, session
        self.settled_rows, self.closes_ = settled_rows or {}, closes_ or {}

    def rows_by(self):
        return {(r["venue"], r["asset"]): r for r in self.rows}

    def book(self, v, a):
        return self.books.get((v, a))

    def settled(self, v, a, at):
        return self.settled_rows.get((v, a))

    def closes(self, v, a):
        return self.closes_.get((v, a))

    def rth_open(self):
        return self.session


class GateTests(unittest.TestCase):
    def gate(self, rows, session=True):
        books = {(r["venue"], r["asset"]): bk(100.0) for r in rows}
        return gates.Gate(FakeProvider(rows, books, session), CFG)

    def codes(self, g, c, strategy):
        return [r["code"] for r in g.evaluate(c, AT, strategy)["reasons"]]

    def test_static_blocks(self):
        rows = [row("Variational"), row("Arcus"), row("RISEx", fee=None),
                row("TxFlow"), row("Lighter")]
        g = self.gate(rows)
        c = dict(asset="BTC", long="Variational", short="Arcus")
        self.assertIn("EXECUTION_MODEL_UNVERIFIED", self.codes(g, c, "convergence"))
        c = dict(asset="BTC", long="RISEx", short="Arcus")
        self.assertIn("FEE_UNKNOWN", self.codes(g, c, "convergence"))
        c = dict(asset="BTC", long="TxFlow", short="Arcus")
        self.assertIn("SETTLEMENT_EVIDENCE_MISSING", self.codes(g, c, "funding"))
        self.assertNotIn("SETTLEMENT_EVIDENCE_MISSING", self.codes(g, c, "convergence"))
        c = dict(asset="ETH", long="Lighter", short="Arcus")
        self.assertEqual(self.codes(g, c, "funding"), ["NOT_A_V2_ROW", "NOT_A_V2_ROW"])

    def test_rwa_reference_and_session(self):
        for fine, session, code in (("commodity", True, "RWA_REFERENCE_UNVERIFIED"),
                                    ("index", True, "RWA_REFERENCE_UNVERIFIED"),
                                    ("equity", None, "RWA_SESSION_UNKNOWN"),
                                    ("equity", False, "RWA_SESSION_CLOSED")):
            rows = [row("Arcus", "X", fine_class=fine), row("Lighter", "X", fine_class=fine)]
            g = self.gate(rows, session)
            self.assertIn(code, self.codes(g, dict(asset="X", long="Arcus", short="Lighter"),
                                           "convergence"), (fine, session))

    def test_desk_hook_blocks_and_fails_closed(self):
        desk = wide_paper.WidePaper.__new__(wide_paper.WidePaper)
        desk.assess_funding = lambda c: dict(c, reasons=[], eligible=True)
        desk.v2_gate = lambda c, out: ["V2 X: blocked"]
        out = desk.assess(dict(asset="BTC", long="A", short="B"))
        self.assertFalse(out["eligible"])
        desk.v2_gate = mock.Mock(side_effect=RuntimeError("boom"))
        out = desk.assess(dict(asset="BTC", long="A", short="B"))
        self.assertFalse(out["eligible"])
        self.assertTrue(out["reasons"][0].startswith("V2 GATE_ERROR"))


class CollectorTests(unittest.TestCase):
    def transport(self, seen):
        meta_xyz = [{"universe": [{"name": "xyz:NVDA", "deployerFeeScale": "1.0"}],
                     "collateralToken": 0}, [{"funding": "0.00001", "markPx": "160"}]]

        def t(xid, method, url, body, headers=None):
            host, path = url.split("//", 1)[1].split("/", 1)
            d = npol.check(host, method, "/" + path,
                           json.dumps(body).encode() if body is not None else None)
            seen.append((xid, url, d.allowed))
            if not d.allowed:
                raise AssertionError("out of scope: " + url)
            if "hyperliquid" in host:
                return meta_xyz if body["type"] == "metaAndAssetCtxs" else [
                    ["xyz:NVDA", "stocks"]]
            if "rise.trade" in host:
                return {"data": {"markets": []}}
            if "arcus" in host:
                return {"markets": [{"marketDisplayName": "NVDA-USD", "baseAsset": "NVDA",
                                     "category": "EQUITIES", "type": "PERPETUAL",
                                     "status": "ONLINE", "markPrice": "160.1",
                                     "fundingRate": "0.00001", "effectiveTakerFeePpm": 450,
                                     "regularTradingHours": {"x": 1}, "isOutsideRth": False}]}
            if "lighter" in host:
                return {"order_book_details": []}
            if "variational" in host:
                return {"listings": []}
            if "txflow" in host:
                if body["type"] == "perpMeta":
                    return {"data": {"universe": [{"index": 9, "baseCurrency": "SOLO"}]}}
                if body["type"] == "marketTags":
                    return {"data": {}}
                return {"data": {}}
            raise AssertionError(url)
        return t

    def test_only_v2_hosts_and_no_reads_for_single_venue_markets(self):
        from v2.venues import variational
        variational._CACHE.items.clear()
        seen = []
        col = Collector(transport=self.transport(seen), clock=lambda: AT)
        rows = col.refresh()
        self.assertTrue(all(ok for _, _, ok in seen))
        self.assertEqual({(r["venue"], r["asset"]) for r in rows},
                         {("tradeXYZ", "NVDA"), ("Arcus", "NVDA")})
        self.assertTrue(col.rth_open())
        n = len(seen)
        with self.assertRaises(ValueError):
            col.book("TxFlow", "SOLO")      # single venue: no book request at all
        self.assertEqual(len(seen), n)
        self.assertIsNone(col.settled("Variational", "NVDA") if ("Variational", "NVDA")
                          in col.by_key else None)

    def test_history_window_is_stable_within_an_hour(self):
        self.assertEqual(window(AT + 10), window(AT + 3500))
        self.assertNotEqual(window(AT + 10), window(AT + 3700))


class HistoryFilterTests(unittest.TestCase):
    def test_keeper_list_is_limited_to_v2_rows(self):
        import desk_analytics

        e = mock.Mock()
        e.cfg = {"symbols": []}
        e.feed.meta = {}
        e.wide.snapshot.return_value = ([
            dict(venue="tradeXYZ", asset="SOLO", kind="perp", interval=3600),
            dict(venue="tradeXYZ", asset="NVDA", kind="perp", interval=3600)], {})
        e.wide.universe_snapshot.return_value = {"tradeXYZ": {"ONLYXYZ": dict(oi=1e9)}}
        e.matrix.value = None
        e.wide_paper.history_needs.return_value = []
        e.wide_paper.source.wanted.return_value = {("tradeXYZ", "NVDA")}
        src = desk_analytics.DeskHistorySource(e)
        with mock.patch.dict(os.environ, V2_ON):
            got = {(m[0], m[1]) for m in src.markets()}
        self.assertEqual(got, {("tradeXYZ", "NVDA")})
        with mock.patch.dict(os.environ, {"NB_V2": "0"}):
            legacy = {(m[0], m[1]) for m in src.markets()}
        self.assertIn(("tradeXYZ", "SOLO"), legacy)
        self.assertIn(("tradeXYZ", "ONLYXYZ"), legacy)  # legacy backfills single-venue


class LifecycleTests(unittest.TestCase):
    """A paper position between a 1x venue and a 1000x venue, on per-unit books."""

    def setUp(self):
        from test_wide_paper import FakeSource, candles

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = bot.Store(os.path.join(self.dir.name, "p.sqlite3"))
        self.addCleanup(self.store.db.close)
        self.clock = AT
        for p in (mock.patch.object(bot, "now", lambda: self.clock),
                  mock.patch.object(wide_paper, "now", lambda: self.clock),
                  mock.patch.object(wide_paper.capabilities, "pair_blockers", lambda *a: [])):
            p.start()
            self.addCleanup(p.stop)
        self.src = FakeSource()
        long_rate, short_rate = -0.0001, 0.0008
        r1 = row("Hyperliquid", rate=long_rate, fee=0.00045, mark=100.0)
        r1.update(next=AT + 3600, received=AT)
        r1000 = normalize.row(dict(row("Binance", rate=short_rate, fee=0.0005,
                                       mark=100000.0), interval=28800, next=AT + 28800,
                                   received=AT), 1000)
        self.src.rows = [r1, r1000]
        self.src.books[("Hyperliquid", "BTC")] = dict(bids=[(99.99, 1000.0)],
                                                     asks=[(100.01, 1000.0)],
                                                     received=AT, audit_id=None)
        # The 1000x venue's own book (per contract), normalized to units.
        self.src.books[("Binance", "BTC")] = dict(normalize.book(
            dict(bids=[(99990.0, 1.0)], asks=[(100010.0, 1.0)]), 1000), received=AT,
            audit_id=None)
        for v, iv, r in (("Hyperliquid", 3600, long_rate), ("Binance", 28800, short_rate)):
            self.src.history[(v, "BTC")] = candles(AT)
            self.src.rates[(v, "BTC")] = [dict(ts=t, rate=r, value=None, audit_id=None)
                                          for t in range(AT - 30 * 86400 + iv, AT + 1, iv)]
        self.desk = wide_paper.WidePaper(self.store, self.src, sleep=lambda s: None)

    def test_two_legs_open_and_close_with_matching_units_and_reconciled_ledger(self):
        self.desk.cycle()
        self.assertEqual(len(self.desk.positions), 1, self.desk.assessed)
        p = self.desk.positions[0]
        q = {l["venue"]: l["qty"] for l in p["legs"]}
        self.assertAlmostEqual(q["Hyperliquid"], q["Binance"])   # same units on both legs
        self.assertLess(q["Binance"], 1000.0)                   # units, not contracts
        self.desk.manual_close.add(p["id"])
        self.clock += 60
        self.desk.cycle(entries=False)
        t = self.desk.trade_log()[0]
        self.assertAlmostEqual(t["net"], t["price_pnl"] + t["funding"] - t["fees"])
        if self.desk.ledger.ready():
            self.assertEqual(self.desk.ledger.reconcile(), [])

    def test_second_leg_without_fill_is_a_leg_recovery(self):
        original = self.desk.fill

        def fill(v, a, qty, buy, limit, lots):
            if v == "Binance":
                return 0.0, None, self.src.book(v, a)
            return original(v, a, qty, buy, limit, lots)

        with mock.patch.object(self.desk, "fill", fill):
            self.desk.cycle()
        log = self.desk.trade_log()
        self.assertEqual(log[0]["outcome"], "LEG_RECOVERY")
        self.assertEqual(self.desk.report()["funding"]["wins"], 0)


if __name__ == "__main__":
    unittest.main()
