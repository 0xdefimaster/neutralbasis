"""OFFLINE ACCELERATED STRESS for the V2 runtime (MOCK market, simulated clock).

This is NOT a 72-hour live run and must never be reported as one. A synthetic market
(two venues, two assets, a mean-reverting price gap, hourly funding) drives the real
V2 runtime, hybrid desk, recorder, replay check and safety checks through simulated
hours, with injected faults:

  outage      the collector raises for a window (API down)
  rate_limit  the collector raises a 429-like error for a window
  stale       books are served with an old receive time for a window
  delist      one asset disappears from the rows for a window
  disk_full   the recorder's disk guard sees no free space for a window
  crash       at a given hour the runtime, desk and recorder are dropped and rebuilt
              from their files (an abrupt stop and restart)
  wal         the WAL file grows past the stop limit for a window

The report counts rounds, errors caught, exceptions that escaped (must be 0), hybrid
positions by state, ledger reconciliation, open exposure, recorder gaps and the
replay integrity result.

Usage: python -m tools.v2_stress --hours 72 --step 60 --out DIR
"""
import argparse
import collections
import json
import math
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wide_paper  # noqa: E402
from v2 import economics, ops, replay  # noqa: E402
from v2.hybrid_desk import HybridDesk  # noqa: E402
from v2.recorder import Recorder  # noqa: E402
from v2.runtime import Runtime  # noqa: E402

VENUES = ("Arcus", "Lighter")
ASSETS = ("BTC", "ETH")
LABEL = "OFFLINE ACCELERATED STRESS on a MOCK market (simulated clock); not a live run"


