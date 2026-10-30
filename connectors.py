"""Exchange connector facade: one typed interface over every data source, so the paper
desk and the strategies never depend on one exchange's internal API.

Each venue has one connector:

  * NativeConnector - the desk's own feed (Lighter RH, Extended, TxFlow, RISEx, Arcus):
    quantities in base units, rates and schedules as each API publishes them;
  * CcxtConnector  - a venue reached through the CCXT scanner (selected assets plus
    the bulk-funding universe).

Every connector offers the same calls (markets, book, funding history, current
funding, live candles) and its capability declaration. Market rows keep the dict
form the desk already uses (`MarketSnapshot.row`), with typed, normalized views beside
them (`MarketSnapshot`, `Instrument`, `Book`, `FundingPayment`). A future native
connector (WebSocket books, account reads) plugs in without touching the strategies.
"""

from dataclasses import dataclass, field
from typing import Optional
import capabilities

# Venues whose market data comes from the desk's own feed instead of the CCXT scanner.
FEED_VENUES = ("Lighter", "Extended", "TxFlow", "RISEx", "Arcus")
# Feed venues whose candles are trades of the venue itself (the others may be oracle).
NATIVE = ("Lighter", "Extended")
# A bulk-scan market of an asset the desk does not hold needs this much open interest
# (or volume, where OI is unknown) to be considered.
UNIVERSE_MIN_USD = 1_000_000


@dataclass(frozen=True)
class Instrument:
    """What one venue lists for one asset, as far as its source publishes it. None
    always means unknown, never a default."""

    venue: str
    asset: str
    kind: str
    symbol: Optional[str]
    contract_size: Optional[float]
    step: Optional[float]
    minimum: Optional[float]
    min_quote: Optional[float]
    max_leverage: Optional[float]
    interval: Optional[float]
    quote: Optional[str]
    source: str

    @property
    def tradable_metadata(self):
        """Contract size and schedule are known: sizes and payments can be computed."""
        return bool(self.contract_size) and bool(self.interval)


@dataclass(frozen=True)
class MarketSnapshot:
    """One fresh market observation. `rate` is per hour, `raw_rate` per payment."""

    venue: str
    asset: str
    kind: str
    mark: Optional[float]
    index: Optional[float]
    rate: Optional[float]
    raw_rate: Optional[float]
    interval: Optional[float]
    next_funding: Optional[float]
    fee: Optional[float]
    oi: Optional[float]
    volume: Optional[float]
    received: Optional[float]
    instrument: Instrument
    row: dict = field(repr=False, compare=False)

    @classmethod
    def from_row(cls, r, source):
        inst = Instrument(
            venue=r["venue"],
            asset=r["asset"],
            kind=r.get("kind", "perp"),
            symbol=r.get("symbol"),
            contract_size=r.get("contract_size"),
            step=r.get("step"),
            minimum=r.get("minimum"),
            min_quote=r.get("min_quote"),
            max_leverage=r.get("leverage"),
            interval=r.get("interval"),
            quote=r.get("quote"),
            source=source,
        )
        return cls(
            venue=r["venue"],
            asset=r["asset"],
            kind=r.get("kind", "perp"),
            mark=r.get("mark"),
            index=r.get("index"),
            rate=r.get("rate"),
            raw_rate=r.get("raw_rate"),
            interval=r.get("interval"),
            next_funding=r.get("next"),
            fee=r.get("fee"),
            oi=r.get("oi"),
            volume=r.get("volume"),
            received=r.get("received"),
            instrument=inst,
            row=r,
        )

    @property
    def premium(self):
        """Mark over index minus one; None when either is unknown."""
        if not self.mark or not self.index:
            return None
        return self.mark / self.index - 1


@dataclass(frozen=True)
class Book:
    venue: str
    asset: str
    bids: tuple
    asks: tuple
    received: Optional[float]
    audit_id: Optional[int]

    @classmethod
    def from_dict(cls, venue, asset, b):
        return cls(
            venue=venue,
            asset=asset,
            bids=tuple(sorted(b["bids"], reverse=True)),
            asks=tuple(sorted(b["asks"])),
            received=b.get("received"),
            audit_id=b.get("audit_id"),
        )

    @property
    def valid(self):
        return bool(self.bids and self.asks) and self.bids[0][0] < self.asks[0][0]

    @property
    def mid(self):
        return (self.bids[0][0] + self.asks[0][0]) / 2

    @property
    def spread_bps(self):
        return (self.asks[0][0] - self.bids[0][0]) / self.mid * 1e4


@dataclass(frozen=True)
class FundingPayment:
    """One settled payment as the venue published it: `rate` per payment interval,
    `value` the per-unit cash when the venue publishes it (else None)."""

    venue: str
    asset: str
    ts: float
    rate: float
    value: Optional[float]
    audit_id: Optional[int]


class Connector:
    """The interface every venue offers to the desk."""

    venue = None
    native = False

    def __init__(self, engine, venue):
        self.engine = engine
        self.venue = venue

    @property
    def kind(self):
        spec = _catalog().get(self.venue) or {}
        return spec.get("kind") or ("DEX" if self.native else "CEX")

    def declaration(self):
        return capabilities.DECLARED.get(self.venue)

    def markets(self):
        """[MarketSnapshot] of this venue's fresh perp markets."""
        raise NotImplementedError

    def book(self, asset):
        """The venue's order book as a dict (bids, asks, received, audit_id)."""
        raise NotImplementedError

    def book_typed(self, asset):
        return Book.from_dict(self.venue, asset, self.book(asset))

    def funding(self, asset, start):
        """Settled payments after `start` as dicts (ts, rate, value, audit_id)."""
        raise NotImplementedError

    def funding_typed(self, asset, start):
        return [
            FundingPayment(
                self.venue,
                asset,
                r["ts"],
                r["rate"],
                r.get("value"),
                r.get("audit_id"),
            )
            for r in self.funding(asset, start)
        ]

    def current_funding(self, asset):
        raise NotImplementedError

    def candles(self, asset):
        """Live 1h closes: dict(prices, price_type, ...)."""
        raise NotImplementedError


