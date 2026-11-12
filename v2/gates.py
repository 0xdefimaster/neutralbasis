"""The V2 paper-entry gate: an independent check every candidate must pass before a
PAPER position may open, on top of the desk's own checks.

Order (first failure wins, all are reported):
  1. identity: both legs come from V2 strategy rows (tier A, verified multi-venue)
  2. execution model: Variational answers with an indicative RFQ quote (bid/ask that
     may be cached up to 600 s; the firm quote can differ; last look), which is not a
     fill price -> EXECUTION_MODEL_UNVERIFIED until an RFQ fill model is proven
  3. fees known (RISEx: per-account only -> FEE_UNKNOWN)
  4. RWA: commodities, FX, indices and mixed headings reference contracts the venues
     do not state alike (futures month, index provider, spot vs future)
     -> RWA_REFERENCE_UNVERIFIED; equities/ETFs need the US regular session open, read
     from Arcus's own `isOutsideRth` (venue-published) -> RWA_SESSION_UNKNOWN / _CLOSED
  5. funding and hybrid: both legs need settled public payments; TxFlow and Variational
     publish a rate only -> SETTLEMENT_EVIDENCE_MISSING (published and settled kept
     apart)
  6. books fresh: older than the venue's limit (v2.marketdata.MAX_AGE) -> BOOK_STALE
  7. the strategy itself (v2.strategies), on the real books at the position size:
     liquidity, fees, spread, funding, basis, reserve, break-even, net.
"""
from v2 import marketdata, observation
from v2.strategies import COST, DATA, EXECUTION, convergence, funding, hybrid, reason
from v2.strategies.common import Leg

RFQ_VENUES = ("Variational",)
PUBLISHED_ONLY = ("TxFlow", "Variational")
REFERENCE_UNVERIFIED = ("commodity", "fx", "index", "rwa_mixed", "rwa")
SESSION_CLASSES = ("equity", "etf")
STRATEGIES = dict(funding=funding, convergence=convergence, hybrid=hybrid)


class Gate:
    """`provider` gives rows_by() {(venue, asset): row}, book(v, a), settled(v, a, at),
    closes(v, a) and rth_open() (True / False / None)."""

    def __init__(self, provider, cfg, size=None):
        self.provider = provider
        self.cfg = cfg
        self.size = size

    def static(self, c, rows):
        out = []
        legs = [(side, c[side]) for side in ("long", "short")]
        for side, v in legs:
            if (v, c["asset"]) not in rows:
                out.append(reason(DATA, "NOT_A_V2_ROW", "%s %s is not a verified V2 row"
                                  % (side, v)))
        if out:
            return out
        for side, v in legs:
            # Defence in depth: an observation-only row (v2.observation) mixed into the
            # PAPER rows can never become a leg.
            xid = rows[(v, c["asset"])].get("exchange_id")
            if xid is not None and not observation.is_paper_venue(xid):
                out.append(reason(DATA, "CONFIG_EXCLUDED", "%s %s (%s) is observation "
                                  "only, not a PAPER venue" % (side, v, xid)))
        if out:
            return out
        for side, v in legs:
            if v in RFQ_VENUES:
                out.append(reason(EXECUTION, "EXECUTION_MODEL_UNVERIFIED",
                                  "%s %s: indicative RFQ quote (cached up to 600 s, firm "
                                  "quote may differ) is not a fill price" % (side, v)))
            if rows[(v, c["asset"])].get("fee") is None:
                out.append(reason(DATA, "FEE_UNKNOWN", "%s %s fee UNKNOWN" % (side, v)))
        fine = rows[(c["long"], c["asset"])].get("fine_class")
        if fine in REFERENCE_UNVERIFIED:
            out.append(reason(DATA, "RWA_REFERENCE_UNVERIFIED",
                              "%s: venues do not state the same reference contract "
                              "(%s)" % (c["asset"], fine)))
        elif fine in SESSION_CLASSES:
            state = self.provider.rth_open()
            if state is None:
                out.append(reason(DATA, "RWA_SESSION_UNKNOWN",
                                  "US regular session state not published by a venue"))
            elif not state:
                out.append(reason(EXECUTION, "RWA_SESSION_CLOSED",
                                  "outside US regular trading hours (Arcus isOutsideRth)"))
        if c.get("strategy") in ("funding", "hybrid", None):
            for side, v in legs:
                if v in PUBLISHED_ONLY:
                    out.append(reason(DATA, "SETTLEMENT_EVIDENCE_MISSING",
                                      "%s %s publishes a rate only; no public settled "
                                      "payments" % (side, v)))
        return out

    def legs(self, c, rows, at):
        out = []
        for side in ("long", "short"):
            v, a = c[side], c["asset"]
            out.append(Leg(row=rows[(v, a)], book=self.provider.book(v, a),
                           settled=self.provider.settled(v, a, at),
                           closes=self.provider.closes(v, a)))
        return out

    def evaluate(self, c, at, strategy=None):
        strategy = strategy or c.get("strategy") or "funding"
        rows = self.provider.rows_by()
        reasons = self.static(dict(c, strategy=strategy), rows)
        if reasons:
            return dict(strategy=strategy, reasons=reasons, eligible=False)
        long, short = self.legs(c, rows, at)
        stale = [reason(DATA, "BOOK_STALE", "%s %s book older than its limit"
                        % (side, leg.row.get("venue")))
                 for side, leg in (("long", long), ("short", short))
                 if leg.book is not None and marketdata.stale(
                     leg.book, leg.row.get("exchange_id"), at)]
        if stale:
            return dict(strategy=strategy, reasons=stale, eligible=False)
        size = self.size or self.cfg["wide_size"]
        ev = STRATEGIES[strategy].evaluate(long, short, size, self.cfg, at)
        ev["eligible"] = not ev["reasons"] and not ev.get("research_only")
        return ev

    def __call__(self, c, out=None, at=None):
        """wide_paper hook: reason texts that block this entry (empty: the gate passes)."""
        import time

        ev = self.evaluate(c, at if at is not None else time.time())
        return ["V2 %s: %s" % (r["code"], r["text"]) for r in ev["reasons"]]


def category_counts(evaluations):
    """{first-reason category: count} plus 'ELIGIBLE' and 'RESEARCH_ONLY'."""
    out = {}
    for ev in evaluations:
        if ev.get("eligible"):
            k = "ELIGIBLE"
        elif not ev["reasons"]:
            k = "RESEARCH_ONLY"
        else:
            k = ev["reasons"][0]["category"]
        out[k] = out.get(k, 0) + 1
    return out
