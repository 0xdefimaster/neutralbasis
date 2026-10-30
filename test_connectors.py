import unittest
import connectors
from connectors import (
    Book,
    CcxtConnector,
    Connectors,
    MarketSnapshot,
    NativeConnector,
    universe_rows,
)

T0 = 2_000_000_000


class FakeFeed:
    def __init__(self):
        self.meta = {
            "Lighter": {
                "BTC": dict(
                    mark=100.0,
                    index=99.9,
                    rate=0.0001,
                    interval=3600,
                    received=T0,
                    max_leverage=10,
                    step=0.001,
                    minimum=0.001,
                    min_quote=10.0,
                    next=T0 + 600,
                )
            },
            "Arcus": {
                "BTC": dict(
                    mark=100.2,
                    index=100.0,
                    rate=0.0002,
                    raw_rate=0.0002,
                    interval=3600,
                    received=T0,
                    max_leverage=5,
                )
            },
            "RISEx": {"BTC": dict(mark=100.1, rate=0.0001, received=T0)},
        }
        self.calls = []

    def book(self, v, a):
        self.calls.append(("feed.book", v, a))
        return dict(bids=[(99.0, 1.0)], asks=[(101.0, 2.0)], received=T0)

    def funding_history(self, v, a, start):
        self.calls.append(("feed.funding", v, a, start))
        return [dict(ts=T0, rate=0.0001, value=None, audit_id=7)]

    def candles(self, v, a):
        return dict(prices={T0: 100.0}, price_type="oracle" if v == "Arcus" else "x")


class FakeWide:
    def __init__(self):
        self.calls = []

    def snapshot(self, max_age=180):
        return (
            [
                dict(
                    venue="Hyperliquid",
                    asset="BTC",
                    kind="perp",
                    mark=100.0,
                    rate=0.0001,
                    raw_rate=0.0001,
                    interval=3600,
                    received=T0,
                    contract_size=1.0,
                    index=100.0,
                ),
                dict(
                    venue="Binance", asset="BTC", kind="spot", mark=100.0, received=T0
                ),
                dict(venue="Lighter", asset="ETH", kind="perp", mark=10.0, received=T0),
            ],
            {},
        )

    def universe_snapshot(self, max_age=300):
        entry = dict(
            symbol="X/USDT:USDT",
            rate=0.0001,
            raw_rate=0.0004,
            interval=14400,
            received=T0,
            oi=2e6,
            contract_size=1.0,
        )
        return {
            "Bybit": {"IRYS": entry, "DUST": dict(entry, oi=1e3, volume=1e3)},
            "Hyperliquid": {"BTC": entry},
            "Lighter": {"IRYS": entry},
        }

    def book(self, v, a, kind="perp"):
        self.calls.append(("wide.book", v, a))
        return dict(bids=[(1.0, 1.0)], asks=[(1.1, 1.0)], received=T0)

    def funding_since(self, v, a, start):
        self.calls.append(("wide.funding", v, a, start))
        return []

    def current_funding(self, v, a):
        self.calls.append(("wide.current", v, a))
        return dict(raw_rate=0.0001)

    def history(self, v, a, days, kind="perp", at=None):
        return dict(prices={T0: 1.0}, price_type="trade"), None, None


class FakeEngine:
    def __init__(self):
        self.feed, self.wide = FakeFeed(), FakeWide()
        self.fees = {"Lighter": 0.0003, "Arcus": 0.0004}

    def fee(self, v):
        if v not in self.fees:
            raise ValueError(v + ": taker ücreti bilinmiyor")
        return self.fees[v]


def legacy_rows(e):
    """The pre-facade EngineSource.market_rows algorithm, kept as the reference."""
    rows = []
    for v in connectors.FEED_VENUES:
        try:
            fee = e.fee(v)
        except ValueError:
            fee = None
        for a, m in list(e.feed.meta.get(v, {}).items()):
            if not m or m.get("rate") is None:
                continue
            interval = m.get("interval")
            raw = m.get("raw_rate")
            if raw is None and interval:
                raw = m["rate"] * interval / 3600
            rows.append(
                dict(
                    asset=a,
                    venue=v,
                    kind="perp",
                    mark=m["mark"],
                    rate=m["rate"],
                    raw_rate=raw,
                    interval=interval,
                    contract_size=1.0,
                    index=m.get("index"),
                    next=m.get("next"),
                    received=m["received"],
                    fee=fee,
                    leverage=m.get("max_leverage"),
                    price_type="mark",
                    rfq=bool(m.get("rfq")),
                    step=m.get("step"),
                    minimum=m.get("minimum"),
                    min_quote=m.get("min_quote"),
                )
            )
    extra, _ = e.wide.snapshot()
    rows += [
        r
        for r in extra
        if r["kind"] == "perp" and r["venue"] not in connectors.FEED_VENUES
    ]
    return rows + universe_rows(e.wide.universe_snapshot(), rows, set())


