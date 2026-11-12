"""Estimated RFQ fills for Variational Omni (PAPER research only).

Omni has no order book: the OLP answers a request for quote, does a last look, and the
firm price may differ from the indicative one (docs.variational.io, read 2026-11-12).
The public /metadata/v2/stats answer carries INDICATIVE quotes at a few notional sizes
(`base` = minimum order size, `size_1k`, `size_100k`, `size_1m` for major markets),
cached up to 600 s, with `updated_at`.

This module turns those quotes into ESTIMATED fills, never verified ones:
  * the quote's age is checked at decision time (STALE_QUOTE / QUOTE_IN_FUTURE);
  * a size is priced only from a published tier that covers it: an exact tier is
    EXACT_TIER; a size between tiers is BRACKETED: it is priced on the next LARGER
    tier (wider, conservative) and the smaller tier's price is reported as the other
    end of the uncertainty band; nothing is interpolated or invented; a size above the
    largest published tier has NO price (ABOVE_LARGEST_TIER);
  * the spread is paid once: a buy fills at the tier's ask, a sell at its bid; the
    price P&L already contains it; `spread_cost` (vs the tier mid) is only a breakdown,
    never added again as slippage;
  * stress scenarios cover what public data cannot show: firm-vs-indicative gap,
    last-look rejection (and unwinding the other leg), quote staleness.

Every result carries label ESTIMATED_RFQ_FILL. Nothing here is an order, a fill or a
gate decision; the strict PAPER gate keeps EXECUTION_MODEL_UNVERIFIED for Variational.
"""
import datetime
import math

LABEL = "ESTIMATED_RFQ_FILL"
TIERS = (("base", None), ("size_1k", 1000.0), ("size_100k", 100000.0),
         ("size_1m", 1000000.0))
DOC_CACHE_S = 600.0          # api.md: bid/ask may be cached for up to 600 s
MAX_QUOTE_AGE_S = 60.0       # stricter default for PAPER estimates
FUTURE_TOLERANCE_S = 5.0     # clock skew allowed for an updated_at after `at`
EXACT, BRACKETED, ABOVE, NO_QUOTE = ("EXACT_TIER", "BRACKETED", "ABOVE_LARGEST_TIER",
                                     "NO_QUOTE")


def ts(iso):
    """ISO-8601 updated_at (nanoseconds allowed) -> epoch seconds, or None."""
    if not iso:
        return None
    try:
        s = str(iso).rstrip("Z")
        head, _, frac = s.partition(".")
        t = datetime.datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=datetime.timezone.utc).timestamp()
        return t + (float("0." + frac[:9]) if frac else 0.0)
    except ValueError:
        return None


def tiers(record):
    """[(name, notional or None, bid, ask)] of the published tiers, smallest first."""
    q = (record or {}).get("quotes") or {}
    out = []
    for name, notional in TIERS:
        ba = q.get(name)
        if ba and ba[0] and ba[1] and ba[0] > 0 and ba[1] >= ba[0]:
            out.append((name, notional, float(ba[0]), float(ba[1])))
    return out


def quote_for(record, size_usd):
    """The tier(s) that price `size_usd`. dict(status, tier, bid, ask, mid, below)."""
    ts_ = tiers(record)
    if not ts_:
        return dict(status=NO_QUOTE)
    sized = [t for t in ts_ if t[1] is not None]
    for name, notional, bid, ask in sized:
        if abs(size_usd - notional) < 1e-9:
            return dict(status=EXACT, tier=name, bid=bid, ask=ask, mid=(bid + ask) / 2,
                        below=None)
    above = [t for t in sized if t[1] > size_usd]
    if not above:
        return dict(status=ABOVE, largest=sized[-1][0] if sized else None)
    use = above[0]
    lower = [t for t in ts_ if t[1] is None or t[1] < size_usd]
    below = lower[-1] if lower else None
    return dict(status=BRACKETED, tier=use[0], bid=use[2], ask=use[3],
                mid=(use[2] + use[3]) / 2,
                below=None if below is None else dict(tier=below[0], bid=below[2],
                                                      ask=below[3]))


def quote_age(record, at):
    t = ts((record or {}).get("quotes_updated"))
    return None if t is None else at - t


def fill(record, buy, size_usd, at, max_age=MAX_QUOTE_AGE_S, fee=0.0):
    """One ESTIMATED fill of `size_usd` (buy at ask, sell at bid). dict with price, qty,
    fee, spread_cost (breakdown only), price band from the bracketing tiers, quote age
    and status; `reason` is set when no estimate is possible."""
    age = quote_age(record, at)
    base = dict(label=LABEL, buy=buy, size=size_usd, quote_age_s=age, at=at)
    if age is None:
        return dict(base, reason="QUOTE_TIME_UNKNOWN")
    if age < -FUTURE_TOLERANCE_S:
        return dict(base, reason="QUOTE_IN_FUTURE")
    if age > max_age:
        return dict(base, reason="STALE_QUOTE")
    q = quote_for(record, size_usd)
    if q["status"] in (NO_QUOTE, ABOVE):
        return dict(base, reason=q["status"], quote=q)
    price = q["ask"] if buy else q["bid"]
    qty = size_usd / q["mid"]
    other = q.get("below")
    band = sorted([price, (other["ask"] if buy else other["bid"])]) if other else [price,
                                                                                   price]
    return dict(base, reason=None, status=q["status"], tier=q["tier"], price=price,
                mid=q["mid"], qty=qty, fee=qty * price * (fee or 0.0),
                spread_cost=qty * abs(price - q["mid"]), price_band=band,
                band_bp=1e4 * (band[1] - band[0]) / q["mid"])