class Market:
    """Synthetic market: a base price random walk, a mean-reverting gap between the two
    venues, hourly funding; deterministic for a seed."""

    def __init__(self, t0, seed=7):
        self.rng = random.Random(seed)
        self.t = t0
        self.base = {"BTC": 70000.0, "ETH": 2000.0}
        self.gap = {a: 0.0 for a in ASSETS}
        self.hist = collections.defaultdict(dict)
        self.paid = collections.defaultdict(list)

    def step(self, dt):
        self.t += dt
        for a in ASSETS:
            self.base[a] *= math.exp(self.rng.gauss(0, 0.0004 * math.sqrt(dt / 60)))
            shock = 0.004 if self.rng.random() < 0.01 else 0.0
            self.gap[a] += -0.05 * self.gap[a] + self.rng.gauss(0, 0.0003) + shock
            h = int(self.t // 3600) * 3600
            for v in VENUES:
                self.hist[(v, a)][h] = self.mid(v, a)
            if int(self.t) % 3600 < dt:
                for v in VENUES:
                    self.paid[(v, a)].append(dict(ts=h, rate=0.00001 if v == "Lighter"
                                                  else 0.0))

    def mid(self, v, a):
        return self.base[a] * (math.exp(self.gap[a]) if v == "Lighter" else 1.0)


class World:
    """Collector + provider over the Market with fault windows (hours from start)."""

    def __init__(self, market, faults, start):
        self.m, self.faults, self.start = market, faults, start
        self.reasons = {"arcus": {"ROW": 2}, "lighter_rh": {"ROW": 2}}
        self.rows, self.sink, self.streams = [], None, {}

    def active(self, name):
        h = (self.m.t - self.start) / 3600
        return any(a <= h < b for a, b in self.faults.get(name, []))

    def refresh(self):
        if self.active("outage"):
            raise ConnectionError("MOCK outage")
        if self.active("rate_limit"):
            raise RuntimeError("MOCK HTTP 429")
        assets = [a for a in ASSETS if not (a == "ETH" and self.active("delist"))]
        self.rows = [dict(venue=v, asset=a, kind="perp", mark=self.m.mid(v, a),
                          rate=0.00001 if v == "Lighter" else 0.0,
                          raw_rate=0.00001 if v == "Lighter" else 0.0, interval=3600,
                          next=None, received=self.m.t, fee=0.0004, leverage=None,
                          price_type="mark", exchange_id={"Arcus": "arcus",
                                                          "Lighter": "lighter_rh"}[v],
                          market_id=a)
                     for v in VENUES for a in assets]
        return list(self.rows)

    def wanted(self):
        return {(r["venue"], r["asset"]) for r in self.rows}

    # provider interface
    def rows_by(self):
        return {(r["venue"], r["asset"]): r for r in self.rows}

    def book(self, v, a):
        if self.active("outage"):
            raise ConnectionError("MOCK outage")
        mid = self.m.mid(v, a)
        age = 120 if self.active("stale") else 0
        return dict(bids=[(mid * 0.9999, 50.0)], asks=[(mid * 1.0001, 50.0)],
                    received=self.m.t - age, model="orderbook")

    def settled(self, v, a, at):
        return list(self.m.paid[(v, a)])

    def closes(self, v, a):
        return dict(self.m.hist[(v, a)])

    def rth_open(self):
        return True


class StressGate:
    """MOCK evaluator on the synthetic market: a hybrid passes when the gap is 3 MAD
    above its median and the round trip on the books is covered; costs come from the
    real v2.economics on the synthetic books."""

    def __init__(self, world, cfg):
        self.w, self.cfg = world, cfg

    def evaluate(self, c, at, strategy):
        rows = self.w.rows_by()
        if (c["long"], c["asset"]) not in rows or (c["short"], c["asset"]) not in rows:
            return dict(reasons=[dict(category="DATA", code="NOT_A_V2_ROW", text="")],
                        eligible=False)
        lb, sb = self.w.book(c["long"], c["asset"]), self.w.book(c["short"], c["asset"])
        if at - lb["received"] > 15 or at - sb["received"] > 15:
            return dict(reasons=[dict(category="DATA", code="BOOK_STALE", text="")],
                        eligible=False)
        hl, hs = self.w.closes(c["long"], c["asset"]), self.w.closes(c["short"], c["asset"])
        gaps = [math.log(hs[t] / hl[t]) for t in hl if t in hs]
        if len(gaps) < 24 or strategy != "hybrid":
            return dict(reasons=[dict(category="DATA", code="PRICE_HISTORY_MISSING",
                                      text="")], eligible=False)
        gaps.sort()
        base = gaps[len(gaps) // 2]
        mad = sorted(abs(g - base) for g in gaps)[len(gaps) // 2] * 1.4826 or 1e-4
        now_gap = math.log(economics.mid(sb) / economics.mid(lb))
        dev = now_gap - base
        costs, why = economics.round_trip(lb, sb, self.cfg["wide_size"], 0.0004, 0.0004)
        if why or dev < 3 * mad:
            return dict(reasons=[dict(category="COST", code="GAP_NOT_EXTREME", text="")],
                        eligible=False)
        basis = self.cfg["wide_size"] * dev * 0.6
        n = economics.net(costs, funding=0.0, basis=basis, reserve=0.0)
        ok = n["net"] > 0
        return dict(reasons=[] if ok else [dict(category="COST", code="NET_BELOW_MIN",
                                                text="")],
                    eligible=ok, net=n["net"], baseline=base, mad=mad, deviation=dev,
                    funding=0.0, basis=basis, fees=n["fees"], spread=n["spread"])


DEFAULT_FAULTS = dict(outage=[(5, 6)], rate_limit=[(10, 10.5)], stale=[(15, 16)],
                      delist=[(20, 26)], disk_full=[(30, 31)], wal=[(40, 41)],
                      crash=[45])


def run(hours=72, step=60, seed=7, faults=None, out=None):
    faults = dict(DEFAULT_FAULTS if faults is None else faults)
    tmp = tempfile.mkdtemp(prefix="v2stress-")
    root = Path(out or tmp)
    root.mkdir(parents=True, exist_ok=True)
    t0 = 1_800_000_000.0
    m = Market(t0 - 7 * 86400, seed)
    while m.t < t0:                                   # 7 days of synthetic history
        m.step(3600)
    world = World(m, faults, t0)
    cfg = dict(wide_paper.WIDE_DEFAULTS, wide_hybrid_enabled=True, wide_hybrid_max_open=2)
    db = root / "paper.sqlite3"
    db.write_bytes(b"")
    clock = lambda: m.t                               # noqa: E731
    free = lambda p: ops.shutil.disk_usage(p)._replace(  # noqa: E731
        free=0 if world.active("disk_full") else 10 ** 12)

    def build():
        desk = HybridDesk(root / "hybrid.json", world, cfg, clock=clock)
        rec = Recorder(root / "recorder", world.wanted, clock=clock, disk_usage=free,
                       segment_seconds=3600)
        rt = Runtime(world, StressGate(world, cfg), cfg, desk=desk, recorder=rec,
                     data_dir=str(root), db_path=str(db), clock=clock)
        return rt, desk, rec

    rt, desk, rec = build()
    escaped, crashes, rounds, blocked_by_safety = [], 0, 0, 0
    crash_at = set(faults.get("crash", []))
    errors = []                                   # kept across restarts
    rec_totals = collections.Counter()            # recorder counters across restarts
    wal = Path(str(db) + "-wal")
    end = t0 + hours * 3600
    while m.t < end:
        m.step(step)
        h = (m.t - t0) / 3600
        wal.write_bytes(b"" if not world.active("wal") else b"")
        if world.active("wal"):
            # A WAL past the stop limit, without writing gigabytes: the check is patched.
            pre = lambda *a, **k: (False, ["MOCK WAL past stop limit"])  # noqa: E731
        else:
            pre = None
        if int(h) in crash_at:
            crash_at.discard(int(h))
            crashes += 1
            rec_totals.update(rec.stats)
            rec.fh = None                              # abrupt: nothing flushed/sealed
            rt, desk, rec = build()                    # restart from files
        try:
            if pre:
                orig = ops.preflight
                ops.preflight = pre
            try:
                rt.evaluate_once()
                if not rt.last["safe"]:
                    blocked_by_safety += 1
            finally:
                if pre:
                    ops.preflight = orig
            rounds += 1
        except Exception as ex:                        # the runtime loop catches these
            errors.append(dict(at=m.t, error=repr(ex)[:200]))
            if not isinstance(ex, (ConnectionError, RuntimeError)):
                escaped.append(repr(ex)[:200])
            if world.active("outage") or world.active("rate_limit"):
                rec.gap(None, None, m.t, m.t + step, "MOCK collector error: %s"
                        % type(ex).__name__)
    rec.close()
    rec_totals.update(rec.stats)
    states = collections.Counter(p["state"] for p in desk.positions.values())
    recon = []
    for p in desk.positions.values():
        pn = HybridDesk.pnl(p)
        recon.append(abs(pn["net"] - (pn["price_pnl"] + pn["funding"] - pn["fees"])) < 1e-9)
    integrity = replay.verify(root / "recorder")
    report = dict(
        label=LABEL, simulated_hours=hours, step_s=step, rounds=rounds,
        rounds_failed=len(errors),
        errors_caught=dict(collections.Counter(e["error"].split("(")[0] for e in errors)),
        exceptions_escaped=escaped, crashes=crashes,
        entries_blocked_by_safety=blocked_by_safety, positions=dict(states),
        ledger_reconciled=all(recon), open_exposure=[r for r in desk.risk()
                                                    if abs(r["exposure"]) > 1e-12],
        recorder=dict(rec_totals), replay_ok=integrity["ok"],
        replay_recovered_segments=len(integrity["recovered"]),
        replay_records=integrity["records"], faults=faults)
    (root / "stress_report.json").write_text(json.dumps(report, indent=1, default=str),
                                             encoding="utf-8")
    if out is None:
        shutil.rmtree(tmp, ignore_errors=True)
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--step", type=float, default=60)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out).resolve()
    if str(out).lower().startswith(("e:", "d:")):
        raise SystemExit("must not write on the failing disk (D:/E:)")
    r = run(a.hours, a.step, out=out)
    print(json.dumps({k: v for k, v in r.items() if k != "faults"}, indent=1, default=str))


if __name__ == "__main__":
    main()
