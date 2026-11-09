"""PAPER pilot network guard: public reads pass, trading/account calls and unknown hosts
are refused. The integration test runs the real audit hook in a child process against a
local MOCK HTTP server; no venue is contacted."""
import os
import subprocess
import sys
import textwrap
import unittest

from pilot import network_guard as g


class DecisionTests(unittest.TestCase):
    def test_public_reads_pass(self):
        for host, method, path in (
            ("api.hyperliquid.xyz", "POST", "/info"),
            ("rpc.txflow.com", "POST", "/info"),
            ("mainnet.zklighter.elliot.ai", "GET", "/api/v1/fundings?market_id=1"),
            ("api.rh.lighter.xyz", "GET", "/api/v1/orderBookOrders?market_id=1&limit=250"),
            ("mainnet.zklighter.elliot.ai", "GET", "/api/v1/orderBookDetails"),
            ("api.starknet.extended.exchange", "GET", "/api/v1/info/markets/BTC-USD/orderbook"),
            ("api.rise.trade", "GET", "/v1/markets"),
            ("api.arcus.xyz", "GET", "/v1/fundingRates"),
            ("api.prod.paradex.trade", "GET", "/v1/markets/summary?market=ALL"),
        ):
            self.assertIsNone(g.check_request(host, method, path), (host, method, path))

    def test_trading_and_account_calls_are_refused_on_every_host(self):
        for host, method, path in (
            ("api.hyperliquid.xyz", "POST", "/exchange"),          # HL order/cancel
            ("mainnet.zklighter.elliot.ai", "POST", "/api/v1/sendTx"),
            ("mainnet.zklighter.elliot.ai", "GET", "/api/v1/sendTxBatch"),
            ("api.rh.lighter.xyz", "GET", "/api/v1/account?by=index&value=1"),
            ("api.rh.lighter.xyz", "GET", "/api/v1/accountsByL1Address?l1_address=x"),
            ("mainnet.zklighter.elliot.ai", "GET", "/api/v1/nextNonce"),
            ("mainnet.zklighter.elliot.ai", "GET", "/api/v1/apikeys"),
            ("api.starknet.extended.exchange", "POST", "/api/v1/user/order"),
            ("api.starknet.extended.exchange", "POST", "/api/v1/user/order/rfq"),
            ("api.starknet.extended.exchange", "GET", "/api/v1/user/balance"),
            ("api.starknet.extended.exchange", "DELETE", "/api/v1/user/order/1"),
            ("api.rise.trade", "POST", "/v1/orders"),
            ("api.prod.paradex.trade", "POST", "/v1/auth"),
            ("api.prod.paradex.trade", "GET", "/v1/account"),
            ("rpc.txflow.com", "POST", "/exchange"),
            ("api.arcus.xyz", "PUT", "/v1/markets"),
        ):
            self.assertIsNotNone(g.check_request(host, method, path), (host, method, path))

    def test_unknown_hosts_are_refused(self):
        for host in ("api.binance.com", "example.com", "api.hyperliquid.xyz.evil.io", "8.8.8.8"):
            self.assertIsNotNone(g.check_host(host), host)
        self.assertIsNone(g.check_host("127.0.0.1"))

    def test_live_engine_port_is_off_limits(self):
        self.assertIsNotNone(g.check_connect(("127.0.0.1", 8765)))
        self.assertIsNone(g.check_connect(("127.0.0.1", 18765)))

    def test_request_line_parsing_ignores_body_chunks(self):
        self.assertEqual(g.parse_request_line(b"POST /info HTTP/1.1\r\nHost: x\r\n\r\n"),
                         ("POST", "/info"))
        self.assertIsNone(g.parse_request_line(b'{"type":"order"}'))

    def test_pacing_spaces_requests_per_host(self):
        slept, now = [], [100.0]
        guard = g.Guard(sleep=lambda s: slept.append(s), clock=lambda: now[0])
        guard.pace("api.hyperliquid.xyz")
        guard.pace("api.hyperliquid.xyz")
        guard.pace("api.rise.trade")
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 1.0)


class HookIntegrationTests(unittest.TestCase):
    def test_real_hook_blocks_in_a_child_process(self):
        root = os.path.dirname(os.path.abspath(__file__))
        code = textwrap.dedent("""
            import http.server, json, sys, threading, urllib.request
            sys.path.insert(0, %r)
            from pilot import network_guard as g
            seen = []
            class H(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    seen.append(("GET", self.path)); self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
                def do_POST(self):
                    seen.append(("POST", self.path)); self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
                def log_message(self, *a): pass
            srv = http.server.HTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            port = srv.server_address[1]
            g.install()
            out = {}
            def attempt(name, fn):
                try:
                    fn(); out[name] = "ok"
                except PermissionError as e:
                    out[name] = "refused"
                except Exception as e:
                    # urllib wraps socket-level errors: URLError(reason=PermissionError).
                    out[name] = ("refused" if isinstance(getattr(e, "reason", None), g.Refused)
                                 else "error:" + type(e).__name__)
            base = "http://127.0.0.1:%%d" %% port
            attempt("get_public", lambda: urllib.request.urlopen(base + "/api/v1/fundings", timeout=5).read())
            attempt("post_exchange", lambda: urllib.request.urlopen(urllib.request.Request(base + "/exchange", data=b"{}", method="POST"), timeout=5).read())
            attempt("get_account", lambda: urllib.request.urlopen(base + "/api/v1/account", timeout=5).read())
            attempt("unknown_host", lambda: urllib.request.urlopen("http://example.com/", timeout=5).read())
            attempt("live_port", lambda: urllib.request.urlopen("http://127.0.0.1:8765/healthz", timeout=5).read())
            out["server_saw"] = seen
            print(json.dumps(out))
        """) % root
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                           timeout=60, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        import json
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["get_public"], "ok")
        for k in ("post_exchange", "get_account", "unknown_host", "live_port"):
            self.assertEqual(out[k], "refused", (k, out))
        # The refused requests never reached the server.
        self.assertEqual(out["server_saw"], [["GET", "/api/v1/fundings"]])


if __name__ == "__main__":
    unittest.main()
