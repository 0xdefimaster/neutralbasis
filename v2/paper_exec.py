"""Simulated execution for the ESTIMATED PAPER ledger (PAPER only, nothing is sent).

Every fill made here is a SIMULATION on observed public data and is labelled so:
  ESTIMATED_CLOB_FILL   a marketable IOC walked through an observed order book
  ESTIMATED_RFQ_FILL    a Variational indicative RFQ quote of EXACTLY that notional
No fill is ever `verified`; none is an exchange order or an exchange fill.

CLOB (`clob_ioc`): marketable immediate-or-cancel. It takes the observed levels from
the best price outward, never beyond the slippage limit (bp from the book mid at
decision time); whatever the visible depth inside the limit cannot fill is NOT filled
(partial fill, the rest cancelled). Nothing is filled from depth the book did not show.

RFQ (`rfq_fill`): a size-specific indicative bid/ask (v2.rfq_paper): quote age checked,
exact tier only (a size between tiers is unpriced), firm-vs-indicative gap and last
look are SCENARIO inputs (`firm_gap_bp`, `last_look_reject`), never assumed away and
never drawn at random: a replay is deterministic.
"""
from v2 import economics, rfq_paper

CLOB_LABEL, RFQ_LABEL = "ESTIMATED_CLOB_FILL", rfq_paper.LABEL
DEFAULT_SLIPPAGE_BP = 10.0
EPS = 1e-12


def clob_ioc(book, buy, qty, max_slippage_bp=DEFAULT_SLIPPAGE_BP, fee_rate=0.0,
             ref_mid=None):
    """Simulated marketable IOC of `qty` units. dict(filled, price, partial, ...)."""
    base = dict(label=CLOB_LABEL, model="CLOB_IOC", verified=False, buy=buy,
                requested=qty, filled=0.0, price=None, fee=0.0, partial=False)
    if not book or book.get("empty") or not book.get("bids") or not book.get("asks"):
        return dict(base, reason="BOOK_MISSING_OR_EMPTY")
    if book.get("model") == "rfq_indicative":
        return dict(base, reason="NOT_A_CLOB")
    mid = ref_mid if ref_mid is not None else economics.mid(book)
    limit = mid * (1 + max_slippage_bp * 1e-4) if buy else mid * (1 - max_slippage_bp * 1e-4)
    got = cost = 0.0
    levels = 0
    for p, q in (book["asks"] if buy else book["bids"]):
        if (p > limit) if buy else (p < limit):
            break
        take = min(qty - got, q)
        if take <= 0:
            break
        got += take
        cost += take * p
        levels += 1
        if got >= qty * (1 - 1e-12):
            got = qty
            break
    if got <= EPS:
        return dict(base, reason="NO_DEPTH_INSIDE_SLIPPAGE_LIMIT", limit=limit, mid=mid)
    px = cost / got
    return dict(base, reason=None, filled=got, price=px, mid=mid, limit=limit,
                levels=levels, partial=got < qty * (1 - 1e-9),
                fee=got * px * (fee_rate or 0.0), spread_cost=got * abs(px - mid),
                slippage_bp=1e4 * abs(px / mid - 1))


def rfq_fill(record, buy, size_usd, at, max_age=rfq_paper.MAX_QUOTE_AGE_S, fee_rate=0.0,
             firm_gap_bp=0.0, last_look_reject=False, qty=None):
    """Simulated RFQ fill. `qty` (units) overrides the notional when the leg must match
    another leg's quantity; its notional must then still be an exact tier (within 1%)
    or it is unpriced. Last look: `last_look_reject` = the OLP refuses (a scenario)."""
    base = dict(label=RFQ_LABEL, model="RFQ_INDICATIVE", verified=False, buy=buy,
                requested_usd=size_usd, filled=0.0, price=None, fee=0.0, partial=False)
    if last_look_reject:
        return dict(base, reason="LAST_LOOK_REJECTED (scenario)")
    f = rfq_paper.fill(record, buy, size_usd, at, max_age, 0.0)
    if f.get("reason"):
        return dict(base, reason=f["reason"], quote_age_s=f.get("quote_age_s"),
                    observed_range=f.get("observed_range"))
    want = f["qty"] if qty is None else qty
    if qty is not None and abs(qty * f["mid"] - size_usd) > 0.01 * size_usd:
        return dict(base, reason=rfq_paper.UNPRICED, note="matched quantity is not "
                    "an exact published tier")
    px = f["price"] * (1 + firm_gap_bp * 1e-4) if buy else f["price"] * (1 - firm_gap_bp * 1e-4)
    return dict(base, reason=None, filled=want, price=px, mid=f["mid"], tier=f["tier"],
                quote_age_s=f["quote_age_s"], firm_gap_bp=firm_gap_bp,
                fee=want * px * (fee_rate or 0.0), spread_cost=want * abs(px - f["mid"]),
                indicative_price=f["price"])
