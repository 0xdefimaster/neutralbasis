"""Phase 14 Slice E: read model + GET-only Control Center server.

Security tests: side effects, sensitive-data leaks, path traversal, uncontrolled file
reads, unauthorized operations, DNS rebinding, no outbound connections.
Server tests use a MOCK read model (labelled); read-model tests use the saved
OBSERVED artifacts and skip when they are absent.
"""
import http.client
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from v2 import paths
from v2.control import netguard, readmodel, server

HAVE_DATA = paths.bench("v2-phase4-2026-11-12", "answers.json.gz").exists()


def mock_readmodel(folder):
    """MOCK read model (synthetic, never evidence)."""
    head = dict(schema=1, generated_at=time.time(), data_mode="HISTORICAL",
                banner="MOCK", trading_mode="PAPER", live_trading="DISABLED")
    opps = [dict(id="op-%05d" % i, at=1.0, asset="A%d" % (i % 3), strategy="funding",
                 long="TxFlow", short="tradeXYZ", long_xid="txflow", short_xid="tradexyz",
                 expected_net=None if i % 4 == 0 else float(i - 5),
                 stress_net=None if i % 4 == 0 else float(i - 8), label="WATCHLIST",
                 freshness=None, flags=["FRESHNESS_UNKNOWN"], safety="RESEARCH_ONLY",
                 risk_adjusted_score=None, gap_bp=1.0, carry_8h=None)
            for i in range(250)]
    files = dict(
        overview=dict(head, venues=[]),
        exchanges=dict(head, items=[dict(exchange_id="txflow", capabilities=dict(
            rest=dict(level="LIVE_OBSERVED", current=False, as_of=1.0)), records=[],
            problems=[], health="HISTORICAL_ONLY")]),
        markets=dict(head, items=[dict(asset="A1", venues=["TxFlow"])], doctor_records=[]),
        system=dict(head, items=[]), opportunities=dict(head, items=opps),
        opportunities_summary=dict(head, counts={}), strategies_readiness=dict(head, items={}),
        paper_positions=dict(head, items=[dict(id="p1", scenario_only=False),
                                          dict(id="p2", scenario_only=True)]),
        health_history=dict(head), funding_persistence=dict(head, status="NOT_RUN"))
    folder.mkdir(parents=True, exist_ok=True)
    for k, v in files.items():
        (folder / (k + ".json")).write_text(json.dumps(v), encoding="utf-8")
    (folder / "manifest.json").write_text(json.dumps(dict(head, files={})), encoding="utf-8")
    # a file next to the read model that must never be reachable
    (folder / "secret.txt").write_text("TOP-SECRET-DO-NOT-SERVE", encoding="utf-8")


class ServerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nb-ctl-"))
        self.rm = self.tmp / "readmodel"
        mock_readmodel(self.rm)
        self.srv = server.ControlServer("127.0.0.1", 0, folder=self.rm, heartbeat=False)
        self.srv.start_background()

    def tearDown(self):
        self.srv.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, method, path, host=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", host if host is not None else "127.0.0.1:%d" % self.srv.port)
        if body is not None:
            c.putheader("Content-Length", str(len(body)))
        c.endheaders(body)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def get(self, path, **kw):
        st, h, d = self.req("GET", path, **kw)
        return st, h, (json.loads(d) if d and h.get("Content-Type", "").startswith(
            "application/json") else d)


class BindTests(unittest.TestCase):
    def test_non_loopback_bind_refused(self):
        for host in ("0.0.0.0", "::", "192.168.1.5", "100.64.0.1", "example.com", ""):
            with self.assertRaises(ValueError, msg=host):
                server.ControlServer(host, 0, folder=tempfile.gettempdir(), heartbeat=False)

    def test_cli_refuses_non_loopback(self):
        from tools import nb_control
        self.assertEqual(nb_control.main(["serve", "--host", "0.0.0.0"]), 2)

    def test_loopback_accepted(self):
        s = server.ControlServer("127.0.0.1", 0, folder=tempfile.gettempdir(),
                                 heartbeat=False)
        self.assertEqual(s.httpd.server_address[0], "127.0.0.1")
        s.httpd.server_close()


