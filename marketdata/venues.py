"""Venue identities of the data platform.

Every venue has one explicit id. The project's older names stay readable as aliases
only: in the paper engine and the recorder "Lighter" is the Robinhood deployment
(api.rh.lighter.xyz) and "LighterCore" the main one, which made "Lighter" mean two
different things. Stored records are never renamed.
"""

VENUES = dict(
    hyperliquid=dict(label="Hyperliquid", host="https://api.hyperliquid.xyz"),
    paradex=dict(label="Paradex", host=None),
    txflow=dict(label="TxFlow", host="https://rpc.txflow.com"),
    arcus=dict(label="Arcus", host="https://api.arcus.xyz"),
    extended=dict(
        label="Extended", host="https://api.starknet.extended.exchange/api/v1/"
    ),
    variational=dict(
        label="Variational Omni",
        host="https://omni-client-api.prod.ap-northeast-1.variational.io",
    ),
    risex=dict(label="RISEx", host="https://api.rise.trade"),
    lighter_core=dict(
        label="Lighter (Core)", host="https://mainnet.zklighter.elliot.ai/api/v1/"
    ),
    lighter_rh=dict(
        label="Lighter RH (Robinhood)", host="https://api.rh.lighter.xyz/api/v1/"
    ),
)

# Names used by the paper engine, the CCXT scanner and the recorder.
ALIASES = dict(
    Hyperliquid="hyperliquid",
    Paradex="paradex",
    TxFlow="txflow",
    Arcus="arcus",
    Extended="extended",
    Variational="variational",
    RISEx="risex",
    LighterCore="lighter_core",
    Lighter="lighter_rh",
)


def venue_id(name):
    """The platform id of a venue id or legacy name; an unknown name is an error."""
    if name in VENUES:
        return name
    if name in ALIASES:
        return ALIASES[name]
    raise KeyError("Bilinmeyen borsa: %s" % name)


def legacy_names(vid):
    return sorted(k for k, v in ALIASES.items() if v == vid)
