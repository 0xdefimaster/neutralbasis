"""Bounded containers and error accounting for market-data metrics (Phase 15 P4).

Two eviction policies are kept apart on purpose:
  STATE    order-book / snapshot state of a subscribed market. Never evicted by a size
           policy: dropping it would silently lose a sequence chain. Removed only by an
           explicit unsubscribe (v2.ws_readonly.Books.drop_market).
  METRICS  counters, gaps, latencies, telemetry buckets. Bounded by key count with LRU
           eviction; keys whose market still has live STATE (an active subscription) are
           protected. Every eviction is counted, never silent.

CallbackErrors: a callback (telemetry hook) may fail without stopping the data path, but
every failure is counted with time, source and a safe one-line summary (no secrets, no
long ids), so the Doctor can say the telemetry is incomplete.
"""
import collections
import re
import time


class LRUDict(collections.OrderedDict):
    """An OrderedDict bounded to `max_keys` (None = unbounded). `touch(key, factory)`
    returns the value (created if absent) and marks it most recently used. Eviction
    takes the least recently used key that `protect(key)` does not protect; the key
    being touched is never evicted. If every other key is protected the dict may exceed
    the cap (counted in `over_cap`), because protected keys belong to live state that
    is itself bounded by the subscriptions."""

    def __init__(self, max_keys=None, protect=None, on_evict=None):
        super().__init__()
        self.max_keys, self.protect, self.on_evict = max_keys, protect, on_evict
        self.evictions = 0
        self.over_cap = 0

    def touch(self, key, factory):
        if key in self:
            self.move_to_end(key)
            return self[key]
        value = factory()
        self[key] = value
        self._evict(keep=key)
        return value

    def _evict(self, keep=None):
        if self.max_keys is None:
            return
        while len(self) > self.max_keys:
            victim, seen = None, 0
            # a protected (or the kept) key found at the old end is moved to the new
            # end, so later evictions do not scan it again (amortized O(1))
            while seen < len(self):
                k = next(iter(self))
                if k != keep and not (self.protect and self.protect(k)):
                    victim = k
                    break
                self.move_to_end(k)
                seen += 1
            if victim is None:
                self.over_cap += 1
                return
            v = self.pop(victim)
            self.evictions += 1
            if self.on_evict:
                self.on_evict(victim, v)

    def stats(self):
        return dict(keys=len(self), max_keys=self.max_keys, evictions=self.evictions,
                    over_cap=self.over_cap)


_SECRETISH = re.compile(r"(?i)(key|secret|token|signature|sig|auth|password|bearer)"
                        r"(\s*[=:]\s*|\s+)\S+")
_LONG_ID = re.compile(r"(0x)?[0-9a-fA-F]{24,}|\d{16,}")
_QUERY = re.compile(r"(https?://[^\s?#]+)\?\S*")


def safe_summary(ex, limit=160):
    """'TypeName: message' with secret-like values, long ids and URL queries masked."""
    msg = " ".join(str(ex).split())
    msg = _QUERY.sub(r"\1?<query>", msg)
    msg = _SECRETISH.sub(lambda m: m.group(1) + "=<masked>", msg)
    msg = _LONG_ID.sub("<id>", msg)
    return ("%s: %s" % (type(ex).__name__, msg))[:limit]


class CallbackErrors:
    """Failures of a non-critical callback. `summary(now)` gives the Doctor view."""

    def __init__(self, window_s=300.0, max_sources=200, keep=1000):
        self.window_s = window_s
        self.total = 0
        self.first_at = self.last_at = None
        self.last_error = None
        self.recent = collections.deque(maxlen=keep)       # times, for the frequency
        self.by_source = LRUDict(max_sources)

    def add(self, ex, now=None, source=None):
        now = time.time() if now is None else now
        self.total += 1
        self.first_at = self.first_at if self.first_at is not None else now
        self.last_at = now
        self.last_error = safe_summary(ex)
        self.recent.append(now)
        key = tuple(str(x) for x in (source or ("?",)))
        self.by_source[key] = self.by_source.touch(key, int) + 1

    def in_window(self, now):
        return sum(1 for t in self.recent if now - t <= self.window_s)

    def summary(self, now=None):
        now = time.time() if now is None else now
        n = self.in_window(now)
        return dict(status="OK" if n == 0 else "DEGRADED", total=self.total,
                    in_window=n, window_s=self.window_s,
                    per_minute=n / (self.window_s / 60.0), first_at=self.first_at,
                    last_at=self.last_at, last_error=self.last_error,
                    top_sources=[dict(source=list(k), count=c) for k, c in sorted(
                        self.by_source.items(), key=lambda kv: -kv[1])[:5]],
                    sources_evicted=self.by_source.evictions,
                    telemetry_complete=self.total == 0)
