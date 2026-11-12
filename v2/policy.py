"""The closed V2 venue universe: exactly eight execution venues, one source of truth.

PAPER ONLY. V2 is off unless NB_V2=1. When it is on, the process is fail-closed:
only the eight exchange_ids below exist, legacy and CEX venues cannot be re-enabled by
an environment flag or an old config, and any unknown value raises at start.

The eight ids are the owner's list of 2026-11-12. Two have no adapter yet
(popdex_perps, veranta); their status is in docs/v2/03_VENUE_CAPABILITY_MATRIX.md.
"""
import os

EXCHANGE_IDS = (
    "lighter_rh",
    "risex",
    "arcus",
    "txflow",
    "variational",
    "popdex_perps",
    "veranta",
    "tradexyz",
)

# The name each V2 venue carries in the legacy code and its stored history (never
# renamed in a database). Lighter RH is the legacy "Lighter" feed, NOT "LighterCore".
LEGACY_NAME = {
    "lighter_rh": "Lighter",
    "risex": "RISEx",
    "arcus": "Arcus",
    "txflow": "TxFlow",
    "variational": "Variational",
    "tradexyz": "tradeXYZ",
    "popdex_perps": None,
    "veranta": None,
}
FROM_LEGACY = {v: k for k, v in LEGACY_NAME.items() if v}

# Kept in code and history for frozen replay only; never collected, discovered,
# matched, assessed or traded in V2.
LEGACY_OUT_OF_SCOPE = (
    "Hyperliquid",  # native perps; tradeXYZ uses only the HIP-3 xyz dex on its API
    "Extended",
    "LighterCore",
    "Paradex",
    "Aster",
    "Pacifica",
    "Nado",
    "Hibachi",
    "ApeX",
    "dYdX",
    "GRVT",
    "Derive",
)
CEX = (
    "Binance",
    "Bybit",
    "OKX",
    "Bitget",
    "KuCoin",
    "KrakenFutures",
    "Kraken",
    "Gate",
    "HTX",
    "MEXC",
    "BingX",
    "Deribit",
    "Phemex",
    "BloFin",
    "Coinbase",
    "Backpack",
)

# Environment switches of the legacy pilot that could bring an out-of-scope venue
# back. Under V2 they must be unset, or (NB_EXTRA_PERP_DEXES) name tradeXYZ only.
LEGACY_SWITCHES = ("NB_EXTRA_PERP_DEXES", "NB_LIGHTER_RH_ALL", "NB_RFQ_STREAMS")


class PolicyError(ValueError):
    pass


def v2_enabled(environ=None):
    """True only for NB_V2=1; unset or 0 is off; anything else is an error."""
    env = os.environ if environ is None else environ
    raw = (env.get("NB_V2") or "").strip()
    if raw in ("", "0"):
        return False
    if raw == "1":
        return True
    raise PolicyError("NB_V2 must be 0 or 1, not %r" % raw)


def exchange_id(name):
    """The V2 exchange_id of a legacy venue name or of an id; None if out of scope."""
    if name in EXCHANGE_IDS:
        return name
    return FROM_LEGACY.get(name)


def require_in_scope(names):
    """Raise unless every venue named is one of the eight (legacy names accepted)."""
    bad = sorted({n for n in names if exchange_id(n) is None})
    if bad:
        raise PolicyError("out of V2 scope (legacy/CEX): " + ", ".join(bad))
    return [exchange_id(n) for n in names]


def check_environment(environ=None):
    """Under V2, refuse legacy switches that would widen the venue set."""
    env = os.environ if environ is None else environ
    if not v2_enabled(env):
        return []
    problems = []
    extra = [x.strip() for x in (env.get("NB_EXTRA_PERP_DEXES") or "").split(",") if x.strip()]
    if [x for x in extra if x != "tradeXYZ"]:
        problems.append("NB_EXTRA_PERP_DEXES names out-of-scope venues: %s" % extra)
    for k in ("NB_RFQ_STREAMS",):
        if env.get(k):
            problems.append("%s is an Extended RFQ switch; Extended is out of V2 scope" % k)
    if problems:
        raise PolicyError("; ".join(problems))
    return []


def restrict_catalog(catalog):
    """Switch off every CCXT/native scan in a wide_markets-style CATALOG whose venue is
    not one of the eight (in place). Returns the venues switched off, sorted."""
    keep = {n for n in LEGACY_NAME.values() if n}
    off = []
    for name, spec in catalog.items():
        if name not in keep and (spec.get("adapter") or spec.get("scan")):
            spec["adapter"], spec["scan"] = None, False
            off.append(name)
    return sorted(off)


def filter_config_venues(venues):
    """A stored workspace/config venue map narrowed to V2: out-of-scope venues are
    dropped and reported, never silently kept."""
    kept, dropped = {}, []
    for name, value in (venues or {}).items():
        x = exchange_id(name)
        if x is None:
            dropped.append(name)
        else:
            kept[name] = value
    return kept, sorted(dropped)
