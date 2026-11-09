"""Funding observations with explicit timing (PAPER ONLY; model only, not wired into the
engine; no gate or acceptance rule changes).

Separates what a funding number IS from WHEN we could have known it:

  kind          forecast      a rate the venue publishes for a period that has not ended
                last_settled  a live field that carries the last CLOSED period's rate
                              (RISEx current_funding_rate, Lighter RH last funding row)
                settled       a public settlement record
  period_start / period_end   the funding period the rate belongs to (None if unknown)
  settled_at    when the venue settled it (RISEx block_time); a lower bound for knowing it
  published_at  when the venue says it published it (None: RISEx/Lighter do not say)
  observed_at   when WE received it. The only time used for "known at T" decisions.
                Never back-filled: history downloaded later carries its download time.

Evidence categories stay separate and are never summed into one another:
  public_settled  public settlement records (and their internal consistency)
  account_cash    cash actually paid/received on an account (unavailable in PAPER)
  paper_accrual   our simulated accrual from public records

RISEx facts used (funding-evidence-audit, risex-funding-sign-and-timing):
  * current_funding_rate equals the last settled period's rate (in-period experiment);
  * accumulated_funding steps at settlement (hourly) and is flat inside a period;
  * docs: payment = (new_checkpoint - last_checkpoint) * signed_size, positive = paid.
  * Whether the venue charges a position opened inside a period for the whole period is
    UNKNOWN (docs: funding is computed on trades; public data: hourly steps).
"""

from dataclasses import dataclass
from typing import Optional

KINDS = ("forecast", "last_settled", "settled")
NS = 1_000_000_000

# Docs say funding is computed when a trade happens; public SettleFundingRate events are
# hourly. Not reconciled by any official source -> UNKNOWN (kept explicit on purpose).
RISEX_SETTLEMENT_TRIGGER = "UNKNOWN"
# Tolerance for public consistency checks (relative).
CONSISTENCY_TOL = 1e-9


@dataclass(frozen=True)
class FundingObservation:
    venue: str
    market: str
    kind: str
    rate: float
    observed_at: Optional[float]
    period_start: Optional[float] = None
    period_end: Optional[float] = None
    settled_at: Optional[float] = None
    published_at: Optional[float] = None
    accumulated: Optional[float] = None
    index_price: Optional[float] = None
    source: str = ""

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError("Bilinmeyen funding gözlem türü: %r" % self.kind)


# ---- constructors from venue data -------------------------------------------------
def risex_settled(record, observed_at, market="BTC"):
    """A RISEx funding-rate-history record (SettleFundingRate). `observed_at` is the time
    we downloaded it: never its block_time."""
    return FundingObservation(
        venue="RISEx", market=market, kind="settled",
        rate=float(record["funding_rate"]), observed_at=observed_at,
        period_start=int(record["start_time"]) / NS, period_end=int(record["end_time"]) / NS,
        settled_at=int(record["block_time"]) / NS,
        accumulated=float(record["accumulated_funding"]),
        index_price=float(record["index_price"]), source="funding-rate-history",
    )


def risex_live(market_row, observed_at, interval, market="BTC"):
    """RISEx /v1/markets current_funding_rate: the LAST CLOSED period's rate. Its period
    is the one that ended at next_funding_time - interval. Never a forecast."""
    nxt = int(market_row["next"]) / NS
    end = nxt - interval
    acc = market_row.get("acc")
    return FundingObservation(
        venue="RISEx", market=market, kind="last_settled",
        rate=float(market_row["cur"]), observed_at=observed_at,
        period_start=end - interval, period_end=end, settled_at=None,
        accumulated=float(acc) if acc is not None else None, source="markets",
    )


def lighter_settled(row, observed_at, market="BTC"):
    """A Lighter RH /fundings row. Its `timestamp` meaning (period start or end) is not
    documented: the period stays unknown; the sign comes from `direction`."""
    sign = 1 if row["direction"] == "long" else -1 if row["direction"] == "short" else None
    if sign is None:
        raise ValueError("Bilinmeyen funding yönü")
    return FundingObservation(
        venue="Lighter", market=market, kind="settled",
        rate=abs(float(row["rate"])) / 100 * sign, observed_at=observed_at,
        settled_at=float(row["timestamp"]), source="fundings",
    )


# ---- what was knowable at T ---------------------------------------------------------
def knowable(observations, at):
    """Only what we had actually received by `at`. Observations without an
    observed_at are never treated as known."""
    return [o for o in observations if o.observed_at is not None and o.observed_at <= at]


def settled_but_not_observed(observations, at):
    """Settlements that had happened by `at` but that we had not yet received: using them
    at `at` would be lookahead."""
    return [o for o in observations
            if o.settled_at is not None and o.settled_at <= at
            and (o.observed_at is None or o.observed_at > at)]


def latest_rate(observations, at):
    """(rate, period_end, kind) of the most recent closed period known at `at`."""
    xs = [o for o in knowable(observations, at)
          if o.kind in ("settled", "last_settled") and o.period_end is not None]
    if not xs:
        return None
    o = max(xs, key=lambda o: (o.period_end, o.observed_at))
    return (o.rate, o.period_end, o.kind)


