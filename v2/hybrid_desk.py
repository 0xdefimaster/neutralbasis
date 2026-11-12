"""The PAPER hybrid position: one two-leg position that earns funding and the
convergence of the price gap, with one state machine and one set of costs.

States:
  OPENING            legs being filled
  HEDGED_OPEN        both legs open with equal quantities
  PARTIAL_EXPOSURE   legs unequal (an unwind could not finish); the excess is retried
                     every cycle and the position is never treated as hedged meanwhile
  EXIT_PENDING       closing; retried until both legs are flat
  RECOVERY_PENDING   a failed entry is being unwound; retried until every leg is flat
  CLOSED             both legs flat after an exit
  LEG_RECOVERY       both legs flat after a failed entry (never a win)
  FAILED             nothing filled
A position is CLOSED or LEG_RECOVERY only when no leg holds any quantity. Every book
read goes through `_book`: a venue error or a missing book never raises inside the
desk, it leaves the position pending.

Exit reasons are evaluated independently every cycle, in this priority:
  STOP > CONVERGED > FUNDING_REVERSED > MAX_HOLD
When several fire, the first is the close reason and the others are recorded as
secondary; the position closes ONCE, so fees, spread and price PnL are booked once.

Ledger (append-only per position, each event under a unique key):
  fee, price                   open / unwind / close fills
  funding                      a settled payment valued at the venue's settlement price
  funding_estimated            a settled rate valued at the entry price (estimate)
pnl(): realized net = price + funding - fees; funding_estimated reported apart.
State is saved atomically (temp file + replace) after every change; on restart every
non-final position resumes where it was.
"""
import json
import math
import os
import time
import uuid
from pathlib import Path

from bot import FILL_TOLERANCE
from v2 import economics, funding_accounting, ops

OPENING, HEDGED_OPEN, EXIT_PENDING = "OPENING", "HEDGED_OPEN", "EXIT_PENDING"
PARTIAL_EXPOSURE, RECOVERY_PENDING = "PARTIAL_EXPOSURE", "RECOVERY_PENDING"
CLOSED, LEG_RECOVERY, FAILED = "CLOSED", "LEG_RECOVERY", "FAILED"
ACTIVE = (OPENING, HEDGED_OPEN, EXIT_PENDING, PARTIAL_EXPOSURE, RECOVERY_PENDING)
EXIT_PRIORITY = ("STOP", "CONVERGED", "FUNDING_REVERSED", "MAX_HOLD")
EPS = 1e-12


def ioc(book_side, qty, buy, limit):
    """(filled qty, average price) taking levels (best first) without crossing limit."""
    got = cost = 0.0
    for p, q in book_side or []:
        if (p > limit) if buy else (p < limit):
            break
        take = min(qty - got, q)
        got += take
        cost += take * p
        if got >= qty * (1 - 1e-12):
            break
    return (got, cost / got) if got > 0 else (0.0, None)


