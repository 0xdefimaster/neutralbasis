"""Phase 10: read-only WebSocket allowlist and book handling, TxFlow userFundings contract,
portable data paths, and regression tests for look-ahead, funding sign flips, stale
prices and double counting.

All frames and payment rows here are MOCK (shaped like the official docs' examples);
no socket is opened and no account is read.
"""
import gzip
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import test_v2_paper as tp
from v2 import paper_engine, paths, storage, ws_readonly
from v2 import paper_ledger as L
from v2.venues import txflow

ADDR = "0x" + "ab" * 20
AUTH = {"NB_TXFLOW_FUNDING_ADDRESS": ADDR}


def tx_sub(t, coin="119"):
    return {"method": "subscribe", "type": t, "subscription": {"type": t, "coin": coin}}


def hl_sub(t, coin="xyz:NATGAS"):
    return {"method": "subscribe", "subscription": {"type": t, "coin": coin}}


class AllowlistTests(unittest.TestCase):
    def test_documented_public_frames_pass(self):
        for f in (tx_sub("l2BookDepth"), tx_sub("activeAssetCtx"), {"method": "ping"}):
            self.assertIsNone(ws_readonly.check_frame("txflow", f), f)
        for f in (hl_sub("l2Book"), hl_sub("activeAssetCtx", "INIT"), {"method": "ping"}):
            self.assertIsNone(ws_readonly.check_frame("hyperliquid", f), f)
        for f in ({"type": "subscribe", "channel": "order_book/3"}, {"type": "ping"}):
            self.assertIsNone(ws_readonly.check_frame("lighter_rh", f), f)

    def test_order_and_account_paths_are_refused(self):
        bad = [
            ("hyperliquid", {"method": "post", "id": 1, "request": {"type": "action",
                                                                    "payload": {}}}),
            ("hyperliquid", {"method": "post", "id": 2, "request": {"type": "info",
                                                                    "payload": {}}}),
            ("hyperliquid", {"method": "subscribe", "subscription": {"type": "orderUpdates",
                                                                     "user": ADDR}}),
            ("hyperliquid", hl_sub("l2Book") | {"extra": 1}),
            ("lighter_rh", {"type": "jsonapi/sendtx", "data": {"tx_type": 1, "tx_info": "x"}}),
            ("lighter_rh", {"type": "subscribe", "channel": "account_all/5"}),
            ("lighter_rh", {"type": "subscribe", "channel": "order_book/3", "auth": "t"}),
            ("txflow", {"method": "subscribe", "subscription": {"type": "l2BookDepth",
                                                                "coin": "119"}}),
            ("txflow", tx_sub("orderUpdates")),
            ("txflow", {"method": "subscribe", "type": "userFundings",
                        "subscription": {"type": "userFundings", "user": ADDR}}),
            ("variational", {"method": "ping"}),
            ("lighter_core", {"type": "ping"}),
        ]
        for venue, f in bad:
            self.assertIsNotNone(ws_readonly.check_frame(venue, f, environ={}), (venue, f))
        # userFundings only for the owner-authorized address
        uf = {"method": "subscribe", "type": "userFundings",
              "subscription": {"type": "userFundings", "user": ADDR}}
        self.assertIsNone(ws_readonly.check_frame("txflow", uf, environ=AUTH))
        other = json.loads(json.dumps(uf).replace(ADDR, "0x" + "cd" * 20))
        self.assertIsNotNone(ws_readonly.check_frame("txflow", other, environ=AUTH))

    def test_live_connection_is_off_by_default(self):
        with self.assertRaises(ws_readonly.LiveDisabled):
            ws_readonly.ReadOnlyWS("txflow", [tx_sub("l2BookDepth")], environ={})
        with self.assertRaises(ws_readonly.LiveDisabled):
            ws_readonly.ReadOnlyWS("txflow", [tx_sub("l2BookDepth")], allow_live=True,
                                   environ={})
        with self.assertRaises(ws_readonly.FrameRefused):
            ws_readonly.ReadOnlyWS("hyperliquid", [{"method": "post"}], connect=lambda u: None)


