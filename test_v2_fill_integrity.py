"""PAPER fill integrity (readiness audit, 2026-11-14): one price-quality layer
(v2.fill_guard) in front of EVERY simulated fill and mark of both paper engines.
MOCK books / positions only; no network."""
import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bot
import wide_paper
from v2 import fill_guard as fg

T0 = 1_000 * 86400
ROOT = Path(__file__).resolve().parent


def book(mid, at=T0, half=0.01, depth=1000.0, **kw):
    return dict(bids=[(mid - half, depth)], asks=[(mid + half, depth)], received=at,
                audit_id=None, **kw)


class GuardUnitTests(unittest.TestCase):
    def test_book_checks(self):
        self.assertEqual(fg.book_reasons(None, "arcus", T0), ["BOOK_MISSING"])
        self.assertEqual(fg.book_reasons(dict(bids=[], asks=[]), "arcus", T0), ["BOOK_EMPTY"])
        crossed = dict(bids=[(101.0, 1.0)], asks=[(100.0, 1.0)], received=T0)
        locked = dict(bids=[(100.0, 1.0)], asks=[(100.0, 1.0)], received=T0)
        self.assertIn("BOOK_CROSSED", fg.book_reasons(crossed, "arcus", T0))
        self.assertIn("BOOK_LOCKED", fg.book_reasons(locked, "arcus", T0))
        self.assertIn("BOOK_STALE", fg.book_reasons(book(100, at=T0 - 60), "arcus", T0))
        self.assertIn("BOOK_STALE", fg.book_reasons(book(100, at=None), "arcus", T0))
        self.assertIn("SPREAD_TOO_WIDE", fg.book_reasons(book(100, half=1.0), "arcus", T0))
        self.assertEqual(fg.book_reasons(book(100, half=0.4), "arcus", T0), [])  # 80 bp
        self.assertIn("OFF_TICK_GRID",
                      fg.book_reasons(book(100.003, half=0.01), "arcus", T0, tick=0.01))
        self.assertEqual(fg.book_reasons(book(100.0, half=0.01), "arcus", T0, tick=0.01), [])
        self.assertEqual(fg.book_reasons(book(100.003), "arcus", T0, tick=None), [])

    def test_price_checks(self):
        self.assertIsNone(fg.price_reason(101.0, cross_ref=100.0))
        self.assertIn("CROSS_LEG", fg.price_reason(106.0, cross_ref=100.0))
        self.assertIn("SCALE", fg.price_reason(3000.0, own_ref=3.0))
        self.assertIn("SLIPPAGE", fg.price_reason(102.0, book_mid=100.0, cross_ref=101.0,
                                                  walk=True))
        self.assertEqual(fg.price_reason(100.0), "NO_REFERENCE_PRICE")
        self.assertEqual(fg.price_reason(None, cross_ref=1.0), "NO_PRICE")

    def test_a_large_legitimate_move_never_blocks_a_close(self):
        """Both legs moved +40 % since entry: the fresh other leg is the reference."""
        self.assertIsNone(fg.price_reason(140.2, cross_ref=140.0, own_ref=100.0))
        # without a fresh other leg, the own-entry bound is the last defence
        self.assertIn("SCALE", fg.price_reason(140.2, own_ref=100.0))


