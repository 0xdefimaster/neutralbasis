"""Funding semantics per venue and the funding observation / settlement contract
(Phase 15 P0-A). PAPER research; no network.

Why: a "funding rate" field means different things on different venues. On some it is
a running estimate of the CURRENT period that converges to the payment; on others its
relation to the payment is unknown; Lighter RH has no published current rate at all
(the last settlement is used as a projection). A published value is therefore valid
for its current period at most. Anything beyond that is a MODEL
(v2/funding_expectation.py), never an observation.

Three record types, never mixed:
  observation   what a venue publishes now about the current period
                (settlement_status PROJECTED, valid_for CURRENT_PERIOD_ONLY)
  settlement    a venue's public per-market applied rate for a past period
                (SETTLED_PUBLIC, valid_for PAST_PERIOD); or an account payment
                (ACCOUNT_PAYMENT; none in PAPER: no authorized address)
  expectation   a model over a horizon (baseline / persistence / adverse); ESTIMATED

Sign (one convention, as v2/funding_evidence): rate > 0 = longs pay shorts.
Rates in the contract are fractions; `rate_per_hour` = rate per period / (interval/1h).
Missing values stay None; a unit that is not known is never guessed.
"""
from v2 import fees

SCHEMA_VERSION = "funding-contract/1"
HOUR = 3600.0

# Rate kinds of a PUBLISHED current value.
RUNNING_CONVERGES = "CURRENT_PERIOD_RUNNING_CONVERGES"     # observed to end at the payment
RUNNING_PAYMENT_UNKNOWN = "CURRENT_PERIOD_RUNNING_PAYMENT_UNKNOWN"
LAST_SETTLED = "LAST_SETTLED_PROJECTION"                   # last settlement re-used
UNVERIFIED = "PUBLISHED_SEMANTICS_UNVERIFIED"              # never observed inside a period
UNKNOWN = "UNKNOWN"
RATE_KINDS = (RUNNING_CONVERGES, RUNNING_PAYMENT_UNKNOWN, LAST_SETTLED, UNVERIFIED, UNKNOWN)

# Settlement status of a record.
PROJECTED = "PROJECTED"
SETTLED_PUBLIC = "SETTLED_PUBLIC"
ACCOUNT_PAYMENT = "ACCOUNT_PAYMENT"

# How a fact about a venue is known (weakest first).
EV_UNKNOWN = "UNKNOWN"
EV_ENGINE = "ENGINE_CONVENTION"        # the legacy engine's choice; docs silent
EV_INFERRED = "INFERRED"               # same code base / same API family, not observed
EV_DOC = "DOC"                         # read in the venue's docs (date in `ref`)
EV_OBSERVED = "OBSERVED"               # seen in recorded public data (sample in `ref`)
EV_VERIFIED = "VERIFIED"               # docs AND recorded data agree

_STREAM = "Phase 8 rescued stream 2026-11-12 11:39-12:09 UTC (docs/v2/43 §3)"

