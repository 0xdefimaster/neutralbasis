"""Normalized market-data contract: an AUDIT and adapters, not a new event bus.
Phase 14 §4A defined v1; Phase 15 P1 extends it (v2, backward-compatible: every v1
field keeps its meaning, new fields are added and default to None = UNKNOWN).

Existing producers keep their own shapes; `normalize_*` maps them onto the contract.
Missing values stay None; nothing is defaulted, interpolated or invented.

v1 fields:
  exchange_id, market_id, instrument_id, data_type (book / ctx / quote / funding /
  trades / history / stats), source_ts (s), source_ts_raw, source_unit (ms / s / us /
  ns / iso / NOT_STATED / NOT_PUBLISHED), received_ts (s, local wall clock), sequence,
  prev_sequence, kind (snapshot / delta / full), raw_ref (where the raw answer is
  kept), evidence (OBSERVED / HISTORICAL / MOCK / FAILED / SIMULATED).
v2 fields (Phase 15 P1):
  schema_version          SCHEMA_VERSION
  canonical_underlying_id v2.identity underlying id (None unless identity-verified)
  channel                 transport channel (l2BookDepth, metaAndAssetCtxs, ...)
  monotonic_receive_ts    time.monotonic() at receipt (interval-safe; not a clock)
  integrity               INTEGRITY values (a missing sequence is a SUSPICION, never
                          a confirmed loss)
  funding_evidence        for ctx/funding rows: rate kind, settlement status, valid-for,
                          warnings (v2/funding_semantics) - a projection is never a
                          payment
  quote_type              ORDERBOOK / RFQ_INDICATIVE / MARK_ONLY / NONE
  execution_model         PAPER only: SIMULATED_ORDERBOOK / RFQ_INDICATIVE_UNVERIFIED /
                          NONE (no real execution exists)
  multiplier              contract multiplier (None = unknown; prices are per unit only
                          after v2/normalize)
"""
import math

from v2 import funding_semantics as fs
from v2.paper_obs import source_time

SCHEMA_VERSION = "md-contract/2"
FIELDS_V1 = ("exchange_id", "market_id", "instrument_id", "data_type", "source_ts",
             "source_ts_raw", "source_unit", "received_ts", "sequence", "prev_sequence",
             "kind", "raw_ref", "evidence")
FIELDS = FIELDS_V1 + ("schema_version", "canonical_underlying_id", "channel",
                      "monotonic_receive_ts", "integrity", "funding_evidence",
                      "quote_type", "execution_model", "multiplier")
EVIDENCE = ("OBSERVED", "HISTORICAL", "MOCK", "FAILED", "SIMULATED")
SOURCE_UNITS = ("ms", "s", "us", "ns", "iso", "NOT_STATED", "NOT_PUBLISHED")
INTEGRITY = ("OK", "NO_SEQUENCE", "DUPLICATE", "STALE", "RESYNC_REQUESTED",
             "SEQUENCE_GAP_SUSPECTED", "CROSSED", "EMPTY", "IGNORED", "FAILED",
             "STATE_LIMIT", "UNKNOWN")
QUOTE_TYPES = ("ORDERBOOK", "RFQ_INDICATIVE", "MARK_ONLY", "NONE")
EXECUTION_MODELS = ("SIMULATED_ORDERBOOK", "RFQ_INDICATIVE_UNVERIFIED", "NONE")

# Quote type of each venue's price source (docs/v2/42 capability matrix).
VENUE_QUOTE = dict(variational="RFQ_INDICATIVE", popdex_perps="NONE", veranta="NONE",
                   phoenix_perps="NONE", qfex="NONE")
EXECUTION_OF = dict(ORDERBOOK="SIMULATED_ORDERBOOK", RFQ_INDICATIVE="RFQ_INDICATIVE_UNVERIFIED",
                    MARK_ONLY="NONE", NONE="NONE")

