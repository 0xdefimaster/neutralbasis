"""The V2 real-time evaluation loop (PAPER).

Each round:
  1. collector.refresh(): the eight venues' public reads -> tier-A strategy rows
  2. candidates: funding (wide_paper.rank), convergence (rank_spreads, BOTH directions
     of every pair), hybrid (every directed pair of the two lists)
  3. every candidate through v2.gates.Gate on real books -> eligible or the first
     reason with its category (DATA / EXECUTION / COST) and the estimated net when the
     strategy got that far
  4. the hybrid desk: settles funding, checks exits, closes; opens new hybrids only
     when `wide_hybrid_enabled` is set (default False: evaluation only)
  5. the recorder: each evaluated book and row of a V2 market (nothing else)
  6. safety: disk and WAL checks (v2.ops) stop new entries when a limit is crossed

Funding and convergence positions are opened by the paper desk (WidePaper) through its
own cycle with the V2 gate attached (desk_analytics, NB_V2=1); this loop does not open
them a second time. Risk settings are read, never changed (wide_max_breakeven_hours,
wide_funding_realization stay at the owner's values).
"""
import collections
import threading
import time
import traceback

from v2 import gates, marketdata, ops

DEFAULT_HYBRID = dict(wide_hybrid_enabled=False, wide_hybrid_max_open=1)


