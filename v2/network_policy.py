"""V2 egress policy: host + method + path + payload scope + exchange_id, fail-closed.

PAPER ONLY, public reads only. Every request is matched to exactly one of the eight
exchange_ids; anything else (CEX, legacy DEX, unknown host, write path, account
query, unscoped payload) is refused.

tradeXYZ exception: tradeXYZ is its own venue but reads Hyperliquid's public info API
(api.hyperliquid.xyz POST /info). Only queries scoped to the HIP-3 dex "xyz" pass:
  meta / metaAndAssetCtxs        body "dex" must be exactly "xyz"
  l2Book / fundingHistory        body "coin" must start with "xyz:"
  candleSnapshot                 body "req.coin" must start with "xyz:"
  perpDexs                       allowed, flagged BROAD_METADATA (the answer lists every
                                 dex; only the xyz entry may be consumed)
Native perps (dex "" or a coin without "xyz:"), other HIP-3 dexes, predictedFundings
(first dex only), allMids, spot and every user/account query are refused. Source:
Hyperliquid info-endpoint docs read 2026-11-12 (docs/v2/04_API_SOURCE_REGISTER.md).

Hosts and paths come from each venue's official docs (read 2026-11-12) or, where the
docs do not state them, from the legacy code's working calls (marked UNVERIFIED_DOC in
the source register). popdex_perps and veranta have no allowed endpoint yet.
"""
import json
import re

ALLOW = "ALLOW"
BROAD = "BROAD_METADATA"

# Keys that turn a public query into an account/trading request.
ACCOUNT_KEYS = ("user", "signature", "action", "nonce", "vaultAddress", "agent",
                "wallet", "address", "privateKey", "apiKey")

HL_HOST = "api.hyperliquid.xyz"
XYZ = "xyz"
XYZ_PREFIX = "xyz:"

GET_ROUTES = {
    # exchange_id: (host, [path regex, ...])
    "lighter_rh": ("api.rh.lighter.xyz", [
        r"/api/v1/(orderBooks|orderBookDetails|orderBookOrders|recentTrades|fundings|"
        r"funding-rates|candles|candlesticks|exchangeStats)",
    ]),
    "risex": ("api.rise.trade", [
        r"/v1/markets",
        r"/v1/orderbook",
        r"/v1/markets/id/\d+/(trade-history|funding-rate-history)",
    ]),
    "arcus": ("api.arcus.xyz", [
        r"/v1/(markets|feetiers|fundingRates|prices|mids|candles|trades)",
        r"/v1/markets/feeSchedules",
        # docs: get-l2-orderbook-snapshot / get-best-bid-offer-bbo (read 2026-11-12)
        r"/v1/(l2OrderBook|bbo)/[A-Za-z0-9._-]+",
        r"/health",
    ]),
    "variational": ("omni-client-api.prod.ap-northeast-1.variational.io", [
        r"/metadata/v2/stats",
        r"/metadata/stats",
    ]),
}
TXFLOW_HOST = "rpc.txflow.com"
TXFLOW_TYPES = ("perpMeta", "perpMetaDetail", "activeAssetCtx", "marketTicker", "l2Book",
                "trades", "candleSnapshot", "feeTierInfo", "feeTierRates", "tags",
                "marketTags")
# Hyperliquid types that answer for every dex at once (no dex field documented); only
# the xyz rows may be consumed, and each call is written to the audit log.
XYZ_BROAD_TYPES = ("perpDexs", "perpCategories")
XYZ_DEX_TYPES = ("meta", "metaAndAssetCtxs")
XYZ_COIN_TYPES = ("l2Book", "fundingHistory")


class Decision:
    __slots__ = ("allowed", "exchange_id", "reason", "flag")

    def __init__(self, allowed, exchange_id=None, reason=None, flag=None):
        self.allowed, self.exchange_id, self.reason, self.flag = (
            allowed, exchange_id, reason, flag)

    def __repr__(self):
        return "Decision(%r, %r, %r, %r)" % (self.allowed, self.exchange_id,
                                             self.reason, self.flag)


