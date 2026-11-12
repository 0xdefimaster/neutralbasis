"""ESTIMATED PAPER: an opt-in second opinion beside the strict PAPER gate.

PAPER ONLY. It never changes the strict gate (v2.gates.Gate), never opens a PAPER
position and never passes anything to the hybrid desk or any order path. Its results
are stored apart (`estimated`), labelled, and counted separately from strict results.

Switch: NB_V2_ESTIMATED_RFQ=1 (0/unset = off; anything else is an error). The project
has no live mode (CLAUDE.md). The evaluator is pure computation: it reads the
provider's rows/books/quotes and returns dicts; the runtime only attaches them to its
snapshot under `estimated` (no desk call, no order path).

What it may estimate (the strict gate stops these for missing EVIDENCE, not for a known
loss):
  EXECUTION_MODEL_UNVERIFIED   Variational RFQ: priced by v2.rfq_paper from the tiered
                               indicative quotes -> ESTIMATED_RFQ_FILL
  SETTLEMENT_EVIDENCE_MISSING  funding from the published rates of the same snapshot
                               -> ESTIMATED_FUNDING_PUBLISHED (never realized)
  RWA_REFERENCE_UNVERIFIED     only for ECONOMICALLY_RELATED or UNKNOWN references, with
                               a basis-risk stress -> ESTIMATED_REFERENCE_BASIS_RISK
                               (DIFFERENT_UNDERLYING stays rejected)
Everything else stays a rejection (scope/CONFIG_EXCLUDED, unknown fee, session closed or
unknown, stale book, identity, missing price history, liquidity at size). The strict
thresholds apply unchanged: break-even <= wide_max_breakeven_hours (24 h), funding
realization wide_funding_realization (1.0), wide_min_net, the combined stress; RFQ legs
must also survive the rfq_moderate stress.
"""
import math
import os
import statistics

from v2 import economics, observation, references, rfq_paper
from v2 import funding_evidence as fe
from v2.strategies import COST, DATA, EXECUTION, reason

ENV = "NB_V2_ESTIMATED_RFQ"
ESTIMABLE = ("EXECUTION_MODEL_UNVERIFIED", "SETTLEMENT_EVIDENCE_MISSING",
             "RWA_REFERENCE_UNVERIFIED")
LABELS = dict(EXECUTION_MODEL_UNVERIFIED=rfq_paper.LABEL,
              SETTLEMENT_EVIDENCE_MISSING="ESTIMATED_FUNDING_PUBLISHED",
              RWA_REFERENCE_UNVERIFIED="ESTIMATED_REFERENCE_BASIS_RISK")
ENTERED, REJECTED, STRICT = "ESTIMATED_PAPER_ENTRY", "ESTIMATED_REJECTED", "STRICT_ELIGIBLE"


class EstimatedError(RuntimeError):
    pass


def enabled(environ=None):
    env = os.environ if environ is None else environ
    raw = (env.get(ENV) or "").strip()
    if raw in ("", "0"):
        return False
    if raw == "1":
        return True
    raise EstimatedError("%s must be 0 or 1, not %r" % (ENV, raw))


def sigma_hourly(closes):
    """Hourly log-return volatility from {hour ts: close}, or None (< 24 returns)."""
    if not closes:
        return None
    pts = sorted(closes.items())
    r = [math.log(b / a) for (ta, a), (tb, b) in zip(pts, pts[1:])
         if a and b and a > 0 and b > 0 and tb - ta == 3600]
    return statistics.pstdev(r) if len(r) >= 24 else None