def round_trip(record, buy, size_usd, at, max_age=MAX_QUOTE_AGE_S, fee=0.0):
    """Open and close on the SAME quote (a single-moment estimate): the cost is the
    full spread once, plus fees. Returns (dict, reason)."""
    o = fill(record, buy, size_usd, at, max_age, fee)
    if o["reason"]:
        return None, o["reason"]
    c = fill(record, not buy, size_usd, at, max_age, fee)
    if c["reason"]:
        return None, c["reason"]
    qty = o["qty"]
    pnl = qty * ((c["price"] - o["price"]) if buy else (o["price"] - c["price"]))
    return dict(label=LABEL, open=o, close=c, qty=qty, fees=o["fee"] + c["fee"],
                spread=-pnl, total=-pnl + o["fee"] + c["fee"],
                entry_cost=o["spread_cost"] + o["fee"],
                exit_cost=c["spread_cost"] + c["fee"]), None


def simulate(open_record, close_record, buy, size_usd, open_at, close_at, hourly_rate,
             max_age=MAX_QUOTE_AGE_S, fee=0.0):
    """A position opened on one quote and closed on a LATER one: price P&L from the two
    fill prices (spread inside), funding ESTIMATED from the published hourly rate over
    the elapsed time (positive rate = longs pay), fees. Status SIMULATED, never FILLED."""
    if close_at <= open_at:
        return dict(label=LABEL, reason="CLOSE_NOT_AFTER_OPEN")
    o = fill(open_record, buy, size_usd, open_at, max_age, fee)
    if o["reason"]:
        return dict(label=LABEL, reason="OPEN_" + o["reason"])
    c = fill(close_record, not buy, size_usd, close_at, max_age, fee)
    if c["reason"]:
        return dict(label=LABEL, reason="CLOSE_" + c["reason"])
    qty = o["qty"]
    price_pnl = qty * ((c["price"] - o["price"]) if buy else (o["price"] - c["price"]))
    hours = (close_at - open_at) / 3600.0
    funding = None if hourly_rate is None else (
        -1 if buy else 1) * hourly_rate * size_usd * hours
    fees = o["fee"] + c["fee"]
    return dict(label=LABEL, status="SIMULATED", reason=None, open=o, close=c, qty=qty,
                hours=hours, price_pnl=price_pnl, funding_estimated=funding,
                funding_basis="published rate (RATE_PUBLISHED), not a payment",
                fees=fees, net=price_pnl + (funding or 0.0) - fees)


# ----------------------------------------------------------------------- stress
# Public data cannot show these; each is a scenario, not a measurement.
FIRM_GAP_BP = (0.0, 5.0, 10.0, 25.0)       # firm price worse than indicative, per fill
LAST_LOOK_P = (0.0, 0.05, 0.20)            # probability a fill is rejected
MODERATE = dict(firm_gap_bp=10.0, last_look_p=0.10, staleness_sigma=1.0)


def staleness_bp(age_s, sigma_hourly):
    """1-sigma price move (bp) over the quote's age, from an hourly volatility."""
    if age_s is None or sigma_hourly is None or age_s <= 0:
        return None
    return 1e4 * sigma_hourly * math.sqrt(age_s / 3600.0)


def stress(base_net, size_usd, rfq_fills=2, other_leg_round_trip=0.0, age_s=None,
           sigma_hourly=None):
    """Net under RFQ scenarios. rfq_fills = RFQ fills in the round trip (2 per RFQ leg).
    last-look: with probability p a fill is rejected after the other leg traded, and the
    other leg is unwound at its round-trip cost (expected cost p x that, per fill).
    staleness: a 1-sigma adverse move over the quote age on each RFQ fill (UNKNOWN when
    no volatility is known: then the scenario is reported as None, not as zero)."""
    out = [dict(name="base", net=base_net)]
    for g in FIRM_GAP_BP[1:]:
        out.append(dict(name="firm_gap_%gbp" % g, net=base_net - rfq_fills * g * 1e-4
                        * size_usd))
    for p in LAST_LOOK_P[1:]:
        out.append(dict(name="last_look_p%g" % p,
                        net=base_net - rfq_fills * p * other_leg_round_trip))
    st = staleness_bp(age_s, sigma_hourly)
    out.append(dict(name="staleness_1sigma",
                    net=None if st is None else base_net - rfq_fills * st * 1e-4 * size_usd))
    m = MODERATE
    mod = (base_net - rfq_fills * m["firm_gap_bp"] * 1e-4 * size_usd
           - rfq_fills * m["last_look_p"] * other_leg_round_trip
           - (0.0 if st is None else rfq_fills * m["staleness_sigma"] * st * 1e-4 * size_usd))
    out.append(dict(name="rfq_moderate", net=mod, staleness_included=st is not None))
    return out
