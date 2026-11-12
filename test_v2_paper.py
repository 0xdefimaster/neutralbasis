"""Phase 8: ESTIMATED PAPER end-to-end (storage, simulated execution, ledger, engine,
strategy separation, funding scenarios, observations, stream reader).

All market data here is MOCK (labelled) unless a test says OBSERVED; nothing is traded.
"""
import json
import math
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import wide_paper
from v2 import economics, estimated, funding_scenarios, gates, paper_engine, paper_exec, paper_obs
from v2 import paper_ledger as L
from v2 import paths, rfq_paper, storage

AT = 1_794_456_000.0          # an exact hour (MOCK clock)
H = 3600.0


def ob(mid, half=0.0005, depth=1e9, received=AT, extra_bid=None):
    bids = [(mid * (1 - half), depth)] + (extra_bid or [])
    return dict(bids=bids, asks=[(mid * (1 + half), depth)], model="orderbook",
                empty=False, crossed=False, received=received)


def rfq_record(at, quotes=None):
    import datetime

    iso = datetime.datetime.fromtimestamp(at - 2, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    q = quotes or dict(base=(99.99, 100.01), size_1k=(99.98, 100.02),
                       size_100k=(99.5, 100.5))
    return dict(quotes=q, quotes_updated=iso)


class FakeObs:
    """MOCK observation: rows, books, settled history, closes; legs aligned."""

    def __init__(self, at, rows, books, settled=None, closes=None, rfq=None, skew=0.0):
        self.at, self.rows, self.books = at, rows, books
        self.s, self.cl, self.rfq, self.skew = settled or {}, closes or {}, rfq or {}, skew
        self.origin = "MOCK@%d" % at
        self.times = {k: [at] for k in rows}

    def rows_by(self):
        return dict(self.rows)

    def book(self, v, a):
        return self.books.get((v, a))

    def settled(self, v, a, at):
        return [x for x in self.s.get((v, a), []) if x["ts"] <= min(at, self.at)]

    def closes(self, v, a):
        return self.cl.get((v, a))

    def rth_open(self):
        return None

    def rfq_record(self, v, a):
        return self.rfq.get((v, a))

    def pair_time(self, lv, sv, asset):
        return self.skew, 60.0, "VOL_UNKNOWN"


def rows_for(at, tx_rate, arcus_rate=0.0, tx_fee=0.0004):
    return {("TxFlow", "X"): dict(venue="TxFlow", asset="X", exchange_id="txflow",
                                  market_id="X", rate=tx_rate, fee=tx_fee, mark=100.0,
                                  interval=3600, next_funding=at - at % H + H,
                                  fine_class="crypto", kind="perp"),
            ("Arcus", "X"): dict(venue="Arcus", asset="X", exchange_id="arcus",
                                 market_id="X", rate=arcus_rate, fee=0.0004, mark=100.0,
                                 interval=3600, fine_class="crypto", kind="perp")}


def arcus_history(until, rate=0.0001):
    return [dict(ts=t, rate=rate, kind="settled_public")
            for t in [until - k * H for k in range(48, -1, -1)]]


def obs_at(at, tx_rate, books=None, hist_rate=0.0001):
    rows = rows_for(at, tx_rate)
    b = books or {("TxFlow", "X"): ob(100.0, received=at), ("Arcus", "X"): ob(100.0,
                                                                              received=at)}
    return FakeObs(at, rows, b, settled={("Arcus", "X"): arcus_history(at - at % H,
                                                                       hist_rate)})


CFG = dict(wide_paper.WIDE_DEFAULTS)
PAIR = [("X", "TxFlow", "Arcus")]


class Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="nb-p8-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


# ============================================================== storage
class StorageTests(Tmp):
    @unittest.skipUnless(os.name == "nt", "D:/E: drive letters exist only on Windows")
    def test_paths(self):
        with self.assertRaises(storage.StorageError):
            storage.check_dir(r"D:\x")
        with self.assertRaises(storage.StorageError):
            storage.check_dir(r"E:\NeutralBasis\data-five-rh")
        with self.assertRaises(storage.StorageError):
            storage.check_dir(r"E:\somewhere-else", environ={storage.E_ACK: "1"})
        with self.assertRaises(storage.StorageError):     # failing disk: no ack
            storage.check_dir(r"E:\nb-v2-phase8\run", environ={})
        self.assertTrue(str(storage.check_dir(r"E:\nb-v2-phase8\run",
                                              environ={storage.E_ACK: "1"})).lower()
                        .startswith(r"e:\nb-v2-phase8"))
        with self.assertRaises(storage.StorageError):
            storage.check_dir(r"E:\NeutralBasis\x", environ={storage.E_ACK: "1"})
        self.assertFalse(storage.DEFAULT_ROOT.lower().startswith(("e:", "d:")))
        storage.check_dir(self.dir)                       # off D:/E: is allowed
        with self.assertRaises(storage.StorageError):
            storage.ChainLog(self.dir / "x.sqlite3")

    def test_chain_detects_corruption_and_isolates_a_torn_tail(self):
        log = storage.ChainLog(self.dir / "l.jsonl")
        for i in range(3):
            log.append(dict(i=i))
        recs, rep = storage.read_chain(self.dir / "l.jsonl")
        self.assertEqual(([r["i"] for r in recs], rep["status"]), ([0, 1, 2], "OK"))
        with open(self.dir / "l.jsonl", "a", encoding="utf-8") as f:
            f.write('{"prev": "torn')                     # crash mid-write
        self.assertEqual(storage.read_chain(self.dir / "l.jsonl")[1]["status"], "TORN_TAIL")
        log2 = storage.ChainLog(self.dir / "l.jsonl")     # restart: isolated, appended
        log2.append(dict(i=3))
        recs, rep = storage.read_chain(self.dir / "l.jsonl")
        self.assertEqual(([r["i"] for r in recs], rep["status"], rep["torn_lines"]),
                         ([0, 1, 2, 3], "OK", [3]))
        lines = (self.dir / "l.jsonl").read_text(encoding="utf-8").split("\n")
        lines[1] = lines[1].replace('"i":1', '"i":9')       # tampering
        (self.dir / "l.jsonl").write_text("\n".join(lines), encoding="utf-8")
        recs, rep = storage.read_chain(self.dir / "l.jsonl")
        self.assertEqual((len(recs), rep["status"]), (1, "CORRUPT"))
        with self.assertRaises(storage.StorageError):
            storage.ChainLog(self.dir / "l.jsonl")


# ============================================================== execution models
class ExecTests(unittest.TestCase):
    def test_clob_ioc_partial_inside_the_slippage_limit(self):
        book = dict(bids=[(99.99, 3.0)], asks=[(100.01, 3.0), (100.04, 2.0),
                                                (100.5, 100.0)],
                    model="orderbook", empty=False, crossed=False)
        f = paper_exec.clob_ioc(book, True, 10.0, max_slippage_bp=10.0, fee_rate=0.001)
        self.assertEqual((f["filled"], f["partial"], f["verified"]), (5.0, True, False))
        self.assertAlmostEqual(f["price"], (3 * 100.01 + 2 * 100.04) / 5)
        self.assertEqual(f["label"], paper_exec.CLOB_LABEL)
        none = paper_exec.clob_ioc(dict(book, asks=[(101.0, 9.0)]), True, 1.0, 10.0)
        self.assertEqual((none["filled"], none["reason"]),
                         (0.0, "NO_DEPTH_INSIDE_SLIPPAGE_LIMIT"))

    def test_rfq_exact_tier_last_look_and_firm_gap(self):
        rec = rfq_record(AT)
        f = paper_exec.rfq_fill(rec, True, 1000.0, AT)
        self.assertEqual((f["price"], f["tier"], f["verified"]), (100.02, "size_1k", False))
        self.assertEqual(paper_exec.rfq_fill(rec, True, 2500.0, AT)["reason"],
                         rfq_paper.UNPRICED)
        self.assertEqual(paper_exec.rfq_fill(rec, True, 1000.0, AT, last_look_reject=True)
                         ["filled"], 0.0)
        g = paper_exec.rfq_fill(rec, True, 1000.0, AT, firm_gap_bp=10.0)
        self.assertAlmostEqual(g["price"], 100.02 * 1.001)
        self.assertEqual(paper_exec.rfq_fill(rec, False, 1000.0, AT, qty=5.0)["reason"],
                         rfq_paper.UNPRICED)              # 500 USD is not a tier


# ============================================================== engine lifecycle
class EngineTests(Tmp):
    def engine(self, folder=None, **kw):
        led = L.PaperLedger(folder or self.dir / "led")
        return led, paper_engine.PaperEngine(led, CFG, PAIR, **kw)

    def test_full_lifecycle_and_accounting(self):
        led, eng = self.engine()
        eng.step(obs_at(AT + 60, -0.0005))                 # carry 0.05 %/h: signal
        self.assertEqual(len(led.signals), 1)
        self.assertEqual(led.signals[0]["status"], L.ENTRY_SIGNAL)
        p = list(led.positions.values())[0]
        self.assertEqual(p["state"], L.MONITORING)
        self.assertEqual(p["size"], 10000.0)               # best worst-case net
        self.assertTrue(all(f["verified"] is False for lg in p["legs"].values()
                            for f in lg["fills"]))
        eng.step(obs_at(AT + H + 60, 0.0005))              # carry reversed: weak
        self.assertEqual(p["state"], L.MONITORING)
        eng.step(obs_at(AT + 2 * H + 60, 0.0005))          # 60 min weak: exit
        self.assertEqual((p["state"], p["close_reason"]), (L.CLOSED, "FUNDING_REVERSED"))
        pn = L.PaperLedger.pnl(p)
        qty = p["legs"]["long"]["entry_qty"]
        # spread paid once per fill (inside the prices): 4 fills x 5 bp of 100
        self.assertAlmostEqual(pn["simulated_price_pnl"], -4 * qty * 0.05, places=6)
        self.assertAlmostEqual(pn["simulated_fees"], 4 * qty * 100.0 * 0.0004,
                               delta=qty * 0.01)
        ev = {f["evidence"] for f in p["funding"]}
        self.assertEqual(ev, {"SETTLED_PUBLIC_RATE", "PUBLISHED_RATE_ESTIMATE"})
        lay = pn["layers"]
        self.assertAlmostEqual(lay["public_data_reproducible"] + lay["assumption_dependent"]
                               + lay["account_verified"], pn["realized_total"])
        self.assertEqual(lay["account_verified"], 0.0)          # PAPER: never an account
        self.assertEqual(pn["price_basis"], "SIMULATED_FILLS_ON_OBSERVED_QUOTES")
        self.assertEqual(p["reliability"][-1], "RELIABLE")
        self.assertEqual(led.risk(), [])

    def test_no_opposite_position_on_the_same_venues(self):
        led = L.PaperLedger(self.dir / "rev")
        eng = paper_engine.PaperEngine(led, CFG, PAIR + [("X", "Arcus", "TxFlow")],
                                       strategies=("funding",))
        eng.step(obs_at(AT + 60, -0.0005))
        o = obs_at(AT + 120, 0.0009)              # now the reverse direction pays
        o.s[("TxFlow", "X")] = []
        eng.step(o)
        self.assertEqual(len(led.open_positions()), 1)
        self.assertEqual(led.open_positions()[0]["legs"]["long"]["venue"], "TxFlow")

    def test_partial_fill_retry_then_leg_recovery_never_hides_exposure(self):
        led, eng = self.engine(slippage_bp=5.0, max_retries=2)
        thin = ob(100.0, received=AT + 60)
        thin["bids"] = [(99.95, 30.0), (99.92, 1e9)]       # 8 bp away: outside IOC limit
        o = obs_at(AT + 60, -0.0005, books={("TxFlow", "X"): ob(100.0, received=AT + 60),
                                            ("Arcus", "X"): thin})
        eng.step(o)
        p = list(led.positions.values())[0]
        self.assertEqual(p["state"], L.PARTIAL)
        r = led.risk({"X": 100.0})[0]
        self.assertGreater(r["unhedged_units"], 0)
        self.assertGreater(r["unhedged_usd"], 0)
        thin2 = dict(thin, received=AT + 120)
        eng.step(obs_at(AT + 120, -0.0005, books={("TxFlow", "X"): ob(100.0, received=AT + 120),
                                                  ("Arcus", "X"): thin2}))
        self.assertIn(p["state"], (L.PARTIAL, L.MONITORING, L.RECOVERY_PENDING,
                                   L.CLOSED, L.HEDGED))
        eng.step(obs_at(AT + 180, -0.0005, books={("TxFlow", "X"): ob(100.0, received=AT + 180),
                                                  ("Arcus", "X"): dict(thin, received=AT + 180)}))
        states = [s[1] for s in p["states"]]
        self.assertIn(L.LEG_RECOVERY, states)
        self.assertLessEqual(abs(led.exposure(p)), 1e-6)    # recovered: hedged or flat

    def test_crash_after_first_leg_and_restart(self):
        led, eng = self.engine(crash_at="after_first_leg")
        with self.assertRaises(paper_engine.Crash):
            eng.step(obs_at(AT + 60, -0.0005))
        led2 = L.PaperLedger(self.dir / "led")              # new process, same folder
        p = list(led2.positions.values())[0]
        self.assertEqual((p["state"], p["restarted"]), (L.ATTEMPT, True))
        self.assertNotEqual(led2.exposure(p), 0)             # the open risk is visible
        eng2 = paper_engine.PaperEngine(led2, CFG, PAIR)
        eng2.step(obs_at(AT + 120, -0.0005))
        self.assertEqual(p["state"], L.MONITORING)
        self.assertIn("RESTART_DURING_ATTEMPT", [s[2] for s in p["states"]])
        self.assertAlmostEqual(led2.exposure(p), 0.0, places=9)

    # ---------------------------------------------------------- Phase 9 crash safety
    def crash_then(self, restart_at, books=None):
        led, eng = self.engine(crash_at="after_first_leg")
        with self.assertRaises(paper_engine.Crash):
            eng.step(obs_at(AT + 60, -0.0005))
        led2 = L.PaperLedger(self.dir / "led")
        eng2 = paper_engine.PaperEngine(led2, CFG, PAIR)
        o = obs_at(restart_at, -0.0005, books=books)
        eng2.step(o)
        return led2, list(led2.positions.values())[0]

    def restart_event(self, led):
        recs, _ = storage.read_chain(self.dir / "led" / "ledger.jsonl")
        return [r for r in recs if r["kind"] == "restart"]

    def test_long_downtime_reduces_risk_instead_of_hedging_late(self):
        # Phase 8 replay: restarted 5.2 h later and hedged at a different price
        # (-6.75 $). Now: past the 60 s recovery timeout the open leg is unwound.
        led, p = self.crash_then(AT + 60 + 5.2 * H)
        states = [s[1] for s in p["states"]]
        self.assertIn(L.LEG_RECOVERY, states)
        self.assertEqual((p["state"], p["close_reason"]), (L.CLOSED, "LEG_RECOVERY_FLAT"))
        self.assertEqual(p["legs"]["short"]["entry_qty"], 0.0)   # never hedged late
        ev = self.restart_event(led)[0]
        self.assertAlmostEqual(ev["positions"][0]["unhedged_seconds"], 5.2 * H, places=0)
        self.assertEqual(ev["positions"][0]["reconcile"], "OK")
        secs, since = L.PaperLedger.unhedged(p, AT + 10 * H)
        self.assertAlmostEqual(secs, 5.2 * H, places=0)          # spell closed by unwind
        self.assertIsNone(since)

    def test_restart_without_data_keeps_the_risk_visible(self):
        empty = {("TxFlow", "X"): None, ("Arcus", "X"): None}
        led, p = self.crash_then(AT + 60 + 2 * H, books=empty)
        self.assertEqual(p["state"], L.RECOVERY_PENDING)
        r = led.risk({"X": 100.0}, at=AT + 60 + 3 * H)[0]
        self.assertGreater(abs(r["unhedged_units"]), 0)
        self.assertAlmostEqual(r["unhedged_seconds"], 3 * H, places=0)
        self.assertEqual(r["unhedged_since"], AT + 60)

    def test_reconcile_mismatch_is_never_trusted(self):
        led, eng = self.engine()
        eng.step(obs_at(AT + 60, -0.0005))
        p = list(led.positions.values())[0]
        p["legs"]["short"]["qty"] *= 0.5                          # state corrupted
        self.assertTrue(L.PaperLedger.reconcile(p))
        p["restarted"] = True
        eng2 = paper_engine.PaperEngine(led, CFG, PAIR)
        eng2.step(obs_at(AT + 120, -0.0005))
        self.assertIn("RECONCILE_MISMATCH", [s[2] for s in p["states"]])

    def test_heartbeat_every_step(self):
        led, eng = self.engine()
        for k in range(3):
            eng.step(obs_at(AT + 60 + 20 * k, -0.0005))
        recs, _ = storage.read_chain(self.dir / "led" / "ledger.jsonl")
        self.assertEqual(sum(1 for r in recs if r["kind"] == "heartbeat"), 3)
        self.assertEqual(led.last_heartbeat, AT + 100)

    # ---------------------------------------------------------- Phase 9 entry gate
    def test_alternative_worst_case_gate_is_opt_in(self):
        o = obs_at(AT + 60, -0.00032)        # combined_moderate >= 0, half-filled leg < 0
        led, eng = self.engine()
        eng.step(o)
        ev = [e for e in eng.evaluations if e["strategy"] == "funding"][0]
        self.assertEqual(ev["entry_gate"], "STRICT_THRESHOLDS")
        if not ev["alt_gate_blocks"]:
            self.skipTest("MOCK carry gave a non-negative worst case: %s" % ev["sizes"])
        self.assertEqual(len(led.signals), 1)                      # default: unchanged
        self.assertIsNotNone(ev["gating_stresses"]["informational_worst"])
        self.assertLess(ev["strict_best_net_min"], 0)
        led2 = L.PaperLedger(self.dir / "alt")
        eng2 = paper_engine.PaperEngine(led2, CFG, PAIR, entry_gate="WORST_CASE_NONNEGATIVE")
        eng2.step(o)
        self.assertEqual(led2.signals, [])
        with self.assertRaises(ValueError):
            paper_engine.PaperEngine(led2, CFG, PAIR, entry_gate="LOOSER")

    def test_missing_close_data_never_makes_a_closed_trade(self):
        led, eng = self.engine()
        eng.step(obs_at(AT + 60, -0.0005))
        p = list(led.positions.values())[0]
        eng.step(obs_at(AT + H + 60, 0.0005))
        o = obs_at(AT + 2 * H + 60, 0.0005)
        o.books[("Arcus", "X")] = None                       # the close book is missing
        eng.step(o)
        self.assertEqual(p["state"], L.RECOVERY_PENDING)
        self.assertIsNone(p["closed_at"])
        self.assertTrue(led.risk({"X": 100.0}))              # still reported as open risk
        eng.step(obs_at(AT + 2 * H + 120, 0.0005))
        self.assertEqual(p["state"], L.CLOSED)
        self.assertTrue(p["reliability"][-1].startswith("UNRELIABLE"))

    def test_misaligned_data_blocks_the_signal(self):
        led, eng = self.engine()
        o = obs_at(AT + 60, -0.0005)
        o.skew = 90.0                                        # > 60 s limit
        eng.step(o)
        self.assertEqual(led.signals, [])
        self.assertTrue(eng.evaluations[0]["blocked_by_time"])

    def test_last_look_on_the_rfq_first_leg_leaves_no_exposure(self):
        rows = rows_for(AT + 60, -0.0005)
        rows[("Var", "X")] = dict(venue="Var", asset="X", exchange_id="variational",
                                  market_id="X-PERP", rate=0.0, fee=0.0, mark=100.0,
                                  interval=3600, fine_class="crypto", kind="perp")
        o = FakeObs(AT + 60, rows, {("TxFlow", "X"): ob(100.0, received=AT + 60),
                                    ("Var", "X"): dict(ob(100.0), model="rfq_indicative")},
                    rfq={("Var", "X"): rfq_record(AT + 60)})
        led = L.PaperLedger(self.dir / "ll")
        eng = paper_engine.PaperEngine(led, CFG, [("X", "TxFlow", "Var")],
                                       strategies=("funding",),
                                       last_look=lambda pos, side, action, n: True)
        eng.step(o)
        p = list(led.positions.values())[0]
        self.assertEqual(p["size"], 1000.0)                  # only the exact 1k tier
        self.assertEqual((p["state"], p["close_reason"]), (L.CLOSED, "ENTRY_NOT_FILLED"))
        self.assertEqual(p["legs"]["long"]["entry_qty"], 0.0)    # hedge never sent
        sizes = {x["size"]: x["first"] for x in eng.evaluations[0]["sizes"]}
        self.assertEqual(sizes[2500.0], rfq_paper.UNPRICED)
        # cooldown: the same pair is not re-signalled seconds after a failed attempt
        o.at += 60
        eng.step(o)
        self.assertEqual(len(led.signals), 1)

    def test_replay_is_deterministic(self):
        heads = []
        for name in ("a", "b"):
            led, eng = self.engine(self.dir / name)
            for k, rate in enumerate((-0.0005, 0.0005, 0.0005)):
                eng.step(obs_at(AT + 60 + k * H, rate))
            heads.append(storage.read_chain(self.dir / name / "ledger.jsonl")[1]["last_hash"])
        self.assertEqual(heads[0], heads[1])


# ============================================================== strategies (A1, A4, A5)
class Prov:
    def __init__(self, rows, books, settled=None, closes=None):
        self.rows, self.books, self.s, self.c = rows, books, settled or {}, closes or {}

    def rows_by(self):
        return dict(self.rows)

    def book(self, v, a):
        return self.books.get(v)

    def settled(self, v, a, at):
        return self.s.get(v)

    def closes(self, v, a):
        return self.c.get(v)

    def rth_open(self):
        return True


def crow(v, xid, rate, fee=0.0004):
    return dict(venue=v, asset="X", exchange_id=xid, rate=rate, fee=fee, mark=100.0,
                interval=3600, fine_class="crypto", market_id="X")


def closes_series(at, base, bump_hours=(), level=0.0):
    out = {}
    for k in range(1, 8 * 24 + 1):
        t = (at - at % H) - k * H
        out[t] = base * (1 + (level if k in bump_hours else 0.0))
    return out


class StrategyTests(unittest.TestCase):
    def test_estimate_runs_the_checks_strict_never_reached(self):
        # A4: strict stops at SETTLEMENT_EVIDENCE_MISSING (TxFlow) and never checks
        # the books; the estimate must still reject the STALE book.
        rows = {("TxFlow", "X"): crow("TxFlow", "txflow", -0.002),
                ("Arcus", "X"): crow("Arcus", "arcus", 0.0)}
        books = {"TxFlow": ob(100.0, received=AT), "Arcus": ob(100.0, received=AT - 600)}
        g = gates.Gate(Prov(rows, books), dict(CFG))
        strict = g.evaluate(dict(asset="X", long="TxFlow", short="Arcus"), AT, "funding")
        self.assertNotIn("BOOK_STALE", [r["code"] for r in strict["reasons"]])
        r = estimated.EstimatedEvaluator(g, dict(CFG)).evaluate(
            dict(asset="X", long="TxFlow", short="Arcus"), AT, "funding")
        self.assertEqual((r["status"], r["first"]), (estimated.REJECTED, "BOOK_STALE"))

    def test_signal_label_is_not_a_position(self):
        self.assertEqual(estimated.ENTERED, "ESTIMATED_ENTRY_SIGNAL")

    def test_funding_constant_rate_assumption_and_scenarios(self):
        rows = {("TxFlow", "X"): dict(crow("TxFlow", "txflow", -0.0005),
                                      next_funding=AT + 1800),
                ("Arcus", "X"): crow("Arcus", "arcus", 0.0)}
        books = {"TxFlow": ob(100.0), "Arcus": ob(100.0)}
        hist = {"Arcus": arcus_history(AT, 0.0002)}
        g = gates.Gate(Prov(rows, books, settled=hist), dict(CFG))
        r = estimated.EstimatedEvaluator(g, dict(CFG)).evaluate(
            dict(asset="X", long="TxFlow", short="Arcus"), AT, "funding")
        fs = r["funding_scenarios"]
        self.assertIn("PUBLISHED_RATE_CONSTANT", fs["assumption"])
        self.assertTrue(r["uncertainty"]["constant_rate_assumed"])
        names = {x["name"]: x for x in fs["scenarios"]}
        self.assertEqual(set(names), {"constant_published", "history_mean_24h",
                                      "history_mean_7d", "regime_shift_half",
                                      "sign_flip_midway", "sign_flip_at_entry",
                                      "payment_schedule"})
        self.assertAlmostEqual(names["sign_flip_at_entry"]["funding"],
                               -names["constant_published"]["funding"])
        self.assertIn("published for long", names["history_mean_24h"]["basis"])
        pays = names["payment_schedule"]["payments"]
        self.assertEqual(pays["long"]["basis"], "VENUE_PUBLISHED_NEXT_FUNDING")
        self.assertEqual(pays["long"]["payments"], 24)       # AT+0.5h ... AT+23.5h
        self.assertEqual(pays["short"]["basis"], "DERIVED_FROM_SETTLED_HISTORY_TIMES")
        self.assertEqual(fs["persistence"]["basis"], "PER_LEG_ONLY")

    def test_hybrid_components_apart_costs_once(self):
        # A1: a hybrid = funding over the convergence hold + convergence capture, ONE
        # round trip. MOCK history: gap mostly 0, some hours at +1 %, now at +1 %.
        at = AT + 600
        bump = set(range(5, 160, 12))
        hours = [(k, (at - at % H) - k * H) for k in range(1, 193)]
        closes = {"A": {t: 100.0 for _, t in hours},
                  "B": {t: 100.0 * math.exp(0.01 if k in bump else
                                            0.001 * (1 if k % 2 else -1))
                        for k, t in hours}}
        rows = {("A", "X"): crow("A", "arcus", -0.0002), ("B", "X"): crow("B", "risex", 0.0002)}
        books = {"A": ob(100.0, received=at), "B": ob(101.0, received=at)}
        cfg = dict(CFG)
        g = gates.Gate(Prov(rows, books, closes=closes), cfg)
        ev = estimated.EstimatedEvaluator(g, cfg)
        c = dict(asset="X", long="A", short="B")
        h = ev.evaluate(c, at, "hybrid")
        self.assertIsNotNone(h.get("components"), h)
        comp = h["components"]
        # costs counted once: exactly one round trip (4 taker fills) on the two books
        one, _ = economics.round_trip(books["A"], books["B"], 1000.0, 0.0004, 0.0004)
        self.assertAlmostEqual(comp["shared_costs"]["fees"], one["fees"])
        self.assertAlmostEqual(comp["shared_costs"]["spread"], one["spread"])
        self.assertAlmostEqual(h["net"], comp["funding"] + comp["convergence"]
                               - comp["shared_costs"]["fees"]
                               - comp["shared_costs"]["spread"] - comp["reserve"])
        # funding over the convergence hold, from the published carry, counted once
        self.assertAlmostEqual(comp["funding"], 1000.0 * 0.0004 * h["expected_hold_h"])
        self.assertGreater(comp["convergence"], 0)

    def test_hybrid_without_convergence_is_not_a_hybrid(self):
        rows = {("TxFlow", "X"): crow("TxFlow", "txflow", -0.002),
                ("Arcus", "X"): crow("Arcus", "arcus", 0.0)}
        books = {"TxFlow": ob(100.0), "Arcus": ob(100.0)}
        g = gates.Gate(Prov(rows, books), dict(CFG))
        ev = estimated.EstimatedEvaluator(g, dict(CFG))
        h = ev.evaluate(dict(asset="X", long="TxFlow", short="Arcus"), AT, "hybrid")
        f = ev.evaluate(dict(asset="X", long="TxFlow", short="Arcus"), AT, "funding")
        self.assertEqual(h["first"], "HYBRID_CONVERGENCE_UNAVAILABLE")
        self.assertEqual(f["status"], estimated.ENTERED)     # the funding strategy covers it

    def test_scenario_runs_the_static_checks_behind_config_excluded(self):
        rows = {("A", "X"): crow("A", "arcus", -0.002, fee=None),
                ("H", "X"): crow("H", "hyperliquid_native", 0.0)}
        g = gates.Gate(Prov(rows, {"A": ob(100.0), "H": ob(100.0)}), dict(CFG))
        r = estimated.EstimatedEvaluator(g, dict(CFG)).scenario(
            dict(asset="X", long="A", short="H"), AT, "funding")
        self.assertEqual((r["status"], r["first"]), ("SCENARIO_REJECTED", "FEE_UNKNOWN"))


class FundingScenarioTests(unittest.TestCase):
    def test_payment_counts(self):
        leg = dict(rate=0.001, interval=14400, next_funding=AT + 3600, history=[])
        self.assertEqual(funding_scenarios.payments_in(leg, AT, 24), (6, "VENUE_PUBLISHED_NEXT_FUNDING"))
        self.assertEqual(funding_scenarios.payments_in(dict(leg, next_funding=None), AT, 24),
                         (6, "UNKNOWN_PHASE_WORST_CASE"))
        self.assertEqual(funding_scenarios.payments_in(dict(leg, next_funding=AT + 30 * H),
                                                       AT, 24)[0], 0)


# ============================================================== observations / stream
class ObservationTests(unittest.TestCase):
    def answers(self, t):
        book = {"coin": "x", "time": int((t - 3) * 1000),
                "levels": [[{"px": "3.6", "sz": "100", "n": 1}],
                           [{"px": "3.61", "sz": "100", "n": 1}]]}
        return {"xyz_book_NATGAS": dict(ok=True, t0=t - 1, t1=t, data=book),
                "native_book_INIT": dict(ok=True, t0=t - 1, t1=t + 2, data=book)}

    def test_source_times_and_no_lookahead(self):
        o = paper_obs.Observation(self.answers(AT), None,
                                  closes={("tradeXYZ", "NATGAS"): {AT - H: 1.0, AT + H: 2.0}},
                                  histories={"xyz_fh_NATGAS": [[dict(ts=AT - H, rate=1e-4),
                                                                dict(ts=AT + H, rate=2e-4)]]})
        self.assertEqual(o.at, AT + 2)                       # the LAST receive time
        self.assertEqual(paper_obs.source_time("xyz_book_NATGAS",
                                               self.answers(AT)["xyz_book_NATGAS"]["data"]),
                         AT - 3)
        self.assertEqual([x["ts"] for x in o.settled("tradeXYZ", "NATGAS", AT + 10 * H)],
                         [AT - H])                           # nothing after the observation
        self.assertEqual(o.closes("tradeXYZ", "NATGAS"), {AT - H: 1.0})

    def test_integrity_flags_repeats_backwards_gaps_and_stale_books(self):
        def mk(t, src):
            a = self.answers(t)
            a["xyz_book_NATGAS"]["data"] = dict(a["xyz_book_NATGAS"]["data"],
                                                time=int(src * 1000))
            o = paper_obs.Observation(a, None)
            o.times = {("tradeXYZ", "NATGAS"): [src]}
            o.books = {("tradeXYZ", "NATGAS"): dict(ob(3.6), received=src)}
            o.rows = {("tradeXYZ", "NATGAS"): dict(exchange_id="tradexyz")}
            return o

        obs = [mk(AT, AT - 1), mk(AT + 20, AT - 1), mk(AT + 40, AT - 5), mk(AT + 200, AT + 100)]
        rep = paper_obs.integrity(obs, expected_every=20.0)
        leg = rep["legs"]["tradeXYZ/NATGAS"]
        self.assertEqual((leg["repeated"], leg["backwards"]), (1, 1))
        self.assertGreaterEqual(leg["stale_books"], 2)       # 21 s and 45 s old > 15 s
        self.assertEqual(len(rep["polling_gaps"]), 1)
        self.assertIn("UNKNOWN", rep["sequence_numbers"])

    def test_parallel_cycle_keeps_each_answer_time_and_marks_cached_quotes(self):
        import threading
        import time as _t

        from tools import v2_stream

        class Base:
            seen = []

            def http_json(self, xid, method, url, body=None, headers=None, **kw):
                Base.seen.append((xid, kw.get("tries")))
                _t.sleep(0.2)
                if xid == "variational":
                    return {"listings": [{"ticker": "INIT-PERP",
                                          "quotes": {"updated_at": "2026-11-12T12:00:00Z"}}]}
                return {"time": 1000, "levels": [[], []]}

        r = v2_stream.Reader(Base())
        t0 = _t.time()
        A = v2_stream.cycle(r, None, parallel=True)
        took = _t.time() - t0
        self.assertLess(took, 0.2 * len(A) * 0.6)            # not one after another
        self.assertTrue(all(a["t1"] >= a["t0"] and a["ok"] for a in A.values()))
        self.assertIn(("variational", 1), Base.seen)         # one try: no 429 hammering
        self.assertEqual(A["variational_stats"]["quotes_unchanged"], [])
        A2 = v2_stream.cycle(r, None, parallel=False)
        self.assertEqual(A2["variational_stats"]["quotes_unchanged"], ["INIT-PERP"])

    def test_stream_reader_backs_off_a_failing_venue(self):
        from tools import v2_stream

        class Base:
            calls = 0

            def http_json(self, *a, **k):
                Base.calls += 1
                raise OSError("MOCK network down")

        now = [AT]
        r = v2_stream.Reader(Base(), clock=lambda: now[0])
        A = {}
        for _ in range(4):
            r.get(A, "xyz_ctx", "tradexyz", "POST", "u")
        self.assertEqual((Base.calls, r.stats["skipped"]), (2, 2))
        self.assertTrue(A["xyz_ctx"]["skipped"])
        now[0] += 500
        r.get(A, "xyz_ctx", "tradexyz", "POST", "u")
        self.assertEqual(Base.calls, 3)                      # retried after the back-off
        self.assertEqual(v2_stream.source_time("txflow_ctx_NATGAS",
                                               {"data": {"blockTime": 5000}}),
                         (5.0, "ctx.blockTime"))
        self.assertEqual(v2_stream.source_time("xyz_ctx", {})[1], "SOURCE_TIME_NOT_PUBLISHED")


if __name__ == "__main__":
    unittest.main()


BENCH = paths.bench()
RESCUE = BENCH / "v2-phase8-rescue-2026-11-12"


@unittest.skipUnless((RESCUE / "stream-2026-11-12-1440" / "stream.jsonl").exists() and
                     (BENCH / "v2-aligned-2026-11-12" / "aligned.json.gz").exists(),
                     "OBSERVED Phase 8 data not on this machine")
class VerifiedTradeTests(unittest.TestCase):
    """OBSERVED data: the Phase 8 NATGAS TxFlow/tradeXYZ trade, recomputed line by line
    from the raw public answers, must equal the ledger (Phase 9 item 3)."""

    def test_natgas_trade_recomputes_exactly(self):
        from tools import v2_verify_trade as vt

        r = vt.verify(RESCUE / "replay-2026-11-12-final3" / "estimated" / "ledger.jsonl",
                      "ep-012fa0060889eaf2",
                      [BENCH / "v2-aligned-2026-11-12"] +
                      [BENCH / "v2-bursts-2026-11-12" / ("burst-%d" % i) for i in (1, 2, 3)],
                      RESCUE / "stream-2026-11-12-1440" / "stream.jsonl")
        self.assertEqual(r["checks"], dict(fills=True, funding=True, net=True))
        self.assertAlmostEqual(r["net"]["recomputed"], -0.4214, places=4)
        lo, hi = r["gap"]["carried_funding"]["net_band"]
        self.assertLess(lo, -3.0)                 # the gap could have cost ~2.7 $ more
        self.assertAlmostEqual(hi, -0.4214, places=4)
