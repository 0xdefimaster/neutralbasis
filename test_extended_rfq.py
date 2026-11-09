import base64, hashlib, json, os, socket, tempfile, threading, time, unittest
from unittest import mock
import bot
import extended_rfq
from extended_rfq import RfqBook, RfqStream, RfqRecorder, WebSocket, ExtendedRfq

T0 = 1_000 * 86400


def msg(kind, seq, bids=(), asks=(), market="BTW-USD", ts=None):
    return dict(
        type=kind,
        seq=seq,
        ts=int((ts if ts is not None else T0) * 1000),
        data=dict(
            m=market,
            b=[dict(p=str(p), q=str(q)) for p, q in bids],
            a=[dict(p=str(p), q=str(q)) for p, q in asks],
        ),
    )


class RfqBookTests(unittest.TestCase):
    def test_snapshot_then_delta_changes_levels(self):
        b = RfqBook("BTW-USD")
        b.apply(msg("SNAPSHOT", 1, [(10, 5), (9, 2)], [(11, 3)], ts=T0), T0 + 0.2)
        b.apply(msg("DELTA", 2, [(10, -5), (9.5, 1)], [(11, 2)], ts=T0 + 1), T0 + 1.1)
        bids, asks = b.levels()
        self.assertEqual([(l["price"], l["qty"]) for l in bids], [(9.5, 1), (9, 2)])
        self.assertEqual([(l["price"], l["qty"]) for l in asks], [(11, 5)])
        # Each level keeps exchange time, receive time and sequence of its last change.
        self.assertEqual(
            (asks[0]["exchange_ts"], asks[0]["received"], asks[0]["seq"]),
            (T0 + 1, T0 + 1.1, 2),
        )
        self.assertEqual((bids[1]["exchange_ts"], bids[1]["seq"]), (T0, 1))

    def test_absolute_size_is_used_when_sent(self):
        b = RfqBook("BTW-USD")
        b.apply(msg("SNAPSHOT", 1, [(10, 5)], [(11, 3)]), T0)
        m = msg("DELTA", 2, [(10, -1)])
        m["data"]["b"][0]["c"] = "4"
        b.apply(m, T0)
        self.assertEqual(b.bids[10]["qty"], 4)

    def test_delta_before_snapshot_is_ignored(self):
        b = RfqBook("BTW-USD")
        self.assertFalse(b.apply(msg("DELTA", 5, [(10, 1)]), T0))
        self.assertFalse(b.ready)
        self.assertEqual(b.bids, {})

    def test_sequence_gap_drops_the_book(self):
        b = RfqBook("BTW-USD")
        b.apply(msg("SNAPSHOT", 1, [(10, 5)], [(11, 3)]), T0)
        with self.assertRaises(ConnectionError):
            b.apply(msg("DELTA", 3, [(10, 1)]), T0)
        self.assertFalse(b.ready)
        self.assertEqual((b.bids, b.asks, b.gaps), ({}, {}, 1))
        # Later deltas are not applied; only a snapshot restores the book.
        self.assertFalse(b.apply(msg("DELTA", 4, [(10, 1)]), T0))
        b.apply(msg("SNAPSHOT", 10, [(10, 2)], [(11, 1)]), T0)
        self.assertTrue(b.ready)

    def test_other_market_is_refused(self):
        b = RfqBook("BTW-USD")
        with self.assertRaises(ValueError):
            b.apply(msg("SNAPSHOT", 1, [(10, 5)], market="BTC-USD"), T0)


class FakeSocket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.closed = False

    def recv(self):
        if not self.frames:
            raise ConnectionError("koptu")
        x = self.frames.pop(0)
        if isinstance(x, Exception):
            raise x
        return json.dumps(x)

    def close(self):
        self.closed = True


class RfqStreamTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = bot.Store(os.path.join(self.dir.name, "r.sqlite3"))
        self.clock = T0
        p = mock.patch.object(extended_rfq, "now", lambda: self.clock)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.store.db.close()
        self.dir.cleanup()

    def test_disconnect_stops_serving_the_old_book(self):
        sock = FakeSocket([msg("SNAPSHOT", 1, [(10, 5)], [(11, 3)])])
        s = RfqStream("BTW-USD", RfqRecorder(self.store), lambda url: sock)
        s.connected = True
        s.handle(sock.recv(), T0)
        v = s.view(T0)
        self.assertEqual(
            (v["source"], v["asks"], v["seq"]), ("extended_rfq", [(11, 3)], 1)
        )
        with self.assertRaises(ConnectionError):
            s.run_once()
        s.lost("koptu")
        with self.assertRaises(ValueError) as e:
            s.view(T0)
        self.assertIn("bağlı değil", str(e.exception))
        self.assertTrue(sock.closed)
        # Reconnected but no snapshot yet: still refused.
        s.connected = True
        s.handle(json.dumps(msg("DELTA", 2, [(10, 1)])), T0)
        with self.assertRaises(ValueError):
            s.view(T0)
        s.handle(json.dumps(msg("SNAPSHOT", 7, [(10, 1)], [(11, 1)])), T0)
        self.assertEqual(s.view(T0)["asks"], [(11, 1)])

    def test_silent_socket_is_not_fresh(self):
        s = RfqStream("BTW-USD")
        s.connected = True
        s.handle(json.dumps(msg("SNAPSHOT", 1, [(10, 5)], [(11, 3)])), T0)
        with self.assertRaises(ValueError):
            s.view(T0 + extended_rfq.VIEW_MAX_AGE + 1)

    def test_run_reconnects_and_records(self):
        stop = threading.Event()
        rounds = []

        def connect(url):
            self.assertTrue(url.endswith("/orderbooks/rfq/BTW-USD"))
            rounds.append(url)
            if len(rounds) == 2:
                stop.set()
            return FakeSocket(
                [msg("SNAPSHOT", 1, [(10, 5)], [(11, 3)]), msg("DELTA", 3, [(10, 1)])]
            )

        rec = RfqRecorder(self.store)
        s = RfqStream("BTW-USD", rec, connect, stop)
        with mock.patch.object(extended_rfq, "RECONNECT_MIN", 0):
            s.run()
        self.assertEqual(len(rounds), 2)
        self.assertGreaterEqual(s.book.gaps, 1)
        self.assertFalse(s.book.ready)
        rows = rec.messages("BTW-USD", T0 - 1)
        self.assertEqual([r["seq"] for r in rows][:2], [1, 3])
        self.assertEqual(
            (rows[0]["kind"], rows[0]["exchange_ts"], rows[0]["received"]),
            ("SNAPSHOT", T0, T0),
        )

    def test_manager_starts_only_rfq_markets(self):
        made = []

        class Started(RfqStream):
            def start(self):
                made.append(self.market)

        with mock.patch.object(extended_rfq, "RfqStream", Started):
            m = ExtendedRfq(self.store)
            m.sync(
                dict(
                    BTW=dict(rfq=True, id="BTW-USD"), BTC=dict(rfq=False, id="BTC-USD")
                )
            )
        self.assertEqual(made, ["BTW-USD"])
        with self.assertRaises(ValueError):
            m.book("BTC")
        with self.assertRaises(ValueError):
            m.book("BTW")


