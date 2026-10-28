"""Extended RFQ real order book, read from its dedicated WebSocket stream.

On an RFQ market Extended publishes two different books:
  * the indicative book (REST orderbook and the normal stream): market-maker quotes,
    no user orders, no fill guarantee;
  * the real book (`/orderbooks/rfq/{market}` stream): resting user orders,
    aggregated per price level.
Only the real book is kept here, and it never comes from or goes to the normal book
path. It is built from SNAPSHOT and DELTA messages; a sequence gap, a disconnect or a
silent socket drops the book, and nothing is served until a fresh SNAPSHOT arrives.
Every message is recorded with its exchange time, receive time and sequence number.

Stdlib only: a minimal RFC 6455 client, so no new package is needed.
"""

import base64, hashlib, json, os, socket, ssl, threading, time, urllib.parse
from bot import now, number

STREAM = (
    "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks/rfq/"
)
SOURCE = "extended_rfq"
# The server pings regularly; a socket silent this long is treated as lost.
SILENT_AFTER = 30
RECONNECT_MIN, RECONNECT_MAX = 2, 60
# Recorded raw messages are kept this long.
KEEP_SECONDS = 3 * 86400
# Book views older than this (since the last frame of a live connection) are refused.
VIEW_MAX_AGE = 30
WAITING = "RFQ defteri yeni snapshot bekliyor"


class RfqBook:
    """One market's real RFQ book. Levels keep the exchange time, receive time and
    sequence number of the message that last set them."""

    def __init__(self, market):
        self.market = market
        self.bids = {}
        self.asks = {}
        self.seq = None
        self.ready = False
        self.exchange_ts = None
        self.received = None
        self.reason = WAITING
        self.snapshots = 0
        self.gaps = 0

    def invalidate(self, reason):
        """Drop the book; only a new SNAPSHOT makes it usable again."""
        self.bids, self.asks = {}, {}
        self.ready = False
        self.seq = None
        self.reason = reason

    def apply(self, msg, received):
        """Apply one stream message. Returns True when the book changed."""
        kind = msg.get("type")
        if kind not in ("SNAPSHOT", "DELTA"):
            return False
        data = msg.get("data") or {}
        if data.get("m") not in (None, self.market):
            raise ValueError("RFQ akışında başka piyasa: " + str(data.get("m")))
        seq, ts = msg.get("seq"), msg.get("ts")
        if seq is None or ts is None:
            raise ValueError("RFQ mesajında seq/ts yok")
        seq = int(seq)
        ts = number(ts) / 1000
        if kind == "SNAPSHOT":
            self.bids, self.asks = {}, {}
            self.snapshots += 1
        elif not self.ready:
            # Deltas before the first snapshot cannot be placed.
            return False
        elif seq != self.seq + 1:
            self.gaps += 1
            self.invalidate(
                f"RFQ akışında sıra boşluğu ({self.seq} → {seq}); yeni snapshot bekleniyor"
            )
            raise ConnectionError(self.reason)
        for side, key in ((self.bids, "b"), (self.asks, "a")):
            for x in data.get(key) or []:
                price = number(x["p"])
                if price <= 0:
                    continue
                if kind == "SNAPSHOT":
                    qty = number(x["q"])
                elif x.get("c") is not None:
                    # Absolute level size when the stream sends it.
                    qty = number(x["c"])
                else:
                    # Delta quantity is a change to the level.
                    old = side.get(price)
                    qty = (old["qty"] if old else 0.0) + number(x["q"])
                if qty <= 1e-12:
                    side.pop(price, None)
                else:
                    side[price] = dict(
                        price=price, qty=qty, exchange_ts=ts, received=received, seq=seq
                    )
        self.seq = seq
        self.exchange_ts = ts
        self.received = received
        self.ready = True
        self.reason = None
        return True

    def levels(self):
        bids = sorted(self.bids.values(), key=lambda l: -l["price"])
        asks = sorted(self.asks.values(), key=lambda l: l["price"])
        return bids, asks


