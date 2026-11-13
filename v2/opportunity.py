"""Opportunity labels and strategy-aware discovery freshness (Phase 13.1 / 13.2).

NOT a second state machine. A label is a read-only VIEW derived from what the existing
machinery already decided:
  * the funnel trace outcome (v2.funnel: NOT_EVALUATED / FAILED / UNPROFITABLE /
    RESEARCH_ONLY / ELIGIBLE) of the STRICT gate (v2.gates, unchanged),
  * the ESTIMATED evaluator status (v2.estimated),
  * whether a simulated position exists in the ESTIMATED ledger (v2.paper_ledger),
  * the discovery freshness of the data (below).
Discovery and execution are separate questions: a DISCOVERED opportunity says nothing
about whether a position may open.

Labels (first match wins, in this order):
  STRICT_PAPER_ELIGIBLE        funnel outcome ELIGIBLE (strict gate passed)
  ESTIMATED_PAPER_SIMULATED    a simulated ESTIMATED position exists for it
  BLOCKED_REFERENCE_MISMATCH   DIFFERENT_UNDERLYING (identity / reference conflict)
  DATA_STALE                   discovery data too old for this strategy's policy
  BLOCKED_EXECUTION_MODEL      the only way to price it is an unverified execution
                               model (Variational RFQ) and the estimate was not possible
  ESTIMATED_PAPER_CANDIDATE    ESTIMATED_ENTRY_SIGNAL (estimated economics positive
                               under the strict thresholds; never verified)
  BLOCKED_ECONOMICS            economics ran and the result is below the bar
                               (UNPROFITABLE, or an estimated cost/stress rejection)
  WATCHLIST                    economically interesting (positive carry or a synchronous
                               price gap) but blocked by evidence/scope, not economics
  DISCOVERED                   seen, nothing more decided

Discovery freshness (never used for execution; the strict gate keeps its own limits):
  funding      the published rate must belong to the current funding period (age <=
               the venue's interval, max 8 h) and the prices used for the cost estimate
               may be up to RESEARCH_PRICE_AGE_S old (Variational indicative quotes,
               refreshed about every 40 s, qualify for RESEARCH; never as a fill price)
  price_gap    both legs' price times within the market's volatility limit
               (v2.opportunity_log.freshness_limit, floor 2 s); otherwise the gap is
               ASYNCHRONOUS and is reported as such, never as a gap signal
  strict       unchanged: v2.gates (BOOK_STALE per venue), RFQ quote <= 60 s,
               v2.paper_engine time alignment
"""
from v2 import opportunity_log

LABELS = ("STRICT_PAPER_ELIGIBLE", "ESTIMATED_PAPER_SIMULATED", "BLOCKED_REFERENCE_MISMATCH",
          "DATA_STALE", "BLOCKED_EXECUTION_MODEL", "ESTIMATED_PAPER_CANDIDATE",
          "BLOCKED_ECONOMICS", "WATCHLIST", "DISCOVERED")
# The existing state each label is derived from (documentation and a test invariant).
DERIVED_FROM = dict(
    STRICT_PAPER_ELIGIBLE="funnel.outcome == ELIGIBLE",
    ESTIMATED_PAPER_SIMULATED="paper_ledger position (any state) for this signal",
    BLOCKED_REFERENCE_MISMATCH="estimated reason DIFFERENT_UNDERLYING / funnel CONFLICT",
    DATA_STALE="discovery_freshness(...) == STALE",
    BLOCKED_EXECUTION_MODEL="strict EXECUTION_MODEL_UNVERIFIED and no estimate",
    ESTIMATED_PAPER_CANDIDATE="estimated.status == ESTIMATED_ENTRY_SIGNAL",
    BLOCKED_ECONOMICS="funnel UNPROFITABLE or estimated cost/stress rejection",
    WATCHLIST="positive carry or synchronous gap, blocked by evidence or scope",
    DISCOVERED="seen only",
)
RESEARCH_PRICE_AGE_S = 120.0
MAX_RATE_AGE_S = 8 * 3600.0
ECON_CODES = ("BREAKEVEN", "NET_BELOW_MIN", "STRESS_NEGATIVE", "CARRY_NOT_POSITIVE",
              "RFQ_STRESS_NEGATIVE", "REFERENCE_STRESS_NEGATIVE", "GAP_NOT_EXTREME",
              "NO_EPISODES", "CARRY_NOT_PERSISTENT")