# One entry per venue. `published` describes the CURRENT-rate field; `settled` the
# public history; `account` the account payment source. Every claim carries its
# evidence level; nothing here is a default for missing data.
VENUES = {
    "tradexyz": dict(
        published=dict(field="metaAndAssetCtxs ctx.funding", unit="fraction",
                       period_basis="per_hour", rate_kind=RUNNING_CONVERGES,
                       rate_kind_evidence=EV_OBSERVED,
                       rate_kind_ref="xyz:NATGAS last value before 12:00 UTC 6.905777e-4 "
                       "vs settled 6.906106e-4; 62 distinct values in 62 samples; " + _STREAM),
        interval_s=3600, interval_evidence=EV_DOC, interval_ref="trading/funding docs "
        "(read 2026-11-12): paid every hour, applies to HIP-3",
        next_payment="DERIVED: next whole hour (settled history times are on the hour)",
        positive_pays="long", sign_evidence=EV_DOC, sign_ref="Hyperliquid funding docs "
        "(read 2026-11-12); cross-venue check: tools/v2_funding_semantics_check.py",
        price_basis="oracle", formula=EV_DOC,
        settled="info fundingHistory coin xyz:SYM", account="UNVERIFIED (no address)"),
    "hyperliquid_native": dict(
        published=dict(field="metaAndAssetCtxs ctx.funding", unit="fraction",
                       period_basis="per_hour", rate_kind=RUNNING_CONVERGES,
                       rate_kind_evidence=EV_OBSERVED,
                       rate_kind_ref="INIT last value before 12:00 UTC -8.87029e-5 vs "
                       "settled -8.89231e-5; " + _STREAM),
        interval_s=3600, interval_evidence=EV_DOC, interval_ref="trading/funding docs",
        next_payment="DERIVED: next whole hour",
        positive_pays="long", sign_evidence=EV_DOC, sign_ref="Hyperliquid funding docs",
        price_basis="oracle", formula=EV_DOC,
        settled="info fundingHistory coin SYM", account="UNVERIFIED (no address)"),
    "entropy_io": dict(
        published=dict(field="metaAndAssetCtxs ctx.funding (dex io)", unit="fraction",
                       period_basis="per_hour", rate_kind=RUNNING_CONVERGES,
                       rate_kind_evidence=EV_INFERRED,
                       rate_kind_ref="same Hyperliquid info API as tradexyz / native; "
                       "not observed for io"),
        interval_s=3600, interval_evidence=EV_INFERRED, interval_ref="HIP-3 docs",
        next_payment="DERIVED: next whole hour",
        positive_pays="long", sign_evidence=EV_INFERRED, sign_ref="Hyperliquid docs",
        price_basis="oracle", formula=EV_INFERRED,
        settled="info fundingHistory coin io:SYM", account="UNVERIFIED (no address)"),
    "txflow": dict(
        published=dict(field="activeAssetCtx nodeCtx.funding", unit="percent",
                       period_basis="per_hour", rate_kind=RUNNING_PAYMENT_UNKNOWN,
                       rate_kind_evidence=EV_OBSERVED,
                       rate_kind_ref="NATGAS 2026-11-12: continuous fall to the period "
                       "end, carried over the boundary, a different pattern after it "
                       "(docs/v2/41 Finding 1); formula not documented"),
        interval_s=3600, interval_evidence=EV_DOC, interval_ref="settlementIntervalMs "
        "(docs/v2/09); nodeCtx next-prev when both are present",
        next_payment="VENUE_FIELD nodeCtx.nextFundingTime (ms)",
        positive_pays="long", sign_evidence=EV_DOC, sign_ref="docs.txflow.com trade/funding "
        "(read 2026-11-12)", price_basis="mark", formula=EV_UNKNOWN,
        settled=None, account="userFundings: needs an owner-authorized address (none)"),
    "variational": dict(
        published=dict(field="stats funding.last.interval_rate", unit="fraction",
                       period_basis="per_interval", rate_kind=RUNNING_PAYMENT_UNKNOWN,
                       rate_kind_evidence=EV_OBSERVED,
                       rate_kind_ref="funding.last == funding.next in 152/152 listings; "
                       "changes about every 5 min (not a settled value); " + _STREAM),
        interval_s=None, interval_evidence=EV_DOC, interval_ref="funding.interval_seconds "
        "per market (1 h to 8 h, docs)",
        next_payment="VENUE_FIELD funding.next_timestamp (s); seen up to 285 s in the past "
        "after a boundary",
        positive_pays="long", sign_evidence=EV_DOC, sign_ref="docs.variational.io funding",
        price_basis="index", formula=EV_DOC,
        settled=None, account="no trading API (none)"),
    "lighter_rh": dict(
        published=dict(field="(none) last row of /api/v1/fundings", unit="fraction",
                       period_basis="per_hour", rate_kind=LAST_SETTLED,
                       rate_kind_evidence=EV_ENGINE,
                       rate_kind_ref="no current-rate field is read; the adapter labels "
                       "the last settlement last_settled_projection"),
        interval_s=3600, interval_evidence=EV_DOC, interval_ref="hourly (Lighter docs; RH "
        "lists differences only)",
        next_payment="DERIVED: last settlement + 1 h",
        positive_pays="long", sign_evidence=EV_ENGINE, sign_ref="`direction` long = the "
        "long pays (engine convention; docs do not define it); `rate` percent (engine)",
        price_basis="index", formula=EV_DOC,
        settled="/api/v1/fundings", account="UNVERIFIED (no address)"),
    "arcus": dict(
        published=dict(field="markets fundingRate", unit="fraction",
                       period_basis="per_hour", rate_kind=UNVERIFIED,
                       rate_kind_evidence=EV_UNKNOWN,
                       rate_kind_ref="never sampled inside a period"),
        interval_s=3600, interval_evidence=EV_DOC, interval_ref="docs: hourly",
        next_payment="VENUE_FIELD nextFundingAt (s)",
        positive_pays="long", sign_evidence=EV_DOC, sign_ref="docs formula (2026-11-12)",
        price_basis="oracle", formula=EV_DOC,
        settled="/v1/fundingRates", account="UNVERIFIED (no address)"),
    "risex": dict(
        published=dict(field="markets current_funding_rate", unit="fraction",
                       period_basis="per_interval", rate_kind=UNVERIFIED,
                       rate_kind_evidence=EV_UNKNOWN,
                       rate_kind_ref="never sampled inside a period; docs also say "
                       "funding settles when a trade touches the market (docs/v2/09)"),
        interval_s=None, interval_evidence=EV_DOC, interval_ref="funding_interval (ns)",
        next_payment="VENUE_FIELD next_funding_time (ns)",
        positive_pays="long", sign_evidence=EV_DOC, sign_ref="WS example (docs)",
        price_basis="index (verified: accumulated_funding steps by rate x index)",
        formula=EV_UNKNOWN,
        settled="markets/id/{id}/funding-rate-history", account="UNVERIFIED (no address)"),
}
for _x in ("popdex_perps", "veranta", "phoenix_perps", "qfex"):
    VENUES[_x] = dict(
        published=dict(field=None, unit=None, period_basis=None, rate_kind=UNKNOWN,
                       rate_kind_evidence=EV_UNKNOWN, rate_kind_ref="no verified public API"),
        interval_s=None, interval_evidence=EV_UNKNOWN, interval_ref=None,
        next_payment="UNKNOWN", positive_pays=None, sign_evidence=EV_UNKNOWN,
        sign_ref=None, price_basis=None, formula=EV_UNKNOWN, settled=None, account=None)


