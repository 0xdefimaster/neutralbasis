"""The four Phase 1 venues. Lighter RH ("Lighter") and Lighter Core ("LighterCore")
are separate venues: separate hosts, markets and market ids."""

from ..http import Http
from .extended import Extended
from .hyperliquid import Hyperliquid
from .lighter import CORE, RH, Lighter

VENUES = ("Hyperliquid", "LighterCore", "Lighter", "Extended")
LABELS = dict(
    Hyperliquid="Hyperliquid",
    LighterCore="Lighter Core",
    Lighter="Lighter RH",
    Extended="Extended",
)
HOSTS = dict(
    Hyperliquid="https://api.hyperliquid.xyz/info",
    LighterCore=CORE,
    Lighter=RH,
    Extended="https://api.starknet.extended.exchange/api/v1/",
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
    raise ValueError("Bilinmeyen borsa: " + venue)
