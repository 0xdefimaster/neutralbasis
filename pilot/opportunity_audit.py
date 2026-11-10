"""Why does a screen-visible pair (e.g. arb.sh) not reach NeutralBasis' economic evaluation?

Read-only, PAPER ONLY. Public market data through the endpoints the engine itself uses,
inside the pilot network guard. For each pair it reports four stages and never drops a
pair: a stage that cannot be computed is UNKNOWN with its reason.

  DISCOVERY      listed on both venues? present in the engine's funding matrix (live
                 snapshot file and the pilot)?
  NORMALIZATION  same instrument (price level, category), funding units and direction
  ECONOMICS      HISTORICAL 7-day net (arb.sh-like: realized funding + price change - 4
                 taker fees; backward-looking, NOT a forecast) and FORWARD funding-only
                 scenarios for 24/48/72/168 h (tools.shadow_ranking: 1/3/7-day agreement,
                 weakest window, fees, book cost, reserve; price convergence not credited)
  EXECUTION_GATE scanner candidate? research reasons / first deep-check blocker

usage: python -m pilot.opportunity_audit OUT.json [LIVE_MATRIX.json] [CAPTURE_ANALYSIS.json]
"""
import json
import math
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pilot import network_guard  # noqa: E402

network_guard.install()
import ccxt  # noqa: E402
import bot  # noqa: E402
import extended_rfq  # noqa: E402
from tools import shadow_ranking as sr  # noqa: E402

UA = {"User-Agent": "NeutralBasis-PaperAudit/1.0", "Accept": "application/json"}
EXT = "https://api.starknet.extended.exchange/api/v1/"
RISE = "https://api.rise.trade/v1/"
LC = "https://mainnet.zklighter.elliot.ai/api/v1/"
# Taker fee per fill. Extended / HL / Lighter Core: official docs (2026-11-10). RISEx:
# the engine's user-entered value (no venue document read) -> labelled as such.
FEE = dict(Extended=(0.00025, "docs"), Hyperliquid=(0.00045, "docs, base tier"),
           LighterCore=(0.00028, "docs, Premium; account type unverified"),
           RISEx=(0.0003, "engine user setting; venue fee not verified"))
PAIRS = [
    dict(asset="AERO", long="Extended", short="Hyperliquid", source="arb.sh 03:09 TRT"),
    dict(asset="AERO", long="Extended", short="RISEx", source="arb.sh"),
    dict(asset="SOL", long="RISEx", short="Extended", source="arb.sh"),
    dict(asset="XRP", long="RISEx", short="Extended", source="arb.sh"),
    dict(asset="CL", long="Variational", short="Extended", short_asset="WTI", source="arb.sh"),
]
DAYS = 8


def get(url, timeout=30, tries=4):
    """Public GET; on HTTP 429 waits the server's Retry-After / retry_after_seconds (the
    venues' per-IP budgets are shared with the live engine on this machine)."""
    import urllib.error
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as ex:
            if ex.code != 429 or i == tries - 1:
                raise
            wait = ex.headers.get("Retry-After")
            try:
                wait = float(wait or json.loads(ex.read() or b"{}").get("retry_after_seconds") or 10)
            except (ValueError, TypeError):
                wait = 10.0
            time.sleep(min(60.0, max(1.0, wait)))


hl = ccxt.hyperliquid({"enableRateLimit": True})
_ext_markets, _rise_markets = None, None


def ext_market(asset):
    global _ext_markets
    if _ext_markets is None:
        _ext_markets = {m["assetName"]: m for m in get(EXT + "info/markets")["data"]}
    return _ext_markets.get(asset)


def rise_market(asset):
    global _rise_markets
    if _rise_markets is None:
        _rise_markets = {m["underlying"].split("/")[0]: m
                         for m in get(RISE + "markets")["data"]["markets"]}
    return _rise_markets.get(asset)


