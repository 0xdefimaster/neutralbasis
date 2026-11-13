r"""Full-market opportunity scan: DISCOVERY, ECONOMICS and EXECUTION READINESS kept apart
(Phase 13.4 / 13.8). PAPER research, offline: it reads one saved snapshot.

Universe: every identity group (v2.identity via the collector) listed on >= 2 venues of
the snapshot; every ordered (long, short) venue pair; three strategies, independently:
funding, price_gap (the strict `convergence` strategy) and hybrid. Nothing is traded.

Per row: time, asset, long/short venue, strategy, both legs' data age (UNKNOWN when the
snapshot holds no per-answer time; a Variational quote's age comes from updated_at),
funding rates and intervals, next payment, price gap, size, entry / exit cost, expected
net (EXPECTED_AT_ENTRY), stress (ENTRY_STRESS_ESTIMATE), break-even hours, evidence
level, the Phase 13 label (v2.opportunity, derived from the funnel / estimated state),
strict eligibility and rejection reasons.

Three reports, never mixed:
  DISCOVERY            rows whose own edge is positive (funding: carry > 0; price_gap:
                       short leg priced above long leg; hybrid: both)
  ECONOMIC             of those, expected net > 0 after estimated costs; and separately
                       stress > 0
  EXECUTION_READINESS  of those, STRICT eligible; and separately ESTIMATED candidates
plus first-rejection frequencies and per-venue counts.

Risk-adjusted score (RANKING ONLY, never a gate): min(expected, stress) x evidence
weight (both legs settled 1.0 / one published 0.5 / both published 0.25) x execution
weight (order books 1.0 / any indicative RFQ 0.5) x reference weight (crypto or exact
reference 1.0 / related or unknown 0.5).

Usage: python -m tools.v2_opportunity_scan --phase4 DIR --out DIR
"""
import argparse
import collections
import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wide_paper  # noqa: E402
from v2 import economics, estimated, funnel, opportunity, paths, references, rfq_paper  # noqa: E402
from v2 import storage  # noqa: E402
from v2 import funding_evidence as fe  # noqa: E402

STRATEGIES = (("funding", "funding"), ("price_gap", "convergence"), ("hybrid", "hybrid"))
SETTLED = (fe.MARKET_SETTLEMENT, fe.ACCOUNT_PAYMENT)



def models_report(disc):
    """How many discovered funding / hybrid rows stay positive under each model
    (ESTIMATED; a model whose net is None counts as UNKNOWN, never as positive)."""
    rows = [x for x in disc if x.get("funding_models")]
    out = dict(rows_with_models=len(rows), note="baseline = published rate held for "
               "the horizon (comparison only); persistence / adverse per "
               "v2/funding_expectation; ESTIMATED")
    for m in ("baseline", "persistence", "adverse"):
        nets = [(x["funding_models"].get("net") or {}).get(m) for x in rows]
        out[m] = dict(net_positive=sum(1 for v in nets if v is not None and v > 0),
                      net_unknown=sum(1 for v in nets if v is None))
    out["baseline_only_positive"] = sum(
        1 for x in rows if "BASELINE_ONLY_POSITIVE" in (x["funding_models"].get("flags") or []))
    cls = [x.get("funding_class") for x in rows if x.get("funding_class")]
    out["classes"] = {c: sum(1 for k in cls if c in k["classes"]) for c in (
        "DISCOVERED", "BASELINE_POSITIVE", "PERSISTENCE_POSITIVE", "ADVERSE_POSITIVE",
        "INSUFFICIENT_HISTORY")}
    out["highest"] = dict(collections.Counter(str(k["highest"]) for k in cls))
    out["profit_label"] = dict(collections.Counter(k["profit_label"] for k in cls))
    out["paper_entry_readiness"] = dict(collections.Counter(k["paper_entry_readiness"]
                                                            for k in cls))
    return out


