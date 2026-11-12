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
RFQ = "EXECUTION_MODEL_UNVERIFIED"
EXIT_BASIS = "current book (not guaranteed at exit)"


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
    if book.get("model") == "rfq_indicative":
        # An indicative RFQ quote (Variational) is not an order that can be filled.
        return dict(reason=RFQ)
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
                entry_spread=qty * abs(entry - m), exit_spread=qty * abs(m - exit_),
                entry_fee=qty * entry * fee, exit_fee=qty * exit_ * fee,
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
    entry = lo["entry_fee"] + sh["entry_fee"] + lo["entry_spread"] + sh["entry_spread"]
    exit_ = lo["exit_fee"] + sh["exit_fee"] + lo["exit_spread"] + sh["exit_spread"]
    return dict(long=lo, short=sh, fees=lo["fees"] + sh["fees"],
                spread=lo["spread"] + sh["spread"], entry_cost=entry, exit_cost=exit_,
                exit_basis=EXIT_BASIS, notional=size_usd,
                total=lo["fees"] + sh["fees"] + lo["spread"] + sh["spread"]), None


def net(costs, funding=0.0, basis=0.0, reserve=0.0):
    """The single net formula: funding + basis - fees - spread - reserve."""
    return dict(funding=funding, basis=basis, fees=costs["fees"], spread=costs["spread"],
                reserve=reserve,
                net=funding + basis - costs["fees"] - costs["spread"] - reserve)


def stress(costs, funding=0.0, basis=0.0, reserve=0.0):
    """Net under adverse but plausible conditions. The exit priced on today's book is
    not guaranteed, so its spread is widened; slippage, funding and a half-filled second
    leg are stressed too. Every scenario is at most the base net.
      exit_spread_x3          exit spread three times today's
      slippage_+5bp           5 bp of notional more on each of the four fills
      funding_-50%            income halved (a cost made 50 % larger)
      second_leg_half_filled  half the position unwound at once: half the income lost
                              and a second round trip on that half
      combined_moderate       exit spread x2, +2 bp per fill, funding -25 % (the gate's
                              stress check)"""
    n = costs.get("notional") or 0.0
    xs = costs["long"]["exit_spread"] + costs["short"]["exit_spread"]
    base = net(costs, funding, basis, reserve)["net"]

    def f(scale):
        return funding * scale if funding > 0 else funding * (2 - scale)

    half_rt = 0.5 * (costs["long"]["fees"] + costs["long"]["spread"])
    out = [
        dict(name="base", net=base),
        dict(name="exit_spread_x3", net=base - 2 * xs),
        dict(name="slippage_+5bp", net=base - 4 * 0.0005 * n),
        dict(name="funding_-50%", net=base - funding + f(0.5)),
        dict(name="second_leg_half_filled",
             net=base - 0.5 * max(funding, 0) - 0.5 * max(basis, 0) - half_rt),
        dict(name="combined_moderate",
             net=base - xs - 4 * 0.0002 * n - funding + f(0.75)),
    ]
    for x in out:
        x["net"] = min(x["net"], base)
    return out
