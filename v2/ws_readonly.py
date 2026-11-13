"""Read-only WebSocket market data (Phase 10). PAPER ONLY. OFF by default.

Nothing here can trade. The egress guard (pilot/network_guard) sees only the host of a
WebSocket, not its frames (doc 35), so this module checks EVERY outgoing frame against a
per-venue allowlist before it is sent; anything else raises FrameRefused and the socket
is closed. A live connection is opened only when NB_V2_READONLY_WS=1 AND the caller
passes allow_live=True (owner decision; not done in Phase 10: all tests use MOCK frames).

Venues (official docs read 2026-11-13; UNKNOWN where the docs are silent):
  txflow        wss://rpc.txflow.com/ws (docs.txflow.com/api/websocket). Frames:
                {"method": "subscribe"|"unsubscribe", "type": T, "subscription":
                {"type": T, "coin": C}} - the top-level type is required ("the node
                routes on the top-level type and silently drops a frame without it");
                {"method": "ping"} -> {"method": "PONG"}; ping every 10 s, the gateway
                closes after ~16 s of client silence. l2BookDepth: one snapshot then
                deltas, seq / prevSeq continuity (resync procedure NOT STATED: we
                resubscribe). userFundings (account channel): only for the owner-
                authorized address (v2.observation.authorized_address). No order frame
                is documented on the WS; none is allowed.
  hyperliquid   wss://api.hyperliquid.xyz/ws (Hyperliquid docs, websocket pages); serves
                Hyperliquid native AND tradeXYZ (HIP-3 coin "xyz:NAME": the subscribe
                spelling is NOT STATED in the docs -> UNVERIFIED until a read-only probe
                is approved). Frames: {"method": "subscribe"|"unsubscribe",
                "subscription": {"type": T, "coin": C}}, {"method": "ping"} ->
                {"channel": "pong"}; the server closes after 60 s without a message.
                l2Book is a snapshot feed (no sequence numbers documented): each push
                replaces the book; an older `time` is ignored, never merged.
                "post" is REFUSED (it carries signed actions as well as info).
  lighter_rh    wss://api.rh.lighter.xyz/stream?readonly=true (docs.lighter.xyz/api
                websocket; Lighter on Robinhood Chain). Frames: {"type": "subscribe"|
                "unsubscribe", "channel": "order_book/N" | ...}, {"type": "ping"} ->
                {"type": "pong"}; at least one frame every 2 min. order_book: snapshot on
                subscribe, then state changes; continuity: begin_nonce of an update ==
                nonce of the previous one. "jsonapi/sendtx" / "sendtxbatch" (signed
                transactions) and any "auth" are REFUSED. Time unit of last_updated_at
                is NOT STATED: kept raw, no latency computed from it.
  Lighter Core (wss://mainnet.zklighter.elliot.ai/stream) is NOT in the 8 PAPER / 11
  observation venues: no adapter (scope rule). Variational: no WebSocket is documented.

Every incoming message becomes a record with source time, receive time, latency (when the
source time is in a known unit), market, sequence fields and a status: ok / duplicate /
stale / resync / ignored. A book is never patched from an old or out-of-order frame.
"""
import json
import os
import re
import time

from v2 import observation
from v2.marketdata import Backoff, SeqBook

ENV = "NB_V2_READONLY_WS"
FORBIDDEN_KEYS = {"signature", "action", "nonce", "vaultaddress", "auth", "tx_info",
                  "tx_infos", "tx_type", "tx_types", "privatekey", "private_key", "apikey",
                  "api_key", "secret", "expiresafter", "agentaddress"}
VENUES = dict(
    txflow=dict(url="wss://rpc.txflow.com/ws", ping_s=10.0, ping={"method": "ping"},
                public=("l2BookDepth", "l2Book", "activeAssetCtx", "trades", "bbo")),
    hyperliquid=dict(url="wss://api.hyperliquid.xyz/ws", ping_s=30.0,
                     ping={"method": "ping"},
                     public=("l2Book", "activeAssetCtx", "trades", "bbo")),
    lighter_rh=dict(url="wss://api.rh.lighter.xyz/stream?readonly=true", ping_s=60.0,
                    ping={"type": "ping"},
                    public=("order_book", "market_stats", "trade", "ticker")),
)


