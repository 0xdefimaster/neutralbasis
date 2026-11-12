"""RWA reference-contract registry: what each venue's own primary documentation says a
TradFi perp tracks, field by field, with the source URL of every field.

A pair of RWA legs is REFERENCE_VERIFIED only when every field in REQUIRED is known on
both legs AND equal. One UNKNOWN or one difference keeps the PAPER gate closed
(RWA_REFERENCE_UNVERIFIED), and the gate text names the fields. Nothing here is
inferred: a field a venue does not document stays UNKNOWN.

Fields:
  underlying       what is priced (e.g. "Henry Hub natural gas, USD per MMBtu")
  exchange         listing exchange of the reference futures (e.g. NYMEX); UNKNOWN when
                   the venue does not name it
  contract_month   which futures month is referenced today (e.g. "F27")
  roll_rule        when and how the reference moves to the next month
  price_source     who supplies the index/oracle price
  off_hours        how the price is formed when the reference market is closed
  settlement       expiry/settlement of the perp itself (perps: none) and funding basis

Read 2026-11-12 (official docs only; reports in docs/v2/29_ROOT_CAUSE_FIXES.md).
"""
UNKNOWN = "UNKNOWN"
REQUIRED = ("underlying", "exchange", "contract_month", "roll_rule", "price_source",
            "off_hours", "settlement")

XYZ = "https://docs.trade.xyz/perpetuals/"
VAR = "https://docs.variational.io/omni/trading/tradfi-perpetuals.md"
TXF = "https://docs.txflow.com/"

REGISTRY = {
    ("tradexyz", "NATGAS"): dict(
        underlying=("USD price of 1 MMBtu of Henry Hub natural gas",
                    XYZ + "specifications-and-schedules/specification-index.md"),
        exchange=(UNKNOWN, XYZ + "markets/commodities.md (\"futures contracts\", no "
                           "exchange named)"),
        contract_month=("Z26 -> F27 roll window 2026-11-06 17:30 ET to 2026-11-12 17:30 ET",
                        XYZ + "specifications-and-schedules/roll-schedules.md"),
        roll_rule=("CONFLICT: 5th-10th business day (commodities.md) vs 5th-9th business "
                   "day (roll-schedules.md)",
                   XYZ + "markets/commodities.md ; " + XYZ +
                   "specifications-and-schedules/roll-schedules.md"),
        price_source=("Relayer over 'venues, markets and institutional data providers' "
                      "(provider not named)", XYZ + "mechanics/oracle-price.md"),
        off_hours=("discovery bounds +-10% around the last external price; oracle EMA "
                   "tau 30 min when external data is unavailable",
                   XYZ + "mechanics/discovery-bounds.md"),
        settlement=("no expiry stated; funding interval UNKNOWN on xyz pages "
                    "(Hyperliquid: hourly)", XYZ + "changelog/funding-rate-formula-"
                    "updates.md"),
    ),
    ("variational", "NATGAS"): dict(
        underlying=("Henry Hub Natural Gas futures", VAR),
        exchange=(UNKNOWN, VAR),
        contract_month=(UNKNOWN, VAR),
        roll_rule=("Continuous Month Roll: 5th-9th trading day of the calendar month of "
                   "the near-month contract's last trading day", VAR),
        price_source=("Omni index (OLP); weighted average in regular hours", VAR),
        off_hours=("commodities: Orderbook EWMA Mode (impact mid price, EWMA, movement "
                   "limits)", VAR),
        settlement=("perpetual, no expiry; funding interval per listing "
                    "(NATGAS 14400 s observed); settlement price UNKNOWN",
                    "https://docs.variational.io/omni/trading/funding-rates.md"),
    ),
    ("txflow", "NATGAS"): dict(
        underlying=(UNKNOWN, TXF + "trade/contract-specifications.md (generic: 'TxFlow "
                             "oracle index of the spot asset'; NATGAS not documented)"),
        exchange=(UNKNOWN, TXF),
        contract_month=(UNKNOWN, TXF),
        roll_rule=(UNKNOWN, TXF),
        price_source=("validator-published aggregated external reference price "
                      "(provider not named)", TXF + "txcore/oracle.md"),
        off_hours=(UNKNOWN, TXF),
        settlement=("perpetual, no expiry; hourly funding on mark price",
                    TXF + "trade/funding.md"),
    ),
}


def spec(exchange_id, asset):
    return REGISTRY.get((exchange_id, asset))


def value(s, field):
    v = (s or {}).get(field)
    return v[0] if v else UNKNOWN


def known(v):
    return v != UNKNOWN and not str(v).startswith("CONFLICT")


def compare(a_xid, b_xid, asset):
    """dict(verified, missing, different, fields) for two venues' references.
    verified is True only when every REQUIRED field is known on both and equal."""
    a, b = spec(a_xid, asset), spec(b_xid, asset)
    missing, different, fields = [], [], {}
    for f in REQUIRED:
        va, vb = value(a, f), value(b, f)
        fields[f] = dict(a=va, b=vb)
        if not known(va) or not known(vb):
            missing.append(f)
        elif va != vb:
            different.append(f)
    return dict(verified=not missing and not different, missing=missing,
                different=different, fields=fields,
                registered=dict(a=a is not None, b=b is not None))


