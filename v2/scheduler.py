"""Channel scheduler (Phase 15 P3). PAPER / public reads only.

Decides WHEN each (venue, channel, key) is read; the reads themselves stay the existing
adapter / collector calls (`run_due(fetchers)`). No event bus, no second recorder.

Channels and their policies:
  orderbook        WS first where the venue has one (txflow, tradeXYZ / HL native, Lighter
                   RH); REST fallback while the WS is stale or down, back to WS after
                   `recover_after` fresh checks (hysteresis); while the WS is fresh a REST
                   read is only a slow VERIFICATION (`verify_s`), never a duplicate feed
  public_trades    DISABLED: no verified public-trades reader exists (capability matrix)
  funding_current  REST from the venue's market answers; adaptive (below)
  funding_history  incremental backfill after each settlement, from a DURABLE cursor
                   (the last settled time per market; never moves back; survives a restart)
  rfq_quote        Variational indicative quotes (shared stats answer); adaptive
  metadata         slow (1 h); fees_specs daily; both LOW priority

Adaptive REST polling keeps the REQUEST rate apart from the PUBLICATION rate: the
interval is half the observed publication cadence (time between real data changes),
clamped to [rest_min_s, min(rest_max_s, stale_limit / 2)]. Polling faster than the venue
publishes only re-reads the same answer; polling slower than half the stale limit
would let data go stale. Until a cadence is observed, rest_min_s is used.

Priorities map to v2.ratecoord HIGH / LOW. Every request and every real data change is
recorded; `snapshot()` feeds the Doctor and the panel (a panel refresh never calls a
venue).
"""
import collections
import hashlib
import json
import statistics
import time
from pathlib import Path

from v2 import funding_semantics as fs
from v2 import marketdata

HIGH, LOW = "HIGH", "LOW"
WS, REST, REST_FALLBACK, DISABLED = "WS", "REST", "REST_FALLBACK", "DISABLED"
CHANNELS = ("orderbook", "public_trades", "funding_current", "funding_history", "rfq_quote",
            "metadata", "fees_specs")
POLICY = dict(
    orderbook=dict(priority=HIGH, rest_min_s=2.0, rest_max_s=10.0, verify_s=300.0),
    public_trades=dict(priority=LOW),
    funding_current=dict(priority=HIGH, rest_min_s=30.0, rest_max_s=300.0, stale_s=3600.0),
    funding_history=dict(priority=LOW, after_settlement_s=300.0, period_s=3600.0),
    rfq_quote=dict(priority=HIGH, rest_min_s=15.0, rest_max_s=60.0),
    metadata=dict(priority=LOW, interval_s=3600.0),
    fees_specs=dict(priority=LOW, interval_s=86400.0),
)
WS_OK = ("LIVE_OBSERVED", "VERIFIED", "MOCK_TESTED")
RECOVER_AFTER = 3


def plan(matrix):
    """{(venue, channel): dict(mode, why, evidence)} from the capability matrix (no
    capability is assumed: a level that is not proven leaves the channel DISABLED or
    REST-only, with the reason)."""
    out = {}
    for xid, row in matrix.items():
        caps = row.get("capabilities") or {}

        def lvl(c):
            return (caps.get(c) or {}).get("level")

        rest = lvl("rest") not in (None, "UNSUPPORTED", "UNVERIFIED")
        ws = lvl("websocket") in WS_OK
        rfq = xid == "variational"
        if rfq:
            out[(xid, "orderbook")] = dict(mode=DISABLED, why="RFQ venue: no order book",
                                           evidence=lvl("orderbook"))
            out[(xid, "rfq_quote")] = dict(mode=REST if rest else DISABLED,
                                           why="indicative quotes (shared stats answer)",
                                           evidence=lvl("rest"))
        elif lvl("orderbook") in (None, "UNSUPPORTED", "UNVERIFIED") and not ws:
            out[(xid, "orderbook")] = dict(mode=DISABLED, why="order book not verified",
                                           evidence=lvl("orderbook"))
        else:
            out[(xid, "orderbook")] = dict(mode=WS if ws else REST, why=(
                "WS first, REST fallback / verification" if ws else "REST only (no WS)"),
                evidence=lvl("websocket") if ws else lvl("orderbook"))
        out[(xid, "public_trades")] = dict(mode=DISABLED, why="no verified public-trades "
                                           "reader (capability %s)" % lvl("public_trades"),
                                           evidence=lvl("public_trades"))
        fc = lvl("funding_current")
        out[(xid, "funding_current")] = dict(
            mode=REST if rest and fc not in (None, "UNSUPPORTED", "UNVERIFIED") else DISABLED,
            why="from the market answers" if rest else "no verified REST", evidence=fc)
        hist = (fs.venue(xid) or {}).get("settled")
        out[(xid, "funding_history")] = dict(
            mode=REST if hist and rest else DISABLED,
            why="incremental backfill (durable cursor)" if hist else
            "no public settled history", evidence=lvl("funding_history"))
        for ch in ("metadata", "fees_specs"):
            out[(xid, ch)] = dict(mode=REST if rest else DISABLED,
                                  why="slow refresh" if rest else "no verified REST",
                                  evidence=lvl("rest"))
    return out


