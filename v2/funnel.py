"""The opportunity funnel: every candidate gets one immutable trace from discovery to a
PAPER decision, so an opportunity a person sees elsewhere (arb.sh, a venue page) can
be found here with the exact stage and reason that stopped it.

Stages (in order):
  DISCOVERED -> MATCHED -> OBSERVED -> ECONOMIC_CANDIDATE -> EXECUTION_MODEL_CHECK
  -> PAPER_ELIGIBLE -> PAPER_OPENED

Outcomes (never merged):
  NOT_EVALUATED  stopped before the economics ran (identity, scope, evidence, session,
                 stale book). The net is UNKNOWN, not zero, not negative.
  FAILED         the economics ran but an input was missing or unusable inside it
                 (liquidity at size, settled history coverage, price history).
  UNPROFITABLE   the economics ran on complete inputs and the result is below the bar
                 (break-even, minimum net, stress, carry not persistent).
  RESEARCH_ONLY  the strategy passed but is marked research only.
  ELIGIBLE       may open a PAPER position.

Each trace records first_rejection, all_rejections (canonical code + internal code +
text, per leg), missing_evidence, source_hashes, confidence and next_action. The trace
id is a hash of the snapshot hash and the candidate, so it is stable for a replay.

The Potential Opportunity Radar (radar()) prices what a candidate WOULD be worth on
published rates and the current books at $1k/$5k/$10k, including observation-only
venues and RFQ quotes. It is labelled POTENTIAL or RESEARCH_ONLY and never makes
anything eligible.
"""
import hashlib
import json

from v2 import economics, funding_evidence as fe
from v2 import observation

STAGES = ("DISCOVERED", "MATCHED", "OBSERVED", "ECONOMIC_CANDIDATE",
          "EXECUTION_MODEL_CHECK", "PAPER_ELIGIBLE", "PAPER_OPENED")
OUTCOMES = ("NOT_EVALUATED", "FAILED", "UNPROFITABLE", "RESEARCH_ONLY", "ELIGIBLE")

# The master prompt's reason codes (2026-11-12), first and all.
CANONICAL = ("NOT_DISCOVERED", "IDENTITY_UNVERIFIED", "RWA_REFERENCE_UNVERIFIED",
             "SINGLE_VENUE_ONLY", "MISSING_FUNDING_HISTORY", "SETTLEMENT_EVIDENCE_MISSING",
             "SESSION_UNKNOWN", "BOOK_STALE", "INSUFFICIENT_LIQUIDITY", "NET_AFTER_COSTS",
             "NOT_IN_CANDIDATES", "CONFIG_EXCLUDED")
# Codes the gate uses that the list above does not name; kept as their own code
# (never folded into a code that means something else).
EXTRA = ("EXECUTION_MODEL_UNVERIFIED", "FEE_UNKNOWN", "SESSION_CLOSED", "BOOK_MISSING",
         "PRICE_HISTORY_MISSING", "NO_SIGNAL", "CONFLICT")
MAP = {
    "RWA_REFERENCE_UNVERIFIED": "RWA_REFERENCE_UNVERIFIED",
    "RWA_SESSION_UNKNOWN": "SESSION_UNKNOWN",
    "RWA_SESSION_CLOSED": "SESSION_CLOSED",
    "BOOK_STALE": "BOOK_STALE",
    "INSUFFICIENT_LIQUIDITY": "INSUFFICIENT_LIQUIDITY",
    "BOOK_CROSSED_OR_EMPTY": "BOOK_MISSING",
    "BOOK_MISSING": "BOOK_MISSING",
    "BREAKEVEN": "NET_AFTER_COSTS",
    "NET_BELOW_MIN": "NET_AFTER_COSTS",
    "STRESS_NEGATIVE": "NET_AFTER_COSTS",
    "CARRY_NOT_PERSISTENT": "NO_SIGNAL",
    "GAP_NOT_EXTREME": "NO_SIGNAL",
    "NO_EPISODES": "NO_SIGNAL",
    "PRICE_HISTORY_MISSING": "PRICE_HISTORY_MISSING",
    "EXECUTION_MODEL_UNVERIFIED": "EXECUTION_MODEL_UNVERIFIED",
    "FEE_UNKNOWN": "FEE_UNKNOWN",
}
# Reasons decided before the economics (gate.static and the stale-book check).
PRE_ECONOMICS = ("NOT_A_V2_ROW", "EXECUTION_MODEL_UNVERIFIED", "FEE_UNKNOWN",
                 "RWA_REFERENCE_UNVERIFIED", "RWA_SESSION_UNKNOWN", "RWA_SESSION_CLOSED",
                 "SETTLEMENT_EVIDENCE_MISSING", "BOOK_STALE")