class MethodAndSideEffectTests(ServerBase):
    def snapshot(self):
        return {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in self.tmp.rglob("*")}

    def test_only_get_and_head(self):
        before = self.snapshot()
        for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"):
            for path in ("/api/doctor/overview", "/api/start", "/api/config", "/"):
                st, h, _ = self.req(m, path, body=b"{}" if m in ("POST", "PUT", "PATCH")
                                    else None)
                self.assertEqual(st, 405, (m, path))
                self.assertEqual(h.get("Allow"), "GET, HEAD")
        self.assertEqual(before, self.snapshot())          # nothing written or changed
        self.assertGreaterEqual(self.srv.stats["refused"], 24)

    def test_no_action_routes_exist(self):
        for path in ("/api/start", "/api/pause", "/api/config", "/api/orders",
                     "/api/connections/extended-key", "/api/keys", "/api/rebuild",
                     "/api/doctor/run"):
            st, _, _ = self.get(path)
            self.assertEqual(st, 404, path)

    def test_head_has_no_body(self):
        st, h, d = self.req("HEAD", "/api/doctor/overview")
        self.assertEqual(st, 200)
        self.assertEqual(d, b"")
        self.assertGreater(int(h["Content-Length"]), 0)

    def test_get_reads_cache_only(self):
        """A refresh must not build, run checks or touch the network."""
        with mock.patch("v2.control.readmodel.build", side_effect=AssertionError("build")), \
                mock.patch("v2.control.doctor.run", side_effect=AssertionError("doctor")), \
                netguard.offline(allow_loopback=True) as attempts:
            for p in ("/api/meta", "/api/doctor/overview", "/api/doctor/exchanges",
                      "/api/doctor/exchanges/txflow", "/api/doctor/markets",
                      "/api/doctor/system", "/api/opportunities", "/api/opportunities/summary",
                      "/api/strategies/readiness", "/api/paper/positions",
                      "/api/health/history", "/api/funding/persistence"):
                st, _, body = self.get(p)
                self.assertEqual(st, 200, p)
                self.assertEqual(body.get("live_trading"), "DISABLED", p)
        self.assertEqual(attempts, [])


class TraversalAndFileTests(ServerBase):
    def test_traversal_and_unlisted_files(self):
        for path in ("/../secret.txt", "/secret.txt", "/%2e%2e/secret.txt",
                     "/..%2fsecret.txt", "/app.js/../secret.txt", "/static/../server.py",
                     "/readmodel/overview.json", "/overview.json", "/manifest.json",
                     "/api/doctor/exchanges/../../secret", "/api/doctor/exchanges/..%2f..",
                     "/api/doctor/exchanges/notavenue", "/api/opportunities/op-1",
                     "/api/opportunities/../../x", "//etc/passwd", "/\\..\\secret.txt",
                     "/C:/Windows/win.ini", "/api/doctor/exchanges/TXFLOW"):
            st, _, d = self.req("GET", path)
            self.assertIn(st, (400, 404), path)
            self.assertNotIn(b"TOP-SECRET", d, path)

    def test_known_venue_and_opportunity(self):
        st, _, b = self.get("/api/doctor/exchanges/txflow")
        self.assertEqual((st, b["item"]["exchange_id"]), (200, "txflow"))
        st, _, b = self.get("/api/doctor/exchanges/risex")   # whitelisted but absent
        self.assertEqual(st, 404)
        st, _, b = self.get("/api/opportunities/op-00007")
        self.assertEqual(b["item"]["id"], "op-00007")