class FakeConn:
    def __init__(self, frames):
        self.frames, self.sent, self.closed = list(frames), [], False

    def send(self, op, data):
        self.sent.append(json.loads(data))

    def recv(self):
        if not self.frames:
            raise ConnectionError("MOCK closed")
        f = self.frames.pop(0)
        if isinstance(f, Exception):
            raise f
        return f

    def close(self):
        self.closed = True


def depth(seq, prev, kind, bids, asks, t=1_794_500_000_000, coin="119"):
    return json.dumps({"channel": "l2BookDepth", "data": {
        "coin": coin, "id": 119, "time": t, "seq": seq, "prevSeq": prev, "type": kind,
        "levels": [[{"px": str(p), "sz": str(s), "n": 1} for p, s in bids],
                   [{"px": str(p), "sz": str(s), "n": 1} for p, s in asks]]}})


class BookTests(unittest.TestCase):
    def client(self, venue, subs, frames, clock=None):
        conns = []

        def connect(url):
            c = FakeConn(frames.pop(0) if frames else [])
            conns.append(c)
            return c

        now = [1_794_500_000.5]
        sleeps = []
        ws = ws_readonly.ReadOnlyWS(venue, subs, connect=connect,
                                    clock=clock or (lambda: now[0]),
                                    sleep=lambda s: sleeps.append(s))
        return ws, conns, now, sleeps

    def test_txflow_snapshot_delta_gap_resync_duplicate(self):
        frames = [[depth(1, None, "snapshot", [(3.6, 10)], [(3.61, 10)]),
                   depth(2, 1, "delta", [(3.6, 0), (3.59, 5)], []),
                   depth(2, 1, "delta", [(3.6, 0), (3.59, 5)], []),       # duplicate
                   depth(5, 3, "delta", [(3.58, 1)], [])]]                 # gap
        ws, conns, now, _ = self.client("txflow", [tx_sub("l2BookDepth")], frames)
        st = [ws.step()["status"] for _ in range(4)]
        self.assertEqual(st, ["ok", "ok", "duplicate", "resync"])
        sent = [f.get("method") for f in conns[0].sent]
        self.assertEqual(sent[-2:], ["unsubscribe", "subscribe"])         # resync
        self.assertIsNone(ws.books.top("txflow", "119"))                  # invalid until
        r = ws.books.records[0]                                           # a new snapshot
        self.assertAlmostEqual(r["latency_s"], 0.5)
        self.assertEqual(r["source_ts"], 1_794_500_000.0)

    def test_hyperliquid_snapshots_replace_and_old_frames_are_not_merged(self):
        def book(t, bid, ask):
            return json.dumps({"channel": "l2Book", "data": {
                "coin": "xyz:NATGAS", "time": t,
                "levels": [[{"px": str(bid), "sz": "1", "n": 1}],
                           [{"px": str(ask), "sz": "1", "n": 1}]]}})
        frames = [[book(2000, 3.60, 3.61), book(1000, 3.50, 3.70), book(2000, 3.60, 3.61),
                   book(3000, 3.62, 3.61)]]
        ws, _, _, _ = self.client("hyperliquid", [hl_sub("l2Book")], frames)
        st = [ws.step()["status"] for _ in range(4)]
        self.assertEqual(st, ["ok", "stale", "duplicate", "resync"])
        self.assertEqual(ws.books.top("hyperliquid", "xyz:NATGAS")[:2], (3.60, 3.61))

    def test_lighter_nonce_continuity(self):
        def ob(kind, nonce, begin, bids, asks):
            return json.dumps({"type": kind + "/order_book", "channel": "order_book:3",
                               "order_book": {"nonce": nonce, "begin_nonce": begin,
                                              "bids": [{"price": str(p), "size": str(s)}
                                                       for p, s in bids],
                                              "asks": [{"price": str(p), "size": str(s)}
                                                       for p, s in asks]}})
        frames = [[ob("update", 5, 4, [(1, 1)], []),                     # delta first
                   ob("subscribed", 10, None, [(100, 1)], [(101, 1)]),
                   ob("update", 11, 10, [(100, 0), (99.5, 2)], []),
                   ob("update", 13, 12, [(99, 1)], [])]]                   # gap
        ws, _, _, _ = self.client("lighter_rh",
                                  [{"type": "subscribe", "channel": "order_book/3"}], frames)
        st = [ws.step()["status"] for _ in range(4)]
        self.assertEqual(st, ["resync", "ok", "ok", "resync"])
        self.assertIsNone(ws.books.records[1]["latency_s"])              # unit NOT STATED

    def test_reconnect_backoff_resubscribe_and_ping(self):
        frames = [[ConnectionError("MOCK drop")],
                  [depth(1, None, "snapshot", [(3.6, 1)], [(3.61, 1)]),
                   depth(2, 1, "delta", [(3.59, 1)], [])]]
        ws, conns, now, sleeps = self.client("txflow", [tx_sub("l2BookDepth")], frames)
        self.assertEqual(ws.step()["status"], "reconnect")
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(ws.step()["status"], "ok")
        now[0] += 11                                                      # ping is due
        self.assertEqual(ws.step()["status"], "ok")
        self.assertEqual(len(conns), 2)
        self.assertEqual(conns[1].sent[0]["type"], "l2BookDepth")         # resubscribed
        self.assertIn({"method": "ping"}, conns[1].sent)

    def test_refused_frame_closes_the_socket(self):
        ws, conns, _, _ = self.client("txflow", [tx_sub("l2BookDepth")], [[]])
        ws.open()
        with self.assertRaises(ws_readonly.FrameRefused):
            ws.send({"method": "post", "type": "order"})
        self.assertTrue(conns[0].closed)
        self.assertEqual(ws.stats["refused"], 1)

    def test_ws_vs_rest(self):
        rest = dict(bids=[(3.600, 1)], asks=[(3.610, 1)])
        self.assertEqual(ws_readonly.compare((3.601, 3.611, 100.0), rest, 100.5)["status"],
                         "CONSISTENT")
        self.assertEqual(ws_readonly.compare((3.70, 3.71, 100.0), rest, 100.5)["status"],
                         "INCONSISTENT")
        self.assertEqual(ws_readonly.compare((3.601, 3.611, 100.0), rest, 110.0)["status"],
                         "NOT_COMPARABLE")


