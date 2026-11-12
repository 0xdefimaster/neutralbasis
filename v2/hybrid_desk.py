"""The PAPER hybrid position: one two-leg position that earns funding and the
convergence of the price gap, with one state machine and one set of costs.

States:  OPENING -> HEDGED_OPEN | LEG_RECOVERY | FAILED
         HEDGED_OPEN -> EXIT_PENDING -> CLOSED  (EXIT_PENDING retried until the books
         take the whole remaining quantity; a leg is never closed alone)
Exit reasons are evaluated independently every cycle, in this priority:
  STOP              the gap widened past the hard stop (risk)
  CONVERGED         the gap is back within the exit band of its baseline (convergence)
  FUNDING_REVERSED  the published carry has been against the position for
                    `weak_minutes` (funding)
  MAX_HOLD          the hold limit
When several fire in the same cycle the first is the close reason and the others are
recorded as secondary; the position is closed ONCE, so fees, spread and price PnL
are booked once whatever triggered it.

Accounting is an append-only ledger per position, each event under a unique key:
  open:<leg> fee        close:<leg> fee and price PnL
  funding:<leg>:<ts>    one settled public payment (idempotent; a payment seen twice
                        is booked once)
net = sum(ledger amounts) = price_pnl + funding - fees.
Fills: immediate-or-cancel on the real book within the fill tolerance, the second leg
sized by the first; an unmatched part of the first leg is unwound at once; a second
leg that does not fill at all closes the first (LEG_RECOVERY, never a win).
State is saved atomically (temp file + replace) after every change; on restart an
OPENING position is recovered as LEG_RECOVERY and an EXIT_PENDING one keeps closing.
"""
import json
import math
import os
import time
import uuid
from pathlib import Path

from bot import FILL_TOLERANCE
from v2 import economics

OPENING, HEDGED_OPEN, EXIT_PENDING = "OPENING", "HEDGED_OPEN", "EXIT_PENDING"
CLOSED, LEG_RECOVERY, FAILED = "CLOSED", "LEG_RECOVERY", "FAILED"
ACTIVE = (OPENING, HEDGED_OPEN, EXIT_PENDING)
EXIT_PRIORITY = ("STOP", "CONVERGED", "FUNDING_REVERSED", "MAX_HOLD")


