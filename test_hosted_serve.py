import base64, json, socket, threading, unittest, urllib.error, urllib.request
import bot


class StubEngine:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.running = False
        self.public_snapshot = None
        self.events_seen = []

    def snapshot(self):
        return {"mode": "PAPER", "running": self.running}

    def loop(self):
        self.stop_event.wait()

    def persist(self):
        pass

    def asset_view(self, asset):
        if asset != "BTC":
            raise ValueError(asset + ": yok")
        return {"asset": asset}

    def pair_view(self, asset, long, short):
        return {"asset": asset, "long": long, "short": short}

    def request_pair_sync(self, body):
        return {"ok": True, "started": body["venues"]}

    def events(self, kind, message, **extra):
        self.events_seen.append(kind)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HostedServeTests(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.engine = StubEngine()
        self.server = threading.Thread(
            target=bot.serve,
            args=(self.engine, self.port, "0.0.0.0", "s3cret"),
            daemon=True,
        )
        self.server.start()
        for _ in range(50):
            try:
                self.call("/healthz")
                break
            except OSError:
                threading.Event().wait(0.05)

    def tearDown(self):
        token = self.token()
        self.call("/api/shutdown", "s3cret", {}, token)
        self.server.join(5)

    def call(self, path, password=None, body=None, token=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=None if body is None else json.dumps(body).encode(),
            headers={"Host": "bot.up.railway.app"},
        )
        if password:
            raw = base64.b64encode(f"paper:{password}".encode()).decode()
            req.add_header("Authorization", "Basic " + raw)
        if token:
            req.add_header("X-Paper-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as ex:
            return ex.code, ex.read().decode()

    def token(self):
        html = self.call("/", "s3cret")[1]
        return html.split("window.paperToken='")[1].split("'")[0]

    def test_health_is_open_and_dashboard_needs_password(self):
        self.assertEqual(self.call("/healthz")[0], 200)
        self.assertEqual(self.call("/api/state")[0], 401)
        self.assertEqual(self.call("/api/state", "wrong")[0], 401)
        self.assertEqual(self.call("/api/state", "s3cret")[0], 200)

    def test_post_needs_password_and_page_token(self):
        token = self.token()
        self.assertEqual(self.call("/api/start", "wrong", {}, token)[0], 403)
        self.assertEqual(self.call("/api/start", "s3cret", {}, "bad")[0], 403)
        self.assertEqual(self.call("/api/start", "s3cret", {}, token)[0], 200)
        self.assertTrue(self.engine.running)

    def test_asset_and_pair_pages_read_query_and_sync_needs_token(self):
        status, body = self.call("/api/pair?asset=BTC&long=A&short=B%20C", "s3cret")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["short"], "B C")
        self.assertEqual(self.call("/api/asset?asset=BTC", "s3cret")[0], 200)
        self.assertEqual(self.call("/api/asset?asset=NOPE", "s3cret")[0], 404)
        self.assertEqual(self.call("/api/asset?asset=BTC")[0], 401)
        body = {"asset": "BTC", "venues": ["A"]}
        self.assertEqual(self.call("/api/pair-sync", "s3cret", body, "bad")[0], 403)
        status, raw = self.call("/api/pair-sync", "s3cret", body, self.token())
        self.assertEqual((status, json.loads(raw)["started"]), (200, ["A"]))


class BindGuardTests(unittest.TestCase):
    def test_public_bind_without_password_refuses_to_start(self):
        with self.assertRaises(SystemExit):
            bot.serve(StubEngine(), free_port(), "0.0.0.0", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ExclusivePortTests(unittest.TestCase):
    def test_second_server_on_same_port_is_refused(self):
        from http.server import BaseHTTPRequestHandler
        from bot import ExclusiveServer

        first = ExclusiveServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        try:
            with self.assertRaises(OSError):
                ExclusiveServer(
                    ("127.0.0.1", first.server_address[1]), BaseHTTPRequestHandler
                )
        finally:
            first.server_close()
