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
