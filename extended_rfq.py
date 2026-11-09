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
from bot import now, number, first_at

STREAM = (
    "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks/rfq/"
)
SOURCE = "extended_rfq"
# The server pings regularly; a socket silent this long is treated as lost.
SILENT_AFTER = 30
# A blocked read wakes up this often so the worker that owns the socket can notice a
# retirement or a stop and close the socket itself. No other thread ever touches it:
# OpenSSL does not allow one TLS connection to be used from two threads at once.
POLL_SECONDS = 1.0
RECONNECT_MIN, RECONNECT_MAX = 2, 60
# Recorded raw messages are kept this long.
KEEP_SECONDS = 3 * 86400
# Messages are written in batches (one commit about every second, not one per message),
# so the streams do not queue on the store lock the whole engine shares.
FLUSH_SECONDS = 1.0
FLUSH_ROWS = 2000
# Expired messages are deleted in id windows of this size, one short lock hold each.
PRUNE_ROWS = 5000
# A book is current while its connection is proven alive: frames (data or ping) arrive in
# order on one socket, so after a ping every earlier change has been applied. Measured
# on Extended (2026-11-10): pings every ~1.35 s, data frames up to 53.5 s apart on a
# quiet book, periodic SNAPSHOTs. A view needs a frame (any kind) within ALIVE_MAX_AGE
# and a data frame within VIEW_MAX_AGE (the stream must still be delivering its book).
ALIVE_MAX_AGE = 5
VIEW_MAX_AGE = 90
WAITING = "RFQ defteri yeni snapshot bekliyor"
# At most this many RFQ books are streamed (held legs always are, even beyond it).
STREAM_LIMIT = 12
# A stream keeps its slot this long after it starts while its market is still a
# candidate: re-ranking every cycle used to retire books before their first snapshot.
MIN_HOLD_SECONDS = 600


def choose_streams(ranked, held, since, at, limit=STREAM_LIMIT, hold=MIN_HOLD_SECONDS):
    """Which RFQ books to stream. `ranked`: candidate assets, best first; `held`: assets
    with open paper legs, always kept; `since`: start time of the running streams.
    Young streams of still-ranked assets keep their slot first, then the best ranked
    fill the rest. Never more than max(limit, len(held)) streams."""
    cap = max(limit, len(held))
    chosen = set(held)
    young = [a for a in ranked if a in since and at - since[a] < hold]
    for a in young + list(ranked):
        if len(chosen) >= cap:
            break
        chosen.add(a)
    return chosen


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


class Retired(Exception):
    """The stream was asked to stop; raised in the worker that owns the socket."""