class NativeConnector(Connector):
    native = True
    source = "Yerel besleme"

    def fee(self):
        """(fee, why-not) for the venue: the desk's configured or public taker fee."""
        try:
            return self.engine.fee(self.venue), None
        except ValueError as ex:
            return None, str(ex)

    def markets(self):
        e, v = self.engine, self.venue
        fee, _ = self.fee()
        out = []
        for a, m in list(e.feed.meta.get(v, {}).items()):
            if not m or m.get("rate") is None:
                continue
            # An unknown schedule stays unknown: the row is left out of the desk
            # ("Funding aralığı yok") instead of being taken for an hourly one.
            interval = m.get("interval")
            raw = m.get("raw_rate")
            if raw is None and interval:
                raw = m["rate"] * interval / 3600
            out.append(
                MarketSnapshot.from_row(
                    dict(
                        asset=a,
                        venue=v,
                        kind="perp",
                        mark=m["mark"],
                        rate=m["rate"],
                        raw_rate=raw,
                        interval=interval,
                        # The native APIs quote sizes in base units.
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
                    ),
                    self.source,
                )
            )
        return out

    def book(self, asset):
        return self.engine.feed.book(self.venue, asset)

    def funding(self, asset, start):
        return self.engine.feed.funding_history(self.venue, asset, start)

    def current_funding(self, asset):
        raise ValueError(self.venue + ": yerel besleme kendi tazeliğini tutar")

    def candles(self, asset):
        live = self.engine.feed.candles(self.venue, asset)
        if self.venue in NATIVE:
            return {**live, "price_type": "trade"}
        # Oracle-priced candles stay labelled; history checks refuse them.
        return live


class CcxtConnector(Connector):
    source = "CCXT"

    def markets(self):
        rows, _ = self.engine.wide.snapshot()
        return [
            MarketSnapshot.from_row(r, self.source)
            for r in rows
            if r["venue"] == self.venue and r["kind"] == "perp"
        ]

    def book(self, asset):
        return self.engine.wide.book(self.venue, asset)

    def funding(self, asset, start):
        return self.engine.wide.funding_since(self.venue, asset, start)

    def current_funding(self, asset):
        return self.engine.wide.current_funding(self.venue, asset)

    def candles(self, asset):
        live, _, _ = self.engine.wide.history(self.venue, asset, 30)
        return live


def _catalog():
    try:
        from wide_markets import CATALOG

        return CATALOG
    except Exception:
        return {}


def universe_rows(universe, rows, held=()):
    """Bulk-scan markets for every asset, as desk rows. Markets already covered by the
    native feed or the selected scan are skipped; a leg must carry UNIVERSE_MIN_USD of
    open interest (or volume) unless the desk already holds that asset."""
    have = {(r["venue"], r["asset"]) for r in rows}
    out = []
    for v, entries in universe.items():
        if v in NATIVE:
            continue
        for a, x in entries.items():
            if (v, a) in have:
                continue
            depth = x.get("oi") if x.get("oi") is not None else x.get("volume")
            if a not in held and (depth or 0) < UNIVERSE_MIN_USD:
                continue
            out.append(
                dict(
                    asset=a,
                    venue=v,
                    kind="perp",
                    symbol=x["symbol"],
                    mark=x.get("mark"),
                    price_type="mark",
                    rate=x["rate"],
                    raw_rate=x["raw_rate"],
                    interval=x["interval"],
                    next=x.get("next"),
                    received=x["received"],
                    funding_received=x["received"],
                    fee=x.get("fee"),
                    leverage=x.get("leverage"),
                    volume=x.get("volume"),
                    oi=x.get("oi"),
                    execution=False,
                    step=x.get("step"),
                    minimum=x.get("minimum"),
                    min_quote=x.get("min_quote"),
                    contract_size=x.get("contract_size"),
                    index=x.get("index"),
                )
            )
    return out


class Connectors:
    """One connector per venue, built on first use."""

    def __init__(self, engine):
        self.engine = engine
        self.by_venue = {}
        self.excluded = {}

    def get(self, venue):
        c = self.by_venue.get(venue)
        if c is None:
            cls = NativeConnector if venue in FEED_VENUES else CcxtConnector
            c = self.by_venue[venue] = cls(self.engine, venue)
        return c

    def snapshots(self):
        """[MarketSnapshot] for every venue: the feed venues, the scanner's selected
        assets, then the bulk-scan universe not already covered."""
        e = self.engine
        self.excluded = {}
        out = []
        for v in FEED_VENUES:
            c = self.get(v)
            fee, why = c.fee()
            if fee is None and e.feed.meta.get(v):
                # Listed without a fee: ranking skips it until the user enters one.
                self.excluded[v] = why
            out += c.markets()
        rows, _ = e.wide.snapshot()
        out += [
            MarketSnapshot.from_row(r, CcxtConnector.source)
            for r in rows
            if r["kind"] == "perp" and r["venue"] not in FEED_VENUES
        ]
        held = {
            p["asset"] for p in getattr(getattr(e, "wide_paper", None), "positions", [])
        }
        out += [
            MarketSnapshot.from_row(r, "CCXT toplu tarama")
            for r in universe_rows(
                e.wide.universe_snapshot(), [s.row for s in out], held
            )
        ]
        return out

    def market_rows(self):
        return [s.row for s in self.snapshots()]
