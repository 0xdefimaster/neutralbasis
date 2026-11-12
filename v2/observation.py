"""OBSERVATION_UNIVERSE_V3: eleven venues observed read-only. Observation never trades.

PAPER ONLY. Two universes are kept apart on purpose:

  PAPER_EXECUTION_UNIVERSE_V2  the owner's eight venues (v2.policy.EXCHANGE_IDS); the
                               only venues a PAPER position may ever use. Unchanged.
  OBSERVATION_UNIVERSE_V3      eleven venues whose public market data may be READ to
                               find and explain opportunities (the funnel and the
                               Potential Opportunity Radar). An observation-only venue
                               can never become a PAPER leg (assert_paper_venue).

Network: mode "observation_v3" is its own egress policy (check / check_route /
allowed_hosts below). It starts from the NB_V2 eight-venue policy unchanged and adds
only the observation venues' documented public reads. NB_V2=1 keeps
v2.network_policy exactly as it was; this module is never consulted under NB_V2.

Observation-only additions (sources in docs/v2/25_ELEVEN_VENUE_DATA_MATRIX.md):
  hyperliquid_native  POST api.hyperliquid.xyz/info, the first perp dex (dex "" or no
                      dex field; coins without a "dex:" prefix): meta, metaAndAssetCtxs,
                      l2Book, fundingHistory, candleSnapshot.
  entropy_io          the HIP-3 dex "io" (perpDexs fullName "EntropyIO", observed
                      2026-11-12 00:54 UTC): the same types with dex "io" / "io:" coins.
  phoenix_perps       GET perp-api.phoenix.trade/v1/exchange/snapshot only: the one raw
                      path the official SDK README states (Ellipsis-Labs/rise-public
                      ts/README.md, read 2026-11-12). Every other route is UNKNOWN and
                      refused until its path is read in the SDK source.
  qfex                no HTTP route: the CLI source (QFEX-org/cli internal/daemon)
                      shows only an unauthenticated market-data WebSocket
                      (wss://mds.qfex.com/); REST paths are UNKNOWN. Refused (BLOCKED).
Still refused in every mode: account/user/signature fields, spot, other HIP-3 dexes,
predictedFundings (its answer carries CEX venues' predicted rates: CEX data stays zero),
every write path, every CEX host.
"""
import os
import re

from v2 import network_policy as v2np
from v2 import policy

MODE = "observation_v3"
ENV = "NB_OBSERVATION_V3"

PAPER_EXECUTION_UNIVERSE_V2 = tuple(policy.EXCHANGE_IDS)
OBSERVATION_UNIVERSE_V3 = (
    "lighter_rh",
    "risex",
    "arcus",
    "txflow",
    "variational",
    "tradexyz",
    "popdex_perps",
    "entropy_io",
    "hyperliquid_native",
    "phoenix_perps",
    "qfex",
)
OBSERVATION_ONLY = tuple(x for x in OBSERVATION_UNIVERSE_V3
                         if x not in PAPER_EXECUTION_UNIVERSE_V2)
# The legacy engine name an observation venue's rows carry (never a PAPER venue name).
DISPLAY = dict(hyperliquid_native="Hyperliquid", entropy_io="EntropyIO",
               phoenix_perps="Phoenix", qfex="QFEX")

HL_HOST = v2np.HL_HOST
IO_DEX = "io"
IO_PREFIX = "io:"
PHOENIX_HOST = "perp-api.phoenix.trade"
PHOENIX_PATHS = (r"/v1/exchange/snapshot",)
QFEX_HOSTS = ("api.qfex.com", "mds.qfex.com")   # known, refused (no verified route)
HL_DEX_TYPES = ("meta", "metaAndAssetCtxs")
HL_COIN_TYPES = ("l2Book", "fundingHistory")
OBSERVATION = "OBSERVATION_ONLY"


class ObservationError(RuntimeError):
    pass


def enabled(environ=None):
    env = os.environ if environ is None else environ
    raw = (env.get(ENV) or "").strip()
    if raw in ("", "0"):
        return False
    if raw == "1":
        return True
    raise ObservationError("%s must be 0 or 1, not %r" % (ENV, raw))


def assert_paper_venue(exchange_id):
    """Raise unless `exchange_id` is one of the eight PAPER venues. Observation-only
    venues (and anything unknown) can never open, hold or settle a PAPER leg."""
    if exchange_id not in PAPER_EXECUTION_UNIVERSE_V2:
        raise ObservationError("%r is not a PAPER execution venue (observation only or "
                               "unknown)" % (exchange_id,))
    return exchange_id


def is_paper_venue(exchange_id):
    return exchange_id in PAPER_EXECUTION_UNIVERSE_V2


def coin_venue(coin):
    """hyperliquid_native / entropy_io / tradexyz for a Hyperliquid coin name, or None
    for spot, another HIP-3 dex or a malformed name."""
    if not isinstance(coin, str) or not coin or "@" in coin or "/" in coin:
        return None
    if coin.startswith(v2np.XYZ_PREFIX):
        return "tradexyz"
    if coin.startswith(IO_PREFIX):
        return "entropy_io"
    if ":" in coin:
        return None
    return "hyperliquid_native"


