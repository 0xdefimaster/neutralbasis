"""Market-data integrity for the V2 venues: sequenced books, staleness, backoff,
per-venue polling workers and the TxFlow depth stream.

PAPER / public reads. Which transport each venue uses, and why (docs read 2026-11-12):

  txflow       WebSocket `l2BookDepth`: snapshot then deltas with `seq` / `prevSeq`
               (docs.txflow.com/api/websocket/l2BookDepth); ping every 10 s, the gateway
               closes after ~16 s idle. A delta whose prevSeq is not the last seq is a
               gap -> resubscribe for a fresh snapshot.
  risex        REST polling. The WS book carries a CRC32 checksum "on wei-scaled
               integers" whose exact string format is not documented -> not used.
  arcus        REST polling (`l2OrderBook`, lastSequenceId must not go back). The WS
               update message format was not read -> not used.
  tradexyz     REST polling (`l2Book`, full snapshots). The Hyperliquid WS page was not
               read in this project -> not used.
  lighter_rh   REST polling within 60 requests/min (Standard). WS format not read.
  variational  one shared `/metadata/v2/stats` answer (RFQ quotes, not a book).

Every book carries `received`; a book older than its venue's limit is stale and never
priced (the gate refuses BOOK_STALE).
"""
import json
import random
import threading
import time

from v2.venues import base

MAX_AGE = dict(txflow=10.0, risex=15.0, arcus=15.0, tradexyz=15.0, lighter_rh=30.0,
               variational=60.0)


class Backoff:
    """Exponential reconnect / retry delay with jitter, capped; reset on success."""

    def __init__(self, base_s=1.0, cap=60.0, rand=random.random):
        self.base, self.cap, self.rand = base_s, cap, rand
        self.failures = 0

    def next(self):
        self.failures += 1
        d = min(self.cap, self.base * 2 ** (self.failures - 1))
        return d * (0.5 + self.rand() / 2)

    def reset(self):
        self.failures = 0


def stale(book, venue, now):
    if not book or book.get("received") is None:
        return True
    return now - book["received"] > MAX_AGE.get(venue, 15.0)


class SeqBook:
    """Snapshot + delta book with seq / prevSeq continuity. `apply` returns "ok",
    "resync" (gap, crossed book or delta before snapshot) or "ignored" (old frame)."""

    def __init__(self):
        self.bids, self.asks = {}, {}
        self.seq = None
        self.ts = None
        self.valid = False
        self.gaps = 0

    def apply(self, msg):
        kind, seq, prev = msg.get("type"), msg.get("seq"), msg.get("prevSeq")
        levels = msg.get("levels") or [[], []]
        if kind == "snapshot":
            self.bids = {float(x["px"]): float(x["sz"]) for x in levels[0]}
            self.asks = {float(x["px"]): float(x["sz"]) for x in levels[1]}
            self.seq, self.ts, self.valid = seq, msg.get("time"), True
            return self.check()
        if not self.valid:
            return "resync"
        if seq is not None and self.seq is not None and seq <= self.seq:
            return "ignored"
        if prev != self.seq:
            self.valid = False
            self.gaps += 1
            return "resync"
        for side, book in ((0, self.bids), (1, self.asks)):
            for x in levels[side]:
                px, sz = float(x["px"]), float(x["sz"])
                if sz == 0:
                    book.pop(px, None)
                else:
                    book[px] = sz
        self.seq, self.ts = seq, msg.get("time")
        return self.check()

    def check(self):
        if self.bids and self.asks and max(self.bids) >= min(self.asks):
            self.valid = False
            return "resync"
        return "ok"

    def book(self, received):
        b = base.book(list(self.bids.items()), list(self.asks.items()),
                      ts=self.ts / 1000 if self.ts else None, seq=self.seq, source="txflow:ws")
        b.update(received=received, model="orderbook", audit_id=None)
        return b


# -- TxFlow l2BookDepth (docs.txflow.com/api/websocket, read 2026-11-12) -------------
TXFLOW_WS = "wss://rpc.txflow.com/ws"


def txflow_subscribe(coin):
    # On /ws the top-level "type" is required as well as inside "subscription".
    return json.dumps({"method": "subscribe", "type": "l2BookDepth",
                       "subscription": {"type": "l2BookDepth", "coin": str(coin)}})


def txflow_unsubscribe(coin):
    return json.dumps({"method": "unsubscribe", "type": "l2BookDepth",
                       "subscription": {"type": "l2BookDepth", "coin": str(coin)}})


