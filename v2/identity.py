"""Instrument identity and economic equivalence. A ticker match is never enough.

Two venue markets are the same economic instrument only with POSITIVE evidence. A
group of markets sharing a base ticker is verified when:
  1. at least one member's asset class comes from its own venue's metadata, with a
     documented meaning (the "anchor"): RISEx `category`, Arcus `category`, TxFlow
     `marketTags` (TradFi/Crypto tabs), tradeXYZ `perpCategories`, Lighter RH
     `funding_premium_multiplier` (docs: 1 crypto, 1/2 RWA, 1/100 pre-IPO);
  2. every member's price per unit of underlying agrees with the anchors' median
     within a tight tolerance (crypto 1 %, RWA 2 %);
  3. members that publish a full name (Arcus fullAssetName, Variational name) do not
     contradict each other;
  4. no member is a pre-IPO market or on the conflict list, every member is a
     perpetual with a known contract multiplier.
A member without a declared class (Variational) may join a verified group only through
2 and 3; it can never be the anchor. A curated registry entry overrides all of this for
markets whose identity was checked by hand (RWA share classes, indices).

Known conflicts (2026-11-12): Variational QNT 178.2 vs Extended QNT 40.33; ANTHROPIC and
OPENAI pre-IPO perps with venue-specific share-count conventions; Variational SPX-PERP
is "SPX6900" (a token), not the S&P 500.
"""
import re
import statistics
from dataclasses import dataclass, field

VERIFIED = "IDENTITY_VERIFIED"
UNVERIFIED = "IDENTITY_UNVERIFIED"

CRYPTO, RWA, PREIPO = "crypto", "rwa", "preipo"
FINE_TO_COARSE = {
    "crypto": CRYPTO, "equity": RWA, "equities": RWA, "stock": RWA, "stocks": RWA,
    "etf": RWA, "index_etf": RWA, "commodity": RWA, "commodities": RWA, "index": RWA,
    "indices": RWA, "fx": RWA, "rwa": RWA, "preipo": PREIPO, "pre-ipo": PREIPO,
}
# Venues file the same RWA under different headings (Arcus GLD: COMMODITIES; RISEx
# SPY-like funds: index_etf), so RWA members are matched on the coarse class, the price
# and the names, never on the venue's sub-heading.
PERP = "perp"
TOLERANCE = {CRYPTO: 0.01, RWA: 0.02}
TIER_B_TOLERANCE = 0.005
# A full name that reads as a company, fund, index or commodity is not a crypto token.
TRADFI_NAME = re.compile(
    r"\b(inc|corp|corporation|company|co|ltd|limited|plc|n\.?v|s\.?p\.?a|oyj|ag|sa|"
    r"holdings?|group|trust|etf|fund|shares|stock|bank|motors?|gold|silver|platinum|"
    r"palladium|copper|crude|oil|natural|brent|wti|index|s&p|nasdaq|treasury|swap|"
    r"futures?|incorporated|technologies)\b", re.I)

# Reasons a market is not part of a verified multi-venue group (report order).
INACTIVE = "INACTIVE"
NOT_PERPETUAL = "NOT_PERPETUAL"
CONFLICT = "CONFLICT_LIST"
PREIPO_REASON = "PREIPO"
MULTIPLIER_UNKNOWN = "MULTIPLIER_UNKNOWN"
SINGLE_VENUE = "SINGLE_VENUE_ONLY"
PRICE_MISSING = "PRICE_MISSING"
NO_ANCHOR = "NO_CLASS_ANCHOR"
CLASS_CONFLICT = "CLASS_CONFLICT"
PRICE_MISMATCH = "PRICE_MISMATCH"
NAME_CONFLICT = "NAME_CONFLICT"
AMBIGUOUS = "AMBIGUOUS_CLASS"
WEAK = "SINGLE_ANCHOR_UNCONFIRMED"
REASONS = (INACTIVE, NOT_PERPETUAL, CONFLICT, PREIPO_REASON, MULTIPLIER_UNKNOWN,
           SINGLE_VENUE, PRICE_MISSING, NO_ANCHOR, CLASS_CONFLICT, PRICE_MISMATCH,
           NAME_CONFLICT, AMBIGUOUS, WEAK)

CONFLICTS = {
    "QNT": "Variational QNT 178.2 vs Extended QNT 40.33 on 2026-11-12 (pilot marks)",
    "ANTHROPIC": "pre-IPO perp; venues price different share-count conventions "
                 "(Variational 1380 vs Lighter 1824 on 2026-11-12)",
    "OPENAI": "pre-IPO perp; share-count convention per venue",
    "ANT": "tradeXYZ xyz:ANT is a pre-IPO perp (perpCategories preipo)",
}