NEXT_ACTION = {
    "CONFIG_EXCLUDED": "venue is outside the PAPER universe: observe it (observation_v3); "
                       "PAPER use needs a separate owner review",
    "NOT_DISCOVERED": "the asset is not listed on the venues read; check the snapshot time",
    "SINGLE_VENUE_ONLY": "only one PAPER venue lists it; the other leg is not in scope",
    "IDENTITY_UNVERIFIED": "find a venue-published anchor (class/name) for both legs",
    "CONFLICT": "same ticker, different underlying: never matched",
    "RWA_REFERENCE_UNVERIFIED": "record each venue's reference contract/index from its own "
                                "docs (e.g. NATGAS: Henry Hub month / roll) in the registry",
    "SESSION_UNKNOWN": "a venue must publish the session state",
    "SESSION_CLOSED": "re-evaluate in the US regular session",
    "SETTLEMENT_EVIDENCE_MISSING": "settled payments needed: TxFlow userFundings only with "
                                   "an authorized read-only address; Variational has no "
                                   "public settlement history (UNKNOWN)",
    "MISSING_FUNDING_HISTORY": "settled history shorter than the 7-day window; wait/backfill",
    "EXECUTION_MODEL_UNVERIFIED": "RFQ fill model from recorded firm quotes (Variational)",
    "FEE_UNKNOWN": "venue fee must be published (RISEx: per-account only)",
    "BOOK_STALE": "fresh book needed (stream or re-read)",
    "BOOK_MISSING": "no usable book",
    "INSUFFICIENT_LIQUIDITY": "smaller size or deeper venue",
    "PRICE_HISTORY_MISSING": "hourly closes needed on both legs (7 days)",
    "NET_AFTER_COSTS": "economics complete; not worth it at this size/horizon",
    "NO_SIGNAL": "no persistent carry / extreme gap now",
    "NOT_IN_CANDIDATES": "ranker did not produce this pair (rates/filters)",
}


def sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def trace_id(snapshot_sha, strategy, asset, long, short):
    return "nb-" + sha([snapshot_sha, strategy, asset, long, short])[:16]


def canonical(code, venue_status=None):
    """Canonical code for an internal reason code. A settlement shortfall on a venue
    that does publish settled history is a history gap, not missing evidence."""
    if code == "SETTLEMENT_EVIDENCE_MISSING":
        return ("MISSING_FUNDING_HISTORY" if fe.is_settled(venue_status)
                else "SETTLEMENT_EVIDENCE_MISSING")
    return MAP.get(code, code)


def equivalence(group, fine_class=None):
    """EXACT / ECONOMICALLY_RELATED_BUT_NOT_IDENTICAL / UNVERIFIED / CONFLICT for an
    identity group (v2.source / identity.verify output)."""
    if not group:
        return "UNVERIFIED"
    status = group.get("status")
    if status == "CONFLICT" or group.get("reason") == "CONFLICT_LIST":
        return "CONFLICT"
    if status != "MULTI_VENUE" or group.get("tier") != "A":
        return "UNVERIFIED"
    from v2.gates import REFERENCE_UNVERIFIED

    if fine_class in REFERENCE_UNVERIFIED:
        return "ECONOMICALLY_RELATED_BUT_NOT_IDENTICAL"
    return "EXACT"


def outcome_of(ev):
    if ev.get("eligible"):
        return "ELIGIBLE"
    rs = ev.get("reasons") or []
    if not rs:
        return "RESEARCH_ONLY"
    codes = [r["code"] for r in rs]
    if any(c in PRE_ECONOMICS for c in codes) and "net" not in ev and "costs" not in ev:
        return "NOT_EVALUATED"
    if all(r.get("category") == "COST" for r in rs):
        return "UNPROFITABLE"
    return "FAILED"