def funding_class(fm, edge, strict, label):
    """Phase 15: classes of a funding / hybrid row (v2.funding_expectation.classify);
    reporting only. None when the evaluator never reached the funding economics."""
    if not fm or not fm.get("models"):
        return None
    from v2 import funding_expectation as fx
    ready = ("STRICT_ELIGIBLE" if strict.get("eligible") else
             "ESTIMATED_CANDIDATE" if label == "ESTIMATED_PAPER_CANDIDATE" else "NOT_READY")
    return fx.classify(edge, fm.get("net"), fm.get("legs"), ready)


def model_summary(fm):
    """Phase 15 P0-B: the three funding models of an ESTIMATED result, compact
    (ESTIMATED; None when the evaluator did not reach the funding economics)."""
    if not fm:
        return None
    return dict(evidence_class=fm.get("evidence_class"), hours=fm.get("hours"),
                net=fm.get("net"), cash={k: v.get("cash") for k, v in fm["models"].items()},
                confidence=fm.get("confidence"), flags=fm.get("flags"),
                legs={k: dict(rate_kind=v.get("rate_kind"),
                              payment_relation_known=v.get("payment_relation_known"),
                              fit_status=v.get("fit_status"))
                      for k, v in (fm.get("legs") or {}).items()})

def evidence_weight(xl, xs):
    st = [fe.venue_status(x) for x in (xl, xs)]
    n = sum(1 for s in st if s in SETTLED)
    return {2: 1.0, 1: 0.5}.get(n, 0.25 if all(s != fe.MISSING for s in st) else 0.0), st


def pairable(rows, assets=None):
    """{asset: [venues]} grouped by the VERIFIED underlying (v2.identity), never by the
    ticker alone: a symbol carried by two different underlyings is left out entirely,
    and a row without an underlying_id never pairs."""
    by_u = collections.defaultdict(list)
    for (v, a), row in rows.items():
        by_u[(row.get("underlying_id"), a)].append(v)
    conflicts = collections.Counter(a for (u, a) in by_u)
    return {a: vs for (u, a), vs in by_u.items() if u and len(vs) >= 2
            and conflicts[a] == 1 and (assets is None or a in assets)}


