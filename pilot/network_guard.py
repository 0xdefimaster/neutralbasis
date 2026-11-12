"""Network guard for the isolated PAPER pilot (process-wide Python audit hook).

PAPER ONLY. The pilot may read public market data and nothing else:

* DNS: only the public market-data hosts the engine's own code uses, plus loopback.
* HTTP request line: GET only, except POST /info on the two info-RPC venues
  (Hyperliquid, TxFlow), which is how they serve public data.
* Any path segment naming a trading or account action is refused on every host
  (exchange, order(s), sendTx, account*, user, auth, apikey(s), nonce, withdraw,
  transfer, private, ...), even under GET.
* No connection to the live engine's port (8765).
* A per-host minimum spacing between requests, so the pilot never crowds the live
  engine's public rate limits.

A refused call raises PermissionError inside the caller and is logged (JSONL). An audit
hook cannot be removed once installed: the guard lasts for the life of the process.
The decision functions are pure and unit-tested; `install` wires them to the hooks.
"""
import json
import re
import threading
import time

PUBLIC_HOSTS = {
    # host: allowed POST paths (public info RPC); every other request must be GET.
    "api.hyperliquid.xyz": ("/info",),
    "rpc.txflow.com": ("/info",),
    "mainnet.zklighter.elliot.ai": (),
    "api.rh.lighter.xyz": (),
    "api.starknet.extended.exchange": (),
    "api.rise.trade": (),
    "api.arcus.xyz": (),
    "api.prod.paradex.trade": (),
    "omni-client-api.prod.ap-northeast-1.variational.io": (),
    # Extra perp DEXes (owner decision 2026-11-11), reached only through CCXT public
    # market-data calls; hosts read from the installed CCXT 4.5.90 `urls`. Nado's public
    # reads are POST queries (gateway /v1/query, archive /v1); its order path
    # (/v1/execute) is not on this list and is refused.
    "fapi.asterdex.com": (),
    "sapi.asterdex.com": (),  # CCXT load_markets also lists Aster spot (exchangeInfo)
    "api.pacifica.fi": (),
    "gateway.prod.nado.xyz": ("/v1/query",),
    "archive.prod.nado.xyz": ("/v1",),
    "data-api.hibachi.xyz": (),
    "omni.apex.exchange": (),
}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
LIVE_ENGINE_PORT = 8765
# Path segments (case-insensitive) that name a trading or account action.
FORBIDDEN = re.compile(
    r"^(exchange|orders?|order_?book_?cancel|cancel\w*|sendtx\w*|tx|account\w*|users?|"
    r"auth\w*|apikeys?|api_keys?|nonce|nextnonce|withdraw\w*|transfer\w*|private|"
    r"sign\w*|deposit\w*|leverage|margin|subaccounts?|onboarding|referral\w*|rfq|"
    # Account reads on the extra CCXT venues' shared hosts (Aster fapi, ApeX v3).
    r"balance\w*|position\w*|usertrades|listenkey|income|allorders|openorders|"
    r"batchorders|fills|execute)$",
    re.I,
)
# Pacifica answered HTTP 429 at the default spacing (pilot, 2026-11-11).
SPACING = {"api.hyperliquid.xyz": 1.0, "api.pacifica.fi": 1.2}
DEFAULT_SPACING = 0.25


class Refused(PermissionError):
    pass


def check_host(host):
    """None when the pilot may resolve/connect to `host`, else the reason."""
    if host is None:
        return None
    h = host.decode() if isinstance(host, bytes) else str(host)
    h = h.strip("[]").lower()
    if h in LOOPBACK or h in PUBLIC_HOSTS:
        return None
    return "host not on the public market-data allowlist: " + h


def check_request(host, method, path):
    """None when the HTTP request is a public market-data read, else the reason."""
    why = check_host(host)
    if why:
        return why
    h = str(host).lower()
    m = (method or "").upper()
    p = (path or "/").split("?", 1)[0]
    for segment in p.split("/"):
        if segment and FORBIDDEN.match(segment):
            return "trading/account path segment refused: %s %s%s" % (m, h, p)
    if m == "GET":
        return None
    if m == "POST" and p in PUBLIC_HOSTS.get(h, ()):
        return None
    if h in LOOPBACK:
        # The pilot's own HTTP server is reached by the operator, not by the pilot.
        return "pilot process makes no non-GET loopback calls: %s %s" % (m, p)
    return "method refused: %s %s%s" % (m, h, p)


def check_connect(address):
    """None unless the address is the live engine's loopback port."""
    try:
        ip, port = address[0], address[1]
    except (TypeError, IndexError):
        return None
    if str(ip) in LOOPBACK and port == LIVE_ENGINE_PORT:
        return "live engine port %d is off limits to the pilot" % LIVE_ENGINE_PORT
    return None


