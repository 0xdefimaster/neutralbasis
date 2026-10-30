"""Asset and pair pages: every venue's current funding for one asset, and one long/short
pair's legs with realized funding over fixed windows.

Rates are hourly; realized payments are per-interval rates as the venue published them.
A window total is given only when the stored payments cover the whole window; a short
window is reported with its payment count instead of being scaled up.
"""

import threading
from bot import now

FRESH_SECONDS = 300
PRICE_FRESH_SECONDS = 90
# Marks further apart than this are treated as different tokens sharing a ticker.
MAX_PRICE_GAP = 0.15
WINDOWS = (
    ("1d", 86400),
    ("3d", 3 * 86400),
    ("7d", 7 * 86400),
    ("14d", 14 * 86400),
    ("30d", 30 * 86400),
)
SYNC_COOLDOWN = 600


def merge_legs(native, rows, universe, kinds, labels, at=None):
    """{venue: leg} for one asset. Earlier sources win per field (native feed, selected
    scan, bulk universe); stale funding or prices are dropped, not shown as current."""
    at = at or now()
    out = {}
    for src in (native, rows, universe):
        for v, x in src.items():
            leg = out.setdefault(
                v,
                dict(
                    venue=v,
                    label=labels.get(v, v),
                    kind=kinds.get(v, "DEX"),
                    rate=None,
                    interval=None,
                    next=None,
                    fee=None,
                    mark=None,
                    volume=None,
                    oi=None,
                    leverage=None,
                    rate_at=None,
                    mark_at=None,
                ),
            )
            received = x.get("received") or 0
            fresh = at - received <= FRESH_SECONDS
            if (
                fresh
                and leg["rate"] is None
                and x.get("rate") is not None
                and x.get("interval")
            ):
                leg["rate"], leg["interval"] = x["rate"], x["interval"]
                leg["rate_at"] = received or None
                nxt = x.get("next")
                leg["next"] = nxt if nxt and nxt > at - x["interval"] else None
            if (
                leg["mark"] is None
                and x.get("mark")
                and at - received <= PRICE_FRESH_SECONDS
            ):
                leg["mark"] = x["mark"]
                leg["mark_at"] = received or None
            for k in ("fee", "volume", "oi", "leverage"):
                if leg[k] is None and x.get(k) is not None:
                    leg[k] = x[k]
    return {v: x for v, x in out.items() if x["rate"] is not None}


def realized(rows, interval, at=None):
    """Per window: sum of published per-interval rates, payment count, expected count and
    whether stored payments cover the window."""
    at = at or now()
    out = {}
    for key, span in WINDOWS:
        picked = [r["rate"] for r in rows if at - span < r["ts"] <= at]
        expected = int(span / interval) if interval else None
        out[key] = dict(
            sum=sum(picked),
            count=len(picked),
            expected=expected,
            complete=bool(expected) and len(picked) >= expected,
        )
    return out


def cumulative(long_rows, short_rows, start):
    """Cumulative funding earned per unit notional: short receives, long pays."""
    events = [(r["ts"], -r["rate"]) for r in long_rows if r["ts"] > start] + [
        (r["ts"], r["rate"]) for r in short_rows if r["ts"] > start
    ]
    total = 0.0
    out = []
    for ts, x in sorted(events):
        total += x
        out.append(dict(ts=ts, value=total))
    return out


def common_start(long_rows, short_rows, floor):
    """Start the path where both legs have stored payments, so a one-leg stretch does not
    read as pair funding."""
    if not long_rows or not short_rows:
        return floor
    first = max(min(r["ts"] for r in long_rows), min(r["ts"] for r in short_rows))
    return max(floor, first - 1)


def price_gap(long_leg, short_leg):
    """Short mark over long mark minus one; None when unknown or implausibly wide."""
    lm, sm = long_leg.get("mark"), short_leg.get("mark")
    if not lm or not sm:
        return None
    gap = sm / lm - 1
    return gap if abs(gap) <= MAX_PRICE_GAP else None


