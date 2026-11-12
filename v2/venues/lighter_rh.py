"""lighter_rh: Lighter on Robinhood Chain (a separate exchange from Lighter Core).

Public reads only, GET https://api.rh.lighter.xyz/api/v1/ (docs.lighter.xyz API
reference, server "Lighter on Robinhood Chain mainnet", read 2026-11-12).

Asset class: `funding_premium_multiplier`. The funding docs give the premium
multiplier as 1 for crypto, 1/2 for RWA and 1/100 for pre-IPO/prelaunch markets; the
API field is an integer and the live values (2026-11-12) were 100 on BTC/ETH/SOL, 50 on
NVDA/SPY/XAU and 1 on OPENAI/ANTHROPIC, i.e. the documented ratio x100. Any other value
leaves the class unknown.

Funding history (`fundings`): `rate` in percent, `direction` "long" = the long pays
(the engine's convention, bot.Feed / wide_markets.lighter_fundings; the docs do not
define `direction`), timestamps in seconds. Fees: `taker_fee` / `maker_fee` strings in
percent (orderBooks docs: "Taker and maker fees are in percentage"). The `multiplier`
field has no documented meaning: a value other than 1 makes the multiplier unknown.
"""
from v2.identity import Instrument
from v2.venues.base import book, funding, num

XID = "lighter_rh"
LEGACY = "Lighter"
API = "https://api.rh.lighter.xyz/api/v1/"
CLASS_BY_PREMIUM = {100: "crypto", 50: "rwa", 1: "preipo"}


def markets_request():
    return ("GET", API + "orderBookDetails?filter=perp", None)


def book_request(market_id, limit=50):
    return ("GET", API + "orderBookOrders?market_id=%d&limit=%d" % (int(market_id),
                                                                   min(int(limit), 250)),
            None)


def fundings_request(market_id, start_s, end_s, resolution="1h"):
    return ("GET", API + "fundings?market_id=%d&resolution=%s&start_timestamp=%d"
            "&end_timestamp=%d&count_back=0" % (int(market_id), resolution,
                                                 int(start_s * 1000), int(end_s * 1000)),
            None)


def parse_markets(raw):
    out = []
    for m in raw.get("order_book_details") or []:
        if m.get("market_type") != "perp":
            continue
        cfg = m.get("market_config") or {}
        active = (m.get("status") == "active" and not m.get("is_frozen")
                  and not cfg.get("force_reduce_only") and not cfg.get("hidden"))
        premium = m.get("funding_premium_multiplier")
        cls = CLASS_BY_PREMIUM.get(premium, "unknown")
        mult_field = num(m.get("multiplier"))
        inst = Instrument(XID, str(m["market_id"]), m["symbol"], asset_class=cls,
                          quote_asset="USDG", price=num(m.get("mark_price")),
                          class_source="funding_premium_multiplier=%s" % premium
                          if cls != "unknown" else None,
                          active=active, session=cfg.get("trading_hours") or None)
        if mult_field is not None and abs(mult_field - 1) > 1e-12:
            inst.multiplier = None  # undocumented field differs from 1: unknown
        taker = num(m.get("taker_fee"))
        out.append(dict(
            instrument=inst, legacy_venue=LEGACY,
            mark=num(m.get("mark_price")), index=num(m.get("index_price")),
            funding=None,  # current rate: from the last settlement (fundings)
            fee_taker=taker / 100 if taker is not None and m.get("is_taker_fee_enabled")
            else (0.0 if taker is not None else None),
            fee_source="orderBookDetails taker_fee (percent)",
            open_interest=num(m.get("open_interest")),
            min_base=num(m.get("min_base_amount")),
            min_quote=num(m.get("min_quote_amount")),
        ))
    return out


def parse_book(raw):
    return book([(x["price"], x["remaining_base_amount"]) for x in raw.get("bids") or []],
                [(x["price"], x["remaining_base_amount"]) for x in raw.get("asks") or []],
                source=XID)


def parse_fundings(raw):
    """Settled hourly rows, oldest first: dict(ts s, rate fraction, value, sign)."""
    rows = []
    for x in raw.get("fundings") or []:
        d = x.get("direction")
        sign = 1 if d == "long" else -1 if d == "short" else None
        rate, ts = num(x.get("rate")), num(x.get("timestamp"))
        if sign is None or rate is None or ts is None:
            continue
        rows.append(dict(ts=ts, rate=abs(rate) / 100 * sign, unit="fraction",
                         kind="settled_public", sign_source="engine convention"))
    return sorted(rows, key=lambda r: r["ts"])


def current_funding(rows):
    """The last settlement as the current projection (labelled), or None."""
    if not rows:
        return None
    last = rows[-1]
    return funding(last["rate"], "fraction", 3600, last["ts"] + 3600,
                   kind="last_settled_projection", source=XID + " fundings")