TXFLOW_PING = json.dumps({"method": "ping"})


class TxFlowDepthStream:
    """One connection, several coins. `connect(url)` returns an object with
    send(opcode, bytes), recv() -> str and close() (extended_rfq.WebSocket in use)."""

    def __init__(self, coins, connect, clock=time.time, sleep=time.sleep, backoff=None,
                 sink=None):
        self.sink = sink  # recorder hook for every frame and its integrity result
        self.coins = [str(c) for c in coins]
        self.connect, self.clock, self.sleep = connect, clock, sleep
        self.backoff = backoff or Backoff()
        self.books = {c: SeqBook() for c in self.coins}
        self.received = {}
        self.conn = None
        self.last_ping = 0.0
        self.stats = dict(connects=0, resyncs=0, errors=0, frames=0)
        self.lock = threading.Lock()

    def open(self):
        self.conn = self.connect(TXFLOW_WS)
        self.stats["connects"] += 1
        for c in self.coins:
            self.conn.send(1, txflow_subscribe(c).encode())
        self.last_ping = self.clock()

    def resync(self, coin):
        self.stats["resyncs"] += 1
        self.conn.send(1, txflow_unsubscribe(coin).encode())
        self.conn.send(1, txflow_subscribe(coin).encode())

    def handle(self, text):
        msg = json.loads(text)
        data = msg.get("data") or msg
        coin = str(data.get("coin", ""))
        if coin not in self.books:
            return None
        self.stats["frames"] += 1
        with self.lock:
            result = self.books[coin].apply(data)
            if result == "ok":
                self.received[coin] = self.clock()
        if self.sink:
            self.sink(kind="l2_snapshot" if data.get("type") == "snapshot" else "l2_delta",
                      exchange_id="txflow", market_id=coin, seq=data.get("seq"),
                      prev_seq=data.get("prevSeq"), source_ts=data.get("time"),
                      integrity=result, payload=data, received=self.clock())
        if result == "resync":
            self.resync(coin)
        return result

    def step(self):
        """One read (or reconnect). Never raises; a failure closes and backs off."""
        try:
            if self.conn is None:
                self.open()
            if self.clock() - self.last_ping >= 10:
                self.conn.send(1, TXFLOW_PING.encode())
                self.last_ping = self.clock()
            r = self.handle(self.conn.recv())
            self.backoff.reset()
            return r
        except Exception:
            self.stats["errors"] += 1
            try:
                if self.conn is not None:
                    self.conn.close()
            except Exception:
                pass
            self.conn = None
            with self.lock:
                for b in self.books.values():
                    b.valid = False
            self.sleep(self.backoff.next())
            return "error"

    def book(self, coin):
        with self.lock:
            b = self.books.get(str(coin))
            if not b or not b.valid:
                return None
            return b.book(self.received.get(str(coin)))


class PollWorker:
    """Polls the books of one venue's wanted markets through `fetch(venue, asset)`
    (the collector, i.e. the venue's shared request budget), with backoff on errors;
    `health()` reports what is fresh, stale and failing."""

    def __init__(self, venue, exchange_id, wanted, fetch, clock=time.time, sleep=time.sleep,
                 backoff=None):
        self.venue, self.xid = venue, exchange_id
        self.wanted, self.fetch = wanted, fetch
        self.clock, self.sleep = clock, sleep
        self.backoff = backoff or Backoff()
        self.books, self.errors = {}, {}
        self.stats = dict(polls=0, failures=0)

    def poll_once(self):
        keys = sorted(a for v, a in self.wanted() if v == self.venue)
        for a in keys:
            try:
                b = self.fetch(self.venue, a)
                self.books[a] = b
                self.errors.pop(a, None)
                self.stats["polls"] += 1
                self.backoff.reset()
            except Exception as ex:
                self.errors[a] = str(ex)[:200]
                self.stats["failures"] += 1
                self.sleep(self.backoff.next())
        for a in list(self.books):
            if a not in keys:
                self.books.pop(a)  # no longer wanted: dropped (no subscription kept)
        return len(keys)

    def health(self):
        now = self.clock()
        fresh = sum(1 for b in self.books.values() if not stale(b, self.xid, now))
        return dict(venue=self.venue, markets=len(self.books), fresh=fresh,
                    stale=len(self.books) - fresh, failing=len(self.errors),
                    **self.stats)
