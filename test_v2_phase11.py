"""Phase 11: live-data readiness, offline. WebSocket heartbeat and frame-boundary
wake-ups, stale snapshots, outgoing rate limit, apparent latency, Lighter scope, TxFlow
userFundings paging and WS/REST field differences, policy PnL naming and stale-price
valuation, the probe's refusal and MOCK run, data-pack hash verification.

Every frame, page and book here is MOCK; no socket or network is used.
"""
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import test_v2_phase10 as t10
from v2 import observation, paths, policy, ws_readonly
from v2.venues import lighter_rh, txflow


class QuietConn:
    """MOCK socket that has no data: recv_or_tick returns None (one poll each)."""

    def __init__(self, clock):
        self.clock, self.sent = clock, []

    def send(self, op, data):
        self.sent.append(json.loads(data))

    def recv_or_tick(self):
        self.clock[0] += 1.0                      # one 1 s poll elapsed
        return None

    def close(self):
        pass


class HeartbeatTests(unittest.TestCase):
    def test_ping_keeps_going_while_no_data_arrives(self):
        now = [1000.0]
        conn = QuietConn(now)
        ws = ws_readonly.ReadOnlyWS("txflow", [t10.tx_sub("l2BookDepth")],
                                    connect=lambda u: conn, clock=lambda: now[0],
                                    sleep=lambda s: None)
        statuses = [ws.step()["status"] for _ in range(35)]   # ~35 s of silence
        self.assertTrue(all(s == "tick" for s in statuses))
        pings = [f for f in conn.sent if f == {"method": "ping"}]
        self.assertEqual(len(pings), 3)                        # at 10, 20, 30 s
        self.assertEqual(ws.stats["reconnects"], 0)

    def test_wake_up_only_at_a_frame_boundary(self):
        cls = ws_readonly.heartbeat_class()
        frame = json.dumps({"channel": "pong"}).encode()
        raw = bytes([0x81, len(frame)]) + frame                # one unmasked text frame

        class Sock:
            def __init__(self, chunks):
                self.chunks = list(chunks)

            def recv(self, n):
                c = self.chunks.pop(0)
                if c is None:
                    raise socket.timeout()
                return c

        def make(chunks):
            ws = cls.__new__(cls)
            ws.owner, ws.buffer, ws.sock = threading.get_ident(), b"", Sock(chunks)
            ws.last_data, ws.silent_after = time.monotonic(), 30
            ws.cancelled, ws.alive = (lambda: False), None
            return ws

        self.assertIsNone(make([None]).recv_or_tick())         # quiet: a tick
        # a frame split in two with a poll timeout in the middle is NOT cut
        self.assertEqual(make([raw[:5], None, raw[5:]]).recv_or_tick(),
                         frame.decode())
        silent = make([None])
        silent.last_data -= 31
        with self.assertRaises(ConnectionError):
            silent.recv_or_tick()

    def test_ping_intervals_match_the_docs(self):
        self.assertEqual(ws_readonly.VENUES["txflow"]["ping_s"], 10.0)    # closes ~16 s
        self.assertLess(ws_readonly.VENUES["hyperliquid"]["ping_s"], 60.0)
        self.assertLess(ws_readonly.VENUES["lighter_rh"]["ping_s"], 120.0)
        self.assertLess(ws_readonly.POLL_S, ws_readonly.VENUES["txflow"]["ping_s"])

    def test_older_snapshot_never_replaces_a_newer_book(self):
        b = ws_readonly.Books(lambda: 2_000_000.0)
        b.handle("txflow", t10.depth(5, None, "snapshot", [(3.6, 1)], [(3.61, 1)], t=2000))
        r = b.handle("txflow", t10.depth(1, None, "snapshot", [(3.0, 1)], [(3.1, 1)], t=1000))
        self.assertEqual(r["status"], "stale")
        self.assertEqual(b.top("txflow", "119")[:2], (3.6, 3.61))

    def test_outgoing_rate_limit(self):
        now, sleeps = [100.0], []
        conn = QuietConn(now)
        ws = ws_readonly.ReadOnlyWS("hyperliquid", [t10.hl_sub("l2Book", "INIT")],
                                    connect=lambda u: conn, clock=lambda: now[0],
                                    sleep=lambda s: sleeps.append(s))
        ws.open()
        for _ in range(ws.MAX_FRAMES_PER_S + 1):
            ws.send({"method": "ping"})
        self.assertTrue(sleeps)                                 # throttled, not sent faster

    def test_latency_is_apparent_and_negative_values_are_flagged(self):
        b = ws_readonly.Books(lambda: 999.0)                    # local clock BEHIND source
        r = b.handle("txflow", t10.depth(1, None, "snapshot", [(3.6, 1)], [(3.61, 1)],
                                         t=1_000_000))
        self.assertTrue(r["source_ahead_of_local"])
        self.assertIn("APPARENT", r["latency_basis"])
        st = b.latency_stats("txflow")
        self.assertEqual((st["n"], st["negative"]), (1, 1))