class WebSocketTests(unittest.TestCase):
    def test_handshake_ping_and_text_frames(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        got = {}

        def frame(opcode, payload):
            n = len(payload)
            head = bytes([0x80 | opcode])
            head += bytes([n]) if n < 126 else bytes([126]) + n.to_bytes(2, "big")
            return head + payload

        def serve():
            c, _ = srv.accept()
            req = b""
            while b"\r\n\r\n" not in req:
                req += c.recv(4096)
            key = [
                l.split(":", 1)[1].strip()
                for l in req.decode().split("\r\n")
                if l.lower().startswith("sec-websocket-key")
            ][0]
            acc = base64.b64encode(
                hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                ).digest()
            ).decode()
            c.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n"
                ).encode()
            )
            c.sendall(frame(0x9, b"hi"))
            c.sendall(frame(0x1, b'{"a":' + b"1" * 200 + b"}"))
            # Client pong comes back masked.
            h = c.recv(2)
            n = h[1] & 0x7F
            mask = c.recv(4)
            body = bytes(b ^ mask[i % 4] for i, b in enumerate(c.recv(n)))
            got["pong"] = (h[0] & 0x0F, body)
            c.sendall(frame(0x8, b""))
            c.close()

        t = threading.Thread(target=serve)
        t.start()
        ws = WebSocket(f"ws://127.0.0.1:{port}/x", timeout=5)
        self.assertEqual(ws.recv(), '{"a":' + "1" * 200 + "}")
        with self.assertRaises(ConnectionError):
            ws.recv()
        ws.close()
        t.join(5)
        srv.close()
        self.assertEqual(got["pong"], (0xA, b"hi"))


def ws_frame(opcode, payload=b""):
    n = len(payload)
    head = bytes([0x80 | opcode])
    head += bytes([n]) if n < 126 else bytes([126]) + n.to_bytes(2, "big")
    return head + payload


class LocalWsServer:
    """MOCK WebSocket server on 127.0.0.1 (no network). Each accepted connection is
    handshaken, then `script(conn)` runs; client frames and EOF are recorded."""

    def __init__(self, script=None, connections=1):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.srv.settimeout(10)
        self.port = self.srv.getsockname()[1]
        self.script = script or (lambda c: None)
        self.log = []  # one dict per connection
        self.thread = threading.Thread(target=self.serve, args=(connections,), daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/x"

    def serve(self, connections):
        for _ in range(connections):
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            entry = dict(opcodes=[], eof=False)
            self.log.append(entry)
            c.settimeout(10)
            req = b""
            while b"\r\n\r\n" not in req:
                req += c.recv(4096)
            key = [
                l.split(":", 1)[1].strip()
                for l in req.decode().split("\r\n")
                if l.lower().startswith("sec-websocket-key")
            ][0]
            acc = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode()
            c.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n"
                ).encode()
            )
            self.script(c)
            try:
                while True:
                    h = c.recv(2)
                    if len(h) < 2:
                        entry["eof"] = True
                        break
                    entry["opcodes"].append(h[0] & 0x0F)
                    n = h[1] & 0x7F
                    c.recv(4 + n)
            except OSError:
                entry["eof"] = True
            c.close()

    def close(self):
        self.srv.close()
        self.thread.join(10)


