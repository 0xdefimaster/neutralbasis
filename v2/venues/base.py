"""Shared pieces of the V2 venue adapters: the HTTP transport (public reads only, one
budget per venue), number parsing, book normalization and the market record.

A market record (dict) carries, for every venue the same way:
  instrument      v2.identity.Instrument (exchange_id, market_id, symbol, declared
                  class, fine class, name, multiplier, contract type, price)
  legacy_venue    the venue's name in the legacy code and stored history
  mark, index     prices as published (None when not published)
  funding         dict(raw, unit, interval_s, next_ts, kind, sign) or None
  fee_taker       fraction or None (UNKNOWN); fee_source
  active          bool; inactive_reason
Missing values stay None; nothing is defaulted.
"""
import json
import time
import urllib.error
import urllib.request

from v2 import ratelimit

UA = {"User-Agent": "NeutralBasisV2/1.0 (paper research)", "Accept": "application/json"}


class RateLimited(RuntimeError):
    pass


def num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def http_json(exchange_id, method, url, body=None, headers=None, timeout=20, tries=3,
              priority="HIGH"):
    """One public read through the venue's shared budget. A 429 pauses the venue for
    the time it asks (JSON retry_after_seconds / retryAfterMs or Retry-After) and is
    retried at most `tries` times in total. With NB_V2_SHARED_BUDGET=1 the budget is
    shared between processes (v2.ratecoord); otherwise per process (unchanged)."""
    from v2 import ratecoord
    if ratecoord.enabled():
        return _http_json_shared(exchange_id, method, url, body, headers, timeout, tries,
                                 priority)
    b = ratelimit.budget(exchange_id)
    data = json.dumps(body).encode() if body is not None else None
    h = dict(UA, **(headers or {}))
    if data is not None:
        h["Content-Type"] = "application/json"
    last = None
    for _ in range(tries):
        b.acquire()
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read(60_000_000))
        except urllib.error.HTTPError as ex:
            if ex.code != 429:
                raise
            last = ex
            b.rate_limited(retry_after_of(ex))
    raise RateLimited("%s: HTTP 429 after %d tries (%s)" % (exchange_id, tries, last))


def _open(method, url, data, h, timeout):
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(60_000_000))


def _http_json_shared(exchange_id, method, url, body, headers, timeout, tries, priority):
    """http_json through the cross-process coordinator: one budget per venue scope for
    every process; 429 / 5xx / timeouts feed the shared breaker and pause."""
    from v2 import ratecoord
    coord = ratecoord.shared()
    scope = ratecoord.scope_id(ratelimit.SHARED.get(exchange_id, exchange_id))
    data = json.dumps(body).encode() if body is not None else None
    h = dict(UA, **(headers or {}))
    if data is not None:
        h["Content-Type"] = "application/json"
    last = None
    for _ in range(tries):
        lease = coord.acquire(scope, priority=priority, timeout=max(60.0, timeout * 3))
        ok, status, ra = False, None, None
        try:
            out = _open(method, url, data, h, timeout)
            ok = True
            return out
        except urllib.error.HTTPError as ex:
            status = ex.code
            if ex.code != 429:
                raise
            last, ra = ex, num(retry_after_of(ex))
        except (urllib.error.URLError, TimeoutError, OSError):
            status = "timeout"
            raise
        finally:
            coord.release(scope, lease, ok=ok, status=status, retry_after=ra)
    raise RateLimited("%s: HTTP 429 after %d tries (%s)" % (exchange_id, tries, last))


def retry_after_of(ex):
    try:
        body = json.loads(ex.read(4000) or b"{}")
    except Exception:
        body = {}
    if body.get("retry_after_seconds") is not None:
        return body["retry_after_seconds"]
    if body.get("retryAfterMs") is not None:
        return num(body["retryAfterMs"]) / 1000 if num(body["retryAfterMs"]) else None
    return ex.headers.get("Retry-After") if ex.headers else None


def book(bids, asks, ts=None, seq=None, source=None):
    """A normalized book: bids high->low, asks low->high, (price, size) floats; levels
    with a non-positive price or size dropped. `crossed`/`empty` flag a bad book."""
    b = sorted(((num(p), num(q)) for p, q in bids), key=lambda x: -(x[0] or 0))
    a = sorted(((num(p), num(q)) for p, q in asks), key=lambda x: x[0] or 0)
    b = [(p, q) for p, q in b if p and q and p > 0 and q > 0]
    a = [(p, q) for p, q in a if p and q and p > 0 and q > 0]
    return dict(bids=b, asks=a, ts=ts, seq=seq, source=source,
                empty=not b or not a, crossed=bool(b and a and b[0][0] >= a[0][0]))


def funding(raw, unit, interval_s, next_ts=None, kind="observed_published", sign=None,
            source=None):
    """A published rate as the venue states it (unit explicit, never guessed)."""
    if raw is None:
        return None
    return dict(raw=raw, unit=unit, interval_s=interval_s, next_ts=next_ts, kind=kind,
                sign=sign, source=source)


def now():
    return time.time()