def trace(c, ev, rows, groups=None, snapshot_sha=None, strategy=None):
    """One immutable funnel record for candidate `c` and its gate evaluation `ev`."""
    strategy = strategy or c.get("strategy") or ev.get("strategy")
    asset = c["asset"]
    legs = {}
    for side in ("long", "short"):
        r = rows.get((c[side], asset)) or {}
        xid = r.get("exchange_id") or observation_xid(c[side])
        legs[side] = dict(venue=c[side], exchange_id=xid,
                          evidence=fe.venue_status(xid),
                          paper_venue=observation.is_paper_venue(xid),
                          row_sha=sha(r)[:16] if r else None,
                          hourly=r.get("rate"), mark=r.get("mark"))
    rej = []
    for r in ev.get("reasons") or []:
        side = "long" if r.get("text", "").startswith("long") else (
            "short" if r.get("text", "").startswith("short") else None)
        status = legs[side]["evidence"] if side else None
        code = r["code"]
        if code == "NOT_A_V2_ROW":
            code_c = ("CONFIG_EXCLUDED" if side and not legs[side]["paper_venue"]
                      else "IDENTITY_UNVERIFIED")
        else:
            code_c = canonical(code, status)
        item = dict(canonical=code_c, code=code, category=r.get("category"),
                    text=r.get("text"), leg=side)
        if item not in rej:
            rej.append(item)
    out = outcome_of(ev)
    passed = "ECONOMIC_CANDIDATE"
    if out == "ELIGIBLE":
        passed = "PAPER_ELIGIBLE"
    elif out in ("FAILED", "UNPROFITABLE", "RESEARCH_ONLY"):
        passed = "EXECUTION_MODEL_CHECK"
    failed_at = None if out == "ELIGIBLE" else STAGES[STAGES.index(passed) + 1]
    long_row = rows.get((c["long"], asset)) or {}
    fine = long_row.get("fine_class")
    group = (groups or {}).get(long_row.get("underlying_id"))
    missing = sorted({r["canonical"] for r in rej if r["canonical"] in (
        "SETTLEMENT_EVIDENCE_MISSING", "MISSING_FUNDING_HISTORY", "RWA_REFERENCE_UNVERIFIED",
        "SESSION_UNKNOWN", "FEE_UNKNOWN", "PRICE_HISTORY_MISSING", "BOOK_MISSING",
        "IDENTITY_UNVERIFIED")})
    first = rej[0]["canonical"] if rej else None
    return dict(
        trace_id=trace_id(snapshot_sha, strategy, asset, c["long"], c["short"]),
        snapshot_sha=snapshot_sha, strategy=strategy, asset=asset,
        long=c["long"], short=c["short"], legs=legs,
        stage_passed=passed, failed_at=failed_at, outcome=out,
        economics="NOT_EVALUATED" if out == "NOT_EVALUATED" else "EVALUATED",
        net=ev.get("net"), first_rejection=first,
        first_rejection_internal=rej[0]["code"] if rej else None,
        all_rejections=rej, canonical_codes=list(dict.fromkeys(r["canonical"] for r in rej)),
        missing_evidence=missing,
        equivalence=equivalence(group, fine) if group else (
            "ECONOMICALLY_RELATED_BUT_NOT_IDENTICAL"
            if fine in ("commodity", "fx", "index", "rwa_mixed", "rwa") else None),
        source_hashes=dict(snapshot=snapshot_sha, long_row=legs["long"]["row_sha"],
                           short_row=legs["short"]["row_sha"]),
        confidence=confidence(legs, rej),
        next_action=NEXT_ACTION.get(first, "eligible" if out == "ELIGIBLE" else None))


def observation_xid(venue):
    from v2 import policy

    if venue in policy.FROM_LEGACY:
        return policy.FROM_LEGACY[venue]
    for k, v in observation.DISPLAY.items():
        if v == venue:
            return k
    return None