class WebSocket:
    """Minimal blocking RFC 6455 client: text frames, ping/pong, close.

    Single owner: the thread that opens the socket is the only one allowed to read,
    write or close it. Another thread gets a RuntimeError instead of racing the TLS
    state. A blocked read wakes up every `poll` seconds to ask `cancelled()`; silence
    longer than `timeout` is a lost connection."""

    def __init__(self, url, timeout=SILENT_AFTER, poll=None, cancelled=None, alive=None):
        self.owner = threading.get_ident()
        self.silent_after = timeout
        self.cancelled = cancelled or (lambda: False)
        # Called (in this owner thread) on every ping/pong: proof the link is alive.
        self.alive = alive
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
        self.last_data = time.monotonic()
        if poll:
            self.sock.settimeout(min(poll, timeout))

    def check_owner(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError(
                "WebSocket yalnızca açan thread tarafından kullanılabilir"
            )

    def read(self, n):
        self.check_owner()
        while len(self.buffer) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                # A poll wake-up: nothing was consumed, the TLS state is intact.
                if self.cancelled():
                    raise Retired("RFQ akışı durduruldu")
                if time.monotonic() - self.last_data >= self.silent_after:
                    raise ConnectionError("WebSocket sessiz; bağlantı kopmuş sayıldı")
                continue
            if not chunk:
                raise ConnectionError("WebSocket bağlantısı kapandı")
            self.last_data = time.monotonic()
            self.buffer += chunk
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def send(self, opcode, payload=b""):
        self.check_owner()
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
                if self.alive is not None:
                    self.alive()
                continue
            if opcode == 0xA:
                if self.alive is not None:
                    self.alive()
                continue
            if opcode == 0x8:
                raise ConnectionError("Sunucu WebSocket bağlantısını kapattı")
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf8")

    def close(self):
        self.check_owner()
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
        self.pending = []
        self.flushed = time.monotonic()
        self.lock = threading.Lock()
        # One flush at a time, so batches (and each market's messages) keep their order.
        self.flushing = threading.Lock()
        with store.lock:
            store.db.executescript(
                """create table if not exists rfq_messages(id integer primary key,
     market text, seq integer, kind text, exchange_ts real, received real, raw text);
   create index if not exists rfq_messages_lookup on rfq_messages(market, received);"""
            )
            store.db.commit()

    def record(self, market, msg, raw, received):
        ts = msg.get("ts")
        row = (
            market,
            msg.get("seq"),
            msg.get("type") or "",
            number(ts) / 1000 if ts is not None else None,
            received,
            raw,
        )
        with self.lock:
            self.pending.append(row)
            due = (
                len(self.pending) >= FLUSH_ROWS
                or time.monotonic() - self.flushed >= FLUSH_SECONDS
            )
        if due:
            # A stream never waits for another stream's write; it reads its socket on.
            self.flush(wait=False)

    def flush(self, wait=True):
        """Write the pending messages in one transaction; expired ones are deleted
        about once an hour, in the background."""
        if not self.flushing.acquire(blocking=wait):
            return
        try:
            with self.lock:
                rows, self.pending = self.pending, []
                self.flushed = time.monotonic()
            if not rows:
                return
            with self.store.lock:
                self.store.db.executemany(
                    "insert into rfq_messages(market,seq,kind,exchange_ts,received,raw)"
                    " values(?,?,?,?,?,?)",
                    rows,
                )
                self.store.db.commit()
            received = rows[-1][4]
            if received - self.pruned > 3600:
                self.pruned = received
                cutoff = received - KEEP_SECONDS
                with self.store.lock:
                    oldest = self.store.db.execute(
                        "select received from rfq_messages order by id limit 1"
                    ).fetchone()
                if oldest and oldest[0] < cutoff:
                    threading.Thread(
                        target=self.prune, args=(cutoff,), daemon=True, name="rfq-prune"
                    ).start()
        finally:
            self.flushing.release()

    def prune(self, cutoff):
        """Delete messages received before `cutoff`, in id windows up to the first id at
        the cutoff, so no lock hold scans the table."""
        with self.store.lock:
            end = first_at(self.store.db, "rfq_messages", "received", cutoff)
            start = (
                self.store.db.execute("select min(id) from rfq_messages").fetchone()[0]
                or 0
            )
        while start < end:
            stop = min(start + PRUNE_ROWS, end)
            with self.store.lock:
                self.store.db.execute(
                    "delete from rfq_messages where id>=? and id<? and received<?",
                    (start, stop, cutoff),
                )
                self.store.db.commit()
            start = stop

    def messages(self, market, start, end=None):
        self.flush()
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

    def __init__(self, market, recorder=None, connect=WebSocket, stop=None, trace=None,
                 asset=None):
        self.market = market
        self.asset = asset or market
        self.book = RfqBook(market)
        self.recorder = recorder
        self.connect = connect
        self.stop = stop or threading.Event()
        self.lock = threading.RLock()
        self.connected = False
        self.last_frame = None
        # Last frame of any kind (data or ping) on the live connection.
        self.last_alive = None
        self.reconnects = 0
        self.error = None
        self.thread = None
        self.retired = threading.Event()
        # Observation only (rfq_trace.RfqTrace or None); never touches the socket.
        self.trace = trace
        self.subscribed_at = None
        self.first_snapshot_at = None

    def note(self, kind, **fields):
        # Observation only; a failure is counted by the trace, never raised here.
        if self.trace is not None:
            self.trace.guard(self.trace.event, kind, self.asset, market=self.market,
                             **fields)

    def cancelled(self):
        return self.stop.is_set() or self.retired.is_set()

    def handle(self, text, received):
        """One raw message from the socket."""
        msg = json.loads(text)
        if self.recorder is not None:
            self.recorder.record(self.market, msg, text, received)
        with self.lock:
            self.last_frame = received
            self.last_alive = received
            if msg.get("error"):
                self.error = str(msg["error"])
            was_ready = self.book.ready
            changed = self.book.apply(msg, received)
            became_ready = not was_ready and self.book.ready
            if became_ready and self.first_snapshot_at is None:
                self.first_snapshot_at = received
            seq, ts = self.book.seq, self.book.exchange_ts
        if became_ready:
            # First valid snapshot after (re)connecting: the book is usable from here.
            self.note("first_snapshot", at=received, seq=seq, exchange_ts=ts)
        return changed

    def lost(self, reason):
        with self.lock:
            self.connected = False
            self.error = reason
            self.book.invalidate("Bağlantı koptu; yeni snapshot bekleniyor")
        if not self.retired.is_set():
            kind = "gap" if "boşluğu" in str(reason) else "lost"
            self.note(kind, error=reason)

    def mark_alive(self):
        """A ping or pong arrived (called in the worker that owns the socket)."""
        with self.lock:
            self.last_alive = now()

    def open(self):
        url = STREAM + urllib.parse.quote(self.market)
        if self.connect is WebSocket:
            return WebSocket(
                url, timeout=SILENT_AFTER, poll=POLL_SECONDS, cancelled=self.cancelled,
                alive=self.mark_alive,
            )
        return self.connect(url)

    def run_once(self):
        """Connect, read and close, all in the calling (worker) thread."""
        ws = self.open()
        try:
            with self.lock:
                self.connected = True
                self.last_frame = now()
                self.last_alive = self.last_frame
            self.note("connected")
            while not self.cancelled():
                self.handle(ws.recv(), now())
        finally:
            ws.close()

    def run(self):
        delay = RECONNECT_MIN
        while not self.cancelled():
            started = now()
            try:
                self.run_once()
            except Retired:
                break
            except Exception as ex:
                self.lost(str(ex) or ex.__class__.__name__)
            if self.cancelled():
                break
            self.reconnects += 1
            delay = (
                RECONNECT_MIN if now() - started > 60 else min(RECONNECT_MAX, delay * 2)
            )
            self.retired.wait(delay)
        self.lost("Durduruldu")

    def retire(self):
        """Ask the worker to stop. Only a flag: the worker closes its own socket within
        POLL_SECONDS (or after the frame it is reading). Never touches the socket."""
        self.retired.set()
        self.lost("İzleme sırasından çıktı")

    def join(self, timeout=None):
        t = self.thread
        if t is not None:
            t.join(timeout)
        return t is None or not t.is_alive()

    def start(self):
        if self.retired.is_set():
            raise RuntimeError("Emekli RFQ akışı yeniden başlatılamaz")
        if self.thread is None or not self.thread.is_alive():
            if self.subscribed_at is None:
                self.subscribed_at = now()
            self.thread = threading.Thread(
                target=self.run, daemon=True, name="rfq-" + self.market
            )
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
            alive = max(self.last_alive or 0, self.last_frame or 0) or None
            if alive is None or at - alive > ALIVE_MAX_AGE:
                raise ValueError("RFQ akışı sessiz; bağlantı canlılığı doğrulanmadı")
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
                # When the book was last proven current (live link, no sequence gap);
                # the time of its last change stays in `changed`.
                received=alive,
                data_received=self.last_frame,
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
                alive_age=None if self.last_alive is None else at - self.last_alive,
                bid_levels=len(bids),
                ask_levels=len(asks),
                best_bid=bids[0]["price"] if bids else None,
                best_ask=asks[0]["price"] if asks else None,
                snapshots=b.snapshots,
                gaps=b.gaps,
                reconnects=self.reconnects,
                reason=b.reason,
                error=self.error,
                subscribed_at=self.subscribed_at,
                first_snapshot_at=self.first_snapshot_at,
            )


class ExtendedRfq:
    """One stream per Extended RFQ market; started for markets the feed marks RFQ."""

    def __init__(self, store, stop=None, connect=WebSocket, trace=None):
        self.recorder = RfqRecorder(store)
        self.stop = stop or threading.Event()
        self.connect = connect
        self.streams = {}
        self.lock = threading.RLock()
        self.trace = trace

    def sync(self, meta):
        """Keep exactly the requested books; retire unused subscriptions."""
        wanted = {a for a, m in (meta or {}).items() if m and m.get("rfq")}
        with self.lock:
            for a in set(self.streams) - wanted:
                self.streams.pop(a).retire()
        for asset, m in (meta or {}).items():
            if m and m.get("rfq"):
                self.ensure(asset, m["id"])

    def ensure(self, asset, market):
        with self.lock:
            s = self.streams.get(asset)
            if s is None:
                s = RfqStream(market, self.recorder, self.connect, self.stop,
                              trace=getattr(self, "trace", None), asset=asset)
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