def leg(venue, asset, now):
    """dict(listed, id, kind, funding=[(settled_at, rate/h)], closes=[(start, close)],
    books={name: book}, notes) or listed=False with the reason."""
    start = now - DAYS * 86400
    if venue == "Extended":
        m = ext_market(asset)
        if not m:
            return dict(listed=False, why="Extended info/markets: no market for " + asset)
        name = m["name"]
        f = get(EXT + "info/%s/funding?%s" % (name, urllib.parse.urlencode(
            dict(startTime=start * 1000, endTime=(now + 60) * 1000, limit=1000))))["data"]
        funding = sorted({round(bot.stamp(x["T"]) / 3600) * 3600: float(x["f"]) for x in f}.items())
        c = get(EXT + "info/candles/%s/trades?%s" % (name, urllib.parse.urlencode(
            dict(interval="PT1H", limit=DAYS * 24 + 2, endTime=(now + 60) * 1000))))["data"]
        closes = sorted((int(bot.stamp(x["T"])), float(x["c"])) for x in c)
        books = {}
        ob = get(EXT + "info/markets/%s/orderbook" % name)["data"]
        rest = dict(bids=[(float(x["price"]), float(x["qty"])) for x in ob.get("bid") or []],
                    asks=[(float(x["price"]), float(x["qty"])) for x in ob.get("ask") or []])
        if m.get("isRfq"):
            books["indicative_REST_not_firm"] = rest
            books["real_RFQ_book_not_a_guaranteed_fill"] = rfq_book(name)
        else:
            books["orderbook"] = rest
        return dict(listed=True, id=name, kind="RFQ" if m.get("isRfq") else "orderbook",
                    category=m.get("category"), funding=funding, closes=closes, books=books)
    if venue == "Hyperliquid":
        sym = next((s for s, m in hl.load_markets().items() if m.get("swap") and m.get("base") == asset
                    and m.get("settle") == "USDC" and ":" not in m.get("base", "")), None)
        if not sym:
            return dict(listed=False, why="Hyperliquid: no USDC perp for " + asset)
        fr = {}
        since = start * 1000
        for _ in range(3):
            rows = hl.fetch_funding_rate_history(sym, since)
            for r in rows:
                fr[round(r["timestamp"] / 3600000) * 3600] = float(r["fundingRate"])
            if len(rows) < 400:
                break
            since = rows[-1]["timestamp"] + 1
        ohlcv = hl.fetch_ohlcv(sym, "1h", start * 1000, 1000)
        b = hl.fetch_order_book(sym, 100)
        return dict(listed=True, id=sym, kind="orderbook", funding=sorted(fr.items()),
                    closes=[(int(r[0] // 1000), float(r[4])) for r in ohlcv],
                    books=dict(orderbook=dict(bids=[(p, q) for p, q, *_ in b["bids"]],
                                              asks=[(p, q) for p, q, *_ in b["asks"]])))
    if venue == "RISEx":
        m = rise_market(asset)
        if not m:
            return dict(listed=False, why="RISEx v1/markets: no market for " + asset)
        mid = m["market_id"]
        h = get(RISE + "markets/id/%s/funding-rate-history?%s" % (mid, urllib.parse.urlencode(
            dict(start_time=start * 10**9, end_time=(now + 60) * 10**9, limit=1000))))
        funding = sorted({round(float(x["end_time"]) / 1e9 / 3600) * 3600: float(x["funding_rate"])
                          for x in h["data"]["records"]}.items())
        tv = get(RISE + "markets/id/%s/trading-view-data?%s" % (mid, urllib.parse.urlencode(
            {"interval": 3600 * 10**9, "from": start * 10**9, "to": (now + 60) * 10**9})))
        closes = sorted((int(float(x["time"]) / 1e9 if float(x["time"]) > 1e14 else float(x["time"])),
                         float(x["close"])) for x in tv["data"]["data"])
        ob = get(RISE + "orderbook?market_id=%s&limit=250" % mid)["data"]
        book = dict(bids=[(float(x["price"]), float(x["quantity"])) for x in ob["bids"]],
                    asks=[(float(x["price"]), float(x["quantity"])) for x in ob["asks"]])
        return dict(listed=True, id=mid, kind="orderbook", category=m.get("category"),
                    funding=funding, closes=closes, books=dict(orderbook=book),
                    notes="current_funding_rate is the last settled rate (risex report)")
    if venue == "Variational":
        stats = get("https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats")
        listings = stats.get("listings") or []
        hit = [x for x in listings if json.dumps(x).upper().find('"%s"' % asset) >= 0]
        return dict(listed=bool(hit), id=None, kind="UNKNOWN",
                    why=("no connector; metadata/stats fields are undocumented, so no funding, "
                         "price type or contract value is interpreted"),
                    listing_fields=sorted(hit[0].keys()) if hit else None)
    return dict(listed=False, why="venue not covered by this audit")


def rfq_book(name):
    s = extended_rfq.RfqStream(name)
    ws = extended_rfq.WebSocket(extended_rfq.STREAM + name, timeout=40, poll=1.0)
    s.connected = True
    try:
        end = time.time() + 30
        while time.time() < end and not s.book.ready:
            s.handle(ws.recv(), time.time())
    finally:
        ws.close()
    v = s.view(time.time())
    return dict(bids=v["bids"], asks=v["asks"])


def historical_7d(L, S, fee_l, fee_s, now):
    """arb.sh-like: opened 7 days ago, closed now. Funding settled in the window (both
    legs), price change of each leg from 1h trade closes, minus 4 taker fees. Per $1."""
    start = now - 7 * 86400
    fl = {t: r for t, r in L["funding"] if start < t <= now}
    fs = {t: r for t, r in S["funding"] if start < t <= now}
    common = sorted(set(fl) & set(fs))
    if len(common) < 0.9 * 168:
        return dict(status="UNKNOWN", why="settled funding covers %d of 168 h on both legs" % len(common))
    funding = sum(fs[t] - fl[t] for t in common)

    def close_at(closes, t):
        xs = [c for ts, c in closes if ts <= t]
        return xs[-1] if xs else None

    l0, l1 = close_at(L["closes"], start), close_at(L["closes"], now)
    s0, s1 = close_at(S["closes"], start), close_at(S["closes"], now)
    if None in (l0, l1, s0, s1):
        return dict(status="UNKNOWN", why="price closes missing", funding_bp=round(funding * 1e4, 1))
    price = (l1 / l0 - 1) - (s1 / s0 - 1)
    fees = 2 * (fee_l + fee_s)
    return dict(status="OK", funding_bp=round(funding * 1e4, 1), price_bp=round(price * 1e4, 1),
                fees_bp=round(fees * 1e4, 1), net_bp=round((funding + price - fees) * 1e4, 1),
                net_usd_per_10k=round((funding + price - fees) * 1e4, 2),
                label="historical, backward-looking; not a forecast")


def forward(L, S, fee_l, fee_s, now):
    carry = sr.carry_series(L["funding"], S["funding"])
    gaps = sr.gap_series(L["closes"], S["closes"])
    out = {}
    for lname, lb in L["books"].items():
        for sname, sb in S["books"].items():
            scen = "%s + %s" % (lname, sname)
            per_size = {}
            for size in (100.0, 500.0, 1000.0, 5000.0):
                dl, ds = sr.round_trip_drag(lb, size), sr.round_trip_drag(sb, size)
                if dl is None or ds is None:
                    per_size[int(size)] = "INSUFFICIENT_LIQUIDITY"
                    continue
                s = sr.score(carry, gaps, now, dict(fees=2 * (fee_l + fee_s), drag=dl + ds),
                             price=False, funding_lag=3600)
                if s["status"] == "UNKNOWN":
                    per_size[int(size)] = "UNKNOWN (1/3/7-day funding windows incomplete)"
                    continue
                per_size[int(size)] = dict(
                    drag_bp=round((dl + ds) * 1e4, 1), fees_bp=round(2 * (fee_l + fee_s) * 1e4, 1),
                    reserve_bp=round(s["gap_sd_7d"] * 1e4, 1),
                    carry_bp_h=dict(d1=round(s["carry_1d"] * 1e4, 3), d3=round(s["carry_3d"] * 1e4, 3),
                                    d7=round(s["carry_7d"] * 1e4, 3)),
                    flip_share_7d=round(s["flip_share_7d"], 2),
                    value_bp={h: round(x["value"] * 1e4, 1) for h, x in s["horizons"].items()})
            out[scen] = per_size
    return out


def main():
    out_path = sys.argv[1]
    live = json.load(open(sys.argv[2], encoding="utf8")) if len(sys.argv) > 2 else None
    capture = json.load(open(sys.argv[3], encoding="utf8")) if len(sys.argv) > 3 else None
    pilot_fm = get("http://127.0.0.1:18765/api/funding-matrix", timeout=600)
    pilot_state = get("http://127.0.0.1:18765/api/state", timeout=600)["wide_paper"]
    now = int(time.time()) // 3600 * 3600

    def in_matrix(m, asset, venue):
        if m is None:
            return None
        row = next((r for r in m["assets"] if r["asset"] == asset), None)
        return bool(row and venue in row["cells"])

    res = []
    for p in PAIRS:
        a, la, sa = p["asset"], p["asset"], p.get("short_asset", p["asset"])
        L, S = leg(p["long"], la, now), leg(p["short"], sa, now)
        r = dict(pair="%s %s>%s" % (a if la == sa else "%s/%s" % (la, sa), p["long"], p["short"]),
                 source=p["source"])
        r["discovery"] = dict(
            long_listed=L.get("listed"), short_listed=S.get("listed"),
            long_why=L.get("why"), short_why=S.get("why"),
            live_matrix=dict(long=in_matrix(live, la, p["long"]), short=in_matrix(live, sa, p["short"])),
            pilot_matrix=dict(long=in_matrix(pilot_fm, la, p["long"]), short=in_matrix(pilot_fm, sa, p["short"])),
            kinds=dict(long=L.get("kind"), short=S.get("kind")))
        if not (L.get("listed") and S.get("listed")) or L.get("kind") == "UNKNOWN" or S.get("kind") == "UNKNOWN":
            r["normalization"] = dict(status="UNKNOWN", why=L.get("why") or S.get("why"),
                                      listing_fields=L.get("listing_fields") or S.get("listing_fields"))
            r["economics"] = dict(status="UNKNOWN", why="a leg has no interpretable funding/price data")
            res.append(r)
            continue
        gaps = sr.gap_series(L["closes"], S["closes"])
        med = statistics.median(v for _, v in gaps) if gaps else None
        same = med is not None and abs(med) <= 0.05 and la == sa
        r["normalization"] = dict(
            status="OK" if same else "NOT_NORMALIZED",
            median_price_gap_bp=None if med is None else round(med * 1e4, 1),
            categories=dict(long=L.get("category"), short=S.get("category")),
            funding_units="hourly rate per payment; positive = long pays (both legs)",
            notes=L.get("notes") or S.get("notes"))
        fl, fs = FEE[p["long"]][0], FEE[p["short"]][0]
        r["fees"] = {p["long"]: FEE[p["long"]], p["short"]: FEE[p["short"]]}
        r["historical_7d"] = historical_7d(L, S, fl, fs, now)
        r["forward"] = forward(L, S, fl, fs, now)
        res.append(r)
    # Scanner / gate status (pilot now; live from the read-only capture analysis).
    cands = {}
    for strat, lst in (("funding", pilot_state.get("candidates") or []),
                       ("convergence", pilot_state.get("conv_candidates") or [])):
        for c in lst:
            cands[(strat, c["asset"], c["long"], c["short"])] = c
    for r, p in zip(res, PAIRS):
        a = p["asset"]
        pilot = cands.get(("funding", a, p["long"], p["short"]))
        r["scanner"] = dict(
            pilot_funding_candidate=None if pilot is None else dict(
                rank=pilot.get("rank"), screen_bp=round((pilot.get("score") or 0) * 1e4, 1),
                research=pilot.get("research")),
            pilot_candidate_count=pilot_state.get("candidate_count"),
            note=None if pilot else "not in the pilot's top-40 funding list (state shows 40)")
        if capture:
            key = "funding|%s|%s|%s" % (a, p["long"], p["short"])
            c = capture["candidates"].get(key)
            r["scanner"]["live_capture"] = None if c is None else dict(
                polls=c["polls"], best_rank=c["best_rank"], best_screen_bp=round(c["best_score"] * 1e4, 1),
                assessed=c["assessed"], first_gate=c["first_gate"], research=c["research"])
    Path(out_path).write_text(json.dumps(dict(at=now, pairs=res), indent=1, default=str), encoding="utf8")
    print(json.dumps(dict(at=now, pairs=res), indent=1, default=str)[:12000])


if __name__ == "__main__":
    main()
