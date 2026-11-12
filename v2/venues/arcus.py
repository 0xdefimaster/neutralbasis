"""arcus: Arcus Perps. Public GET reads on https://api.arcus.xyz/v1/ (docs.arcus.xyz,
read 2026-11-12).

Asset class: `category` (live: CRYPTO, EQUITIES, COMMODITIES, INDICES); `type`
PERPETUAL; `fullAssetName`. Fees in ppm (`effectiveTakerFeePpm`; docs: "100 ppm =
0.01%"). Funding hourly, `fundingRate` a per-hour fraction (docs formula), positive =
longs pay; `nextFundingAt` in seconds. L2: GET /v1/l2OrderBook/{market} (timestamp in
microseconds, lastSequenceId). `fundingRates` history answered HTTP 400 without
parameters (2026-11-12); its parameters are not read yet.
"""
from v2.identity import Instrument
from v2.venues.base import book, funding, num

XID = "arcus"
LEGACY = "Arcus"
API = "https://api.arcus.xyz/v1/"


def markets_request():
    return ("GET", API + "markets", None)


def book_request(market, levels=20):
    return ("GET", API + "l2OrderBook/%s?nLevels=%d" % (market, min(int(levels), 100)), None)


def _rows(raw):
    return raw if isinstance(raw, list) else (raw.get("markets") or raw.get("data") or [])


def parse_markets(raw):
    out = []
    for m in _rows(raw):
        cat = (m.get("category") or "unknown").lower()
        ctype = "perp" if m.get("type") == "PERPETUAL" else str(m.get("type") or "?").lower()
        inst = Instrument(XID, m["marketDisplayName"], m["baseAsset"], asset_class=cat,
                          contract_type=ctype, quote_asset=m.get("quoteAsset"),
                          name=m.get("fullAssetName"), price=num(m.get("markPrice")),
                          class_source="category=%s" % m.get("category"),
                          active=m.get("status") == "ONLINE",
                          session="RTH" if m.get("regularTradingHours") else "24/7")
        taker = num(m.get("effectiveTakerFeePpm"))
        nxt = num(m.get("nextFundingAt"))
        out.append(dict(
            instrument=inst, legacy_venue=LEGACY,
            mark=num(m.get("markPrice")), index=num(m.get("oraclePrice")),
            funding=funding(num(m.get("fundingRate")), "fraction", 3600, nxt,
                            sign="positive: longs pay", source="arcus fundingRate"),
            fee_taker=taker / 1e6 if taker is not None else None,
            fee_source="effectiveTakerFeePpm (ppm, base tier)",
            open_interest=num(m.get("openInterest")),
            min_base=num(m.get("minOrderSize")), min_quote=num(m.get("minOrderNotional")),
            step=num(m.get("stepSize")), outside_rth=m.get("isOutsideRth"),
        ))
    return out


def parse_book(raw):
    ts = num(raw.get("timestamp"))
    return book(raw.get("bids") or [], raw.get("asks") or [],
                ts=ts / 1e6 if ts else None, seq=raw.get("lastSequenceId"), source=XID)