def check_hl(obj):
    t = obj.get("type")
    if t in HL_DEX_TYPES:
        dex = obj.get("dex", "")
        if dex == v2np.XYZ:
            return v2np.Decision(True, "tradexyz")
        if dex == IO_DEX:
            return v2np.Decision(True, "entropy_io", flag=OBSERVATION)
        if dex == "":
            return v2np.Decision(True, "hyperliquid_native", flag=OBSERVATION)
        return v2np.refuse("Hyperliquid %s for dex %r is outside observation_v3" % (t, dex))
    if t in HL_COIN_TYPES or t == "candleSnapshot":
        coin = (obj.get("req") or {}).get("coin") if t == "candleSnapshot" else obj.get("coin")
        xid = coin_venue(coin)
        if xid is None:
            return v2np.refuse("Hyperliquid %s coin %r is not native, io: or xyz:" % (t, coin))
        return v2np.Decision(True, xid, flag=None if xid == "tradexyz" else OBSERVATION)
    if t in v2np.XYZ_BROAD_TYPES:
        return v2np.check_xyz(obj)
    return v2np.refuse("Hyperliquid info type %r is outside observation_v3" % t)


def check(host, method, path, body=None):
    """Decision for one request in observation_v3 mode."""
    h = (host or "").lower().strip("[]")
    m = (method or "").upper()
    p = (path or "/").split("?", 1)[0]
    if h == HL_HOST:
        if m != "POST" or p != "/info":
            return v2np.refuse("%s accepts only POST /info reads (%s %s)" % (h, m, p))
        obj = v2np.parse_body(body)
        if obj is None:
            return v2np.refuse("%s POST /info body missing or not a JSON object" % h)
        if v2np.has_account_key(obj):
            return v2np.refuse("%s query names an account/signature field" % h)
        return check_hl(obj)
    if h == PHOENIX_HOST:
        if m != "GET":
            return v2np.refuse("phoenix_perps: only GET reads (%s %s)" % (m, p), "phoenix_perps")
        if any(re.fullmatch(rx, p) for rx in PHOENIX_PATHS):
            return v2np.Decision(True, "phoenix_perps", flag=OBSERVATION)
        return v2np.refuse("phoenix_perps: path %s is not a documented public read "
                           "(UNKNOWN routes are refused)" % p, "phoenix_perps")
    if h in QFEX_HOSTS:
        return v2np.refuse("qfex: no verified public HTTP route (BLOCKED)", "qfex")
    if h == v2np.TXFLOW_HOST and m == "POST" and p == "/info":
        obj = v2np.parse_body(body)
        if obj is not None and obj.get("type") == USER_FUNDINGS:
            return check_user_fundings(obj)
    return v2np.check(h, m, p, body)


USER_FUNDINGS = "userFundings"
FUNDING_ADDRESS_ENV = "NB_TXFLOW_FUNDING_ADDRESS"
ACCOUNT_READ = "ACCOUNT_READ_AUTHORIZED"


def authorized_address(environ=None):
    """The one TxFlow address the owner authorized for read-only funding evidence
    (NB_TXFLOW_FUNDING_ADDRESS), lower case, or None. Never another account."""
    env = os.environ if environ is None else environ
    a = (env.get(FUNDING_ADDRESS_ENV) or "").strip().lower()
    return a if re.fullmatch(r"0x[0-9a-f]{40}", a) else None


def check_user_fundings(obj, environ=None):
    """TxFlow userFundings (docs.txflow.com/api/info/userFundings.md: 'No auth - the
    body user is the subject'): allowed in observation_v3 only for the authorized
    address, with no other account field. It reads payments, it cannot trade."""
    addr = authorized_address(environ)
    if addr is None:
        return v2np.refuse("TxFlow userFundings needs the owner-authorized read-only "
                           "address (%s unset); other accounts are never read"
                           % FUNDING_ADDRESS_ENV, "txflow")
    if str(obj.get("user") or "").lower() != addr:
        return v2np.refuse("TxFlow userFundings for an address that is not the "
                           "authorized one", "txflow")
    rest = {k: v for k, v in obj.items() if k != "user"}
    if v2np.has_account_key(rest):
        return v2np.refuse("TxFlow userFundings with another account field", "txflow")
    return v2np.Decision(True, "txflow", flag=ACCOUNT_READ)


def check_route(host, method, path):
    h = (host or "").lower().strip("[]")
    if h == HL_HOST:
        m, p = (method or "").upper(), (path or "/").split("?", 1)[0]
        if m == "POST" and p == "/info":
            return v2np.Decision(True, None)
        return v2np.refuse("%s accepts only POST /info reads (%s %s)" % (h, m, p))
    if h in (PHOENIX_HOST,) + QFEX_HOSTS:
        return check(h, method, path)
    return v2np.check_route(host, method, path)


def allowed_hosts():
    return v2np.allowed_hosts() | {PHOENIX_HOST}


