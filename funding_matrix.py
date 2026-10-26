"""Asset x venue funding matrix over every listed perp the scan can reach.

Rates are hourly (the panel scales them to its display period). `paid7` is the sum of
stored realized funding over the last 7 days, given only when the stored payments cover
the whole window; nothing is estimated or interpolated.
"""

import threading
from bot import now
from risk_scan import VENUES

CACHE_SECONDS = 60
FRESH_SECONDS = 300
WEEK = 7 * 86400


def paid7(sums, v, a, interval):
    s = sums.get((v, a))
    if not s or not interval:
        return None
    total, count = s
    return total if count >= int(WEEK / interval) else None


def build(native, rows, universe, sums, kinds, labels, at=None):
    """native/rows: lists of dicts with venue, asset, rate (hourly), interval, fee;
    universe: {venue: {asset: entry}}; sums: {(venue, asset): (sum, count)} over 7d.
    Earlier sources win for the same cell (native feed, selected scan, bulk universe).
    """
    at = at or now()
    cells = {}

    def put(v, a, rate, interval, fee, received):
        if rate is None or not interval or at - (received or 0) > FRESH_SECONDS:
            return
        cells.setdefault(a, {}).setdefault(
            v,
            [rate, interval, fee, paid7(sums, v, a, interval)],
        )

    for r in native:
        put(
            r["venue"],
            r["asset"],
            r.get("rate"),
            r.get("interval"),
            r.get("fee"),
            r["received"],
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
            )
    for v, entries in universe.items():
        for a, x in entries.items():
            put(v, a, x["rate"], x["interval"], x.get("fee"), x["received"])
    assets = []
    for a, cs in cells.items():
        rates = [c[0] for c in cs.values()]
        assets.append(
            dict(
                asset=a,
                cells=cs,
                max_spread=max(rates) - min(rates) if len(rates) > 1 else None,
            )
        )
    assets.sort(
        key=lambda x: (x["max_spread"] is None, -(x["max_spread"] or 0), x["asset"])
    )
    used = {v for x in assets for v in x["cells"]}
    count = {v: sum(v in x["cells"] for x in assets) for v in used}
    venues = [
        dict(id=v, label=labels.get(v, v), kind=kinds.get(v, "DEX"), assets=count[v])
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
                        interval=m.get("interval") or 3600,
                        fee=fee,
                        received=m.get("received"),
                    )
                )
        rows, _ = e.wide.snapshot()
        sums = e.history.db.funding_sums(now() - WEEK)
        kinds = {v: c["kind"] for v, c in CATALOG.items()}
        return build(native, rows, e.wide.universe_snapshot(), sums, kinds, LABELS)

    def get(self):
        with self.lock:
            if self.value is None or now() - self.value["ts"] >= CACHE_SECONDS:
                self.value = self.collect()
            return self.value