# What the existing producers carry today (audited 2026-11-13, updated for P1;
# "derived" = computed from what they keep, "missing" = not kept at all).
AUDIT = {
    "v2.ws_readonly.Books.record": dict(
        exchange_id="yes (venue key; hyperliquid covers native + tradeXYZ)",
        market_id="yes (coin / id / channel suffix)", instrument_id="txflow alias only",
        data_type="yes (channel)", source_ts="yes (s, converted from ms)",
        source_unit="derived (ms for txflow/hyperliquid; NOT_STATED for lighter_rh)",
        received_ts="yes (recv_ts)", sequence="txflow seq / lighter nonce; none for HL",
        kind="txflow/lighter snapshot|delta; HL full snapshot", raw_ref="first 2 frames only",
        evidence="implicit (live or MOCK by caller)",
        monotonic_receive_ts="yes (recv_mono, P1)",
        integrity="status ok / duplicate / stale / resync / ignored"),
    "tools.v2_stream answers": dict(
        exchange_id="yes (xid)", market_id="in request body", instrument_id="missing",
        data_type="answer name", source_ts="yes for books/ctx (source_ts), none for ctx "
        "of HL", source_unit="field name only (book.time ms)", received_ts="yes (t0, t1)",
        sequence="missing (REST)", kind="full", raw_ref="stream.jsonl#cycle",
        evidence="OBSERVED (label)", monotonic_receive_ts="missing",
        integrity="NO_SEQUENCE (REST full answers)"),
    "v2.paper_obs.Observation": dict(
        exchange_id="yes (rows)", market_id="yes", instrument_id="missing",
        data_type="rows/books", source_ts="times[(venue, asset)] (mixed SOURCE / "
        "RECEIVE_TIME, labelled in time_basis)", source_unit="s", received_ts="answers t1",
        sequence="missing", kind="full", raw_ref="origin", evidence="OBSERVED",
        monotonic_receive_ts="missing", integrity="NO_SEQUENCE"),
    "v2.collector rows / books": dict(
        exchange_id="yes", market_id="yes", instrument_id="underlying_id",
        data_type="row / book", source_ts="books: venue time where parsed; rows: missing",
        source_unit="per venue (v2/control/capability SOURCE_TS)",
        received_ts="answer_received = per ANSWER fetch time (P1; rows: oldest input of "
        "the venue); legacy `received` (build / read time) kept for the engines' gates",
        sequence="arcus lastSequenceId only", kind="full", raw_ref="recorder sink",
        evidence="OBSERVED live / HISTORICAL replay",
        monotonic_receive_ts="books: received_mono (P1); rows: missing",
        integrity="books: crossed / empty flags"),
}
UNITS = dict(txflow="ms", hyperliquid="ms", lighter_rh="NOT_STATED", variational="iso")
WS_INTEGRITY = dict(ok="OK", duplicate="DUPLICATE", stale="STALE", resync="RESYNC_REQUESTED",
                    ignored="IGNORED", state_limit="STATE_LIMIT")


def _row(**kw):
    out = dict.fromkeys(FIELDS)
    out["schema_version"] = SCHEMA_VERSION
    out.update(kw)
    return out


def quote_type(exchange_id):
    return VENUE_QUOTE.get(exchange_id, "ORDERBOOK")


def normalize_ws(rec, venue, evidence="OBSERVED", instrument_id=None,
                 canonical_underlying_id=None):
    """A v2.ws_readonly record -> contract dict (the record itself is unchanged)."""
    unit = UNITS.get(venue, "NOT_STATED")
    xid = rec.get("venue") or venue
    integ = WS_INTEGRITY.get(rec.get("status"), "UNKNOWN")
    if integ == "OK" and rec.get("seq") is None:
        integ = "NO_SEQUENCE"
    qt = quote_type(xid)
    return _row(exchange_id=xid, market_id=rec.get("market"),
                instrument_id=instrument_id, data_type=rec.get("channel"),
                source_ts=rec.get("source_ts"),
                source_ts_raw=rec.get("source_raw"), source_unit=unit,
                received_ts=rec.get("recv_ts"), sequence=rec.get("seq"),
                prev_sequence=rec.get("prev_seq"), kind=rec.get("kind") or (
                    "full" if rec.get("channel") == "l2Book" else None),
                raw_ref=None, evidence=evidence,
                canonical_underlying_id=canonical_underlying_id,
                channel=rec.get("channel"), monotonic_receive_ts=rec.get("recv_mono"),
                integrity=integ, quote_type=qt, execution_model=EXECUTION_OF[qt])


