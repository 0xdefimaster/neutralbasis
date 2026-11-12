"""The V2 net-profit engine shared by the funding, convergence and hybrid strategies.

Every strategy prices a two-leg position the same way, so the three results add up
and nothing is counted twice:

  entry/exit fills   walked on each leg's real book at the position size, within the
                     fill tolerance; a book that cannot fill it is INSUFFICIENT_LIQUIDITY
  fees               four taker fees (open + close on both legs); maker fills are never
                     assumed; an UNKNOWN fee stops the evaluation (FEE_UNKNOWN)
  spread/slippage    entry and exit fill versus each leg's mid, from the same walk
  funding            supplied by the strategy (published or settled, labelled)
  basis              supplied by the strategy (convergence capture), never by funding
  risk reserve       supplied by the strategy

All amounts are USD for one position of `size` USD notional per leg.
"""
from bot import FILL_TOLERANCE

FEE_UNKNOWN = "FEE_UNKNOWN"
LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
BOOK_MISSING = "BOOK_MISSING"
BOOK_BAD = "BOOK_CROSSED_OR_EMPTY"


def mid(book):
    return (book["bids"][0][0] + book["asks"][0][0]) / 2


def fill(levels, qty, buy, limit):
    """Average price of `qty` taken from `levels` (best first) without crossing `limit`;
    None when the book cannot fill it inside the limit."""
    remain, total = qty, 0.0
    for p, q in levels:
        if (p > limit) if buy else (p < limit):
            break
        take = min(remain, q)
        total += take * p
        remain -= take
        if remain <= qty * 1e-9:
            return total / qty
    return None


def leg(book, buy, size_usd, fee, tolerance=FILL_TOLERANCE):
    """One leg opened (buy for a long) and closed on this book now. Returns a dict with
    qty, entry/exit price, fees, spread cost (entry and exit vs mid) or a reason."""
    if not book:
        return dict(reason=BOOK_MISSING)
    if book.get("empty") or book.get("crossed") or not book.get("bids") or not book.get("asks"):
        return dict(reason=BOOK_BAD)
    if fee is None:
        return dict(reason=FEE_UNKNOWN)
    m = mid(book)
    qty = size_usd / m
    open_side = book["asks"] if buy else book["bids"]
    close_side = book["bids"] if buy else book["asks"]
    entry = fill(open_side, qty, buy, m * (1 + tolerance if buy else 1 - tolerance))
    exit_ = fill(close_side, qty, not buy, m * (1 - tolerance if buy else 1 + tolerance))
    if entry is None or exit_ is None:
        return dict(reason=LIQUIDITY, qty=qty, mid=m)
    return dict(qty=qty, mid=m, entry=entry, exit=exit_,
                spread=qty * (abs(entry - m) + abs(m - exit_)),
                fees=qty * (entry + exit_) * fee, reason=None)


def round_trip(long_book, short_book, size_usd, fee_long, fee_short):
    """Both legs. Returns (costs dict, reason): costs = fees + spread of both legs, plus
    the quantity both legs share (the smaller one)."""
    lo = leg(long_book, True, size_usd, fee_long)
    sh = leg(short_book, False, size_usd, fee_short)
    for x in (lo, sh):
        if x.get("reason"):
            return None, x["reason"]
    return dict(long=lo, short=sh, fees=lo["fees"] + sh["fees"],
                spread=lo["spread"] + sh["spread"],
                total=lo["fees"] + sh["fees"] + lo["spread"] + sh["spread"]), None


def net(costs, funding=0.0, basis=0.0, reserve=0.0):
    """The single net formula: funding + basis - fees - spread - reserve."""
    return dict(funding=funding, basis=basis, fees=costs["fees"], spread=costs["spread"],
                reserve=reserve,
                net=funding + basis - costs["fees"] - costs["spread"] - reserve)