class CursorStore:
    """Durable per-key cursor (last settled time). Never moves back; atomic writes on
    C: (D:/E: refused)."""

    def __init__(self, path):
        from v2 import storage
        self.path = Path(path)
        storage.check_dir(self.path.parent)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.data = {}
        except Exception:
            self.data, self.corrupt = {}, True        # restart from scratch, never guess

    def get(self, key):
        return self.data.get(key)

    def advance(self, key, ts):
        if ts is None:
            return False
        cur = self.data.get(key)
        if cur is not None and ts <= cur:
            return False
        from v2 import ratecoord
        self.data[key] = ts
        ratecoord.atomic_write(self.path, self.data)
        return True


def _digest(answer):
    try:
        raw = json.dumps(answer, sort_keys=True, default=str).encode()
    except Exception:
        raw = repr(answer).encode()
    return hashlib.sha1(raw).hexdigest()


class Channel:
    def __init__(self, venue, channel, key, mode, why, evidence, stale_limit):
        self.venue, self.channel, self.key = venue, channel, key
        self.mode, self.why, self.evidence = mode, why, evidence
        self.preferred = mode                 # WS channels fall back and recover
        self.policy = POLICY[channel]
        self.stale_limit = stale_limit
        self.last_request = self.last_change = self.last_hash = None
        self.last_verify = None
        self.requests = collections.deque(maxlen=100)       # request times
        self.changes = collections.deque(maxlen=100)        # data-change times
        self.results = collections.deque(maxlen=50)         # True / False
        self.fresh_streak = 0
        self.mode_changes = 0

    # -- cadence -------------------------------------------------------------------------
    @staticmethod
    def _median_gap(ts):
        g = [b - a for a, b in zip(ts, list(ts)[1:])]
        return statistics.median(g) if g else None

    def publication_cadence(self):
        return self._median_gap(self.changes)

    def request_cadence(self):
        return self._median_gap(self.requests)

    def rest_interval(self):
        p = self.policy
        if self.channel in ("metadata", "fees_specs"):
            return p["interval_s"]
        if self.channel == "funding_history":
            return p["period_s"]              # once per settlement (+ after_settlement_s)
        hi = min(p.get("rest_max_s", 3600.0),
                 (self.stale_limit / 2.0) if self.stale_limit else p.get("rest_max_s", 3600.0))
        cad = self.publication_cadence()
        if cad is None:
            return p["rest_min_s"]
        return max(p["rest_min_s"], min(hi, cad / 2.0))

    # -- decisions -----------------------------------------------------------------------
    def update_ws(self, ws_age):
        """WS health -> mode (fallback at once, recovery after RECOVER_AFTER checks)."""
        if self.preferred != WS:
            return
        fresh = ws_age is not None and ws_age <= self.stale_limit
        if not fresh:
            self.fresh_streak = 0
            if self.mode != REST_FALLBACK:
                self.mode, self.mode_changes = REST_FALLBACK, self.mode_changes + 1
        else:
            self.fresh_streak += 1
            if self.mode == REST_FALLBACK and self.fresh_streak >= RECOVER_AFTER:
                self.mode, self.mode_changes = WS, self.mode_changes + 1

    def due(self, now, cursor=None):
        """None or the reason a REST read is due now."""
        if self.mode == DISABLED:
            return None
        if self.mode == WS:
            if self.last_verify is None or now - self.last_verify >= self.policy["verify_s"]:
                return "WS_VERIFICATION"
            return None
        if self.channel == "funding_history":
            p = self.policy
            if cursor is None:
                return "BACKFILL_START" if self.last_request is None else None
            nxt = cursor + p["period_s"] + p["after_settlement_s"]
            if now >= nxt and (self.last_request is None or now - self.last_request >=
                               p["after_settlement_s"]):
                return "AFTER_SETTLEMENT"
            return None
        if self.last_request is None or now - self.last_request >= self.rest_interval():
            return "REST_FALLBACK" if self.mode == REST_FALLBACK else "REST_POLL"
        return None

    def record(self, now, answer=None, ok=True, reason=None):
        self.last_request = now
        if reason == "WS_VERIFICATION":
            self.last_verify = now
        self.requests.append(now)
        self.results.append(bool(ok))
        if ok and answer is not None:
            h = _digest(answer)
            if h != self.last_hash:
                self.last_hash, self.last_change = h, now
                self.changes.append(now)

    def ws_data(self, now):
        """A real data change seen on the WS (no request made)."""
        self.last_change = now
        self.changes.append(now)

    def view(self, now):
        age = None if self.last_change is None else now - self.last_change
        n = len(self.results)
        return dict(
            exchange_id=self.venue, channel=self.channel, key=self.key, mode=self.mode,
            preferred=self.preferred, why=self.why, evidence=self.evidence,
            priority=self.policy.get("priority"),
            policy_interval_s=None if self.mode in (DISABLED, WS) else self.rest_interval(),
            observed_request_interval_s=self.request_cadence(),
            observed_publication_cadence_s=self.publication_cadence(),
            last_request_at=self.last_request, last_data_change_at=self.last_change,
            data_change_age_s=age, stale_limit_s=self.stale_limit,
            status=("DISABLED" if self.mode == DISABLED else "NO_DATA" if age is None else
                    "STALE" if self.stale_limit and age > self.stale_limit else "FRESH"),
            error_rate=None if not n else 1 - sum(self.results) / n, requests=n,
            mode_changes=self.mode_changes)


