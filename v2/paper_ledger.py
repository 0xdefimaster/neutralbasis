"""ESTIMATED PAPER portfolio ledger: virtual positions from entry signal to close.

PAPER ONLY and opt-in (it is used by tools.v2_paper_replay and tests; the runtime does
not create it). Separate from the strict PAPER desk (v2.hybrid_desk) and its state.
Every record says ESTIMATED_PAPER: a position here is a SIMULATION on observed public
data, never an exchange position; a fill is never verified.

Event-sourced: each change is one hash-chained JSON line (v2.storage.ChainLog). The
in-memory state is rebuilt from the events on start, so a restart resumes every
position where it was (`restarted` is set on positions that were not final).

States (a signal is not a position; a position starts at the execution attempt):
  DISCOVERED, ECONOMICALLY_INTERESTING, ENTRY_SIGNAL         (signal log only)
  PAPER_EXECUTION_ATTEMPT   legs being simulated
  PARTIALLY_FILLED          legs unequal after the attempt; the gap is retried
  HEDGED_OPEN               both legs filled with equal quantity
  LEG_RECOVERY              retries exhausted: unwinding the excess / failed entry
  POSITION_MONITORING       hedged and watched every observation
  EXIT_SIGNAL               an exit condition fired
  PAPER_CLOSE_ATTEMPT       closing fills being simulated
  RECOVERY_PENDING          a close or an unwind could not finish; retried
  CLOSED                    every leg flat
Unhedged exposure is never hidden: `risk()` reports, per position, each leg's open
quantity, the unhedged units and their value at the last observed mid.

Accounting per position (USD):
  price_pnl         from simulated fill prices (spread is inside the prices; never
                    added again)
  fees              taker fees of every simulated fill
  funding           by evidence: SETTLED_RATE (a public settled rate, valued at the
                    observed mark: the venue's price basis is not in the history ->
                    value estimated), PUBLISHED_ESTIMATE (a published rate at a
                    payment time), PUBLISHED_CARRIED_FORWARD (no observation at the
                    payment time: the last published rate is carried, an assumption)
  total             price_pnl - fees + all funding; `observed_part` = price_pnl - fees
                    + SETTLED_RATE funding; `estimated_part` = the published funding
"""
import hashlib
import json

from v2 import storage

LABEL = "ESTIMATED_PAPER (simulated on observed public data; not an exchange position)"
DISCOVERED, INTERESTING, ENTRY_SIGNAL = ("DISCOVERED", "ECONOMICALLY_INTERESTING",
                                         "ENTRY_SIGNAL")
ATTEMPT, PARTIAL, HEDGED = "PAPER_EXECUTION_ATTEMPT", "PARTIALLY_FILLED", "HEDGED_OPEN"
LEG_RECOVERY, MONITORING, EXIT_SIGNAL = "LEG_RECOVERY", "POSITION_MONITORING", "EXIT_SIGNAL"
CLOSE_ATTEMPT, RECOVERY_PENDING, CLOSED = ("PAPER_CLOSE_ATTEMPT", "RECOVERY_PENDING",
                                           "CLOSED")
FINAL = (CLOSED,)
FUNDING_KINDS = ("SETTLED_RATE", "PUBLISHED_ESTIMATE", "PUBLISHED_CARRIED_FORWARD")
EPS = 1e-9


def pos_id(seed, signal):
    """Deterministic id: the same replay gives the same ids."""
    raw = json.dumps([seed, signal["strategy"], signal["asset"], signal["long"],
                      signal["short"], signal["at"], signal["size"]], sort_keys=True)
    return "ep-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


