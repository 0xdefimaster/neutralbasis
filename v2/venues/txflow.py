"""txflow: TxFlow Perps. POST https://rpc.txflow.com/info read queries with
`x-api-version: 2026-08-16` (docs.txflow.com/api, read 2026-11-12). /exchange is never
called.

Asset class: `marketTags`. Its `l1` operational tabs "TradFi" and "Crypto" list the
sub-tags (`l2Refs`) under them; `instrumentTags[].tagIds` refer to those sub-tags
(docs: "Sub-tags referenced by l1[].l2Refs and instrumentTags[].tagIds"). An instrument
tagged under one tab only gets that tab's class; under both or none it stays unknown.
Sub-tag names map to fine classes (Stocks, Commodities, FX, Pre-IPO, Indices).

Funding: activeAssetCtx `nodeCtx.funding` in PERCENT ("0.01 means 0.01%"), hourly,
positive = longs pay, paid on the mark price. No public funding history exists.
"""
from v2.identity import Instrument
from v2.venues.base import book, funding, num

XID = "txflow"
LEGACY = "TxFlow"
INFO = "https://rpc.txflow.com/info"
HEADERS = {"x-api-version": "2026-08-16"}
FINE = {"stocks": "equity", "commodities": "commodity", "fx": "fx", "pre-ipo": "preipo",
        "indices": "index"}


def meta_request():
    return ("POST", INFO, {"type": "perpMeta", "exchangeId": "all"})


def tags_request():
    return ("POST", INFO, {"type": "marketTags"})


def ctx_request(coin):
    return ("POST", INFO, {"type": "activeAssetCtx", "coin": str(coin)})


def book_request(coin):
    return ("POST", INFO, {"type": "l2Book", "coin": str(coin)})


def classes(market_tags):
    """{instrumentId: (fine class, source)} from a marketTags answer."""
    d = market_tags.get("data", market_tags)
    names = {x["id"]: x["name"] for x in d.get("l2") or []}
    tab = {}
    for l1 in d.get("l1") or []:
        if l1.get("kind") == "operational" and l1.get("name") in ("TradFi", "Crypto"):
            for ref in l1.get("l2Refs") or []:
                tab.setdefault(ref, set()).add(l1["name"])
    out = {}
    for row in d.get("instrumentTags") or []:
        tabs = set()
        for t in row.get("tagIds") or []:
            tabs |= tab.get(t, set())
        if tabs == {"Crypto"}:
            out[row["instrumentId"]] = ("crypto", "marketTags Crypto tab")
        elif tabs == {"TradFi"}:
            fine = {FINE.get(str(names.get(t, "")).lower()) for t in row.get("tagIds") or []}
            fine.discard(None)
            out[row["instrumentId"]] = (fine.pop() if len(fine) == 1 else "rwa",
                                        "marketTags TradFi tab")
        elif tabs:
            out[row["instrumentId"]] = ("conflict", "marketTags: both tabs")
    return out


def parse_markets(meta, market_tags=None, ctxs=None):
    """perpMeta universe + marketTags classes + {coin: activeAssetCtx answer}."""
    cls = classes(market_tags) if market_tags else {}
    out = []
    for u in meta["data"]["universe"]:
        coin = str(u["index"])
        c, src = cls.get(u["index"], ("unknown", None))
        ctx = ((ctxs or {}).get(coin) or {}).get("data") or {}
        node = ctx.get("nodeCtx") or {}
        mark = num(node.get("markPx") or ctx.get("markPx"))
        prev, nxt = num(node.get("prevFundingTime")), num(node.get("nextFundingTime"))
        inst = Instrument(XID, coin, u["baseCurrency"],
                          asset_class="unknown" if c == "conflict" else c,
                          quote_asset=u.get("quoteCurrency"), price=mark,
                          class_source=src if c not in ("unknown", "conflict") else None,
                          active=not u.get("delisted") and not u.get("haltTrading"))
        out.append(dict(
            instrument=inst, legacy_venue=LEGACY, name=u.get("name"),
            mark=mark, index=num(node.get("oraclePx") or ctx.get("oraclePx")),
            funding=funding(num(node.get("funding")), "percent",
                            (nxt - prev) / 1000 if nxt and prev else None,
                            nxt / 1000 if nxt else None, sign="positive: longs pay",
                            source="txflow activeAssetCtx nodeCtx.funding"),
            fee_taker=0.00045, fee_source="docs VIP 0 taker 0.0450% (account tier unverified)",
            open_interest=num(node.get("openInterest")),
            step=num(u.get("basePrecision")), tick=num(u.get("priceTick")),
            tags_conflict=c == "conflict",
        ))
    return out


def parse_book(raw):
    d = raw.get("data", raw)
    levels = d.get("levels") or [[], []]
    t = num(d.get("time"))
    return book([(x["px"], x["sz"]) for x in levels[0]], [(x["px"], x["sz"]) for x in levels[1]],
                ts=t / 1000 if t else None, source=XID)



# userFundings (docs.txflow.com/api/info/userFundings.md, read 2026-11-12): body
# {type, user, coin?, startTimeMs?, endTimeMs?, limit<=2000, offset}; answer
# {data: {data: [FundingRow], hasMore, total?}}, FundingRow = time (ms), coin, usdc,
# szi, fundingRate, amount (signed). The unit of fundingRate in these rows (percent vs
# fraction) is not stated on that page -> kept raw; no rate is derived from it until
# it is checked against activeAssetCtx samples of the same hours.
USER_FUNDINGS_RATE_UNIT = "UNKNOWN"


def user_fundings_request(address, coin, start_ms, end_ms, limit=2000, offset=0):
    return ("POST", INFO, {"type": "userFundings", "user": str(address), "coin": str(coin),
                           "startTimeMs": int(start_ms), "endTimeMs": int(end_ms),
                           "limit": int(limit), "offset": int(offset)})


def parse_user_fundings(raw):
    """Account payment rows (ACCOUNT_PAYMENT evidence) for the authorized address:
    [dict(ts, coin, amount, szi, usdc, rate_raw, rate=None, kind="account_payment")],
    oldest first. `rate` stays None while USER_FUNDINGS_RATE_UNIT is UNKNOWN."""
    d = (raw or {}).get("data", raw) or {}
    rows = d.get("data") if isinstance(d, dict) else d
    out = []
    for x in rows or []:
        t = num(x.get("time"))
        if t is None:
            continue
        out.append(dict(ts=t / 1000.0, coin=x.get("coin"), amount=num(x.get("amount")),
                        szi=num(x.get("szi")), usdc=num(x.get("usdc")),
                        rate_raw=num(x.get("fundingRate")), rate=None,
                        kind="account_payment"))
    return sorted(out, key=lambda r: r["ts"])
