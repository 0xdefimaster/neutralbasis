"""The four Phase 1 venues, plus discovery-only venues. Lighter RH ("Lighter") and
Lighter Core ("LighterCore") are separate venues: separate hosts, markets and market
ids. A discovery venue (Variational) only stores its raw public responses and their
schema: no field of it is parsed into market evidence."""

from ..http import Http
from .extended import Extended
from .hyperliquid import Hyperliquid
from .lighter import CORE, RH, Lighter
from .variational import URL as VARIATIONAL_URL
from .variational import Variational

VENUES = ("Hyperliquid", "LighterCore", "Lighter", "Extended")
DISCOVERY = ("Variational",)
ALL_VENUES = VENUES + DISCOVERY
LABELS = dict(
    Hyperliquid="Hyperliquid",
    LighterCore="Lighter Core",
    Lighter="Lighter RH",
    Extended="Extended",
    Variational="Variational (keşif)",
)
HOSTS = dict(
    Hyperliquid="https://api.hyperliquid.xyz/info",
    LighterCore=CORE,
    Lighter=RH,
    Extended="https://api.starknet.extended.exchange/api/v1/",
    Variational=VARIATIONAL_URL,
)


def make_client(venue, store, cfg, transport=None, sleep=None, clock=None):
    kw = dict(transport=transport)
    if sleep is not None:
        kw["sleep"] = sleep
    if clock is not None:
        kw["clock"] = clock
    http = Http(venue, store, cfg, **kw)
    if venue == "Hyperliquid":
        return Hyperliquid(http, cfg)
    if venue == "LighterCore":
        return Lighter(http, cfg, "LighterCore", CORE)
    if venue == "Lighter":
        return Lighter(http, cfg, "Lighter", RH)
    if venue == "Extended":
        return Extended(http, cfg)
    if venue == "Variational":
        return Variational(http, cfg)
    raise ValueError("Bilinmeyen borsa: " + venue)