class PaperLedger:
    def __init__(self, folder, seed="nb-v2-phase8", environ=None):
        self.folder = storage.check_dir(folder, environ)
        self.log = storage.ChainLog(self.folder / "ledger.jsonl", environ)
        self.seed = seed
        self.positions, self.signals = {}, []
        events, self.integrity = storage.read_chain(self.log.path)
        for e in events:
            self._apply(e)
        for p in self.positions.values():
            if p["state"] not in FINAL:
                p["restarted"] = True

    # -- events ------------------------------------------------------------------------
    def emit(self, kind, **e):
        e = dict(e, kind=kind, ledger_label=LABEL)
        self.log.append(e)
        self._apply(e)
        return e

    def _apply(self, e):
        k = e["kind"]
        if k == "signal":
            self.signals.append(e)
        elif k == "open_attempt":
            self.positions[e["pos"]] = dict(
                id=e["pos"], state=ATTEMPT, strategy=e["strategy"], asset=e["asset"],
                size=e["size"], signal=e["signal"], opened_at=e["at"], closed_at=None,
                legs={s: dict(e["legs"][s], qty=0.0, entry_qty=0.0, entry_cost=0.0,
                              exit_qty=0.0, exit_value=0.0, fees=0.0, fills=[])
                      for s in ("long", "short")},
                funding=[], funding_keys=[], states=[[e["at"], ATTEMPT, None]],
                expected=e.get("expected"), exit_params=e.get("exit_params"),
                close_reason=None, secondary=[], retries=0, restarted=False,
                reliability=[], marks=[], scenario_only=e.get("scenario_only", False))
        elif k == "fill":
            p = self.positions[e["pos"]]
            leg = p["legs"][e["side"]]
            leg["fills"].append({x: e.get(x) for x in (
                "at", "action", "qty", "price", "fee", "label", "model", "obs_time",
                "quote_age_s", "tier", "slippage_bp", "partial", "verified")})
            leg["fees"] += e["fee"]
            opening = e["action"] in ("open", "retry")
            if opening:
                leg["entry_cost"] += e["qty"] * e["price"]
                leg["entry_qty"] += e["qty"]
                leg["qty"] += e["qty"]
            else:
                leg["exit_value"] += e["qty"] * e["price"]
                leg["exit_qty"] += e["qty"]
                leg["qty"] = max(0.0, leg["qty"] - e["qty"])
        elif k == "state":
            p = self.positions[e["pos"]]
            p["state"] = e["state"]
            p["states"].append([e["at"], e["state"], e.get("reason")])
            if e.get("reason") and e["state"] == EXIT_SIGNAL and p["close_reason"] is None:
                p["close_reason"] = e["reason"]
                p["secondary"] = e.get("secondary") or []
            if e["state"] == CLOSED:
                p["closed_at"] = e["at"]
                if p["close_reason"] is None:
                    p["close_reason"] = e.get("reason")
            if e.get("retries") is not None:
                p["retries"] = e["retries"]
            if e.get("time_quality"):
                p["time_quality_close"] = e["time_quality"]
        elif k == "funding":
            p = self.positions[e["pos"]]
            if e["key"] not in p["funding_keys"]:
                p["funding_keys"].append(e["key"])
                p["funding"].append({x: e.get(x) for x in (
                    "key", "side", "ts", "rate", "units", "price", "amount", "evidence",
                    "basis")})
        elif k == "mark":
            p = self.positions[e["pos"]]
            p["marks"].append({x: v for x, v in e.items()
                               if x not in ("kind", "ledger_label", "pos")})
            del p["marks"][:-50]
        elif k == "reliability":
            self.positions[e["pos"]]["reliability"].append(e["note"])

    # -- views -------------------------------------------------------------------------
    @staticmethod
    def entry_price(leg):
        return leg["entry_cost"] / leg["entry_qty"] if leg["entry_qty"] > EPS else None

    @classmethod
    def pnl(cls, p, marks=None):
        """Realized parts plus, for open quantity, an unrealized mark at `marks`
        {side: mid} (or None: then open quantity is reported unvalued)."""
        price = fees = 0.0
        unreal = 0.0
        unvalued = False
        for side, leg in p["legs"].items():
            ep = cls.entry_price(leg)
            sign = 1 if side == "long" else -1
            fees += leg["fees"]
            if leg["exit_qty"] > EPS and ep is not None:
                price += sign * (leg["exit_value"] - leg["exit_qty"] * ep)
            if leg["qty"] > EPS:
                m = (marks or {}).get(side)
                if m is None or ep is None:
                    unvalued = True
                else:
                    unreal += sign * leg["qty"] * (m - ep)
        fund = {k: sum(f["amount"] for f in p["funding"] if f["evidence"] == k)
                for k in FUNDING_KINDS}
        total_f = sum(fund.values())
        out = dict(price_pnl=price, fees=fees, funding=fund, funding_total=total_f,
                   realized_total=price - fees + total_f,
                   observed_part=price - fees + fund["SETTLED_RATE"],
                   estimated_part=fund["PUBLISHED_ESTIMATE"] +
                   fund["PUBLISHED_CARRIED_FORWARD"],
                   unrealized=None if unvalued else unreal)
        if p["strategy"] == "hybrid":
            out["components"] = dict(convergence_price_pnl=price, funding=total_f,
                                     shared_fees=fees, counted="fees once")
        return out

    @staticmethod
    def exposure(p):
        return p["legs"]["long"]["qty"] - p["legs"]["short"]["qty"]

    def risk(self, mids=None):
        """Open, unhedged risk of every position that is not CLOSED (never hidden)."""
        out = []
        for p in self.positions.values():
            if p["state"] in FINAL:
                continue
            x = self.exposure(p)
            m = (mids or {}).get(p["asset"])
            out.append(dict(id=p["id"], asset=p["asset"], strategy=p["strategy"],
                            state=p["state"], long_qty=p["legs"]["long"]["qty"],
                            short_qty=p["legs"]["short"]["qty"], unhedged_units=x,
                            unhedged_usd=None if m is None else abs(x) * m,
                            gross_usd=None if m is None else (p["legs"]["long"]["qty"] +
                                                              p["legs"]["short"]["qty"]) * m))
        return out

    def open_positions(self):
        return [p for p in self.positions.values() if p["state"] not in FINAL]
