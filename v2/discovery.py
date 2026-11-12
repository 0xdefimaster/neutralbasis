"""Two-stage discovery: a light metadata catalog for every market of the eight venues,
heavy data only where an instrument trades on two or more distinct exchange_ids.

Stage A (LightweightDiscovery) keeps metadata only: ids, activity, tick/lot,
multiplier, session. Stage B (intersection + SubscriptionManager) opens order book,
mark, trades, funding and history subscriptions for MULTI_VENUE instruments, keeps
any subscription a PAPER position / pending fill / recovery / replay still needs, and
closes the rest idempotently.

  SINGLE_VENUE_ONLY      one venue lists it: catalog only, no candidate, no stream
  IDENTITY_UNVERIFIED    no positive identity evidence: catalog only
  MULTI_VENUE            2+ distinct exchange_ids, identity verified: streams + pairs
"""
import itertools
import time

from v2 import identity
from v2.policy import EXCHANGE_IDS

SINGLE = "SINGLE_VENUE_ONLY"
MULTI = "MULTI_VENUE"
UNVERIFIED = identity.UNVERIFIED
HEAVY_CHANNELS = ("book", "mark", "trades", "funding", "history")


class LightweightDiscovery:
    """Metadata catalog with a per-venue TTL plus jitter; a venue is refetched only when
    its entry is older than its TTL. `fetch(exchange_id)` returns Instruments."""

    def __init__(self, fetch, ttl=900.0, jitter=0.1, clock=time.time, rand=None):
        self.fetch, self.ttl, self.jitter, self.clock = fetch, ttl, jitter, clock
        self.rand = rand or (lambda: 0.5)
        self.catalog, self.fetched, self.errors = {}, {}, {}

    def due(self, xid):
        last = self.fetched.get(xid)
        if last is None:
            return True
        return self.clock() - last >= self.ttl * (1 + self.jitter * (2 * self.rand() - 1))

    def refresh(self, venues=EXCHANGE_IDS):
        changed = []
        for xid in venues:
            if xid not in EXCHANGE_IDS:
                raise ValueError("out of V2 scope: %s" % xid)
            if not self.due(xid):
                continue
            try:
                rows = list(self.fetch(xid))
            except Exception as ex:  # a failed venue keeps its last catalog, marked
                self.errors[xid] = str(ex)[:200]
                self.fetched[xid] = self.clock()
                continue
            self.errors.pop(xid, None)
            old = {i.market_id for i in self.catalog.get(xid, [])}
            self.catalog[xid] = rows
            self.fetched[xid] = self.clock()
            if old != {i.market_id for i in rows}:
                changed.append(xid)
        return changed


def intersect(catalog, registry=None):
    """{underlying_id or 'unverified:<xid>:<market>': dict(status, venues, members,
    reason)} through the group identity check (v2.identity.verify). A market outside a
    verified group keeps the first reason it was left out (identity.REASONS)."""
    every = [inst for _, rows in sorted(catalog.items()) for inst in rows]
    out = {uid: dict(g, reason=None) for uid, g in identity.verify(every, registry).items()}
    for inst in every:
        if inst.status != identity.VERIFIED:
            out["unverified:%s:%s" % (inst.exchange_id, inst.market_id)] = dict(
                status=SINGLE if inst.reason == identity.SINGLE_VENUE else UNVERIFIED,
                venues=[inst.exchange_id], members=[inst], reason=inst.reason)
    return out


def reasons(catalog):
    """{exchange_id: Counter(reason or 'MULTI_VENUE')} after intersect()."""
    import collections

    out = {}
    for xid, rows in catalog.items():
        c = collections.Counter()
        for i in rows:
            if i.status == identity.VERIFIED and i.reason is None:
                c[MULTI] += 1
            else:
                c[i.reason or UNVERIFIED] += 1
        out[xid] = c
    return out


def pairs(venues):
    """Unordered venue pairs and both trade directions of each: n venues give
    n(n-1)/2 pairs and twice as many directions (8 venues: 28 pairs, 56 directions)."""
    unordered = list(itertools.combinations(sorted(set(venues)), 2))
    directed = [(a, b) for a, b in unordered] + [(b, a) for a, b in unordered]
    return unordered, directed


def summary(groups):
    counts = dict(MULTI_VENUE=0, SINGLE_VENUE_ONLY=0, IDENTITY_UNVERIFIED=0)
    venue_pairs = set()
    for g in groups.values():
        counts[g["status"]] += 1
        if g["status"] == MULTI:
            venue_pairs.update(pairs(g["venues"])[0])
    return dict(counts, venue_pairs=len(venue_pairs), theoretical_venue_pairs=28)


class SubscriptionManager:
    """Reference-counted heavy subscriptions. desired = MULTI_VENUE members x channels,
    plus holds (open position, pending fill, recovery, replay) that keep a market's
    streams until released. apply() returns (to_open, to_close); repeating it with the
    same inputs returns nothing (idempotent)."""

    def __init__(self, channels=HEAVY_CHANNELS):
        self.channels = channels
        self.active = set()
        self.holds = {}  # (xid, market_id) -> set(reasons)

    def hold(self, xid, market_id, reason):
        self.holds.setdefault((xid, str(market_id)), set()).add(reason)

    def release(self, xid, market_id, reason):
        key = (xid, str(market_id))
        self.holds.get(key, set()).discard(reason)
        if not self.holds.get(key):
            self.holds.pop(key, None)

    def desired(self, groups):
        want = set()
        for g in groups.values():
            if g["status"] == MULTI:
                for m in g["members"]:
                    want.update((m.exchange_id, str(m.market_id), c) for c in self.channels)
        for (xid, mid) in self.holds:
            want.update((xid, mid, c) for c in self.channels)
        return want

    def apply(self, groups):
        want = self.desired(groups)
        to_open = sorted(want - self.active)
        to_close = sorted(self.active - want)
        self.active = want
        return to_open, to_close
