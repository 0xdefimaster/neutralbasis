import base64, hashlib, json, os, socket, tempfile, threading, unittest
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


if __name__ == "__main__":
    unittest.main()