class RfqLifecycleTests(unittest.TestCase):
    """Regression for the 2026-11-09 native crash suspect: the TLS socket of an RFQ
    stream must be read, written and closed only by the worker thread that opened it.
    MOCK local sockets; no exchange connection is made."""

    def setUp(self):
        for name, value in (
            ("POLL_SECONDS", 0.05),
            ("RECONNECT_MIN", 0),
            ("STREAM", "ws://127.0.0.1:1/"),
        ):
            p = mock.patch.object(extended_rfq, name, value)
            p.start()
            self.addCleanup(p.stop)

    def stream_on(self, server, **kw):
        s = RfqStream("BTW-USD", **kw)
        s.open = lambda: WebSocket(
            server.url,
            timeout=extended_rfq.SILENT_AFTER,
            poll=extended_rfq.POLL_SECONDS,
            cancelled=s.cancelled,
        )
        return s

    def wait(self, cond, seconds=5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.01)
        return False

    def test_retire_is_a_flag_and_the_worker_closes_its_own_socket(self):
        server = LocalWsServer()  # handshakes, then stays silent
        self.addCleanup(server.close)
        s = self.stream_on(server)
        calls = []
        real = WebSocket.check_owner

        def spy(ws):
            calls.append(threading.get_ident())
            return real(ws)

        with mock.patch.object(WebSocket, "check_owner", spy):
            s.start()
            self.assertTrue(self.wait(lambda: s.connected))
            started = time.monotonic()
            s.retire()  # from the test (engine) thread
            self.assertTrue(s.join(3), "worker did not stop after retire")
            took = time.monotonic() - started
        server.thread.join(5)
        self.assertLess(took, 2)
        # Every socket operation ran in the worker; the close frame came from it.
        self.assertTrue(calls)
        self.assertEqual(set(calls), {s.thread.ident})
        self.assertEqual(server.log[0]["opcodes"], [0x8])
        self.assertTrue(server.log[0]["eof"])
        self.assertFalse(s.connected)

    def test_socket_refuses_use_from_another_thread(self):
        server = LocalWsServer()
        self.addCleanup(server.close)
        ws = WebSocket(server.url, timeout=5, poll=0.05)
        errors = []

        def other():
            for call in (lambda: ws.send(0x9), ws.close, lambda: ws.read(1)):
                try:
                    call()
                except RuntimeError as ex:
                    errors.append(str(ex))

        t = threading.Thread(target=other)
        t.start()
        t.join(5)
        self.assertEqual(len(errors), 3)
        ws.close()  # the owner may close
        server.thread.join(5)
        self.assertEqual(server.log[0]["opcodes"], [0x8])

    def test_silent_server_is_lost_and_the_stream_reconnects(self):
        server = LocalWsServer(connections=2)
        self.addCleanup(server.close)
        with mock.patch.object(extended_rfq, "SILENT_AFTER", 0.3):
            s = self.stream_on(server)
            s.start()
            self.assertTrue(self.wait(lambda: s.reconnects >= 1 and len(server.log) == 2))
            self.assertIn("sessiz", s.error)
            s.retire()
            self.assertTrue(s.join(3))
        server.thread.join(5)
        self.assertTrue(all(c["eof"] for c in server.log))

    def test_engine_stop_ends_the_worker_and_closes_the_socket(self):
        server = LocalWsServer()
        self.addCleanup(server.close)
        stop = threading.Event()
        s = self.stream_on(server, stop=stop)
        s.start()
        self.assertTrue(self.wait(lambda: s.connected))
        stop.set()
        self.assertTrue(s.join(3))
        server.thread.join(5)
        self.assertEqual(server.log[0]["opcodes"], [0x8])

    def test_frames_keep_flowing_until_retire_then_the_book_is_dropped(self):
        def script(c):
            c.sendall(ws_frame(0x1, json.dumps(msg("SNAPSHOT", 1, [(10, 5)], [(11, 3)])).encode()))
            for seq in range(2, 40):
                c.sendall(ws_frame(0x1, json.dumps(msg("DELTA", seq, [(10, 1)])).encode()))

        server = LocalWsServer(script)
        self.addCleanup(server.close)
        s = self.stream_on(server)
        s.start()
        self.assertTrue(self.wait(lambda: s.book.ready))
        s.retire()
        self.assertTrue(s.join(3))
        with self.assertRaises(ValueError):
            s.view()

    def test_retire_during_connect_never_reads_and_closes_in_worker(self):
        release, opened = threading.Event(), threading.Event()
        record = []

        class Fake:
            def recv(self):
                record.append(("recv", threading.get_ident()))
                raise ConnectionError("x")

            def close(self):
                record.append(("close", threading.get_ident()))

        def connect(url):
            opened.set()
            release.wait(5)
            return Fake()

        s = RfqStream("BTW-USD", connect=connect)
        s.start()
        self.assertTrue(opened.wait(5))
        s.retire()
        release.set()
        self.assertTrue(s.join(3))
        self.assertEqual(record, [("close", s.thread.ident)])

    def test_retired_stream_cannot_be_restarted(self):
        s = RfqStream("BTW-USD", connect=lambda url: None)
        s.retire()
        with self.assertRaises(RuntimeError):
            s.start()

    def test_rotation_under_load_keeps_every_socket_in_its_worker(self):
        # Many streams retired and replaced while their workers are reading.
        violations, closes = [], []

        class Fake:
            def __init__(self):
                self.owner = threading.get_ident()
                self.seq = 0

            def guard(self):
                if threading.get_ident() != self.owner:
                    violations.append(threading.get_ident())

            def recv(self):
                self.guard()
                time.sleep(0.002)
                self.seq += 1
                kind = "SNAPSHOT" if self.seq == 1 else "DELTA"
                return json.dumps(msg(kind, self.seq, [(10, 1)], [(11, 1)]))

            def close(self):
                self.guard()
                closes.append(1)

        stop = threading.Event()
        m = ExtendedRfq.__new__(ExtendedRfq)
        m.recorder, m.stop, m.connect = None, stop, (lambda url: Fake())
        m.streams, m.lock = {}, threading.RLock()
        names = [f"A{i}" for i in range(16)]
        for r in range(30):
            chosen = names[r % 4 : r % 4 + 12]
            m.sync({a: dict(rfq=True, id=a + "-USD") for a in chosen})
            time.sleep(0.005)
        streams = list(m.streams.values())
        stop.set()
        for x in streams:
            self.assertTrue(x.join(3))
        self.assertEqual(violations, [])
        self.assertGreater(len(closes), 12)


