"""Canonical market identity.

One record per venue instrument keeps the venue's own symbol and id and every contract
field with its provenance:

  OBSERVED  read from a field of the venue's response;
  DERIVED   computed from an observed field (e.g. a step from a decimals count);
  DECLARED  stated by the adapter or a naming convention, not by the response;
  UNKNOWN   not published or not read: the value stays None, never filled in.

Instruments of different venues share a canonical id only through their base asset
after a declared size prefix (1000PEPE, kPEPE) is removed. A shared id is a
candidate, not a proof that the contracts are the same: the identity check compares
their recorded marks scaled by the multiplier and reports PRICE_CONSISTENT,
PRICE_CONFLICT, NO_PRICE or SINGLE_VENUE.
"""

import math
import re
import statistics
from dataclasses import asdict, dataclass, field

from .venues import venue_id

OBSERVED = "OBSERVED"
DERIVED = "DERIVED"
DECLARED = "DECLARED"
UNKNOWN = "UNKNOWN"

PERPETUAL = "PERPETUAL"
LISTED = "LISTED"
INACTIVE = "INACTIVE"
DELISTED = "DELISTED"

# Same tolerance the paper desk uses to tell two tokens sharing a ticker apart.
MAX_PRICE_GAP = 0.15
# Size prefixes some venues put on low-priced assets. A naming convention, so the
# multiplier it gives is DECLARED until the price check agrees.
PREFIXES = (
    (re.compile(r"^1000000([A-Z0-9]+)$"), 1_000_000),
    (re.compile(r"^1M([A-Z][A-Z0-9]*)$"), 1_000_000),
    (re.compile(r"^10000([A-Z0-9]+)$"), 10_000),
    (re.compile(r"^1000([A-Z0-9]+)$"), 1_000),
    (re.compile(r"^k([A-Z][A-Z0-9]*)$"), 1_000),
)
FIELDS = (
    "base",
    "quote",
    "settlement_asset",
    "contract_type",
    "contract_multiplier",
    "tick_size",
    "lot_size",
    "min_order_size",
    "price_precision",
    "quantity_precision",
    "max_leverage",
    "funding_interval_seconds",
    "status",
)


@dataclass
class Instrument:
    canonical_id: str
    venue: str
    venue_symbol: str
    venue_market_id: str
    base: str = None
    quote: str = None
    settlement_asset: str = None
    contract_type: str = None
    contract_multiplier: float = None
    tick_size: float = None
    lot_size: float = None
    min_order_size: float = None
    price_precision: int = None
    quantity_precision: int = None
    max_leverage: float = None
    funding_interval_seconds: float = None
    status: str = None
    provenance: dict = field(default_factory=dict)
    first_seen: float = None
    last_seen: float = None
    status_history: list = field(default_factory=list)
    raw_sha256: str = None

    def as_dict(self):
        return asdict(self)


def split_prefix(symbol):
    """(base, multiplier, prefixed) of a venue symbol; no prefix gives (symbol, 1)."""
    for rx, mult in PREFIXES:
        m = rx.match(symbol or "")
        if m:
            return m.group(1), mult, True
    return symbol, 1, False


def canonical_id(base, contract_type=PERPETUAL):
    return "%s:%s" % ("PERP" if contract_type == PERPETUAL else contract_type, base)


def precision(step):
    """Decimal places of a power-of-ten step (0.001 -> 3); None for any other step."""
    if not step or step <= 0:
        return None
    p = -math.log10(step)
    return int(round(p)) if abs(p - round(p)) < 1e-9 and round(p) >= 0 else None


def _set(inst, name, value, status):
    setattr(inst, name, value)
    inst.provenance[name] = status if value is not None else UNKNOWN