class FrameRefused(RuntimeError):
    pass


class LiveDisabled(RuntimeError):
    pass


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k).lower()
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def check_frame(venue, obj, environ=None):
    """None when the outgoing frame is an allowed read-only frame, else the reason."""
    if not isinstance(obj, dict):
        return "frame is not a JSON object"
    bad = FORBIDDEN_KEYS & set(_keys(obj))
    if bad:
        return "forbidden field(s) %s" % sorted(bad)
    v = VENUES.get(venue)
    if v is None:
        return "venue %r has no read-only WebSocket allowlist" % venue
    if obj == v["ping"]:
        return None
    if venue in ("txflow", "hyperliquid"):
        if obj.get("method") not in ("subscribe", "unsubscribe"):
            return "method %r is not allowed (only subscribe/unsubscribe/ping)" % (
                obj.get("method"),)
        sub = obj.get("subscription")
        if not isinstance(sub, dict):
            return "subscription object missing"
        t = sub.get("type")
        if venue == "txflow" and obj.get("type") != t:
            return "TxFlow needs the same top-level type as subscription.type"
        if venue == "hyperliquid" and set(obj) - {"method", "subscription"}:
            return "unexpected top-level fields %s" % sorted(set(obj) - {"method",
                                                                          "subscription"})
        if venue == "txflow" and t == "userFundings":
            addr = observation.authorized_address(environ)
            if addr is None or str(sub.get("user") or "").lower() != addr:
                return "userFundings only for the owner-authorized address"
            if set(sub) - {"type", "user", "exchangeId"}:
                return "unexpected userFundings fields"
            return None
        if t not in v["public"]:
            return "subscription type %r is not a public market-data channel" % t
        if "user" in sub:
            return "account field 'user' on a public channel"
        allowed = {"type", "coin", "nSigFigs", "mantissa"}
        if venue == "hyperliquid" and t == "l2Book":
            # docs: optional `fast` (boolean; "5 levels if fast, 20 levels if slow")
            allowed = allowed | {"fast"}
            if "fast" in sub and not isinstance(sub["fast"], bool):
                return "fast must be a boolean"
        if set(sub) - allowed:
            return "unexpected subscription fields %s" % sorted(set(sub) - allowed)
        if not isinstance(sub.get("coin"), str) or not sub["coin"]:
            return "coin missing"
        return None
    # lighter_rh
    if obj.get("type") not in ("subscribe", "unsubscribe"):
        return "type %r is not allowed (only subscribe/unsubscribe/ping)" % (obj.get("type"),)
    if set(obj) - {"type", "channel"}:
        return "unexpected fields %s" % sorted(set(obj) - {"type", "channel"})
    ch = str(obj.get("channel") or "")
    if not re.fullmatch(r"(%s)/\d+" % "|".join(v["public"]), ch):
        return "channel %r is not a public market channel" % ch
    return None