class ChooseStreamsTests(unittest.TestCase):
    choose = staticmethod(extended_rfq.choose_streams)

    def test_young_stream_keeps_its_slot_when_its_rank_drops(self):
        ranked = [f"N{i}" for i in range(12)] + ["OLD"]
        got = self.choose(ranked, set(), {"OLD": 1000}, 1000 + 60, limit=12, hold=600)
        self.assertIn("OLD", got)
        self.assertEqual(len(got), 12)

    def test_after_the_hold_the_better_ranked_market_takes_the_slot(self):
        ranked = [f"N{i}" for i in range(12)] + ["OLD"]
        got = self.choose(ranked, set(), {"OLD": 1000}, 1000 + 601, limit=12, hold=600)
        self.assertNotIn("OLD", got)

    def test_a_market_that_is_no_longer_a_candidate_is_dropped_even_if_young(self):
        got = self.choose(["A", "B"], set(), {"GONE": 1000, "A": 1000}, 1001, limit=12)
        self.assertEqual(got, {"A", "B"})

    def test_held_legs_are_always_kept_and_the_cap_is_not_raised(self):
        held = {f"H{i}" for i in range(3)}
        ranked = [f"N{i}" for i in range(30)]
        since = {a: 1000 for a in ranked}
        got = self.choose(ranked, held, since, 1001, limit=12)
        self.assertTrue(held <= got)
        self.assertEqual(len(got), 12)
        many = {f"H{i}" for i in range(15)}
        self.assertEqual(len(self.choose(ranked, many, since, 1001, limit=12)), 15)

    def test_rank_noise_no_longer_retires_books_every_cycle(self):
        # MOCK ranking that reshuffles every cycle, as candidate scores do.
        import random

        rng = random.Random(7)
        pool = [f"A{i}" for i in range(20)]

        def retirements(select):
            since, running, retired = {}, set(), 0
            for cycle in range(60):
                at = cycle * 70.0
                ranked = rng.sample(pool, len(pool))
                chosen = select(ranked, since, at)
                retired += len(running - chosen)
                since = {a: since.get(a, at) for a in chosen}
                running = set(chosen)
            return retired

        old = retirements(lambda ranked, since, at: set(ranked[:12]))
        rng.seed(7)
        new = retirements(lambda ranked, since, at: self.choose(ranked, set(), since, at))
        self.assertLess(new, old / 3)