class ScopeTests(unittest.TestCase):
    def test_lighter_core_is_not_in_either_list(self):
        self.assertEqual(len(policy.EXCHANGE_IDS), 8)
        self.assertEqual(len(observation.OBSERVATION_UNIVERSE_V3), 11)
        for lst in (policy.EXCHANGE_IDS, observation.OBSERVATION_UNIVERSE_V3):
            self.assertIn("lighter_rh", lst)
            self.assertFalse(any("core" in x.lower() for x in lst))
        self.assertEqual(policy.LEGACY_NAME["lighter_rh"], "Lighter")   # not LighterCore
        self.assertIn("api.rh.lighter.xyz", lighter_rh.API)
        self.assertIn("api.rh.lighter.xyz", ws_readonly.VENUES["lighter_rh"]["url"])
        self.assertNotIn("lighter_core", ws_readonly.VENUES)
        self.assertNotIn("zklighter", json.dumps(ws_readonly.VENUES))


def page(rows, more, total=None):
    return {"success": True, "code": 0, "data": {"data": rows, "hasMore": more,
                                                 "total": total}}


def row(t, usdc="0.1", szi="-1", rate="0.01"):
    return {"time": t, "coin": "119", "usdc": usdc, "szi": szi, "fundingRate": rate,
            "amount": usdc}


class TxFlowPagingTests(unittest.TestCase):
    def test_pages_newest_first_until_has_more_is_false(self):
        pages = [page([row(3000), row(2000)], True), page([row(1000)], False)]
        seen = []

        def fetch(body):
            seen.append(body["offset"])
            return pages[len(seen) - 1]

        r = txflow.fetch_user_fundings(fetch, t10.ADDR, "119", 0, 9999, limit=2)
        self.assertEqual((r["status"], r["pages"], seen), ("COMPLETE", 2, [0, 2]))
        self.assertEqual([x["ts"] for x in r["rows"]], [1.0, 2.0, 3.0])
        self.assertTrue(all(x["status"] == txflow.UNVERIFIED for x in r["rows"]))

    def test_overlap_duplicates_and_truncation(self):
        pages = [page([row(3000), row(2000)], True), page([row(2000), row(2500)], True)]
        r = txflow.fetch_user_fundings(lambda b: pages.pop(0) if pages else page([], False),
                                       t10.ADDR, "119", 0, 9999, limit=2)
        self.assertEqual(r["status"], "INCONSISTENT")
        self.assertEqual(len(r["rows"]), 3)                     # 2000 kept once
        many = lambda b: page([row(10_000 - b["offset"])], True)  # noqa: E731
        r2 = txflow.fetch_user_fundings(many, t10.ADDR, "119", 0, 9999, limit=1,
                                        max_pages=4)
        self.assertEqual((r2["status"], r2["pages"]), ("TRUNCATED", 4))
        self.assertEqual(txflow.user_fundings_request(t10.ADDR, "1", 0, 1, limit=99999)[2][
            "limit"], 99999)                                    # raw request unchanged...
        r3 = txflow.fetch_user_fundings(lambda b: page([], False), t10.ADDR, "1", 0, 1,
                                        limit=99999)
        self.assertEqual(r3["status"], "COMPLETE")              # ...the pager clamps to 2000

    def test_ws_and_rest_fields_differ_and_stay_unverified(self):
        ws_rows = txflow.parse_ws_user_funding({"channel": "userFundings", "data": {
            "time": 1700000000000, "coin": "4", "usdc": "", "szi": "", "fundingRate": ""}})
        r = ws_rows[0]
        self.assertEqual((r["amount"], r["usdc"], r["szi"], r["rate_raw"], r["source"]),
                         (None, None, None, None, "ws"))       # empty is None, never 0
        rest = txflow.parse_user_fundings(page([row(1700000000000)], False))[0]
        self.assertEqual(rest["amount"], rest["usdc"])
        v = txflow.verify_payments(ws_rows + [rest], lambda ts: None)
        self.assertEqual(v["status"], txflow.UNVERIFIED)        # no address, no venue price