def asset_detail(asset, legs, funding, at=None):
    """Every venue for one asset with realized totals, plus the widest funding pair and
    the widest plausible price gap. `funding`: {venue: stored rows}."""
    at = at or now()
    venues = []
    for v, leg in legs.items():
        venues.append(dict(leg, paid=realized(funding.get(v, []), leg["interval"], at)))
    venues.sort(key=lambda x: -x["rate"])
    best = None
    if len(venues) > 1:
        hi, lo = venues[0], venues[-1]
        best = dict(long=lo["venue"], short=hi["venue"], spread=hi["rate"] - lo["rate"])
    widest = None
    priced = [x for x in venues if x["mark"]]
    for lo in priced:
        for hi in priced:
            g = price_gap(lo, hi)
            if (
                lo is not hi
                and g is not None
                and g > 0
                and (not widest or g > widest["gap"])
            ):
                widest = dict(long=lo["venue"], short=hi["venue"], gap=g)
    return dict(
        asset=asset, ts=at, venues=venues, best_funding=best, widest_price=widest
    )


def price_history(long_prices, short_prices, at):
    """Hourly short/long gap on hours both legs closed, and its mean per window. A window
    mean is given only when at least 90% of its hours have both closes."""
    hours = sorted(set(long_prices) & set(short_prices))
    points = [
        dict(
            ts=t,
            long_price=long_prices[t],
            short_price=short_prices[t],
            gap=short_prices[t] / long_prices[t] - 1,
        )
        for t in hours
        if long_prices[t] > 0 and short_prices[t] > 0
    ]
    # Same-ticker clashes would swamp the mean; such a pair has no usable gap history.
    if any(abs(p["gap"]) > MAX_PRICE_GAP for p in points):
        points = []
    windows = {}
    by_ts = {p["ts"]: p for p in points}
    last = points[-1] if points else None
    for key, span in WINDOWS:
        picked = [p["gap"] for p in points if at - span < p["ts"] <= at]
        expected = int(span / 3600)
        complete = len(picked) >= expected * 0.9
        # The window's first common close, at most an hour late (a 30-day store holds 720
        # closes, one short of a close exactly 30 days before the last); the move states
        # its own start.
        then = (
            by_ts.get(last["ts"] - span) or by_ts.get(last["ts"] - span + 3600)
            if last
            else None
        )
        windows[key] = dict(
            mean=sum(picked) / len(picked) if complete else None,
            low=min(picked) if complete else None,
            high=max(picked) if complete else None,
            count=len(picked),
            expected=expected,
            move=price_move(then, last),
        )
    return points, windows


def price_move(then, last):
    """Both legs' return between two common closes and what long/short made on it per $1
    notional (long return − short return); None without a close at the window start."""
    if not then or not last:
        return None
    lr = last["long_price"] / then["long_price"] - 1
    sr = last["short_price"] / then["short_price"] - 1
    return dict(start=then["ts"], end=last["ts"], long=lr, short=sr, pnl=lr - sr)


def book_cost(book, usd, buy, fee):
    """One-side entry cost from mid at a USD size: taker fee plus slippage, in bp.
    `fits` is False when the visible book cannot fill the size."""
    bid, ask = max(p for p, _ in book["bids"]), min(p for p, _ in book["asks"])
    mid = (bid + ask) / 2
    out = dict(usd=usd, half_spread_bp=(ask - bid) / 2 / mid * 1e4)
    try:
        from bot import walk

        vwap = walk(book["asks"] if buy else book["bids"], usd / mid, buy)
    except ValueError:
        return dict(out, fits=False, bp=None, cost=None, slip_bp=None, fee_bp=None)
    slip = vwap / mid - 1 if buy else 1 - vwap / mid
    bp = (slip + (fee or 0)) * 1e4
    return dict(
        out,
        fits=True,
        bp=bp,
        cost=bp / 1e4 * usd,
        fee_known=fee is not None,
        slip_bp=slip * 1e4,
        fee_bp=(fee or 0) * 1e4,
    )


BOOK_SIZES = (10_000, 100_000, 1_000_000)
CAPACITY_BPS = (10, 25, 50)


def capacity(book, buy):
    """USD the visible book fills on one side while the worst fill stays within each
    limit of mid, and the side's whole visible depth."""
    bid, ask = max(p for p, _ in book["bids"]), min(p for p, _ in book["asks"])
    mid = (bid + ask) / 2
    side = book["asks"] if buy else book["bids"]
    out = {}
    for bp in CAPACITY_BPS:
        edge = mid * (1 + bp / 1e4) if buy else mid * (1 - bp / 1e4)
        out[str(bp)] = sum(p * q for p, q in side if (p <= edge if buy else p >= edge))
    return out, sum(p * q for p, q in side)


