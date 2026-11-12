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
                        kind="account_payment", scope="ACCOUNT",
                        status="ACCOUNT_PAYMENT_UNVERIFIED"))
    return sorted(out, key=lambda r: r["ts"])



def infer_rate_unit(rows, mark_at, min_rows=3, tolerance=0.05):
    """Check the unit of userFundings `fundingRate` against the payment itself.

    docs.txflow.com/trade/funding.md: payment = size x mark price x rate (positive rate:
    longs pay). For each row with szi, amount, rate_raw and a mark price AT that funding
    time (`mark_at(ts)` -> price or None; it must come from venue data, never guessed),
    the implied rate is -amount / (szi x mark). The unit is FRACTION or PERCENT only if
    at least `min_rows` rows agree with one reading within `tolerance` (relative) and
    none agrees with the other only; otherwise UNVERIFIED. Returns (unit, details)."""
    votes = dict(FRACTION=0, PERCENT=0, NEITHER=0)
    checked = []
    for r in rows or []:
        szi, amount, raw = r.get("szi"), r.get("amount"), r.get("rate_raw")
        mark = mark_at(r["ts"]) if mark_at else None
        if not szi or amount is None or not raw or not mark:
            continue
        implied = -amount / (szi * mark)
        ok_f = abs(implied - raw) <= tolerance * abs(raw)
        ok_p = abs(implied - raw / 100.0) <= tolerance * abs(raw / 100.0)
        votes["FRACTION" if ok_f and not ok_p else "PERCENT" if ok_p and not ok_f
              else "NEITHER"] += 1
        checked.append(dict(ts=r["ts"], implied=implied, raw=raw, mark=mark))
    unit = "UNVERIFIED"
    for u, other in (("FRACTION", "PERCENT"), ("PERCENT", "FRACTION")):
        if votes[u] >= min_rows and votes[other] == 0 and votes["NEITHER"] == 0:
            unit = u
    return unit, dict(votes=votes, checked=checked)


def apply_rate_unit(rows, unit):
    """Rows with `rate` (fraction per payment) set ONLY for a verified unit; any other
    unit leaves rate None (no market funding yield is derived)."""
    k = {"FRACTION": 1.0, "PERCENT": 0.01}.get(unit)
    return [dict(r, rate=None if k is None or r.get("rate_raw") is None
                 else r["rate_raw"] * k, rate_unit=unit) for r in rows]


# ----------------------------------------------------------------- Phase 10: userFundings
# Contract (docs.txflow.com/api/info/userFundings + /api/websocket/userFundings, read
# 2026-11-13): POST /info {"type": "userFundings", "user": ADDRESS, ...}; "No auth - the
# body user is the subject" (no signature, no key); newest-first pages (limit <= 2000,
# offset, hasMore); "Served from an index - can briefly lag the latest block"; perp
# markets only. Row fields: time (ms), coin, usdc, szi, fundingRate, amount.
#   fundingRate  WS page: "percentage form (e.g. "0.01" means 0.01%)"; REST: NOT STATED
#   szi          WS page: "Signed position size at settlement time"
#   usdc         WS page: "Negative = paid, positive = received"
#   amount       REST page: collected > 0, paid < 0 (equality with usdc NOT STATED)
#   coin         request: a name ("BTC", "BTC-USDC") or the perpMeta numeric id; the
#                response form is NOT STATED (example "BTC" on REST, "4" on WS)
# Mainnet host for this type: NOT STATED on the page (its example is testnet); the same
# /info endpoint as every other info type is used, and the unit is verified from DATA:
# until payments agree with size x price x rate the status stays
# ACCOUNT_PAYMENT_UNVERIFIED and no market yield is derived.
DOCUMENTED_RATE_FORM = "PERCENT (WS docs); REST page NOT STATED"
UNVERIFIED, VERIFIED = "ACCOUNT_PAYMENT_UNVERIFIED", "ACCOUNT_PAYMENT_CONFIRMED"


def coin_map(meta):
    """{key: dict(index, name, base, market_id, exchange_id)} where key is the numeric
    index, the perpMeta name and "BASE-QUOTE"; used to map a userFundings `coin`
    (name or id) onto the V2 market_id (= the index as a string)."""
    out = {}
    for u in ((meta or {}).get("data") or meta or {}).get("universe", []):
        rec = dict(index=str(u["index"]), name=u.get("name"), base=u.get("baseCurrency"),
                   market_id=str(u["index"]), exchange_id=XID)
        keys = {str(u["index"]), str(u.get("name") or ""), str(u.get("baseCurrency") or "")}
        if u.get("baseCurrency") and u.get("quoteCurrency"):
            keys.add("%s-%s" % (u["baseCurrency"], u["quoteCurrency"]))
        for k in keys - {""}:
            if k in out and out[k]["index"] != rec["index"]:
                out[k] = dict(index=None, ambiguous=True)      # never guess
            else:
                out[k] = rec
    return out


def resolve_coin(cmap, coin):
    r = cmap.get(str(coin))
    return None if r is None or r.get("ambiguous") else r


def verify_payments(rows, mark_at, min_rows=3, tolerance=0.05):
    """Payment evidence status for parsed userFundings rows of ONE account:
    VERIFIED (the unit is proven by the payments themselves: usdc/amount agree, and
    -usdc / (szi x mark at that time) matches fundingRate in ONE unit for >= min_rows
    rows) or UNVERIFIED with the reason. `mark_at(ts)` must return venue data or None."""
    mismatched = [r for r in rows if r.get("usdc") is not None and r.get("amount") is not None
                  and abs(r["usdc"] - r["amount"]) > 1e-9 * max(1.0, abs(r["usdc"]))]
    unit, det = infer_rate_unit([dict(r, amount=r["usdc"] if r.get("usdc") is not None
                                      else r.get("amount")) for r in rows], mark_at,
                                min_rows, tolerance)
    status = VERIFIED if unit in ("FRACTION", "PERCENT") and not mismatched else UNVERIFIED
    return dict(status=status, unit=unit, documented=DOCUMENTED_RATE_FORM,
                usdc_amount_mismatch=len(mismatched), rows=len(rows),
                reason=None if status == VERIFIED else
                "usdc != amount on %d rows" % len(mismatched) if mismatched else
                "unit not proven by payments (%s)" % det["votes"], details=det)


def market_history_from_account(rows):
    """Refused: one account's payments are not the market's funding history (only the
    hours that account held a position, at its own size)."""
    raise ValueError("ACCOUNT_SCOPE_ONLY: userFundings rows describe one account, never "
                     "the market's complete funding history")