class DeskSource:
    """MOCK source whose books are returned AS GIVEN (stale stays stale)."""

    def __init__(self, books):
        self.books, self.rows = books, []

    def market_rows(self):
        return []

    def book(self, v, a):
        return dict(self.books[(v, a)])


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = bot.Store(os.path.join(self.dir.name, "p.sqlite3"))
        self.addCleanup(self.store.db.close)
        for p in (mock.patch.object(bot, "now", lambda: T0),
                  mock.patch.object(wide_paper, "now", lambda: T0)):
            p.start()
            self.addCleanup(p.stop)

    def desk(self, books):
        return wide_paper.WidePaper(self.store, DeskSource(books), sleep=lambda s: None)

    @staticmethod
    def position():
        leg = lambda v, side: dict(venue=v, side=side, qty=10.0, entry=100.0,  # noqa: E731
                                   fee_rate=0.0, margin=100.0, closed=False,
                                   price_pnl=0.0)
        return dict(id="w1", asset="BTC", legs=[leg("A", "long"), leg("B", "short")],
                    price_pnl=0.0, fees=0.0, funding=0.0, status="OPEN",
                    close_reason=None)

    def test_close_refuses_a_scaled_or_stale_leg_and_closes_nothing(self):
        for name, bad in (("scaled", book(100000.0)), ("stale", book(100.0, at=T0 - 300)),
                          ("crossed", dict(bids=[(101.0, 9e9)], asks=[(99.0, 9e9)],
                                           received=T0))):
            d = self.desk({("A", "BTC"): book(100.0), ("B", "BTC"): bad})
            p = self.position()
            d.positions.append(p)
            cash = d.wallet["cash"]
            d.close(p, "test")
            self.assertEqual(p["status"], "RECOVERY", name)
            self.assertFalse(any(leg["closed"] for leg in p["legs"]), name)  # hedge kept
            self.assertEqual(d.wallet["cash"], cash, name)                   # no fake PnL
            self.assertEqual(p["price_pnl"], 0.0, name)

    def test_close_books_a_large_legitimate_move(self):
        d = self.desk({("A", "BTC"): book(140.0), ("B", "BTC"): book(140.1)})
        p = self.position()
        d.positions.append(p)
        d.close(p, "test")
        self.assertTrue(all(leg["closed"] for leg in p["legs"]))
        x = d.extra_slippage                        # the desk's own exit slippage
        self.assertAlmostEqual(p["price_pnl"], 10 * (139.99 * (1 - x) - 100) -
                               10 * (140.11 * (1 + x) - 100), places=6)

    def test_unwind_refuses_a_scaled_book(self):
        d = self.desk({("A", "BTC"): book(100000.0), ("B", "BTC"): book(100.0)})
        p = self.position()
        cash = d.wallet["cash"]
        with self.assertRaises(wide_paper.FillGuardRefused):
            d.unwind(p, p["legs"][0], 5.0)
        self.assertEqual(d.wallet["cash"], cash)
        self.assertEqual(p["legs"][0]["qty"], 10.0)

    def test_entry_fill_refuses_a_price_far_from_the_other_leg(self):
        d = self.desk({("A", "BTC"): book(110.0), ("B", "BTC"): book(100.0)})
        got, px, _ = d.fill("A", "BTC", 1.0, True, 110.5, [None, None], other="B")
        self.assertEqual((got, px), (0.0, None))          # 10 % from the other leg
        d = self.desk({("A", "BTC"): book(100.2), ("B", "BTC"): book(100.0)})
        got, px, _ = d.fill("A", "BTC", 1.0, True, 100.4, [None, None], other="B")
        self.assertEqual(got, 1.0)

    def test_mark_keeps_the_last_mark_on_a_bad_book(self):
        d = self.desk({})
        p = self.position()
        d.mark_position(p, {"A": book(101.0), "B": book(101.1)})
        good = p["unrealized"]
        d.mark_position(p, {"A": book(101.0), "B": book(101.0, at=T0 - 600)})
        self.assertEqual(p["unrealized"], good)
        self.assertTrue(p["mark_status"].startswith("UNRELIABLE"))


class EveryFillPathIsGuardedTests(unittest.TestCase):
    """Structural proof: every place that books a fill price or a realized / marked
    PnL from a book goes through v2.fill_guard."""

    def functions(self, path):
        t = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        return {n.name: n for n in ast.walk(t) if isinstance(n, ast.FunctionDef)}, t

    @staticmethod
    def calls(node):
        out = []
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                f = n.func
                out.append((f.attr if isinstance(f, ast.Attribute) else
                            getattr(f, "id", None), n.lineno))
        return out

    def test_estimated_engine(self):
        fns, tree = self.functions("v2/paper_engine.py")
        emitters = [name for name, fn in fns.items() if any(
            c == "emit" and isinstance(a, ast.Constant) and a.value == "fill"
            for c, a in ((x.func.attr, x.args[0]) for x in ast.walk(fn)
                         if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
                         and x.args))]
        self.assertEqual(emitters, ["sim_fill"])
        sim = dict(self.calls(fns["sim_fill"]))
        lines = sorted(self.calls(fns["sim_fill"]), key=lambda x: x[1])
        first_emit = min(l for c, l in lines if c == "emit")
        guards = [l for c, l in lines if c == "fill_guard"]
        self.assertTrue(guards and min(guards) < first_emit)
        self.assertIn("fill_guard", sim)
        for name in ("clob_ioc", "rfq_fill"):     # pricing only inside sim_fill
            users = [n for n, fn in fns.items() if any(c == name for c, _ in self.calls(fn))]
            self.assertEqual(users, ["sim_fill"], name)

    def test_desk(self):
        fns, _ = self.functions("wide_paper.py")
        credit_users = {n for n, fn in fns.items()
                        if any(c == "credit" for c, _ in self.calls(fn))}
        funding_only = {"settle"}                   # funding payments, not fills
        for n in credit_users - funding_only:
            cs = {c for c, _ in self.calls(fns[n])}
            guarded = bool(cs & {"price_guard", "price_reason", "fill"}) or "unwind" in cs
            self.assertTrue(guarded, "%s books PnL without the guard" % n)
        self.assertIn("price_guard", {c for c, _ in self.calls(fns["fill"])})
        self.assertIn("price_guard", {c for c, _ in self.calls(fns["unwind"])})
        self.assertIn("price_reason", {c for c, _ in self.calls(fns["close"])})
        self.assertIn("price_reason", {c for c, _ in self.calls(fns["mark_position"])})
        ioc_users = {n for n, fn in fns.items() if any(c == "ioc" for c, _ in self.calls(fn))}
        self.assertEqual(ioc_users, {"fill"})