class TxFlowFundingTests(unittest.TestCase):
    DOC_ROW = {"time": 1720000000000, "coin": "BTC", "usdc": "0.12", "szi": "0.5",
               "fundingRate": "0.0001", "amount": "0.12"}       # docs example (MOCK)

    def test_contract_parse_and_status(self):
        rows = txflow.parse_user_fundings({"success": True, "code": 0, "data": {
            "data": [self.DOC_ROW], "hasMore": True, "total": 1337}})
        r = rows[0]
        self.assertEqual((r["ts"], r["coin"], r["usdc"], r["szi"], r["rate_raw"]),
                         (1720000000.0, "BTC", 0.12, 0.5, 0.0001))
        self.assertIsNone(r["rate"])
        self.assertEqual((r["status"], r["scope"]), (txflow.UNVERIFIED, "ACCOUNT"))
        req = txflow.user_fundings_request(ADDR, "119", 0, 1)[2]
        self.assertEqual(set(req) & {"signature", "nonce", "action"}, set())

    def test_unit_is_proven_only_by_payments(self):
        mark = 100.0
        # MOCK: a short (szi -2) receives when the rate is positive; rate in PERCENT form
        rows = [dict(ts=t, coin="119", szi=-2.0, usdc=2 * mark * 0.0005, amount=2 * mark *
                     0.0005, rate_raw=0.05) for t in (1.0, 2.0, 3.0)]
        v = txflow.verify_payments(rows, lambda ts: mark)
        self.assertEqual((v["status"], v["unit"]), (txflow.VERIFIED, "PERCENT"))
        self.assertEqual(txflow.verify_payments(rows[:2], lambda ts: mark)["status"],
                         txflow.UNVERIFIED)                         # too few rows
        self.assertEqual(txflow.verify_payments(rows, lambda ts: None)["status"],
                         txflow.UNVERIFIED)                         # no venue price
        odd = [dict(r, amount=r["usdc"] * 2) for r in rows]
        v2 = txflow.verify_payments(odd, lambda ts: mark)
        self.assertEqual((v2["status"], v2["usdc_amount_mismatch"]), (txflow.UNVERIFIED, 3))

    def test_coin_mapping_and_account_scope(self):
        meta = {"data": {"universe": [
            {"index": 119, "name": "NATGAS-USDC", "baseCurrency": "NATGAS",
             "quoteCurrency": "USDC"},
            {"index": 4, "name": "BTC-USDC", "baseCurrency": "BTC", "quoteCurrency": "USDC"}]}}
        cm = txflow.coin_map(meta)
        for k in ("119", "NATGAS", "NATGAS-USDC"):
            self.assertEqual(txflow.resolve_coin(cm, k)["market_id"], "119")
        self.assertIsNone(txflow.resolve_coin(cm, "ETH"))
        with self.assertRaises(ValueError):
            txflow.market_history_from_account([])


