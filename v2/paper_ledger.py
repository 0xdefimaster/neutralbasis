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

Accounting per position (USD), Phase 9 - every number says what it rests on:
  simulated_price_pnl   from SIMULATED fill prices (IOC walked through an observed book,
                        or an indicative RFQ quote); the spread is inside the prices.
                        Never an exchange fill: reproducible from saved public data, but
                        still a simulation.
  simulated_fees        published taker fee rate x simulated fills
  funding (by evidence):
    SETTLED_PUBLIC_RATE        the venue's own settled public rate x simulated units x
                               observed mark (the venue's price basis is not in the
                               history, so the AMOUNT is an estimate; the RATE is not)
    ACCOUNT_PAYMENT_CONFIRMED  an account's real funding payment: always 0 here (PAPER
                               has no account); kept as its own line so it can never
                               be confused with the above
    PUBLISHED_RATE_ESTIMATE    a rate published in the same funding period, not settled
    ASSUMED_CARRIED_FORWARD    no observation in that period: the last published rate
                               was carried (an assumption, not data)
  mark_to_market        open quantity at the last OBSERVED mid (unrealized)
Layers (they replace Phase 8's misleading `observed_part`, which mixed simulated
fills with observed data):
  public_data_reproducible  simulated price PnL - simulated fees + SETTLED_PUBLIC_RATE
                            (recomputable from the saved public answers; a simulation)
  assumption_dependent      PUBLISHED_RATE_ESTIMATE + ASSUMED_CARRIED_FORWARD
  account_verified          ACCOUNT_PAYMENT_CONFIRMED (0: no account, no real fills)
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
FUNDING_KINDS = ("SETTLED_PUBLIC_RATE", "ACCOUNT_PAYMENT_CONFIRMED",
                 "PUBLISHED_RATE_ESTIMATE", "ASSUMED_CARRIED_FORWARD")
# Phase 8 ledgers used shorter names; read them under the Phase 9 names.
ALIASES = dict(SETTLED_RATE="SETTLED_PUBLIC_RATE", PUBLISHED_ESTIMATE="PUBLISHED_RATE_ESTIMATE",
               PUBLISHED_CARRIED_FORWARD="ASSUMED_CARRIED_FORWARD")
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
        self.last_heartbeat = None
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
                reliability=[], marks=[], scenario_only=e.get("scenario_only", False),
                exposure_path=[[e["at"], 0.0]])
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
            p.setdefault("exposure_path", []).append([e["at"], self.exposure(p)])
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
                f = {x: e.get(x) for x in ("key", "side", "ts", "rate", "units", "price",
                                           "amount", "evidence", "basis")}
                f["evidence"] = ALIASES.get(f["evidence"], f["evidence"])
                p["funding"].append(f)
        elif k == "mark":
            p = self.positions[e["pos"]]
            p["marks"].append({x: v for x, v in e.items()
                               if x not in ("kind", "ledger_label", "pos")})
            del p["marks"][:-50]
        elif k == "reliability":
            self.positions[e["pos"]]["reliability"].append(e["note"])
        elif k == "heartbeat":
            self.last_heartbeat = e["at"]

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
        out = dict(simulated_price_pnl=price, simulated_fees=fees, funding=fund,
                   funding_total=total_f, realized_total=price - fees + total_f,
                   layers=dict(
                       public_data_reproducible=price - fees + fund["SETTLED_PUBLIC_RATE"],
                       assumption_dependent=fund["PUBLISHED_RATE_ESTIMATE"] +
                       fund["ASSUMED_CARRIED_FORWARD"],
                       account_verified=fund["ACCOUNT_PAYMENT_CONFIRMED"]),
                   mark_to_market=None if unvalued else unreal,
                   price_basis="SIMULATED_FILLS_ON_OBSERVED_QUOTES",
                   mark_basis="OBSERVED_MID")
        if p["strategy"] == "hybrid":
            out["components"] = dict(convergence_price_pnl=price, funding=total_f,
                                     shared_fees=fees, counted="fees once")
        return out

    @staticmethod
    def unhedged(p, until):
        """(seconds the position held unhedged units up to `until`, start of the
        current unhedged spell or None) from the exposure after every fill."""
        path = p.get("exposure_path") or []
        total, since = 0.0, None
        for (t0, x0), (t1, _) in zip(path, path[1:] + [[until, None]]):
            if abs(x0) > EPS:
                total += max(0.0, t1 - t0)
                since = since if since is not None else t0
            else:
                since = None
        return total, since

    @staticmethod
    def reconcile(p):
        """Each leg's open quantity recomputed from its fills; [] when they agree."""
        out = []
        for side, leg in p["legs"].items():
            q = sum(f["qty"] for f in leg["fills"] if f["action"] in ("open", "retry")) - \
                sum(f["qty"] for f in leg["fills"] if f["action"] not in ("open", "retry"))
            if abs(max(q, 0.0) - leg["qty"]) > 1e-6 * max(1.0, abs(q)):
                out.append(dict(side=side, from_fills=q, state_qty=leg["qty"]))
        return out

    @staticmethod
    def exposure(p):
        return p["legs"]["long"]["qty"] - p["legs"]["short"]["qty"]

    def risk(self, mids=None, at=None):
        """Open, unhedged risk of every position that is not CLOSED (never hidden)."""
        out = []
        for p in self.positions.values():
            if p["state"] in FINAL:
                continue
            x = self.exposure(p)
            m = (mids or {}).get(p["asset"])
            secs, since = self.unhedged(p, at if at is not None else 0.0)
            out.append(dict(id=p["id"], asset=p["asset"], strategy=p["strategy"],
                            unhedged_since=since, unhedged_seconds=secs,
                            state=p["state"], long_qty=p["legs"]["long"]["qty"],
                            short_qty=p["legs"]["short"]["qty"], unhedged_units=x,
                            unhedged_usd=None if m is None else abs(x) * m,
                            gross_usd=None if m is None else (p["legs"]["long"]["qty"] +
                                                              p["legs"]["short"]["qty"]) * m))
        return out

    def open_positions(self):
        return [p for p in self.positions.values() if p["state"] not in FINAL]
