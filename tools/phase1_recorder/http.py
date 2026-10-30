"""Public HTTP reads with retries, rate spacing and an audit record for every request.

Only unauthenticated public reads are made: GET everywhere, plus POST to Hyperliquid's
public `/info` endpoint for an allowlisted set of read-only request types (Hyperliquid
publishes its market data only through POST /info; the main project reads it the same
way through CCXT). No credentials are ever sent; any header or parameter whose name
looks like one is redacted before it is recorded.
"""

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "NeutralbasisPhase1Recorder/1.0 (public read-only)"
SECRET = re.compile(r"key|token|secret|sign|auth|password|cookie|session", re.I)
KEPT_HEADERS = (
    "date",
    "age",
    "cache-control",
    "x-request-id",
    "x-amzn-requestid",
    "cf-ray",
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "ratelimit-limit",
    "ratelimit-remaining",
    "ratelimit-reset",
    "content-type",
)


class HttpError(Exception):
    def __init__(self, message, status=None, retry=False):
        super().__init__(message)
        self.status = status
        self.retry = retry


def redact(d):
    return {
        k: ("<redacted>" if SECRET.search(str(k)) else v) for k, v in (d or {}).items()
    }


class UrllibTransport:
    """The real network. Returns (status, headers, body bytes)."""

    def __call__(self, method, url, body, timeout):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as ex:
            return (
                ex.code,
                {k.lower(): v for k, v in (ex.headers or {}).items()},
                ex.read() or b"",
            )


class Http:
    """One venue's request path: spacing, retries with exponential backoff on 429, 5xx,
    timeouts and connection errors, and an audit row per attempt."""

    def __init__(
        self, venue, store, cfg, transport=None, sleep=time.sleep, clock=time.time
    ):
        self.venue = venue
        self.store = store
        self.cfg = cfg
        self.transport = transport or UrllibTransport()
        self.sleep = sleep
        self.clock = clock
        self.spacing = cfg["min_spacing"].get(venue, 1.0)
        self.last = 0.0
        self.lock = threading.Lock()
        self.stop = None

    def _wait_turn(self):
        with self.lock:
            gap = self.last + self.spacing - self.clock()
            if gap > 0:
                self.sleep(gap)
            self.last = self.clock()

    def request(self, endpoint, url, params=None, body=None, method="GET"):
        """JSON of a successful response and the sha256 of its stored raw body."""
        if method not in ("GET", "POST"):
            raise ValueError("Yalnızca public okuma isteği yapılır")
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        attempts = self.cfg["retries"] + 1
        delay = self.cfg["backoff_seconds"]
        last_error = None
        for attempt in range(attempts):
            if self.stop is not None and self.stop.is_set():
                raise HttpError("Kayıt durduruldu")
            self._wait_turn()
            started = self.clock()
            status, headers, raw, error = None, {}, b"", None
            try:
                status, headers, raw = self.transport(
                    method, url, body, self.cfg["timeout_seconds"]
                )
            except (socket.timeout, TimeoutError) as ex:
                error = "Zaman aşımı: %s" % ex
            except (urllib.error.URLError, OSError, ConnectionError) as ex:
                error = "Bağlantı hatası: %s" % ex
            finished = self.clock()
            retry = error is not None or status == 429 or (status or 0) >= 500
            if status is not None and not retry and status >= 400:
                error = "HTTP %d" % status
            elif status == 429:
                error = "HTTP 429: istek sınırı"
            elif status is not None and status >= 500:
                error = "HTTP %d: sunucu hatası" % status
            parsed, sha = None, None
            if error is None:
                try:
                    parsed = json.loads(raw)
                except ValueError as ex:
                    error = "Bozuk JSON: %s" % str(ex)[:120]
            if raw:
                sha = self.store.put_raw(self.venue, raw, finished)
            self.store.audit(
                dict(
                    venue=self.venue,
                    endpoint=endpoint,
                    method=method,
                    url=url.split("?")[0],
                    params=json.dumps(
                        redact(
                            dict(
                                urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)
                            )
                        )
                    ),
                    body=json.dumps(redact(body)) if body is not None else None,
                    request_at=started,
                    response_at=finished,
                    status=status,
                    duration_ms=round((finished - started) * 1000, 1),
                    sha256=sha,
                    size=len(raw),
                    headers=json.dumps(
                        {k: headers[k] for k in KEPT_HEADERS if k in headers}
                    ),
                    error=error,
                    attempt=attempt + 1,
                    rate_limited=int(status == 429),
                )
            )
            if error is None:
                return parsed, sha
            last_error = HttpError(error, status, retry)
            if not retry or attempt == attempts - 1:
                break
            wait = delay * (2**attempt)
            after = headers.get("retry-after")
            if status == 429 and after:
                try:
                    wait = max(wait, float(after))
                except ValueError:
                    pass
            self.sleep(min(wait, 120))
        raise last_error