class PolicyReportTests(unittest.TestCase):
    def test_names_and_stale_valuation(self):
        from tools import v2_policy_compare as pc

        src = Path(pc.__file__).read_text(encoding="utf-8")
        self.assertIn("entry_stress_estimate", src)
        self.assertNotIn("worst_modelled=", src)                # renamed: not a measurement
        p = {"legs": {"long": {"venue": "A", "exchange_id": "arcus", "qty": 1.0},
                      "short": {"venue": "B", "exchange_id": "arcus", "qty": 1.0}}}

        class O:
            at = 1000.0
            books = {("A", "X"): dict(bids=[(99.0, 1)], asks=[(101.0, 1)], received=999.0,
                                      model="orderbook"),
                     ("B", "X"): dict(bids=[(99.0, 1)], asks=[(101.0, 1)], received=900.0,
                                      model="orderbook")}
        m = pc.mark_now(p, O, "X")
        self.assertEqual((m["long"], m["short"]), (100.0, None))   # 100 s old: not used


class ProbeTests(unittest.TestCase):
    def test_refuses_without_owner_approval_and_switch(self):
        from tools import v2_ws_probe

        for argv, env in ((["x", "--out", "C:\\tmp\\p"], {}),
                          (["x", "--out", "C:\\tmp\\p", "--owner-approved"], {}),
                          (["x", "--out", "C:\\tmp\\p"], {ws_readonly.ENV: "1"})):
            with mock.patch("sys.argv", argv), mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop(ws_readonly.ENV, None) if not env else None
                with self.assertRaises(SystemExit):
                    v2_ws_probe.main()

    def test_mock_run_measures_everything_and_caps_time(self):
        from tools import v2_ws_probe

        frames = [t10.depth(1, None, "snapshot", [(3.6, 1)], [(3.61, 1)], t=1_794_500_000_000),
                  t10.depth(2, 1, "delta", [(3.59, 1)], [], t=1_794_500_001_000),
                  t10.depth(5, 3, "delta", [(3.58, 1)], [], t=1_794_500_002_000)]
        now = [1_794_500_000.5]

        class Conn(t10.FakeConn):
            def recv(self):
                now[0] += 1.0
                return super().recv()

        def http_json(xid, method, url, body=None, headers=None):
            return {"success": True, "data": {"levels": [[{"px": "3.6", "sz": "1"}],
                                                         [{"px": "3.61", "sz": "1"}]]}}

        subs = v2_ws_probe.plan_subscriptions()["txflow"][:1]
        for s in v2_ws_probe.plan_subscriptions(lighter_market=1).values():
            for f in s:
                self.assertIsNone(ws_readonly.check_frame(
                    "lighter_rh" if "channel" in f else
                    "txflow" if "type" in f else "hyperliquid", f))
        r = v2_ws_probe.run_venue("txflow", subs, 5000, connect=lambda u: Conn(frames),
                                  clock=lambda: now[0], http_json=http_json, rest_every=1,
                                  sleep=lambda s: now.__setitem__(0, now[0] + s))
        self.assertEqual(r["seconds"], 300.0)                   # hard cap
        self.assertTrue(r["connected"])
        self.assertGreaterEqual(r["resyncs"], 1)
        self.assertGreaterEqual(r["reconnects"], 1)
        self.assertGreaterEqual(r["with_source_time"], 2)
        self.assertIn("APPARENT", r["apparent_latency"]["basis"])


