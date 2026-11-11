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
SPACING = {"api.hyperliquid.xyz": 1.0, "api.pacifica.fi": 0.6}
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


class Guard:
    def __init__(self, log_path=None, sleep=time.sleep, clock=time.monotonic):
        self.log_path = log_path
        self.sleep, self.clock = sleep, clock
        self.lock = threading.Lock()
        self.last = {}
        self.stats = dict(allowed=0, refused=0)

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

    def __call__(self, event, args):
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


def install(log_path=None):
    """Install the guard once for this process; returns it."""
    import sys

    global _GUARD
    if _GUARD is None:
        _GUARD = Guard(log_path)
        sys.addaudithook(_GUARD)
    return _GUARD