@unittest.skipUnless(paths.pilot_db().exists(), "pilot DB not on this machine")
class PortableDataTests(unittest.TestCase):
    def test_closes_export_equals_the_db(self):
        from tools import v2_export_closes

        d = Path(tempfile.mkdtemp(prefix="nb-p10-"))
        try:
            out = d / "closes.json.gz"
            meta = v2_export_closes.export(paths.pilot_db(), out)
            a = paths.load_closes(environ={"NB_V2_CLOSES_FILE": str(out)})
            b = paths.load_closes(environ={})
            self.assertEqual(a, b)
            self.assertEqual(meta["series"], len(a))
            self.assertLess(out.stat().st_size, 5_000_000)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class RegressionTests(tp.Tmp):
    """Look-ahead, funding sign flips, stale prices, double counting (Phase 10 item 4)."""

    def test_no_lookahead_in_funding_bookings(self):
        led = L.PaperLedger(self.dir / "la")
        eng = paper_engine.PaperEngine(led, tp.CFG, tp.PAIR, strategies=("funding",))
        eng.step(tp.obs_at(tp.AT + 60, -0.0005))
        o = tp.obs_at(tp.AT + 1800, -0.0005)
        # history that already contains FUTURE payments (read later in a real stream)
        o.s[("Arcus", "X")] = tp.arcus_history(tp.AT + 5 * tp.H)
        eng.step(o)
        p = list(led.positions.values())[0]
        self.assertTrue(all(f["ts"] <= tp.AT + 1800 for f in p["funding"]))

    def test_funding_sign_follows_the_rate(self):
        led = L.PaperLedger(self.dir / "sf")
        eng = paper_engine.PaperEngine(led, tp.CFG, tp.PAIR, strategies=("funding",))
        eng.step(tp.obs_at(tp.AT + 60, -0.0005))           # long TxFlow, rate < 0: receives
        eng.step(tp.obs_at(tp.AT + tp.H + 60, 0.0005))     # flip: rate > 0, the long pays
        eng.step(tp.obs_at(tp.AT + 2 * tp.H + 60, 0.0005))
        p = list(led.positions.values())[0]
        long_f = sorted((f["ts"], f["amount"]) for f in p["funding"] if f["side"] == "long")
        self.assertGreater(long_f[0][1], 0)                # paid at the negative rate
        self.assertLess(long_f[-1][1], 0)                  # pays after the flip

    def test_stale_price_gives_no_entry(self):
        led = L.PaperLedger(self.dir / "st")
        eng = paper_engine.PaperEngine(led, tp.CFG, tp.PAIR, strategies=("funding",))
        o = tp.obs_at(tp.AT + 60, -0.0005)
        o.books[("Arcus", "X")] = tp.ob(100.0, received=tp.AT + 60 - 600)   # 10 min old
        eng.step(o)
        self.assertEqual(led.signals, [])
        self.assertIn("BOOK_STALE", [x["first"] for e in eng.evaluations for x in e["sizes"]])

    def test_a_payment_seen_twice_is_booked_once(self):
        led = L.PaperLedger(self.dir / "dc")
        eng = paper_engine.PaperEngine(led, tp.CFG, tp.PAIR, strategies=("funding",))
        eng.step(tp.obs_at(tp.AT + 60, -0.0005))
        for k in range(1, 4):                              # the same hour observed 3 times
            eng.step(tp.obs_at(tp.AT + tp.H + 60 * k, -0.0005))
        p = list(led.positions.values())[0]
        keys = [f["key"] for f in p["funding"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(sum(1 for f in p["funding"] if f["ts"] == tp.AT + tp.H
                             and f["side"] == "short"), 1)


if __name__ == "__main__":
    unittest.main()