def stale_limit_for(venue, channel):
    if channel == "orderbook":
        return marketdata.MAX_AGE.get(venue, 15.0)
    if channel == "rfq_quote":
        return marketdata.MAX_AGE.get("variational", 60.0)
    if channel == "funding_current":
        return POLICY["funding_current"]["stale_s"]
    return None


class Scheduler:
    def __init__(self, matrix, keys=None, cursor_path=None, clock=time.time):
        """`keys`: {(venue, channel): [market keys]} (one Channel per key; default one
        venue-wide key "*")."""
        self.clock = clock
        self.channels = {}
        for (xid, ch), p in plan(matrix).items():
            for k in (keys or {}).get((xid, ch), ["*"]):
                self.channels[(xid, ch, k)] = Channel(xid, ch, k, p["mode"], p["why"],
                                                      p["evidence"],
                                                      stale_limit_for(xid, ch))
        self.cursors = CursorStore(cursor_path) if cursor_path else None
        self.errors = collections.Counter()

    def tick(self, now=None, ws_age=None):
        """Due reads: [(venue, channel, key, reason, priority)]. `ws_age(venue, key)` gives
        the age of the venue's WS data (None = no WS data)."""
        now = self.clock() if now is None else now
        out = []
        for (xid, ch, k), c in self.channels.items():
            if c.preferred == WS:
                # no WS feed connected = no WS data: fall back to REST at once
                c.update_ws(ws_age(xid, k) if ws_age is not None else None)
            cur = self.cursors.get("%s|%s" % (xid, k)) if (
                self.cursors and ch == "funding_history") else None
            why = c.due(now, cur)
            if why:
                out.append((xid, ch, k, why, c.policy.get("priority")))
        out.sort(key=lambda x: (x[4] != HIGH, x[1] != "orderbook"))
        return out

    def run_due(self, fetchers, now=None, ws_age=None):
        """Call the existing fetchers for every due read. `fetchers[(venue, channel)]
        (key) -> answer`; a funding_history answer is [points with ts] and advances the
        durable cursor. Errors are counted, never raised."""
        now = self.clock() if now is None else now
        done = []
        for xid, ch, k, why, prio in self.tick(now, ws_age):
            f = fetchers.get((xid, ch))
            if f is None:
                continue
            c = self.channels[(xid, ch, k)]
            try:
                ans = f(k)
                c.record(now, ans, True, why)
                if ch == "funding_history" and self.cursors and ans:
                    self.cursors.advance("%s|%s" % (xid, k),
                                         max(p["ts"] for p in ans if p.get("ts") is not None))
                done.append((xid, ch, k, why, "OK"))
            except Exception as ex:
                c.record(now, None, False, why)
                self.errors["%s/%s" % (xid, ch)] += 1
                done.append((xid, ch, k, why, "ERROR %s" % type(ex).__name__))
        return done

    def snapshot(self, now=None):
        now = self.clock() if now is None else now
        return [c.view(now) for c in self.channels.values()]
