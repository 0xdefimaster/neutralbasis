"""Asset x venue funding matrix over every listed perp the scan can reach.

Rates are hourly (the panel scales them to its display period). `paid7` is the sum of
stored realized funding over the last 7 days, given only when the stored payments cover
the whole window; nothing is estimated or interpolated.
"""

import math, threading
from bot import now
from risk_scan import VENUES

CACHE_SECONDS = 60
FRESH_SECONDS = 300
WEEK = 7 * 86400
MAX_PRICE_GAP = 0.15


def paid7(sums, v, a, interval):
    s = sums.get((v, a))
    if not s or not interval:
        return None
    total, count = s
    return total if count >= int(WEEK / interval) else None


def price_spread(cells):
    """Widest perp mark gap: long the cheapest venue, short the dearest. A gap above
    MAX_PRICE_GAP is almost always two different tokens sharing a ticker, so the mark
    farthest from the median is dropped until the rest agree."""
    marks = sorted((c[4], v) for v, c in cells.items() if c[4])
    while len(marks) > 1 and math.log(marks[-1][0] / marks[0][0]) > MAX_PRICE_GAP:
        median = marks[len(marks) // 2][0]
        marks.remove(max(marks, key=lambda m: abs(math.log(m[0] / median))))
    if len(marks) < 2:
        return dict(price_spread=None, price_long=None, price_short=None)
    return dict(
        price_spread=math.log(marks[-1][0] / marks[0][0]),
        price_long=marks[0][1],
        price_short=marks[-1][1],
    )


def build(native, rows, universe, sums, kinds, labels, at=None):
    """native/rows: lists of dicts with venue, asset, rate (hourly), interval, fee;
    cells are [rate, interval, fee, paid7, mark, volume 24h, open interest] (USD);
    universe: {venue: {asset: entry}}; sums: {(venue, asset): (sum, count)} over 7d.
    Earlier sources win for the same cell (native feed, selected scan, bulk universe).
    """
    at = at or now()
    cells = {}

    def put(v, a, rate, interval, fee, received, mark=None, volume=None, oi=None):
        if rate is None or not interval or at - (received or 0) > FRESH_SECONDS:
            return
        cells.setdefault(a, {}).setdefault(
            v,
            [
                rate,
                interval,
                fee,
                paid7(sums, v, a, interval),
                mark if mark and mark > 0 else None,
                volume,
                oi,
            ],
        )

    for r in native:
        put(
            r["venue"],
            r["asset"],
            r.get("rate"),
            r.get("interval"),
            r.get("fee"),
            r["received"],
            r.get("mark"),
        )
    for r in rows:
        if r.get("kind", "perp") == "perp":
            put(
                r["venue"],
                r["asset"],
                r.get("rate"),
                r.get("interval"),
                r.get("fee"),
                r["received"],
                r.get("mark"),
                r.get("volume"),
                r.get("oi"),
            )
    for v, entries in universe.items():
        for a, x in entries.items():
            put(
                v,
                a,
                x["rate"],
                x["interval"],
                x.get("fee"),
                x["received"],
                x.get("mark"),
                x.get("volume"),
                x.get("oi"),
            )
    assets = []
    for a, cs in cells.items():
        rates = [c[0] for c in cs.values()]
        assets.append(
            dict(
                asset=a,
                cells=cs,
                max_spread=max(rates) - min(rates) if len(rates) > 1 else None,
                **price_spread(cs),
            )
        )
    assets.sort(
        key=lambda x: (x["max_spread"] is None, -(x["max_spread"] or 0), x["asset"])
    )
    used = {v for x in assets for v in x["cells"]}
    count = {v: sum(v in x["cells"] for x in assets) for v in used}
    # Native-feed venues publish no volume or OI here; the panel treats their unknown
    # liquidity as curated (selected assets only) instead of illiquid.
    curated = {r["venue"] for r in native}
    venues = [
        dict(
            id=v,
            label=labels.get(v, v),
            kind=kinds.get(v, "DEX"),
            assets=count[v],
            curated=v in curated,
        )
        for v in sorted(used, key=lambda v: (-count[v], v))
    ]
    return dict(ts=at, venues=venues, assets=assets)


def top_pairs(matrix, limit=40):
    """(asset, venue) markets of the widest spreads, for the history keeper's funding sync."""
    out = []
    for x in matrix["assets"]:
        if x["max_spread"] is None:
            continue
        if len(out) >= limit * 2:
            break
        cs = x["cells"]
        hi = max(cs, key=lambda v: cs[v][0])
        lo = min(cs, key=lambda v: cs[v][0])
        out += [(hi, x["asset"], cs[hi][1]), (lo, x["asset"], cs[lo][1])]
    return out


def track_listings(state, matrix, at=None, keep=WEEK):
    """New listings since tracking began. A venue's first scan only seeds what it already
    lists; later, an asset no venue listed before is a new listing, and venues adding it
    within `keep` are appended. Returns (state, listings newest first)."""
    at = at or now()
    seen = {v: set(a) for v, a in state.get("seen", {}).items()}
    first = {a: x for a, x in state.get("first", {}).items() if at - x["ts"] <= keep}
    known = set().union(*seen.values()) if seen else set()
    for x in matrix["assets"]:
        a = x["asset"]
        for v in x["cells"]:
            if v not in seen:
                continue
            if a in seen[v]:
                continue
            if a in first:
                first[a]["venues"].append(v)
            elif a not in known:
                first[a] = dict(ts=at, venues=[v])
            seen[v].add(a)
    for x in matrix["assets"]:
        for v in x["cells"]:
            if v not in state.get("seen", {}):
                seen.setdefault(v, set()).add(x["asset"])
    out = dict(seen={v: sorted(a) for v, a in seen.items()}, first=first)
    listings = sorted(
        (dict(asset=a, **x) for a, x in first.items()),
        key=lambda x: (-len(x["venues"]), -x["ts"]),
    )
    return out, listings


class MatrixCache:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()
        self.value = None

    def collect(self):
        from wide_markets import CATALOG, LABELS

        e = self.engine
        native = []
        for v in VENUES:
            try:
                fee = e.fee(v)
            except ValueError:
                fee = None
            for a, m in e.feed.meta.get(v, {}).items():
                native.append(
                    dict(
                        venue=v,
                        asset=a,
                        rate=m.get("rate"),
                        mark=m.get("mark"),
                        interval=m.get("interval") or 3600,
                        fee=fee,
                        received=m.get("received"),
                    )
                )
        rows, _ = e.wide.snapshot()
        sums = e.history.db.funding_sums(now() - WEEK)
        kinds = {v: c["kind"] for v, c in CATALOG.items()}
        m = build(native, rows, e.wide.universe_snapshot(), sums, kinds, LABELS)
        state, m["listings"] = track_listings(e.store.get("matrix_listings", {}), m)
        e.store.put("matrix_listings", state)
        return m

    def get(self):
        with self.lock:
            if self.value is None or now() - self.value["ts"] >= CACHE_SECONDS:
                self.value = self.collect()
            return self.value