# Tickers that start with a digit but carry no multiplier.
DIGIT_TICKERS = frozenset(("1INCH", "2Z"))
MULTIPLIER_PREFIX = (("1000000", 1_000_000), ("1M", 1_000_000), ("1000", 1000), ("k", 1000))
QUOTE_SUFFIX = re.compile(r"(-PERP|-USD|-USDC|-USDT|USDT|USDC|/USD|/USDC|-SWAP)$", re.I)
NAME_NOISE = frozenset((
    "inc", "corp", "corporation", "co", "company", "ltd", "plc", "the", "class", "a",
    "etf", "trust", "token", "coin", "protocol", "network", "finance", "holdings",
    "group", "sa", "nv", "ag", "se", "spot", "on", "swap", "perp", "fund", "shares",
))

# Backwards-compatible curated list (used only by `resolve`, the single-market check).
CURATED_CRYPTO = frozenset((
    "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "DOT", "LTC", "BCH",
    "TRX", "SUI", "APT", "ARB", "OP", "NEAR", "ATOM", "UNI", "AAVE", "TON", "HYPE",
    "PEPE", "WIF", "BONK", "SHIB", "ENA", "TIA", "SEI", "INJ", "WLD", "ONDO", "PUMP",
    "FIL", "ETC", "XLM", "HBAR", "CRV", "LDO", "JUP", "TAO", "FET", "RENDER", "POL",
))
ASSET_CLASSES = ("crypto", "equity", "etf", "index", "commodity", "fx", "preipo", "unknown")


@dataclass
class Instrument:
    exchange_id: str
    market_id: str
    symbol: str
    asset_class: str = "unknown"      # fine class as the venue states it, or unknown
    contract_type: str = PERP
    quote_asset: str = None
    settlement_asset: str = None
    session: str = None
    base: str = None
    multiplier: float = None
    underlying_id: str = None
    status: str = UNVERIFIED
    evidence: list = field(default_factory=list)
    name: str = None                  # full name as the venue publishes it
    price: float = None               # mark (or mid) per contract
    class_source: str = None          # which venue field declared the class
    active: bool = True
    reason: str = None                # first reason it is not verified
    tier: str = "A"                   # "B": weaker evidence, never a strategy row

    @property
    def coarse(self):
        return FINE_TO_COARSE.get(str(self.asset_class or "").lower())

    @property
    def unit_price(self):
        if self.price is None or not self.multiplier:
            return None
        return self.price / self.multiplier


def parse_symbol(symbol):
    """(base, multiplier) from a venue symbol. xyz:NVDA -> NVDA; 1000PEPE / kPEPE ->
    PEPE x1000; BTC-PERP / BTCUSDT -> BTC x1. The multiplier is None when the symbol
    carries a numeric prefix this parser does not know."""
    s = str(symbol).strip()
    if ":" in s:
        s = s.split(":", 1)[1]
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


def name_tokens(name):
    words = re.findall(r"[a-z0-9]+", str(name or "").lower().replace("​", ""))
    return {w for w in words if w not in NAME_NOISE}