class EstimatedEvaluator:
    """`gate`: the strict v2.gates.Gate (used as is). `rfq_record(venue, asset)`: the
    Variational market record (quotes with updated_at) or None."""

    def __init__(self, gate, cfg, rfq_record=None, max_quote_age=rfq_paper.MAX_QUOTE_AGE_S):
        self.gate, self.cfg = gate, cfg
        self.provider = gate.provider
        self.rfq_record = rfq_record or (lambda v, a: None)
        self.max_quote_age = max_quote_age

    # -- one leg ------------------------------------------------------------------
    def leg_cost(self, side, venue, asset, row, size, at):
        """(round-trip dict, label, reason) for one leg at `size`."""
        book = self.provider.book(venue, asset)
        if book is not None and book.get("model") == "rfq_indicative":
            rec = self.rfq_record(venue, asset)
            if rec is None:
                return None, None, "RFQ_RECORD_MISSING"
            rt, why = rfq_paper.round_trip(rec, side == "long", size, at,
                                           self.max_quote_age, row.get("fee") or 0.0)
            if why:
                return None, None, why
            return dict(fees=rt["fees"], spread=rt["spread"], total=rt["total"],
                        entry_cost=rt["entry_cost"], exit_cost=rt["exit_cost"],
                        exit_spread=rt["close"]["spread_cost"],
                        band_bp=rt["open"]["band_bp"], tier=rt["open"]["tier"],
                        tier_status=rt["open"]["status"], quote_age_s=rt["open"][
                            "quote_age_s"], mid=rt["open"]["mid"]), rfq_paper.LABEL, None
        x = economics.leg(book, side == "long", size, row.get("fee"))
        if x.get("reason"):
            return None, None, x["reason"]
        return dict(fees=x["fees"], spread=x["spread"], total=x["fees"] + x["spread"],
                    entry_cost=x["entry_fee"] + x["entry_spread"],
                    exit_cost=x["exit_fee"] + x["exit_spread"],
                    exit_spread=x["exit_spread"], mid=x["mid"],
                    band_bp=0.0), "ORDERBOOK", None

    # -- one candidate ------------------------------------------------------------
    def evaluate(self, c, at, strategy):
        strict = self.gate.evaluate(c, at, strategy)
        out = dict(asset=c["asset"], long=c["long"], short=c["short"], strategy=strategy,
                   strict_eligible=bool(strict.get("eligible")),
                   strict_reasons=[r["code"] for r in strict.get("reasons") or []])
        if strict.get("eligible"):
            return dict(out, status=STRICT, assumptions=[], estimated=False)
        rows = self.provider.rows_by()
        codes = out["strict_reasons"]
        hard = [r for r in strict.get("reasons") or [] if r["code"] not in ESTIMABLE]
        ref = None
        if "RWA_REFERENCE_UNVERIFIED" in codes:
            ref = references.classify(rows[(c["long"], c["asset"])].get("exchange_id"),
                                      rows[(c["short"], c["asset"])].get("exchange_id"),
                                      c["asset"])
            if ref["cls"] == references.DIFFERENT_UNDERLYING:
                hard.append(reason(DATA, "DIFFERENT_UNDERLYING", "reference underlyings "
                                   "differ: not a hedge, not estimated"))
        if hard:
            return dict(out, status=REJECTED, first=hard[0]["code"],
                        reasons=[r["code"] for r in hard], estimated=False)
        if strategy == "convergence":
            return self.convergence(out, c, at, strict, ref)
        return self.funding(out, c, at, strategy, codes, ref)

    def scenario(self, c, at, strategy="funding"):
        """The same estimate for a pair with an OBSERVATION-ONLY leg (e.g. INIT on
        Hyperliquid native). Only CONFIG_EXCLUDED is set aside; every other rule holds.
        The status is SCENARIO_ONLY: never an estimated PAPER entry, never strict."""
        strict = self.gate.evaluate(c, at, strategy)
        codes = [r["code"] for r in strict.get("reasons") or []]
        other = [x for x in codes if x not in ESTIMABLE + ("CONFIG_EXCLUDED",)]
        base = dict(asset=c["asset"], long=c["long"], short=c["short"], strategy=strategy,
                    strict_eligible=False, strict_reasons=codes)
        if other:
            return dict(base, status="SCENARIO_REJECTED", first=other[0], reasons=other)
        r = self.funding(base, c, at, strategy, codes + ["SETTLEMENT_EVIDENCE_MISSING"],
                         None)
        r["scenario_result"] = r["status"]
        r["status"] = "SCENARIO_ONLY"
        r["label"] = "SCENARIO_ONLY: a leg is outside the PAPER universe"
        return r

    def assumptions(self, codes, ref):
        a = [dict(code=k, label=LABELS[k]) for k in ESTIMABLE if k in codes]
        if ref is not None:
            a.append(dict(code="REFERENCE_CLASS", label=ref["cls"],
                          unknown=ref["unknown"], different=ref["different"],
                          method_differences=ref["method_differences"]))
        return a

    def funding(self, out, c, at, strategy, codes, ref):
        rows = self.provider.rows_by()
        size = self.cfg["wide_size"]
        lr, sr = rows[(c["long"], c["asset"])], rows[(c["short"], c["asset"])]
        legs = {}
        for side, v, r in (("long", c["long"], lr), ("short", c["short"], sr)):
            cost, label, why = self.leg_cost(side, v, c["asset"], r, size, at)
            if why:
                code = ("INSUFFICIENT_LIQUIDITY" if why in (economics.LIQUIDITY,
                                                            rfq_paper.ABOVE)
                        else why)
                return dict(out, status=REJECTED, first=code, reasons=[code],
                            estimated=True, assumptions=self.assumptions(codes, ref))
            legs[side] = dict(cost, label=label)
        carry = fe.pair_carry(lr.get("rate"), sr.get("rate"))
        if carry is None:
            return dict(out, status=REJECTED, first="FUNDING_RATE_MISSING",
                        reasons=["FUNDING_RATE_MISSING"], estimated=True)
        from wide_paper import breakeven_refusal, funding_economics

        horizon = self.cfg["wide_horizon"]
        expected = size * carry * horizon
        fees = legs["long"]["fees"] + legs["short"]["fees"]
        spread = legs["long"]["spread"] + legs["short"]["spread"]
        sig = {side: sigma_hourly(self.provider.closes(v, c["asset"]))
               for side, v in (("long", c["long"]), ("short", c["short"]))}
        gap = abs(legs["short"]["mid"] / legs["long"]["mid"] - 1)
        reserve, reserve_basis = size * gap, "RESERVE_PROXY_CURRENT_GAP"
        econ = funding_economics(expected, horizon, fees, 0.0, spread, reserve,
                                 self.cfg["wide_funding_realization"])
        costs = dict(fees=fees, spread=spread, notional=size,
                     long=dict(exit_spread=legs["long"]["exit_spread"],
                               fees=legs["long"]["fees"], spread=legs["long"]["spread"]),
                     short=dict(exit_spread=legs["short"]["exit_spread"],
                                fees=legs["short"]["fees"],
                                spread=legs["short"]["spread"]))
        n = economics.net(costs, funding=econ["expected_funding"], reserve=reserve)
        st = economics.stress(costs, econ["expected_funding"], 0.0, reserve)
        reasons = []
        if carry <= 0:
            reasons.append("CARRY_NOT_POSITIVE")
        why = breakeven_refusal(econ, self.cfg)
        if why:
            reasons.append("BREAKEVEN")
        if n["net"] < self.cfg["wide_min_net"]:
            reasons.append("NET_BELOW_MIN")
        if next(x["net"] for x in st if x["name"] == "combined_moderate") < 0:
            reasons.append("STRESS_NEGATIVE")
        rfq_sides = [s for s in legs if legs[s]["label"] == rfq_paper.LABEL]
        rfq_st = None
        if rfq_sides:
            other = [s for s in legs if s not in rfq_sides]
            other_rt = sum(legs[s]["total"] for s in other)
            age = max(legs[s]["quote_age_s"] or 0 for s in rfq_sides)
            sg = max((sig[s] for s in rfq_sides if sig[s] is not None), default=None)
            rfq_st = rfq_paper.stress(n["net"], size, 2 * len(rfq_sides), other_rt, age, sg)
            if next(x["net"] for x in rfq_st if x["name"] == "rfq_moderate") < 0:
                reasons.append("RFQ_STRESS_NEGATIVE")
        basis_st = None
        if ref is not None:
            # Reference basis risk: the current mid gap repeated once more against the
            # position (the two references may move apart by that much again).
            basis_st = dict(name="reference_gap_x1", net=n["net"] - size * gap)
            if basis_st["net"] < 0:
                reasons.append("REFERENCE_STRESS_NEGATIVE")
        stresses = [x["net"] for x in st] + [x["net"] for x in rfq_st or [] if
                                             x["net"] is not None]
        if basis_st:
            stresses.append(basis_st["net"])
        return dict(out, status=ENTERED if not reasons else REJECTED,
                    first=reasons[0] if reasons else None, reasons=reasons,
                    estimated=True, label="ESTIMATED_PAPER (not a strict decision)",
                    assumptions=self.assumptions(codes, ref),
                    carry_hourly=carry, carry_8h=fe.per_8h(carry), net=n["net"],
                    funding=econ["expected_funding"], fees=fees, spread=spread,
                    reserve=reserve, reserve_basis=reserve_basis,
                    breakeven_h=econ.get("breakeven_hours"),
                    legs={s: dict(label=legs[s]["label"], tier=legs[s].get("tier"),
                                  tier_status=legs[s].get("tier_status"),
                                  band_bp=legs[s]["band_bp"],
                                  quote_age_s=legs[s].get("quote_age_s"))
                          for s in legs},
                    uncertainty=dict(net_min=min(stresses), net_max=n["net"],
                                     rfq_band_bp=sum(legs[s]["band_bp"] for s in legs),
                                     staleness_known=all(sig[s] is not None
                                                         for s in rfq_sides),
                                     funding_basis="published rates of one snapshot; "
                                     "persistence not evidenced"),
                    stress=st, rfq_stress=rfq_st, reference_stress=basis_st)

    def convergence(self, out, c, at, strict, ref):
        """Convergence needs hourly price history on both legs; Variational has none
        (no public candles), so an RFQ leg stays rejected. Otherwise the strict strategy
        runs on the real books with the reference blocker waived and a basis stress."""
        from v2.strategies import convergence as conv

        rows = self.provider.rows_by()
        legs = self.gate.legs(c, rows, at)
        if any((l.book or {}).get("model") == "rfq_indicative" for l in legs):
            return dict(out, status=REJECTED, first="PRICE_HISTORY_MISSING",
                        reasons=["PRICE_HISTORY_MISSING"], estimated=True,
                        note="no public price history for the RFQ venue")
        ev = conv.evaluate(legs[0], legs[1], self.cfg["wide_size"], self.cfg, at)
        codes = [r["code"] for r in ev["reasons"]]
        net = ev.get("net")
        if net is not None and ref is not None:
            gap = abs(economics.mid(legs[1].book) / economics.mid(legs[0].book) - 1)
            if net - self.cfg["wide_size"] * gap < 0:
                codes.append("REFERENCE_STRESS_NEGATIVE")
        return dict(out, status=ENTERED if not codes else REJECTED,
                    first=codes[0] if codes else None, reasons=codes, estimated=True,
                    assumptions=self.assumptions(out["strict_reasons"], ref), net=net)


def summary(results):
    """Per strategy: seen, strict eligible, estimated entries, rejected (first reason),
    estimated net assumptions and uncertainty of the entries."""
    import collections

    out = {}
    for s in sorted({r["strategy"] for r in results}):
        rs = [r for r in results if r["strategy"] == s]
        ent = [r for r in rs if r["status"] == ENTERED]
        out[s] = dict(
            seen=len(rs), strict_eligible=sum(1 for r in rs if r["status"] == STRICT),
            estimated_entries=len(ent),
            rejected=sum(1 for r in rs if r["status"] == REJECTED),
            rejected_first=dict(collections.Counter(
                r.get("first") for r in rs if r["status"] == REJECTED).most_common()),
            entry_assumptions=dict(collections.Counter(
                a["label"] for r in ent for a in r.get("assumptions") or []).most_common()),
            entry_net_total=sum(r.get("net") or 0 for r in ent),
            entry_net_min_total=sum((r.get("uncertainty") or {}).get("net_min") or 0
                                    for r in ent))
    return out
