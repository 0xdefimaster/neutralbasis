"""HTTP 429/5xx, Retry-After and circuit breaker for the public readers.
MOCK exchanges and errors only; no network request is made."""

import email.utils, threading, types, unittest
from unittest import mock
import runtime_metrics
import wide_markets
import zero_trade
from runtime_metrics import BackoffActive, HttpGuard, classify_http_error, retry_after_seconds


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class RetryAfterTests(unittest.TestCase):
    def test_seconds_and_http_date(self):
        self.assertEqual(retry_after_seconds("7"), 7.0)
        date = email.utils.formatdate(1030.0, usegmt=True)
        self.assertAlmostEqual(retry_after_seconds(date, at=1000.0), 30.0, places=3)
        self.assertIsNone(retry_after_seconds(None))
        self.assertIsNone(retry_after_seconds("soon"))
        self.assertEqual(retry_after_seconds("-5"), 0.0)


class ClassifyTests(unittest.TestCase):
    def test_kinds(self):
        RateLimitExceeded = type("RateLimitExceeded", (Exception,), {})
        RequestTimeout = type("RequestTimeout", (Exception,), {})
        ExchangeNotAvailable = type("ExchangeNotAvailable", (Exception,), {})
        self.assertEqual(classify_http_error(RateLimitExceeded("x")), "429")
        self.assertEqual(classify_http_error(RequestTimeout("x")), "timeout")
        self.assertEqual(classify_http_error(TimeoutError("timed out")), "timeout")
        self.assertEqual(
            classify_http_error(ExchangeNotAvailable("bybit GET https://a/b 503 Service Unavailable")),
            "5xx",
        )
        self.assertEqual(
            classify_http_error(ExchangeNotAvailable("bybit GET https://a/b 404 Not Found")), "4xx"
        )
        err = types.SimpleNamespace()
        http = type("HTTPError", (Exception,), {})("HTTP Error 429")
        http.code = 429
        self.assertEqual(classify_http_error(http), "429")
        self.assertEqual(classify_http_error(ValueError("bad json")), "error")


class HttpGuardTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.g = HttpGuard(clock=self.clock, jitter=lambda: 1.0)

    def test_retry_after_is_honoured_and_bounded(self):
        self.assertEqual(self.g.failure("Bybit", "429", retry_after=20), 20)
        with self.assertRaises(BackoffActive) as e:
            self.g.check("Bybit")
        self.assertIn("istek sınırı", str(e.exception))
        self.g.check("OKX")  # other venues are not affected
        self.clock.t += 20.1
        self.g.check("Bybit")
        self.assertEqual(self.g.failure("Bybit", "429", retry_after=99999), runtime_metrics.RATE_CAP)
        self.assertEqual(self.g.failure("Bybit", "429", retry_after=0), 1.0)

    def test_429_without_retry_after_backs_off_exponentially_and_resets(self):
        delays = [self.g.failure("X", "429") for _ in range(12)]
        self.assertEqual(delays[:4], [2.0, 4.0, 8.0, 16.0])
        self.assertEqual(max(delays), runtime_metrics.RATE_CAP)
        self.g.success("X")
        self.assertEqual(self.g.failure("X", "429"), 2.0)

    def test_circuit_opens_after_consecutive_server_errors_only(self):
        self.assertEqual(self.g.failure("X", "5xx"), 0)
        self.assertEqual(self.g.failure("X", "timeout"), 0)
        self.g.check("X")
        self.assertEqual(self.g.failure("X", "5xx"), runtime_metrics.CIRCUIT_BASE)
        with self.assertRaises(BackoffActive) as e:
            self.g.check("X")
        self.assertIn("devre açık", str(e.exception))
        self.assertEqual(self.g.failure("X", "5xx"), 2 * runtime_metrics.CIRCUIT_BASE)
        # A success closes it for the next streak.
        self.clock.t += 1000
        self.g.success("X")
        self.assertEqual(self.g.failure("X", "5xx"), 0)

    def test_client_errors_do_not_back_off(self):
        for _ in range(10):
            self.assertEqual(self.g.failure("X", "4xx"), 0)
        self.g.check("X")
        r = self.g.report()["X"]
        self.assertEqual((r["client_errors"], r["blocked_seconds_left"]), (10, 0.0))

    def test_skipped_requests_are_counted_and_thread_safe(self):
        self.g.failure("X", "429", retry_after=60)
        errors = []

        def hit():
            for _ in range(200):
                try:
                    self.g.check("X")
                except BackoffActive:
                    errors.append(1)

        ts = [threading.Thread(target=hit) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(errors), 1600)
        self.assertEqual(self.g.report()["X"]["skipped"], 1600)

    def test_backoff_messages_have_their_own_reason_codes(self):
        self.g.failure("Bybit", "429", retry_after=30)
        for _ in range(3):
            self.g.failure("OKX", "5xx")
        for venue, code in (("Bybit", "API_RATE_LIMIT"), ("OKX", "VENUE_CIRCUIT_OPEN")):
            try:
                self.g.check(venue)
            except BackoffActive as ex:
                self.assertEqual(zero_trade.code_of("defter okunamadı — " + str(ex))[0], code)
        self.assertEqual(
            zero_trade.code_of("Bybit: RateLimitExceeded: bybit 429 Too Many Requests")[0],
            "API_RATE_LIMIT",
        )


class CcxtWrapperTests(unittest.TestCase):
    """The real WideMarkets.client() wrapper around a MOCK ccxt exchange."""

    def make(self, responses):
        RateLimitExceeded = type("RateLimitExceeded", (Exception,), {})
        calls = []

        class FakeExchange:
            def __init__(self, cfg):
                self.options = {}
                self.last_response_headers = {}
                self.last_http_response = None
                self.urls = {}

            def fetch(self, url, method="GET", headers=None, body=None):
                calls.append(url)
                kind = responses.pop(0)
                if kind == "429":
                    self.last_response_headers = {"Retry-After": "30"}
                    raise RateLimitExceeded("bybit GET " + url + " 429 Too Many Requests")
                self.last_response_headers = {}
                self.last_http_response = "{}"
                return {}

            def load_markets(self):
                return {}

        fake = types.SimpleNamespace(fakeex=FakeExchange)
        store = mock.Mock()
        store.audit.return_value = 1
        w = wide_markets.WideMarkets(store, ["BTC"], threading.Event())
        catalog = dict(wide_markets.CATALOG, Fake=dict(wide_markets.CATALOG["Bybit"], adapter="fakeex"))
        with mock.patch.object(wide_markets, "ccxt", fake), mock.patch.dict(
            wide_markets.CATALOG, catalog
        ):
            ex = w.client("Fake")
        return w, ex, calls

    def test_429_with_retry_after_stops_further_calls_to_that_venue(self):
        w, ex, calls = self.make(["ok", "429", "ok"])
        ex.fetch("https://fake/a")
        with self.assertRaises(Exception):
            ex.fetch("https://fake/b")
        with self.assertRaises(BackoffActive):
            ex.fetch("https://fake/c")  # not sent
        self.assertEqual(calls, ["https://fake/a", "https://fake/b"])
        r = w.http_stats.report()["Fake"]
        self.assertEqual((r["ok"], r["rate_limited"], r["skipped"], r["retry_after"]), (1, 1, 1, 30.0))
        self.assertGreater(r["blocked_seconds_left"], 25)


if __name__ == "__main__":
    unittest.main()