def normalize_answer(name, ans, raw_ref=None, evidence="OBSERVED"):
    """One saved REST answer (tools.v2_stream / tools.v2_aligned) -> contract dict."""
    st = ans.get("source_ts")
    if st is None and ans.get("ok"):
        st = source_time(name, ans.get("data"))
    dt = ("book" if "book" in name else "ctx" if "ctx" in name else
          "stats" if "stats" in name else "history" if "_fh_" in name else name)
    qt = quote_type(ans.get("xid")) if dt in ("book", "stats") else (
        "MARK_ONLY" if dt == "ctx" else "NONE")
    return _row(exchange_id=ans.get("xid"), market_id=name.rsplit("_", 1)[-1]
                if "_" in name else None, instrument_id=None, data_type=dt,
                source_ts=st, source_ts_raw=None,
                source_unit="ms" if st is not None else "NOT_PUBLISHED",
                received_ts=ans.get("t1"), sequence=None, prev_sequence=None, kind="full",
                raw_ref=raw_ref, evidence=evidence if ans.get("ok") else "FAILED",
                channel=name, monotonic_receive_ts=ans.get("t1_mono"),
                integrity="NO_SEQUENCE" if ans.get("ok") else "FAILED",
                quote_type=qt, execution_model=EXECUTION_OF[qt])


def normalize_market(record, received_ts, evidence="OBSERVED", raw_ref=None,
                     monotonic_receive_ts=None):
    """An adapter market record (v2.venues.*.parse_markets) -> ctx contract row with
    its funding evidence. Identity fields come from the record's Instrument."""
    inst = record["instrument"]
    fo = fs.observation(inst.exchange_id, inst.market_id, record.get("funding"),
                        observed_at=received_ts if received_ts is not None else 0.0,
                        evidence=evidence, raw_ref=raw_ref)
    fev = {k: fo[k] for k in ("rate_per_hour", "rate_kind", "rate_kind_evidence",
                              "settlement_status", "valid_for", "next_payment_status",
                              "interval_source", "sign_evidence", "warnings")}
    if record.get("funding") is None:
        fev = dict(fev, warnings=["NO_FUNDING_PUBLISHED"])
    qt = quote_type(inst.exchange_id)
    return _row(exchange_id=inst.exchange_id, market_id=inst.market_id,
                instrument_id=getattr(inst, "symbol", None), data_type="ctx",
                source_unit="NOT_PUBLISHED", received_ts=received_ts, kind="full",
                raw_ref=raw_ref, evidence=evidence,
                canonical_underlying_id=getattr(inst, "underlying_id", None),
                channel="markets", monotonic_receive_ts=monotonic_receive_ts,
                integrity="NO_SEQUENCE", funding_evidence=fev, quote_type=qt,
                execution_model=EXECUTION_OF[qt], multiplier=getattr(inst, "multiplier",
                                                                     None))


def normalize_book(book, exchange_id, market_id, evidence="OBSERVED", raw_ref=None,
                   canonical_underlying_id=None):
    """A normalized book (v2.venues.base.book / collector.book) -> contract dict."""
    integ = ("CROSSED" if book.get("crossed") else "EMPTY" if book.get("empty") else
             "OK" if book.get("seq") is not None else "NO_SEQUENCE")
    model = book.get("model")
    qt = "RFQ_INDICATIVE" if model == "rfq_indicative" else quote_type(exchange_id)
    return _row(exchange_id=exchange_id, market_id=market_id, data_type="book",
                source_ts=book.get("ts"), source_unit="s" if book.get("ts") is not None
                else "NOT_PUBLISHED",
                received_ts=book.get("answer_received") or book.get("received"),
                sequence=book.get("seq"), kind="full", raw_ref=raw_ref, evidence=evidence,
                canonical_underlying_id=canonical_underlying_id, channel=book.get("source"),
                monotonic_receive_ts=book.get("received_mono"), integrity=integ,
                quote_type=qt, execution_model=EXECUTION_OF[qt],
                multiplier=book.get("multiplier"))


