"""Per-underlying-unit normalization of multiplied contracts (1000PEPE, kPEPE, 1M...).

A venue that lists "1000PEPE" quotes the price of 1,000 PEPE and sizes in lots of 1,000
PEPE. The paper desk compares legs per unit of the underlying, so a row and its book
are converted once, here:

  price per unit = contract price / multiplier
  size in units  = contract size  x multiplier
  lot step, minimum size: x multiplier;  minimum quote (USD): unchanged
  funding rate (a fraction of notional) and fee rates: unchanged

`to_contracts` converts a unit quantity back for the venue (paper fills only). A
multiplier that is unknown, zero or negative is refused: such a market never gets a
row (identity reason MULTIPLIER_UNKNOWN).
"""


def check(multiplier):
    if multiplier is None or multiplier <= 0:
        raise ValueError("contract multiplier unknown or invalid: %r" % (multiplier,))
    return float(multiplier)


def row(r, multiplier):
    """A copy of a strategy row with prices per unit and sizes in units."""
    m = check(multiplier)
    out = dict(r)
    if m == 1:
        out["multiplier"] = 1.0
        return out
    for k in ("mark", "index"):
        if out.get(k) is not None:
            out[k] = out[k] / m
    for k in ("step", "minimum"):
        if out.get(k) is not None:
            out[k] = out[k] * m
    out["multiplier"] = m
    return out


def book(b, multiplier):
    """A copy of a book with level prices per unit and level sizes in units."""
    m = check(multiplier)
    if m == 1:
        return dict(b, multiplier=1.0)
    return dict(b, bids=[(p / m, q * m) for p, q in b.get("bids") or []],
                asks=[(p / m, q * m) for p, q in b.get("asks") or []], multiplier=m)


def to_contracts(units, multiplier):
    return units / check(multiplier)
