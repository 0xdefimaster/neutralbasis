"""Runtime measurements for the paper desk (PAPER ONLY; observes, never decides).

* TimedLock: the shared SQLite store lock, with wait and hold times per acquisition and
  the slowest holders by call site. Measures the contention seen in the PHASE 0 audit
  before any storage change is made.
* StageClock: how long each cycle stage took, from the engine's existing `stage` marks.
* Stall watchdog: a cycle running longer than STALL_SECONDS dumps every thread's Python
  stack to the crash log (faulthandler), so a hang leaves evidence instead of a gap.
"""

import collections, faulthandler, os, sys, threading, time

SAMPLES = 4000
SLOW_SECONDS = 1.0
STALL_SECONDS = 300
# The open file faulthandler writes stall dumps to; set by risk_main.enable_crash_log.
STALL_LOG = None


def summary(values):
    """n / p50 / p95 / max of a sample, in seconds; None when empty (never 0)."""
    xs = sorted(values)
    if not xs:
        return dict(n=0, p50=None, p95=None, max=None)
    pick = lambda q: xs[min(len(xs) - 1, int(q * len(xs)))]
    return dict(n=len(xs), p50=pick(0.5), p95=pick(0.95), max=xs[-1])


def call_site(depth):
    try:
        f = sys._getframe(depth)
    except ValueError:
        return "?"
    return "%s (%s:%d)" % (f.f_code.co_name, os.path.basename(f.f_code.co_filename), f.f_lineno)


class TimedLock:
    """Drop-in for threading.RLock used as a context manager or via acquire/release.
    Only the outermost acquisition of a thread is measured (re-entry is free)."""

    def __init__(self, samples=SAMPLES, slow=SLOW_SECONDS, clock=time.perf_counter):
        self._lock = threading.RLock()
        self._local = threading.local()
        self._stats = threading.Lock()
        self.clock = clock
        self.slow = slow
        self.waits = collections.deque(maxlen=samples)
        self.holds = collections.deque(maxlen=samples)
        self.slowest = collections.deque(maxlen=20)
        self.by_site = collections.defaultdict(lambda: [0, 0.0, 0.0])  # n, total, max
        self.count = 0
        self.started = time.time()

    def acquire(self, blocking=True, timeout=-1, _depth=2):
        depth = getattr(self._local, "depth", 0)
        if depth:
            got = self._lock.acquire(blocking, timeout)
            if got:
                self._local.depth = depth + 1
            return got
        asked = self.clock()
        got = self._lock.acquire(blocking, timeout)
        if not got:
            return False
        held = self.clock()
        self._local.depth = 1
        self._local.since = held
        self._local.site = call_site(_depth)
        with self._stats:
            self.waits.append(held - asked)
        return True

    def release(self):
        depth = self._local.depth - 1
        self._local.depth = depth
        if depth:
            return self._lock.release()
        hold = self.clock() - self._local.since
        site = self._local.site
        self._lock.release()
        with self._stats:
            self.count += 1
            self.holds.append(hold)
            s = self.by_site[site]
            s[0] += 1
            s[1] += hold
            s[2] = max(s[2], hold)
            if hold >= self.slow:
                self.slowest.append(dict(
                    site=site, thread=threading.current_thread().name,
                    hold_seconds=hold, at=time.time()))

    def __enter__(self):
        self.acquire(_depth=3)
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def report(self, top=10):
        with self._stats:
            waits, holds = list(self.waits), list(self.holds)
            sites = sorted(self.by_site.items(), key=lambda kv: -kv[1][1])[:top]
            slowest = list(self.slowest)
            count = self.count
        return dict(
            acquisitions=count,
            since=self.started,
            wait_seconds=summary(waits),
            hold_seconds=summary(holds),
            busiest_sites=[dict(site=k, n=n, total_seconds=t, max_seconds=m)
                           for k, (n, t, m) in sites],
            slow_holds=slowest,
            note="Son %d edinim örneği; yalnız en dış edinim ölçülür." % self.waits.maxlen,
        )


class StageClock:
    """Durations of the stages of each cycle, from `stage` assignments."""

    def __init__(self, keep=200, clock=time.monotonic):
        self.clock = clock
        self.cycles = collections.deque(maxlen=keep)
        self.lock = threading.Lock()
        self.current = None

    def begin(self):
        with self.lock:
            self.current = dict(started=time.time(), stages=[], _mark=None)

    def mark(self, stage):
        with self.lock:
            c = self.current
            if c is None:
                return
            t = self.clock()
            if c["_mark"] is not None:
                name, since = c["_mark"]
                c["stages"].append((name, t - since))
            c["_mark"] = (stage, t)

    def end(self):
        with self.lock:
            c, self.current = self.current, None
            if c is None:
                return None
            if c["_mark"] is not None:
                name, since = c["_mark"]
                c["stages"].append((name, self.clock() - since))
            c.pop("_mark")
            c["seconds"] = sum(d for _, d in c["stages"])
            self.cycles.append(c)
            return c

    def report(self):
        with self.lock:
            cycles = list(self.cycles)
        per = collections.defaultdict(list)
        for c in cycles:
            for name, d in c["stages"]:
                per[name].append(d)
        return dict(
            cycles=len(cycles),
            cycle_seconds=summary([c["seconds"] for c in cycles]),
            stages={k: summary(v) for k, v in per.items()},
            last=cycles[-1] if cycles else None,
        )