def discovery_freshness(strategy, legs, sigma_hourly=None):
    """legs: [dict(price_age_s, rate_age_s, interval_s, price_time)] for the two legs.
    Returns dict(status FRESH / STALE / ASYNCHRONOUS / UNKNOWN, why, limit_s)."""
    known = [l for l in legs if l.get("price_age_s") is not None]
    # A leg KNOWN to be too old makes the row stale even when the other leg's age is
    # unknown; unknown ages alone never pass as fresh.
    if any(l["price_age_s"] > RESEARCH_PRICE_AGE_S for l in known):
        return dict(status="STALE", why="price older than %.0f s" % RESEARCH_PRICE_AGE_S,
                    limit_s=RESEARCH_PRICE_AGE_S)
    if len(known) < len(legs):
        return dict(status="UNKNOWN", why="price time missing", limit_s=None)
    if strategy == "funding":
        for l in legs:
            iv = min(l.get("interval_s") or 3600.0, MAX_RATE_AGE_S)
            if l.get("rate_age_s") is not None and l["rate_age_s"] > iv:
                return dict(status="STALE", why="rate older than its funding period",
                            limit_s=iv)
            if l["price_age_s"] > RESEARCH_PRICE_AGE_S:
                return dict(status="STALE", why="price older than %.0f s"
                            % RESEARCH_PRICE_AGE_S, limit_s=RESEARCH_PRICE_AGE_S)
        return dict(status="FRESH", why=None, limit_s=RESEARCH_PRICE_AGE_S)
    lim, basis = opportunity_log.freshness_limit(sigma_hourly)
    times = [l.get("price_time") for l in legs]
    if any(t is None for t in times):
        return dict(status="UNKNOWN", why="price time missing", limit_s=lim)
    skew = max(times) - min(times)
    if skew > lim:
        return dict(status="ASYNCHRONOUS", why="leg prices %.1f s apart > %.1f s (%s)"
                    % (skew, lim, basis), limit_s=lim, skew_s=skew)
    if max(l["price_age_s"] for l in legs) > RESEARCH_PRICE_AGE_S:
        return dict(status="STALE", why="price older than %.0f s" % RESEARCH_PRICE_AGE_S,
                    limit_s=RESEARCH_PRICE_AGE_S)
    return dict(status="FRESH", why=None, limit_s=lim, skew_s=skew)


def label(trace=None, est=None, simulated=False, freshness=None, carry=None,
          gap_signal=False):
    """The Phase 13 label for one (asset, long, short, strategy)."""
    outcome = (trace or {}).get("outcome")
    est = est or {}
    codes = set(est.get("reasons") or []) | set((trace or {}).get("canonical_codes") or [])
    if outcome == "ELIGIBLE":
        return "STRICT_PAPER_ELIGIBLE"
    if simulated:
        return "ESTIMATED_PAPER_SIMULATED"
    if "DIFFERENT_UNDERLYING" in codes or "CONFLICT" in codes:
        return "BLOCKED_REFERENCE_MISMATCH"
    if (freshness or {}).get("status") in ("STALE", "ASYNCHRONOUS"):
        return "DATA_STALE"
    strict_codes = set(est.get("strict_reasons") or []) | codes
    if est.get("status") == "ESTIMATED_ENTRY_SIGNAL":
        return "ESTIMATED_PAPER_CANDIDATE"
    if "EXECUTION_MODEL_UNVERIFIED" in strict_codes and est.get("net") is None and \
            est.get("first") not in ECON_CODES:
        return "BLOCKED_EXECUTION_MODEL"
    if outcome == "UNPROFITABLE" or est.get("first") in ECON_CODES:
        return "BLOCKED_ECONOMICS"
    if (carry is not None and carry > 0) or gap_signal:
        return "WATCHLIST"
    return "DISCOVERED"