def forecast_for(observations, period_end, at):
    """A forecast for the period ending at `period_end`, known at `at`; None if the
    venue published none. A last_settled rate is never returned as a forecast."""
    xs = [o for o in knowable(observations, at)
          if o.kind == "forecast" and o.period_end == period_end]
    return max(xs, key=lambda o: o.observed_at) if xs else None


def view(observations, at):
    """Everything a decision at `at` may use. Used by the truncation (lookahead) test:
    view(all, at) must equal view(knowable(all, at), at)."""
    lr = latest_rate(observations, at)
    nxt = None if lr is None else lr[1] + 3600
    return dict(latest=lr, forecast_next=forecast_for(observations, nxt, at),
                known=sorted((o.kind, o.period_end, o.rate) for o in knowable(observations, at)
                             if o.period_end is not None))


# ---- public settlement consistency (evidence: public_settled) -------------------------
def unique_settled(settled):
    """One settlement per (venue, market, period_end): the same record downloaded twice
    is the same record (the earliest observation is kept)."""
    best = {}
    for o in settled:
        if o.kind != "settled":
            continue
        k = (o.venue, o.market, o.period_end, o.settled_at)
        if k not in best or (o.observed_at or 0) < (best[k].observed_at or 0):
            best[k] = o
    return list(best.values())


def settled_consistency(settled):
    """For consecutive RISEx settlements: does the accumulated funding step equal
    rate x index price? Returns [(period_end, ok)]."""
    xs = sorted((o for o in unique_settled(settled) if o.accumulated is not None),
                key=lambda o: o.period_end)
    out = []
    for a, b in zip(xs, xs[1:]):
        if b.period_start != a.period_end:
            continue  # not adjacent periods (a gap in the records): not comparable
        step = b.accumulated - a.accumulated
        expect = b.rate * b.index_price
        ok = abs(step - expect) <= CONSISTENCY_TOL * max(1.0, abs(expect)) + 1e-12
        out.append((b.period_end, ok))
    return out


# ---- paper accrual (evidence: paper_accrual) ------------------------------------------
def paper_accrual(entry_at, exit_at, size, entry_checkpoint, settled, at, interval=3600):
    """Simulated funding for a paper position with signed `size` (negative = short),
    using the venue's checkpoint rule: payment = (checkpoint_k - checkpoint_{k-1}) * size,
    positive = paid by us (cash = -payment).

    Only settlements with entry_at < settled_at <= min(exit_at, at) AND observed by
    `at` are used. Each entry is KNOWN, UNKNOWN_PARTIAL_PERIOD or MISSING; unknown and
    missing payments are never filled, pro-rated or treated as zero.

    A position that did not hold a whole period (opened or closed inside it) has an
    UNKNOWN payment for that period: the venue's treatment of partial periods is not
    verified. `entry_checkpoint` must be an observation with an accumulated value and
    observed_at <= entry_at, or the first payment is UNKNOWN as well."""
    horizon = min(exit_at, at)
    known = sorted((o for o in unique_settled(knowable(settled, at))
                    if o.settled_at is not None and entry_at < o.settled_at <= horizon),
                   key=lambda o: o.period_end)
    out = []
    prev = None
    if (entry_checkpoint is not None and entry_checkpoint.accumulated is not None
            and entry_checkpoint.observed_at is not None
            and entry_checkpoint.observed_at <= entry_at):
        prev = entry_checkpoint

    def missing(start, end):
        # Periods between `start` and `end` with no settlement known: listed, never filled.
        rows, t = [], end
        while t - interval >= start - interval * 0.5 and t > start:
            rows.append(dict(period_end=t, settled_at=None, evidence="paper_accrual",
                             payment=None, cash=None, status="MISSING"))
            t -= interval
        return list(reversed(rows))

    expected_end = None
    for o in known:
        row = dict(period_end=o.period_end, settled_at=o.settled_at,
                   evidence="paper_accrual", payment=None, cash=None)
        if expected_end is None and o.period_start is not None and o.period_start > entry_at:
            # Time between the entry and the first known period is not covered.
            gap = missing(entry_at, o.period_start)
            if gap:
                out += gap
                prev = None
        if expected_end is not None and o.period_end - expected_end > interval * 1.5:
            out += missing(expected_end, o.period_end - interval)
            prev = None
        partial = (o.period_start is None or entry_at > o.period_start
                   or exit_at < o.period_end)
        if partial:
            row["status"] = "UNKNOWN_PARTIAL_PERIOD"
        elif prev is None or prev.accumulated is None:
            row["status"] = "UNKNOWN_NO_CHECKPOINT"
        else:
            payment = (o.accumulated - prev.accumulated) * size
            row.update(status="KNOWN", payment=payment, cash=-payment)
        out.append(row)
        prev = o
        expected_end = o.period_end
    return out


ACCOUNT_CASH_STATUS = "UNAVAILABLE"  # PAPER: no account connection, never simulated