def from_recorder(rec, history=()):
    """An Instrument from the recorder's newest market record of one market, with its
    listing status over the earlier records of the same market (`history`)."""
    vid = venue_id(rec["venue"])
    symbol = str(rec.get("asset"))
    mid = str(rec.get("market_id"))
    base_raw = rec.get("base")
    base, mult, prefixed = split_prefix(base_raw or symbol)
    inst = Instrument(
        canonical_id=canonical_id(base),
        venue=vid,
        venue_symbol=symbol,
        venue_market_id=mid,
        raw_sha256=rec.get("raw_sha256"),
    )
    _set(inst, "base", base, OBSERVED if base_raw and not prefixed else DERIVED)
    _set(inst, "quote", rec.get("quote") if vid != "extended" else None, OBSERVED)
    # Extended publishes collateralAssetName; reading the collateral as the
    # settlement asset is the adapter's interpretation.
    _set(
        inst,
        "settlement_asset",
        rec.get("quote") if vid == "extended" else None,
        DECLARED,
    )
    mtype = (rec.get("market_type") or "").lower()
    _set(
        inst,
        "contract_type",
        PERPETUAL if mtype in ("perp", "perpetual") else (mtype.upper() or None),
        # The Hyperliquid parser writes "perp" for every universe entry itself.
        DECLARED if vid == "hyperliquid" else OBSERVED,
    )
    size = rec.get("contract_size")
    _set(
        inst,
        "contract_multiplier",
        mult * size if size is not None else (mult if prefixed else None),
        DECLARED,
    )
    _set(inst, "tick_size", rec.get("price_step"), _step_status(vid, "price"))
    _set(inst, "lot_size", rec.get("amount_step"), _step_status(vid, "amount"))
    _set(inst, "min_order_size", rec.get("min_order_size"), OBSERVED)
    _set(inst, "price_precision", precision(inst.tick_size), DERIVED)
    _set(inst, "quantity_precision", precision(inst.lot_size), DERIVED)
    _set(
        inst,
        "max_leverage",
        rec.get("max_leverage"),
        DERIVED if vid in ("lighter_core", "lighter_rh") else OBSERVED,
    )
    _set(
        inst,
        "funding_interval_seconds",
        rec.get("funding_interval"),
        rec.get("funding_interval_status") or UNKNOWN,
    )
    active = rec.get("active")
    delisted = (rec.get("extra") or {}).get("isDelisted")
    _set(
        inst,
        "status",
        (
            DELISTED
            if delisted
            else (None if active is None else LISTED if active else INACTIVE)
        ),
        OBSERVED,
    )
    seen = [r for r in history if r.get("observed_at") is not None] + [rec]
    inst.first_seen = min(
        (r["observed_at"] for r in seen if r.get("observed_at")), default=None
    )
    inst.last_seen = rec.get("observed_at")
    prev = object()
    for r in sorted(seen, key=lambda r: r.get("observed_at") or 0):
        state = r.get("active")
        if state != prev:
            inst.status_history.append(dict(at=r["observed_at"], active=state))
            prev = state
    return inst


def _step_status(vid, which):
    # Lighter publishes decimals counts and Hyperliquid szDecimals: the steps are
    # computed from them. Extended publishes the steps themselves.
    if vid == "extended":
        return OBSERVED
    return DERIVED


def instruments(evidence, venues):
    """Instruments of the recorded venues, newest record per market."""
    out = []
    for name in venues:
        by_market = {}
        for r in evidence.markets(name):
            by_market.setdefault(str(r.get("market_id")), []).append(r)
        for rows in by_market.values():
            out.append(from_recorder(rows[-1], rows[:-1]))
    return out


def identities(insts, marks, max_gap=MAX_PRICE_GAP):
    """Group instruments by canonical id and check each group's recorded marks.

    `marks` maps (venue, venue_market_id) to a mark price. A member's mark is divided
    by its multiplier; members further than `max_gap` from the group median are
    PRICE_CONFLICT. Different known settlement assets are a conflict as well."""
    groups = {}
    for i in insts:
        groups.setdefault(i.canonical_id, []).append(i)
    out = {}
    for cid, members in sorted(groups.items()):
        venues = sorted({m.venue for m in members})
        priced = []
        for m in members:
            px = marks.get((m.venue, m.venue_market_id))
            if px and m.contract_multiplier:
                priced.append((m, px / m.contract_multiplier))
        settle = {m.settlement_asset for m in members if m.settlement_asset}
        conflicts = []
        if len(venues) < 2:
            status = "SINGLE_VENUE"
        elif len(priced) < 2:
            status = "NO_PRICE"
        else:
            mid = statistics.median(p for _, p in priced)
            conflicts = [
                dict(venue=m.venue, symbol=m.venue_symbol, unit_price=p)
                for m, p in priced
                if abs(p / mid - 1) > max_gap
            ]
            status = "PRICE_CONFLICT" if conflicts else "PRICE_CONSISTENT"
        if len(settle) > 1:
            status = "SETTLEMENT_CONFLICT"
        out[cid] = dict(
            status=status,
            venues=venues,
            members=[
                dict(
                    venue=m.venue,
                    symbol=m.venue_symbol,
                    market_id=m.venue_market_id,
                    multiplier=m.contract_multiplier,
                    multiplier_status=m.provenance.get("contract_multiplier"),
                )
                for m in members
            ],
            conflicts=conflicts,
        )
    return out