def confidence(legs, rej):
    """HIGH: both legs PAPER venues with settled evidence and no execution doubt;
    MEDIUM: a leg rests on a published rate only; LOW: observation-only venue, RFQ
    quote or unverified identity/reference."""
    codes = {r["canonical"] for r in rej}
    if (not all(l["paper_venue"] for l in legs.values())
            or codes & {"EXECUTION_MODEL_UNVERIFIED", "IDENTITY_UNVERIFIED",
                        "RWA_REFERENCE_UNVERIFIED", "CONFIG_EXCLUDED", "CONFLICT"}):
        return "LOW"
    if not all(fe.is_settled(l["evidence"]) for l in legs.values()):
        return "MEDIUM"
    return "HIGH"


def summary(traces):
    """Counts per stage passed, outcome, first canonical rejection and any rejection."""
    import collections

    st = collections.Counter(t["stage_passed"] for t in traces)
    passed_at_least = {s: sum(n for k, n in st.items() if STAGES.index(k) >= STAGES.index(s))
                       for s in STAGES}
    return dict(
        traces=len(traces),
        passed_at_least=passed_at_least,
        outcome=dict(collections.Counter(t["outcome"] for t in traces).most_common()),
        first_rejection=dict(collections.Counter(
            t["first_rejection"] for t in traces if t["first_rejection"]).most_common()),
        any_rejection=dict(collections.Counter(
            c for t in traces for c in t["canonical_codes"]).most_common()),
        not_evaluated=sum(1 for t in traces if t["outcome"] == "NOT_EVALUATED"))


# ----------------------------------------------------------------------------- radar
SIZES = (1000.0, 5000.0, 10000.0)
RADAR_LABEL = "POTENTIAL / RESEARCH_ONLY: published rates on current books; not PAPER"


def quote_leg(book, buy, size, fee):
    """One leg's round-trip cost on `book` at `size`; an RFQ quote is used as if it
    were fillable but marked INDICATIVE_RFQ (research only)."""
    if book and book.get("model") == "rfq_indicative":
        # A quote is one price for its whole bucket: no tolerance band around its own
        # mid (its spread is still paid in full).
        b = dict(book, model="orderbook")
        x = economics.leg(b, buy, size, 0.0 if fee is None else fee, tolerance=1.0)
        if fee is None:
            x = dict(reason=economics.FEE_UNKNOWN)
        return dict(x, quote="INDICATIVE_RFQ")
    x = economics.leg(book, buy, size, fee)
    return dict(x, quote="ORDERBOOK" if book else None)


def radar(asset, long, short, size_list=SIZES, horizon_h=24.0, max_breakeven_h=24.0):
    """Potential economics of long/short (dicts: venue, exchange_id, hourly rate, book,
    fee, interval) at each size. Funding uses the published rates as they stand (not
    settled, not persistent): labelled POTENTIAL when the carry is positive and the
    round trip is earned back within `max_breakeven_h` at some size, else
    RESEARCH_ONLY. Never eligible."""
    carry = fe.pair_carry(long.get("hourly"), short.get("hourly"))
    sizes = []
    for size in size_list:
        lo = quote_leg(long.get("book"), True, size, long.get("fee"))
        sh = quote_leg(short.get("book"), False, size, short.get("fee"))
        why = lo.get("reason") or sh.get("reason")
        if why:
            sizes.append(dict(size=size, reason=why))
            continue
        cost = lo["fees"] + lo["spread"] + sh["fees"] + sh["spread"]
        per_h = None if carry is None else carry * size
        be = (cost / per_h) if per_h and per_h > 0 else None
        sizes.append(dict(size=size, round_trip=cost, cost_bp=1e4 * cost / size,
                          carry_per_h=per_h,
                          net_horizon=None if per_h is None else per_h * horizon_h - cost,
                          breakeven_h=be,
                          quotes=[lo["quote"], sh["quote"]]))
    ok = [s for s in sizes if s.get("breakeven_h") is not None
          and s["breakeven_h"] <= max_breakeven_h]
    return dict(asset=asset, long=long["venue"], short=short["venue"],
                long_xid=long.get("exchange_id"), short_xid=short.get("exchange_id"),
                carry_hourly=carry, carry_8h=fe.per_8h(carry),
                label="POTENTIAL" if ok else "RESEARCH_ONLY", note=RADAR_LABEL,
                eligible=False, sizes=sizes,
                paper_venues=all(observation.is_paper_venue(x.get("exchange_id"))
                                 for x in (long, short)))