def ioc(book_side, qty, buy, limit):
    """(filled qty, average price) taking levels (best first) without crossing limit."""
    got = cost = 0.0
    for p, q in book_side:
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
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.positions, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, self.path)

    def load(self):
        if self.path.exists():
            self.positions = json.loads(self.path.read_text(encoding="utf-8"))
        for p in self.positions.values():
            if p["state"] == OPENING:
                # Crashed between legs: whatever filled is unhedged -> recover it.
                self._recover(p, "restart while opening")
            elif p["state"] == EXIT_PENDING:
                self._close(p)
        if self.positions:
            self.save()

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
        out = dict(price_pnl=0.0, funding=0.0, fees=0.0)
        for e in p["ledger"]:
            if e["kind"] == "fee":
                out["fees"] -= e["amount"]
            elif e["kind"] == "price":
                out["price_pnl"] += e["amount"]
            elif e["kind"] == "funding":
                out["funding"] += e["amount"]
        out["net"] = sum(e["amount"] for e in p["ledger"])
        return out

    # -- open -------------------------------------------------------------------------------
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
        first = self._fill(p, "long", long_v, asset, size, rows, qty=None)
        if first is None:
            p["state"], p["close_reason"] = FAILED, "first leg did not fill"
            self.save()
            return p
        second = self._fill(p, "short", short_v, asset, size, rows, qty=first)
        if second is None:
            self._recover(p, "second leg did not fill")
            return p
        if second < first * (1 - 1e-9):
            self._unwind(p, "long", first - second)
        p["state"] = HEDGED_OPEN
        self.save()
        return p

    def _fill(self, p, side, venue, asset, size, rows, qty):
        book = self.provider.book(venue, asset)
        if not book or not book.get("bids") or not book.get("asks"):
            return None
        mid = economics.mid(book)
        want = qty if qty is not None else size / mid
        buy = side == "long"
        limit = mid * (1 + self.tol if buy else 1 - self.tol)
        got, px = ioc(book["asks"] if buy else book["bids"], want, buy, limit)
        if got <= 0:
            return None
        fee_rate = rows[(venue, asset)]["fee"]
        p["legs"][side] = dict(venue=venue, side=side, qty=got, entry=px, mid=mid,
                               fee_rate=fee_rate, open=True)
        self.book_event(p, "open:%s:fee" % side, "fee", -got * px * fee_rate)
        self.save()
        return got

    def _unwind(self, p, side, qty):
        leg = p["legs"][side]
        book = self.provider.book(leg["venue"], p["asset"])
        buy = side == "short"
        got, px = ioc(book["asks"] if buy else book["bids"], qty, buy, -math.inf
                      if not buy else math.inf)
        if got <= 0:
            return
        sign = 1 if side == "long" else -1
        self.book_event(p, "unwind:%s:%d" % (side, len(p["ledger"])), "price",
                        got * (px - leg["entry"]) * sign)
        self.book_event(p, "unwind:%s:fee:%d" % (side, len(p["ledger"])), "fee",
                        -got * px * leg["fee_rate"])
        leg["qty"] -= got

    def _recover(self, p, why):
        for side, leg in p["legs"].items():
            if leg.get("open") and leg["qty"] > 0:
                self._close_leg(p, side, leg["qty"])
        p["state"], p["close_reason"] = LEG_RECOVERY, why
        p["closed"] = self.clock()
        self.save()

    # -- funding ------------------------------------------------------------------------------
    def settle(self, p, at=None):
        """Book every settled public payment since the position opened, once."""
        at = at or self.clock()
        for side, leg in p["legs"].items():
            rows = self.provider.settled(leg["venue"], p["asset"], at) or []
            sign = -1 if side == "long" else 1  # positive rate: longs pay, shorts receive
            for x in rows:
                if p["opened"] < x["ts"] <= min(at, p.get("closed") or at):
                    self.book_event(p, "funding:%s:%d" % (side, int(x["ts"])), "funding",
                                    sign * leg["qty"] * leg["entry"] * x["rate"],
                                    valuation="entry price")

    # -- exits ----------------------------------------------------------------------------------
    def exit_reasons(self, p, at=None):
        at = at or self.clock()
        lb = self.provider.book(p["legs"]["long"]["venue"], p["asset"])
        sb = self.provider.book(p["legs"]["short"]["venue"], p["asset"])
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

    def _close_leg(self, p, side, qty):
        leg = p["legs"][side]
        book = self.provider.book(leg["venue"], p["asset"])
        buy = side == "short"
        if not book:
            return 0.0
        got, px = ioc(book["asks"] if buy else book["bids"], qty, buy,
                      math.inf if buy else -math.inf)
        if got <= 0:
            return 0.0
        sign = 1 if side == "long" else -1
        n = len(p["ledger"])
        self.book_event(p, "close:%s:%d" % (side, n), "price", got * (px - leg["entry"]) * sign)
        self.book_event(p, "close:%s:fee:%d" % (side, n), "fee", -got * px * leg["fee_rate"])
        leg["qty"] -= got
        if leg["qty"] <= 1e-12:
            leg["open"] = False
        return got

    def _close(self, p):
        """Close what both books take now, matched on both legs; the rest waits."""
        p["state"] = EXIT_PENDING
        lo, sh = p["legs"]["long"], p["legs"]["short"]
        lb = self.provider.book(lo["venue"], p["asset"])
        sb = self.provider.book(sh["venue"], p["asset"])
        room = min(lo["qty"], sh["qty"],
                   sum(q for _, q in (lb or {}).get("bids") or []),
                   sum(q for _, q in (sb or {}).get("asks") or []))
        if room > 0:
            self._close_leg(p, "long", room)
            self._close_leg(p, "short", room)
        if not lo["open"] and not sh["open"]:
            self.settle(p)
            p["state"], p["closed"] = CLOSED, self.clock()
        self.save()
        return p["state"]

    def close(self, p, reasons):
        if p["close_reason"] is None:
            p["close_reason"], p["secondary"] = reasons[0], list(reasons[1:])
        return self._close(p)

    # -- cycle ----------------------------------------------------------------------------------
    def cycle(self):
        for p in list(self.positions.values()):
            if p["state"] == HEDGED_OPEN:
                self.settle(p)
                reasons = self.exit_reasons(p)
                if reasons:
                    self.close(p, reasons)
            elif p["state"] == EXIT_PENDING:
                self._close(p)
        self.save()

    def open_positions(self):
        return [p for p in self.positions.values() if p["state"] in ACTIVE]