def names_agree(a, b):
    """False only when both names exist and share no meaningful word (and neither is
    contained in the other); missing names never conflict."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return True
    if ta & tb:
        return True
    ja, jb = "".join(sorted(ta)), "".join(sorted(tb))
    return ja in jb or jb in ja


def prepare(inst, registry=None):
    """Base and multiplier from the symbol (unless the venue gave them), registry
    override; returns the first hard reason it cannot be verified, or None."""
    base, mult = parse_symbol(inst.symbol)
    inst.base = inst.base or base
    if inst.multiplier is None:
        inst.multiplier = mult
    inst.evidence = list(inst.evidence or [])
    key = (inst.exchange_id, str(inst.market_id))
    if registry and key in registry:
        uid = registry[key]
        inst.underlying_id = uid
        inst.asset_class = uid.split(":", 1)[0]
        inst.class_source = "REGISTRY"
        inst.evidence.append("REGISTRY")
    if not inst.active:
        return INACTIVE
    if inst.contract_type != PERP:
        return NOT_PERPETUAL
    if inst.base in CONFLICTS:
        inst.evidence.append("CONFLICT: " + CONFLICTS[inst.base])
        return CONFLICT
    if inst.coarse == PREIPO:
        return PREIPO_REASON
    if inst.multiplier is None:
        return MULTIPLIER_UNKNOWN
    return None


def verify(instruments, registry=None):
    """Verify every instrument in place. Returns {group_id: dict(status, venues,
    members, coarse)} for the verified groups (MULTI_VENUE or SINGLE_VENUE_ONLY) and
    sets `status`, `reason`, `underlying_id` and `evidence` on each instrument."""
    for i in instruments:
        i.status, i.reason, i.underlying_id, i.tier = UNVERIFIED, None, None, "A"
        i.reason = prepare(i, registry)
    live = [i for i in instruments if i.reason is None]
    by_base = {}
    for i in live:
        by_base.setdefault(i.base, []).append(i)
    groups = {}
    for base, members in sorted(by_base.items()):
        if len({m.exchange_id for m in members}) < 2:
            for m in members:
                m.reason = SINGLE_VENUE
            continue
        for m in members:
            if m.unit_price is None:
                m.reason = PRICE_MISSING
        members = [m for m in members if m.reason is None]
        anchors = [m for m in members if m.coarse in (CRYPTO, RWA)]
        floating = [m for m in members if m.coarse is None]
        if not anchors:
            for m in members:
                m.reason = NO_ANCHOR
            continue
        subgroups = {}
        for m in anchors:
            subgroups.setdefault(m.coarse, []).append(m)
        verified = {}
        for coarse, sub in subgroups.items():
            ref = statistics.median(m.unit_price for m in sub)
            tol = TOLERANCE[coarse]
            good = []
            for m in sub:
                if abs(m.unit_price / ref - 1) > tol:
                    m.reason = PRICE_MISMATCH
                else:
                    good.append(m)
            if good:
                verified[coarse] = (ref, good)
        for m in floating:
            fits = [c for c, (ref, good) in verified.items()
                    if abs(m.unit_price / ref - 1) <= TOLERANCE[c]]
            if not fits:
                m.reason = PRICE_MISMATCH
            elif len(fits) > 1:
                m.reason = AMBIGUOUS
            else:
                anchors_ok = verified[fits[0]][1]
                independent = len({a.exchange_id for a in anchors_ok}) >= 2
                named = any(a.name and m.name and name_tokens(a.name) & name_tokens(m.name)
                            for a in anchors_ok)
                if independent or named:
                    verified[fits[0]][1].append(m)
                elif (fits[0] == CRYPTO and m.name and not TRADFI_NAME.search(m.name)
                      and abs(m.unit_price / verified[CRYPTO][0] - 1) <= TIER_B_TOLERANCE):
                    # Tier B: one crypto anchor, a crypto-like own name and a price
                    # within half the tier-A tolerance. Reported, never a strategy row.
                    m.tier = "B"
                    m.evidence.append("TIER_B: single crypto anchor + crypto-like name "
                                      "+ price within %.1f%%" % (TIER_B_TOLERANCE * 100))
                    verified[fits[0]][1].append(m)
                else:
                    # One venue's class plus a price is not enough for a member that
                    # declares no class itself (e.g. TxFlow GAS "Commodities" vs
                    # Variational "Gas", 2026-11-12).
                    m.reason = WEAK
        for coarse, (ref, good) in verified.items():
            named = [m for m in good if m.name]
            disagree = any(not names_agree(x.name, y.name)
                           for i, x in enumerate(named) for y in named[i + 1:])
            if disagree:
                # Which name is right is unknown: every named member leaves the group.
                for m in named:
                    m.reason = NAME_CONFLICT
                good = [m for m in good if not m.name]
                if not any(m.coarse for m in good):
                    for m in good:
                        m.reason = NO_ANCHOR
                    continue
            uid = "%s:%s" % (coarse, base)
            venues = sorted({m.exchange_id for m in good})
            for m in good:
                m.underlying_id = uid
                m.status = VERIFIED
                m.evidence.append("ANCHOR:" + (m.class_source or "?") if m.coarse
                                  else "JOINED_BY_PRICE_AND_NAME")
                if len(venues) < 2:
                    m.reason = SINGLE_VENUE
            venues_a = sorted({m.exchange_id for m in good if m.tier == "A"})
            tier = "A" if len(venues_a) >= 2 else "B"
            if tier == "B":
                # Without two tier-A venues no pair in this group is tier A.
                for m in good:
                    m.tier = "B"
            groups[uid] = dict(status="MULTI_VENUE" if len(venues) >= 2 else SINGLE_VENUE,
                               venues=venues, venues_a=venues_a, tier=tier, members=good,
                               coarse=coarse, ref_price=ref)
    return groups




# ----------------------------------------------------------------- single-market API
def resolve(inst, registry=None):
    """Single-market check (no cross-venue evidence): REGISTRY, or curated crypto with
    the venue's own crypto class. Kept for callers that look at one market."""
    reason = prepare(inst, registry)
    if reason is not None:
        inst.status, inst.underlying_id = UNVERIFIED, None
        return inst
    if inst.class_source == "REGISTRY":
        inst.status = VERIFIED
        return inst
    if inst.coarse == CRYPTO and inst.base in CURATED_CRYPTO:
        inst.underlying_id = "crypto:" + inst.base
        inst.status = VERIFIED
        inst.evidence.append("CURATED_CRYPTO + venue class crypto")
        return inst
    inst.status, inst.underlying_id = UNVERIFIED, None
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
