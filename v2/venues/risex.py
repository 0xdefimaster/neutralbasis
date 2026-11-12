"""risex: RISEx perps. Public GET reads on https://api.rise.trade/v1/ (the docs' OpenAPI
server is testnet; this mainnet host answers live, 2026-11-12 - see the source
register). Docs: developer.rise.trade (read 2026-11-12).

Asset class: `category` (live values: crypto, stocks, index_etf, commodity, "").
Funding: `current_funding_rate` decimal string ("18 decimals"), per funding interval;
`funding_rate_8h` = current x 8, so the interval is one hour, as `funding_interval`
(nanoseconds) states. Timestamps are nanoseconds unless stated otherwise. History =
SettleFundingRate events. Fees: not public (per-account JWT endpoint) -> UNKNOWN.
"""
from v2.identity import Instrument
from v2.venues.base import book, funding, num

XID = "risex"
LEGACY = "RISEx"
API = "https://api.rise.trade/v1/"
NS = 1e-9


def markets_request():
    return ("GET", API + "markets", None)


def book_request(market_id, limit=50):
    return ("GET", API + "orderbook?market_id=%d&limit=%d" % (int(market_id),
                                                             min(int(limit), 250)), None)


def funding_history_request(market_id):
    return ("GET", API + "markets/id/%d/funding-rate-history" % int(market_id), None)


def _rows(raw):
    data = raw.get("data", raw)
    return data.get("markets", data) if isinstance(data, dict) else data


def parse_markets(raw):
    out = []
    for m in _rows(raw):
        cfg = m.get("config") or {}
        cat = (m.get("category") or "").strip().lower() or "unknown"
        base = str(cfg.get("name") or m.get("display_name") or "").split("/")[0]
        active = bool(m.get("active", True)) and cfg.get("unlocked", True) and not m.get(
            "reduce_only")
        inst = Instrument(XID, str(m["market_id"]), base, asset_class=cat,
                          quote_asset=m.get("quote_asset_symbol"),
                          price=num(m.get("mark_price")),
                          class_source="category=%s" % cat if cat != "unknown" else None,
                          active=active)
        interval = num(m.get("funding_interval"))
        nxt = num(m.get("next_funding_time"))
        out.append(dict(
            instrument=inst, legacy_venue=LEGACY,
            mark=num(m.get("mark_price")), index=num(m.get("index_price")),
            funding=funding(num(m.get("current_funding_rate")), "fraction",
                            interval * NS if interval else None,
                            nxt * NS if nxt else None, source="risex current_funding_rate"),
            fee_taker=None, fee_source="UNKNOWN (per-account JWT /v1/user/fees)",
            open_interest=num(m.get("open_interest")),
            min_base=num(cfg.get("min_order_size")),
            step=num(cfg.get("step_size")),
        ))
    return out


def parse_book(raw):
    d = raw.get("data", raw)
    return book([(x["price"], x["quantity"]) for x in d.get("bids") or []],
                [(x["price"], x["quantity"]) for x in d.get("asks") or []], source=XID)


def parse_funding_history(raw):
    d = raw.get("data", raw)
    rows = []
    for x in d.get("records") or []:
        rate, end = num(x.get("funding_rate")), num(x.get("end_time"))
        if rate is None or end is None:
            continue
        start = num(x.get("start_time"))
        rows.append(dict(ts=end * NS, rate=rate, unit="fraction",
                         interval_s=(end - start) * NS if start else None,
                         kind="settled_public", index_price=num(x.get("index_price"))))
    return sorted(rows, key=lambda r: r["ts"])