class HybridDesk:
    def __init__(self, path, provider, cfg, clock=time.time, tolerance=FILL_TOLERANCE):
        self.path = Path(path)
        if str(self.path.resolve()).lower().startswith("e:"):
            raise ValueError("hybrid desk state must not be on E:")
        self.provider, self.cfg, self.clock, self.tol = provider, cfg, clock, tolerance
        self.positions = {}
        self.load()

    # -- persistence ----------------------------------------------------------------------
    def save(self):
        ops.atomic_write(self.path, json.dumps(self.positions, indent=1, default=str))

    def load(self):
        if self.path.exists():
            self.positions = json.loads(self.path.read_text(encoding="utf-8"))
        for p in self.positions.values():
            if p["state"] in (OPENING, RECOVERY_PENDING):
                self._recover(p, p.get("recovery_reason") or "restart while opening")
            elif p["state"] == EXIT_PENDING:
                self._close(p)
            elif p["state"] == PARTIAL_EXPOSURE:
                self._rebalance(p)
        if self.positions:
            self.save()

    # -- safe reads / risk ------------------------------------------------------------------
    def _book(self, p, venue):
        try:
            return self.provider.book(venue, p["asset"])
        except Exception as ex:
            p.setdefault("errors", []).append(dict(at=self.clock(), venue=venue,
                                                   error=str(ex)[:200]))
            del p["errors"][:-20]
            return None

    @staticmethod
    def open_qty(p, side):
        leg = p["legs"].get(side)
        return leg["qty"] if leg and leg.get("open") else 0.0

    def exposure(self, p):
        """Signed open units not hedged: long open quantity minus short open quantity."""
        return self.open_qty(p, "long") - self.open_qty(p, "short")

    def risk(self):
        """Every non-final position with its open legs and unhedged units."""
        return [dict(id=p["id"], asset=p["asset"], state=p["state"],
                     long=self.open_qty(p, "long"), short=self.open_qty(p, "short"),
                     exposure=self.exposure(p))
                for p in self.positions.values() if p["state"] in ACTIVE]

    # -- ledger -----------------------------------------------------------------------------
    @staticmethod
    def book_event(p, key, kind, amount, **info):
        if key in p["ledger_keys"]:
            return False
        p["ledger_keys"].append(key)
        p["ledger"].append(dict(key=key, kind=kind, amount=amount, **info))
        return True

    @staticmethod
    def pnl(p):
        out = dict(price_pnl=0.0, funding=0.0, funding_estimated=0.0, fees=0.0)
        for e in p["ledger"]:
            if e["kind"] == "fee":
                out["fees"] -= e["amount"]
            elif e["kind"] == "price":
                out["price_pnl"] += e["amount"]
            elif e["kind"] == "funding":
                out["funding"] += e["amount"]
            elif e["kind"] == "funding_estimated":
                out["funding_estimated"] += e["amount"]
        out["net"] = sum(e["amount"] for e in p["ledger"] if e["kind"] != "funding_estimated")
        out["net_with_estimates"] = out["net"] + out["funding_estimated"]
        return out

    # -- fills ------------------------------------------------------------------------------
    def _take(self, p, side, qty, tag):
        """Close `qty` of a leg at what its book takes now; books price PnL and fee.
        Returns the quantity filled (0 when the book is missing)."""
        leg = p["legs"][side]
        qty = min(qty, leg["qty"]) if leg.get("open") else 0.0
        if qty <= EPS:
            return 0.0
        book = self._book(p, leg["venue"])
        if not book:
            return 0.0
        buy = side == "short"
        got, px = ioc(book.get("asks") if buy else book.get("bids"), qty, buy,
                      math.inf if buy else -math.inf)
        if got <= 0:
            return 0.0
        sign = 1 if side == "long" else -1
        n = len(p["ledger"])
        self.book_event(p, "%s:%s:%d" % (tag, side, n), "price", got * (px - leg["entry"]) * sign)
        self.book_event(p, "%s:%s:fee:%d" % (tag, side, n), "fee", -got * px * leg["fee_rate"])
        leg["qty"] -= got
        if leg["qty"] <= EPS:
            leg["qty"], leg["open"] = 0.0, False
        leg.setdefault("history", []).append([self.clock(), leg["qty"]])
        return got

    def open(self, c, evaluation, size):
        """Open a hybrid on the real books. Returns the position (any end state)."""
        long_v, short_v, asset = c["long"], c["short"], c["asset"]
        rows = self.provider.rows_by()
        p = dict(id="h" + uuid.uuid4().hex, asset=asset, state=OPENING,
                 opened=self.clock(), legs={}, ledger=[], ledger_keys=[],
                 baseline=evaluation.get("baseline"), mad=evaluation.get("mad"),
                 entry_dev=evaluation.get("deviation"),
                 expected=dict(funding=evaluation.get("funding"),
                               basis=evaluation.get("basis"), net=evaluation.get("net")),
                 close_reason=None, secondary=[], weak_since=None,
                 max_hold_h=self.cfg["wide_conv_max_hold_hours"])
        self.positions[p["id"]] = p
        self.save()
        first = self._fill(p, "long", long_v, size, rows, qty=None)
        if first is None:
            p["state"], p["close_reason"] = FAILED, "first leg did not fill"
            self.save()
            return p
        second = self._fill(p, "short", short_v, size, rows, qty=first)
        if second is None:
            self._recover(p, "second leg did not fill")
            return p
        self._rebalance(p)
        return p

    def _fill(self, p, side, venue, size, rows, qty):
        book = self._book(p, venue)
        if not book or not book.get("bids") or not book.get("asks"):
            return None
        mid = economics.mid(book)
        want = qty if qty is not None else size / mid
        buy = side == "long"
        limit = mid * (1 + self.tol if buy else 1 - self.tol)
        got, px = ioc(book["asks"] if buy else book["bids"], want, buy, limit)
        if got <= 0:
            return None
        fee_rate = rows[(venue, p["asset"])]["fee"]
        p["legs"][side] = dict(venue=venue, side=side, qty=got, entry=px, mid=mid,
                               fee_rate=fee_rate, open=True,
                               exchange_id=rows[(venue, p["asset"])].get("exchange_id"),
                               history=[[self.clock(), got]])
        self.book_event(p, "open:%s:fee" % side, "fee", -got * px * fee_rate)
        self.save()
        return got

    def _rebalance(self, p):
        """Unwind the larger leg down to the smaller one; HEDGED_OPEN only when equal."""
        x = self.exposure(p)
        if abs(x) > EPS:
            self._take(p, "long" if x > 0 else "short", abs(x), "unwind")
        p["state"] = HEDGED_OPEN if abs(self.exposure(p)) <= EPS else PARTIAL_EXPOSURE
        self.save()

    def _recover(self, p, why):
        """Flatten every leg of a failed entry. LEG_RECOVERY only when all are flat."""
        p["recovery_reason"] = why
        for side in ("long", "short"):
            if self.open_qty(p, side) > EPS:
                self._take(p, side, self.open_qty(p, side), "recover")
        if any(self.open_qty(p, s) > EPS for s in ("long", "short")):
            p["state"] = RECOVERY_PENDING
        else:
            p["state"], p["close_reason"] = LEG_RECOVERY, why
            p["closed"] = self.clock()
        self.save()

    # -- funding ------------------------------------------------------------------------------
    def settle(self, p, at=None):
        """Book every settled public payment since the position opened, once each;
        realized only with the venue's settlement price (v2.funding_accounting)."""
        at = at or self.clock()
        for side, leg in p["legs"].items():
            try:
                rows = self.provider.settled(leg["venue"], p["asset"], at) or []
            except Exception:
                rows = []
            for x in rows:
                if p["opened"] < x["ts"] <= min(at, p.get("closed") or at):
                    units = self.qty_at(leg, x["ts"])
                    if units <= EPS:
                        continue
                    key, kind, amount, info = funding_accounting.payment_event(
                        side, units, leg["entry"], x, leg.get("exchange_id"))
                    self.book_event(p, key, kind, amount, units=units, **info)

    @staticmethod
    def qty_at(leg, ts):
        """The leg's open quantity just before `ts` (payments use the size held then)."""
        q = 0.0
        for t, qty in leg.get("history") or [[0, leg["qty"]]]:
            if t < ts:
                q = qty
        return q

    # -- exits ----------------------------------------------------------------------------------
    def exit_reasons(self, p, at=None):
        at = at or self.clock()
        lb = self._book(p, p["legs"]["long"]["venue"])
        sb = self._book(p, p["legs"]["short"]["venue"])
        out = []
        if lb and sb and lb.get("bids") and sb.get("bids") and p.get("baseline") is not None:
            gap = math.log(economics.mid(sb) / economics.mid(lb))
            band = self.cfg["wide_conv_exit_bps"] / 1e4
            stop = (p["mad"] or 0) * 6
            if stop and gap - p["baseline"] >= stop + (p.get("entry_dev") or 0):
                out.append("STOP")
            if gap <= p["baseline"] + band:
                out.append("CONVERGED")
        rows = self.provider.rows_by()
        r_l = rows.get((p["legs"]["long"]["venue"], p["asset"]))
        r_s = rows.get((p["legs"]["short"]["venue"], p["asset"]))
        if r_l and r_s:
            carry = r_s["rate"] - r_l["rate"]
            if carry < 0:
                p["weak_since"] = p.get("weak_since") or at
                if at - p["weak_since"] >= self.cfg["wide_weak_minutes"] * 60:
                    out.append("FUNDING_REVERSED")
            else:
                p["weak_since"] = None
        if at - p["opened"] >= p["max_hold_h"] * 3600:
            out.append("MAX_HOLD")
        return [r for r in EXIT_PRIORITY if r in out]

    def _close(self, p):
        """Close what both books take now: first any excess on one leg, then the same
        quantity on both legs. CLOSED only when both legs are flat."""
        p["state"] = EXIT_PENDING
        x = self.exposure(p)
        if abs(x) > EPS:
            self._take(p, "long" if x > 0 else "short", abs(x), "close")
        lo, sh = p["legs"]["long"], p["legs"]["short"]
        lb, sb = self._book(p, lo["venue"]), self._book(p, sh["venue"])
        room = min(self.open_qty(p, "long"), self.open_qty(p, "short"),
                   sum(q for _, q in (lb or {}).get("bids") or []),
                   sum(q for _, q in (sb or {}).get("asks") or []))
        if room > EPS:
            got = self._take(p, "long", room, "close")
            if got > EPS:
                self._take(p, "short", got, "close")
        if self.open_qty(p, "long") <= EPS and self.open_qty(p, "short") <= EPS:
            p["closed"] = self.clock()
            self.settle(p)
            p["state"] = CLOSED
        self.save()
        return p["state"]

    def close(self, p, reasons):
        if p["close_reason"] is None:
            p["close_reason"], p["secondary"] = reasons[0], list(reasons[1:])
        return self._close(p)

    # -- cycle ----------------------------------------------------------------------------------
    def cycle(self):
        for p in list(self.positions.values()):
            try:
                if p["state"] == HEDGED_OPEN:
                    self.settle(p)
                    reasons = self.exit_reasons(p)
                    if reasons:
                        self.close(p, reasons)
                elif p["state"] == EXIT_PENDING:
                    self._close(p)
                elif p["state"] in (OPENING, RECOVERY_PENDING):
                    self._recover(p, p.get("recovery_reason") or "entry did not finish")
                elif p["state"] == PARTIAL_EXPOSURE:
                    self._rebalance(p)
            except Exception as ex:   # one position never stops the others
                p.setdefault("errors", []).append(dict(at=self.clock(), error=str(ex)[:200]))
        self.save()

    def open_positions(self):
        return [p for p in self.positions.values() if p["state"] in ACTIVE]