def gate_text(a_xid, b_xid, asset, fine):
    c = compare(a_xid, b_xid, asset)
    if not c["registered"]["a"] or not c["registered"]["b"]:
        return "%s: no registered reference spec for %s (%s)" % (
            asset, " and ".join(x for x, k in ((a_xid, "a"), (b_xid, "b"))
                                if not c["registered"][k]), fine)
    return "%s (%s): unknown %s; different %s" % (
        asset, fine, ",".join(c["missing"]) or "-", ",".join(c["different"]) or "-")



# ----------------------------------------------------------------- classification
# Comparing documentation TEXT is not a test of equivalence: two venues can describe the
# same contract in different words, and the same ticker or a similar price proves
# nothing. Each spec is therefore reduced to normalized identifiers, set by hand from
# the cited documents (never derived from tickers or prices). UNKNOWN stays UNKNOWN.
#
#   underlying_id   what is referenced (a commodity/benchmark id)
#   unit            price unit
#   exchange        listing exchange of the reference futures
#   contract_month  the futures month referenced at the evaluation time
#   roll_rule_id    the roll schedule as one rule id (a self-contradicting doc = UNKNOWN)
#
# Oracle/price method and off-hours handling are NOT identity fields: different
# methods on the same contract are a pricing (basis) risk, reported as
# `method_differences`, not a different underlying.
EXACT_REFERENCE = "EXACT_REFERENCE"
ECONOMICALLY_RELATED = "ECONOMICALLY_RELATED"
DIFFERENT_UNDERLYING = "DIFFERENT_UNDERLYING"
UNKNOWN_CLASS = "UNKNOWN"
CLASSES = (EXACT_REFERENCE, ECONOMICALLY_RELATED, DIFFERENT_UNDERLYING, UNKNOWN_CLASS)
CONTRACT_FIELDS = ("unit", "exchange", "contract_month", "roll_rule_id")
METHOD_FIELDS = ("price_source", "off_hours")

NORM = {
    # docs.trade.xyz specification-index: "USD price of 1 MMBtu of Henry Hub natural
    # gas"; roll-schedules: Z26 -> F27 window ends 2026-11-12 17:30 ET; the roll rule
    # differs between two of its own pages (5th-9th vs 5th-10th business day).
    ("tradexyz", "NATGAS"): dict(underlying_id="HENRY_HUB_NATURAL_GAS", unit="USD/MMBtu",
                                 exchange=UNKNOWN, contract_month="Z26->F27 (rolling)",
                                 roll_rule_id=UNKNOWN),
    # docs.variational.io tradfi-perpetuals: "Henry Hub Natural Gas futures"; Continuous
    # Month Roll (5th-9th trading day of the near-month LTD month); unit and current
    # month not stated.
    ("variational", "NATGAS"): dict(underlying_id="HENRY_HUB_NATURAL_GAS", unit=UNKNOWN,
                                    exchange=UNKNOWN, contract_month=UNKNOWN,
                                    roll_rule_id="CONTINUOUS_MONTH_ROLL_TD5_TD9_LTD_MONTH"),
    # docs.txflow.com: NATGAS is not documented at all.
    ("txflow", "NATGAS"): dict(underlying_id=UNKNOWN, unit=UNKNOWN, exchange=UNKNOWN,
                               contract_month=UNKNOWN, roll_rule_id=UNKNOWN),
}


def classify(a_xid, b_xid, asset):
    """dict(cls, hedge_ok, unknown, different, method_differences) for two legs.

    UNKNOWN               a leg has no normalized spec or its underlying is UNKNOWN
    DIFFERENT_UNDERLYING  both underlyings known and different
    EXACT_REFERENCE       same underlying and every contract field known and equal
    ECONOMICALLY_RELATED  same underlying, but a contract field is unknown or differs
    Only EXACT_REFERENCE is hedge-eligible (strict PAPER); the others stay available
    to research and scenario simulation with their basis risk named."""
    a, b = NORM.get((a_xid, asset)), NORM.get((b_xid, asset))
    method = [f for f in METHOD_FIELDS
              if value(spec(a_xid, asset), f) != value(spec(b_xid, asset), f)]
    if a is None or b is None or not known(a["underlying_id"]) \
            or not known(b["underlying_id"]):
        return dict(cls=UNKNOWN_CLASS, hedge_ok=False, unknown=["underlying_id"],
                    different=[], method_differences=method)
    if a["underlying_id"] != b["underlying_id"]:
        return dict(cls=DIFFERENT_UNDERLYING, hedge_ok=False, unknown=[],
                    different=["underlying_id"], method_differences=method)
    unknown = [f for f in CONTRACT_FIELDS if not known(a[f]) or not known(b[f])]
    different = [f for f in CONTRACT_FIELDS if f not in unknown and a[f] != b[f]]
    exact = not unknown and not different
    return dict(cls=EXACT_REFERENCE if exact else ECONOMICALLY_RELATED, hedge_ok=exact,
                unknown=unknown, different=different, method_differences=method)


def gate_text_class(a_xid, b_xid, asset, fine):
    c = classify(a_xid, b_xid, asset)
    return "%s (%s): %s; unknown %s; different %s; method differences %s" % (
        asset, fine, c["cls"], ",".join(c["unknown"]) or "-",
        ",".join(c["different"]) or "-", ",".join(c["method_differences"]) or "-")