# Guard interface (pilot.network_guard uses BROAD from the policy module it is given).
BROAD = v2np.BROAD


# The eleven-venue data matrix (plus veranta, PAPER-universe only), checked 2026-11-12.
# OK = read live and parsed; ROUTE_ALLOWED = documented and allowed, not read yet;
# UNKNOWN_ROUTE = the data exists per the docs/SDK but its raw path is not verified
# (refused); NO_VERIFIED_API = nothing public found; BLOCKED_* = a rule stops it.
# Evidence values are v2.funding_evidence statuses.
# Source: docs/v2/25_ELEVEN_VENUE_DATA_MATRIX.md
VENUE_MATRIX = {
    "lighter_rh": dict(role="PAPER+OBSERVE", metadata="OK", book="OK", funding_now="OK",
                       funding_history="MARKET_SETTLEMENT_CONFIRMED", trades="ROUTE_ALLOWED",
                       fees="KNOWN", status="ACTIVE"),
    "risex": dict(role="PAPER+OBSERVE", metadata="OK", book="OK", funding_now="OK",
                  funding_history="MARKET_SETTLEMENT_CONFIRMED", trades="ROUTE_ALLOWED",
                  fees="UNKNOWN (per account)", status="BLOCKED_FEE_UNKNOWN"),
    "arcus": dict(role="PAPER+OBSERVE", metadata="OK", book="OK", funding_now="OK",
                  funding_history="MARKET_SETTLEMENT_CONFIRMED", trades="ROUTE_ALLOWED",
                  fees="KNOWN", status="ACTIVE (equities: US session)"),
    "txflow": dict(role="PAPER+OBSERVE", metadata="OK", book="OK (+WS, off by default)",
                   funding_now="OK", funding_history="RATE_PUBLISHED; no public market "
                   "history; userFundings only for NB_TXFLOW_FUNDING_ADDRESS (unset)", trades="ROUTE_ALLOWED",
                   fees="KNOWN", status="ACTIVE (funding unproven)"),
    "variational": dict(role="PAPER+OBSERVE", metadata="OK", book="RFQ_INDICATIVE",
                        funding_now="OK", funding_history="RATE_PUBLISHED (observer: "
                        "RATE_SAMPLED_AT_CUTOFF)", trades="NO_VERIFIED_API",
                        fees="0 (OLP spread)", status="BLOCKED_EXECUTION_MODEL"),
    "tradexyz": dict(role="PAPER+OBSERVE", metadata="OK", book="OK", funding_now="OK",
                     funding_history="MARKET_SETTLEMENT_CONFIRMED", trades="NOT_ALLOWED",
                     fees="DERIVED (HIP-3 scale)", status="ACTIVE"),
    "popdex_perps": dict(role="PAPER+OBSERVE", metadata="NO_VERIFIED_API",
                         book="NO_VERIFIED_API", funding_now="NO_VERIFIED_API",
                         funding_history="MISSING", trades="NO_VERIFIED_API",
                         fees="UNKNOWN", status="BLOCKED_NO_API"),
    "veranta": dict(role="PAPER only (not in the observation list)",
                    metadata="NO_VERIFIED_API", book="NO_VERIFIED_API",
                    funding_now="NO_VERIFIED_API", funding_history="MISSING",
                    trades="NO_VERIFIED_API", fees="UNKNOWN", status="BLOCKED_NO_API"),
    "entropy_io": dict(role="OBSERVE ONLY", metadata="OK (22 markets)",
                       book="ROUTE_ALLOWED", funding_now="OK",
                       funding_history="MARKET_SETTLEMENT_CONFIRMED (route allowed)",
                       trades="NOT_ALLOWED", fees="DERIVED (HIP-3 scale)",
                       status="OBSERVATION_ACTIVE"),
    "hyperliquid_native": dict(role="OBSERVE ONLY", metadata="OK (235 markets)", book="OK",
                               funding_now="OK",
                               funding_history="MARKET_SETTLEMENT_CONFIRMED (INIT 90d read)",
                               trades="NOT_ALLOWED", fees="KNOWN (tier 0)",
                               status="OBSERVATION_ACTIVE"),
    "phoenix_perps": dict(role="OBSERVE ONLY", metadata="OK (exchange snapshot)",
                          book="UNKNOWN_ROUTE", funding_now="UNKNOWN_ROUTE",
                          funding_history="MISSING (SDK route, path UNKNOWN)",
                          trades="UNKNOWN_ROUTE", fees="PUBLISHED (snapshot takerFee)",
                          status="OBSERVATION_METADATA_ONLY"),
    "qfex": dict(role="OBSERVE ONLY", metadata="UNKNOWN_ROUTE", book="UNKNOWN_ROUTE (WS)",
                 funding_now="UNKNOWN_ROUTE (WS)", funding_history="MISSING",
                 trades="UNKNOWN_ROUTE (WS)", fees="UNKNOWN",
                 status="BLOCKED_NO_VERIFIED_ROUTE (never qfex login / agents init)"),
}