def pair_detail(asset, long, short, legs, funding, at=None, prices=None):
    """One pair: both legs, realized windows per leg and for the pair, the cumulative
    funding path over the stored 30 days and, given stored closes, the price gap history.
    """
    at = at or now()
    if long == short:
        raise ValueError("Long ve short aynı borsa olamaz")
    for v in (long, short):
        if v not in legs:
            raise ValueError(v + ": bu varlık için güncel funding verisi yok")
    L, S = legs[long], legs[short]
    lf, sf = funding.get(long, []), funding.get(short, [])
    lp = realized(lf, L["interval"], at)
    sp = realized(sf, S["interval"], at)
    windows = {
        k: dict(
            funding=sp[k]["sum"] - lp[k]["sum"],
            complete=lp[k]["complete"] and sp[k]["complete"],
            long_count=lp[k]["count"],
            short_count=sp[k]["count"],
            long_expected=lp[k]["expected"],
            short_expected=sp[k]["expected"],
        )
        for k, _ in WINDOWS
    }
    fee = (
        2 * (L["fee"] + S["fee"])
        if L["fee"] is not None and S["fee"] is not None
        else None
    )
    points, price_windows = price_history(
        (prices or {}).get(long, {}), (prices or {}).get(short, {}), at
    )
    return dict(
        asset=asset,
        ts=at,
        price_points=points,
        price_windows=price_windows,
        long=dict(L, paid=lp, last_payment=max((r["ts"] for r in lf), default=None)),
        short=dict(S, paid=sp, last_payment=max((r["ts"] for r in sf), default=None)),
        funding_gap=S["rate"] - L["rate"],
        price_gap=price_gap(L, S),
        fee_roundtrip=fee,
        windows=windows,
        path=cumulative(lf, sf, common_start(lf, sf, at - WINDOWS[-1][1])),
        others=sorted(
            (
                dict(
                    venue=v,
                    label=x["label"],
                    kind=x["kind"],
                    rate=x["rate"],
                    interval=x["interval"],
                )
                for v, x in legs.items()
            ),
            key=lambda x: -x["rate"],
        ),
    )