class StuckPositionTests(unittest.TestCase):
    """A real persistent divergence or a chronically wide book must not keep a position
    from closing forever (its loss never realized); data errors stay refused."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = bot.Store(os.path.join(self.dir.name, "p.sqlite3"))
        self.addCleanup(self.store.db.close)
        self.t = [T0]
        for p in (mock.patch.object(bot, "now", lambda: self.t[0]),
                  mock.patch.object(wide_paper, "now", lambda: self.t[0])):
            p.start()
            self.addCleanup(p.stop)

    def run_close(self, b_long, b_short, hours=7):
        src = DeskSource({})
        d = wide_paper.WidePaper(self.store, src, sleep=lambda s: None)
        p = DeskTests.position()
        d.positions.append(p)

        def books(at):
            src.books = {("A", "BTC"): dict(b_long, received=at),
                         ("B", "BTC"): dict(b_short, received=at)}
        books(self.t[0])
        d.close(p, "test")
        first = (p["status"], any(l["closed"] for l in p["legs"]))
        self.t[0] = T0 + hours * 3600
        books(self.t[0])
        d.close(p, "test")
        return first, p

    def test_a_real_10pct_divergence_closes_after_the_stuck_period(self):
        first, p = self.run_close(book(100.0), book(110.0))
        self.assertEqual(first, ("RECOVERY", False))               # refused at first
        self.assertTrue(all(l["closed"] for l in p["legs"]))      # closed after 6 h
        self.assertTrue(p.get("closed_under_relaxed_guard"))
        self.assertLess(p["price_pnl"], 0)                         # the loss is realized

    def test_a_scale_sized_error_is_never_relaxed(self):
        first, p = self.run_close(book(100.0), book(300.0), hours=48)
        self.assertEqual(first, ("RECOVERY", False))
        self.assertFalse(any(l["closed"] for l in p["legs"]))

    def test_a_wide_book_closes_after_the_stuck_period_but_not_an_absurd_one(self):
        first, p = self.run_close(book(100.0), book(100.0, half=1.5))   # 300 bp
        self.assertEqual(first, ("RECOVERY", False))
        self.assertTrue(all(l["closed"] for l in p["legs"]))
        first, p = self.run_close(book(100.0), book(100.0, half=4.5))   # 900 bp
        self.assertFalse(any(l["closed"] for l in p["legs"]))

    def test_stale_is_never_relaxed(self):
        src = DeskSource({("A", "BTC"): book(100.0, at=T0 - 600),
                          ("B", "BTC"): book(100.0)})
        d = wide_paper.WidePaper(self.store, src, sleep=lambda s: None)
        p = DeskTests.position()
        p["guard_stuck_since"] = T0 - 30 * 3600                      # long stuck
        d.close(p, "test")
        self.assertFalse(any(l["closed"] for l in p["legs"]))

    def test_estimated_engine_retry_relaxes_only_after_the_stuck_period(self):
        from tools import v2_failure_harness as h
        from v2 import paper_engine, paper_ledger as L
        d = tempfile.mkdtemp(prefix="nb-stuck-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        led = L.PaperLedger(Path(d) / "led")
        p = h.partial_position(led)
        eng = paper_engine.PaperEngine(led, dict(wide_paper.WIDE_DEFAULTS), [])
        t1 = h.T0
        obs = lambda t: h.FakeObs(t, {("tradeXYZ", "X"): h.book(3.3, received=t),  # noqa
                                      ("TxFlow", "X"): h.book(3.0, received=t)})
        eng.retry(p, obs(t1))                                       # 10 % apart
        self.assertFalse(p["legs"]["short"]["fills"])
        self.assertEqual(p["guard_stuck_since"], t1)
        eng.retry(p, obs(t1 + 7 * 3600))
        # after 6 h stuck the engine's recovery timeout unwinds the excess long leg
        # under the relaxed guard: the position is flat and the outcome is realized
        unwinds = [f for f in p["legs"]["long"]["fills"] if f["action"] == "unwind"]
        self.assertTrue(unwinds)
        self.assertEqual(p["state"], L.CLOSED)
        notes = [m for m in (Path(d) / "led" / "ledger.jsonl").read_text().splitlines()
                 if "RELAXED GUARD" in m]
        self.assertTrue(notes)


if __name__ == "__main__":
    unittest.main()
