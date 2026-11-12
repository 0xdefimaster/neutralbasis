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
gate stops at its first failing stage, so checks it never reached for an estimable
reason (book present and fresh) are run here again (`critical`). The strict
thresholds apply unchanged: break-even <= wide_max_breakeven_hours (24 h), funding
realization wide_funding_realization (1.0), wide_min_net, the combined stress; RFQ legs
must also survive the rfq_moderate stress.

Status ESTIMATED_ENTRY_SIGNAL means "an entry would be signalled"; it is NOT a position.
A position exists only in the separate ESTIMATED PAPER ledger (v2.paper_ledger), after
a simulated execution attempt.

Strategies are kept apart (Phase 8):
  funding      published carry over the funding horizon; the constant-rate assumption is
               reported with history / regime / sign-flip / payment-schedule scenarios
               (v2.funding_scenarios)
  convergence  the strict convergence strategy on real books (no RFQ: no history)
  hybrid       a funding component (published carry over the convergence's expected
               hold) and a convergence component (capture), on ONE round trip and ONE
               reserve; without a convergence component it is not a hybrid
               (HYBRID_CONVERGENCE_UNAVAILABLE) - the funding strategy covers that case
"""
import math
import os
import statistics

from v2 import economics, marketdata, observation, references, rfq_paper
from v2 import funding_evidence as fe
from v2 import funding_scenarios as fsc
from v2.strategies import COST, DATA, EXECUTION, reason

ENV = "NB_V2_ESTIMATED_RFQ"
ESTIMABLE = ("EXECUTION_MODEL_UNVERIFIED", "SETTLEMENT_EVIDENCE_MISSING",
             "RWA_REFERENCE_UNVERIFIED")
LABELS = dict(EXECUTION_MODEL_UNVERIFIED=rfq_paper.LABEL,
              SETTLEMENT_EVIDENCE_MISSING="ESTIMATED_FUNDING_PUBLISHED",
              RWA_REFERENCE_UNVERIFIED="ESTIMATED_REFERENCE_BASIS_RISK")
ENTERED, REJECTED, STRICT = ("ESTIMATED_ENTRY_SIGNAL", "ESTIMATED_REJECTED",
                             "STRICT_ELIGIBLE")


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
        crit = self.critical(c, rows, at)
        if crit:
            return dict(out, status=REJECTED, first=crit[0], reasons=crit, estimated=False,
                        note="checks the strict gate never reached (it stopped earlier)")
        if strategy == "convergence":
            return self.convergence(out, c, at, strict, ref)
        if strategy == "hybrid":
            return self.hybrid(out, c, at, ref)
        return self.funding(out, c, at, strategy, codes, ref)

    def critical(self, c, rows, at):
        """Book present and fresh on every order-book leg. The strict gate checks this
        only after its static stage passes, so a candidate it stopped for an estimable
        reason was never checked; the estimate must not skip it. RFQ legs are checked
        by quote age in leg_cost."""
        out = []
        for side in ("long", "short"):
            v = c[side]
            book = self.provider.book(v, c["asset"])
            if book is None:
                out.append("BOOK_MISSING")
            elif book.get("model") != "rfq_indicative" and marketdata.stale(
                    book, rows[(v, c["asset"])].get("exchange_id"), at):
                out.append("BOOK_STALE")
        return out

    def leg_inputs(self, v, asset, row, at):
        """Rate, interval, settled history and next payment of one leg (scenarios)."""
        try:
            hist = self.provider.settled(v, asset, at) or []
        except Exception:
            hist = []
        hist = [dict(p, per_hour=False) for p in hist
                if fe.kind_status(p.get("kind")) in fe.SETTLED and fe.is_number(p.get("rate"))]
        return dict(rate=row.get("rate"), interval=row.get("interval"), history=hist,
                    next_funding=row.get("next_funding"))

    def scenario(self, c, at, strategy="funding"):
        """The same estimate for a pair with an OBSERVATION-ONLY leg (e.g. INIT on
        Hyperliquid native). Only CONFIG_EXCLUDED is set aside; every other rule holds.
        The status is SCENARIO_ONLY: never an estimated PAPER entry, never strict."""
        strict = self.gate.evaluate(c, at, strategy)
        codes = [r["code"] for r in strict.get("reasons") or []]
        if codes == ["CONFIG_EXCLUDED"] * len(codes):
            # The strict gate stops at CONFIG_EXCLUDED: run its remaining static checks
            # (RFQ, fee, reference/session, settlement) with the observation-only
            # identity masked, so none of them is skipped for the scenario.
            rows = self.provider.rows_by()
            masked = {k: dict(v, exchange_id=None) if v.get("exchange_id") and not
                      observation.is_paper_venue(v["exchange_id"]) else v
                      for k, v in rows.items()}
            codes = codes + [r["code"] for r in self.gate.static(
                dict(c, strategy=strategy), masked, at)]
        other = [x for x in codes if x not in ESTIMABLE + ("CONFIG_EXCLUDED",)]
        base = dict(asset=c["asset"], long=c["long"], short=c["short"], strategy=strategy,
                    strict_eligible=False, strict_reasons=codes)
        if other:
            return dict(base, status="SCENARIO_REJECTED", first=other[0], reasons=other)
        crit = self.critical(c, self.provider.rows_by(), at)
        if crit:
            return dict(base, status="SCENARIO_REJECTED", first=crit[0], reasons=crit)
        if strategy == "convergence":
            r = self.convergence(base, c, at, strict, None)
        elif strategy == "hybrid":
            r = self.hybrid(base, c, at, None)
        else:
            r = self.funding(base, c, at, strategy,
                             codes + ["SETTLEMENT_EVIDENCE_MISSING"], None)
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
        # The published rate is assumed constant over the horizon: say so, and show
        # what history, a regime change, a sign flip and the payment times would do.
        fs = fsc.scenarios(size, horizon, self.leg_inputs(c["long"], c["asset"], lr, at),
                           self.leg_inputs(c["short"], c["asset"], sr, at), at,
                           fees + spread + reserve)
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
                                     "persistence not evidenced",
                                     constant_rate_assumed=True,
                                     funding_scenario_min=fs["net_min"]),
                    stress=st, rfq_stress=rfq_st, reference_stress=basis_st,
                    funding_scenarios=fs,
                    components=dict(funding=econ["expected_funding"], convergence=None,
                                    shared_costs=dict(fees=fees, spread=spread,
                                                      counted="once"),
                                    reserve=reserve))

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
                    assumptions=self.assumptions(out["strict_reasons"], ref), net=net,
                    baseline=ev.get("baseline"), mad=ev.get("mad"),
                    deviation=ev.get("deviation"), expected_hold_h=ev.get("expected_hold_h"),
                    components=None if net is None else dict(
                        funding=ev.get("funding"), convergence=ev.get("basis"),
                        shared_costs=dict(fees=ev.get("fees"), spread=ev.get("spread"),
                                          counted="once"), reserve=ev.get("reserve")))

    def hybrid(self, out, c, at, ref):
        """One position, two components on one round trip and one reserve:
          convergence  capture from the strict convergence strategy (real books, 7 d
                       of matched hourly closes, past episodes)
          funding      published carry x size x the convergence's expected hold x
                       realization (ESTIMATED_FUNDING_PUBLISHED); the convergence net
                       already holds the same carry, so the net is rebuilt from the
                       components here, never added to it
        Without a convergence component (RFQ leg, no history, gap not extreme) there is
        no hybrid: HYBRID_CONVERGENCE_UNAVAILABLE."""
        from v2.strategies import convergence as conv

        rows = self.provider.rows_by()
        legs = self.gate.legs(c, rows, at)
        a = self.assumptions(out["strict_reasons"], ref)
        if any((l.book or {}).get("model") == "rfq_indicative" for l in legs):
            return dict(out, status=REJECTED, first="HYBRID_CONVERGENCE_UNAVAILABLE",
                        reasons=["HYBRID_CONVERGENCE_UNAVAILABLE", "PRICE_HISTORY_MISSING"],
                        estimated=True, assumptions=a,
                        note="no public price history for the RFQ venue")
        size = self.cfg["wide_size"]
        cv = conv.evaluate(legs[0], legs[1], size, self.cfg, at)
        if "expected_hold_h" not in cv:
            codes = [r["code"] for r in cv["reasons"]] or ["NO_CONVERGENCE_COMPONENT"]
            return dict(out, status=REJECTED, first="HYBRID_CONVERGENCE_UNAVAILABLE",
                        reasons=["HYBRID_CONVERGENCE_UNAVAILABLE"] + codes, estimated=True,
                        assumptions=a)
        hold = cv["expected_hold_h"]
        carry = fe.pair_carry(legs[0].hourly, legs[1].hourly)
        if carry is None:
            return dict(out, status=REJECTED, first="FUNDING_RATE_MISSING",
                        reasons=["FUNDING_RATE_MISSING"], estimated=True, assumptions=a)
        funding_cash = size * carry * hold * self.cfg["wide_funding_realization"]
        gap = abs(economics.mid(legs[1].book) / economics.mid(legs[0].book) - 1)
        reserve = max(size * gap, cv.get("reserve") or 0.0)
        costs = cv["costs"]
        n = economics.net(costs, funding=funding_cash, basis=cv["basis"], reserve=reserve)
        st = economics.stress(costs, funding_cash, cv["basis"], reserve)
        reasons = []
        if n["net"] < self.cfg["wide_min_net"]:
            reasons.append("NET_BELOW_MIN")
        if next(x["net"] for x in st if x["name"] == "combined_moderate") < 0:
            reasons.append("STRESS_NEGATIVE")
        basis_st = None
        if ref is not None:
            basis_st = dict(name="reference_gap_x1", net=n["net"] - size * gap)
            if basis_st["net"] < 0:
                reasons.append("REFERENCE_STRESS_NEGATIVE")
        return dict(out, status=ENTERED if not reasons else REJECTED,
                    first=reasons[0] if reasons else None, reasons=reasons,
                    estimated=True, assumptions=a, net=n["net"], carry_hourly=carry,
                    carry_8h=fe.per_8h(carry), expected_hold_h=hold,
                    baseline=cv["baseline"], mad=cv["mad"], deviation=cv["deviation"],
                    components=dict(
                        funding=funding_cash, funding_basis="ESTIMATED_FUNDING_PUBLISHED "
                        "x expected hold %.1f h" % hold,
                        convergence=cv["basis"], capture_share=cv["capture_share"],
                        shared_costs=dict(fees=costs["fees"], spread=costs["spread"],
                                          counted="once"),
                        reserve=reserve, reserve_basis="max(funding gap proxy, "
                        "convergence 2 x MAD)"),
                    stress=st, reference_stress=basis_st,
                    uncertainty=dict(net_min=min([x["net"] for x in st] + (
                        [basis_st["net"]] if basis_st else [])), net_max=n["net"]))


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
