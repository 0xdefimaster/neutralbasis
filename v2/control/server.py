"""NeutralBasis Control Center: a GET-only, read-only HTTP server (Phase 14 Slice E).

PAPER ONLY. Separate from the legacy bot.serve (which has POST routes; not used here).

  * binds to loopback only (127.0.0.1 / ::1 / localhost); anything else is refused.
    Later remote access is meant to be `tailscale serve` proxying to this loopback
    port (no code change, no public IP, never Tailscale Funnel); not enabled now.
  * GET and HEAD only; every other method gets 405 and touches nothing.
  * a fixed route table: API routes read the read-model JSON files (built offline by
    v2.control.readmodel); static routes map fixed names to fixed files. No request
    value is ever used as a filesystem path, so there is no traversal surface.
  * the Host header must be a loopback name (DNS-rebinding defence).
  * no CORS; CSP self-only; nosniff; no-referrer; frame-ancestors none; no-store.
  * query values are bounded and pattern-checked; pages are capped.
  * the server never opens an outbound connection, never reads env into a response,
    never runs a check, never starts or stops anything. A refresh reads files only.
"""
import ipaddress
import json
import re
import socket
import socketserver
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from v2.control import readmodel

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/manifest.webmanifest": ("manifest.webmanifest",
                                    "application/manifest+json; charset=utf-8"),
          "/icon.svg": ("icon.svg", "image/svg+xml")}
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1", "[::1]"}
MAX_URL = 2048
MAX_PAGE_SIZE = 100
MAX_PAGE = 10000
VALUE = re.compile(r"^[A-Za-z0-9_.:\- ]{1,40}$")
SORTS = {"expected_net", "stress_net", "risk_adjusted_score", "gap_bp", "carry_8h", "asset"}
OPP_FILTERS = ("strategy", "label", "venue", "asset", "safety", "flag", "freshness")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")


class BadRequest(Exception):
    pass


def is_loopback(host):
    h = str(host).strip("[]")
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


