"""Fee, funding-unit and timestamp-unit records for the eight venues.

An UNKNOWN cost is None with status UNKNOWN, never a numeric 0. A verified zero
(Variational's documented 0% trading fee, Lighter RH Standard 0/0) is 0.0 with its
source, and the quote spread / slippage it does not cover stays a separate cost.

Every FeeRecord carries source_url, retrieved_at, raw_value, raw_unit and
normalized_fraction. Values below were read from official pages on 2026-11-12
(docs/v2/02_VENUE_FEE_MATRIX.md); they are documentation values, not live account
tiers, and must be re-read at runtime where the venue serves them (Arcus /v1/feetiers,
TxFlow feeTierInfo/feeTierRates, Hyperliquid perpDexs for the xyz deployer scale).
"""
from dataclasses import dataclass

VERIFIED_DOC = "VERIFIED_DOC"
VERIFIED_LIVE = "VERIFIED_LIVE"
UNKNOWN = "UNKNOWN"
THIRD_PARTY = "THIRD_PARTY_UNVERIFIED"
RETRIEVED = "2026-11-12"

UNIT_SCALE = {
    "fraction": 1.0,
    "percent": 0.01,
    "bp": 0.0001,
    "ppm": 0.000001,
}


class FeeUnknown(ValueError):
    pass


def to_fraction(raw, unit):
    """A raw rate in a stated unit as a fraction. The unit must be one we know."""
    if raw is None:
        return None
    if unit not in UNIT_SCALE:
        raise ValueError("unknown rate unit %r" % unit)
    return float(raw) * UNIT_SCALE[unit]


TS_SCALE = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9}


def to_seconds(value, unit):
    """Epoch seconds from a timestamp in an explicitly documented unit (no guessing)."""
    if value is None:
        return None
    if unit not in TS_SCALE:
        raise ValueError("unknown timestamp unit %r" % unit)
    return float(value) * TS_SCALE[unit]


@dataclass(frozen=True)
class FeeRecord:
    exchange_id: str
    tier: str
    maker_raw: object
    taker_raw: object
    raw_unit: str
    status: str
    source_url: str
    retrieved_at: str = RETRIEVED
    market_scope: str = "all"
    note: str = None

    @property
    def maker(self):
        return to_fraction(self.maker_raw, self.raw_unit) if self.status != UNKNOWN else None

    @property
    def taker(self):
        return to_fraction(self.taker_raw, self.raw_unit) if self.status != UNKNOWN else None


ARCUS_FEES = "https://docs.arcus.xyz/concepts/perpetuals/fees.md"
TXFLOW_FEES = "https://docs.txflow.com/trade/fees"
LIGHTER_RH = "https://docs.lighter.xyz/developers/robinhood-chain/overview.md"
VARIATIONAL_FEES = "https://docs.variational.io/omni/trading/fees"
HL_FEES = "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees"
RISEX_FEES = "https://developer.rise.trade/reference/feetierservice_getuserfees.md"
VERANTA_MARKETS = "https://sdk.veranta.xyz/data/markets.md"
POPDEX_DOCS = "https://popdex.xyz/docs"

FEES = (
    FeeRecord("lighter_rh", "Standard", 0, 0, "percent", VERIFIED_DOC, LIGHTER_RH,
              note="RH Standard 0%/0%; RH Standard read limit 60 requests/minute"),
    FeeRecord("lighter_rh", "Plus", 0.015, 0.015, "percent", VERIFIED_DOC, LIGHTER_RH),
    FeeRecord("lighter_rh", "Premium <$1M", 0.0120, 0.0350, "percent", VERIFIED_DOC,
              LIGHTER_RH),
    FeeRecord("risex", "unknown", None, None, "fraction", UNKNOWN, RISEX_FEES,
              note="schedule served only per account with JWT (GET /v1/user/fees); the "
                   "legacy pilot used 0.0003, an unverified owner setting"),
    FeeRecord("arcus", "Base (crypto)", 150, 450, "ppm", VERIFIED_DOC, ARCUS_FEES,
              note="unit ppm per docs; GET /v1/feetiers 2026-11-12 Base 150/450"),
    FeeRecord("arcus", "Base (RWA custom schedule)", 0, 225, "ppm", VERIFIED_LIVE,
              "https://api.arcus.xyz/v1/markets", market_scope="RWA markets with "
              "hasCustomFeeSchedule=true (AMD, SPY, GLD, ... on 2026-11-12)"),
    FeeRecord("txflow", "VIP 0", 0.0150, 0.0450, "percent", VERIFIED_DOC, TXFLOW_FEES,
              note="tier from 14-day weighted volume; live via feeTierInfo/feeTierRates"),
    FeeRecord("variational", "all", 0, 0, "percent", VERIFIED_DOC, VARIATIONAL_FEES,
              note="no trading fee; the OLP quote spread is the cost (quote_cost), "
                   "liquidation at quote +/-0.5%, $0.1 per deposit/withdrawal"),
    FeeRecord("tradexyz", "tier 0, deployer scale unknown", None, None, "percent",
              UNKNOWN, HL_FEES,
              note="HIP-3 all-in fee = base tier (taker 0.045%, maker 0.015%) x "
                   "scaleIfHip3 from the deployer fee scale (perpDexs); growth mode "
                   ">=90% lower. Effective xyz rate must be read live per asset"),
    FeeRecord("popdex_perps", "unknown", None, None, "percent", UNKNOWN, POPDEX_DOCS,
              note="official docs page empty 2026-11-12; arb.sh shows 0.010%/0.032% "
                   "(third party, not used)"),
    FeeRecord("veranta", "unknown", None, None, "percent", UNKNOWN, VERANTA_MARKETS,
              note="open_fee_p / close_fee_p / spread_p fields documented, values not "
                   "read; skew-adjusted open fee, rollover and profit share apply"),
)


def hip3_scale(deployer_scale, growth_mode=False):
    """Hyperliquid fee multiplier for a HIP-3 market from its deployer fee scale
    (fees docs, 'Fee formula for developers'): scale < 1 -> 1 + scale; scale >= 1 ->
    2 x scale; growth mode multiplies by 0.1."""
    if deployer_scale is None:
        return None
    s = float(deployer_scale)
    if s < 0:
        raise ValueError("negative deployer scale")
    m = s + 1 if s < 1 else 2 * s
    return m * (0.1 if growth_mode else 1.0)


def record(exchange_id, tier=None):
    rows = [f for f in FEES if f.exchange_id == exchange_id and (tier is None or
                                                                  f.tier == tier)]
    if not rows:
        raise KeyError(exchange_id)
    return rows[0]


def taker(exchange_id, tier=None):
    """The taker fraction or FeeUnknown: an unknown fee never prices as zero."""
    r = record(exchange_id, tier)
    if r.taker is None:
        raise FeeUnknown("%s taker fee UNKNOWN (%s)" % (exchange_id, r.note or r.source_url))
    return r.taker


def four_leg_fees(notional, taker_a, taker_b):
    """Open and close of both legs at taker rates (USD). Spread and slippage are
    separate costs; a maker fill is never assumed."""
    for t in (taker_a, taker_b):
        if t is None:
            raise FeeUnknown("a leg's taker fee is UNKNOWN")
    return dict(open_a=notional * taker_a, open_b=notional * taker_b,
                close_a=notional * taker_a, close_b=notional * taker_b,
                total=2 * notional * (taker_a + taker_b))