def refuse(reason, xid=None):
    return Decision(False, xid, reason)


def parse_body(body):
    if body is None:
        return None
    if isinstance(body, (bytes, bytearray, memoryview)):
        body = bytes(body).decode("utf-8", "replace")
    try:
        obj = json.loads(body)
    except (TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def has_account_key(obj):
    if isinstance(obj, dict):
        return any(k in obj for k in ACCOUNT_KEYS) or any(
            has_account_key(v) for v in obj.values())
    if isinstance(obj, list):
        return any(has_account_key(v) for v in obj)
    return False


def check_xyz(obj):
    t = obj.get("type")
    if t in XYZ_DEX_TYPES:
        if obj.get("dex") != XYZ:
            return refuse("Hyperliquid %s without dex=\"xyz\" (native or other dex)" % t,
                          "tradexyz")
        return Decision(True, "tradexyz")
    if t in XYZ_COIN_TYPES:
        coin = obj.get("coin")
        if not (isinstance(coin, str) and coin.startswith(XYZ_PREFIX)):
            return refuse("Hyperliquid %s coin %r is not an xyz: market" % (t, coin),
                          "tradexyz")
        return Decision(True, "tradexyz")
    if t == "candleSnapshot":
        coin = (obj.get("req") or {}).get("coin")
        if not (isinstance(coin, str) and coin.startswith(XYZ_PREFIX)):
            return refuse("Hyperliquid candleSnapshot coin %r is not an xyz: market"
                          % coin, "tradexyz")
        return Decision(True, "tradexyz")
    if t in XYZ_BROAD_TYPES:
        if len(obj) != 1:
            return refuse("Hyperliquid %s takes no other field" % t, "tradexyz")
        return Decision(True, "tradexyz", flag=BROAD)
    return refuse("Hyperliquid info type %r is outside the HIP-3 xyz scope" % t,
                  "tradexyz")


def check_route(host, method, path):
    """Host, method and path only (a POST whose body has not been sent yet)."""
    h = (host or "").lower().strip("[]")
    m = (method or "").upper()
    p = (path or "/").split("?", 1)[0]
    if h in (HL_HOST, TXFLOW_HOST):
        if m == "POST" and p == "/info":
            return Decision(True, "tradexyz" if h == HL_HOST else "txflow")
        return refuse("%s accepts only POST /info reads (%s %s)" % (h, m, p))
    return check(h, m, p)


def check(host, method, path, body=None):
    """Decision for one HTTP request. `body` is the raw request body (POST only)."""
    h = (host or "").lower().strip("[]")
    m = (method or "").upper()
    p = (path or "/").split("?", 1)[0]
    if h == HL_HOST or h == TXFLOW_HOST:
        if m != "POST" or p != "/info":
            return refuse("%s accepts only POST /info reads (%s %s)" % (h, m, p))
        obj = parse_body(body)
        if obj is None:
            return refuse("%s POST /info body missing or not a JSON object" % h)
        if has_account_key(obj):
            return refuse("%s query names an account/signature field" % h)
        if h == HL_HOST:
            return check_xyz(obj)
        if obj.get("type") not in TXFLOW_TYPES:
            return refuse("TxFlow info type %r is not a public market read"
                          % obj.get("type"), "txflow")
        return Decision(True, "txflow")
    for xid, (route_host, patterns) in GET_ROUTES.items():
        if h != route_host:
            continue
        if m != "GET":
            return refuse("%s: only GET reads are allowed (%s %s)" % (xid, m, p), xid)
        if any(re.fullmatch(rx, p) for rx in patterns):
            return Decision(True, xid)
        return refuse("%s: path %s is not a documented public read" % (xid, p), xid)
    return refuse("host %s is not one of the eight V2 venues' public APIs" % (h or "?"))


def allowed_hosts():
    return {HL_HOST, TXFLOW_HOST} | {h for h, _ in GET_ROUTES.values()}