class BackoffActive(Exception):
    """A request was not sent: the venue is in a rate-limit backoff or its circuit is
    open. Callers treat it like any failed public read (no data, nothing invented)."""


# Rate limit (429/418): Retry-After when given, else exponential; both bounded.
RATE_BASE, RATE_CAP = 2.0, 300.0
# Server errors / timeouts: the circuit opens after this many in a row.
CIRCUIT_AFTER, CIRCUIT_BASE, CIRCUIT_CAP = 3, 5.0, 120.0


def retry_after_seconds(value, at=None):
    """Retry-After as seconds (delta-seconds or HTTP-date); None if absent/invalid."""
    if value is None:
        return None
    value = str(value).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        import email.utils

        when = email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return max(0.0, when - (at if at is not None else time.time()))


def classify_http_error(exc):
    """'429' (rate limited), '5xx', 'timeout', '4xx' or 'error' for a failed request."""
    import re

    name = type(exc).__name__
    text = str(exc)
    if name in ("RateLimitExceeded", "DDoSProtection") or re.search(r"\b(429|418)\b", text) and (
        "HTTP" in text.upper() or "Too Many" in text
    ):
        return "429"
    code = getattr(exc, "code", None) or getattr(exc, "status", None)
    if isinstance(code, int):
        if code in (429, 418):
            return "429"
        if 500 <= code <= 599:
            return "5xx"
        if 400 <= code <= 499:
            return "4xx"
    if "Timeout" in name or isinstance(exc, TimeoutError) or "timed out" in text.lower():
        return "timeout"
    m = re.search(r"\s([45]\d\d)(\s|$)", text)
    if m:
        return "5xx" if m.group(1).startswith("5") else "4xx"
    return "error"


class HttpGuard:
    """Per-venue HTTP outcome counters, rate-limit backoff and a circuit breaker for the
    thread-based public readers. Thread-safe; it never sleeps a caller (a blocked call
    fails fast with BackoffActive), so no worker is held and no thread is added."""

    def __init__(self, clock=time.time, jitter=None):
        import random

        self.clock = clock
        self.jitter = jitter or random.random
        self.lock = threading.Lock()
        self.venues = {}

    def _state(self, venue):
        s = self.venues.get(venue)
        if s is None:
            s = self.venues[venue] = dict(
                ok=0, rate_limited=0, server_errors=0, timeouts=0, client_errors=0,
                errors=0, skipped=0, consecutive=0, rate_streak=0, blocked_until=0.0,
                blocked_reason=None, retry_after=None, last_error=None, last_error_at=None,
            )
        return s

    def check(self, venue):
        with self.lock:
            s = self._state(venue)
            left = s["blocked_until"] - self.clock()
            if left > 0:
                s["skipped"] += 1
                reason = s["blocked_reason"]
        if left > 0:
            if reason == "429":
                raise BackoffActive(
                    "%s: istek sınırı (HTTP 429) — %.0f sn geri çekilme" % (venue, left)
                )
            raise BackoffActive(
                "%s: borsa geçici olarak yanıt vermiyor — devre açık, %.0f sn" % (venue, left)
            )

    def success(self, venue):
        with self.lock:
            s = self._state(venue)
            s["ok"] += 1
            s["consecutive"] = 0
            s["rate_streak"] = 0

    def failure(self, venue, kind, retry_after=None):
        """Record a failed request; returns the backoff applied in seconds (0 if none)."""
        with self.lock:
            s = self._state(venue)
            now_ = self.clock()
            s["last_error"], s["last_error_at"] = kind, now_
            delay = 0.0
            if kind == "429":
                s["rate_limited"] += 1
                s["rate_streak"] += 1
                if retry_after is not None:
                    delay = min(RATE_CAP, max(1.0, retry_after))
                    s["retry_after"] = retry_after
                else:
                    raw = min(RATE_CAP, RATE_BASE * 2 ** (s["rate_streak"] - 1))
                    delay = raw * (0.5 + 0.5 * self.jitter())
            elif kind in ("5xx", "timeout"):
                s["server_errors" if kind == "5xx" else "timeouts"] += 1
                s["consecutive"] += 1
                if s["consecutive"] >= CIRCUIT_AFTER:
                    n = s["consecutive"] - CIRCUIT_AFTER
                    raw = min(CIRCUIT_CAP, CIRCUIT_BASE * 2 ** n)
                    delay = raw * (0.5 + 0.5 * self.jitter())
            elif kind == "4xx":
                s["client_errors"] += 1
            else:
                s["errors"] += 1
            if delay > 0:
                s["blocked_until"] = max(s["blocked_until"], now_ + delay)
                s["blocked_reason"] = "429" if kind == "429" else "circuit"
            return delay

    def report(self):
        with self.lock:
            now_ = self.clock()
            return {
                v: dict(s, blocked_seconds_left=max(0.0, s["blocked_until"] - now_))
                for v, s in sorted(self.venues.items())
            }


def arm_stall_dump(seconds=None):
    """Dump all thread stacks if the cycle is still running after `seconds` (and every
    `seconds` after that). Cancelled by disarm_stall_dump at the end of the cycle."""
    f = STALL_LOG or sys.stderr
    try:
        faulthandler.dump_traceback_later(seconds or STALL_SECONDS, repeat=True, file=f)
        return True
    except (ValueError, OSError, AttributeError):
        # No usable file (e.g. a closed stderr in a hosted run): measuring stays optional.
        return False


def disarm_stall_dump():
    faulthandler.cancel_dump_traceback_later()