class HeaderAndHostTests(ServerBase):
    def test_security_headers_and_no_cors(self):
        st, h, _ = self.get("/api/doctor/overview")
        self.assertEqual(st, 200)
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["Referrer-Policy"], "no-referrer")
        self.assertEqual(h["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", h)
        self.assertNotIn("Python", h.get("Server", ""))

    def test_dns_rebinding_host_refused(self):
        for host in ("evil.example", "evil.example:%d" % self.srv.port, "192.168.1.5",
                     "127.0.0.1.evil.example", ""):
            st, _, _ = self.get("/api/doctor/overview", host=host)
            self.assertEqual(st, 421, host)
        st, _, _ = self.get("/api/doctor/overview", host="localhost:%d" % self.srv.port)
        self.assertEqual(st, 200)


class QueryTests(ServerBase):
    def test_bounds_and_patterns(self):
        for q in ("page_size=1000", "page_size=0", "page=0", "page=99999999", "page_size=x",
                  "sort=__import__", "venue=%3Cscript%3E", "asset=" + "A" * 80,
                  "strategy=a&strategy=b"):
            st, _, b = self.get("/api/opportunities?" + q)
            self.assertEqual(st, 400, q)
            self.assertNotIn("Traceback", json.dumps(b))
        many = "&".join("k%d=1" % i for i in range(30))
        self.assertEqual(self.get("/api/opportunities?" + many)[0], 400)
        self.assertEqual(self.get("/api/opportunities?x=" + "a" * 3000)[0], 414)

    def test_paging_and_unknown_sorts_last(self):
        st, _, b = self.get("/api/opportunities?page_size=100&page=3")
        self.assertEqual((b["total"], b["pages"], len(b["items"])), (250, 3, 50))
        st, _, b = self.get("/api/opportunities?page_size=100&page=1&sort=expected_net")
        vals = [o["expected_net"] for o in b["items"]]
        self.assertEqual(vals[0], 244.0)
        st, _, b = self.get("/api/opportunities?page_size=100&page=3&sort=expected_net")
        self.assertIsNone(b["items"][-1]["expected_net"])   # null last, never 0
        st, _, b = self.get("/api/opportunities?asset=A1&page_size=100")
        self.assertTrue(all(o["asset"] == "A1" for o in b["items"]))

    def test_positions_exclude_scenarios_by_default(self):
        b = self.get("/api/paper/positions")[2]
        self.assertEqual([p["id"] for p in b["items"]], ["p1"])
        b = self.get("/api/paper/positions?include_scenarios=1")[2]
        self.assertEqual(len(b["items"]), 2)

    def test_missing_read_model_is_reported_not_faked(self):
        (self.rm / "overview.json").unlink()
        st, _, b = self.get("/api/doctor/overview")
        self.assertEqual((st, b["status"], b["data_mode"]), (200, "NOT_BUILT", "UNKNOWN"))


class RedactionTests(unittest.TestCase):
    def test_redact_drops_secret_keys_and_local_paths(self):
        bench = r"C:\Users\someone\nb-benchmark"
        x = dict(api_key="k", apiKey="k2", private_key="p", signing="s", auth_token="t",
                 password="pw", mnemonic="m", nested=[dict(secret="s", ok=1)],
                 path=bench + r"\v2-paper-data\x.json", rate=1e-4, nan=float("nan"))
        r = readmodel.redact(x, bench=bench, home=r"C:\Users\someone")
        self.assertEqual(sorted(r), ["nan", "nested", "path", "rate"])
        self.assertEqual(r["nested"], [dict(ok=1)])
        self.assertEqual(r["path"], r"<bench>\v2-paper-data\x.json")
        self.assertIsNone(r["nan"])


@unittest.skipUnless(HAVE_DATA, "saved artifacts absent")
class ReadModelObservedTests(unittest.TestCase):
    """Built from the saved OBSERVED artifacts into a temp folder, offline."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="nb-rm-"))
        os.environ["NB_FAKE_SECRET_TOKEN"] = "zz-planted-secret-value-zz"
        try:
            with netguard.offline() as attempts:
                cls.manifest = readmodel.build(out=cls.tmp)
        finally:
            os.environ.pop("NB_FAKE_SECRET_TOKEN", None)
        cls.attempts = attempts
        cls.docs = {n: json.loads((cls.tmp / (n + ".json")).read_text(encoding="utf-8"))
                    for n in readmodel.FILES}
        cls.raw = "".join((cls.tmp / (n + ".json")).read_text(encoding="utf-8")
                          for n in readmodel.FILES)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_offline_and_labelled(self):
        self.assertEqual(self.attempts, [])
        for n, d in self.docs.items():
            self.assertEqual((d["trading_mode"], d["live_trading"]), ("PAPER", "DISABLED"))
            if d["data_mode"] == "HISTORICAL":
                self.assertIn("RECORDER NOT RUNNING", d["banner"])

    def test_no_leaks(self):
        self.assertNotIn("zz-planted-secret-value-zz", self.raw)
        self.assertNotIn(str(paths.bench()), self.raw)
        self.assertNotIn(str(Path.home()), self.raw)
        self.assertNotIn(str(Path.home()).replace("\\", "\\\\"), self.raw)
        for k in ("api_key", "private_key", "apiKey", "secret", "password"):
            self.assertNotIn('"%s"' % k, self.raw)

    def test_unknown_never_healthy_or_verified(self):
        for e in self.docs["exchanges"]["items"]:
            if self.docs["exchanges"]["data_mode"] == "HISTORICAL":
                self.assertNotEqual(e["health"], "HEALTHY", e["exchange_id"])
            self.assertNotEqual(e["capabilities"]["execution_model"]["level"], "VERIFIED")
        var = [e for e in self.docs["exchanges"]["items"] if e["exchange_id"] == "variational"]
        self.assertEqual(var[0]["capabilities"]["execution_model"]["level"], "UNSUPPORTED")

    def test_opportunities_never_safe(self):
        items = self.docs["opportunities"]["items"]
        self.assertEqual(len(items), 3174)
        self.assertNotIn("SAFE", {o["safety"] for o in items})
        for o in items:
            if (o["expected_net"] or 0) > 0 and (o["stress_net"] is None or o["stress_net"] < 0):
                self.assertEqual(o["safety"], "NOT_SAFE", o["id"])
            if o["long_xid"] == "variational" or o["short_xid"] == "variational":
                self.assertIn("RFQ_INDICATIVE_NO_FIRM_QUOTE", o["flags"])

    def test_positions_are_estimated_and_reliability_explained(self):
        ps = self.docs["paper_positions"]["items"]
        self.assertTrue(ps)
        for p in ps:
            self.assertEqual(p["data_source"], "ESTIMATED")
            self.assertTrue(all(f["verified"] is False for f in p["fills"]))
            if p["valuation_problems"]:
                self.assertFalse(p["valuation_reliable"])
        rfq = [p for p in ps if p["short"] == "Variational" and not p["scenario_only"]]
        self.assertTrue(rfq and not rfq[0]["valuation_reliable"])

    def test_served_end_to_end_offline(self):
        srv = server.ControlServer("127.0.0.1", 0, folder=self.tmp, heartbeat=False)
        srv.start_background()
        try:
            with netguard.offline(allow_loopback=True) as attempts:
                for p in ("/api/meta", "/api/doctor/overview", "/api/doctor/exchanges",
                          "/api/doctor/exchanges/txflow", "/api/doctor/markets?venue=TxFlow",
                          "/api/doctor/system", "/api/opportunities?strategy=funding&"
                          "sort=expected_net&page_size=5", "/api/opportunities/op-00000",
                          "/api/opportunities/summary", "/api/strategies/readiness",
                          "/api/paper/positions", "/api/health/history",
                          "/api/funding/persistence"):
                    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=20)
                    c.request("GET", p)
                    r = c.getresponse()
                    body = json.loads(r.read())
                    c.close()
                    self.assertEqual(r.status, 200, p)
                    self.assertNotEqual(body.get("status"), "NOT_BUILT", p)
            self.assertEqual(attempts, [])
        finally:
            srv.shutdown()

    def test_files_are_small_enough(self):
        for n, size in self.manifest["files"].items():
            self.assertLess(size, 8 * 2 ** 20, n)


if __name__ == "__main__":
    unittest.main()