def venue(exchange_id):
    return VENUES.get(exchange_id)


def rate_kind(exchange_id):
    v = venue(exchange_id)
    return v["published"]["rate_kind"] if v else UNKNOWN


def payment_relation_known(exchange_id):
    """True only where recorded data showed the published value ending at the payment."""
    v = venue(exchange_id)
    return bool(v) and v["published"]["rate_kind"] == RUNNING_CONVERGES and \
        v["published"]["rate_kind_evidence"] in (EV_OBSERVED, EV_VERIFIED)


def _fraction(raw, unit):
    if raw is None or unit is None:
        return None, "RATE_OR_UNIT_MISSING"
    try:
        return fees.to_fraction(raw, unit), None
    except ValueError:
        return None, "UNIT_UNKNOWN"


def observation(exchange_id, market_id, funding, observed_at, evidence="OBSERVED",
                source_ts=None, raw_ref=None):
    """A venue's published current funding (an adapter's `funding` dict) -> contract.

    `funding`: dict(raw, unit, interval_s, next_ts, kind, sign, source) or None.
    `evidence`: OBSERVED (live read), HISTORICAL (replay of a saved read) or MOCK (test).
    Returns a dict that is never a payment: settlement_status PROJECTED and
    valid_for CURRENT_PERIOD_ONLY; `warnings` lists everything not proven."""
    v = venue(exchange_id)
    warnings = []
    f = funding or {}
    frac, why = _fraction(f.get("raw"), f.get("unit"))
    if why:
        warnings.append(why)
    iv, iv_src = f.get("interval_s"), "VENUE_FIELD"
    if not iv and v and v.get("interval_s"):
        iv, iv_src = v["interval_s"], "VENUE_DOC"
        warnings.append("INTERVAL_FROM_DOC")
    if not iv:
        iv, iv_src = None, "UNKNOWN"
        warnings.append("INTERVAL_UNKNOWN")
    per_hour = None if frac is None or not iv else frac / (iv / HOUR)
    nxt = f.get("next_ts")
    if nxt is None:
        next_status = "UNKNOWN"
        warnings.append("NEXT_PAYMENT_UNKNOWN")
    elif nxt <= observed_at:
        next_status = "PAST_AT_READ"      # the venue has not rolled it yet: not usable
        warnings.append("NEXT_PAYMENT_IN_PAST")
    else:
        next_status = "FUTURE"
    kind = f.get("kind")
    rk = LAST_SETTLED if kind == "last_settled_projection" else rate_kind(exchange_id)
    if rk == RUNNING_PAYMENT_UNKNOWN:
        warnings.append("PAYMENT_RELATION_UNKNOWN")
    elif rk in (UNVERIFIED, UNKNOWN):
        warnings.append("RATE_KIND_UNVERIFIED")
    elif rk == LAST_SETTLED:
        warnings.append("LAST_SETTLEMENT_REUSED")
    if v is None:
        warnings.append("VENUE_NOT_IN_REGISTRY")
    return dict(
        schema_version=SCHEMA_VERSION, record="observation", exchange_id=exchange_id,
        market_id=market_id, rate_raw=f.get("raw"), unit=f.get("unit"),
        rate_per_period=frac, rate_per_hour=per_hour, interval_s=iv,
        interval_source=iv_src, next_payment_ts=nxt if next_status == "FUTURE" else None,
        next_payment_raw=nxt, next_payment_status=next_status, rate_kind=rk,
        rate_kind_evidence=(v or {}).get("published", {}).get("rate_kind_evidence",
                                                               EV_UNKNOWN),
        settlement_status=PROJECTED, valid_for="CURRENT_PERIOD_ONLY",
        positive_pays=(v or {}).get("positive_pays"),
        sign_evidence=(v or {}).get("sign_evidence", EV_UNKNOWN),
        price_basis=(v or {}).get("price_basis"), observed_at=observed_at,
        source_ts=source_ts, evidence=evidence, raw_ref=raw_ref,
        source=f.get("source"), warnings=warnings)