# ----------------------------------------------------------------- incoming handlers
class Books:
    """Per (venue, market) book state from WS frames; never merges an old frame."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.seq = {}            # txflow SeqBook per coin
        self.snap = {}           # hyperliquid: (coin) -> dict(time, bids, asks)
        self.lighter = {}        # lighter_rh: market -> dict(nonce, bids, asks, raw_ts)
        self.seen = set()        # (venue, channel, market, source time / seq) for dedupe
        self.records = []        # the last 5000 records (bounded)
        self.ctx = {}
        # Totals that never shrink (the bounded record list must not hide the first
        # snapshot or an early error): per (venue, channel, market, status, kind).
        self.totals = {}
        self.per_market = {}     # market -> dict(last_recv, gaps [..], lat [..]) bounded
        self.samples = {}        # (venue, channel) -> first raw frames (truncated)
        self.aliases = {}        # txflow book id -> coin field seen

    LATENCY_BASIS = ("APPARENT: receive time - source time; includes the offset between "
                     "the local clock and the venue clock (local sync UNKNOWN)")

    def record(self, **r):
        r.setdefault("recv_ts", self.clock())
        st = r.get("source_ts")
        r["latency_s"] = None if st is None else r["recv_ts"] - st
        if r["latency_s"] is not None:
            r["latency_basis"] = self.LATENCY_BASIS
            r["source_ahead_of_local"] = r["latency_s"] < 0
        k = (r.get("venue"), r.get("channel"), str(r.get("market")), r.get("status"),
             r.get("kind"))
        self.totals[k] = self.totals.get(k, 0) + 1
        if r.get("status") == "ok" and r.get("market") is not None:
            pm = self.per_market.setdefault((r.get("venue"), str(r["market"])),
                                            dict(last=None, gaps=[], lat=[], ok=0))
            pm["ok"] += 1
            if pm["last"] is not None:
                pm["gaps"].append(r["recv_ts"] - pm["last"])
                del pm["gaps"][:-20000]
            pm["last"] = r["recv_ts"]
            if r["latency_s"] is not None:
                pm["lat"].append(r["latency_s"])
                del pm["lat"][:-20000]
        self.records.append(r)
        del self.records[:-5000]
        return r

    def total(self, venue=None, status=None, kind=None, channel=None, market=None):
        return sum(n for (v, c, m, s, k), n in self.totals.items()
                   if (venue is None or v == venue) and (status is None or s == status)
                   and (kind is None or k == kind) and (channel is None or c == channel)
                   and (market is None or m == str(market)))

    def latency_stats(self, venue):
        """Apparent latency per venue. Its minimum bounds the clock offset: when the
        local clock is not synchronized, no single value is a true network delay."""
        xs = sorted(r["latency_s"] for r in self.records
                    if r.get("venue") == venue and r.get("latency_s") is not None
                    and r.get("status") in ("ok", "duplicate", "stale"))
        if not xs:
            return dict(n=0)
        return dict(n=len(xs), min=xs[0], median=xs[len(xs) // 2], max=xs[-1],
                    negative=sum(1 for x in xs if x < 0), basis=self.LATENCY_BASIS)

    def handle(self, venue, text):
        msg = json.loads(text)
        ch = msg.get("type") if venue == "lighter_rh" else (
            msg.get("channel") or msg.get("method"))
        smp = self.samples.setdefault((venue, str(ch)), [])
        if len(smp) < 2:
            smp.append(text[:600])
        if venue == "txflow":
            return self.txflow(msg)
        if venue == "hyperliquid":
            return self.hyperliquid(msg)
        return self.lighter_rh(msg)

    def dedupe(self, key):
        if key in self.seen:
            return True
        self.seen.add(key)
        if len(self.seen) > 200000:
            self.seen.clear()
        return False

    # TxFlow: l2BookDepth snapshot + deltas with seq/prevSeq; activeAssetCtx
    def txflow(self, msg):
        if msg.get("method") == "PONG":
            return self.record(venue="txflow", channel="pong", status="ok")
        ch, d = msg.get("channel"), msg.get("data") or {}
        if ch == "l2BookDepth":
            # key the book by the instrument id ("id", e.g. 119) when present: the
            # `coin` field can be a name ("NATGAS-USDC") while subscriptions use the id
            coin = str(d["id"]) if d.get("id") is not None else str(d.get("coin"))
            if d.get("coin") is not None:
                self.aliases[coin] = d.get("coin")
            st = d["time"] / 1000.0 if d.get("time") is not None else None
            if self.dedupe(("txflow", ch, coin, d.get("seq"), d.get("type"))):
                return self.record(venue="txflow", channel=ch, market=coin, seq=d.get("seq"),
                                   source_ts=st, status="duplicate")
            cur = self.seq.get(coin)
            if (d.get("type") == "snapshot" and cur is not None and cur.valid and
                    cur.ts is not None and d.get("time") is not None and d["time"] < cur.ts):
                # an older snapshot never replaces a newer valid book
                return self.record(venue="txflow", channel=ch, market=coin, seq=d.get("seq"),
                                   source_ts=st, status="stale", kind="snapshot")
            res = self.seq.setdefault(coin, SeqBook()).apply(d)
            return self.record(venue="txflow", channel=ch, market=coin, seq=d.get("seq"),
                               prev_seq=d.get("prevSeq"), kind=d.get("type"), source_ts=st,
                               status=res)
        if ch == "activeAssetCtx":
            ctx = d.get("ctx") or {}
            bt = ctx.get("blockTime")
            st = bt / 1000.0 if bt else None
            if self.dedupe(("txflow", ch, str(d.get("coin")), bt)):
                return self.record(venue="txflow", channel=ch, market=str(d.get("coin")),
                                   source_ts=st, status="duplicate")
            self.ctx[("txflow", str(d.get("coin")))] = dict(ctx, source_ts=st)
            return self.record(venue="txflow", channel=ch, market=str(d.get("coin")),
                               source_ts=st, status="ok")
        return self.record(venue="txflow", channel=ch, status="ignored")

    # Hyperliquid / tradeXYZ: l2Book full snapshots (no seq); activeAssetCtx (no time)
    def hyperliquid(self, msg):
        ch, d = msg.get("channel"), msg.get("data") or {}
        if ch == "pong":
            return self.record(venue="hyperliquid", channel="pong", status="ok")
        if ch == "l2Book":
            coin, t = d.get("coin"), d.get("time")
            st = t / 1000.0 if t is not None else None
            prev = self.snap.get(coin)
            if self.dedupe(("hyperliquid", ch, coin, t)):
                return self.record(venue="hyperliquid", channel=ch, market=coin,
                                   source_ts=st, status="duplicate")
            if prev is not None and t is not None and t < prev["time"]:
                return self.record(venue="hyperliquid", channel=ch, market=coin,
                                   source_ts=st, status="stale")
            lv = d.get("levels") or [[], []]
            bids = [(float(x["px"]), float(x["sz"])) for x in lv[0]]
            asks = [(float(x["px"]), float(x["sz"])) for x in lv[1]]
            if bids and asks and bids[0][0] >= asks[0][0]:
                return self.record(venue="hyperliquid", channel=ch, market=coin,
                                   source_ts=st, status="resync")
            self.snap[coin] = dict(time=t, bids=bids, asks=asks)   # replace, never merge
            return self.record(venue="hyperliquid", channel=ch, market=coin, source_ts=st,
                               status="ok")
        if ch == "activeAssetCtx":
            self.ctx[("hyperliquid", d.get("coin"))] = dict(d.get("ctx") or {},
                                                             source_ts=None)
            return self.record(venue="hyperliquid", channel=ch, market=d.get("coin"),
                               source_ts=None, status="ok",
                               note="no source time in activeAssetCtx (receive time only)")
        if ch == "subscriptionResponse":
            return self.record(venue="hyperliquid", channel=ch, status="ok")
        return self.record(venue="hyperliquid", channel=ch, status="ignored")

    # Lighter RH: order_book snapshot then deltas; begin_nonce == previous nonce
    def lighter_rh(self, msg):
        t = str(msg.get("type") or "")
        if t == "pong":
            return self.record(venue="lighter_rh", channel="pong", status="ok")
        if not t.endswith("order_book"):
            return self.record(venue="lighter_rh", channel=t, status="ignored")
        market = str(msg.get("channel") or "").split(":")[-1]
        ob = msg.get("order_book") or {}
        nonce, begin = ob.get("nonce"), ob.get("begin_nonce")
        raw_ts = ob.get("last_updated_at", msg.get("timestamp"))
        base = dict(venue="lighter_rh", channel="order_book", market=market, seq=nonce,
                    prev_seq=begin, source_raw=raw_ts, source_ts=None,
                    note="time unit NOT STATED in docs: latency not computed")
        if self.dedupe(("lighter_rh", market, nonce, t)):
            return self.record(status="duplicate", **base)
        st = self.lighter.get(market)
        if t.startswith("subscribed/"):
            st = dict(nonce=nonce, valid=True,
                      bids={float(x["price"]): float(x["size"]) for x in ob.get("bids") or []},
                      asks={float(x["price"]): float(x["size"]) for x in ob.get("asks") or []})
            self.lighter[market] = st
            return self.record(status=self.lighter_check(st), kind="snapshot", **base)
        if st is None or not st["valid"]:
            return self.record(status="resync", kind="delta", **base)
        if begin != st["nonce"]:
            st["valid"] = False
            return self.record(status="resync", kind="delta", **base)
        for side, book in (("bids", st["bids"]), ("asks", st["asks"])):
            for x in ob.get(side) or []:
                px, sz = float(x["price"]), float(x["size"])
                if sz == 0:
                    book.pop(px, None)
                else:
                    book[px] = sz
        st["nonce"] = nonce
        return self.record(status=self.lighter_check(st), kind="delta", **base)

    @staticmethod
    def lighter_check(st):
        if st["bids"] and st["asks"] and max(st["bids"]) >= min(st["asks"]):
            st["valid"] = False
            return "resync"
        return "ok"

    def top(self, venue, market):
        """(best bid, best ask, source time or None) or None when no valid book."""
        if venue == "txflow":
            b = self.seq.get(str(market))
            if not b or not b.valid or not b.bids or not b.asks:
                return None
            return max(b.bids), min(b.asks), b.ts / 1000.0 if b.ts else None
        if venue == "hyperliquid":
            b = self.snap.get(market)
            if not b or not b["bids"] or not b["asks"]:
                return None
            return b["bids"][0][0], b["asks"][0][0], b["time"] / 1000.0
        b = self.lighter.get(str(market))
        if not b or not b["valid"] or not b["bids"] or not b["asks"]:
            return None
        return max(b["bids"]), min(b["asks"]), None


# ----------------------------------------------------------------- heartbeat socket
POLL_S = 1.0
SILENT_S = 30.0


def heartbeat_class():
    """extended_rfq.WebSocket with recv_or_tick(): wait for the FIRST byte of the next
    frame for at most the socket's poll timeout and return None if none came (the caller
    then pings); once a frame has started it is read to the end. A wake-up therefore
    never cuts a frame (extended_rfq's `cancelled` hook could, mid-frame)."""
    import socket as _socket

    from extended_rfq import WebSocket

    class HeartbeatWebSocket(WebSocket):
        def recv_or_tick(self):
            self.check_owner()
            if not self.buffer:
                try:
                    chunk = self.sock.recv(65536)
                except _socket.timeout:
                    if time.monotonic() - self.last_data >= self.silent_after:
                        raise ConnectionError("WebSocket silent for %.0f s" % self.silent_after)
                    return None
                if not chunk:
                    raise ConnectionError("WebSocket closed")
                self.last_data = time.monotonic()
                self.buffer += chunk
            return self.recv()

    return HeartbeatWebSocket


def heartbeat_socket(url, poll=POLL_S, silent=SILENT_S):
    return heartbeat_class()(url, timeout=silent, poll=poll)


# ----------------------------------------------------------------- client
class ReadOnlyWS:
    """One venue, several subscriptions. `connect(url)` returns an object with
    send(opcode, bytes), recv() -> str and close() (extended_rfq.WebSocket shape)."""

    MAX_FRAMES_PER_S = 5

    def __init__(self, venue, subscriptions, connect=None, clock=time.time,
                 sleep=time.sleep, backoff=None, books=None, allow_live=False,
                 environ=None):
        if venue not in VENUES:
            raise ValueError("no read-only WebSocket for %r" % venue)
        for s in subscriptions:
            why = check_frame(venue, s, environ)
            if why:
                raise FrameRefused(why)
        if connect is None:
            env = os.environ if environ is None else environ
            if not allow_live or (env.get(ENV) or "").strip() != "1":
                raise LiveDisabled("live WebSocket needs %s=1 and allow_live=True (owner "
                                   "decision)" % ENV)
            connect = heartbeat_socket
        self.venue, self.subs, self.connect = venue, list(subscriptions), connect
        self.clock, self.sleep = clock, sleep
        self.backoff = backoff or Backoff(1.0, 60.0)
        self.books = books or Books(clock)
        self.environ = environ
        self.conn, self.last_ping, self.sent = None, 0.0, []
        self.stats = dict(connects=0, reconnects=0, errors=0, refused=0, frames=0)

    def send(self, obj):
        why = check_frame(self.venue, obj, self.environ)
        if why:
            self.stats["refused"] += 1
            if self.conn is not None:
                self.conn.close()
                self.conn = None
            raise FrameRefused(why)
        now = self.clock()
        self.sent = [t for t in self.sent if now - t < 1.0]
        if len(self.sent) >= self.MAX_FRAMES_PER_S:
            self.sleep(1.0 - (now - self.sent[0]))
        self.conn.send(1, json.dumps(obj).encode())
        self.sent.append(self.clock())

    def open(self):
        self.conn = self.connect(VENUES[self.venue]["url"])
        self.stats["connects"] += 1
        for s in self.subs:
            self.send(s)
        self.last_ping = self.clock()

    def step(self):
        """One read or one reconnect; never raises except FrameRefused."""
        try:
            if self.conn is None:
                self.open()
            if self.clock() - self.last_ping >= VENUES[self.venue]["ping_s"]:
                self.send(VENUES[self.venue]["ping"])
                self.last_ping = self.clock()
            # The read must not block past the next ping: a connection that only waits
            # for data goes silent and the gateway closes it (TxFlow: ~16 s). recv_or_tick
            # returns None when nothing arrived within the poll interval (frame boundary
            # only, so no frame is ever cut).
            if hasattr(self.conn, "recv_or_tick"):
                text = self.conn.recv_or_tick()
                if text is None:
                    return dict(venue=self.venue, status="tick")
            else:
                text = self.conn.recv()
            if text is None:
                raise ConnectionError("socket closed")
            self.stats["frames"] += 1
            r = self.books.handle(self.venue, text)
            if r.get("status") == "resync":
                self.resubscribe(r.get("market"))
            self.backoff.reset()
            return r
        except FrameRefused:
            raise
        except Exception as ex:
            self.stats["errors"] += 1
            try:
                if self.conn is not None:
                    self.conn.close()
            except Exception:
                pass
            self.conn = None
            self.stats["reconnects"] += 1
            self.sleep(self.backoff.next())
            return dict(venue=self.venue, status="reconnect", error=str(ex)[:200])

    def resubscribe(self, market):
        for s in self.subs:
            m = (s.get("subscription") or {}).get("coin") or str(s.get("channel", "")).split(
                "/")[-1]
            if market is None or str(m) == str(market):
                un = dict(s, method="unsubscribe") if "method" in s else dict(s,
                                                                             type="unsubscribe")
                self.send(un)
                self.send(s)


# ----------------------------------------------------------------- WS vs REST
def compare(ws_top, rest_book, rest_ts, max_gap_s=2.0):
    """Consistency of a WS top of book with a REST book of the same market.
    NOT_COMPARABLE when either side is missing or the times are more than max_gap_s
    apart (never compared across moments); CONSISTENT when the mids differ by no more
    than the wider half-spread."""
    if ws_top is None or not rest_book or not rest_book.get("bids") or not rest_book.get(
            "asks"):
        return dict(status="NOT_COMPARABLE", reason="missing side")
    wb, wa, wts = ws_top
    if wts is None or rest_ts is None or abs(wts - rest_ts) > max_gap_s:
        return dict(status="NOT_COMPARABLE", reason="times %s / %s more than %.1f s apart"
                    % (wts, rest_ts, max_gap_s))
    rb, ra = rest_book["bids"][0][0], rest_book["asks"][0][0]
    wm, rm = (wb + wa) / 2, (rb + ra) / 2
    tol = max((wa - wb) / 2, (ra - rb) / 2)
    diff = abs(wm - rm)
    return dict(status="CONSISTENT" if diff <= tol + 1e-12 else "INCONSISTENT",
                mid_diff_bp=1e4 * diff / rm, tolerance_bp=1e4 * tol / rm,
                time_gap_s=abs(wts - rest_ts))
