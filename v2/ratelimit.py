"""One request budget per venue for the whole process, plus a shared answer cache.

PAPER / public reads. Every caller of a venue (engine adapter, observer, health check,
smoke) takes from the same budget, so the venue sees one client, not several.

Variational (docs.variational.io/technical-documentation/api, read 2026-11-12):
"Per IP: 10 requests per 10 seconds", "Global: 1000 requests per minute"; over the
limit HTTP 429 with JSON `retry_after_seconds`. Live 2026-11-12: a single request got
429 "Service is busy; try again in 1 seconds" (the global limit is shared with every
other client), so the budget alone cannot prevent 429: the answer is cached and shared,
and a 429 pauses the whole venue for the time the venue asks.
"""
import threading
import time

# Requests per window the process may make, kept below each venue's documented limit.
BUDGETS = {
    "variational": (8, 10.0),     # doc: 10 / 10 s per IP
    "lighter_rh": (50, 60.0),     # doc: Standard 60 / min (weighted endpoints cost more)
    "risex": (400, 10.0),         # doc: REST 500 / 10 s
    "arcus": (60, 60.0),          # doc: 1,500 weight / min; listing calls weigh 20
    "txflow": (40, 1.0),          # doc: /info 50 / s
    "tradexyz": (50, 60.0),       # doc: 1,200 weight / min; info calls weigh 2-20
    # observation_v3 only: Phoenix limit UNKNOWN -> a deliberately small budget.
    "phoenix_perps": (10, 60.0),
}
# Venues that read the same host share one budget (one client as the venue sees it):
# Hyperliquid native and the HIP-3 dex "io" go through api.hyperliquid.xyz like xyz.
SHARED = {"hyperliquid_native": "tradexyz", "entropy_io": "tradexyz"}


class Budget:
    """Sliding-window request budget with a venue-wide pause after a 429."""

    def __init__(self, limit, window, clock=time.monotonic, sleep=time.sleep):
        self.limit, self.window = limit, window
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.sent = []
        self.paused_until = 0.0
        self.stats = dict(acquired=0, waited=0.0, rate_limited=0)

    def acquire(self):
        """Block until one request may be sent; returns the seconds waited."""
        waited = 0.0
        while True:
            with self.lock:
                now = self.clock()
                self.sent = [t for t in self.sent if now - t < self.window]
                wait = max(self.paused_until - now, 0.0)
                if not wait and len(self.sent) >= self.limit:
                    wait = self.window - (now - self.sent[0])
                if wait <= 0:
                    self.sent.append(now)
                    self.stats["acquired"] += 1
                    self.stats["waited"] += waited
                    return waited
            self.sleep(wait)
            waited += wait

    def rate_limited(self, retry_after=None):
        """A 429: pause every caller of this venue for what the venue asked (at least
        1 s, at most 120 s; without a value, one whole window)."""
        try:
            pause = float(retry_after) if retry_after is not None else self.window
        except (TypeError, ValueError):
            pause = self.window
        pause = min(max(pause, 1.0), 120.0)
        with self.lock:
            self.paused_until = max(self.paused_until, self.clock() + pause)
            self.stats["rate_limited"] += 1
        return pause


_BUDGETS = {}
_LOCK = threading.Lock()


def budget(exchange_id):
    exchange_id = SHARED.get(exchange_id, exchange_id)
    with _LOCK:
        if exchange_id not in _BUDGETS:
            if exchange_id not in BUDGETS:
                raise KeyError("no request budget for %s" % exchange_id)
            _BUDGETS[exchange_id] = Budget(*BUDGETS[exchange_id])
        return _BUDGETS[exchange_id]


class SharedCache:
    """Answers kept for `ttl` seconds and handed to every caller; one fetch at a time
    per key (callers arriving meanwhile wait for it instead of sending their own)."""

    def __init__(self, ttl, clock=time.monotonic, max_items=256):
        self.ttl, self.clock, self.max_items = ttl, clock, max_items
        self.lock = threading.Lock()
        self.items = {}
        self.key_locks = {}

    def get(self, key, fetch):
        with self.lock:
            hit = self.items.get(key)
            if hit and self.clock() - hit[0] <= self.ttl:
                return hit[1], True
            klock = self.key_locks.setdefault(key, threading.Lock())
        with klock:
            with self.lock:
                hit = self.items.get(key)
                if hit and self.clock() - hit[0] <= self.ttl:
                    return hit[1], True
            value = fetch()
            with self.lock:
                self.items[key] = (self.clock(), value)
                if len(self.items) > self.max_items:       # bounded: drop the oldest
                    for k in sorted(self.items, key=lambda k: self.items[k][0])[
                            :len(self.items) - self.max_items]:
                        self.items.pop(k, None)
                        self.key_locks.pop(k, None)
            return value, False