def settlement(exchange_id, market_id, point, evidence="OBSERVED", raw_ref=None):
    """One settled history point (adapter row: ts, rate per PERIOD, kind, interval_s?)
    -> contract. Only kinds settled_public / account_payment are settlements; any
    other kind is refused (returns None) so a projection can never enter a history."""
    kind = point.get("kind")
    if kind == "settled_public":
        status = SETTLED_PUBLIC
    elif kind == "account_payment":
        status = ACCOUNT_PAYMENT
    else:
        return None
    v = venue(exchange_id) or {}
    iv = point.get("interval_s") or v.get("interval_s")
    rate = point.get("rate")
    ok = isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate == rate
    return dict(
        schema_version=SCHEMA_VERSION, record="settlement", exchange_id=exchange_id,
        market_id=market_id, paid_at=point.get("ts"), rate_per_period=rate if ok else None,
        rate_per_hour=rate / (iv / HOUR) if ok and iv else None, interval_s=iv,
        interval_source="POINT" if point.get("interval_s") else (
            "VENUE_DOC" if iv else "UNKNOWN"),
        settlement_status=status, valid_for="PAST_PERIOD",
        positive_pays=v.get("positive_pays"), sign_evidence=v.get("sign_evidence",
                                                                  EV_UNKNOWN),
        evidence=evidence, raw_ref=raw_ref,
        warnings=[] if ok else ["RATE_NOT_PRICED"])


def from_record(record, observed_at, evidence="OBSERVED", raw_ref=None):
    """An adapter market record (v2.venues.*.parse_markets) -> observation."""
    inst = record["instrument"]
    return observation(inst.exchange_id, inst.market_id, record.get("funding"), observed_at,
                       evidence=evidence, raw_ref=raw_ref)


def hourly(record_funding, exchange_id=None):
    """Fraction per hour from an adapter's funding dict through the one unit path, with
    the venue's documented interval when the answer did not carry one (labelled in
    `observation`). None when the unit or interval is unknown. Replaces the hard-coded
    `/100` and `or 3600` of earlier research code."""
    o = observation(exchange_id, None, record_funding, observed_at=0.0)
    return o["rate_per_hour"]


def matrix():
    """Per-venue table for reports and the Doctor (no market data)."""
    out = []
    for xid, v in VENUES.items():
        p = v["published"]
        out.append(dict(exchange_id=xid, field=p["field"], unit=p["unit"],
                        period_basis=p["period_basis"], rate_kind=p["rate_kind"],
                        rate_kind_evidence=p["rate_kind_evidence"],
                        payment_relation_known=payment_relation_known(xid),
                        interval_s=v["interval_s"], interval_evidence=v["interval_evidence"],
                        next_payment=v["next_payment"], positive_pays=v["positive_pays"],
                        sign_evidence=v["sign_evidence"], price_basis=v["price_basis"],
                        formula=v["formula"], settled_history=v["settled"],
                        account_payments=v["account"]))
    return out
