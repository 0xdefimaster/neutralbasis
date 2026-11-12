"""A CCXT-shaped client for tradeXYZ under V2, built only on xyz-scoped info queries.

wide_markets drives every venue through a CCXT exchange object. CCXT's hyperliquid
loader first asks `spotMeta`, outside the xyz scope, so under V2 (NB_V2=1) it is
refused and tradeXYZ would vanish. This class answers the subset of the CCXT API that
wide_markets uses for a venue (load_markets, markets, market, has, fetch_funding_rates,
fetch_tickers, fetch_order_book, fetch_funding_rate_history, fetch_ohlcv, set_markets)
from scoped requests only:

  metaAndAssetCtxs dex="xyz"   markets, funding, mark/oracle, 24h notional volume
  l2Book coin="xyz:SYM"         order book
  fundingHistory coin="xyz:SYM" settled funding
  candleSnapshot req.coin="xyz:SYM"

Every request goes through `self.fetch(url, method, headers, body)`, the same hook
wide_markets wraps for its audit log, HTTP statistics and 429 backoff.

Funding: Hyperliquid pays every hour, positive = longs pay, on the oracle price, and
the same interval applies to HIP-3 perps (trading/funding docs, read 2026-11-12). The
next payment time is therefore the next whole hour (derived, labelled).
"""
import json
import math
import time
import urllib.request

from v2.venues import tradexyz as tx
from v2.venues.base import UA, num

try:
    import ccxt
    TICK_SIZE = ccxt.TICK_SIZE
except Exception:  # pragma: no cover - ccxt is installed in the venv
    ccxt, TICK_SIZE = None, 4

QUOTE = "USDC"


class XyzClient:
    id = "tradexyz_v2"
    precisionMode = TICK_SIZE
    urls = {"doc": "https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-"
                   "proposals-hips/hip-3-builder-deployed-perpetuals"}
    has = {
        "fetchFundingRates": True, "fetchFundingRate": False,
        "fetchFundingRateHistory": True, "fetchOHLCV": True, "fetchOrderBook": True,
        "fetchTickers": True, "fetchTicker": False, "fetchOpenInterests": False,
        "fetchOpenInterest": False, "fetchMarkPrices": False, "fetchFundingIntervals": False,
    }

    def __init__(self, transport=None, clock=time.time):
        self.options = {}
        self.neutralbasis_dex = tx.DEX
        self.markets = {}
        self.currencies = {}
        self.last_response_headers = {}
        self.last_http_response = None
        self.clock = clock
        self._transport = transport
        self._ctx_cache = None

    # -- transport ------------------------------------------------------------------
    def fetch(self, url, method="GET", headers=None, body=None):
        if self._transport is not None:
            return self._transport(url, method, headers, body)
        data = body.encode() if isinstance(body, str) else body
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=dict(UA, **(headers or {})))
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read(40_000_000)
            self.last_response_headers = dict(r.headers.items())
        self.last_http_response = raw.decode("utf-8", "replace")
        return json.loads(raw)

    def info(self, body):
        return self.fetch(tx.INFO_URL, "POST", {"Content-Type": "application/json"},
                          json.dumps(body))

    # -- markets --------------------------------------------------------------------
    def _meta(self, max_age=5.0):
        c = self._ctx_cache
        if c and self.clock() - c[0] <= max_age:
            return c[1]
        answer = self.info(tx.meta_and_ctxs())
        self._ctx_cache = (self.clock(), answer)
        return answer

    def load_markets(self, reload=False, params=None):
        if self.markets and not reload:
            return self.markets
        answer = self._meta(0 if reload else 5.0)
        out = {}
        for u, rec in zip(answer[0]["universe"], tx.parse_markets(answer)):
            name = u["name"]
            base = name[len(tx.PREFIX):]
            symbol = "%s/%s:%s" % (base, QUOTE, QUOTE)
            sz = u.get("szDecimals")
            out[symbol] = dict(
                id=name, symbol=symbol, base=base, quote=QUOTE, settle=QUOTE,
                type="swap", swap=True, linear=True, contract=True,
                active=not u.get("isDelisted"), contractSize=1.0,
                precision=dict(amount=10.0 ** -sz if sz is not None else None, price=None),
                limits=dict(leverage=dict(max=num(u.get("maxLeverage"))),
                            amount=dict(min=None), cost=dict(min=None)),
                taker=rec["taker"], maker=rec["maker"],
                neutralbasis_category="RWA",
                info=dict(u, dex=tx.DEX),
            )
        self.markets = out
        return out

    def set_markets(self, markets, currencies=None):
        self.markets = markets
        self.currencies = currencies or {}

    def market(self, symbol):
        return self.markets[symbol]

    # -- funding / prices -------------------------------------------------------------
    def fetch_funding_rates(self, symbols=None, params=None):
        answer = self._meta()
        now_ms = int(self.clock() * 1000)
        nxt = int(math.floor(self.clock() / 3600) + 1) * 3600 * 1000
        by_id = {m["id"]: m["symbol"] for m in self.markets.values()}
        out = {}
        for u, c in zip(answer[0]["universe"], answer[1]):
            sym = by_id.get(u["name"])
            if sym is None or (symbols and sym not in symbols):
                continue
            rate = num(c.get("funding"))
            if rate is None:
                continue
            out[sym] = dict(symbol=sym, fundingRate=rate, markPrice=num(c.get("markPx")),
                            indexPrice=num(c.get("oraclePx")), interval="1h",
                            timestamp=now_ms, nextFundingTimestamp=nxt,
                            info=dict(c, next_funding="derived: next whole hour"))
        return out

    def fetch_tickers(self, symbols=None, params=None):
        answer = self._meta()
        now_ms = int(self.clock() * 1000)
        by_id = {m["id"]: m["symbol"] for m in self.markets.values()}
        out = {}
        for u, c in zip(answer[0]["universe"], answer[1]):
            sym = by_id.get(u["name"])
            if sym is None or (symbols and sym not in symbols):
                continue
            out[sym] = dict(symbol=sym, timestamp=now_ms, last=num(c.get("midPx")),
                            bid=None, ask=None, quoteVolume=num(c.get("dayNtlVlm")),
                            info=dict(markPrice=num(c.get("markPx"))))
        return out

    def fetch_order_book(self, symbol, limit=None, params=None):
        raw = self.info(tx.l2_book(self.market(symbol)["id"]))
        levels = raw.get("levels") or [[], []]
        bids = [[num(x["px"]), num(x["sz"])] for x in levels[0]]
        asks = [[num(x["px"]), num(x["sz"])] for x in levels[1]]
        if limit:
            bids, asks = bids[:limit], asks[:limit]
        return dict(symbol=symbol, bids=bids, asks=asks, timestamp=raw.get("time"),
                    nonce=None)

    def fetch_funding_rate_history(self, symbol, since=None, limit=None, params=None):
        start = since if since is not None else int((self.clock() - 86400) * 1000)
        rows = self.info(tx.funding_history(self.market(symbol)["id"], start))
        out = [dict(symbol=symbol, fundingRate=num(r.get("fundingRate")),
                    timestamp=r.get("time"), info=r) for r in rows or []]
        return out[:limit] if limit else out

    def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=None, params=None):
        end = int(self.clock() * 1000)
        start = since if since is not None else end - 30 * 86400 * 1000
        rows = self.info(tx.candles_request(self.market(symbol)["id"], timeframe, start, end))
        out = [[r["t"], num(r["o"]), num(r["h"]), num(r["l"]), num(r["c"]), num(r["v"])]
               for r in rows or []]
        return out[:limit] if limit else out