MAX_FUTURE_SKEW_S = 5.0          # source time ahead of receipt beyond this: suspicious
MAX_SOURCE_AGE_S = 86400.0       # older than a day at receipt: implausible for live data


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def validate(row):
    """Problems of one contract row (empty list = valid). Never repairs anything."""
    p = []
    for k in FIELDS:
        if k not in row:
            p.append("MISSING_FIELD:%s" % k)
    if row.get("schema_version") != SCHEMA_VERSION:
        p.append("SCHEMA_VERSION")
    if row.get("evidence") not in EVIDENCE:
        p.append("EVIDENCE_UNLABELLED")
    if not row.get("exchange_id"):
        p.append("EXCHANGE_ID_MISSING")
    if row.get("source_unit") is not None and row["source_unit"] not in SOURCE_UNITS:
        p.append("SOURCE_UNIT_UNKNOWN")
    st, rt = row.get("source_ts"), row.get("received_ts")
    if st is not None and not _num(st):
        p.append("SOURCE_TS_NOT_NUMERIC")
    elif st is not None:
        if st > 1e11:
            p.append("SOURCE_TS_UNIT_SUSPECT")          # ms / us / ns stored as seconds
        if rt is None:
            p.append("RECEIVE_TIME_MISSING")
        elif _num(rt):
            if st - rt > MAX_FUTURE_SKEW_S:
                p.append("SOURCE_AHEAD_OF_RECEIPT")     # clock offset or unit error
            if rt - st > MAX_SOURCE_AGE_S:
                p.append("SOURCE_TIME_IMPLAUSIBLE")
    if row.get("integrity") is not None and row["integrity"] not in INTEGRITY:
        p.append("INTEGRITY_UNKNOWN_VALUE")
    qt, em = row.get("quote_type"), row.get("execution_model")
    if qt is not None and qt not in QUOTE_TYPES:
        p.append("QUOTE_TYPE_UNKNOWN_VALUE")
    if em is not None and em not in EXECUTION_MODELS:
        p.append("EXECUTION_MODEL_UNKNOWN_VALUE")
    if qt is not None and em is not None and EXECUTION_OF.get(qt) != em:
        p.append("EXECUTION_MODEL_MISMATCH")            # e.g. an RFQ priced as a book
    m = row.get("multiplier")
    if m is not None and (not _num(m) or m <= 0):
        p.append("MULTIPLIER_INVALID")
    fev = row.get("funding_evidence")
    if fev:
        if fev.get("settlement_status") == fs.PROJECTED and \
                fev.get("valid_for") != "CURRENT_PERIOD_ONLY":
            p.append("PROJECTION_NOT_LIMITED_TO_CURRENT_PERIOD")
        if fev.get("settlement_status") in (fs.SETTLED_PUBLIC, fs.ACCOUNT_PAYMENT) and \
                fev.get("valid_for") != "PAST_PERIOD":
            p.append("SETTLEMENT_NOT_PAST")
    return p


def sequence_check(rows):
    """Integrity of one (exchange, market, channel) stream of contract rows in
    receive order. dict(duplicates, regressions, gaps_suspected, out_of_order_source,
    no_sequence). A gap is only a SUSPICION (the venue may skip numbers); it is never
    reported as a confirmed loss."""
    out = dict(rows=len(rows), duplicates=0, regressions=0, gaps_suspected=0,
               out_of_order_source=0, no_sequence=0)
    last_seq, last_src = None, None
    seen = set()
    for r in rows:
        s, prev, st = r.get("sequence"), r.get("prev_sequence"), r.get("source_ts")
        if s is None:
            out["no_sequence"] += 1
        else:
            if s in seen:
                out["duplicates"] += 1
            elif last_seq is not None and s < last_seq:
                out["regressions"] += 1
            elif last_seq is not None and prev is not None and prev != last_seq:
                out["gaps_suspected"] += 1
            seen.add(s)
            last_seq = s if last_seq is None else max(last_seq, s)
        if st is not None and last_src is not None and st < last_src:
            out["out_of_order_source"] += 1
        if st is not None:
            last_src = st if last_src is None else max(last_src, st)
    return out


def coverage(rows):
    """Share of non-None values per field over rows (what a producer really fills)."""
    n = len(rows)
    return {k: (sum(1 for r in rows if r.get(k) is not None) / n if n else None)
            for k in FIELDS}