def scan(r, size=1000.0, assets=None):
    prov, at, gate = r["prov"], r["at"], r["gate"]
    rows = prov.rows_by()
    recs = {x["instrument"].market_id: x for x in r["col"].records.get("variational", [])}

    def rfq_record(v, a):
        row = rows.get((v, a)) or {}
        return recs.get(row.get("market_id")) if row.get("exchange_id") == "variational" \
            else None

    matched = pairable(rows, assets)
    cfg = dict(wide_paper.WIDE_DEFAULTS, wide_size=size)
    ev = estimated.EstimatedEvaluator(gate, cfg, rfq_record)
    out = []
    for asset, venues in sorted(matched.items()):
        for lv in venues:
            for sv in venues:
                if lv == sv:
                    continue
                lr, sr = rows[(lv, asset)], rows[(sv, asset)]
                lb, sb = prov.book(lv, asset), prov.book(sv, asset)
                carry = fe.pair_carry(lr.get("rate"), sr.get("rate"))
                gap = None
                try:
                    gap = 1e4 * (economics.mid(sb) / economics.mid(lb) - 1)
                except Exception:
                    pass
                ages = []
                for v, row in ((lv, lr), (sv, sr)):
                    if row.get("exchange_id") == "variational":
                        ages.append(rfq_paper.quote_age(rfq_record(v, asset), at))
                    else:
                        ages.append(None)            # no per-answer time in this snapshot
                ref = references.classify(lr.get("exchange_id"), sr.get("exchange_id"), asset) \
                    if lr.get("fine_class") in ("commodity", "fx", "index", "rwa", "rwa_mixed") \
                    else None
                ew, est_ev = evidence_weight(lr.get("exchange_id"), sr.get("exchange_id"))
                rfq = any((b or {}).get("model") == "rfq_indicative" for b in (lb, sb))
                for name, strat in STRATEGIES:
                    c = dict(asset=asset, long=lv, short=sv, strategy=strat)
                    try:
                        strict = gate.evaluate(c, at, strat)
                    except Exception as ex:
                        strict = dict(eligible=False, reasons=[dict(
                            code="EVAL_ERROR", category="DATA", text=str(ex)[:120])])
                    tr = funnel.trace(c, strict, rows, r["col"].groups, r["sha"], strat)
                    try:
                        e = ev.evaluate(c, at, strat)
                    except Exception as ex:
                        e = dict(status="ESTIMATED_REJECTED", first="EVAL_ERROR",
                                 reasons=[str(ex)[:120]])
                    legs_f = [dict(price_age_s=a_, rate_age_s=None,
                                   interval_s=row.get("interval"), price_time=None)
                              for a_, row in ((ages[0], lr), (ages[1], sr))]
                    fresh = opportunity.discovery_freshness(
                        "funding" if name == "funding" else "price_gap", legs_f)
                    if name == "funding":
                        edge = carry is not None and carry > 0
                    elif name == "price_gap":
                        edge = gap is not None and gap > 0
                    else:
                        edge = carry is not None and carry > 0 and gap is not None and gap > 0
                    lab = opportunity.label(trace=tr, est=e, freshness=fresh,
                                            carry=carry if name != "price_gap" else None,
                                            gap_signal=name != "funding" and bool(edge))
                    net = e.get("net")
                    stress = (e.get("uncertainty") or {}).get("net_min")
                    rw = 1.0 if ref is None or ref.get("hedge_ok") else 0.5
                    score = None if net is None else min(net, stress if stress is not None
                                                         else net) * ew * (0.5 if rfq else 1.0) * rw
                    out.append(dict(
                        at=at, asset=asset, long=lv, short=sv, strategy=name,
                        long_xid=lr.get("exchange_id"), short_xid=sr.get("exchange_id"),
                        data_age_s=dict(long=ages[0], short=ages[1],
                                        basis="Variational updated_at; others UNKNOWN in "
                                              "this snapshot"),
                        freshness=fresh["status"],
                        funding=dict(long_rate_h=lr.get("rate"), short_rate_h=sr.get("rate"),
                                     long_interval_s=lr.get("interval"),
                                     short_interval_s=sr.get("interval"),
                                     carry_8h=fe.per_8h(carry),
                                     evidence=est_ev),
                        gap_bp=gap, size=size, edge_positive=bool(edge),
                        entry_cost=e.get("entry_cost"), exit_cost=e.get("exit_cost"),
                        expected_net=net, stress_net=stress,
                        breakeven_h=e.get("breakeven_h"),
                        evidence_level=min(est_ev, key=lambda s: fe.RANK.get(s, -1)),
                        reference=None if ref is None else ref["cls"],
                        execution_model="RFQ_INDICATIVE" if rfq else "ORDERBOOK",
                        label=lab, strict_eligible=bool(strict.get("eligible")),
                        estimated_status=e.get("status"),
                        strict_first=tr.get("first_rejection"),
                        estimated_first=e.get("first"),
                        rejections=sorted(set(tr.get("canonical_codes") or []) |
                                          set(e.get("reasons") or [])),
                        risk_adjusted_score=score,
                        funding_models=model_summary(e.get("funding_models")),
                        funding_class=funding_class(e.get("funding_models"), bool(edge),
                                                    strict, lab)))
    return out, matched


