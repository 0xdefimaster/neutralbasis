"""The V2 market source: venue adapters -> identity check -> strategy rows.

PAPER / public reads. `catalog_from(answers)` turns each venue's raw public answers
(live or fixture) into market records; `build(records)` runs the group identity check
and returns the rows the paper desk ranks (wide_paper.rank / rank_spreads take the same
row shape as the legacy scanners), plus why every other market produced no row.

A row is produced only for a market in a verified MULTI_VENUE group whose funding
(rate, unit, interval) and taker fee are known and whose contract multiplier is 1 (per
unit normalization of 1000x contracts is not written yet). Rows carry the legacy venue
name (stored history and fees use it) and the V2 exchange_id and underlying_id.
"""
import collections
import time

from v2 import discovery, fees as v2fees, identity, normalize
from v2.venues import arcus, lighter_rh, risex, tradexyz, txflow, variational

FEE_UNKNOWN = "FEE_UNKNOWN"
FUNDING_UNKNOWN = "FUNDING_UNKNOWN"
MULTIPLIER_PENDING = "MULTIPLIER_NORMALIZATION_PENDING"
NOT_MULTI = "NOT_IN_VERIFIED_MULTI_VENUE_GROUP"
TIER_B = "IDENTITY_TIER_B (reported, not paper-eligible)"
ROW_REASONS = (NOT_MULTI, TIER_B, MULTIPLIER_PENDING, FUNDING_UNKNOWN, FEE_UNKNOWN)
NO_API = {
    "popdex_perps": "BLOCKED: no official public API found (docs/v2/venues/popdex_perps.md)",
    "veranta": "RESEARCH_ONLY: read-only API contract not verified (docs/v2/venues/veranta.md)",
}


def catalog_from(answers):
    """answers: {exchange_id: dict of raw answers}. Returns {exchange_id: [records]}.
    Keys used: tradexyz {meta, categories}; txflow {meta, market_tags, ctxs};
    risex {markets}; arcus {markets}; lighter_rh {markets, fundings: {market_id: raw}};
    variational {stats}."""
    out = {}
    a = answers.get("tradexyz")
    if a and a.get("meta"):
        out["tradexyz"] = tradexyz.parse_markets(a["meta"], tradexyz.categories(a.get("categories")))
    a = answers.get("txflow")
    if a and a.get("meta"):
        out["txflow"] = txflow.parse_markets(a["meta"], a.get("market_tags"), a.get("ctxs"))
    if answers.get("risex", {}).get("markets"):
        out["risex"] = risex.parse_markets(answers["risex"]["markets"])
    if answers.get("arcus", {}).get("markets"):
        out["arcus"] = arcus.parse_markets(answers["arcus"]["markets"])
    a = answers.get("lighter_rh")
    if a and a.get("markets"):
        recs = lighter_rh.parse_markets(a["markets"])
        for r in recs:
            raw = (a.get("fundings") or {}).get(r["instrument"].market_id)
            if raw:
                r["funding"] = lighter_rh.current_funding(lighter_rh.parse_fundings(raw))
        out["lighter_rh"] = recs
    if answers.get("variational", {}).get("stats"):
        out["variational"] = variational.parse_markets(answers["variational"]["stats"])
    return out


FINE = {"equity": "equity", "equities": "equity", "stock": "equity", "stocks": "equity",
        "etf": "etf", "index_etf": "etf", "commodity": "commodity",
        "commodities": "commodity", "index": "index", "indices": "index", "fx": "fx",
        "crypto": "crypto"}


def fine_class(members):
    """The most specific class the group's venues agree on: crypto, equity, etf,
    commodity, index, fx; "rwa_mixed" when venues file it differently; "rwa" when no
    venue is more specific than RWA."""
    found = {FINE[str(m.asset_class).lower()] for m in members
             if str(m.asset_class).lower() in FINE}
    if not found:
        return "rwa" if any(m.coarse == identity.RWA for m in members) else None
    if len(found) == 1:
        return found.pop()
    return "rwa_mixed"