class FacadeTests(unittest.TestCase):
    def setUp(self):
        self.e = FakeEngine()
        self.c = Connectors(self.e)

    def test_rows_match_the_pre_facade_algorithm(self):
        self.assertEqual(self.c.market_rows(), legacy_rows(self.e))
        # A feed venue without a fee is listed and says why it cannot rank.
        self.assertIn("ücret", self.c.excluded["RISEx"])
        self.assertNotIn("Lighter", self.c.excluded)

    def test_each_venue_routes_to_its_own_backend(self):
        self.assertIsInstance(self.c.get("Lighter"), NativeConnector)
        self.assertIsInstance(self.c.get("Hyperliquid"), CcxtConnector)
        self.assertIs(self.c.get("Lighter"), self.c.get("Lighter"))
        self.c.get("Arcus").book("BTC")
        self.c.get("Paradex").book("ETH")
        self.c.get("Lighter").funding("BTC", T0 - 10)
        self.c.get("Paradex").funding("BTC", T0 - 10)
        self.c.get("Paradex").current_funding("BTC")
        self.assertEqual(
            self.e.feed.calls,
            [
                ("feed.book", "Arcus", "BTC"),
                ("feed.funding", "Lighter", "BTC", T0 - 10),
            ],
        )
        self.assertEqual(
            self.e.wide.calls,
            [
                ("wide.book", "Paradex", "ETH"),
                ("wide.funding", "Paradex", "BTC", T0 - 10),
                ("wide.current", "Paradex", "BTC"),
            ],
        )
        with self.assertRaises(ValueError):
            self.c.get("Lighter").current_funding("BTC")

    def test_candle_labels_follow_the_source(self):
        self.assertEqual(self.c.get("Lighter").candles("BTC")["price_type"], "trade")
        self.assertEqual(self.c.get("Arcus").candles("BTC")["price_type"], "oracle")
        self.assertEqual(
            self.c.get("Hyperliquid").candles("BTC")["price_type"], "trade"
        )

    def test_declarations_come_from_the_registry(self):
        self.assertEqual(
            self.c.get("Arcus").declaration(),
            connectors.capabilities.DECLARED["Arcus"],
        )
        self.assertIsNone(self.c.get("Binance").declaration())
        self.assertEqual(self.c.get("Lighter").kind, "DEX")


class TypedViewTests(unittest.TestCase):
    def test_snapshots_carry_normalized_instruments(self):
        c = Connectors(FakeEngine())
        snaps = {(s.venue, s.asset): s for s in c.snapshots()}
        s = snaps[("Lighter", "BTC")]
        self.assertIsInstance(s, MarketSnapshot)
        self.assertEqual((s.interval, s.fee), (3600, 0.0003))
        self.assertAlmostEqual(s.raw_rate, 0.0001)
        self.assertAlmostEqual(s.premium, 100.0 / 99.9 - 1)
        self.assertTrue(s.instrument.tradable_metadata)
        self.assertEqual(s.instrument.source, "Yerel besleme")
        risex = snaps[("RISEx", "BTC")]
        # Unknown schedule: unknown, never a default; the instrument is not tradable.
        self.assertIsNone(risex.interval)
        self.assertIsNone(risex.raw_rate)
        self.assertFalse(risex.instrument.tradable_metadata)
        self.assertIsNone(risex.premium)
        self.assertEqual(
            snaps[("Bybit", "IRYS")].instrument.source, "CCXT toplu tarama"
        )
        self.assertNotIn(("Bybit", "DUST"), snaps)

    def test_books_and_payments_have_typed_views(self):
        c = Connectors(FakeEngine())
        b = c.get("Lighter").book_typed("BTC")
        self.assertIsInstance(b, Book)
        self.assertTrue(b.valid)
        self.assertEqual((b.mid, round(b.spread_bps)), (100.0, 200))
        crossed = Book.from_dict("X", "A", dict(bids=[(2, 1)], asks=[(1, 1)]))
        self.assertFalse(crossed.valid)
        (p,) = c.get("Lighter").funding_typed("BTC", T0 - 10)
        self.assertEqual(
            (p.venue, p.rate, p.value, p.audit_id), ("Lighter", 0.0001, None, 7)
        )


class StoredFallbackTests(unittest.TestCase):
    def test_kept_closes_keep_the_oracle_label_when_the_live_read_fails(self):
        import wide_paper

        e = FakeEngine()
        e.feed.candles = lambda v, a: (_ for _ in ()).throw(ValueError("down"))
        closes = {T0 - i * 3600: 1.0 for i in range(10)}
        e.history = type(
            "H", (), {"db": type("D", (), {"candles": lambda *a: closes})()}
        )()
        e.cfg = {"symbols": ["BTC"]}
        e.feed.meta["Arcus"]["BTC"]["price_type"] = "oracle"
        e.wide.histories = {}
        import threading

        e.wide.lock = threading.RLock()
        src = wide_paper.EngineSource(e)
        out = src.prices("Arcus", "BTC")
        self.assertEqual(out["price_type"], "oracle")
        self.assertEqual(src.prices("Lighter", "BTC")["price_type"], "trade")


if __name__ == "__main__":
    unittest.main()