def report(rows, matched):
    disc = [x for x in rows if x["edge_positive"]]
    counts = dict(
        unique_instruments=sum(len(v) for v in matched.values()),
        unique_reference_assets=len(matched),
        unique_venue_pairs=len({frozenset((x["long"], x["short"])) for x in rows}),
        strategy_direction_rows=len(rows),
        raw_positive_edge=len(disc),
        expected_net_positive=sum(1 for x in disc if (x["expected_net"] or 0) > 0),
        stress_positive=sum(1 for x in disc if (x["stress_net"] or 0) > 0),
        estimated_paper_candidates=sum(1 for x in disc
                                       if x["label"] == "ESTIMATED_PAPER_CANDIDATE"),
        simulated_paper_positions=None,
        simulated_paper_positions_note="the scanner never simulates; positions live in "
                                       "the ESTIMATED ledger (v2.paper_ledger)",
        strict_paper_eligible=sum(1 for x in disc if x["strict_eligible"]))
    econ = [x for x in disc if (x["expected_net"] or 0) > 0]
    stress = [x for x in disc if (x["stress_net"] or 0) > 0]
    strict = [x for x in disc if x["strict_eligible"]]
    est = [x for x in disc if x["label"] == "ESTIMATED_PAPER_CANDIDATE"]
    by = lambda xs, k: dict(collections.Counter(x[k] for x in xs).most_common())  # noqa: E731
    venues = collections.Counter()
    for x in disc:
        venues[x["long"]] += 1
        venues[x["short"]] += 1
    first = collections.Counter()
    for x in disc:
        first[x["strict_first"] or "-"] += 1
    est_first = collections.Counter(x["estimated_first"] or "-" for x in disc
                                    if x["label"] != "ESTIMATED_PAPER_CANDIDATE")
    ranked = sorted([x for x in disc if x["risk_adjusted_score"] is not None],
                    key=lambda x: -x["risk_adjusted_score"])[:15]
    return dict(
        counts=counts,
        instruments_matched=sum(len(v) for v in matched.values()),
        assets_on_2plus_venues=len(matched),
        rows=len(rows),
        DISCOVERY=dict(total=len(disc), by_strategy=by(disc, "strategy"),
                       by_label=by(disc, "label"), by_freshness=by(disc, "freshness")),
        ECONOMIC=dict(expected_net_positive=len(econ), by_strategy=by(econ, "strategy"),
                      stress_positive=len(stress),
                      stress_positive_by_strategy=by(stress, "strategy"),
                      funding_models=models_report(disc)),
        EXECUTION_READINESS=dict(strict_paper_eligible=len(strict),
                                 estimated_paper_candidates=len(est),
                                 estimated_by_strategy=by(est, "strategy")),
        research_only=sum(1 for x in disc if x["label"] in (
            "WATCHLIST", "DATA_STALE", "BLOCKED_EXECUTION_MODEL", "DISCOVERED")),
        venues_in_discovered=dict(venues.most_common()),
        strict_first_rejection=dict(first.most_common(12)),
        estimated_first_rejection=dict(est_first.most_common(12)),
        top_risk_adjusted=[{k: x[k] for k in (
            "asset", "long", "short", "strategy", "expected_net", "stress_net",
            "risk_adjusted_score", "label", "evidence_level", "execution_model",
            "reference", "estimated_first")} for x in ranked])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase4", default=str(paths.bench("v2-phase4-2026-11-12")))
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", type=float, default=1000.0)
    a = ap.parse_args()
    from tools import v2_forensics as vf

    out = storage.check_dir(a.out)
    out.mkdir(parents=True, exist_ok=True)
    r = vf.replay(Path(a.phase4))
    rows, matched = scan(r, a.size)
    with gzip.open(out / "opportunities.jsonl.gz", "wt", encoding="utf-8") as f:
        for x in rows:
            f.write(json.dumps(x, default=str) + "\n")
    rep = dict(snapshot=a.phase4, snapshot_sha=r["sha"], at=r["at"], size=a.size,
               note="ESTIMATED research; no verified execution; data ages UNKNOWN for "
                    "order-book legs in this snapshot", **report(rows, matched))
    (out / "opportunity_report.json").write_text(json.dumps(rep, indent=1, default=str),
                                                 encoding="utf-8")
    print(json.dumps(rep, indent=1, default=str))


if __name__ == "__main__":
    main()