def parse_request_line(data):
    """(method, path) from the first bytes of an HTTP request, or None (body chunks)."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    head = bytes(data[:4096])
    line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = line.split(" ")
    if len(parts) >= 3 and parts[2].startswith("HTTP/") and parts[0].isalpha():
        return parts[0], parts[1]
    return None


def split_request(data):
    """(method, path, body bytes or None) of a chunk that starts an HTTP request."""
    req = parse_request_line(data)
    if req is None:
        return None
    raw = bytes(data)
    head, sep, body = raw.partition(b"\r\n\r\n")
    return req[0], req[1], (body if sep and body else None)


class Guard:
    def __init__(self, log_path=None, sleep=time.sleep, clock=time.monotonic, v2=False):
        self.log_path = log_path
        self.sleep, self.clock = sleep, clock
        self.lock = threading.Lock()
        self.last = {}
        self.stats = dict(allowed=0, refused=0, broad_metadata=0)
        # V2 (NB_V2=1): eight-venue scope with payload checks (v2.network_policy).
        self.v2 = v2
        # POST requests whose body has not been seen yet, per connection object id.
        self.pending = {}
        self.by_venue = {}

    def refuse(self, kind, why, detail=None):
        with self.lock:
            self.stats["refused"] += 1
            if self.log_path:
                with open(self.log_path, "a", encoding="utf8") as f:
                    f.write(json.dumps(dict(t=time.time(), kind=kind, why=why,
                                            detail=detail)) + "\n")
        raise Refused("PAPER pilot network guard: " + why)

    def pace(self, host):
        gap = SPACING.get(host, DEFAULT_SPACING)
        with self.lock:
            wait = self.last.get(host, 0.0) + gap - self.clock()
            self.last[host] = max(self.clock(), self.last.get(host, 0.0) + gap)
        if wait > 0:
            self.sleep(wait)

    def v2_send(self, event, conn, data):
        """V2: every request is matched to one of the eight venues, POST bodies
        included. A POST whose body arrives in a later chunk is held until it does; a
        new request on that connection before the body is refused (fail-closed)."""
        from v2 import network_policy

        host = str(getattr(conn, "host", None) or "").lower()
        key = id(conn)
        req = split_request(data)
        if req is None:
            pending = self.pending.pop(key, None)
            if pending is None:
                return
            method, path = pending
            body = data
        else:
            if key in self.pending:
                self.pending.pop(key, None)
                self.refuse(event, "previous POST body was never checked", dict(host=host))
            method, path, body = req
            if host in LOOPBACK:
                if method.upper() != "GET":
                    self.refuse(event, "pilot process makes no non-GET loopback calls")
                return
            if method.upper() == "POST" and body is None:
                # Host and path now; the body is checked when it is sent.
                d = network_policy.check_route(host, method, path)
                if not d.allowed:
                    self.refuse(event, d.reason, dict(method=method, host=host))
                self.pending[key] = (method, path)
                return
        d = network_policy.check(host, method, path, body)
        if not d.allowed:
            self.refuse(event, d.reason, dict(method=method, host=host, path=path))
        with self.lock:
            self.by_venue[d.exchange_id] = self.by_venue.get(d.exchange_id, 0) + 1
            if d.flag == network_policy.BROAD:
                self.stats["broad_metadata"] += 1
                if self.log_path:
                    with open(self.log_path, "a", encoding="utf8") as f:
                        f.write(json.dumps(dict(t=time.time(), kind="audit",
                                                flag=d.flag, venue=d.exchange_id,
                                                host=host, path=path)) + "\n")
        self.pace(host)
        with self.lock:
            self.stats["allowed"] += 1

    def __call__(self, event, args):
        if self.v2:
            if event == "socket.getaddrinfo":
                from v2 import network_policy

                h = args[0] if args else None
                if h is not None:
                    h = (h.decode() if isinstance(h, bytes) else str(h)).strip("[]").lower()
                    if h not in LOOPBACK and h not in network_policy.allowed_hosts():
                        self.refuse(event, "host not in the V2 eight-venue allowlist: " + h)
                return
            if event == "socket.connect":
                why = check_connect(args[1] if len(args) > 1 else None)
                if why:
                    self.refuse(event, why)
                return
            if event == "http.client.send":
                self.v2_send(event, args[0], args[1] if len(args) > 1 else None)
            return
        if event == "socket.getaddrinfo":
            why = check_host(args[0] if args else None)
            if why:
                self.refuse(event, why)
        elif event == "socket.connect":
            why = check_connect(args[1] if len(args) > 1 else None)
            if why:
                self.refuse(event, why)
        elif event == "http.client.send":
            conn, data = args[0], args[1] if len(args) > 1 else None
            req = parse_request_line(data)
            if req is None:
                return
            host = getattr(conn, "host", None)
            why = check_request(host, req[0], req[1])
            if why:
                self.refuse(event, why, dict(method=req[0], host=host))
            if str(host).lower() not in LOOPBACK:
                self.pace(str(host).lower())
            with self.lock:
                self.stats["allowed"] += 1


_GUARD = None


def install(log_path=None, v2=None):
    """Install the guard once for this process; returns it. v2=None follows NB_V2."""
    import sys

    global _GUARD
    if _GUARD is None:
        if v2 is None:
            from v2 import policy

            v2 = policy.v2_enabled()
        _GUARD = Guard(log_path, v2=v2)
        sys.addaudithook(_GUARD)
    return _GUARD