class RfqRecorderTests(unittest.TestCase):
    def test_subscription_rotation_retires_unused_socket_without_stopping_engine(self):
        # MOCK streams; no network connection is made by this test.
        owner = extended_rfq.ExtendedRfq.__new__(extended_rfq.ExtendedRfq)
        owner.stop = threading.Event()
        owner.lock = threading.RLock()
        dropped, held = mock.Mock(), mock.Mock()
        owner.streams = {"OLD": dropped, "HELD": held}
        owner.ensure = mock.Mock()
        owner.sync({"HELD": {"rfq": True, "id": "HELD-USD"},
                    "NEW": {"rfq": True, "id": "NEW-USD"}})
        dropped.retire.assert_called_once()
        held.retire.assert_not_called()
        self.assertFalse(owner.stop.is_set())
        self.assertNotIn("OLD", owner.streams)
        self.assertEqual(owner.ensure.call_count, 2)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = bot.Store(os.path.join(self.dir.name, "r.sqlite3"))
        self.rec = RfqRecorder(self.store)

    def tearDown(self):
        self.store.db.close()
        self.dir.cleanup()

    def count(self):
        return self.store.db.execute("select count(*) from rfq_messages").fetchone()[0]

    def test_messages_are_written_in_batches_in_order(self):
        self.rec.pruned = T0  # no expiry check in this test
        for seq in range(1, 4):
            self.rec.record("BTW-USD", msg("DELTA", seq), "{}", T0 + seq)
        self.assertEqual(self.count(), 0)  # within the flush interval: nothing written
        self.rec.flushed -= extended_rfq.FLUSH_SECONDS
        self.rec.record("BTW-USD", msg("DELTA", 4), "{}", T0 + 4)
        self.assertEqual(self.count(), 4)
        self.rec.record("BTW-USD", msg("DELTA", 5), "{}", T0 + 5)
        # Reading writes what is pending first.
        rows = self.rec.messages("BTW-USD", T0, T0 + 10)
        self.assertEqual([r["seq"] for r in rows], [1, 2, 3, 4, 5])

    def test_stream_does_not_wait_for_another_streams_write(self):
        self.rec.flushed -= extended_rfq.FLUSH_SECONDS
        with self.rec.flushing:  # another stream is writing
            done = threading.Event()

            def other():
                self.rec.record("BTW-USD", msg("DELTA", 1), "{}", T0)
                done.set()

            threading.Thread(target=other, daemon=True).start()
            self.assertTrue(done.wait(2))
        self.assertEqual(len(self.rec.pending), 1)

    def test_expired_messages_are_deleted_in_id_windows(self):
        rows = [
            ("BTW-USD", i, "DELTA", None, t, "{}")
            for i, t in enumerate([T0 - 5 * 86400] * 7 + [T0 - 60, T0 - 4 * 86400, T0])
        ]
        self.store.db.executemany(
            "insert into rfq_messages(market,seq,kind,exchange_ts,received,raw)"
            " values(?,?,?,?,?,?)",
            rows,
        )
        self.store.db.commit()
        deletes = []
        self.store.db.set_trace_callback(
            lambda q: deletes.append(q) if q.startswith("delete") else None
        )
        with mock.patch.object(extended_rfq, "PRUNE_ROWS", 3):
            self.rec.prune(T0 - extended_rfq.KEEP_SECONDS)
        self.store.db.set_trace_callback(None)
        left = self.store.db.execute(
            "select seq from rfq_messages order by id"
        ).fetchall()
        # Every expired row goes; the newer one between them (seq 7) stays.
        self.assertEqual([s for (s,) in left], [7, 9])
        self.assertEqual(len(deletes), 3)
        self.assertTrue(all("id>=" in q for q in deletes))


if __name__ == "__main__":
    unittest.main()
