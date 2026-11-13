"""The paper desk's data source under V2 (NB_V2=1).

Market rows and order books come from v2.collector (the eight venues' public reads,
identity-verified tier-A multi-venue markets only). Price and funding history stay with
the engine's history keeper, as in the legacy EngineSource; the keeper is limited to
these rows (desk_analytics.DeskHistorySource.markets), so single-venue markets get no
backfill. The V2 gate (v2.gates.Gate) reads rows, books, settled payments and closes
through this object.
"""
import time

from v2 import gates
from v2.collector import Collector


class V2EngineSource:
    def __init__(self, engine, collector=None, refresh_every=60.0, clock=time.time):
        from wide_paper import EngineSource

        self.engine = engine
        self.legacy = EngineSource(engine)  # history keeper access only
        self.collector = collector or Collector()
        self.refresh_every = refresh_every
        self.clock = clock
        self.last = 0.0
        self.excluded = {}

    # -- EngineSource interface ---------------------------------------------------------
    def market_rows(self):
        if self.clock() - self.last >= self.refresh_every or not self.collector.rows:
            self.collector.refresh()
            self.last = self.clock()
            self.excluded = {xid: dict(c) for xid, c in self.collector.reasons.items()}
        return [dict(r) for r in self.collector.rows]

    def book(self, v, a):
        return self.collector.book(v, a)

    def history_states(self):
        return self.legacy.history_states()

    def prices(self, v, a):
        return self.legacy.prices(v, a)

    def current_funding(self, v, a):
        # Rows are rebuilt at most every `refresh_every` s (< ENTRY_FUNDING_MAX_AGE).
        # Under the answer time basis (Phase 15) this is a REAL re-read: the cached
        # answers behind the market's rate are dropped first; under the legacy basis a
        # refresh inside the cache TTL serves the same answer re-stamped (docs/v2/45).
        from v2 import marketdata
        if marketdata.TIME_BASIS == marketdata.ANSWER:
            r0 = self.collector.row(v, a)
            self.collector.invalidate_rate(r0["exchange_id"], r0["market_id"])
            self.last = -1e18
        if self.clock() - self.last >= self.refresh_every:
            self.market_rows()
        r = self.collector.row(v, a)
        return dict(raw_rate=r["raw_rate"], rate=r["rate"], interval=r["interval"],
                    next=r["next"], funding_kind=r.get("funding_kind"),
                    funding_received=r["received"],
                    # Phase 15 SHADOW: the fetch time of the rate's answer (P1)
                    funding_answer_received=r.get("answer_received"))

    def funding(self, v, a, start):
        rows = self.collector.settled(v, a) or []
        return [dict(ts=x["ts"], rate=x["rate"], value=None, audit_id=None)
                for x in rows if x["ts"] >= start]

    def stored_funding(self, v, a, start):
        return self.legacy.stored_funding(v, a, start)

    def analysis_funding(self, v, a, start):
        return self.legacy.analysis_funding(v, a, start)

    # -- V2 gate provider -----------------------------------------------------------------
    def rows_by(self):
        return dict(self.collector.by_key)

    def settled(self, v, a, at):
        try:
            return self.collector.settled(v, a, at)
        except Exception:
            return None

    def closes(self, v, a):
        try:
            return self.prices(v, a)["prices"]
        except Exception:
            return None

    def rth_open(self):
        return self.collector.rth_open()

    def wanted(self):
        return self.collector.wanted()


def attach(engine, desk, data_dir=None):
    """Wire V2 into a desk: V2 source, the V2 gate and (with a data folder) the V2
    runtime with its hybrid desk and selective recorder. Nothing starts here; the
    engine loop starts the runtime thread. Returns the source."""
    src = V2EngineSource(engine)
    desk.source = src
    desk.v2_gate = gates.Gate(src, desk.cfg)
    if data_dir:
        from pathlib import Path

        from v2 import ops
        from v2.hybrid_desk import HybridDesk
        from v2.recorder import Recorder
        from v2.runtime import Runtime

        root = ops.not_on_e(data_dir)
        (root / "v2").mkdir(parents=True, exist_ok=True)
        hybrid = HybridDesk(root / "v2" / "hybrid_positions.json", src, desk.cfg)
        recorder = Recorder(root / "v2" / "recorder", src.wanted)
        engine.v2_runtime = Runtime(src.collector, desk.v2_gate, desk.cfg, desk=hybrid,
                                    recorder=recorder, data_dir=str(root),
                                    db_path=str(Path(root) / "paper.sqlite3"))
    return src
