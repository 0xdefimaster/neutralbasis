"""One price-quality layer for every simulated PAPER fill (Phase 15 readiness audit).

Used by BOTH paper engines:
  v2.paper_engine.PaperEngine.sim_fill      ESTIMATED ledger (open, retry, unwind, close)
  wide_paper.WidePaper.fill / unwind / close  the desk (STRICT entries under NB_V2)
A refused fill is "not filled": the position stays at risk, visibly, and is retried;
no price is ever invented or clipped into range.

Book checks (before any fill from a book):
  BOOK_MISSING / BOOK_EMPTY   no two-sided book
  BOOK_CROSSED / BOOK_LOCKED  best bid >= best ask (locked = equal)
  BOOK_STALE                  older than the venue limit (v2.marketdata, answer basis;
                              unknown or future time = stale)
  SPREAD_TOO_WIDE             best spread > MAX_SPREAD_BP of the mid: not a price
  OFF_TICK_GRID               best prices not on the instrument's tick grid (only when
                              the tick is known; unknown tick is reported, not assumed)
Price checks (after the fill price is known):
  SLIPPAGE_ABOVE_LIMIT        a walked (no-limit) exit / unwind price further than
                              MAX_WALK_BP from its own book mid
  CROSS_LEG_DEVIATION         further than MAX_CROSS_LEG_DEV from the OTHER leg's fresh
                              current mid (same underlying, per-unit prices): catches a
                              wrong scale / multiplier / a single stale leg while letting
                              the market move
  PRICE_SCALE_SUSPECT         further than MAX_OWN_REF_DEV from this leg's own entry /
                              fill average (last defence)
  NO_REFERENCE_PRICE          neither reference exists: the price cannot be validated
Partial fills are allowed (each part is checked); IOC limits (v2.paper_exec 10 bp,
wide_paper FILL_TOLERANCE) stay as they are.
"""
from v2 import marketdata

MAX_SPREAD_BP = 100.0
MAX_WALK_BP = 100.0
MAX_CROSS_LEG_DEV = 0.05
MAX_OWN_REF_DEV = 0.25
# Stuck positions (readiness audit 2026-11-14): a real, persistent divergence between
# the legs or a chronically wide book would refuse every close forever, so the loss
# would never be realized. After STUCK_RELAX_S of refused EXIT attempts (close /
# unwind / retry of an open position, never an entry) only two limits widen: the
# cross-leg band and the spread / walk limits. Stale, crossed, locked, off-grid,
# missing-reference and scale-sized errors (> 50 %, e.g. x2 or x10) are never relaxed.
STUCK_RELAX_S = 6 * 3600.0
RELAXED_CROSS_LEG_DEV = 0.50
RELAXED_SPREAD_BP = 500.0
RELAXED_WALK_BP = 500.0


def relaxed_after(stuck_since, now):
    """True once an open position's exits have been refused for STUCK_RELAX_S."""
    return stuck_since is not None and now - stuck_since >= STUCK_RELAX_S


def xid_of(venue):
    """Legacy venue name -> exchange_id (staleness limit lookup)."""
    from v2 import policy
    return policy.FROM_LEGACY.get(venue, str(venue).lower())


def best(book):
    if not book or not book.get("bids") or not book.get("asks"):
        return None, None
    return max(p for p, _ in book["bids"]), min(p for p, _ in book["asks"])


def mid(book):
    b, a = best(book)
    return None if b is None else (b + a) / 2


def book_reasons(book, xid, at, tick=None, rfq=False, relaxed=False):
    """Every reason this book must not be filled from (empty list = usable)."""
    if not book:
        return ["BOOK_MISSING"]
    b, a = best(book)
    if b is None or book.get("empty"):
        return ["BOOK_EMPTY"]
    out = []
    if b > a or book.get("crossed"):
        out.append("BOOK_CROSSED")
    elif b == a:
        out.append("BOOK_LOCKED")
    if not rfq and marketdata.stale(book, xid, at):
        out.append("BOOK_STALE")
    m = (a + b) / 2
    lim = RELAXED_SPREAD_BP if relaxed else MAX_SPREAD_BP
    if m > 0 and (a - b) / m * 1e4 > lim and not out:
        out.append("SPREAD_TOO_WIDE")
    if tick:
        for p in (b, a):
            k = p / tick
            if abs(k - round(k)) > 1e-6 * max(1.0, abs(k)):
                out.append("OFF_TICK_GRID")
                break
    return out


def reference_mid(book, xid, at, rfq=False):
    """A book's mid only when the book itself is usable (fresh, not crossed)."""
    return None if book_reasons(book, xid, at, rfq=rfq) else mid(book)


def price_reason(price, book_mid=None, cross_ref=None, own_ref=None, walk=False,
                 relaxed=False):
    if price is None or price <= 0:
        return "NO_PRICE"
    wl = RELAXED_WALK_BP if relaxed else MAX_WALK_BP
    if walk and book_mid:
        bp = abs(price / book_mid - 1) * 1e4
        if bp > wl:
            return "SLIPPAGE_ABOVE_LIMIT (%.0f bp > %.0f bp)" % (bp, wl)
    if cross_ref:
        # both legs move together: the other leg's FRESH price is the reference, so a
        # large legitimate market move over a long hold never blocks a close
        d = abs(price / cross_ref - 1)
        if d > (RELAXED_CROSS_LEG_DEV if relaxed else MAX_CROSS_LEG_DEV):
            return "CROSS_LEG_DEVIATION (%.6g vs other leg %.6g, %.1f %%)" % (
                price, cross_ref, d * 100)
        return None
    if own_ref:
        # last defence only while no fresh cross-leg price exists
        d = abs(price / own_ref - 1)
        if d > MAX_OWN_REF_DEV:
            return "PRICE_SCALE_SUSPECT (%.6g vs reference %.6g)" % (price, own_ref)
        return None
    return "NO_REFERENCE_PRICE"