class Runtime:
    def __init__(self, collector, gate, cfg, desk=None, recorder=None, data_dir=None,
                 db_path=None, clock=time.time, sleep=time.sleep):
        self.collector, self.gate, self.cfg = collector, gate, dict(DEFAULT_HYBRID, **cfg)
        self.desk, self.recorder = desk, recorder
        self.data_dir, self.db_path = data_dir, db_path
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.last = None
        self.errors = collections.deque(maxlen=50)
        self.rounds = 0
        self.backoff = marketdata.Backoff(5.0, 300.0)
        if recorder is not None:
            # Every raw answer and API error of the eight venues, with provenance.
            collector.sink = recorder.sink

    # -- one round ------------------------------------------------------------------------
    def candidates(self, rows, at):
        import wide_paper

        f = [dict(c, strategy="funding") for c in wide_paper.rank(rows, self.cfg, at)]
        conv = []
        for c in wide_paper.rank_spreads(rows, at, self.cfg):
            for lv, sv in ((c["long"], c["short"]), (c["short"], c["long"])):
                conv.append(dict(asset=c["asset"], long=lv, short=sv, strategy="convergence"))
        pairs = {(c["asset"], c["long"], c["short"]) for c in f + conv}
        hyb = [dict(asset=a, long=lv, short=sv, strategy="hybrid")
               for a, lv, sv in sorted(pairs)]
        return dict(funding=f, convergence=conv, hybrid=hyb)

    def safety(self):
        if not self.data_dir:
            return True, []
        return ops.preflight(self.data_dir, self.db_path)

    def evaluate_once(self):
        at = self.clock()
        rows = self.collector.refresh()
        cands = self.candidates(rows, at)
        results = {}
        for strategy, cs in cands.items():
            out = []
            for c in cs:
                try:
                    ev = self.gate.evaluate(c, at, strategy)
                except Exception as ex:
                    ev = dict(reasons=[dict(category="DATA", code="EVAL_ERROR",
                                            text=str(ex)[:200])], eligible=False)
                out.append(dict(asset=c["asset"], long=c["long"], short=c["short"],
                                eligible=bool(ev.get("eligible")),
                                research_only=bool(ev.get("research_only")),
                                net=ev.get("net"),
                                components={k: ev.get(k) for k in
                                            ("funding", "basis", "fees", "spread", "reserve")
                                            if ev.get(k) is not None},
                                reasons=ev.get("reasons") or [], evaluation=ev))
            results[strategy] = out
        safe, findings = self.safety()
        opened = []
        if self.desk is not None:
            self.desk.cycle()
            if (safe and self.cfg.get("wide_hybrid_enabled")
                    and len(self.desk.open_positions()) < self.cfg["wide_hybrid_max_open"]):
                for r in sorted((x for x in results["hybrid"]
                                 if x["evaluation"].get("net") is not None
                                 and not x["reasons"]), key=lambda x: -x["net"]):
                    p = self.desk.open(r, r["evaluation"], self.cfg["wide_size"])
                    opened.append(p["id"])
                    break
        if self.recorder is not None:
            for r in rows:
                self.recorder.record("row", r["venue"], r["asset"], r,
                                     exchange_id=r.get("exchange_id"),
                                     market_id=r.get("market_id"),
                                     underlying_id=r.get("underlying_id"))
        snap = dict(at=at, rows=len(rows), safe=safe, safety_findings=findings,
                    strategies={s: summarize(v) for s, v in results.items()},
                    top={s: top(v) for s, v in results.items()},
                    open_hybrids=[public(p) for p in (self.desk.open_positions()
                                                      if self.desk else [])],
                    opened=opened, reasons_by_venue=self.collector.reasons,
                    recorder=self.recorder.health() if self.recorder else None)
        with self.lock:
            self.last = snap
            self.rounds += 1
        return snap

    def run(self, stop, every=60.0):
        """Loop until `stop` is set; an error is logged and retried with backoff."""
        while not stop.is_set():
            try:
                self.evaluate_once()
                self.backoff.reset()
                stop.wait(every)
            except Exception:
                self.errors.append(dict(at=self.clock(), error=traceback.format_exc()[-800:]))
                stop.wait(self.backoff.next())

    def start_txflow_stream(self, stop, connect=None):
        """TxFlow l2BookDepth over WebSocket for the TxFlow V2 rows (off unless
        NB_V2_TXFLOW_WS=1: the live socket has not been exercised yet). The stream is
        rebuilt when the set of wanted TxFlow markets changes."""
        import os

        if os.environ.get("NB_V2_TXFLOW_WS") != "1":
            return None
        from extended_rfq import WebSocket

        connect = connect or (lambda url: WebSocket(url))

        def loop():
            coins = None
            stream = None
            while not stop.is_set():
                want = sorted(r["market_id"] for r in self.collector.rows
                              if r.get("exchange_id") == "txflow")
                if want != coins:
                    coins = want
                    stream = (marketdata.TxFlowDepthStream(
                        coins, connect, sink=self.recorder.sink if self.recorder else None)
                        if coins else None)
                    self.collector.streams.pop("txflow", None)
                    if stream:
                        self.collector.streams["txflow"] = stream
                if stream is None:
                    stop.wait(5)
                    continue
                stream.step()

        t = threading.Thread(target=loop, daemon=True, name="v2-txflow-ws")
        t.start()
        return t

    def snapshot(self):
        with self.lock:
            return dict(self.last or {}, rounds=self.rounds, errors=list(self.errors)[-5:])


def summarize(results):
    first = collections.Counter(r["reasons"][0]["code"] for r in results if r["reasons"])
    cats = collections.Counter("ELIGIBLE" if r["eligible"] else
                               "RESEARCH_ONLY" if not r["reasons"] else
                               r["reasons"][0]["category"] for r in results)
    return dict(candidates=len(results), by_category=dict(cats),
                first_reason=dict(first.most_common()))


def top(results, n=15):
    priced = [r for r in results if r.get("net") is not None]
    priced.sort(key=lambda r: -r["net"])
    return [dict(asset=r["asset"], long=r["long"], short=r["short"], net=r["net"],
                 eligible=r["eligible"], components=r["components"],
                 reason=(r["reasons"][0]["code"] if r["reasons"] else None))
            for r in priced[:n]]


def public(p):
    from v2.hybrid_desk import HybridDesk

    return dict(id=p["id"], asset=p["asset"], state=p["state"],
                legs={k: dict(venue=v["venue"], qty=v["qty"], entry=v["entry"])
                      for k, v in p["legs"].items()},
                expected=p.get("expected"), pnl=HybridDesk.pnl(p),
                close_reason=p.get("close_reason"))
