"""Instrument identity and economic equivalence. A ticker match is never enough.

Two venue markets are the same economic instrument only with POSITIVE evidence:
  * the same verified underlying_id (from a curated registry entry, or the curated
    crypto list when both venues' own metadata class the market as crypto),
  * the same contract_type (a perpetual is not a swap or a dated future),
  * a known contract multiplier on both sides (1000PEPE and kPEPE are 1000 PEPE),
  * no entry on the conflict list.
Everything else is IDENTITY_UNVERIFIED and never becomes a candidate. Prices that
agree are a consistency check only (price_consistent), never proof.

Known conflicts (pilot 2026-11-12): Variational QNT 178.2 vs Extended QNT 40.33;
Variational ANTHROPIC (pre-IPO perp, docs: estimated 1B shares) vs Lighter ANTHROPIC
1824 vs 1380. RWA/equity/index/commodity markets stay unverified until a registry entry
cites both venues' own definition of the underlying (same index, same share class,
same session rules).
"""
import re
from dataclasses import dataclass, field

VERIFIED = "IDENTITY_VERIFIED"
UNVERIFIED = "IDENTITY_UNVERIFIED"

CRYPTO = "crypto"
ASSET_CLASSES = (CRYPTO, "equity", "etf", "index", "commodity", "fx", "preipo", "unknown")
PERP = "perp"

# Curated crypto bases whose ticker is unambiguous among the eight venues when the
# venue itself says the market is crypto. Extend only with evidence.
CURATED_CRYPTO = frozenset((
    "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "DOT", "LTC", "BCH",
    "TRX", "SUI", "APT", "ARB", "OP", "NEAR", "ATOM", "UNI", "AAVE", "TON", "HYPE",
    "PEPE", "WIF", "BONK", "SHIB", "ENA", "TIA", "SEI", "INJ", "WLD", "ONDO", "PUMP",
    "FIL", "ETC", "XLM", "HBAR", "CRV", "LDO", "JUP", "TAO", "FET", "RENDER", "POL",
))
# Tickers known to name different instruments on different venues.
CONFLICTS = {
    "QNT": "Variational QNT 178.2 vs Extended QNT 40.33 on 2026-11-12 (pilot marks)",
    "ANTHROPIC": "pre-IPO perp; venues price different share-count conventions "
                 "(Variational 1380 vs Lighter 1824 on 2026-11-12)",
    "OPENAI": "pre-IPO perp; share-count convention per venue",
}

# Tickers that start with a digit but carry no multiplier.
DIGIT_TICKERS = frozenset(("1INCH", "2Z"))
MULTIPLIER_PREFIX =(("1000000", 1_000_000), ("1M", 1_000_000), ("1000", 1000), ("k", 1000))
QUOTE_SUFFIX = re.compile(r"(-PERP|-USD|-USDC|-USDT|USDT|USDC|/USD|/USDC|-SWAP)$", re.I)


@dataclass
class Instrument:
    exchange_id: str
    market_id: str
    symbol: str
    asset_class: str = "unknown"
    contract_type: str = PERP
    quote_asset: str = None
    settlement_asset: str = None
    session: str = None  # "24/7", "RTH", None = unknown
    base: str = None
    multiplier: float = None
    underlying_id: str = None
    status: str = UNVERIFIED
    evidence: list = field(default_factory=list)


def parse_symbol(symbol):
    """(base, multiplier) from a venue symbol. xyz:NVDA -> NVDA; 1000PEPE / kPEPE ->
    PEPE x1000; BTC-PERP / BTCUSDT -> BTC x1. The multiplier is None when the symbol
    carries a numeric prefix this parser does not know."""
    s = str(symbol).strip()
    if ":" in s:
        s = s.split(":", 1)[1]
    if s.upper().startswith("XYZ-"):
        s = s[4:]
    s = QUOTE_SUFFIX.sub("", s)
    for prefix, mult in MULTIPLIER_PREFIX:
        rest = s[len(prefix):]
        if s.startswith(prefix) and rest and rest[:1].isalpha() and (
                prefix != "k" or rest.isupper()):
            return rest.upper(), mult
    if s.upper() in DIGIT_TICKERS:
        return s.upper(), 1
    if re.match(r"^\d", s):
        return s.upper(), None
    return s.upper(), 1


def resolve(inst, registry=None):
    """Fill base, multiplier, underlying_id and status on `inst` (and return it).
    registry: {(exchange_id, market_id): underlying_id} curated entries with evidence."""
    base, mult = parse_symbol(inst.symbol)
    inst.base = inst.base or base
    if inst.multiplier is None:
        inst.multiplier = mult
    inst.evidence = []
    key = (inst.exchange_id, str(inst.market_id))
    if inst.base in CONFLICTS:
        inst.status, inst.underlying_id = UNVERIFIED, None
        inst.evidence.append("CONFLICT: " + CONFLICTS[inst.base])
        return inst
    if inst.contract_type != PERP:
        inst.status = UNVERIFIED
        inst.evidence.append("contract_type %s is not a perpetual" % inst.contract_type)
        return inst
    if inst.multiplier is None:
        inst.status = UNVERIFIED
        inst.evidence.append("contract multiplier unknown")
        return inst
    if registry and key in registry:
        inst.underlying_id = registry[key]
        inst.status = VERIFIED
        inst.evidence.append("REGISTRY")
        return inst
    if inst.asset_class == CRYPTO and inst.base in CURATED_CRYPTO:
        inst.underlying_id = "crypto:" + inst.base
        inst.status = VERIFIED
        inst.evidence.append("CURATED_CRYPTO + venue class crypto")
        return inst
    inst.status = UNVERIFIED
    inst.evidence.append(
        "no positive identity evidence (asset_class %s, base %s)" % (inst.asset_class,
                                                                     inst.base))
    return inst


def equivalent(a, b):
    """(True, None) when a and b are the same economic instrument, else (False, why)."""
    for x in (a, b):
        if x.status != VERIFIED:
            return False, "%s %s: %s" % (x.exchange_id, x.symbol, "; ".join(x.evidence))
    if a.exchange_id == b.exchange_id:
        return False, "same venue"
    if a.underlying_id != b.underlying_id:
        return False, "different underlying (%s vs %s)" % (a.underlying_id, b.underlying_id)
    if a.contract_type != b.contract_type:
        return False, "different contract type"
    return True, None


def price_consistent(price_a, mult_a, price_b, mult_b, tolerance=0.02):
    """Per-unit-of-underlying prices within `tolerance`: a consistency check, never proof."""
    if not (price_a and price_b and mult_a and mult_b):
        return False
    ua, ub = price_a / mult_a, price_b / mult_b
    return abs(ua / ub - 1) <= tolerance