class PairViews:
    """Collects one asset's legs from the engine's feeds and fills stored funding on request."""

    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()
        self.syncing = {}

    def legs(self, asset):
        from risk_scan import VENUES
        from wide_markets import CATALOG, LABELS

        e = self.engine
        native = {}
        for v in VENUES:
            m = e.feed.meta.get(v, {}).get(asset)
            if not m:
                continue
            try:
                fee = e.fee(v)
            except ValueError:
                fee = None
            native[v] = dict(
                rate=m.get("rate"),
                interval=m.get("interval"),
                next=m.get("next"),
                fee=fee,
                mark=m.get("mark"),
                leverage=m.get("max_leverage"),
                received=m.get("received"),
            )
        rows, _ = e.wide.snapshot()
        selected = {
            r["venue"]: r
            for r in rows
            if r["asset"] == asset and r.get("kind", "perp") == "perp"
        }
        universe = {
            v: x[asset] for v, x in e.wide.universe_snapshot().items() if asset in x
        }
        kinds = {v: c["kind"] for v, c in CATALOG.items()}
        legs = merge_legs(native, selected, universe, kinds, LABELS)
        # The fee the paper desk charges: the user's own account fee where entered.
        user = getattr(getattr(e, "wide_paper", None), "fees", None) or {}
        for v, leg in legs.items():
            if v in user:
                leg["fee"] = user[v]
            leg["fee_source"] = (
                "user" if v in user else "public" if leg["fee"] is not None else None
            )
        return legs

    def funding(self, asset, venues):
        start = now() - WINDOWS[-1][1]
        return {v: self.engine.history.db.funding(v, asset, start) for v in venues}

    def asset_view(self, asset):
        asset = str(asset or "").upper()
        legs = self.legs(asset)
        if not legs:
            raise ValueError(asset + ": güncel funding verisi olan borsa yok")
        out = asset_detail(asset, legs, self.funding(asset, legs))
        out["syncing"] = self.sync_state(asset)
        return out

    def prices(self, asset, venues):
        start = now() - WINDOWS[-1][1] - 3600
        return {
            v: self.engine.history.db.candles(v, asset, "perp", start) for v in venues
        }

    def pair_view(self, asset, long, short):
        asset = str(asset or "").upper()
        legs = self.legs(asset)
        out = pair_detail(
            asset,
            long,
            short,
            legs,
            self.funding(asset, (long, short)),
            prices=self.prices(asset, (long, short)),
        )
        out["syncing"] = self.sync_state(asset)
        out["coverage"] = {v: self.coverage(asset, v, legs) for v in (long, short)}
        wide = getattr(self.engine, "wide_paper", None)
        try:
            out["paper"] = wide.verdict(asset, long, short) if wide else None
        except Exception:
            out["paper"] = None
        return out

    def coverage(self, asset, v, legs):
        """Stored 30-day history for one leg (candles x/720, funding x/expected)."""
        keeper = getattr(self.engine, "history", None)
        if keeper is None:
            return None
        st = keeper.market_state(v, asset)
        cov = st.get("coverage") or keeper.coverage(
            v, asset, "perp", (legs.get(v) or {}).get("interval")
        )
        return dict(cov, error=None if st.get("ok", True) else st.get("error"))

    def sync_state(self, asset):
        with self.lock:
            return {v: s for (v, a), s in self.syncing.items() if a == asset}

    def request_sync(self, asset, venues):
        """Fill 30 days of realized funding for the given legs in the background."""
        asset = str(asset or "").upper()
        legs = self.legs(asset)
        venues = [v for v in venues if v in legs][:4]
        if not venues:
            raise ValueError("Doldurulacak güncel piyasa yok")
        started = []
        with self.lock:
            for v in venues:
                s = self.syncing.get((v, asset))
                if s and (s["status"] == "running" or now() - s["ts"] < SYNC_COOLDOWN):
                    continue
                self.syncing[(v, asset)] = dict(status="running", ts=now())
                started.append(v)

        def run():
            for v in started:
                try:
                    added = self.engine.history.sync(
                        v, asset, "perp", legs[v]["interval"]
                    )
                    added += self.fill_candles(v, asset)
                    state = dict(status="done", ts=now(), added=added)
                except Exception as ex:
                    state = dict(status="error", ts=now(), error=str(ex)[:200])
                with self.lock:
                    self.syncing[(v, asset)] = state

        if started:
            threading.Thread(target=run, daemon=True, name="pair-sync").start()
        return dict(ok=True, started=started)

    def fill_candles(self, v, asset):
        """Closed 1h closes for a leg the background keeper skips (outside the selected
        assets); stored next to the keeper's own rows."""
        from risk_scan import VENUES
        from bot import hour

        e = self.engine
        if v in VENUES or asset in e.cfg["symbols"]:
            return 0
        db = e.history.db
        end = hour(now())
        last = db.last_candle(v, asset, "perp")
        start = max(end - WINDOWS[-1][1], (last + 3600) if last is not None else 0)
        if start >= end:
            return 0
        prices = e.wide.candles_since(v, asset, "perp", start, end)
        return db.add_candles(v, asset, "perp", prices)

    def books(self, asset, long, short, size=None):
        """Current books of both legs: entry and exit cost at the page's leg size (taker
        fee and slippage from mid apart), the entry cost at the reference sizes, how
        much each side fills within 10/25/50 bp, and when the book was read."""
        from risk_scan import VENUES
        from bot import number

        asset = str(asset or "").upper()
        if size is not None:
            size = number(size)
            if not 10 <= size <= 10_000_000:
                raise ValueError("Bacak büyüklüğü 10 ile 10.000.000 $ arasında olmalı")
        legs = self.legs(asset)
        out = dict(asset=asset, ts=now(), size=size, legs={})
        for v, buy in ((long, True), (short, False)):
            if v not in legs:
                raise ValueError(v + ": bu varlık için güncel piyasa yok")
            try:
                book = (
                    self.engine.feed.book(v, asset)
                    if v in VENUES
                    else self.engine.wide.book(v, asset)
                )
                fee = legs[v]["fee"]
                entry_cap, depth = capacity(book, buy)
                exit_cap, _ = capacity(book, not buy)
                out["legs"][v] = dict(
                    side="long" if buy else "short",
                    fee=fee,
                    fee_source=legs[v].get("fee_source"),
                    received=book.get("received"),
                    sizes=[book_cost(book, usd, buy, fee) for usd in BOOK_SIZES],
                    # Round trip: the thinner of the entry and the exit side.
                    capacity={k: min(entry_cap[k], exit_cap[k]) for k in entry_cap},
                    depth=depth,
                    entry=book_cost(book, size, buy, fee) if size else None,
                    exit=book_cost(book, size, not buy, fee) if size else None,
                )
            except Exception as ex:
                out["legs"][v] = dict(
                    side="long" if buy else "short", error=str(ex)[:200]
                )
        return out