class WebSocket:
    """Minimal blocking RFC 6455 client: text frames, ping/pong, close."""

    def __init__(self, url, timeout=SILENT_AFTER):
        u = urllib.parse.urlsplit(url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((host, port), timeout=timeout)
        if u.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock = raw
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        self.sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\nUser-Agent: FundingDeskPaper/0.3\r\n\r\n"
            ).encode()
        )
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket el sıkışması kapandı")
            head += chunk
            if len(head) > 65536:
                raise ConnectionError("WebSocket el sıkışması çok uzun")
        head, self.buffer = head.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise ConnectionError("WebSocket reddedildi: " + lines[0])
        accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
        ).decode()
        headers = {
            k.strip().lower(): v.strip()
            for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)
        }
        if headers.get("sec-websocket-accept") != accept:
            raise ConnectionError("WebSocket kabul anahtarı geçersiz")

    def read(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WebSocket bağlantısı kapandı")
            self.buffer += chunk
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def send(self, opcode, payload=b""):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + body)

    def recv(self):
        """Next text message; answers pings on the way. Raises on close."""
        parts = []
        while True:
            b0, b1 = self.read(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = int.from_bytes(self.read(2), "big")
            elif n == 127:
                n = int.from_bytes(self.read(8), "big")
            mask = self.read(4) if b1 & 0x80 else None
            payload = self.read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:
                self.send(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise ConnectionError("Sunucu WebSocket bağlantısını kapattı")
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf8")

    def close(self):
        try:
            self.send(0x8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class RfqRecorder:
    """Raw RFQ stream messages with exchange time, receive time and sequence."""

    def __init__(self, store):
        self.store = store
        self.pruned = 0
        with store.lock:
            store.db.executescript(
                """create table if not exists rfq_messages(id integer primary key,
     market text, seq integer, kind text, exchange_ts real, received real, raw text);
   create index if not exists rfq_messages_lookup on rfq_messages(market, received);"""
            )
            store.db.commit()

    def record(self, market, msg, raw, received):
        ts = msg.get("ts")
        with self.store.lock:
            self.store.db.execute(
                "insert into rfq_messages(market,seq,kind,exchange_ts,received,raw) values(?,?,?,?,?,?)",
                (
                    market,
                    msg.get("seq"),
                    msg.get("type") or "",
                    number(ts) / 1000 if ts is not None else None,
                    received,
                    raw,
                ),
            )
            if received - self.pruned > 3600:
                self.store.db.execute(
                    "delete from rfq_messages where received<?",
                    (received - KEEP_SECONDS,),
                )
                self.pruned = received
            self.store.db.commit()

    def messages(self, market, start, end=None):
        with self.store.lock:
            rows = self.store.db.execute(
                "select seq,kind,exchange_ts,received,raw from rfq_messages "
                "where market=? and received>=? and received<=? order by id",
                (market, start, end if end is not None else now() + 1),
            ).fetchall()
        return [
            dict(seq=s, kind=k, exchange_ts=e, received=r, msg=json.loads(raw))
            for s, k, e, r, raw in rows
        ]


class RfqStream:
    """Keeps one market's real RFQ book from its stream, reconnecting on loss."""

    def __init__(self, market, recorder=None, connect=WebSocket, stop=None):
        self.market = market
        self.book = RfqBook(market)
        self.recorder = recorder
        self.connect = connect
        self.stop = stop or threading.Event()
        self.lock = threading.RLock()
        self.connected = False
        self.last_frame = None
        self.reconnects = 0
        self.error = None
        self.thread = None

    def handle(self, text, received):
        """One raw message from the socket."""
        msg = json.loads(text)
        if self.recorder is not None:
            self.recorder.record(self.market, msg, text, received)
        with self.lock:
            self.last_frame = received
            if msg.get("error"):
                self.error = str(msg["error"])
            return self.book.apply(msg, received)

    def lost(self, reason):
        with self.lock:
            self.connected = False
            self.error = reason
            self.book.invalidate("Bağlantı koptu; yeni snapshot bekleniyor")

    def run_once(self):
        ws = self.connect(STREAM + urllib.parse.quote(self.market))
        try:
            with self.lock:
                self.connected = True
                self.last_frame = now()
            while not self.stop.is_set():
                self.handle(ws.recv(), now())
        finally:
            ws.close()

    def run(self):
        delay = RECONNECT_MIN
        while not self.stop.is_set():
            started = now()
            try:
                self.run_once()
            except Exception as ex:
                self.lost(str(ex) or ex.__class__.__name__)
            if self.stop.is_set():
                break
            self.reconnects += 1
            delay = (
                RECONNECT_MIN if now() - started > 60 else min(RECONNECT_MAX, delay * 2)
            )
            self.stop.wait(delay)
        self.lost("Durduruldu")

    def start(self):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def view(self, at=None, max_age=VIEW_MAX_AGE):
        """The real book as bids/asks for paper fills, or ValueError when unusable."""
        at = now() if at is None else at
        with self.lock:
            b = self.book
            if not self.connected:
                raise ValueError(
                    "RFQ akışı bağlı değil; giriş yok ("
                    + (self.error or b.reason or WAITING)
                    + ")"
                )
            if not b.ready:
                raise ValueError(b.reason or WAITING)
            if self.last_frame is None or at - self.last_frame > max_age:
                raise ValueError("RFQ akışı sessiz; defter güncel sayılmaz")
            bids, asks = b.levels()
            return dict(
                source=SOURCE,
                market=self.market,
                bids=[(l["price"], l["qty"]) for l in bids],
                asks=[(l["price"], l["qty"]) for l in asks],
                bid_levels=[dict(l) for l in bids],
                ask_levels=[dict(l) for l in asks],
                seq=b.seq,
                exchange_ts=b.exchange_ts,
                received=self.last_frame,
                changed=b.received,
                audit_id=None,
            )

    def status(self, at=None):
        at = now() if at is None else at
        with self.lock:
            b = self.book
            bids, asks = b.levels()
            return dict(
                market=self.market,
                connected=self.connected,
                ready=b.ready,
                seq=b.seq,
                exchange_ts=b.exchange_ts,
                age=None if self.last_frame is None else at - self.last_frame,
                bid_levels=len(bids),
                ask_levels=len(asks),
                best_bid=bids[0]["price"] if bids else None,
                best_ask=asks[0]["price"] if asks else None,
                snapshots=b.snapshots,
                gaps=b.gaps,
                reconnects=self.reconnects,
                reason=b.reason,
                error=self.error,
            )


class ExtendedRfq:
    """One stream per Extended RFQ market; started for markets the feed marks RFQ."""

    def __init__(self, store, stop=None, connect=WebSocket):
        self.recorder = RfqRecorder(store)
        self.stop = stop or threading.Event()
        self.connect = connect
        self.streams = {}
        self.lock = threading.RLock()

    def sync(self, meta):
        """Start streams for every RFQ market in the Extended meta (asset -> market)."""
        for asset, m in (meta or {}).items():
            if m and m.get("rfq"):
                self.ensure(asset, m["id"])

    def ensure(self, asset, market):
        with self.lock:
            s = self.streams.get(asset)
            if s is None:
                s = RfqStream(market, self.recorder, self.connect, self.stop)
                self.streams[asset] = s
            s.start()
            return s

    def book(self, asset, at=None):
        s = self.streams.get(asset)
        if s is None:
            raise ValueError("RFQ akışı bu piyasa için başlatılmadı")
        return s.view(at)

    def status(self):
        return {a: s.status() for a, s in sorted(self.streams.items())}