def per_hour(f):
    """Funding as a fraction per hour from a record's funding dict, or None."""
    if not f or f.get("raw") is None or not f.get("interval_s"):
        return None
    raw = v2fees.to_fraction(f["raw"], f["unit"])
    return raw / (f["interval_s"] / 3600.0)


def build(records, registry=None, at=None, received=None):
    """(rows, groups, reasons): strategy rows, verified groups, and
    {exchange_id: Counter(reason)} for every market (one reason each).
    `received` {exchange_id: fetch time of the answers the venue's records came from}
    (Phase 15 P1) is carried as `answer_received` (None when unknown). The row's
    `received` keeps its legacy meaning (the build time `at`), because the engines'
    age gates read it; moving the gates to answer times is an owner decision."""
    at = time.time() if at is None else at
    received = received or {}
    catalog = {x: [r["instrument"] for r in recs] for x, recs in records.items()}
    groups = discovery.intersect(catalog, registry)
    group_fine = {uid: fine_class(g["members"]) for uid, g in groups.items()
                  if g.get("members") and g.get("status") == discovery.MULTI}
    rows, reasons = [], {}
    for xid, recs in records.items():
        c = reasons.setdefault(xid, collections.Counter())
        for r in recs:
            inst = r["instrument"]
            if inst.status != identity.VERIFIED or inst.reason is not None:
                c[inst.reason or identity.UNVERIFIED] += 1
                continue
            if inst.tier != "A":
                c[TIER_B] += 1
                continue
            if not inst.multiplier or inst.multiplier <= 0:
                c[MULTIPLIER_PENDING] += 1
                continue
            f = r.get("funding")
            hourly = per_hour(f)
            if hourly is None:
                c[FUNDING_UNKNOWN] += 1
                continue
            if r.get("fee_taker") is None:
                c[FEE_UNKNOWN] += 1
                continue
            c["ROW"] += 1
            raw = v2fees.to_fraction(f["raw"], f["unit"])
            rows.append(normalize.row(dict(
                asset=inst.base, venue=r["legacy_venue"], kind="perp",
                mark=r.get("mark") or inst.price, index=r.get("index"),
                rate=hourly, raw_rate=raw, interval=f["interval_s"], next=f.get("next_ts"),
                received=at, answer_received=received.get(xid), fee=r["fee_taker"],
                leverage=None, price_type="mark",
                category="RWA" if inst.coarse == identity.RWA else None,
                funding_kind=f.get("kind"), exchange_id=inst.exchange_id,
                underlying_id=inst.underlying_id, market_id=inst.market_id,
                identity_evidence=list(inst.evidence), fee_source=r.get("fee_source"),
                fine_class=group_fine.get(inst.underlying_id), session=inst.session,
                outside_rth=r.get("outside_rth"), symbol=inst.symbol,
            ), inst.multiplier))
    return rows, groups, reasons


def venue_table(records, rows, groups, reasons):
    """Per venue: markets, verified in multi-venue groups, rows, pair partners."""
    partners = collections.defaultdict(collections.Counter)
    for uid, g in groups.items():
        if g.get("status") == discovery.MULTI:
            for a, b in discovery.pairs(g["venues"])[0]:
                partners[a][b] += 1
                partners[b][a] += 1
    out = {}
    for xid in sorted(set(records) | set(NO_API)):
        recs = records.get(xid, [])
        c = reasons.get(xid, collections.Counter())
        out[xid] = dict(
            markets=len(recs),
            active=sum(r["instrument"].active for r in recs),
            verified_multi=sum(1 for r in recs if r["instrument"].status == identity.VERIFIED
                               and r["instrument"].reason is None),
            rows=c.get("ROW", 0),
            partners=dict(partners.get(xid, {})),
            reasons=dict(c),
            api=NO_API.get(xid, "public read-only adapter (v2/venues/%s.py)" % xid),
        )
    return out