class PackTests(unittest.TestCase):
    def test_pack_and_verify_detects_tampering(self):
        from tools import v2_pack_data

        d = Path(tempfile.mkdtemp(prefix="nb-p11-"))
        try:
            bench = d / "nb-benchmark"
            (bench / "v2-aligned-2026-11-12").mkdir(parents=True)
            (bench / "v2-aligned-2026-11-12" / "a.json").write_text("MOCK", encoding="utf-8")
            (bench / "v2-aligned-2026-11-12" / "x.sqlite3").write_text("db", encoding="utf-8")
            out = d / "pack.zip"
            with mock.patch.dict(os.environ, {"NB_V2_BENCH_DIR": str(bench)}):
                info = v2_pack_data.pack(out)
            self.assertEqual(info["files"], 1)                  # no database file packed
            self.assertEqual(v2_pack_data.verify(out)["status"], "OK")
            bad = d / "bad.zip"
            with zipfile.ZipFile(out) as zin, zipfile.ZipFile(bad, "w") as zout:
                for n in zin.namelist():
                    b = zin.read(n)
                    zout.writestr(n, b"TAMPERED" if n.endswith("a.json") else b)
            r = v2_pack_data.verify(bad)
            self.assertEqual((r["status"], r["bad"]), ("FAILED",
                                                       ["v2-aligned-2026-11-12/a.json"]))
            self.assertIn("NOT a backup", r["note"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipUnless(paths.bench("neutralbasis-data-pack-2026-11-13.zip").exists(),
                         "data pack not on this machine")
    def test_real_pack_hashes(self):
        from tools import v2_pack_data

        r = v2_pack_data.verify(paths.bench("neutralbasis-data-pack-2026-11-13.zip"))
        self.assertEqual((r["status"], r["files"]), ("OK", 44))


if __name__ == "__main__":
    unittest.main()


class LiveProbeFindingsTests(unittest.TestCase):
    """Regressions from the first approved live probe (2026-11-13 04:31-04:36)."""

    def test_txflow_book_is_keyed_by_instrument_id_not_coin_name(self):
        b = ws_readonly.Books(lambda: 1_794_500_001.0)
        frame = json.dumps({"channel": "l2BookDepth", "data": {
            "coin": "NATGAS-USDC", "id": 119, "time": 1_794_500_000_000, "seq": 1,
            "prevSeq": None, "type": "snapshot",
            "levels": [[{"px": "3.6", "sz": "1", "n": 1}], [{"px": "3.61", "sz": "1", "n": 1}]]}})
        self.assertEqual(b.handle("txflow", frame)["market"], "119")
        self.assertEqual(b.top("txflow", "119")[:2], (3.6, 3.61))
        self.assertEqual(b.aliases["119"], "NATGAS-USDC")

    def test_totals_survive_the_bounded_record_list(self):
        now = [1000.0]
        b = ws_readonly.Books(lambda: now[0])

        def ob(kind, nonce, begin):
            return json.dumps({"type": kind + "/order_book", "channel": "order_book:1",
                               "order_book": {"nonce": nonce, "begin_nonce": begin,
                                              "bids": [{"price": "100", "size": "1"}],
                                              "asks": [{"price": "101", "size": "1"}]}})
        b.handle("lighter_rh", ob("subscribed", 0, None))
        for n in range(1, 6001):
            now[0] += 0.05
            b.handle("lighter_rh", ob("update", n, n - 1))
        self.assertEqual(len(b.records), 5000)
        self.assertEqual(b.total("lighter_rh", kind="snapshot", status="ok"), 1)
        self.assertEqual(b.total("lighter_rh", status="ok"), 6001)
        self.assertEqual(b.per_market[("lighter_rh", "1")]["ok"], 6001)
        self.assertIn(("lighter_rh", "subscribed/order_book"), b.samples)

    def test_rest_side_uses_its_own_source_time(self):
        from tools import v2_ws_probe

        def http_json(xid, method, url, body=None, headers=None):
            return {"coin": "INIT", "time": 1_794_500_000_000,
                    "levels": [[{"px": "1", "sz": "1"}], [{"px": "1.1", "sz": "1"}]]}
        got = v2_ws_probe.rest_top("hyperliquid", "INIT", http_json)
        self.assertEqual((got[1], got[2]), (1_794_500_000.0, True))
