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
                nxt = x.get("next")
                leg["next"] = nxt if nxt and nxt > at - x["interval"] else None
            if (
                leg["mark"] is None
                and x.get("mark")
                and at - received <= PRICE_FRESH_SECONDS
            ):
                leg["mark"] = x["mark"]
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


def pair_detail(asset, long, short, legs, funding, at=None):
    """One pair: both legs, realized windows per leg and for the pair, and the cumulative
    funding path over the stored 30 days."""
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
    return dict(
        asset=asset,
        ts=at,
        long=dict(L, paid=lp),
        short=dict(S, paid=sp),
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
                interval=m.get("interval") or 3600,
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
        return merge_legs(native, selected, universe, kinds, LABELS)

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

    def pair_view(self, asset, long, short):
        asset = str(asset or "").upper()
        legs = self.legs(asset)
        out = pair_detail(asset, long, short, legs, self.funding(asset, (long, short)))
        out["syncing"] = self.sync_state(asset)
        return out

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
                    state = dict(status="done", ts=now(), added=added)
                except Exception as ex:
                    state = dict(status="error", ts=now(), error=str(ex)[:200])
                with self.lock:
                    self.syncing[(v, asset)] = state

        if started:
            threading.Thread(target=run, daemon=True, name="pair-sync").start()
        return dict(ok=True, started=started)