class Store:
    """Read-model files, parsed once per file mtime. Read-only."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.cache = {}
        self.lock = threading.Lock()

    def get(self, name):
        if name not in readmodel.FILES:
            raise KeyError(name)
        f = self.folder / (name + ".json")
        try:
            st = f.stat()
        except FileNotFoundError:
            return None
        with self.lock:
            hit = self.cache.get(name)
            if hit and hit[0] == (st.st_mtime_ns, st.st_size):
                return hit[1]
        data = json.loads(f.read_text(encoding="utf-8"))
        with self.lock:
            self.cache[name] = ((st.st_mtime_ns, st.st_size), data)
        return data


def q1(qs, key, default=None):
    v = qs.get(key)
    if not v:
        return default
    if len(v) > 1:
        raise BadRequest("repeated parameter: %s" % key)
    v = v[0]
    if not VALUE.match(v):
        raise BadRequest("invalid value for %s" % key)
    return v


def qint(qs, key, default, lo, hi):
    v = q1(qs, key)
    if v is None:
        return default
    if not v.isdigit():
        raise BadRequest("%s must be an integer" % key)
    n = int(v)
    if not lo <= n <= hi:
        raise BadRequest("%s out of range [%d, %d]" % (key, lo, hi))
    return n


def page(items, qs):
    size = qint(qs, "page_size", 25, 1, MAX_PAGE_SIZE)
    p = qint(qs, "page", 1, 1, MAX_PAGE)
    total = len(items)
    return dict(items=items[(p - 1) * size: p * size], page=p, page_size=size, total=total,
                pages=(total + size - 1) // size)


def head_of(doc):
    return {k: doc.get(k) for k in ("schema", "generated_at", "data_mode", "banner",
                                     "trading_mode", "live_trading")}


def missing(name):
    return dict(status="NOT_BUILT", reason="read model '%s' not built; run "
                "`python tools/nb_control.py build`" % name, data_mode="UNKNOWN",
                trading_mode="PAPER", live_trading="DISABLED")


# ------------------------------------------------------------------------- routes
def r_simple(name):
    def h(store, qs, now):
        d = store.get(name)
        return d if d is not None else missing(name)
    return h


def r_exchanges(store, qs, now):
    d = store.get("exchanges")
    if d is None:
        return missing("exchanges")
    items = [dict({k: e[k] for k in e if k not in ("records", "capabilities")},
                  capabilities={c: dict(level=x["level"], current=x.get("current"),
                                        as_of=x.get("as_of"))
                                for c, x in e["capabilities"].items()})
             for e in d["items"]]
    return dict(head_of(d), items=items)


def r_exchange(xid):
    def h(store, qs, now):
        d = store.get("exchanges")
        if d is None:
            return missing("exchanges")
        for e in d["items"]:
            if e["exchange_id"] == xid:
                return dict(head_of(d), item=e)
        return None
    return h


def r_markets(store, qs, now):
    d = store.get("markets")
    if d is None:
        return missing("markets")
    items = d["items"]
    venue, asset = q1(qs, "venue"), q1(qs, "asset")
    if venue:
        items = [m for m in items if venue in m["venues"]]
    if asset:
        items = [m for m in items if m["asset"] == asset]
    return dict(head_of(d), **page(items, qs))


def r_opportunities(store, qs, now):
    d = store.get("opportunities")
    if d is None:
        return missing("opportunities")
    items = d["items"]
    f = {k: q1(qs, k) for k in OPP_FILTERS}
    if f["strategy"]:
        items = [o for o in items if o["strategy"] == f["strategy"]]
    if f["label"]:
        items = [o for o in items if o["label"] == f["label"]]
    if f["venue"]:
        items = [o for o in items if f["venue"] in (o["long"], o["short"], o["long_xid"],
                                                    o["short_xid"])]
    if f["asset"]:
        items = [o for o in items if o["asset"] == f["asset"]]
    if f["safety"]:
        items = [o for o in items if o["safety"] == f["safety"]]
    if f["flag"]:
        items = [o for o in items if f["flag"] in o["flags"]]
    if f["freshness"]:
        items = [o for o in items if (o["freshness"] or "UNKNOWN") == f["freshness"]]
    sort = q1(qs, "sort", "expected_net")
    if sort not in SORTS:
        raise BadRequest("sort must be one of %s" % sorted(SORTS))
    if sort == "asset":
        items = sorted(items, key=lambda o: (o["asset"] or "", o["id"]))
    else:  # unknown (null) values sort last, never as zero
        items = sorted(items, key=lambda o: (o[sort] is None, -(o[sort] or 0), o["id"]))
    return dict(head_of(d), filters={k: v for k, v in f.items() if v}, sort=sort,
                **page(items, qs))


def r_opportunity(oid):
    def h(store, qs, now):
        d = store.get("opportunities")
        if d is None:
            return missing("opportunities")
        for o in d["items"]:
            if o["id"] == oid:
                return dict(head_of(d), item=o)
        return None
    return h


def r_positions(store, qs, now):
    d = store.get("paper_positions")
    if d is None:
        return missing("paper_positions")
    items = d["items"]
    if q1(qs, "include_scenarios") != "1":
        items = [p for p in items if not p["scenario_only"]]
    return dict(head_of(d), **page(items, qs))


def r_meta(store, qs, now):
    m = None
    f = store.folder / "manifest.json"
    if f.exists():
        m = json.loads(f.read_text(encoding="utf-8"))
    return dict(trading_mode="PAPER", live_trading="DISABLED", server="GET-only",
                server_time=now, read_model=None if m is None else
                dict(generated_at=m.get("generated_at"), age_s=now - m["generated_at"],
                     data_mode=m.get("data_mode"), banner=m.get("banner"),
                     files=m.get("files")))


FIXED = {
    "/api/meta": r_meta,
    "/api/doctor/overview": r_simple("overview"),
    "/api/doctor/exchanges": r_exchanges,
    "/api/doctor/markets": r_markets,
    "/api/doctor/system": r_simple("system"),
    "/api/opportunities": r_opportunities,
    "/api/opportunities/summary": r_simple("opportunities_summary"),
    "/api/strategies/readiness": r_simple("strategies_readiness"),
    "/api/paper/positions": r_positions,
    "/api/health/history": r_simple("health_history"),
    "/api/funding/persistence": r_simple("funding_persistence"),
}
EXCHANGE_RE = re.compile(r"^/api/doctor/exchanges/([a-z0-9_]{1,32})$")
OPP_RE = re.compile(r"^/api/opportunities/(op-[0-9]{5})$")


def route(path):
    if path in FIXED:
        return FIXED[path]
    m = EXCHANGE_RE.match(path)
    if m and m.group(1) in readmodel.VENUE_IDS:
        return r_exchange(m.group(1))
    m = OPP_RE.match(path)
    if m:
        return r_opportunity(m.group(1))
    return None


# ------------------------------------------------------------------------ handler
def make_handler(store, stats):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NeutralBasisControl/1"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 15

        def log_message(self, fmt, *args):  # no request logging to stderr/disk
            pass

        def _send(self, code, body, ctype, extra=None, head=False):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header("Cache-Control", "no-store")
            # one request per connection: an unread body (e.g. of a refused POST) can
            # never be parsed as a second request
            self.send_header("Connection", "close")
            self.close_connection = True
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def _json(self, code, obj, head=False, extra=None):
            self._send(code, json.dumps(obj, default=str, separators=(",", ":"),
                                        allow_nan=False),
                       "application/json; charset=utf-8", extra, head)

        def _host_ok(self):
            h = self.headers.get("Host") or ""
            name = h.rsplit(":", 1)[0] if not h.startswith("[") else h.split("]")[0] + "]"
            return name in LOOPBACK_NAMES

        def _deny(self):
            stats["refused"] += 1
            self._json(405, dict(error="method not allowed: this server is GET-only "
                                 "(PAPER; no actions)"), extra={"Allow": "GET, HEAD"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _deny

        def do_HEAD(self):
            self._get(head=True)

        def do_GET(self):
            self._get(head=False)

        def _get(self, head):
            stats["requests"] += 1
            try:
                if len(self.path) > MAX_URL:
                    return self._json(414, dict(error="URL too long"), head)
                if not self._host_ok():
                    stats["refused"] += 1
                    return self._json(421, dict(error="host not allowed"), head)
                parts = urlsplit(self.path)
                path = parts.path
                if "%" in path or ".." in path or "\\" in path or "//" in path:
                    return self._json(404, dict(error="not found"), head)
                if path in STATIC:
                    fname, ctype = STATIC[path]
                    f = STATIC_DIR / fname
                    if not f.is_file():
                        return self._json(404, dict(error="not found"), head)
                    return self._send(200, f.read_bytes(), ctype, head=head)
                h = route(path)
                if h is None:
                    return self._json(404, dict(error="not found"), head)
                qs = parse_qs(parts.query, keep_blank_values=False, max_num_fields=20)
                body = h(store, qs, time.time())
                if body is None:
                    return self._json(404, dict(error="not found"), head)
                return self._json(200, body, head)
            except BadRequest as ex:
                return self._json(400, dict(error=str(ex)), head)
            except ValueError:
                return self._json(400, dict(error="bad request"), head)
            except Exception:
                stats["errors"] += 1
                return self._json(500, dict(error="internal error"), head)
    return Handler


class LocalServer(ThreadingHTTPServer):
    """No socket.getfqdn() at bind (HTTPServer does one): no name lookup at all."""
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        pass  # no traceback output; a dropped client is not a server fault

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port


class LocalV6Server(LocalServer):
    address_family = socket.AF_INET6


class ControlServer:
    def __init__(self, host="127.0.0.1", port=8765, folder=None, heartbeat=True):
        if not is_loopback(host):
            raise ValueError("refused: the Control Center binds to loopback only "
                             "(got %r). Remote access later: tailscale serve -> loopback; "
                             "never 0.0.0.0, a public IP or Funnel." % host)
        self.store = Store(folder or readmodel.readmodel_dir())
        self.stats = dict(requests=0, refused=0, errors=0, started_at=time.time())
        handler = make_handler(self.store, self.stats)
        cls = LocalV6Server if ":" in host else LocalServer
        self.httpd = cls((host.strip("[]"), port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.heartbeat = heartbeat
        self._stop = threading.Event()

    def _beat(self):
        from v2.control import doctor, telemetry
        try:
            w = telemetry.HeartbeatWriter(doctor.control_dir(), "control_server",
                                          every_s=15.0, max_bytes=64 * 1024)
        except Exception:
            return
        t = telemetry.Telemetry()
        while not self._stop.is_set():
            try:
                w.maybe_write(t, extra=dict(stats=dict(self.stats), port=self.port))
            except Exception:
                pass
            self._stop.wait(15.0)

    def serve_forever(self):
        if self.heartbeat:
            threading.Thread(target=self._beat, daemon=True).start()
        try:
            self.httpd.serve_forever(poll_interval=0.5)
        finally:
            self._stop.set()

    def start_background(self):
        th = threading.Thread(target=self.serve_forever, daemon=True)
        th.start()
        return th

    def shutdown(self):
        self._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
